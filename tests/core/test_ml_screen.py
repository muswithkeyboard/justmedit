"""Тесты скрининга скрытого дефицита железа по ОАК (ml/screen.py, модель screen-v1).

Ворота читаются из docs/metrics/screen_metrics.json (его пишет `deficitlens evaluate --what screen`),
модель — из models/screen-v1 (её пишет `deficitlens train --what screen`). Без этих файлов тесты дают SKIP.
Тесты, которым нужны строки NHANES, без файла данных тоже дают SKIP.
"""
from __future__ import annotations

import copy
import csv
import json
import shutil
import tempfile
from pathlib import Path

import numpy as np

import helpers
from helpers import ROOT

from deficitlens_core.config import config_dir, models_dir
from deficitlens_core.constants import CBC_LABS
from deficitlens_core.ml import screen
from deficitlens_core.ml.screen import ScreenModel, build_lab_reference

METRICS = ROOT / "docs" / "metrics" / "screen_metrics.json"

# Пример из постановки: женщина 34 лет, Hb у нижней границы нормы, RDW повышен, MCV и MCH у нижней границы.
EXAMPLE = {"hemoglobin": 124, "RBC": 4.59, "hematocrit": 37.7, "MCV": 82, "MCH": 27.0, "MCHC": 329,
           "RDW": 15.2, "platelets": 310, "WBC": 6.1}

_CACHE: dict = {}


def _model() -> ScreenModel:
    if "model" not in _CACHE:
        if not (models_dir() / "screen-v1" / "metadata.json").exists():
            helpers.skip("модель screen-v1 не обучена: "
                         "PYTHONPATH=src python3 -m deficitlens_core.cli train --what screen")
        _CACHE["model"] = ScreenModel.load()
    return _CACHE["model"]


def _metrics() -> dict:
    if "metrics" not in _CACHE:
        if not METRICS.exists():
            helpers.skip("нет docs/metrics/screen_metrics.json: "
                         "PYTHONPATH=src python3 -m deficitlens_core.cli evaluate --what screen")
        _CACHE["metrics"] = json.loads(METRICS.read_text(encoding="utf-8"))
    return _CACHE["metrics"]


def _metric(target: str, group: str, features: str = "full_cbc", scaling: str = "z") -> dict:
    rows = [r for r in _metrics()["metrics"]
            if (r["target"], r["group"], r["features"], r["scaling"]) == (target, group, features, scaling)]
    assert len(rows) == 1, f"в метриках нет строки {target} / {group} / {features} / {scaling}"
    return rows[0]


def _default_reference() -> dict:
    return copy.deepcopy(screen.load_lab_references()["references"]["default"])


def _synthetic_rows(n: int, seed: int) -> list[dict]:
    """Выдуманные анализы вокруг справочника "default" (не строки NHANES): значения ОАК, пол, возраст; Hb в норме."""
    rng = np.random.default_rng(seed)
    by_sex = _default_reference()["by_sex"]
    rows = []
    for _ in range(n):
        sex = "F" if rng.random() < 0.6 else "M"
        values = {}
        for c in CBC_LABS:
            r = by_sex[sex][c]
            values[c] = float(r["median"] + rng.normal(0, 1.2) * (r["q75"] - r["q25"]))
        values["hemoglobin"] = max(values["hemoglobin"], screen.WHO_HB_G_L[sex] + 1.0)
        rows.append({"sex": sex, "age_years": int(rng.integers(18, 86)), "values": values})
    return rows


def _write_export(path: Path, n_f: int, n_m: int, seed: int = 0, header_upper: bool = True) -> dict:
    """Выдуманная выгрузка ОАК лаборатории (CSV). Возвращает сгенерированные значения по полу для сверки квартилей."""
    rng = np.random.default_rng(seed)
    centers = {"F": [131, 4.4, 39.2, 88.0, 29.6, 334, 13.0, 262, 6.6],
               "M": [149, 4.9, 44.0, 89.0, 30.3, 339, 12.9, 230, 6.5]}
    spread = [9, 0.35, 2.8, 4.5, 1.8, 8, 0.9, 55, 1.7]
    data = {"F": [], "M": []}
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["SEX"] + [c.upper() if header_upper else c for c in CBC_LABS])
        for sex, n in (("F", n_f), ("M", n_m)):
            for _ in range(n):
                row = [round(float(m + rng.normal(0, 1) * s), 2) for m, s in zip(centers[sex], spread)]
                data[sex].append(row)
                w.writerow([sex] + row)
    return {s: np.array(v) for s, v in data.items()}


# --------------------------------------------------------------------------------------------
# Ворота (план, §14): женщины 18–49, тест 2017–2023, полный ОАК со стандартизацией
# --------------------------------------------------------------------------------------------
def test_gate_auc_women_18_49():
    auc15, auc30 = _metric("t15", "F18-49")["auc"], _metric("t30", "F18-49")["auc"]
    assert auc15 >= 0.80, f"AUC для ферритина < 15 у женщин 18–49: {auc15} < 0.80"
    assert auc30 >= 0.70, f"AUC для ферритина < 30 у женщин 18–49: {auc30} < 0.70"


def test_gate_calibration_in_the_large():
    for target in ("t15", "t30"):
        for group in ("ALL", "F18-49"):
            r = _metric(target, group)
            gap = abs(r["mean_predicted"] - r["observed"])
            assert gap <= 0.03, f"{target} / {group}: предсказано {r['mean_predicted']}, наблюдается {r['observed']}"


def test_metrics_match_prototype():
    """Ключевые числа прототипа (RESULTS_nhanes.md): AUC 0,822 и 0,727; предсказано 0,303 против наблюдаемых 0,289."""
    r15, r30 = _metric("t15", "F18-49"), _metric("t30", "F18-49")
    assert abs(r15["auc"] - 0.822) <= 0.005 and abs(r30["auc"] - 0.727) <= 0.005
    assert abs(r30["mean_predicted"] - 0.303) <= 0.005 and abs(r30["observed"] - 0.289) <= 0.005
    assert (r15["n"], r15["cases"], r30["cases"]) == (2180, 168, 629)
    check = _metrics()["summary"]["prototype_check"]
    if check.get("available"):
        assert check["metrics_max_abs_diff"] <= 0.02 and check["referral_max_abs_diff"] <= 0.02 and not check["missing"]


def test_metrics_have_no_patient_rows():
    """В файле метрик только сводные числа: все таблицы короткие, строк NHANES нет."""
    m = _metrics()
    assert set(["summary", "metrics", "referral", "rule_comparison", "calibration_shift", "cohort"]) <= set(m)
    assert len(m["metrics"]) <= 40
    assert METRICS.stat().st_size < 200_000


# --------------------------------------------------------------------------------------------
# Модель: один анализ
# --------------------------------------------------------------------------------------------
def test_example_woman_34_is_above_refer_threshold():
    r = _model().predict(EXAMPLE, "F", 34)
    assert r["features"] == "full_cbc" and r["group"] == "F18-49"
    assert 0.0 < r["p_lt15"] < 1.0 and 0.0 < r["p_lt30"] < 1.0
    assert r["p_lt15"] > r["thresholds"]["refer"], r
    assert r["thresholds"]["high"] >= r["thresholds"]["refer"] > 0.0
    assert set(r) >= {"p_lt15", "p_lt30", "features", "group", "thresholds"}


def test_hemoglobin_only_uses_fallback_model():
    m = _model()
    r = m.predict({"hemoglobin": 124}, "F", 34)
    assert r["features"] == "hb_only" and r["group"] == "F18-49"
    assert 0.0 < r["p_lt15"] < 1.0 and 0.0 < r["p_lt30"] < 1.0
    # неполный ОАК (есть Hb и часть индексов) — тоже запасная модель, результат тот же, что по одному Hb
    partial = m.predict({"hemoglobin": 124, "MCV": 82, "RDW": None, "ferritin": 9.0}, "F", 34)
    assert partial["features"] == "hb_only" and partial["p_lt15"] == r["p_lt15"]
    # пороги берутся для того же набора признаков
    full = m.predict(EXAMPLE, "F", 34)
    assert r["thresholds"] != full["thresholds"]
    try:
        m.predict({"MCV": 82}, "F", 34)
    except ValueError:
        pass
    else:
        raise AssertionError("без гемоглобина должна быть ошибка ValueError")


def test_groups_and_threshold_shares():
    m = _model()
    assert m.predict(EXAMPLE, "F", 49)["group"] == "F18-49"
    assert m.predict(EXAMPLE, "F", 50)["group"] == "F50+"
    man = dict(EXAMPLE, hemoglobin=141)
    assert m.predict(man, "M", 40)["group"] == "M"
    t20 = m.predict(EXAMPLE, "F", 34, refer_share=0.20)["thresholds"]
    t30 = m.predict(EXAMPLE, "F", 34)["thresholds"]
    t40 = m.predict(EXAMPLE, "F", 34, refer_share=0.40)["thresholds"]
    assert t20["refer"] > t30["refer"] > t40["refer"] > 0
    assert t20["high"] == t30["high"] == t40["high"] >= t20["refer"]
    # «высокий» риск — верхние 10 % по умолчанию; долю можно задать, но она не шире доли направления
    assert m.predict(EXAMPLE, "F", 34, high_share=0.20)["thresholds"]["high"] == t20["refer"]
    assert m.predict(EXAMPLE, "F", 34, refer_share=0.20, high_share=0.40)["thresholds"]["high"] == t20["refer"]
    # в метаданных пороги не растут с долей во всех группах и наборах признаков
    for features, by_group in m.metadata["thresholds"]["by_features"].items():
        for group, rows in by_group.items():
            values = [r["threshold"] for r in sorted(rows, key=lambda r: r["share"])]
            assert all(a >= b for a, b in zip(values, values[1:])), (features, group, values)
            assert {0.10, 0.20, 0.30, 0.40} <= {round(r["share"], 2) for r in rows}


def test_analyzer_shift_invariance():
    """Сдвиг анализатора: RDW у всех выше на 1,1 и справочник лаборатории сдвинут на столько же — ответ тот же."""
    m = _model()
    base = m.predict(EXAMPLE, "F", 34)
    ref = _default_reference()
    for sex in ("F", "M"):
        for q in ("q25", "median", "q75"):
            ref["by_sex"][sex]["RDW"][q] += 1.1
    shifted = m.predict(dict(EXAMPLE, RDW=EXAMPLE["RDW"] + 1.1), "F", 34, reference=ref)
    assert abs(shifted["p_lt15"] - base["p_lt15"]) <= 1e-9 and abs(shifted["p_lt30"] - base["p_lt30"]) <= 1e-9
    # без поправки справочника тот же сдвиг меняет ответ — стандартизация действительно работает
    naive = m.predict(dict(EXAMPLE, RDW=EXAMPLE["RDW"] + 1.1), "F", 34)
    assert abs(naive["p_lt15"] - base["p_lt15"]) > 1e-3
    # другие единицы (Hb и MCHC в г/дл) со своим справочником дают тот же ответ
    ref10 = _default_reference()
    for sex in ("F", "M"):
        for c in ("hemoglobin", "MCHC"):
            for q in ("q25", "median", "q75"):
                ref10["by_sex"][sex][c][q] /= 10.0
    gdl = m.predict(dict(EXAMPLE, hemoglobin=12.4, MCHC=32.9), "F", 34, reference=ref10)
    assert abs(gdl["p_lt15"] - base["p_lt15"]) <= 1e-9 and abs(gdl["p_lt30"] - base["p_lt30"]) <= 1e-9


def test_reference_argument_forms():
    """reference: None, имя, запись справочника и «голый» словарь по полу — один и тот же ответ."""
    m = _model()
    ref = _default_reference()
    base = m.predict(EXAMPLE, "F", 34)
    for reference in ("default", ref, ref["by_sex"]):
        r = m.predict(EXAMPLE, "F", 34, reference=reference)
        assert r["p_lt15"] == base["p_lt15"] and "reference_fallback" not in r
    # в справочнике нет нужного пола — берётся "default", в ответе пометка
    only_m = {"by_sex": {"M": ref["by_sex"]["M"]}}
    r = m.predict(EXAMPLE, "F", 34, reference=only_m)
    assert r["reference_fallback"] == "default" and r["p_lt15"] == base["p_lt15"]


def test_p30_not_below_p15_synthetic():
    """Разумность на выдуманных анализах: P(ферритин < 30) не меньше P(ферритин < 15); вероятности в [0, 1]."""
    m = _model()
    for row in _synthetic_rows(200, seed=1):
        for values in (row["values"], {"hemoglobin": row["values"]["hemoglobin"]}):
            r = m.predict(values, row["sex"], row["age_years"])
            assert 0.0 <= r["p_lt15"] <= 1.0 and 0.0 <= r["p_lt30"] <= 1.0
            assert r["p_lt30"] >= r["p_lt15"] - 0.02, r


def test_p30_not_below_p15_on_200_nhanes_rows():
    """Разумность на 200 случайных людях NHANES (взрослые без анемии): p_lt30 ≥ p_lt15 − 0,02."""
    import pandas as pd

    m = _model()
    path = helpers.nhanes_data_path()
    df = pd.read_csv(path, usecols=["age_years", "sex", "pregnant", *CBC_LABS], low_memory=False)
    df = df[(df.age_years >= 18) & (df.pregnant == 0)].dropna(subset=CBC_LABS)
    df = df[screen.who_anemia(df.hemoglobin, df.sex == "F") == 0].sample(200, random_state=0)
    raw_gap = []
    for row in df.to_dict("records"):
        values = {c: row[c] for c in CBC_LABS}
        r = m.predict(values, row["sex"], int(row["age_years"]))
        assert r["features"] == "full_cbc" and r["group"] == screen.group_of(row["sex"], row["age_years"])
        assert 0.0 <= r["p_lt15"] <= 1.0 and 0.0 <= r["p_lt30"] <= 1.0
        assert r["p_lt30"] >= r["p_lt15"] - 0.02, r
        raw_gap.append(r["p_lt15"] - r.get("p_lt30_model", r["p_lt30"]))
    # сами модели (до согласования) почти никогда не спорят: расхождение больше 0,02 — не чаще чем у 2 % людей
    assert float(np.mean(np.array(raw_gap) > 0.02)) <= 0.02


def test_fast_path_equals_sklearn():
    """Быстрый обход деревьев для одной строки даёт то же, что predict_proba sklearn."""
    m = _model()
    assert m.fast_path, "быстрый путь не прошёл самопроверку при загрузке модели"
    by_sex = _default_reference()["by_sex"]
    for features in ("full_cbc", "hb_only"):
        labs = screen.FEATURE_LABS[features]
        rows = _synthetic_rows(100, seed=2)
        X = np.array([screen.standardize(r["values"], r["sex"], by_sex, labs) + [r["age_years"], float(r["sex"] == "F")]
                      for r in rows], dtype=float)
        p15, p30 = m.predict_raw(X, features)
        for row, a, b in zip(rows, p15, p30):
            values = row["values"] if features == "full_cbc" else {"hemoglobin": row["values"]["hemoglobin"]}
            r = m.predict(values, row["sex"], row["age_years"])
            assert abs(r["p_lt15"] - a) <= 1e-9 and abs(r.get("p_lt30_model", r["p_lt30"]) - b) <= 1e-9


def test_saved_model_reproduces_reference_predictions():
    """Эталонные предсказания из metadata.json совпадают с загруженной моделью до 1e-5.
    Если менялся справочник "default" в config/lab_reference.yaml — модель нужно переобучить."""
    m = _model()
    ref = m.metadata["reference_predictions"]
    assert len(ref["cases"]) >= 3
    for case in ref["cases"]:
        r = m.predict(case["values"], case["sex"], case["age_years"], reference=ref["lab_reference"],
                      refer_share=ref["refer_share"])
        e = case["expected"]
        assert abs(r["p_lt15"] - e["p_lt15"]) <= 1e-5 and abs(r["p_lt30"] - e["p_lt30"]) <= 1e-5, case["name"]
        assert (r["features"], r["group"]) == (e["features"], e["group"])
        assert abs(r["thresholds"]["refer"] - e["refer"]) <= 1e-9 and abs(r["thresholds"]["high"] - e["high"]) <= 1e-9


def test_metadata_is_complete():
    m = _model()
    meta = m.metadata
    assert m.version == meta["version"] == "screen-v1"
    for key in ("created", "data", "files", "versions", "cohort", "thresholds", "validation"):
        assert key in meta, key
    assert len(meta["data"]["sha256"]) == 64
    assert {"python", "scikit-learn", "numpy"} <= set(meta["versions"])
    assert set(meta["files"]) == {"t15_full_cbc.joblib", "t15_hb_only.joblib",
                                  "t30_full_cbc.joblib", "t30_hb_only.joblib"}
    for name, info in meta["files"].items():
        path = models_dir() / "screen-v1" / name
        assert screen.sha256_file(path) == info["sha256"] and path.stat().st_size == info["bytes"]
    assert meta["cohort"]["train_rows"] == meta["cohort"]["no_anemia"] > 10000
    assert meta["validation"]["by_group"]["F18-49"]["t15"]["auc"] >= 0.80


# --------------------------------------------------------------------------------------------
# Справочник лаборатории
# --------------------------------------------------------------------------------------------
def test_build_lab_reference_dry_run():
    """Выдуманная выгрузка из 500 строк: квартили по полу совпадают с прямым расчётом, файл конфигурации не тронут."""
    config_file = config_dir() / "lab_reference.yaml"
    before = config_file.read_bytes()
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "cbc_export.csv"
        data = _write_export(path, n_f=250, n_m=250, seed=3)
        with open(path, "a", newline="", encoding="utf-8") as f:   # строки с пропуском и без пола пропускаются
            csv.writer(f).writerows([["F", 130, "", 39, 88, 29, 334, 13, 250, 6],
                                     ["", 130, 4.4, 39, 88, 29, 334, 13, 250, 6]])
        r = build_lab_reference(str(path), "test_lab", write=False)
    assert config_file.read_bytes() == before and r["written"] is False
    assert r["n_rows"] == 502 and r["n_used"] == 500 and r["n_by_sex"] == {"F": 250, "M": 250}
    assert set(r["by_sex"]) == {"F", "M"}
    for sex in ("F", "M"):
        for j, c in enumerate(CBC_LABS):
            q = r["by_sex"][sex][c]
            expected = np.quantile(data[sex][:, j], [0.25, 0.5, 0.75])
            assert q["q25"] < q["median"] < q["q75"]
            assert np.allclose([q["q25"], q["median"], q["q75"]], expected, atol=1e-3), (sex, c)


def test_built_reference_can_be_passed_to_predict():
    """Результат build_lab_reference можно сразу передать в модель как справочник лаборатории."""
    m = _model()
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "cbc_export.csv"
        _write_export(path, n_f=250, n_m=250, seed=3)
        r = build_lab_reference(str(path), "test_lab", write=False)
    out = m.predict(EXAMPLE, "F", 34, reference=r)
    assert "reference_fallback" not in out and 0.0 < out["p_lt15"] < 1.0
    assert out["p_lt15"] != m.predict(EXAMPLE, "F", 34)["p_lt15"]   # другой справочник — другая стандартизация


def test_build_lab_reference_warns_on_small_sample():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "small.csv"
        _write_export(path, n_f=250, n_m=120, seed=4, header_upper=False)
        r = build_lab_reference(str(path), "small_lab", write=False)
        assert any("M" in w and "200" in w for w in r["warnings"]), r["warnings"]
        assert not any(w.startswith("Пол F") for w in r["warnings"])
        for bad_name in ("default", "", "имя с пробелом"):
            try:
                build_lab_reference(str(path), bad_name, write=False)
            except ValueError:
                continue
            raise AssertionError(f"имя справочника {bad_name!r} должно быть отклонено")


def test_build_lab_reference_write_keeps_other_content():
    """write=True дописывает справочник и не трогает остальное (проверяется на копии файла конфигурации)."""
    import yaml

    with tempfile.TemporaryDirectory() as tmp:
        copy_path = Path(tmp) / "lab_reference.yaml"
        shutil.copy(config_dir() / "lab_reference.yaml", copy_path)
        before = yaml.safe_load(copy_path.read_text(encoding="utf-8"))
        path = Path(tmp) / "cbc_export.csv"
        _write_export(path, n_f=260, n_m=240, seed=5)
        r = build_lab_reference(str(path), "my_lab", write=True, config_path=copy_path)
        after = yaml.safe_load(copy_path.read_text(encoding="utf-8"))
        assert r["written"] is True and r["path"] == str(copy_path)
        assert after["references"]["my_lab"]["by_sex"] == r["by_sex"]
        assert after["references"]["default"] == before["references"]["default"]
        other = lambda doc: {k: v for k, v in doc.items() if k != "references"}  # noqa: E731
        assert other(after) == other(before)
        assert set(after["references"]) == set(before["references"]) | {"my_lab"}
