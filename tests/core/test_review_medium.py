"""Этап 6: находки MEDIUM ревизии волн 4–5 (п. 5–7 этапа 6).

MEDIUM-1 — носитель β-талассемии (MCV 70, RBC 5,5, RDW 13,2; индекс Ментцера < 13): уверенность не выше «средней»,
          пациенту при полноте «только ОАК» причина не называется, врачу — подсказка Ментцера;
MEDIUM-2 — флаг «дефицит по сданным анализам не найден» не ставится, если лучший класс уровня 2 — дефицитный
          с уверенностью не ниже «средней»;
MEDIUM-3 — плашка «файл обучения» — по нормализованной таблице, а не по байтам файла.
"""
from __future__ import annotations

import copy
import dataclasses
import io

import numpy as np

import helpers

from deficitlens_core import constants as C
from deficitlens_core.config import load_config
from deficitlens_core.engine import Models, analyze, load_models
from deficitlens_core.schemas import AnalysisInput

CFG = load_config()
# Числа B12 выключены (вариант до решения п. 10) — так воспроизводится пример MEDIUM-2 (B12 90 пг/мл, MCV 112).
LOCAL = dataclasses.replace(CFG, norms={**copy.deepcopy(CFG.norms), "policy": {"use_foreign": False}})
THAL_CBC = {"MCV": 70, "RBC": 5.5, "RDW": 13.2}


class FakeCase:
    """Модель уровня 2 с заданным ответом: вся вероятность — на одном профиле (для проверки понижения уверенности)."""
    version = "fake"
    metadata: dict = {}

    def __init__(self, profile: str, p: float = 0.95):
        self.P = np.full(len(C.PROFILES), (1 - p) / (len(C.PROFILES) - 1))
        self.P[C.PROFILES.index(profile)] = p

    def predict_profiles(self, X, mode="clinical"):
        return np.tile(self.P, (len(X), 1))

    def contributions(self, x_row, top, runner):
        return []

    def hidden_threshold(self, level, mode="clinical"):
        return 0.5

    def information_gain(self, x_row):
        return {}


def run(values, sex="F", cfg=CFG, models=None):
    return analyze(AnalysisInput(sex=sex, age_years=35, values=values), cfg=cfg, models=models)


def lines(report) -> list[str]:
    return [report.headline] + [ln for s in report.sections for ln in s.lines]


# ------------------------------------------------------------------ MEDIUM-1
def test_thalassemia_like_mentzer_caps_confidence():
    fake = Models(case=FakeCase("iron_deficiency_anemia"), screen=None)
    thal = run({"hemoglobin": 110, **THAL_CBC, "TSAT": 25}, models=fake)            # Ментцер 70 / 5,5 = 12,7
    assert thal.level2.confidence == "moderate"
    assert "индекс Ментцера 12,7 — вероятнее носительство β-талассемии" in thal.explanation.note
    control = run({"hemoglobin": 110, "MCV": 70, "RBC": 4.0, "RDW": 13.2, "TSAT": 25}, models=fake)   # Ментцер 17,5
    assert control.level2.confidence == "high"


def test_thalassemia_like_patient_cause_not_named_on_cbc_only():
    full_cbc = {"hemoglobin": 112, **THAL_CBC, "MCH": 21, "hematocrit": 38.5, "MCHC": 300, "platelets": 250, "WBC": 6}
    for models in (None, Models(case=FakeCase("iron_deficiency_anemia", 0.7), screen=None)):
        r = run(full_cbc, models=models)
        if not r.level2.enabled:
            continue
        assert r.level2.completeness == "cbc"
        assert r.level2.confidence in ("low", "moderate")
        main = next(s for s in r.reports.patient.sections if s.title == "Главное").lines
        assert "Данных мало: сдан только общий анализ крови. Причину по нему надёжно назвать нельзя." in main
        assert not any("Возможная причина" in ln for ln in main)
        patient = " ".join(lines(r.reports.patient)).lower()
        assert "ментцер" not in patient and "талассем" not in patient
        why = " ".join(next(s for s in r.reports.doctor.sections if s.title == "Почему такой вывод").lines)
        assert "Индекс Ментцера (MCV/RBC) 12,7: меньше 13 — вероятнее носительство β-талассемии" in why
    # без талассемической картины (Ментцер > 13) причина по одному ОАК может называться — правило не мешает
    from deficitlens_core.reports.patient import _cause_line
    from deficitlens_core.reports import Texts
    from deficitlens_core.schemas import Level2
    lvl = Level2(enabled=True, completeness="cbc", deficiency_cause="iron_deficiency", confidence="moderate")
    T = Texts(CFG.texts["patient"])
    assert "нехватка железа" in _cause_line(T, lvl) and "нехватка" not in _cause_line(T, lvl, thal_like=True)


# ------------------------------------------------------------------ MEDIUM-2
def test_unexplained_flag_not_set_when_level2_deficiency_confident():
    values = {"hemoglobin": 100, "MCV": 112, "vitamin_B12": 90, "ferritin": 100, "CRP": 2, "folate": 10}
    from deficitlens_core.level1 import assess_level1
    from deficitlens_core.normalize import normalize_input
    from deficitlens_core.rules import evaluate_rules
    norm = normalize_input(AnalysisInput(sex="F", age_years=35, values=values), LOCAL)
    rules = evaluate_rules(norm, assess_level1(norm, LOCAL), LOCAL)
    assert "unexplained_checklist" in {f.code for f in rules.flags}                 # правила его ставят…
    fake = Models(case=FakeCase("B12_deficiency_anemia", 0.9), screen=None)
    r = run(values, cfg=LOCAL, models=fake)
    assert r.level2.anemia_class == "B12_deficiency_anemia" and r.level2.confidence in ("moderate", "high")
    assert "unexplained_checklist" not in {f.code for f in r.flags}                 # …а движок снимает
    assert r.checklist                                                              # чек-лист остаётся
    assert "не найден" not in r.reports.doctor.headline
    # уверенность низкая — флаг остаётся
    weak = run(values, cfg=LOCAL, models=Models(case=FakeCase("B12_deficiency_anemia", 0.4), screen=None))
    assert weak.level2.confidence == "low" and "unexplained_checklist" in {f.code for f in weak.flags}
    # недефицитный класс (анемия воспаления) — флаг остаётся
    infl = run(values, cfg=LOCAL, models=Models(case=FakeCase("inflammation_anemia", 0.9), screen=None))
    assert "unexplained_checklist" in {f.code for f in infl.flags}
    # на настоящей модели (если загружена) — пример из ревизии
    real = load_models()
    if real.case is not None:
        r = run(values, cfg=LOCAL, models=real)
        if r.level2.anemia_class in ("B12_deficiency_anemia", "mixed_deficiency") and r.level2.confidence != "low":
            assert "unexplained_checklist" not in {f.code for f in r.flags}


# ------------------------------------------------------------------ MEDIUM-3
SYNTH = [  # придуманные строки в схеме кейса (не данные кейса)
    {"patient_id": "S1", "age_years": 34, "sex": "F", "hemoglobin": 101.5, "MCV": 72.1, "ferritin": 6.2, "CRP": "",
     "anemia": 1, "anemia_class": "iron_deficiency_anemia", "deficiency_cause": "iron_deficiency"},
    {"patient_id": "S2", "age_years": 51, "sex": "M", "hemoglobin": 151.0, "MCV": 90.0, "ferritin": 120, "CRP": 2.5,
     "anemia": 0, "anemia_class": "no_anemia_no_deficiency", "deficiency_cause": "none"},
    {"patient_id": "S3", "age_years": 67, "sex": "F", "hemoglobin": 108.2, "MCV": 104.3, "ferritin": 80, "CRP": 1,
     "anemia": 1, "anemia_class": "B12_deficiency_anemia", "deficiency_cause": "B12_deficiency"},
]


def _synth_variants() -> dict[str, tuple[bytes, str]]:
    import pandas as pd
    df = pd.DataFrame(SYNTH)
    lf_bom = ("﻿" + df.to_csv(index=False)).encode("utf-8")
    crlf = df.to_csv(index=False, lineterminator="\r\n").encode("utf-8")              # CRLF, без BOM
    semi = df.to_csv(index=False, sep=";").replace(".", ",").encode("cp1251")         # «;», десятичная запятая
    shuffled = df.iloc[::-1][list(reversed(df.columns))].to_csv(index=False).encode("utf-8")
    buf = io.BytesIO()
    df.to_excel(buf, index=False)
    return {"lf_bom": (lf_bom, ".csv"), "crlf": (crlf, ".csv"), "semi": (semi, ".csv"),
            "shuffled": (shuffled, ".csv"), "xlsx": (buf.getvalue(), ".xlsx")}


def test_table_fingerprint_ignores_file_format():
    import pandas as pd
    from deficitlens_core.io.table import read_table_bytes, same_as_training, table_fingerprint
    prints = {name: table_fingerprint(read_table_bytes(data, ext)) for name, (data, ext) in _synth_variants().items()}
    assert len(set(prints.values())) == 1 and None not in prints.values(), prints
    changed = pd.DataFrame(SYNTH)
    changed.loc[0, "hemoglobin"] = 101.6
    assert table_fingerprint(read_table_bytes(changed.to_csv(index=False).encode(), ".csv")) != prints["crlf"]
    relabeled = pd.DataFrame(SYNTH)
    relabeled.loc[2, "anemia_class"] = "folate_deficiency_anemia"
    assert table_fingerprint(read_table_bytes(relabeled.to_csv(index=False).encode(), ".csv")) != prints["crlf"]
    no_labels = pd.DataFrame(SYNTH).drop(columns=["anemia", "anemia_class", "deficiency_cause"])
    assert table_fingerprint(read_table_bytes(no_labels.to_csv(index=False).encode(), ".csv")) is None
    meta = {"data": {"sha256": "a" * 64, "table_sha256": prints["xlsx"]}}
    assert same_as_training("b" * 64, prints["crlf"], meta) and same_as_training("a" * 64, None, meta)
    assert not same_as_training("b" * 64, None, meta) and not same_as_training(None, prints["crlf"], {})


def test_benchmark_banner_by_table_fingerprint():
    import pandas as pd
    from deficitlens_api import service
    from deficitlens_core.io.table import read_table_bytes, table_fingerprint
    models = load_models()
    if models.case is None:
        helpers.skip("модель кейса не загружена")
    variants = _synth_variants()
    meta = models.case.metadata
    saved = copy.deepcopy(meta.get("data"))
    try:
        meta.setdefault("data", {})["table_sha256"] = table_fingerprint(read_table_bytes(*variants["lf_bom"]))
        for name in ("crlf", "xlsx", "semi"):
            data, ext = variants[name]
            out = service.benchmark(data, f"case{ext}", mode="clinical")
            assert out["summary"]["metrics"].get("same_as_training_data") is True, name
            assert any("Файл совпадает с набором" in n for n in out["summary"]["notes"]), name
        changed = pd.DataFrame(SYNTH)
        changed.loc[1, "ferritin"] = 121
        out = service.benchmark(changed.to_csv(index=False).encode(), "other.csv", mode="clinical")
        assert not out["summary"]["metrics"].get("same_as_training_data")
    finally:
        meta["data"] = saved


def test_training_file_resaved_is_recognized():
    path = helpers.case_data_path()                                                 # нет файла кейса — SKIP
    import pandas as pd
    from deficitlens_api import service
    from deficitlens_core.io.table import read_table, table_fingerprint
    models = load_models()
    meta = (models.case.metadata if models.case is not None else {}) or {}
    df = read_table(path)
    if table_fingerprint(df) != meta.get("data", {}).get("table_sha256"):
        helpers.skip("файл кейса отличается от набора, на котором обучена модель")
    raw = open(path, "rb").read()
    resaved = raw.decode("utf-8-sig").replace("\r\n", "\n").encode("utf-8")        # LF, без BOM
    crlf = resaved.replace(b"\n", b"\r\n")                                          # CRLF, без BOM
    num = df.copy()
    for c in num.columns:
        conv = pd.to_numeric(num[c], errors="coerce")
        if conv.notna().sum() == (num[c] != "").sum():
            num[c] = conv
    buf = io.BytesIO()
    num.to_excel(buf, index=False)
    for name, data in (("resaved.csv", resaved), ("crlf.csv", crlf), ("case.xlsx", buf.getvalue())):
        out = service.benchmark(data, name, mode="auto")
        assert out["summary"]["metrics"].get("same_as_training_data") is True, name
        assert "cv_metrics" in out or service.cv_metrics() is None, name
