"""Уровень 1: анемия по ВОЗ 2024 — границы порогов, тяжесть, беременность, блок «срочно»."""
from __future__ import annotations

import copy
import dataclasses

import helpers  # noqa: F401 — добавляет src и tests в sys.path

from deficitlens_core.config import load_config
from deficitlens_core.level1 import assess_level1
from deficitlens_core.normalize import normalize_input
from deficitlens_core.schemas import AnalysisInput

CFG = load_config()


def level1(hb, sex="F", age=34, pregnancy=None, cfg=CFG, norms="who", **values):
    payload = {"sex": sex, "age_years": age, "values": {"hemoglobin": hb, **values}, "norms": norms}
    if pregnancy:
        payload["pregnancy"] = pregnancy
    return assess_level1(normalize_input(AnalysisInput.model_validate(payload), cfg), cfg)


def test_women_threshold_is_strict():
    assert level1(119.9, "F").anemia is True
    assert level1(120.0, "F").anemia is False
    assert level1(120.0, "F").threshold_g_l == 120


def test_men_threshold_is_strict():
    assert level1(129.9, "M").anemia is True
    assert level1(130.0, "M").anemia is False
    assert level1(130.0, "M").threshold_g_l == 130


def test_severity_boundaries():
    assert level1(109.9, "F").severity == "moderate"
    assert level1(110.0, "F").severity == "mild"
    assert level1(79.9, "F").severity == "severe"
    assert level1(80.0, "F").severity == "moderate"
    assert level1(109.9, "M").severity == "moderate"
    assert level1(110.0, "M").severity == "mild"
    assert level1(129.9, "M").severity == "mild"
    assert level1(79.9, "M").severity == "severe"
    assert level1(80.0, "M").severity == "moderate"


def test_severity_ru_and_none_without_anemia():
    assert level1(115, "F").severity_ru == "лёгкая"
    assert level1(100, "F").severity_ru == "умеренная"
    assert level1(70, "F").severity_ru == "тяжёлая"
    ok = level1(125, "F")
    assert ok.severity is None and ok.severity_ru is None


def test_pregnancy_second_trimester():
    assert level1(108, pregnancy={"status": "yes", "trimester": 2}).anemia is False     # порог 105
    r = level1(104, pregnancy={"status": "yes", "trimester": 2})
    assert r.anemia is True and r.threshold_g_l == 105 and r.severity == "mild"
    assert level1(105, pregnancy={"status": "yes", "trimester": 2}).anemia is False
    assert level1(94.9, pregnancy={"status": "yes", "trimester": 2}).severity == "moderate"
    assert level1(95, pregnancy={"status": "yes", "trimester": 2}).severity == "mild"
    assert level1(69.9, pregnancy={"status": "yes", "trimester": 2}).severity == "severe"


def test_pregnancy_first_third_and_unknown_trimester():
    for trimester in (1, 3):
        r = level1(109.9, pregnancy={"status": "yes", "trimester": trimester})
        assert r.anemia is True and r.threshold_g_l == 110 and r.severity == "mild"
        assert level1(110, pregnancy={"status": "yes", "trimester": trimester}).anemia is False
        assert level1(99.9, pregnancy={"status": "yes", "trimester": trimester}).severity == "moderate"
        assert level1(100, pregnancy={"status": "yes", "trimester": trimester}).severity == "mild"
    r = level1(108, pregnancy={"status": "yes"})                                        # триместр не указан -> unknown
    assert r.anemia is True and r.threshold_g_l == 110 and "триместр не указан" in r.note


def test_pregnancy_unknown_uses_nonpregnant_threshold_with_warning():
    r = level1(115, pregnancy={"status": "unknown"})
    assert r.anemia is True and r.threshold_g_l == 120
    assert "Беременность не уточнена" in r.note and "нужна оценка врача" in r.note
    assert level1(125, pregnancy={"status": "no"}).note is None


def test_urgent_hemoglobin():
    r = level1(79.9, "F")
    assert [u.code for u in r.urgent] == ["hemoglobin_very_low"]
    assert "выбор проекта" in r.note                                                    # срочность — выбор проекта
    assert "решение команды проекта" in r.urgent[0].source and "врача команды" not in r.urgent[0].source
    assert level1(80.0, "F").urgent == []
    assert level1(75, pregnancy={"status": "yes", "trimester": 2}).urgent == []         # у беременных порог 70
    assert [u.code for u in level1(69.9, pregnancy={"status": "yes", "trimester": 2}).urgent] == ["hemoglobin_very_low"]


def test_urgent_wbc_and_platelets_only_if_submitted():
    assert [u.code for u in level1(130, "F", WBC=1.9).urgent] == ["wbc_very_low"]
    assert level1(130, "F", WBC=2.0).urgent == []
    assert [u.code for u in level1(130, "F", platelets=49).urgent] == ["platelets_very_low"]
    assert level1(130, "F", platelets=50).urgent == []
    assert level1(130, "F").urgent == []
    codes = [u.code for u in level1(68, "M", WBC=1.5, platelets=30).urgent]
    assert codes == ["hemoglobin_very_low", "wbc_very_low", "platelets_very_low"]


def test_age_note_over_65():
    assert CFG.norms["hemoglobin"]["age_note"] in level1(125, "F", age=66).note
    assert level1(125, "F", age=65).note is None
    norms = copy.deepcopy(CFG.norms)                  # граница возраста — из конфигурации (hemoglobin.age_max)
    norms["hemoglobin"]["age_max"] = 60
    assert CFG.norms["hemoglobin"]["age_note"] in level1(125, "F", age=61, cfg=dataclasses.replace(CFG, norms=norms)).note


def test_source_is_who_title():
    assert level1(125).source == CFG.source_title("who_hb_2024")
    assert "ВОЗ" in level1(125).source


def test_ru_norms_add_kr_note():
    assert CFG.norms["hemoglobin"]["ru_note"] in level1(125, norms="ru").note


def test_thresholds_come_from_config():
    norms = copy.deepcopy(CFG.norms)
    norms["hemoglobin"]["anemia_below"]["F"] = 125
    norms["urgent"]["hemoglobin_below"]["default"] = 90
    cfg = dataclasses.replace(CFG, norms=norms)
    r = level1(122, "F", cfg=cfg)
    assert r.anemia is True and r.threshold_g_l == 125
    assert [u.code for u in level1(85, "F", cfg=cfg).urgent] == ["hemoglobin_very_low"]


def case_rows():
    """Строки файла кейса как входы движка (без файла — SKIP). В репозитории данных кейса нет."""
    import pandas as pd

    from deficitlens_core import constants as C
    df = pd.read_csv(helpers.case_data_path())
    sex_map = {str(v).lower(): code for code, variants in CFG.class_mapping["sex_values"].items() for v in variants}
    for _, row in df.iterrows():
        values = {a: float(row[a]) for a in C.ALL_ANALYTES if a in df.columns and not pd.isna(row[a])}
        payload = {"sex": sex_map[str(row["sex"]).strip().lower()], "age_years": int(row["age_years"]), "values": values}
        yield payload, int(row["anemia"])


def test_level1_matches_case_file():
    """Ворота плана: правило ВОЗ совпадает со столбцом anemia во всех строках файла кейса (840 из 840)."""
    total = agree = 0
    for payload, anemia in case_rows():
        norm = normalize_input(AnalysisInput.model_validate(payload), CFG)      # ни одна строка не отклоняется
        assert not [n for n in norm.notes if n.kind in ("soft_range", "ignored")]
        total += 1
        agree += int(int(assess_level1(norm, CFG).anemia) == anemia)
    assert total >= 600 and agree == total, (agree, total)
