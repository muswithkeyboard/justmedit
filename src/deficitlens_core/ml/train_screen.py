"""Обучение итоговой модели скрининга screen-v1 (скрытый дефицит железа по ОАК) на NHANES.

Что делает train():
  * когорта: взрослые 18+, не беременные, полный ОАК, без анемии по ВОЗ (Hb ≥ 120 г/л у женщин, ≥ 130 г/л
    у мужчин), измерен ферритин (мкг/л) — ВСЕ циклы NHANES с ферритином (временная проверка на отложенных
    циклах 2017–2023 считается отдельно в evaluate_screen.py);
  * признаки: ОАК, стандартизованный внутри цикла по полу, + возраст + пол; два набора — полный ОАК и один Hb;
  * четыре модели: (ферритин < 15, < 30 мкг/л) × (full_cbc, hb_only); бустинг + изотоническая калибровка;
  * пороги направлений по p_lt15: для каждой группы (Ж 18–49, Ж 50+, М) и набора признаков — значение
    вероятности, выше которого лежит заданная доля людей группы. Считаются по предсказаниям итоговой модели
    для людей без анемии из циклов 2017–2023 с весами обследования; меток (ферритина) не используют;
  * сохраняет файлы моделей (joblib) и metadata.json: версии, контрольные суммы, размеры когорт, пороги,
    сводку метрик временной проверки и эталонные предсказания для проверки сохранённой модели.
В каталог модели попадают только параметры модели и сводные числа — ни одной строки NHANES.
"""
from __future__ import annotations

import json
import os
import platform
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from ..config import REPO_ROOT
from .screen import (ADULT_AGE, CALIBRATION, DEFAULT_REFERENCE, FEATURE_LABS, GROUPS, HGB_PARAMS, HIGH_SHARE, TARGETS,
                     TEST_CYCLES, THRESHOLD_SHARES, VERSION, WHO_HB_G_L, ScreenModel, feature_matrix, feature_names,
                     fit_model, load_cohort, model_file_name, sha256_file, top_share_threshold, wmean)

METRICS_JSON = REPO_ROOT / "docs" / "metrics" / "screen_metrics.json"

# Проверочные примеры — выдуманные анализы, не строки NHANES. Их предсказания (со справочником "default")
# сохраняются в metadata.json; тест сверяет с ними загруженную модель (до 1e-5).
REFERENCE_CASES = [
    {"name": "Ж 34, полный ОАК: Hb у нижней границы, RDW повышен", "sex": "F", "age_years": 34,
     "values": {"hemoglobin": 124, "RBC": 4.59, "hematocrit": 37.7, "MCV": 82, "MCH": 27.0, "MCHC": 329,
                "RDW": 15.2, "platelets": 310, "WBC": 6.1}},
    {"name": "Ж 34, только гемоглобин", "sex": "F", "age_years": 34, "values": {"hemoglobin": 124}},
    {"name": "Ж 29, полный ОАК без отклонений", "sex": "F", "age_years": 29,
     "values": {"hemoglobin": 136, "RBC": 4.5, "hematocrit": 40.1, "MCV": 89, "MCH": 30.2, "MCHC": 339,
                "RDW": 13.2, "platelets": 255, "WBC": 6.8}},
    {"name": "Ж 62, полный ОАК без отклонений", "sex": "F", "age_years": 62,
     "values": {"hemoglobin": 131, "RBC": 4.4, "hematocrit": 39.5, "MCV": 90, "MCH": 30.1, "MCHC": 334,
                "RDW": 13.9, "platelets": 240, "WBC": 6.4}},
    {"name": "М 45, полный ОАК без отклонений", "sex": "M", "age_years": 45,
     "values": {"hemoglobin": 148, "RBC": 4.95, "hematocrit": 43.9, "MCV": 89, "MCH": 30.2, "MCHC": 339,
                "RDW": 13.4, "platelets": 228, "WBC": 6.6}},
]


def _validation_summary(nhanes_path: str, data_sha256: str) -> dict:
    """Сводка временной проверки: из docs/metrics/screen_metrics.json (если он посчитан по тем же данным),
    иначе — вызов evaluate()."""
    summary, source = None, None
    try:
        saved = json.loads(METRICS_JSON.read_text(encoding="utf-8"))
        if saved.get("data", {}).get("sha256") == data_sha256:
            summary, source = saved["summary"], "docs/metrics/screen_metrics.json"
    except (OSError, ValueError, KeyError):
        pass
    if summary is None:
        from .evaluate_screen import evaluate

        summary, source = evaluate(nhanes_path)["summary"], "evaluate()"
    keep = ("validation", "n_train", "n_test", "by_group", "hb_only_F18-49", "referral_F18-49_t15",
            "baseline_F18-49_t15", "rule_vs_model_F18-49_t15", "application_path_F18-49", "gates",
            "price_ferritin_rub")
    return {"source": source, **{k: summary[k] for k in keep if k in summary}}


def train(nhanes_path: str, out_dir: str) -> dict:
    """Обучает итоговые модели на всех циклах с ферритином и сохраняет их в out_dir. Возвращает краткую сводку."""
    import joblib
    import sklearn

    t0 = time.time()
    adults, cohort, counts = load_cohort(nhanes_path)
    na = cohort[cohort.anemia_who == 0]       # когорта модели: без анемии по ВОЗ, ферритин измерен

    # --- четыре модели -----------------------------------------------------------------------
    models = {}
    for tgt in TARGETS:
        for features in FEATURE_LABS:
            models[(tgt, features)] = fit_model(feature_matrix(na, features), na[tgt].to_numpy())

    # --- пороги направлений (взвешенные квантили p_lt15 по группам; метки не нужны) ------------
    # Люди без анемии из циклов 2017–2023 — все небеременные взрослые с полным ОАК (ферритин для порога
    # не нужен: это доля людей, а не доля случаев). Стандартизация — внутри цикла, как при обучении.
    pop = adults[(adults.anemia_who == 0) & adults.is_test]
    weights = pop.w.to_numpy()
    by_features: dict = {}
    p15_by_features: dict = {}
    for features in FEATURE_LABS:
        p15 = models[("t15", features)].predict_proba(feature_matrix(pop, features))[:, 1]
        p15_by_features[features] = p15
        by_features[features] = {}
        for grp in GROUPS:
            k = (pop.grp == grp).to_numpy()
            rows = []
            for share in THRESHOLD_SHARES:
                thr, realized = top_share_threshold(p15[k], weights[k], share)
                rows.append({"share": share, "threshold": thr, "share_at_or_above": round(realized, 4)})
            by_features[features][grp] = rows
    thresholds = {
        "target": "p_lt15",
        "rule": "p_lt15 ≥ threshold",
        "shares": list(THRESHOLD_SHARES),
        "high_share": HIGH_SHARE,
        "population": ("NHANES " + ", ".join(TEST_CYCLES) + ": взрослые 18+, не беременные, полный ОАК, "
                       "без анемии по ВОЗ (с ферритином и без него); веса mec_weight; "
                       "стандартизация внутри цикла по полу"),
        "note": ("взвешенные квантили предсказаний итоговой модели; при совпадающих вероятностях берётся ближайшая "
                 "достижимая доля — фактическая доля людей на уровне порога и выше записана в share_at_or_above"),
        "n_by_group": {grp: int((pop.grp == grp).sum()) for grp in GROUPS},
        "by_features": by_features,
    }

    # --- файлы модели: сначала во временные имена, затем быстрая замена (каталог не остаётся «наполовину новым») ---
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    files, tmp_paths = {}, {}
    for (tgt, features), model in models.items():
        name = model_file_name(tgt, features)
        tmp = out / (name + ".tmp")
        joblib.dump(model, tmp, compress=3)
        files[name] = {"sha256": sha256_file(tmp), "bytes": int(tmp.stat().st_size)}
        tmp_paths[name] = tmp

    data_sha256 = sha256_file(nhanes_path)
    metadata = {
        "version": VERSION,
        "created": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "description": ("Скрининг скрытого дефицита железа по общему анализу крови: вероятность ферритина "
                        "ниже 15 и ниже 30 мкг/л у взрослых с нормальным гемоглобином. Прототип хакатона, "
                        "не медицинское изделие; данные обучения — США (NHANES)."),
        "scope": {"age_min": ADULT_AGE, "pregnant": False, "anemia_who": False, "who_hb_g_l": WHO_HB_G_L,
                  "ferritin_measured": "не сдан (иначе запас железа оценивается по измеренному значению)",
                  "validated_groups": ["F18-49"]},
        "targets": {"t15": {"ferritin_below_ug_l": TARGETS["t15"], "output": "p_lt15", "source": "ВОЗ, 2020"},
                    "t30": {"ferritin_below_ug_l": TARGETS["t30"], "output": "p_lt30",
                            "source": "порог практики (КР ЖДА, 2024)"}},
        "features": {f: feature_names(f) for f in FEATURE_LABS},
        "units": {"hemoglobin": "г/л", "RBC": "10^12/л", "hematocrit": "%", "MCV": "фл", "MCH": "пг", "MCHC": "г/л",
                  "RDW": "%", "platelets": "10^9/л", "WBC": "10^9/л", "age_years": "лет", "female": "1 — женщина"},
        "standardization": {
            "formula": "z = (x − медиана) / (q75 − q25), отдельно по полу, без меток",
            "training": "внутри цикла NHANES — по всем небеременным взрослым цикла с полным ОАК",
            "application": f"по справочнику лаборатории config/lab_reference.yaml (по умолчанию '{DEFAULT_REFERENCE}')",
        },
        "algorithm": {
            "model": "CalibratedClassifierCV(HistGradientBoostingClassifier, method='isotonic', cv=5)",
            "hgb": HGB_PARAMS, "calibration": CALIBRATION, "sample_weight": None,
            "post_processing": "p_lt30 = max(p_lt30, p_lt15) — вероятности двух моделей согласуются при выдаче",
            "n_iter": {model_file_name(t, f): [int(c.estimator.n_iter_) for c in m.calibrated_classifiers_]
                       for (t, f), m in models.items()},
        },
        "data": {"file": Path(nhanes_path).name, "sha256": data_sha256, "rows": counts["rows_total"],
                 "cycles": sorted(str(c) for c in na.nhanes_cycle.unique())},
        "cohort": {**counts, "train_rows": int(len(na)),
                   "train_by_group": {grp: int((na.grp == grp).sum()) for grp in GROUPS},
                   "train_cases": {tgt: int(na[tgt].sum()) for tgt in TARGETS},
                   "train_prevalence_unweighted": {tgt: round(float(na[tgt].mean()), 4) for tgt in TARGETS}},
        "files": files,
        "versions": {"python": platform.python_version(), "scikit-learn": sklearn.__version__, "numpy": np.__version__,
                     "joblib": joblib.__version__},
        "thresholds": thresholds,
        "validation": _validation_summary(nhanes_path, data_sha256),
        "limitations": [
            "Данные обучения — США (NHANES); на российской популяции модель не проверялась.",
            ("Проверена для женщин 18–49 лет; у женщин 50+ и мужчин случаев мало, "
             "преимущество перед простым правилом не показано."),
            ("Пороги направлений заданы долей людей группы: у мужчин и женщин 50+ "
             "им соответствуют очень малые вероятности."),
            "Не применяется при анемии, беременности и у людей младше 18 лет.",
            "Оценка по модели, а не измерение ферритина; не медицинское изделие.",
        ],
    }
    # Эталонные предсказания: та же функция predict, что в сервисе (справочник лаборатории "default").
    model = ScreenModel(models, metadata, out)
    reference = []
    for case in REFERENCE_CASES:
        r = model.predict(case["values"], case["sex"], case["age_years"])
        reference.append({**case, "expected": {"p_lt15": r["p_lt15"], "p_lt30": r["p_lt30"], "features": r["features"],
                                               "group": r["group"], "refer": r["thresholds"]["refer"],
                                               "high": r["thresholds"]["high"]}})
    metadata["reference_predictions"] = {"lab_reference": DEFAULT_REFERENCE, "refer_share": 0.30, "cases": reference}
    metadata["fast_path_checked"] = bool(model.fast_path)

    for name, tmp in tmp_paths.items():
        os.replace(tmp, out / name)
    meta_tmp = out / "metadata.json.tmp"
    meta_tmp.write_text(json.dumps(metadata, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(meta_tmp, out / "metadata.json")

    def at_share(share: float) -> dict:
        """Пороги по p_lt15 для заданной доли: набор признаков -> группа -> порог (округлён для печати)."""
        return {f: {g: round(next(r["threshold"] for r in rows if abs(r["share"] - share) < 1e-9), 4)
                    for g, rows in by_g.items()} for f, by_g in by_features.items()}

    group_mask = {grp: (pop.grp == grp).to_numpy() for grp in GROUPS}
    return {
        "version": VERSION,
        "out_dir": str(out),
        "files_bytes": {name: info["bytes"] for name, info in files.items()},
        "metadata_bytes": int((out / "metadata.json").stat().st_size),
        "train_rows": int(len(na)),
        "train_cases": metadata["cohort"]["train_cases"],
        "refer_threshold_p_lt15_share_0.30": at_share(0.30),
        "high_threshold_p_lt15_share_0.10": at_share(HIGH_SHARE),
        "example": {"case": REFERENCE_CASES[0]["name"], **{k: (round(v, 4) if isinstance(v, float) else v)
                                                           for k, v in reference[0]["expected"].items()}},
        "validation_source": metadata["validation"]["source"],
        "mean_p_lt15_weighted_2017_2023": {grp: round(wmean(p15_by_features["full_cbc"][k], weights[k]), 4)
                                           for grp, k in group_mask.items()},
        "fast_path": bool(model.fast_path),
        "seconds": round(time.time() - t0, 1),
    }
