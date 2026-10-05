"""Этап 6: референсы проекта по умолчанию, B12 по NICE NG239, гемолиз и медь в чек-листе, локальные референсы.

Решение капитана 04.10.2026 — docs/review/medic_decisions.md, п. 10 (главнее п. 2 и 5); таблица —
docs/facts/reference_table.md. Тесты — обычные функции с assert (без фикстур).
"""
from __future__ import annotations

import copy
import dataclasses
import re

import helpers  # noqa: F401 — пути src/ и tests/

from deficitlens_core.config import load_config
from deficitlens_core.level1 import assess_level1
from deficitlens_core.normalize import normalize_input
from deficitlens_core.rules import evaluate_rules, foreign_blocked, norm_value
from deficitlens_core.schemas import AnalysisInput, InputError
from deficitlens_core.values_table import build_values_table

CFG = load_config()
ROOT = helpers.ROOT
CASE5 = {"hemoglobin": 102, "MCV": 90, "RDW": 13.5, "ferritin": 180, "CRP": 3, "vitamin_B12": 420, "folate": 11,
         "creatinine": 150}


def norm_of(values, sex="F", age=40, pregnancy=None, cfg=CFG, **extra):
    payload = {"sex": sex, "age_years": age, "values": values, **extra}
    if pregnancy:
        payload["pregnancy"] = pregnancy
    return normalize_input(AnalysisInput.model_validate(payload), cfg)


def rules_of(values, cfg=CFG, **kw):
    norm = norm_of(values, cfg=cfg, **kw)
    return evaluate_rules(norm, assess_level1(norm, cfg), cfg)


def table(values, cfg=CFG, **kw):
    return {r.analyte: r for r in build_values_table(norm_of(values, cfg=cfg, **kw), cfg)}


def analyze(values, sex="F", age=40, **extra):
    from deficitlens_core.engine import analyze as run
    return run(AnalysisInput.model_validate({"sex": sex, "age_years": age, "values": values, **extra}))


def items(out) -> dict:
    return {i.code: i for i in out.checklist}


def lines(report) -> list[str]:
    return [report.headline] + [ln for s in report.sections for ln in s.lines]


# ================================================================== п. 4: B12 по NICE NG239
def test_b12_nice_records_apply_without_global_foreign_policy():
    policy = CFG.norms["policy"]
    assert policy["use_foreign"] is False                                     # глобально не включаем
    assert set(policy["foreign_allowed"]) == {"vitamin_b12", "active_b12", "homocysteine"}
    for key in ("vitamin_b12", "active_b12", "homocysteine"):
        assert CFG.norms[key]["origin"] == "foreign" and CFG.norms[key]["source"] == "nice_ng239", key
        assert not foreign_blocked(CFG, key), key
    assert norm_value(CFG, "vitamin_b12", "deficiency_below") == 180
    assert norm_value(CFG, "vitamin_b12", "gray_zone") == [180, 350]
    assert norm_value(CFG, "active_b12", "deficiency_below") == 25
    assert norm_value(CFG, "active_b12", "gray_zone") == [25, 70]
    assert norm_value(CFG, "homocysteine", "high_above") == 15
    # прочие зарубежные записи по-прежнему не работают
    norms = copy.deepcopy(CFG.norms)
    norms["some_bcsh"] = {"high_above": 1, "origin": "foreign", "source": "bcsh_2014"}
    other = dataclasses.replace(CFG, norms=norms)
    assert foreign_blocked(other, "some_bcsh") and norm_value(other, "some_bcsh", "high_above") is None
    assert "зарубежный источник; в КР РФ числового порога нет" in CFG.source_title("nice_ng239")


def test_b12_gray_zone_flag_and_functional_tests():
    out = rules_of({"hemoglobin": 110, "MCV": 90, "ferritin": 90, "CRP": 1, "folate": 12, "vitamin_B12": 230})
    flag = next(f for f in out.flags if f.code == "b12_gray_zone")
    assert flag.title == "B12 требует функционального подтверждения"
    assert "серой зоне NICE NG239 (180–350 пг/мл; зарубежный источник, в КР РФ числового порога нет)" in flag.text
    assert "зарубежный источник; в КР РФ числового порога нет" in flag.source
    assert [t["analyte"] for t in out.mandatory_tests][:2] == ["active_B12", "homocysteine"]
    for b12 in (180, 350):                                                     # обе границы — серая зона
        assert "b12_gray_zone" in {f.code for f in rules_of({"hemoglobin": 135, "vitamin_B12": b12}).flags}
    low = rules_of({"hemoglobin": 135, "vitamin_B12": 179})
    assert {s.code for s in low.rule_signals} == {"b12_low"} and "NICE NG239" in low.rule_signals[0].text
    assert rules_of({"hemoglobin": 135, "vitamin_B12": 351}).flags == []
    # чек-лист: B12 выше 350 — «исключено»
    status = {i.code: i.status for i in rules_of(CASE5, age=74).checklist}
    assert status["b12"] == "excluded"


def test_b12_values_table_signature_nice():
    t = table({"hemoglobin": 130, "vitamin_B12": 230, "active_B12": 20, "homocysteine": 16})
    assert t["vitamin_B12"].norm.status == "borderline"
    assert "NICE NG239 (зарубежный источник; в КР РФ числового порога нет)" in t["vitamin_B12"].norm.threshold_text
    assert t["active_B12"].norm.status == "low" and t["homocysteine"].norm.status == "high"


def test_b12_engine_flag_not_duplicated_and_model_flag_only_without_numbers():
    r = analyze({"hemoglobin": 110, "MCV": 90, "ferritin": 90, "CRP": 1, "folate": 12, "vitamin_B12": 230})
    assert [f.code for f in r.flags].count("b12_gray_zone") == 1
    assert {"active_B12", "homocysteine"} <= {t.analyte for t in r.next_tests}
    limits = " ".join(next(s for s in r.reports.doctor.sections if s.title == "Ограничения").lines)
    assert "NICE NG239 (зарубежный источник; в КР РФ числового порога нет)" in limits


def test_mma_reference_only_no_diagnostic_threshold():
    assert not any(k in CFG.norms["mma"] for k in ("high_above", "low_below", "deficiency_below"))
    assert CFG.norms["reference_ranges"]["ranges"]["MMA"]["range"] == [None, 0.30]
    t = table({"hemoglobin": 130, "MMA": 0.5})
    assert t["MMA"].norm.status == "high"
    assert "выше референса (верхняя граница 0,3 мкмоль/л)" in t["MMA"].norm.threshold_text
    assert "референса не выше" not in t["MMA"].norm.threshold_text        # без склейки (ревизия UX 05.10.2026)
    assert table({"hemoglobin": 130, "MMA": 0.25})["MMA"].norm.status == "normal"
    out = rules_of({"hemoglobin": 135, "vitamin_B12": 230, "MMA": 0.5})
    text = next(f for f in out.flags if f.code == "b12_gray_zone").text
    assert "ММК 0,5 мкмоль/л — выше референса" in text and "диагностического порога нет" in text
    assert "поддерживает функциональный дефицит B12" not in text.split("ММК")[1]


def test_reference_api_marks_nice_records_active():
    from deficitlens_api import service
    rows = {r["key"]: r for r in service.reference()["thresholds"]}
    for key in ("vitamin_b12", "active_b12", "homocysteine"):
        assert rows[key]["active"] is True and rows[key]["origin"] == "foreign", key


# ================================================================== п. 1: референсы проекта по умолчанию
def _table_rows() -> list[str]:
    text = (ROOT / "docs" / "facts" / "reference_table.md").read_text(encoding="utf-8")
    block = text.split("## Пороги")[1].split("## Источники")[0]
    return [ln.split("|")[1].strip() for ln in block.splitlines()
            if ln.startswith("| ") and not ln.startswith("| Поле") and not ln.startswith("|---")]


def test_reference_ranges_cover_table_with_status_and_units():
    section = CFG.norms["reference_ranges"]
    assert section["status"] == "needs_medical_expert" and section["source"] == "project_reference"
    assert "референс проекта по умолчанию" in CFG.source_title("project_reference")
    ranges = section["ranges"]
    fields = _table_rows()
    assert len(fields) == 35
    for code in fields:
        assert code in ranges, code                                              # каждая строка таблицы
        assert code in CFG.analytes, code                                        # канонический код сервиса
        primary = ranges[code].get("primary")
        assert primary is None or primary in CFG.norms["sources"], code
    # по полу — там, где в таблице по полу
    for code in ("hemoglobin", "RBC", "hematocrit", "ferritin", "ESR", "creatinine"):
        assert set(ranges[code]) >= {"F", "M"}, code
    # фолаты: таблица в нмоль/л (7–45), сервис в нг/мл: ÷ 2,266
    lo, hi = ranges["folate"]["range"]
    assert (lo, hi) == (round(7 / 2.266, 1), round(45 / 2.266, 1)) == (3.1, 19.9)
    assert CFG.analytes["folate"]["unit"] == "ng/mL"
    # пять показателей из вопросов врачу — ответы врача 04.10.2026 (п. 11)
    assert ranges["LDH"]["F"] == [135, 214] and ranges["LDH"]["M"] == [135, 225]
    assert ranges["indirect_bilirubin"]["range"] == [0, 16.4]
    assert ranges["haptoglobin"]["range"] == [0.83, 2.67] and ranges["haptoglobin"]["fallback"] is True
    assert ranges["reticulocytes"]["range"] == [0.68, 1.86]
    assert ranges["ceruloplasmin"]["F"] == [0.16, 0.45] and ranges["ceruloplasmin"]["M"] == [0.15, 0.30]
    assert CFG.norms["no_reference_yet"] == []


def test_reference_does_not_break_diagnostic_thresholds():
    t = table({"hemoglobin": 119, "ferritin": 10, "MCV": 79, "TSAT": 17})
    assert t["hemoglobin"].norm.status == "low" and "ВОЗ 2024" in t["hemoglobin"].norm.threshold_text
    assert t["ferritin"].norm.status == "low" and "ВОЗ 2020" in t["ferritin"].norm.threshold_text
    assert t["MCV"].norm.status == "low" and "микроцитоз" in t["MCV"].norm.threshold_text
    assert t["TSAT"].norm.status == "low" and "КР" in t["TSAT"].norm.threshold_text
    # в пределах референса — текст диагностического порога не меняется
    assert table({"hemoglobin": 130})["hemoglobin"].norm.threshold_text.startswith("не ниже 120 г/л")


def test_reference_statuses_in_values_table():
    f = table({"hemoglobin": 160, "creatinine": 90, "albumin": 30, "WBC": 12, "TSAT": 19, "vitamin_B6": 25,
               "LDH": 300, "haptoglobin": 1.0, "ESR": 25, "TSH": 0.2})
    assert f["hemoglobin"].norm.status == "high" and "выше референса 120–150 г/л" in f["hemoglobin"].norm.threshold_text
    assert "ВОЗ 2024" in f["hemoglobin"].norm.threshold_text                    # первоисточник из листа «Источники»
    assert f["creatinine"].norm.status == "high"                                 # Ж: 44–80
    assert f["albumin"].norm.status == "low" and f["WBC"].norm.status == "high"
    assert f["TSAT"].norm.status == "borderline"                                 # между референсом 20 и порогом КР 17,8
    assert "диагностический порог не достигнут" in f["TSAT"].norm.threshold_text
    assert f["vitamin_B6"].norm.status == "borderline"                           # между 20 и 30 нмоль/л
    assert f["LDH"].norm.status == "high" and f["haptoglobin"].norm.status == "normal"
    assert "Гемотест" in f["LDH"].norm.threshold_text                          # первоисточник — ответ врача
    assert "рабочий порог" in f["LDH"].norm.threshold_text                      # status: needs_medical_expert
    assert f["ESR"].norm.status == "normal" and f["TSH"].norm.status == "low"
    m = table({"hemoglobin": 165, "creatinine": 90, "ESR": 25}, sex="M")
    assert m["hemoglobin"].norm.status == "normal" and m["creatinine"].norm.status == "normal"
    assert m["ESR"].norm.status == "high"                                        # М: < 20 мм/ч


def test_reference_not_applied_in_pregnancy():
    t = table({"hemoglobin": 115, "albumin": 30, "LDH": 300}, pregnancy={"status": "yes", "trimester": 2})
    for analyte in ("albumin", "LDH"):
        assert t[analyte].norm.status == "unknown", analyte
        assert "не оценено — отсутствует референс для беременности" in t[analyte].norm.threshold_text


# ================================================================== п. 2: гемолиз и медь в чек-листе
HEM_BASE = {**CASE5, "creatinine": 70, "TSH": 2}


def test_hemolysis_suspected_excluded_not_checked():
    """Ответ врача 04.10.2026 (п. 11): один признак — не гемолиз; при анемии ≥ 2 признаков — подозрение;
    низкий гаптоглобин + ещё один — сильное подозрение; формат: значения, референс, не оценены, что досдать."""
    sus = items(rules_of({**HEM_BASE, "haptoglobin": 0.1, "LDH": 400}))["hemolysis"]
    assert sus.status == "suspected" and sus.analyte == "haptoglobin"
    # статус уже в названии пункта («Гемолиз — под подозрением»): уточнение строчными, без второго двоеточия
    assert sus.detail.startswith("сильное подозрение; ЛДГ 400 Ед/л")
    assert "гаптоглобин 0,1 г/л — ниже референса 0,83–2,67 г/л" in sus.detail
    assert "ЛДГ 400 Ед/л — выше референса 135–214 Ед/л" in sus.detail
    assert "Не оценены: непрямой билирубин, ретикулоциты" in sus.detail
    assert "сопоставить результат с ОАК и мазком периферической крови" in sus.detail
    assert "не универсальный норматив" in sus.detail                             # гаптоглобин по запасному референсу
    for marker in ({"indirect_bilirubin": 25}, {"reticulocytes": 3.0}):
        assert items(rules_of({**HEM_BASE, "haptoglobin": 0.1, **marker}))["hemolysis"].status == "suspected", marker
    two = items(rules_of({**HEM_BASE, "LDH": 400, "indirect_bilirubin": 25}))["hemolysis"]     # без гаптоглобина
    assert two.status == "suspected" and two.detail.startswith("ЛДГ 400 Ед/л — выше референса") and two.analyte == "LDH"
    # один признак — не гемолиз
    assert items(rules_of({**HEM_BASE, "haptoglobin": 0.1, "LDH": 200}))["hemolysis"].status == "not_checked"
    assert items(rules_of({**HEM_BASE, "LDH": 400}))["hemolysis"].status == "not_checked"
    exc = rules_of({**HEM_BASE, "haptoglobin": 1.0, "LDH": 200})
    assert items(exc)["hemolysis"].status == "excluded"
    assert "haptoglobin" not in [t["analyte"] for t in exc.mandatory_tests]
    nc = rules_of({**HEM_BASE, "LDH": 200})
    assert items(nc)["hemolysis"].status == "not_checked"
    assert "haptoglobin" in [t["analyte"] for t in nc.mandatory_tests]           # что досдать


def test_reticulocytes_corrected_by_hematocrit_in_anemia():
    # Ж: Hct нормы 42 %; 2,2 % × 30 / 42 = 1,57 % — в пределах 0,68–1,86: признака нет, гемолиз не заподозрен
    hem = items(rules_of({**HEM_BASE, "hematocrit": 30, "reticulocytes": 2.2, "haptoglobin": 0.1}))["hemolysis"]
    assert hem.status == "not_checked"
    assert "ретикулоциты 2,2 % (скорректированные по Hct 30 %: 1,57 %) — в пределах референса 0,68–1,86 %" in hem.detail
    # без Hct — исходный процент: 2,2 выше 1,86 — второй признак
    assert items(rules_of({**HEM_BASE, "reticulocytes": 2.2, "haptoglobin": 0.1}))["hemolysis"].status == "suspected"


def test_hemolysis_without_anemia_and_outside_checklist():
    # без анемии — только «есть признаки, требующие проверки», без вывода об анемии; пациенту — нейтрально
    r = analyze({"hemoglobin": 135, "haptoglobin": 0.1, "LDH": 400})
    flag = next(f for f in r.flags if f.code == "hemolysis_signs")
    assert flag.severity == "info" and flag.title == "Есть признаки, требующие проверки"
    assert "вывод о гемолизе не делается" in flag.text and not r.checklist
    assert {"indirect_bilirubin", "reticulocytes"} <= {t.analyte for t in r.next_tests}
    assert NEUTRAL in lines(r.reports.patient)
    assert not analyze({"hemoglobin": 135, "LDH": 400}).flags                    # один признак — ничего
    # анемия с дефицитом железа (чек-лист не строится) — гемолиз отдельным флагом
    r2 = analyze({"hemoglobin": 95, "ferritin": 5, "haptoglobin": 0.1, "LDH": 400, "indirect_bilirubin": 30})
    f2 = next(f for f in r2.flags if f.code == "hemolysis_suspected")
    assert f2.title == "Сильное подозрение на гемолиз" and "Не оценены: ретикулоциты" in f2.text
    assert f2.text.startswith("ЛДГ 400 Ед/л") and "подозрение" not in f2.text.split(".")[0]   # статус — в названии


NEUTRAL = ("Есть показатели, которые требуют обсуждения с врачом в связи с возможным изменением обмена или разрушения "
           "эритроцитов.")


def test_hemolysis_suspected_reports_and_confidence_cap():
    r = analyze({**HEM_BASE, "haptoglobin": 0.1, "LDH": 400})
    assert any(i.code == "hemolysis" and i.status == "suspected" for i in r.checklist)
    assert "гемолиз (гаптоглобин 0,1 г/л)" in r.reports.doctor.headline
    if r.level2.enabled:          # не оценены 2 показателя и референс проекта, а не лаборатории — не выше «низкой»
        assert r.level2.confidence == "low"
        assert "Уверенность понижена до низкой: подозрение на гемолиз" in r.explanation.note
    patient = lines(r.reports.patient)
    assert NEUTRAL in patient
    text = " ".join(patient).lower()
    assert "гаптоглобин" not in text and "лдг" not in text and "гемолиз" not in text
    line = next(ln for ln in patient if "разрушения эритроцитов" in ln)
    assert not re.search(r"\d", line)


def test_copper_with_ceruloplasmin():
    # Ж: церулоплазмин 0,16–0,45 г/л. Один церулоплазмин ниже при нормальной меди — не специфично
    one = items(rules_of({**HEM_BASE, "copper": 15, "ceruloplasmin": 0.15}))["copper"]
    assert one.status == "not_checked" and "при несниженной меди не специфично" in one.detail
    only_cer = items(rules_of({**HEM_BASE, "ceruloplasmin": 0.15}))["copper"]
    assert only_cer.status == "not_checked" and "оценивается только вместе с медью" in only_cer.detail
    # медь ниже, церулоплазмин в норме — дефицит меди не исключён
    cu = items(rules_of({**HEM_BASE, "copper": 11.5, "ceruloplasmin": 0.3},
                        reference_ranges={"copper": {"low": 12, "high": 24}}))["copper"]
    assert cu.status == "not_checked" and "церулоплазмин дефицит меди не исключает" in cu.detail
    ok = items(rules_of({**HEM_BASE, "copper": 15, "ceruloplasmin": 0.3}))["copper"]
    assert ok.status == "excluded" and "церулоплазмин 0,3 г/л — в пределах референса" in ok.detail
    high = items(rules_of({**HEM_BASE, "copper": 15, "ceruloplasmin": 0.6}))["copper"]
    assert high.status == "excluded" and "контекст воспаления или беременности" in high.detail
    # медь ниже референса лаборатории (но не ниже рабочего порога 11) и церулоплазмин ниже — «под подозрением»
    both = rules_of({**HEM_BASE, "copper": 11.5, "ceruloplasmin": 0.15},
                    reference_ranges={"copper": {"low": 12, "high": 24}})
    assert items(both)["copper"].status == "suspected"
    assert "медь 11,5 мкмоль/л — ниже референса 12–24 мкмоль/л" in items(both)["copper"].detail


# ================================================================== п. 3: локальные референсы лаборатории
WARNING = ("Использованы референсные интервалы лаборатории. Они имеют приоритет над значениями по умолчанию проекта; "
           "проверьте единицы, метод и возрастно-половую группу.")


def test_local_reference_from_request_overrides_default_with_warning():
    r = analyze({"hemoglobin": 130, "LDH": 240}, reference_ranges={"LDH": {"low": 140, "high": 220}})
    row = next(v for v in r.values if v.analyte == "LDH")
    assert row.norm.status == "high" and "референс лаборатории" in row.norm.threshold_text
    # 140–220 против 135–214: отличие меньше 10 % — только общее предупреждение
    assert r.references.set == "lab" and r.references.local_analytes == ["LDH"] and r.references.warning == WARNING
    assert WARNING in r.limitations
    limits = next(s for s in r.reports.doctor.sections if s.title == "Ограничения").lines
    assert limits[0] == WARNING
    # отличие больше 10 % — дополнительное предупреждение со значением по умолчанию (аудит)
    far = analyze({"hemoglobin": 130, "LDH": 240}, reference_ranges={"LDH": {"low": 100, "high": 200}})
    assert far.references.warning == WARNING + (" Отличается от значений по умолчанию проекта больше чем на 10 %: "
                                                "ЛДГ 100–200 Ед/л (по умолчанию проекта 135–214 Ед/л).")
    assert not any(WARNING in ln for ln in lines(r.reports.patient))            # пациенту — не нужно
    default = analyze({"hemoglobin": 130, "LDH": 200})
    assert default.references.set == "default" and default.references.warning is None
    assert next(v for v in default.values if v.analyte == "LDH").norm.status == "normal"
    assert WARNING not in default.limitations


def test_local_reference_units_and_errors():
    # единица референса пересчитывается в каноническую: гаптоглобин мг/дл × 0,01 = г/л
    t = table({"hemoglobin": 130, "haptoglobin": 0.35}, reference_ranges={"haptoglobin": {"low": 40, "high": 200,
                                                                                            "unit": "мг/дл"}})
    assert t["haptoglobin"].norm.status == "low" and "0,4–2 г/л" in t["haptoglobin"].norm.threshold_text
    for bad, code in (({"nonsense": {"low": 1}}, "unknown_analyte"), ({"LDH": {"low": 1, "unit": "кг"}}, "unknown_unit"),
                      ({"LDH": {"low": 300, "high": 100}}, "invalid_reference"), ({"LDH": {}}, "invalid_reference")):
        try:
            norm_of({"hemoglobin": 130}, reference_ranges=bad)
        except InputError as e:
            assert e.code == code and e.field.startswith("reference_ranges."), (bad, e.code)
        else:
            raise AssertionError(bad)


def test_local_reference_set_from_config_by_lab_reference_name():
    norms = copy.deepcopy(CFG.norms)
    norms["local_references"] = {"my_lab": {"title": "Лаборатория N", "ranges": {"LDH": {"range": [135, 225]},
                                                                                 "creatinine": {"F": [50, 90],
                                                                                                "M": [70, 120]}}}}
    cfg = dataclasses.replace(CFG, norms=norms)
    t = table({"hemoglobin": 130, "LDH": 230, "creatinine": 85}, cfg=cfg, lab_reference="my_lab")
    assert t["LDH"].norm.status == "high" and "Лаборатория N" in t["LDH"].norm.threshold_text
    assert t["creatinine"].norm.status == "normal"                               # Ж: 50–90 лаборатории
    norm = norm_of({"hemoglobin": 130}, cfg=cfg, lab_reference="my_lab")
    assert norm.references.local and norm.references.lab_name == "my_lab"
    assert sorted(norm.references.local_analytes) == ["LDH", "creatinine"]
    assert not norm_of({"hemoglobin": 130}, cfg=cfg).references.local          # lab_reference: default
    assert CFG.norms["local_references"] == {}                                   # в рабочей конфигурации пусто


def test_api_reference_ranges_field_and_validation():
    from deficitlens_api import service
    out = service.analyze_one({"sex": "F", "age_years": 40, "values": {"hemoglobin": 130, "LDH": 240},
                               "reference_ranges": {"LDH": {"high": 200}}})
    assert out["references"]["warning"].startswith(WARNING) and out["references"]["set"] == "lab"
    try:
        service.analyze_one({"sex": "F", "age_years": 40, "values": {"hemoglobin": 130},
                             "reference_ranges": {"LDH": {"high": "много"}}})
    except service.ServiceError as e:
        assert e.status == 422 and e.field == "reference_ranges"
    else:
        raise AssertionError("ожидалась ошибка 422")


def test_hard_frames_documented_and_editable_in_config():
    text = (ROOT / "config" / "analytes.yaml").read_text(encoding="utf-8")
    assert "hard" in text and "редактир" in text.lower()
    norms_doc = (ROOT / "docs" / "norms.md").read_text(encoding="utf-8")
    assert "analytes.yaml" in norms_doc and "Жёсткие рамки" in norms_doc
