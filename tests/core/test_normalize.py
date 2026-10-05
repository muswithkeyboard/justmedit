"""Нормализация ввода: единицы, диапазоны, алиасы, расчётные НТЖ и рСКФ."""
from __future__ import annotations

import math

import helpers  # noqa: F401 — добавляет src и tests в sys.path

from deficitlens_core.config import load_config
from deficitlens_core.normalize import ckd_epi_2021, fmt_num, normalize_input, unit_key
from deficitlens_core.schemas import AnalysisInput, InputError

CFG = load_config()


def make(values, sex="F", age=34, pregnancy=None, norms="who"):
    payload = {"sex": sex, "age_years": age, "values": values, "norms": norms}
    if pregnancy:
        payload["pregnancy"] = pregnancy
    return AnalysisInput.model_validate(payload)


def norm_of(values, **kw):
    return normalize_input(make(values, **kw), CFG)


def error_of(values, **kw) -> InputError:
    try:
        norm_of(values, **kw)
    except InputError as e:
        return e
    raise AssertionError("ожидалась ошибка InputError")


def notes(norm, kind, analyte=None):
    return [n for n in norm.notes if n.kind == kind and (analyte is None or n.analyte == analyte)]


# ------------------------------------------------------------------ пересчёт единиц
def test_b12_pmol_is_converted_to_pg_ml():
    norm = norm_of({"hemoglobin": 124, "vitamin_B12": {"value": 200, "unit": "пмоль/л"}})
    assert math.isclose(norm.values["vitamin_B12"], 271.0, abs_tol=1e-6)        # 200 × 1,355
    note = notes(norm, "converted", "vitamin_B12")[0]
    assert note.original == 200 and math.isclose(note.value, 271.0, abs_tol=1e-6)
    assert note.from_unit == "пмоль/л" and note.to_unit == "pg/mL"
    assert not notes(norm, "unit_assumed", "vitamin_B12")                       # единица указана — пометки нет


def test_folate_nmol_is_converted_to_ng_ml():
    norm = norm_of({"hemoglobin": 124, "folate": {"value": 20, "unit": "nmol/L"}})
    assert math.isclose(norm.values["folate"], 8.826, abs_tol=1e-6)             # 20 × 0,4413 (= 20 ÷ 2,266)
    assert notes(norm, "converted", "folate")


def test_hemoglobin_g_dl_is_converted_to_g_l():
    norm = norm_of({"hemoglobin": {"value": 12.4, "unit": "g/dL"}})
    assert norm.values["hemoglobin"] == 124.0
    note = notes(norm, "converted", "hemoglobin")[0]
    assert note.original == 12.4 and note.value == 124.0 and note.from_unit == "g/dL" and note.to_unit == "g/L"


def test_iron_ug_dl_is_converted_to_umol_l():
    norm = norm_of({"hemoglobin": 124, "serum_iron": {"value": 60, "unit": "мкг/дл"}})
    assert math.isclose(norm.values["serum_iron"], 10.74, abs_tol=1e-6)         # 60 × 0,179


def test_unit_spelling_case_spaces_and_mu_variants():
    assert unit_key(" МкМоль / Л ") == "мкмоль/л"
    micro, mu = chr(0x00B5), chr(0x03BC)                                       # знак «микро» и греческая мю
    assert micro != mu and unit_key(mu + "mol/L") == unit_key(micro + "mol/l")  # в единицах это один символ
    for unit in (micro + "mol/L", mu + "mol/L", "umol/l", " МКМОЛЬ / Л "):
        norm = norm_of({"hemoglobin": 124, "creatinine": {"value": 90, "unit": unit}})
        assert norm.values["creatinine"] == 90
    norm = norm_of({"hemoglobin": 124, "ferritin": {"value": 20, "unit": "ng/mL"}})   # нг/мл = мкг/л: множитель 1
    assert norm.values["ferritin"] == 20 and not notes(norm, "converted")
    norm = norm_of({"hemoglobin": 124, "RBC": {"value": 4.5, "unit": "×10¹²/л"}})     # русская запись канонической единицы
    assert norm.values["RBC"] == 4.5


def test_unknown_unit_is_error_with_allowed_list():
    e = error_of({"hemoglobin": 124, "ferritin": {"value": 20, "unit": "ммоль/л"}})
    assert e.code == "unknown_unit" and e.field == "ferritin"
    assert "мкг/л" in e.message and "ng/ml" in e.message
    assert e.body()["error"]["field"] == "ferritin"


# ------------------------------------------------------------------ диапазоны
def test_out_of_hard_range_is_error():
    e = error_of({"hemoglobin": 300})
    assert e.code == "out_of_range" and e.field == "hemoglobin"
    e = error_of({"hemoglobin": 124, "ferritin": -1})
    assert e.code == "out_of_range" and e.field == "ferritin"
    e = error_of({"hemoglobin": {"value": 30, "unit": "g/dl"}})                 # 300 г/л после пересчёта
    assert e.code == "out_of_range" and e.field == "hemoglobin"


def test_out_of_range_hints_probable_unit():
    e = error_of({"hemoglobin": 12.4})                                          # похоже на г/дл без единицы
    assert e.code == "out_of_range" and "г/дл" in e.message


def test_hard_range_bounds_are_inclusive():
    assert norm_of({"hemoglobin": 20}).values["hemoglobin"] == 20
    assert norm_of({"hemoglobin": 250}).values["hemoglobin"] == 250
    assert error_of({"hemoglobin": 19.9}).code == "out_of_range"
    assert error_of({"hemoglobin": 250.1}).code == "out_of_range"


def test_soft_range_gives_note_not_error():
    norm = norm_of({"hemoglobin": 30})                                          # hard 20–250, soft 40–200
    note = notes(norm, "soft_range", "hemoglobin")[0]
    assert "проверьте" in note.text.lower() and note.value == 30
    assert not notes(norm_of({"hemoglobin": 124}), "soft_range")


# ------------------------------------------------------------------ коды и алиасы
def test_alias_is_resolved_case_insensitively_and_unknown_is_ignored():
    norm = norm_of({"Hb": 124, "ФЕРРИТИН": 20, "wbc": 6.1, "Витамин B12": 300, "xyz": 5})
    assert norm.values["hemoglobin"] == 124 and norm.values["ferritin"] == 20
    assert norm.values["WBC"] == 6.1 and norm.values["vitamin_B12"] == 300
    assert norm.measured == {"hemoglobin", "ferritin", "WBC", "vitamin_B12"}
    ignored = notes(norm, "ignored")
    assert len(ignored) == 1 and ignored[0].analyte == "xyz"


def test_duplicate_analyte_keeps_canonical_and_notes_it():
    norm = norm_of({"Hb": 100, "hemoglobin": 124})
    assert norm.values["hemoglobin"] == 124
    assert notes(norm, "ignored", "hemoglobin")


def test_nan_means_not_submitted():
    norm = norm_of({"hemoglobin": 124, "ferritin": float("nan")})
    assert "ferritin" not in norm.values and "ferritin" not in norm.measured and not norm.notes


def test_unit_assumed_only_for_ambiguous_analytes_without_unit():
    norm = norm_of({"hemoglobin": 124, "vitamin_B12": 300, "folate": 9, "serum_iron": 15, "ferritin": 40})
    assert {n.analyte for n in notes(norm, "unit_assumed")} == {"vitamin_B12", "folate", "serum_iron"}
    norm = norm_of({"hemoglobin": 124, "vitamin_B12": {"value": 300, "unit": "пг/мл"}})
    assert not notes(norm, "unit_assumed")


# ------------------------------------------------------------------ обязательные поля
def test_missing_hemoglobin_is_error():
    e = error_of({"ferritin": 20})
    assert e.code == "missing_hemoglobin" and e.field == "hemoglobin"
    e = error_of({})
    assert e.code == "missing_hemoglobin"


def test_male_pregnancy_is_error():
    e = error_of({"hemoglobin": 140}, sex="M", pregnancy={"status": "yes", "trimester": 2})
    assert e.code == "pregnancy_male" and e.field == "pregnancy"
    norm = norm_of({"hemoglobin": 140}, sex="M", pregnancy={"status": "unknown"})
    assert norm.pregnancy_status == "no"


def test_normalized_fields():
    norm = norm_of({"hemoglobin": 108}, pregnancy={"status": "yes", "trimester": 2}, norms="ru")
    assert (norm.sex, norm.age_years, norm.pregnancy_status, norm.trimester, norm.norms_set) == ("F", 34, "yes", 2, "ru")
    norm = norm_of({"hemoglobin": 108}, pregnancy={"status": "no", "trimester": 2})
    assert norm.trimester is None


# ------------------------------------------------------------------ производные
def test_derived_tsat_from_iron_and_tibc():
    norm = norm_of({"hemoglobin": 104, "serum_iron": 5.5, "TIBC": 50})
    assert norm.values["TSAT"] == 11.0                                           # 5,5 / 50 × 100
    assert "TSAT" not in norm.measured and norm.is_derived("TSAT")
    note = notes(norm, "derived", "TSAT")[0]
    assert note.value == 11.0


def test_measured_tsat_is_not_replaced():
    norm = norm_of({"hemoglobin": 104, "serum_iron": 5.5, "TIBC": 50, "TSAT": 14})
    assert norm.values["TSAT"] == 14 and "TSAT" in norm.measured and not notes(norm, "derived", "TSAT")
    norm = norm_of({"hemoglobin": 104, "serum_iron": 5.5})                       # ОЖСС нет — считать не из чего
    assert "TSAT" not in norm.values


def test_derived_egfr_ckd_epi_2021_woman_74():
    norm = norm_of({"hemoglobin": 105, "creatinine": 160}, sex="F", age=74)
    assert abs(norm.values["eGFR"] - 29.0) < 0.5                                 # женщина 74 лет, креатинин 160 мкмоль/л
    assert "eGFR" not in norm.measured and norm.is_derived("eGFR")
    assert notes(norm, "derived", "eGFR")[0].value == norm.values["eGFR"]


def test_ckd_epi_2021_reference_points():
    # Ручной расчёт по формуле: мужчина 50 лет, 1,0 мг/дл -> 91,7; женщина 30 лет, 0,6 мг/дл -> 123,8.
    assert abs(ckd_epi_2021(88.4, 50, "M") - 91.7) < 0.2
    assert abs(ckd_epi_2021(0.6 * 88.4, 30, "F") - 123.8) < 0.2
    # Креатинин в мг/дл даёт тот же результат, что и в мкмоль/л.
    a = norm_of({"hemoglobin": 140, "creatinine": {"value": 1.0, "unit": "mg/dl"}}, sex="M", age=50)
    b = norm_of({"hemoglobin": 140, "creatinine": 88.4}, sex="M", age=50)
    assert a.values["eGFR"] == b.values["eGFR"]


def test_measured_egfr_wins_over_derived():
    norm = norm_of({"hemoglobin": 105, "creatinine": 160, "eGFR": 45}, sex="F", age=74)
    assert norm.values["eGFR"] == 45 and "eGFR" in norm.measured and not notes(norm, "derived", "eGFR")


def test_egfr_is_not_derived_in_pregnancy():
    norm = norm_of({"hemoglobin": 108, "creatinine": 60}, pregnancy={"status": "yes", "trimester": 2})
    assert "eGFR" not in norm.values
    assert notes(norm, "derived", "eGFR")[0].value is None


def test_fmt_num():
    assert fmt_num(124.0) == "124" and fmt_num(15.2) == "15,2" and fmt_num(17.8) == "17,8"
    assert fmt_num(0.25) == "0,25" and fmt_num(0.0005) == "0,0005" and fmt_num(4.39) == "4,39"
    assert fmt_num(29.04) == "29" and fmt_num(0) == "0"
