"""Обучение модели по файлу кейса (840 строк, синтетика) и сохранение артефактов в models/case-v1.

Что обучается на ВСЕХ строках файла:
  1. слияние без утечки — клинический режим (ml/fusion.py);
  2. бустинг режима бенчмарка — вместе со слиянием образует ансамбль (ml/ensemble.py);
  3. пороги флага скрытого дефицита hidden_tau — по внутренним OOF-предсказаниям (специфичность 0,90).
Честные метрики считает evaluate_case.py кросс-валидацией: итоговая модель видела все строки файла,
поэтому её ответы на самом файле кейса оптимистичны и в отчёт не идут.
В репозиторий попадают только артефакты и агрегаты; строки данных кейса не сохраняются.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import time
from pathlib import Path

import numpy as np
import pandas as pd

from .. import constants as C
from . import ensemble, fusion
from .case_model import (GOLDEN_FILE, METADATA_FILE, VERSION, CaseModel, file_info, library_versions,
                         p_any_deficiency, write_json, write_model_files)
from .features import LEVEL_NAMES, apply_level, to_matrix, who_anemia

SEED = 20261003            # тот же, что в прототипе
HIDDEN_SPECIFICITY = 0.90  # целевая специфичность флага скрытого дефицита
N_GOLDEN_ROWS = 5

# Придуманные входы (не данные пациентов и не строки кейса) для эталонной проверки сохранённой модели:
# по ним тест воспроизводимости работает и без файла кейса. Единицы канонические (config/analytes.yaml).
GOLDEN_SYNTHETIC: list[dict] = [
    {"sex": "F", "age_years": 34, "hemoglobin": 124, "RBC": 4.59, "hematocrit": 37.7, "MCV": 82, "MCH": 27.0,
     "MCHC": 329, "RDW": 15.2, "platelets": 310, "WBC": 6.1},
    {"sex": "M", "age_years": 61, "hemoglobin": 104, "RBC": 4.10, "hematocrit": 32.4, "MCV": 79, "MCH": 25.4,
     "MCHC": 321, "RDW": 16.0, "platelets": 340, "WBC": 8.9, "ferritin": 62, "CRP": 28, "serum_iron": 5.5,
     "TIBC": 50, "TSAT": 11},
    {"sex": "F", "age_years": 58, "hemoglobin": 96, "RBC": 3.35, "hematocrit": 30.5, "MCV": 91, "MCH": 28.7,
     "MCHC": 315, "RDW": 19.5, "platelets": 280, "WBC": 5.2, "ferritin": 8, "vitamin_B12": 160, "folate": 3.1,
     "homocysteine": 22, "LDH": 260, "TSH": 2.1, "albumin": 41},
]


def sha256_bytes(path: str | Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _table_sha256(case_path: str | Path) -> str | None:
    """Отпечаток нормализованной таблицы обучения (io/table.py → table_fingerprint): по нему сервис узнаёт файл
    обучения и после пересохранения (CRLF, без BOM, XLSX)."""
    from ..io.table import read_table, table_fingerprint
    return table_fingerprint(read_table(case_path))


def load_case(case_path: str | Path) -> dict:
    """Читает файл кейса в схеме 840 строк и проверяет целостность разметки.

    Возвращает {"X": (n, 37), "y": номер профиля 0..13, "anemia": метка 0/1, "level1_match": число строк,
    где правило ВОЗ совпало с меткой anemia, "sha256": контрольная сумма файла, "n": число строк}.
    14 профилей = 12 классов, где mixed_deficiency разбит на три подтипа по столбцу deficiency_cause.
    """
    df = pd.read_csv(case_path)
    need = {"anemia", "anemia_class", "deficiency_cause", "sex", "age_years", "hemoglobin"}
    missing = sorted(need - set(df.columns))
    if missing:
        raise ValueError(f"в файле кейса нет столбцов {missing}: для обучения нужна схема с 12 классами и причиной")
    cls = df["anemia_class"].astype(str)
    cause = df["deficiency_cause"].astype(str)
    prof = np.where(cls == "mixed_deficiency", "mixed:" + cause, cls)
    unknown = sorted(set(prof) - set(C.PROFILES))
    if unknown:
        raise ValueError(f"неизвестные классы в файле кейса: {unknown}")
    y = np.array([C.P_IDX[p] for p in prof])
    X = to_matrix(df)
    anemia = df["anemia"].to_numpy(dtype=int)
    if not all(C.PROFILE_TO_CAUSE[p] == c for p, c in zip(prof, cause)):
        raise ValueError("deficiency_cause не согласован с anemia_class: разметка файла кейса повреждена")
    if np.isnan(X[:, [i for i in C.CBC_IDX if C.FEATS[i] != "RDW"]]).any():
        raise ValueError("в файле кейса есть пропуски в ОАК, возрасте или поле")
    return {"X": X, "y": y, "anemia": anemia, "n": int(len(df)),
            "level1_match": int((who_anemia(X) == anemia).sum()), "sha256": sha256_bytes(case_path)}


def specificity_threshold(neg_scores, target: float = HIDDEN_SPECIFICITY) -> float:
    """Наименьший порог tau, при котором доля отрицательных строк с оценкой < tau не меньше target.

    Флаг ставится при оценке ≥ tau, значит специфичность на этих строках ≥ target.
    """
    s = np.sort(np.asarray(neg_scores, dtype=float))
    for c in np.unique(s):
        if (s < c).mean() >= target - 1e-12:
            return float(c)
    return float(np.nextafter(s[-1], np.inf))  # все значения совпали: флаг не ставится никому


def hidden_thresholds(P_by_level: dict[str, np.ndarray], y, anemia, target: float = HIDDEN_SPECIFICITY):
    """Пороги скрытого дефицита по уровням полноты и достигнутые на OOF специфичность и чувствительность.

    Оценка = 1 − P(«анемии и дефицитов нет»). Отрицательные — строки без анемии и без дефицита (профиль 0),
    положительные — строки без анемии с любым дефицитом (латентный дефицит железа, B12, фолаты, B6, медь).
    """
    y = np.asarray(y)
    na = np.asarray(anemia) == 0
    neg, pos = na & (y == 0), na & (y != 0)
    tau, stats = {}, {}
    for lv in LEVEL_NAMES:
        score = p_any_deficiency(P_by_level[lv])
        t = specificity_threshold(score[neg], target)
        tau[lv] = t
        stats[lv] = {"specificity": float((score[neg] < t).mean()), "sensitivity": float((score[pos] >= t).mean()),
                     "n_negative": int(neg.sum()), "n_positive": int(pos.sum())}
    return tau, stats


def golden_predictions(model: CaseModel, X: np.ndarray, data_sha256: str) -> dict:
    """Эталонные вероятности: 5 фиксированных строк файла кейса (только номера строк, без значений анализов)
    и придуманные входы GOLDEN_SYNTHETIC. По ним тест проверяет, что сохранённая модель воспроизводится."""
    rows = np.linspace(0, len(X) - 1, N_GOLDEN_ROWS).astype(int)
    both = model.predict_both(X[rows])
    Xs = to_matrix(GOLDEN_SYNTHETIC)
    both_s = model.predict_both(Xs)
    return {
        "version": VERSION,
        "note": "Вероятности 14 профилей. Значения анализов строк кейса не хранятся: только номера строк (с нуля).",
        "profiles": C.PROFILES,
        "case_rows": {"data_sha256": data_sha256, "row_index": rows.tolist(),
                      "clinical": both["clinical"].tolist(), "benchmark": both["benchmark"].tolist()},
        "synthetic": [{"input": inp, "clinical": both_s["clinical"][i].tolist(),
                       "benchmark": both_s["benchmark"][i].tolist()} for i, inp in enumerate(GOLDEN_SYNTHETIC)],
    }


def train(case_path: str, out_dir: str) -> dict:
    """Обучает модель на всех строках файла кейса и сохраняет артефакты в out_dir. Возвращает краткую сводку."""
    t0 = time.time()
    data = load_case(case_path)
    X, y, anemia = data["X"], data["y"], data["anemia"]
    out = Path(out_dir)

    # 1. Слияние (клинический режим); OOF-вероятности по уровням полноты нужны для порогов скрытого дефицита.
    fus = fusion.fit_fusion(X, y, SEED, keep_oof=True)
    oof = fus.pop("oof")

    # 2. Бустинг режима бенчмарка на всех строках.
    bench = ensemble.fit_bench_gbm(X, y, SEED)

    # 3. Пороги скрытого дефицита. Клинический режим — по OOF слияния. Режим бенчмарка — по OOF ансамбля:
    #    бустинг обучается на тех же внутренних фолдах, его вероятности объединяются с OOF слияния.
    an_who = who_anemia(X)
    tau_clin, stats_clin = hidden_thresholds(oof["posterior"], y, anemia)
    gbm_oof = {lv: np.zeros((len(X), C.NP_)) for lv in LEVEL_NAMES}
    for tr, va in oof["splits"]:
        g = ensemble.fit_bench_gbm(X[tr], y[tr], SEED)
        for lv in LEVEL_NAMES:
            gbm_oof[lv][va] = ensemble.gbm_proba_locked(g, apply_level(X[va], lv))
    ens_oof = {lv: ensemble.combine(gbm_oof[lv], oof["posterior"][lv], an_who) for lv in LEVEL_NAMES}
    tau_bench, stats_bench = hidden_thresholds(ens_oof, y, anemia)

    # 4. Файлы модели, затем эталонные предсказания УЖЕ ЗАГРУЖЕННОЙ с диска модели и metadata.json.
    files = write_model_files(out, fus, bench)
    params = {"prior_power_t": fus["t"], "group_weights": dict(zip(C.GROUP_NAMES, fus["w"]))}
    metadata = {
        "version": VERSION,
        "trained_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "engine_version": C.ENGINE_VERSION,
        "data": {"sha256": data["sha256"], "table_sha256": _table_sha256(case_path), "rows": data["n"],
                 "file": Path(case_path).name,
                 "note": "учебный набор кейса (840 строк); строки данных в репозитории не хранятся"},
        "files": files,
        "versions": library_versions(),
        "seed": SEED,
        "fusion": {**params, "n_bins": fus["n_bins"], "alpha": fus["alpha"],
                   "oof_logloss14": fus["oof_logloss"], "oof_logloss14_prior_only": fus["oof_logloss_prior_only"],
                   "prior_gbm": fusion.PRIOR_GBM_PARAMS, "presence_term": False},
        "benchmark": {"gbm": ensemble.BENCH_GBM_PARAMS, "derived_features": 6,
                      "combine": "геометрическое среднее вероятностей бустинга и слияния, затем замок ВОЗ"},
        "hidden_tau": tau_clin,
        "hidden_tau_benchmark": tau_bench,
        "hidden_oof": {"target_specificity": HIDDEN_SPECIFICITY, "clinical": stats_clin, "benchmark": stats_bench},
    }
    write_json(out / METADATA_FILE, metadata)          # временно: нужен, чтобы загрузить модель с диска
    model = CaseModel.load(out)
    write_json(out / GOLDEN_FILE, golden_predictions(model, X, data["sha256"]))
    metadata["files"][GOLDEN_FILE] = file_info(out / GOLDEN_FILE)
    seconds = round(time.time() - t0, 1)
    metadata["summary"] = {
        "rows": data["n"], "rows_with_anemia": int(anemia.sum()), "level1_match": data["level1_match"],
        "profile_counts": {p: int((y == i).sum()) for i, p in enumerate(C.PROFILES)},
        "mean_share_of_labs_measured": round(float((~np.isnan(X[:, C.LAB_IDX])).mean()), 4),
        "train_seconds": seconds,
        "modes": {"clinical": "слияние без утечки (по умолчанию)", "benchmark": "ансамбль бустинг × слияние"},
        "limitations": "обучено на учебном наборе кейса (840 строк); не медицинское изделие",
    }
    write_json(out / METADATA_FILE, metadata)

    return {"version": VERSION, "out_dir": str(out), "rows": data["n"], "level1_match": data["level1_match"],
            **params, "oof_logloss14": round(fus["oof_logloss"], 4),
            "hidden_tau": {k: round(v, 4) for k, v in tau_clin.items()},
            "hidden_tau_benchmark": {k: round(v, 4) for k, v in tau_bench.items()},
            "hidden_oof_sensitivity": {k: round(v["sensitivity"], 3) for k, v in stats_clin.items()},
            "files": {k: v["bytes"] for k, v in metadata["files"].items()}, "train_seconds": seconds}
