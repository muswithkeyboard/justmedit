"""Тесты модели по файлу кейса: пороги приёмки, отсутствие утечки в клиническом режиме,
«следующий анализ», объяснение, воспроизводимость сохранённой модели, скорость.

Тесты, которым нужен файл кейса, вызывают helpers.case_data_path() — без файла это SKIP.
Остальные работают на придуманных входах (train_case.GOLDEN_SYNTHETIC) и на артефактах models/case-v1.
"""
from __future__ import annotations

import json
import math
import time
from functools import lru_cache

import numpy as np

from helpers import ROOT, case_data_path, skip

from deficitlens_core import constants as C
from deficitlens_core.config import models_dir
from deficitlens_core.ml.case_model import (FUSION_FILE, GOLDEN_FILE, METADATA_FILE, MODEL_FILES, CaseModel,
                                            decode_profiles, sha256_file)
from deficitlens_core.ml.features import apply_level, completeness, feats_plus, to_matrix, who_anemia, who_lock
from deficitlens_core.ml.train_case import GOLDEN_SYNTHETIC, load_case, sha256_bytes

METRICS_PATH = ROOT / "docs" / "metrics" / "case_metrics.json"
MODEL_DIR = models_dir() / "case-v1"
LEVELS = ["cbc", "cbc_iron_crp", "standard", "extended"]


@lru_cache(maxsize=1)
def _model() -> CaseModel:
    return CaseModel.load()


@lru_cache(maxsize=1)
def _metrics() -> dict:
    assert METRICS_PATH.exists(), "нет docs/metrics/case_metrics.json: запустите `deficitlens evaluate --what case`"
    return json.loads(METRICS_PATH.read_text(encoding="utf-8"))


def _mean(mode: str, level: str, metric: str) -> float:
    return _metrics()["by_mode_level"][mode][level][metric]["mean"]


@lru_cache(maxsize=1)
def _fusion_json() -> dict:
    return json.loads((MODEL_DIR / FUSION_FILE).read_text(encoding="utf-8"))


def _synthetic_records(n_extra: int = 24) -> list[dict]:
    """Придуманные записи: три базовые и их вариации (значения ±20 %, часть анализов убрана)."""
    rng = np.random.default_rng(7)
    extra_labs = {"ferritin": 40.0, "serum_iron": 12.0, "TIBC": 60.0, "TSAT": 20.0, "CRP": 4.0, "vitamin_B12": 300.0,
                  "folate": 7.0, "vitamin_B6": 40.0, "copper": 15.0, "TSH": 2.0, "albumin": 42.0, "creatinine": 80.0,
                  "LDH": 200.0, "homocysteine": 10.0, "sTfR": 3.0}
    out = [dict(r) for r in GOLDEN_SYNTHETIC]
    for k in range(n_extra):
        base = dict(GOLDEN_SYNTHETIC[k % len(GOLDEN_SYNTHETIC)])
        rec = {"sex": "F" if k % 2 else "M", "age_years": int(rng.integers(18, 90))}
        for key, v in base.items():
            if key in ("sex", "age_years"):
                continue
            if key in C.CBC or rng.random() < 0.6:
                rec[key] = round(float(v) * float(rng.uniform(0.8, 1.2)), 2)
        for key, v in extra_labs.items():
            if key not in rec and rng.random() < 0.35:
                rec[key] = round(v * float(rng.uniform(0.3, 1.8)), 2)
        out.append(rec)
    return out


def _X() -> np.ndarray:
    return to_matrix(_synthetic_records())


# --------------------------------------------------------------------------------------------
# Пороги приёмки — по сохранённым метрикам кросс-валидации
# --------------------------------------------------------------------------------------------
def test_gate_level1_match_840():
    assert _metrics()["summary"]["level1_match"] == 840


def test_gate_benchmark_extended():
    assert _mean("benchmark", "extended", "acc12") >= 0.90
    assert _mean("benchmark", "extended", "acc5") >= 0.93


def test_gate_clinical_by_level():
    assert _mean("clinical", "extended", "acc12") >= 0.83
    assert _mean("clinical", "cbc_iron_crp", "acc12") >= 0.64
    assert _mean("clinical", "cbc", "acc12") >= 0.55


def test_gate_hidden_auroc_cbc():
    assert _mean("clinical", "cbc", "auroc_hidden") >= 0.87


def test_gate_clinical_ece_pooled():
    for level in LEVELS:
        assert _mean("clinical", level, "ece_pooled") <= 0.06, level


def test_cv_matches_prototype():
    """Перенос не изменил алгоритм: точность по 12 классам совпадает с прототипом с точностью 0,005."""
    expected = {("clinical", "extended"): 0.849, ("benchmark", "extended"): 0.919,
                ("clinical", "cbc"): 0.573, ("clinical", "cbc_iron_crp"): 0.665}
    for (mode, level), ref in expected.items():
        assert abs(_mean(mode, level, "acc12") - ref) <= 0.005, (mode, level, _mean(mode, level, "acc12"))


def test_metrics_file_structure():
    m = _metrics()
    assert set(m) >= {"summary", "by_mode_level", "confusion", "config"}
    for mode in ("clinical", "benchmark", "naive"):
        for level in LEVELS:
            cell = m["by_mode_level"][mode][level]
            for metric in ("acc12", "f1_12", "acc5", "f1_5", "f1_cause", "f1_cause_proto", "logloss12", "ece",
                           "auroc_hidden", "ece_pooled", "acc5_anemic"):
                assert set(cell[metric]) == {"mean", "sd"}, (mode, level, metric)
    for mode in ("clinical", "benchmark"):
        cm5 = np.array(m["confusion"][mode]["groups5"]["matrix"])
        cm12 = np.array(m["confusion"][mode]["classes12"]["matrix"])
        assert cm5.shape == (5, 5) and cm12.shape == (12, 12)
        assert cm5.sum() == 840 and cm12.sum() == 840          # первый повтор: каждая строка ровно один раз
        assert cm5[0, 0] == 309 and cm5[0, 1:].sum() == 0      # группа healthy следует из правила по Hb


# --------------------------------------------------------------------------------------------
# Белый список признаков и отсутствие утечки в клиническом режиме
# --------------------------------------------------------------------------------------------
def test_feature_whitelist():
    """В модель попадают только 37 признаков FEATS; метки и чужие столбцы to_matrix игнорирует."""
    assert len(C.FEATS) == 37 and len(set(C.FEATS)) == 37
    labels = {"anemia", "anemia_class", "deficiency_cause", "anemia_cause", "iron_deficiency", "B12_deficiency",
              "folate_deficiency", "mixed_deficiency", "patient_id"}
    assert not labels & set(C.FEATS)
    rec = dict(GOLDEN_SYNTHETIC[1])
    with_labels = {**rec, "anemia": 1, "anemia_class": "iron_deficiency_anemia", "deficiency_cause": "iron_deficiency",
                   "iron_deficiency": 1, "patient_id": "X1", "record_origin": "lab"}
    assert np.array_equal(to_matrix([rec]), to_matrix([with_labels]), equal_nan=True)
    model = _model()
    assert model._fusion["prior_model"].n_features_in_ == len(C.CBC) == 11   # приор видит только ОАК, возраст, пол
    assert model._bench.n_features_in_ == 37 + 6


def test_clinical_cbc_only_equals_prior_with_who_lock():
    """Без анализов вне ОАК клинический режим = приор^t + замок ВОЗ (ничего больше в ответ не входит)."""
    model = _model()
    X = apply_level(_X(), "cbc")
    t = model.fusion_params["prior_power_t"]
    expected = who_lock(np.clip(model.prior(X), 1e-12, 1.0) ** t, who_anemia(X))
    assert np.allclose(model.predict_profiles(X, "clinical"), expected, atol=1e-10)


def test_clinical_cbc_only_equals_prior_on_case_rows():
    data = load_case(case_data_path())
    model = _model()
    X = apply_level(data["X"], "cbc")
    t = model.fusion_params["prior_power_t"]
    expected = who_lock(np.clip(model.prior(X), 1e-12, 1.0) ** t, who_anemia(X))
    assert np.allclose(model.predict_profiles(X, "clinical"), expected, atol=1e-10)


def test_clinical_depends_only_on_measured_values():
    """Отсутствие ключа, None, пустая строка и NaN — одно и то же; у строк с одинаковыми сданными значениями
    ответ одинаков и не зависит от соседей по пакету."""
    model = _model()
    base = dict(GOLDEN_SYNTHETIC[1])
    unmeasured = [a for a in C.LABS if a not in base]
    variants = [base,
                {**base, **{a: None for a in unmeasured}},
                {**base, **{a: "" for a in unmeasured}},
                {**base, **{a: float("nan") for a in unmeasured}},
                {**base, "unknown_column": 123, "record_origin": "lis"}]
    X = to_matrix(variants)
    assert all(np.array_equal(X[0], X[i], equal_nan=True) for i in range(1, len(variants)))
    P = model.predict_profiles(X, "clinical")
    assert all(np.array_equal(P[0], P[i]) for i in range(1, len(variants)))
    # та же строка в одиночку и среди других строк
    batch = np.vstack([_X(), X[:1]])
    alone = model.predict_profiles(X[:1], "clinical")[0]
    assert np.allclose(model.predict_profiles(batch, "clinical")[-1], alone, atol=1e-12)


def test_clinical_update_uses_only_table_of_measured_analyte():
    """Добавление одного анализа меняет ответ ровно на множитель из его таблицы: P(бин | профиль)^вес группы.
    Члена «сдан / не сдан» в модели нет."""
    model, fj = _model(), _fusion_json()
    assert "presence" not in json.dumps(fj).lower()
    x0 = apply_level(to_matrix([GOLDEN_SYNTHETIC[1]]), "cbc")
    p0 = model.predict_profiles(x0, "clinical")[0]
    for analyte, value in (("ferritin", 12.0), ("vitamin_B12", 150.0), ("CRP", 40.0), ("TSH", 3.0)):
        x1 = x0.copy()
        x1[0, C.F_IDX[analyte]] = value
        tab = fj["tables"][analyte]
        b = int(np.searchsorted(np.array(tab["edges"]), value, side="right"))
        w = fj["group_weights"][C.ANALYTE_GROUP[analyte]]
        expected = p0 * np.exp(w * np.array(tab["log_p"])[:, b])
        expected /= expected.sum()
        assert np.allclose(model.predict_profiles(x1, "clinical")[0], expected, atol=1e-10), analyte


def test_who_lock_in_both_modes():
    model = _model()
    X = _X()
    anemia = who_anemia(X)
    assert 0 < anemia.sum() < len(X)                           # в наборе есть и анемия, и её отсутствие
    for mode in ("clinical", "benchmark"):
        P = model.predict_profiles(X, mode)
        assert P.shape == (len(X), 14) and np.allclose(P.sum(axis=1), 1.0)
        assert np.all(P[~C.ALLOW[anemia]] == 0.0), mode       # запрещённые профили обнулены
        assert np.all(P >= 0)
    both = model.predict_both(X)
    assert np.array_equal(both["clinical"], model.predict_profiles(X, "clinical"))
    assert np.array_equal(both["benchmark"], model.predict_profiles(X, "benchmark"))


def test_decode_cause_consistent_with_class():
    """Причина всегда принадлежит выбранному классу (класс и причина не противоречат)."""
    P = _model().predict_profiles(_X(), "clinical")
    dec = decode_profiles(P)
    for cls, prof, cause in zip(dec["class12"], dec["top_profile"], dec["cause11"]):
        assert C.PROFILE_TO_CLASS[C.PROFILES[prof]] == C.CLASSES12[cls]
        assert C.PROFILE_TO_CAUSE[C.PROFILES[prof]] == C.CAUSES11[cause]


# --------------------------------------------------------------------------------------------
# Следующий анализ и объяснение
# --------------------------------------------------------------------------------------------
def test_information_gain_nonnegative_and_only_unmeasured():
    model, fj = _model(), _fusion_json()
    for x in _X():
        gains = model.information_gain(x)
        measured = {a for a in C.LABS if not np.isnan(x[C.F_IDX[a]])}
        assert set(gains) == set(C.LABS) - measured            # сданных анализов в словаре нет
        assert all(v >= 0 and math.isfinite(v) for v in gains.values())
        assert all(v <= math.log2(14) + 1e-9 for v in gains.values())
        for a, v in gains.items():                             # группа с нулевым весом информации не даёт
            if fj["group_weights"][C.ANALYTE_GROUP[a]] == 0:
                assert v == 0.0


def test_contributions_explain_log_odds():
    """Сумма вкладов + вклад приора = логарифм отношения вероятностей двух профилей (разложение точное)."""
    model = _model()
    t = model.fusion_params["prior_power_t"]
    checked = 0
    for x in _X():
        P = model.predict_profiles(x[None, :], "clinical")[0]
        order = np.argsort(-P)
        top, runner = int(order[0]), int(order[1])
        items = model.contributions(x, top, runner)
        measured = [a for a in C.LABS if not np.isnan(x[C.F_IDX[a]])]
        assert sorted(i["analyte"] for i in items) == sorted(measured)
        deltas = [abs(i["delta"]) for i in items]
        assert deltas == sorted(deltas, reverse=True)
        if P[runner] > 1e-9:
            prior = np.clip(model.prior(x[None, :])[0], 1e-12, 1.0)
            total = t * (math.log(prior[top]) - math.log(prior[runner])) + sum(i["delta"] for i in items)
            assert abs(total - (math.log(P[top]) - math.log(P[runner]))) < 1e-8
            checked += 1
    assert checked > 5


# --------------------------------------------------------------------------------------------
# Воспроизводимость сохранённой модели
# --------------------------------------------------------------------------------------------
def _golden() -> dict:
    path = MODEL_DIR / GOLDEN_FILE
    assert path.exists(), "нет golden_predictions.json: запустите `deficitlens train --what case`"
    return json.loads(path.read_text(encoding="utf-8"))


def test_golden_synthetic_predictions():
    """Сохранённая модель воспроизводит эталон на придуманных входах (работает без файла кейса)."""
    golden, model = _golden(), _model()
    assert golden["profiles"] == C.PROFILES
    X = to_matrix([g["input"] for g in golden["synthetic"]])
    for mode in ("clinical", "benchmark"):
        expected = np.array([g[mode] for g in golden["synthetic"]])
        assert np.abs(model.predict_profiles(X, mode) - expected).max() < 1e-5, mode


def test_golden_case_rows_predictions():
    """Сохранённая модель воспроизводит эталон на 5 фиксированных строках файла кейса (точность 1e-5)."""
    path = case_data_path()
    golden, model = _golden(), _model()
    rows = golden["case_rows"]
    if sha256_bytes(path) != rows["data_sha256"]:
        skip("файл кейса отличается от того, на котором обучена модель")
    X = load_case(path)["X"][rows["row_index"]]
    assert len(rows["row_index"]) == 5
    for mode in ("clinical", "benchmark"):
        assert np.abs(model.predict_profiles(X, mode) - np.array(rows[mode])).max() < 1e-5, mode


def test_golden_file_has_no_analysis_values():
    """В эталоне по строкам кейса — только номера строк и вероятности, без значений анализов."""
    rows = _golden()["case_rows"]
    assert set(rows) == {"data_sha256", "row_index", "clinical", "benchmark"}
    assert all(isinstance(i, int) for i in rows["row_index"])


def test_metadata_and_file_hashes():
    meta = json.loads((MODEL_DIR / METADATA_FILE).read_text(encoding="utf-8"))
    assert meta["version"] == "case-v1" == _model().version
    for key in ("trained_at", "data", "files", "versions", "fusion", "hidden_tau", "summary"):
        assert key in meta, key
    assert len(meta["data"]["sha256"]) == 64
    assert {"python", "scikit-learn", "numpy"} <= set(meta["versions"])
    for name in MODEL_FILES + (GOLDEN_FILE,):
        assert sha256_file(MODEL_DIR / name) == meta["files"][name]["sha256"], name
    assert set(meta["fusion"]["group_weights"]) == set(C.GROUP_NAMES)
    assert meta["fusion"]["presence_term"] is False
    assert 0 < meta["fusion"]["prior_power_t"] <= 1


def test_hidden_threshold_levels():
    model = _model()
    for mode in ("clinical", "benchmark"):
        for level in LEVELS:
            tau = model.hidden_threshold(level, mode=mode)
            assert 0.0 < tau < 1.0, (mode, level, tau)
    assert model.hidden_threshold("cbc") == model.metadata["hidden_tau"]["cbc"]
    stats = model.metadata["hidden_oof"]["clinical"]
    assert all(s["specificity"] >= 0.90 - 1e-9 for s in stats.values())   # порог выбран под специфичность 0,90
    try:
        model.hidden_threshold("full")
    except ValueError:
        pass
    else:
        raise AssertionError("неизвестный уровень должен давать ValueError")


# --------------------------------------------------------------------------------------------
# Признаки
# --------------------------------------------------------------------------------------------
def test_completeness_levels():
    base = {k: v for k, v in GOLDEN_SYNTHETIC[0].items()}
    row = lambda extra: to_matrix([{**base, **extra}])[0]  # noqa: E731
    assert completeness(row({})) == "cbc"
    assert completeness(row({"ferritin": 20, "CRP": 3})) == "cbc_iron_crp"
    assert completeness(row({"ferritin": 20, "vitamin_B12": 300})) == "standard"
    assert completeness(row({"folate": 6})) == "standard"
    assert completeness(row({"ferritin": 20, "TSH": 2})) == "extended"
    assert completeness(row({"sTfR": 3})) == "extended"       # sTfR не входит в набор «железо + СРБ»


def test_who_anemia_thresholds():
    rec = lambda sex, hb: {**GOLDEN_SYNTHETIC[0], "sex": sex, "hemoglobin": hb}  # noqa: E731
    X = to_matrix([rec("F", 119.9), rec("F", 120.0), rec("M", 129.9), rec("M", 130.0)])
    assert who_anemia(X).tolist() == [1, 0, 1, 0]


def test_feats_plus_has_no_infinity():
    rec = {**GOLDEN_SYNTHETIC[1], "ferritin": 1.0, "sTfR": 4.0, "folate": 0.0, "vitamin_B12": 300, "TIBC": 0.0}
    F = feats_plus(to_matrix([rec, GOLDEN_SYNTHETIC[0]]))
    assert F.shape == (2, 43)
    assert not np.isinf(F).any()
    assert np.isfinite(_model().predict_profiles(to_matrix([rec]), "benchmark")).all()


def test_predict_profiles_input_checks():
    model = _model()
    assert model.predict_profiles(np.zeros((0, 37))).shape == (0, 14)
    for bad_call in (lambda: model.predict_profiles(np.zeros((2, 36))),
                     lambda: model.predict_profiles(_X(), mode="auto")):
        try:
            bad_call()
        except ValueError:
            continue
        raise AssertionError("ожидался ValueError")


# --------------------------------------------------------------------------------------------
# Скорость
# --------------------------------------------------------------------------------------------
def test_single_row_speed():
    """predict_profiles на одной строке быстрее 150 мс (медиана по 20 вызовам) в обоих режимах."""
    model, X = _model(), _X()
    for mode in ("clinical", "benchmark"):
        model.predict_profiles(X[:1], mode)                    # прогрев
        times = []
        for k in range(20):
            t0 = time.perf_counter()
            model.predict_profiles(X[k:k + 1], mode)
            times.append(time.perf_counter() - t0)
        assert float(np.median(times)) < 0.150, (mode, float(np.median(times)))
