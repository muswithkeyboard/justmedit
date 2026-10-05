"""Отчёты врачу и пациенту и таблица значений.

Модели ML здесь не нужны: объекты Level2 / HiddenDeficiency / Screening собираются вручную по schemas.py.
Главные проверки: у пациента нет «%», чисел вероятности и запрещённых слов; у врача ровно одна строка про лечение;
при срочных признаках блок срочности стоит первым; оба отчёта заканчиваются строкой о прототипе.
"""
from __future__ import annotations

import copy
import dataclasses

import helpers
from helpers import load_examples

from deficitlens_core import constants as C
from deficitlens_core.config import load_config
from deficitlens_core.level1 import assess_level1
from deficitlens_core.next_test import rank_next_tests
from deficitlens_core.normalize import normalize_input
from deficitlens_core.reports import (DISCLAIMER, NEUTRAL_PATIENT_LINE, TREATMENT_LINE, build_reports,
                                      filter_patient_line, filter_patient_lines)
from deficitlens_core.rules import evaluate_rules
from deficitlens_core.schemas import (AnalysisInput, ClassProb, Contribution, Explanation, Flag, HiddenDeficiency,
                                      Level2, NextTest, Report, Screening)
from deficitlens_core.values_table import build_values_table

CFG = load_config()
EXAMPLES = {e["id"]: e for e in load_examples()}
CBC1 = EXAMPLES["case1_hidden_iron"]["input"]["values"]


def with_policy(use_foreign: bool, cfg=CFG):
    """Копия конфигурации с явным правилом источников (файл конфигурации не меняется)."""
    norms = copy.deepcopy(cfg.norms)
    norms.setdefault("policy", {})["use_foreign"] = use_foreign
    if not use_foreign:
        norms["policy"]["foreign_allowed"] = []        # и поштучно разрешённых зарубежных записей нет
    return dataclasses.replace(cfg, norms=norms)


# Числа B12 выключены (вариант до решения п. 10): пороги зарубежных руководств не применяются вовсе.
LOCAL = with_policy(False)
FOREIGN = with_policy(True)     # все зарубежные пороги включены
# Рабочая конфигурация (решение п. 10): B12, активный B12 и гомоцистеин — по NICE NG239 через policy.foreign_allowed.


# ------------------------------------------------------------------ сборка без моделей (как движок на одних правилах)
def assemble(payload: dict, level2: Level2 | None = None, hidden: HiddenDeficiency | None = None,
             explanation: Explanation | None = None, next_tests: list[NextTest] | None = None,
             cfg=None, extra_flags: list[Flag] | None = None) -> dict:
    """Сборка отчётов как в движке, но без моделей; extra_flags — флаги, которые движок добавляет сам.
    По умолчанию — правило источников «зарубежные пороги выключены» (LOCAL)."""
    cfg = cfg or LOCAL
    norm = normalize_input(AnalysisInput.model_validate(payload), cfg)
    level1 = assess_level1(norm, cfg)
    rules = evaluate_rules(norm, level1, cfg)
    pregnant = norm.pregnancy_status == "yes"
    if level2 is None:
        level2 = Level2(enabled=False, disabled_reason=(
            "При беременности уровень 2 и модели выключены: пороги и причины анемии другие, нужна оценка врача."
            if pregnant else "Модель уровня 2 не загружена; работают правило ВОЗ и подсказки."))
    if hidden is None:
        if level1.anemia or pregnant:
            hidden = HiddenDeficiency(applicable=False, summary_ru=(
                "Скрытый дефицит оценивается при нормальном гемоглобине; здесь действует основной вывод."))
        else:
            sig = list(rules.rule_signals)
            hidden = HiddenDeficiency(
                applicable=True, detected=bool(sig), rule_signals=sig,
                screening=Screening(applicable=False, reason_not_applicable="Модель скрининга не загружена."),
                summary_ru=("Гемоглобин в норме, но есть признаки скрытого дефицита: " + "; ".join(s.text for s in sig[:2])
                            if sig else "Признаков скрытого дефицита по этим данным не видно."))
    if next_tests is None:
        next_tests = rank_next_tests(norm=norm, hidden=hidden, mandatory_tests=rules.mandatory_tests, case_model=None,
                                     x_row=None, cfg=cfg, allow_information=False)
    flags = list(rules.flags) + list(extra_flags or [])
    values = build_values_table(norm, cfg)
    reports = build_reports(norm=norm, level1=level1, level2=level2, hidden=hidden, flags=flags,
                            checklist=rules.checklist, next_tests=next_tests, explanation=explanation or Explanation(),
                            values=values, cfg=cfg)
    return {"norm": norm, "level1": level1, "rules": rules, "hidden": hidden, "values": values, "reports": reports,
            "next_tests": next_tests, "flags": flags}


def all_lines(report: Report) -> list[str]:
    return [report.headline] + [line for s in report.sections for line in s.lines]


def section(report: Report, title: str) -> list[str]:
    return next(s.lines for s in report.sections if s.title == title)


def titles(report: Report) -> list[str]:
    return [s.title for s in report.sections]


def check_patient(report: Report, finding: bool, where: str = "") -> None:
    """Инварианты текста пациента."""
    lines = all_lines(report)
    for line in lines:
        low = line.lower().replace("ё", "е")
        assert "%" not in line, (where, line)
        assert "процент" not in low, (where, line)
        for word in CFG.forbidden_patient_words:
            assert word.lower().replace("ё", "е") not in low, (where, word, line)
        assert line != NEUTRAL_PATIENT_LINE, (where, "сработал фильтр запрещённых слов", lines)
        assert "{" not in line and "}" not in line, (where, "неподставленный шаблон", line)
        assert "ментцер" not in low, (where, "индекс Ментцера пациенту не показываем", line)
        assert "синтетич" not in low, (where, line)
        if finding:
            assert "здоров" not in low and "все в порядке" not in low, (where, line)
    assert lines[-1] == DISCLAIMER, where
    assert not any("Лечение — по клиническим" in line for line in lines), where
    assert titles(report)[-1] == "Возможна ошибка — почему", where
    for title in ("Главное", "Что это значит", "Что обсудить с врачом", "Какие анализы обсудить",
                  "Возможна ошибка — почему", "Когда обращаться срочно"):
        assert title in titles(report), (where, title)


def check_doctor(report: Report, where: str = "") -> None:
    """Инварианты текста врача."""
    lines = all_lines(report)
    assert lines[-1] == DISCLAIMER, where
    treatment = [line for line in lines if "лечение" in line.lower()]
    assert treatment == [TREATMENT_LINE], (where, treatment)                 # ровно одна строка про лечение
    assert TREATMENT_LINE == ("Лечение — по клиническим рекомендациям Минздрава России (ID 669, 536, 540); "
                              "схемы и дозы прототип не приводит.")
    assert section(report, "Тактика")[-1] == TREATMENT_LINE, where
    for line in lines:
        low = line.lower()
        assert "{" not in line and "}" not in line, (where, "неподставленный шаблон", line)
        assert "синтетич" not in low, (where, line)             # файл кейса — «данные кейса (учебный набор)»
        for dose in ("мг/сут", "мг в сутки", "раз в день", "таблет"):
            assert dose not in low, (where, line)
    for title in ("Уровень 1: анемия по ВОЗ", "Уровень 2: группа и причина", "Что досдать", "Почему такой вывод",
                  "Тактика", "Ограничения"):
        assert title in titles(report), (where, title)
    assert titles(report)[-1] == "Ограничения", where


# ------------------------------------------------------------------ восемь примеров
def test_patient_text_is_clean_on_all_examples():
    for ex in EXAMPLES.values():
        r = assemble(ex["input"])
        finding = r["level1"].anemia or r["hidden"].detected
        check_patient(r["reports"].patient, finding, ex["id"])


def test_doctor_has_single_treatment_line_on_all_examples():
    for ex in EXAMPLES.values():
        check_doctor(assemble(ex["input"])["reports"].doctor, ex["id"])


def test_urgent_block_goes_first_and_into_headline():
    r = assemble(EXAMPLES["case7_urgent"]["input"])
    patient, doctor = r["reports"].patient, r["reports"].doctor
    assert titles(patient)[0] == "Когда обращаться срочно"
    assert patient.headline.startswith("Важно: обратитесь к врачу сегодня.")
    assert "68 г/л" in section(patient, "Когда обращаться срочно")[0]
    assert doctor.headline.startswith("Срочно.")
    assert any(line.startswith("Срочно:") for line in section(doctor, "Уровень 1: анемия по ВОЗ"))
    assert section(doctor, "Тактика")[0].startswith("Срочная очная оценка")
    # без срочных признаков блок срочности есть, но не первый и не в заголовке
    calm = assemble(EXAMPLES["case2_inflammation_mask"]["input"])["reports"].patient
    assert titles(calm)[0] == "Главное" and "Когда обращаться срочно" in titles(calm)
    assert "срочн" not in calm.headline.lower()


def test_urgent_by_wbc_without_anemia():
    r = assemble({"sex": "F", "age_years": 40, "values": {**CBC1, "hemoglobin": 130, "WBC": 1.5}})
    patient = r["reports"].patient
    assert titles(patient)[0] == "Когда обращаться срочно" and patient.headline.startswith("Важно")
    assert "Лейкоциты очень низкие" in section(patient, "Когда обращаться срочно")[0]
    check_patient(patient, False)


def test_case1_headlines_and_followup():
    ex = EXAMPLES["case1_hidden_iron"]
    r = assemble(ex["input"])
    # RDW выше порога: пациенту — не «всё хорошо», а «есть особенности»; запас железа не оценён (ферритин не сдан)
    assert r["reports"].patient.headline == ("Признаков анемии нет, но в анализе есть особенности, "
                                             "которые стоит обсудить с врачом.")
    assert r["reports"].doctor.headline == ("Анемии по ВОЗ нет: Hb 124 г/л при пороге 120 г/л. "
                                            "Запас железа не оценён: ферритин не сдан, скрининг по ОАК не выполнен.")
    meaning = " ".join(section(r["reports"].patient, "Что это значит"))
    assert "различаются по размеру" in meaning and "Запас железа по этим данным оценить нельзя" in meaning
    assert "терапевту" in " ".join(section(r["reports"].patient, "Что обсудить с врачом"))
    clean = assemble({"sex": "F", "age_years": 34, "values": {**CBC1, "RDW": 13.0}})["reports"].patient
    assert clean.headline == "Признаков анемии нет: гемоглобин 124 г/л при пороге 120 г/л."
    assert "Запас железа по этим данным оценить нельзя" in " ".join(section(clean, "Главное"))
    payload = copy.deepcopy(ex["input"])
    payload["values"].update(ex["followup"]["add_values"])
    r = assemble(payload)
    assert r["hidden"].detected is True
    patient, doctor = r["reports"].patient, r["reports"].doctor
    assert patient.headline.startswith("Признаков анемии нет, но")
    assert any("Ферритин снижен (9 мкг/л)" in line for line in section(patient, "Что это значит"))
    assert doctor.headline == ("Анемии по ВОЗ нет: Hb 124 г/л при пороге 120 г/л. "
                               "Есть сигналы скрытого дефицита по порогам.")
    hidden_lines = section(doctor, "Скрытый дефицит")
    assert any("ниже 15 мкг/л" in line and "ВОЗ" in line for line in hidden_lines)
    check_patient(patient, True)
    check_doctor(doctor)


def test_case2_headlines():
    r = assemble(EXAMPLES["case2_inflammation_mask"]["input"])
    assert r["reports"].doctor.headline == ("Умеренная анемия по ВОЗ: Hb 104 г/л при пороге 130 г/л. "
                                            "Обратить внимание: ферритин под маской воспаления.")
    assert r["reports"].patient.headline == ("Гемоглобин ниже порога: 104 г/л при пороге 130 г/л — "
                                             "это похоже на анемию, умеренная степень.")
    flags_section = section(r["reports"].doctor, "На что обратить внимание")
    assert "ложно нормальным" in flags_section[0] and "Источник: ВОЗ, 2020" in flags_section[0]
    assert "условная рекомендация ВОЗ" in flags_section[0]                      # порог 70 мкг/л при воспалении
    why = section(r["reports"].doctor, "Почему такой вывод")                     # микроцитоз: индекс Ментцера врачу
    # Ментцер 19,3 ≥ 13: не «меньше 13 — вероятнее носительство» (ревизия содержания 05.10.2026, п. 1; UX (г))
    assert any(line.startswith("Индекс Ментцера (MCV/RBC) 19,3 — не меньше 13: скорее дефицит железа, чем "
                               "носительство β-талассемии (меньше 13 — повод для электрофореза гемоглобина). "
                               "Источник: Индекс Ментцера") for line in why)
    assert not any("меньше 13 — вероятнее" in line for line in why)
    tests_section = section(r["reports"].doctor, "Что досдать")
    assert any("Растворимые рецепторы трансферрина" in line and "₽" not in line for line in tests_section)   # цен нет
    assert any("Где сдать рядом" in line for line in tests_section)
    assert any("Гемоглобин в ретикулоцитах" in line and "В рознице недоступен" in line for line in tests_section)
    patient_tests = section(r["reports"].patient, "Какие анализы обсудить")
    assert any("Растворимые рецепторы трансферрина" in line for line in patient_tests)
    assert not any("₽" in line or "Цен" in line for line in patient_tests)                 # вместо цены — где сдать
    assert any("Где сдать рядом" in line for line in patient_tests)
    # при маске воспаления пациенту не говорим «ферритин снижен»: значение формально в референсе лаборатории
    assert not any("Ферритин снижен" in line for line in all_lines(r["reports"].patient))


def test_case5_checklist_section_and_hematologist():
    r = assemble(EXAMPLES["case5_unexplained_kidney"]["input"])
    doctor, patient = r["reports"].doctor, r["reports"].patient
    assert doctor.headline == ("Умеренная анемия по ВОЗ: Hb 105 г/л при пороге 120 г/л. "
                               "Обратить внимание: дефицит по сданным анализам не найден — чек-лист исключений. "
                               "Под подозрением: почки (рСКФ 29 мл/мин/1,73 м²).")
    checklist = section(doctor, "Чек-лист исключений")
    assert len(checklist) == 8                     # + «Дефицит меди» с церулоплазмином (решение п. 5)
    assert ("Дефицит витамина B12 — не проверено: сдан: 420 пг/мл; числового порога в КР РФ нет — "
            "оценить по референсу лаборатории.") in checklist
    assert any("Витамин B12 программа по числу не оценивает" in line for line in section(patient, "Что это значит"))
    numeric = assemble(EXAMPLES["case5_unexplained_kidney"]["input"], cfg=FOREIGN)["reports"]
    assert "Дефицит витамина B12 — исключено: B12 420 пг/мл не снижен." in section(numeric.doctor, "Чек-лист исключений")
    assert any("Нехватки железа, витамина B12 и фолатов по сданным анализам не видно" in line
               for line in section(numeric.patient, "Что это значит"))
    assert any(line.startswith("Почки — под подозрением") and "выраженное снижение" in line for line in checklist)
    assert any(line.startswith("Щитовидная железа — не проверено") for line in checklist)
    assert any("Тиреотропный гормон" in line and "₽" not in line for line in section(doctor, "Что досдать"))
    discuss = " ".join(section(patient, "Что обсудить с врачом"))
    assert "терапевту" in discuss and "гематолога" in discuss                    # умеренная анемия
    assert "причина по сданным анализам не видна" not in discuss                 # есть подозрение на почки
    assert any("рСКФ" in line for line in section(patient, "Что это значит"))


def test_case6_patient_does_not_name_hereditary_disease():
    r = assemble(EXAMPLES["case6_microcytosis_not_iron"]["input"])
    patient_text = " ".join(all_lines(r["reports"].patient)).lower()
    assert "талассеми" not in patient_text and "гематолог" in patient_text and "ментцер" not in patient_text
    flag_text = " ".join(section(r["reports"].doctor, "На что обратить внимание"))
    assert "талассемию" in flag_text
    assert "Индекс Ментцера (MCV/RBC) 16,4 — не меньше 13: на носительство β-талассемии не указывает" in flag_text
    # индекс уже есть в тексте флага — отдельной строкой в «Почему такой вывод» не повторяется
    assert not any("Ментцера" in line for line in section(r["reports"].doctor, "Почему такой вывод"))


def test_pregnancy_reports():
    r = assemble(EXAMPLES["case8_pregnancy"]["input"])
    patient, doctor = r["reports"].patient, r["reports"].doctor
    assert "нужна оценка врача" in patient.headline
    assert "нужна оценка врача" in " ".join(section(patient, "Главное"))
    assert "ведёт беременность" in " ".join(section(patient, "Что обсудить с врачом"))
    assert "Уровень 2 не выполнен" in section(doctor, "Уровень 2: группа и причина")[0]
    assert "Скрытый дефицит" not in titles(doctor)                               # при беременности не оценивается
    anemic = assemble({"sex": "F", "age_years": 29, "pregnancy": {"status": "yes", "trimester": 2},
                       "values": {**CBC1, "hemoglobin": 100}})
    check_patient(anemic["reports"].patient, True)
    check_doctor(anemic["reports"].doctor)
    assert any("Ферритин" in line for line in section(anemic["reports"].doctor, "Что досдать"))
    assert "нужна оценка врача" in " ".join(section(anemic["reports"].patient, "Главное"))


def test_level2_disabled_reason_is_shown_to_doctor():
    r = assemble(EXAMPLES["case3_mixed_normal_mcv"]["input"])
    lines = section(r["reports"].doctor, "Уровень 2: группа и причина")
    assert lines == ["Уровень 2 не выполнен: модель уровня 2 не загружена; работают правило ВОЗ и подсказки."]


# ------------------------------------------------------------------ разные сочетания без моделей
EXTRA_SCENARIOS = [
    {"sex": "F", "age_years": 30, "values": {**CBC1, "ferritin": 22}},                                   # порог практики
    {"sex": "F", "age_years": 30, "norms": "ru", "values": {**CBC1, "ferritin": 22}},
    {"sex": "F", "age_years": 30, "values": {**CBC1, "ferritin": 50, "CRP": 15}},                        # маска без анемии
    {"sex": "M", "age_years": 70, "values": {"hemoglobin": 150, "vitamin_B12": 230, "creatinine": 150}},  # серая зона, почки
    {"sex": "M", "age_years": 45, "values": {"hemoglobin": 150, "vitamin_B12": 120, "folate": 3, "TSAT": 12}},
    {"sex": "F", "age_years": 45, "values": {"hemoglobin": 135, "MCV": 70, "MCH": 22, "RBC": 5.9, "ferritin": 90}},
    {"sex": "F", "age_years": 45, "values": {"hemoglobin": 135, "vitamin_B6": 10, "copper": 8, "ceruloplasmin": 0.1}},
    {"sex": "F", "age_years": 52, "pregnancy": {"status": "unknown"}, "values": {"hemoglobin": 112}},
    {"sex": "F", "age_years": 80, "values": {"hemoglobin": 90, "MCV": 110, "ferritin": 200, "CRP": 40, "TSH": 14,
                                              "vitamin_B12": 600, "folate": 15, "LDH": 300, "reticulocytes": 2.1}},
    {"sex": "M", "age_years": 33, "values": {"hemoglobin": 30, "platelets": 20, "WBC": 1.0}},            # soft_range и срочно
    {"sex": "F", "age_years": 33, "values": {"hemoglobin": {"value": 9.8, "unit": "g/dL"},
                                              "vitamin_B12": {"value": 100, "unit": "пмоль/л"},
                                              "folate": {"value": 5, "unit": "нмоль/л"}, "ferritin": 5,
                                              "homocysteine": 25, "MMA": 0.9, "active_B12": 15, "Ret_He": 25}},
    {"sex": "F", "age_years": 61, "values": {"hemoglobin": 105, "MCV": 88, "ferritin": 45, "eGFR": 40, "TSH": 6}},
    {"sex": "M", "age_years": 19, "values": {"hemoglobin": 131}},
    {"sex": "F", "age_years": 29, "pregnancy": {"status": "yes"}, "values": {"hemoglobin": 65, "ferritin": 4}},
]


def test_reports_are_clean_on_extra_scenarios():
    for cfg in (LOCAL, FOREIGN):
        for i, payload in enumerate(EXTRA_SCENARIOS):
            r = assemble(payload, cfg=cfg)
            finding = r["level1"].anemia or r["hidden"].detected
            check_patient(r["reports"].patient, finding, f"scenario {i}")
            check_doctor(r["reports"].doctor, f"scenario {i}")


def test_hematologist_rules_for_patient():
    def discuss(payload, cfg=None):
        return " ".join(section(assemble(payload, cfg=cfg)["reports"].patient, "Что обсудить с врачом"))

    mild = discuss({"sex": "F", "age_years": 30, "values": {"hemoglobin": 115, "ferritin": 9}})
    assert "терапевту" in mild and "гематолог" not in mild
    assert "гематолога" in discuss({"sex": "F", "age_years": 30, "values": {"hemoglobin": 100, "ferritin": 9}})
    mixed = discuss({"sex": "F", "age_years": 30, "values": {"hemoglobin": 115, "ferritin": 9, "folate": 3}})
    assert "нехватка сразу нескольких веществ" in mixed
    b12_values = {"sex": "F", "age_years": 30, "values": {"hemoglobin": 115, "ferritin": 9, "vitamin_B12": 120}}
    assert "нехватка сразу нескольких веществ" in discuss(b12_values, cfg=FOREIGN)
    assert "нехватка сразу нескольких веществ" not in discuss(b12_values, cfg=LOCAL)   # B12 по числу не оценивается
    unexplained = discuss({"sex": "F", "age_years": 30, "values": {"hemoglobin": 115, "MCV": 88, "ferritin": 120,
                                                                    "vitamin_B12": 420, "folate": 12, "CRP": 1}})
    assert "причина по сданным анализам не видна" in unexplained
    healthy = discuss({"sex": "F", "age_years": 30, "values": {"hemoglobin": 135, "ferritin": 80}})
    assert "гематолог" not in healthy and "Если есть жалобы" in healthy


def test_patient_error_section_mentions_missing_tests_and_input_notes():
    r = assemble(EXAMPLES["case7_urgent"]["input"])
    errors = " ".join(section(r["reports"].patient, "Возможна ошибка — почему"))
    assert "Вывод вероятностный" in errors and "Сдан только общий анализ крови" in errors
    assert "Не хватает анализов: ферритин" in errors
    r = assemble({"sex": "F", "age_years": 70, "values": {"hemoglobin": 30, "vitamin_B12": 300}})
    errors = " ".join(section(r["reports"].patient, "Возможна ошибка — почему"))
    assert "выглядят необычно (гемоглобин)" in errors and "не была указана единица измерения" in errors
    assert "до 65 лет" in errors
    doctor_limits = " ".join(section(r["reports"].doctor, "Ограничения"))
    assert "Обработка ввода" in doctor_limits and "нетипичное значение" in doctor_limits


# ------------------------------------------------------------------ объекты моделей, собранные вручную
def manual_level2(cause="iron_deficiency", cls="iron_deficiency_anemia", group="iron_deficiency_anemia",
                  confidence="high", completeness="standard", ambiguous=False, p_top=0.8731) -> Level2:
    cm = CFG.class_mapping
    return Level2(
        enabled=True, mode="clinical", completeness=completeness, completeness_ru=cm["completeness_ru"][completeness],
        case_group=group, case_group_ru=cm["groups5"][group], anemia_class=cls, anemia_class_ru=cm["classes12"][cls],
        deficiency_cause=cause, deficiency_cause_ru=cm["causes11"][cause],
        probabilities={"classes12": {cls: p_top, "anemia_other": round(1 - p_top, 4)},
                       "groups5": {group: p_top, "unexplained_anemia": round(1 - p_top, 4)},
                       "causes11": {cause: p_top, "undetermined": round(1 - p_top, 4)}},
        plausible=[ClassProb(code=cls, name_ru=cm["classes12"][cls], p=p_top),
                   ClassProb(code="anemia_other", name_ru=cm["classes12"]["anemia_other"], p=round(1 - p_top, 3))],
        ambiguous=ambiguous, confidence=confidence, confidence_ru=cm["confidence"]["ru"][confidence])


ANEMIA_PAYLOAD = {"sex": "F", "age_years": 40, "values": {"hemoglobin": 104, "MCV": 76, "RDW": 16, "ferritin": 6, "CRP": 2,
                                                          "vitamin_B12": 500, "folate": 12}}


def test_doctor_report_with_level2_and_explanation():
    explanation = Explanation(
        top_class="iron_deficiency_anemia", runner_up="anemia_other",
        items_for=[Contribution(analyte="ferritin", name_ru="Ферритин", value=6, unit="мкг/л", direction="for", weight=1.42,
                                text="поддерживает вывод «железодефицитная анемия»")],
        items_against=[Contribution(analyte="CRP", name_ru="СРБ", value=2, unit="мг/л", direction="against", weight=0.2,
                                    text="говорит скорее в пользу варианта «анемия без выявленного дефицита»")])
    r = assemble(ANEMIA_PAYLOAD, level2=manual_level2(), explanation=explanation)
    doctor = r["reports"].doctor
    check_doctor(doctor)
    # в заголовке — класс, а не группа из пяти; причина «дефицит железа» следует из класса и не повторяется
    # ферритин 6 мкг/л ниже порога ВОЗ: строка «По порогу» — сразу после уровня 1 (ревизия содержания, п. 2)
    assert doctor.headline == ("Умеренная анемия по ВОЗ: Hb 104 г/л при пороге 120 г/л. "
                               "По порогу: ферритин 6 мкг/л ниже 15 мкг/л (ВОЗ 2020) — дефицит железа. "
                               "Вероятнее всего: железодефицитная анемия; уверенность высокая.")
    level2 = section(doctor, "Уровень 2: группа и причина")
    assert level2[0] == "Класс: железодефицитная анемия. Причина: дефицит железа."
    assert level2[1] == "Группа кейса (5 групп): железодефицитная анемия."
    level2_lines = " ".join(level2)
    assert "железодефицитная анемия — 87 %" in level2_lines and "Правдоподобные альтернативы" in level2_lines
    assert "Уверенность: высокая. Полнота панели: общий анализ крови, обмен железа, СРБ, B12 и фолаты." in level2_lines
    assert "учебном наборе кейса" in level2_lines and "клинический" in level2_lines
    why = section(doctor, "Почему такой вывод")
    assert why[0].startswith("За: Ферритин 6 мкг/л") and why[1].startswith("Против: СРБ 2 мг/л")
    assert not any("(вес" in line for line in why)              # вес вклада — внутренняя величина (п. 5)
    assert any("Ферритин 6 мкг/л: ниже 15 мкг/л — дефицит железа; ВОЗ 2020" in line for line in why)


def test_patient_cause_wording_has_no_numbers():
    # ферритин 6 мкг/л ниже порога ВОЗ: при низкой уверенности модели пациенту — нехватка по порогу (п. 2)
    for confidence, phrase in (("high", "вероятная причина — нехватка железа"),
                               ("moderate", "Возможная причина — нехватка железа"),
                               ("low", "Похоже на нехватку железа (запас железа снижен) — обсудите с врачом.")):
        r = assemble(ANEMIA_PAYLOAD, level2=manual_level2(confidence=confidence))
        patient = r["reports"].patient
        check_patient(patient, True, confidence)
        text = " ".join(all_lines(patient))
        assert phrase in text, (confidence, text)
        for number in ("87", "0,87", "0.87", "0,13", "0.13"):
            assert number not in text, (confidence, number)
        # «предположение сделано моделью» — только если модель причину назвала (п. 15)
        errors = " ".join(section(patient, "Возможна ошибка — почему"))
        assert ("учебном наборе данных кейса" in errors) == (confidence != "low"), (confidence, errors)
    # сработал флаг-подсказка (маска воспаления): даже при высокой уверенности модели — только «возможная причина»
    flagged = assemble(EXAMPLES["case2_inflammation_mask"]["input"],
                       level2=manual_level2(cause="inflammation", cls="inflammation_anemia", group="unexplained_anemia"))
    flagged_main = " ".join(section(flagged["reports"].patient, "Главное"))
    assert "Возможная причина — воспаление" in flagged_main and "вероятная причина" not in flagged_main
    low = " ".join(section(assemble(ANEMIA_PAYLOAD, level2=manual_level2(confidence="low"))["reports"].patient, "Главное"))
    assert "причина — нехватка" not in low and "Похоже на нехватку железа" in low     # модель не называет, порог — да
    no_deficit = {**ANEMIA_PAYLOAD, "values": {**ANEMIA_PAYLOAD["values"], "MCV": 88, "ferritin": 60}}
    low_plain = " ".join(section(assemble(no_deficit, level2=manual_level2(confidence="low"))["reports"].patient,
                                 "Главное"))
    assert "данных мало" in low_plain and "нехват" not in low_plain   # нет ни причины модели, ни порога
    cbc = assemble({"sex": "F", "age_years": 40, "values": {"hemoglobin": 104}},
                   level2=manual_level2(confidence="moderate", completeness="cbc"))
    cbc_main = " ".join(section(cbc["reports"].patient, "Главное"))
    assert "Данных мало: сдан только общий анализ крови" in cbc_main and "Возможная причина — нехватка железа" in cbc_main
    cbc_low = assemble({"sex": "F", "age_years": 40, "values": {"hemoglobin": 104}},
                       level2=manual_level2(confidence="low", completeness="cbc", ambiguous=True, p_top=0.4))
    cbc_low_main = " ".join(section(cbc_low["reports"].patient, "Главное"))
    assert "Причину по нему надёжно назвать нельзя" in cbc_low_main and "нехватка" not in cbc_low_main
    amb = assemble(ANEMIA_PAYLOAD, level2=manual_level2(confidence="moderate", ambiguous=True, p_top=0.6))
    assert "Причин может быть несколько" in " ".join(section(amb["reports"].patient, "Главное"))
    assert "Вывод неоднозначен" in " ".join(section(amb["reports"].doctor, "Уровень 2: группа и причина"))


def test_patient_text_is_clean_for_every_cause_and_confidence():
    cm = CFG.class_mapping
    cause_to_class = {"iron_deficiency": "iron_deficiency_anemia", "B12_deficiency": "B12_deficiency_anemia",
                      "folate_deficiency": "folate_deficiency_anemia", "iron_B12": "mixed_deficiency",
                      "iron_folate": "mixed_deficiency", "B12_folate": "mixed_deficiency",
                      "inflammation": "inflammation_anemia", "undetermined": "anemia_other",
                      "B6_deficiency": "B6_deficiency", "copper_deficiency": "copper_deficiency"}
    assert set(cause_to_class) | {"none"} == set(cm["causes11"])
    for cause, cls in cause_to_class.items():
        group = cm["class_to_group5_if_anemia"][cls]
        for confidence in ("high", "moderate", "low"):
            for completeness in ("cbc", "cbc_iron_crp", "standard", "extended"):
                level2 = manual_level2(cause=cause, cls=cls, group=group, confidence=confidence, completeness=completeness)
                r = assemble(ANEMIA_PAYLOAD, level2=level2)
                check_patient(r["reports"].patient, True, f"{cause}/{confidence}/{completeness}")
                check_doctor(r["reports"].doctor, f"{cause}/{confidence}/{completeness}")
    mixed = assemble(ANEMIA_PAYLOAD, level2=manual_level2(cause="iron_B12", cls="mixed_deficiency",
                                                          group="other_deficiency_anemia", confidence="moderate"))
    assert "гематолога" in " ".join(section(mixed["reports"].patient, "Что обсудить с врачом"))


def manual_hidden(risk="elevated", detected=True, p_any: float | None = 0.7377) -> HiddenDeficiency:
    screening = Screening(
        applicable=True, model="screen-v1", p_ferritin_lt15=0.1842, p_ferritin_lt30=0.5263, risk=risk,
        risk_ru=CFG.screen["risk_ru"][risk],
        explanation_ru=("Ферритин ниже 15 мкг/л находят примерно у 18 из 100 людей с похожим общим анализом крови, "
                        "ниже 30 мкг/л — примерно у 53 из 100. Это оценка по модели, а не измерение; "
                        "модель проверена на данных США (NHANES)."),
        features="full_cbc", group="F18-49", validated_group=True, lab_reference="default",
        recommend_ferritin=risk in ("elevated", "high"))
    return HiddenDeficiency(
        applicable=True, detected=detected, p_any=p_any,
        by_nutrient={} if p_any is None else {"iron": 0.6419, "b12": 0.05, "folate": 0.02},
        rule_signals=[], screening=screening,
        summary_ru=("Гемоглобин в норме, но по общему анализу крови риск скрытого дефицита железа повышенный: "
                    "стоит досдать ферритин." if detected else "Признаков скрытого дефицита по этим данным не видно."))


def ferritin_next_tests() -> list[NextTest]:
    return [NextTest(analyte="ferritin", name_ru="Ферритин", kind="rule", price_rub=845, available=True,
                     reason="Гемоглобин в норме, но по общему анализу крови риск скрытого дефицита железа повышенный. "
                            "Ферритин покажет запас железа.",
                     source="скрининг по общему анализу крови (модель проверена на данных NHANES)"),
            NextTest(analyte="MMA", name_ru="Метилмалоновая кислота", kind="information", information_gain=0.412,
                     price_rub=None, available=False, reason="Лучше остальных различает оставшиеся варианты: "
                                                             "ожидаемый прирост информации 0.41 бит.",
                     source="расчёт по таблицам модели и ценам анализов")]


def test_hidden_deficiency_with_screening_by_cbc_only():
    """Сдан только ОАК, анемии нет: класс не называется, оценки модели кейса нет (p_any = None), решает скрининг."""
    level2 = manual_level2(cause="iron_deficiency", cls="latent_deficiency", group="healthy", confidence="moderate",
                           completeness="cbc", p_top=0.62)
    r = assemble(EXAMPLES["case1_hidden_iron"]["input"], level2=level2, hidden=manual_hidden(p_any=None),
                 next_tests=ferritin_next_tests())
    doctor, patient = r["reports"].doctor, r["reports"].patient
    check_doctor(doctor)
    check_patient(patient, True)
    hidden_lines = section(doctor, "Скрытый дефицит")
    assert any("screen-v1" in line and "риск повышенный" in line and "18 %" in line and "53 %" in line for line in hidden_lines)
    assert not any("Оценка модели" in line for line in hidden_lines)             # p_any = None — строки нет
    level2_lines = section(doctor, "Уровень 2: группа и причина")
    assert level2_lines[0] == "По одному общему анализу крови класс не определяется: данных мало."
    assert level2_lines[1] == "Варианты: скрытый (латентный) дефицит железа; анемия без выявленного дефицита."
    assert "Полнота панели: только общий анализ крови." in level2_lines
    joined = " ".join(level2_lines)
    assert "Класс по модели" not in joined and "62 %" not in joined and "Уверенность:" not in joined
    assert "Ферритин не сдан: ориентир по запасу железа — скрининг по ОАК" in joined
    assert doctor.headline == ("Анемии по ВОЗ нет: Hb 124 г/л при пороге 120 г/л. "
                               "Скрининг по ОАК: риск скрытого дефицита железа повышенный — показан ферритин.")
    tests_lines = section(doctor, "Что досдать")
    assert "₽" not in tests_lines[0] and "В рознице недоступен" in tests_lines[1] and "0.41 бит" in tests_lines[1]
    # пациенту — словами, без чисел вероятности и без «18 из 100»
    assert patient.headline.startswith("Признаков анемии нет, но")
    text = " ".join(all_lines(patient))
    assert "риск скрытой нехватки железа повышенный" in text and "оценка программы" in text
    for number in ("18 из 100", "53", "74", "0,18", "0.18", "0,74", "64", "0.41", "0,41"):
        assert number not in text, number
    patient_tests = section(patient, "Какие анализы обсудить")
    assert patient_tests[0] == "Ферритин: чтобы оценить запас железа."                    # без цены
    assert "Метилмалоновая кислота" in patient_tests[1] and "обычно не сдаётся" in patient_tests[1]


def test_hidden_deficiency_with_case_model_estimate():
    """Сданы анализы вне ОАК: оценка модели выводится отдельной строкой с подписью «по данным кейса (учебный набор)»."""
    level2 = manual_level2(cause="iron_deficiency", cls="latent_deficiency", group="healthy", confidence="moderate",
                           completeness="cbc_iron_crp", p_top=0.62)
    payload = {"sex": "F", "age_years": 34, "values": {**CBC1, "ferritin": 40, "CRP": 1}}
    r = assemble(payload, level2=level2, hidden=manual_hidden(risk="usual", detected=True), next_tests=[])
    doctor, patient = r["reports"].doctor, r["reports"].patient
    check_doctor(doctor)
    check_patient(patient, True)
    model_line = next(line for line in section(doctor, "Скрытый дефицит") if "Оценка модели" in line)
    assert model_line == ("Оценка модели — по данным кейса (учебный набор): вероятность любого дефицита — 74 %; "
                          "по нутриентам: железо — 64 %, витамин B12 — 5 %, фолаты — 2 %.")
    level2_lines = " ".join(section(doctor, "Уровень 2: группа и причина"))
    assert ("Класс по модели — по данным кейса (учебный набор): скрытый (латентный) дефицит железа; "
            "причина: дефицит железа.") in level2_lines
    assert "скрытый (латентный) дефицит железа — 62 %" in level2_lines and "Уверенность: средняя." in level2_lines
    assert "ориентир по запасу железа — скрининг" not in level2_lines           # ферритин сдан
    text = " ".join(all_lines(patient))
    for number in ("74", "64", "62", "0,7", "0.7"):
        assert number not in text, number


def test_hidden_deficiency_detected_only_by_case_model():
    """Сигнал только от модели (скрининг «обычный», порогов не нарушено): подпись «по данным кейса (учебный набор)»."""
    hidden = manual_hidden(risk="usual", detected=True)
    hidden.summary_ru = ("Гемоглобин в норме, но сочетание показателей повышает вероятность скрытого дефицита (железо) — "
                         "оценка модели по данным кейса.")
    r = assemble({"sex": "F", "age_years": 34, "values": {**CBC1, "RDW": 13.0}}, hidden=hidden, next_tests=[])
    doctor, patient = r["reports"].doctor, r["reports"].patient
    assert "Модель указывает на возможный скрытый дефицит — по данным кейса (учебный набор)." in doctor.headline
    assert patient.headline.startswith("Признаков анемии нет, но")
    meaning = " ".join(section(patient, "Что это значит"))
    assert "предположение программы" in meaning and "риск скрытой нехватки железа обычный" in meaning
    check_patient(patient, True)
    check_doctor(doctor)


# ------------------------------------------------------------------ заголовок врача: класс, причина, уверенность
def doctor_headline(level2: Level2, payload=None, **kw) -> str:
    return assemble(payload or ANEMIA_PAYLOAD, level2=level2, **kw)["reports"].doctor.headline


def test_doctor_headline_names_class_and_skips_redundant_cause():
    prefix = ("Умеренная анемия по ВОЗ: Hb 104 г/л при пороге 120 г/л. "
              "По порогу: ферритин 6 мкг/л ниже 15 мкг/л (ВОЗ 2020) — дефицит железа. ")
    # причина следует из класса — не пишется
    assert doctor_headline(manual_level2()) == prefix + "Вероятнее всего: железодефицитная анемия; уверенность высокая."
    b6 = manual_level2(cause="B6_deficiency", cls="B6_deficiency", group="other_deficiency_anemia", confidence="moderate")
    assert doctor_headline(b6) == prefix + "Вероятнее всего: дефицит витамина B6; уверенность средняя."
    infl = manual_level2(cause="inflammation", cls="inflammation_anemia", group="unexplained_anemia", confidence="moderate")
    assert doctor_headline(infl) == prefix + ("Вероятнее всего: анемия воспаления (хронических заболеваний); "
                                              "уверенность средняя.")
    # «анемия без выявленного дефицита» при дефиците по порогу — расхождение называется прямо (п. 2)
    other = manual_level2(cause="undetermined", cls="anemia_other", group="unexplained_anemia", confidence="moderate")
    assert doctor_headline(other) == prefix + ("Класс по модели — анемия без выявленного дефицита (уверенность "
                                               "средняя) — расходится с порогом: решает врач.")
    # без дефицита по порогу «причина не установлена» не пишется
    plain = {**ANEMIA_PAYLOAD, "values": {**ANEMIA_PAYLOAD["values"], "MCV": 88, "ferritin": 60}}
    assert doctor_headline(other, plain).startswith(
        "Умеренная анемия по ВОЗ: Hb 104 г/л при пороге 120 г/л. "
        "Вероятнее всего: анемия без выявленного дефицита; уверенность средняя.")
    assert "причина" not in doctor_headline(other, plain).lower()
    # у смешанного дефицита три подтипа — причина уточняет класс
    mixed = manual_level2(cause="iron_folate", cls="mixed_deficiency", group="other_deficiency_anemia", confidence="moderate")
    assert doctor_headline(mixed) == prefix + ("Вероятнее всего: анемия смешанного дефицита; "
                                               "причина: сочетание — железо и фолаты; уверенность средняя.")
    for level2 in (manual_level2(), b6, infl, other, mixed):
        headline = doctor_headline(level2)
        assert "Группа" not in headline                                          # группа из пяти — только в разделе
    section_lines = section(assemble(ANEMIA_PAYLOAD, level2=mixed)["reports"].doctor, "Уровень 2: группа и причина")
    # одно двоеточие в строке (п. 10): «сочетание — железо и фолаты»
    assert section_lines[0] == "Класс: анемия смешанного дефицита. Причина: сочетание — железо и фолаты."
    assert section_lines[1] == "Группа кейса (5 групп): другая дефицитная анемия (фолаты, медь, B6, смешанная)."
    other_lines = section(assemble(ANEMIA_PAYLOAD, level2=other)["reports"].doctor, "Уровень 2: группа и причина")
    assert other_lines[0] == "Класс: анемия без выявленного дефицита. Причина не установлена."


def test_doctor_headline_low_confidence_lists_variants():
    low = manual_level2(cause="iron_folate", cls="mixed_deficiency", group="other_deficiency_anemia", confidence="low",
                        completeness="cbc", ambiguous=True, p_top=0.41)
    r = assemble(EXAMPLES["case7_urgent"]["input"], level2=low)
    assert r["reports"].doctor.headline == (
        "Срочно. Тяжёлая анемия по ВОЗ: Hb 68 г/л при пороге 130 г/л. По этим данным причина не определяется; "
        "варианты: анемия смешанного дефицита, анемия без выявленного дефицита.")
    assert "ероятнее всего" not in r["reports"].doctor.headline
    level2_lines = " ".join(section(r["reports"].doctor, "Уровень 2: группа и причина"))
    assert "Уверенность: низкая." in level2_lines and "причина по этим данным не определяется" in level2_lines
    # низкая уверенность: «Наиболее вероятный класс», без строки «Причина:» (п. 6)
    first = section(r["reports"].doctor, "Уровень 2: группа и причина")[0]
    assert first == "Наиболее вероятный класс: анемия смешанного дефицита." and "Причина:" not in level2_lines
    # микроцитоз без флага микроцитоза: индекс Ментцера — строкой в «Почему такой вывод»; пациенту его нет
    why = section(r["reports"].doctor, "Почему такой вывод")
    assert any(line.startswith("Индекс Ментцера (MCV/RBC) 26,9 — не меньше 13: скорее дефицит железа") for line in why)
    check_patient(r["reports"].patient, True)
    check_doctor(r["reports"].doctor)


def test_doctor_headline_shows_suspected_checklist_items():
    other = manual_level2(cause="undetermined", cls="anemia_other", group="unexplained_anemia", confidence="moderate",
                          completeness="extended")
    note = "Уверенность понижена до средней: под подозрением: Почки."
    r = assemble(EXAMPLES["case5_unexplained_kidney"]["input"], level2=other, explanation=Explanation(note=note))
    doctor = r["reports"].doctor
    assert doctor.headline == ("Умеренная анемия по ВОЗ: Hb 105 г/л при пороге 120 г/л. "
                               "Вероятнее всего: анемия без выявленного дефицита; уверенность средняя. "
                               "Обратить внимание: дефицит по сданным анализам не найден — чек-лист исключений. "
                               "Под подозрением: почки (рСКФ 29 мл/мин/1,73 м²).")
    # фраза движка о понижении уверенности — в разделе «Уровень 2», а не в «Почему такой вывод»
    assert note in section(doctor, "Уровень 2: группа и причина")
    assert not any("Уверенность понижена" in line for line in section(doctor, "Почему такой вывод"))
    several = assemble({"sex": "F", "age_years": 61, "values": {"hemoglobin": 105, "MCV": 88, "ferritin": 200, "CRP": 40,
                                                                "folate": 6, "eGFR": 40, "TSH": 12}})
    assert several["reports"].doctor.headline.endswith(
        "Под подозрением: дефицит фолатов (фолаты 6 нг/мл); воспаление (СРБ 40 мг/л); "
        "почки (рСКФ 40 мл/мин/1,73 м²); щитовидная железа (ТТГ 12 мМЕ/л).")


def test_confidence_cap_note_is_split_between_sections():
    note = ("Вывод сделан по общему анализу крови: анализы вне ОАК не сданы. "
            "Уверенность понижена до средней: Ферритин под маской воспаления.")
    infl = manual_level2(cause="inflammation", cls="inflammation_anemia", group="unexplained_anemia", confidence="moderate",
                         completeness="cbc_iron_crp")
    r = assemble(EXAMPLES["case2_inflammation_mask"]["input"], level2=infl, explanation=Explanation(note=note))
    doctor = r["reports"].doctor
    assert doctor.headline == ("Умеренная анемия по ВОЗ: Hb 104 г/л при пороге 130 г/л. "
                               "Вероятнее всего: анемия воспаления (хронических заболеваний); уверенность средняя. "
                               "Обратить внимание: ферритин под маской воспаления.")
    level2_lines = section(doctor, "Уровень 2: группа и причина")
    assert "Уверенность понижена до средней: Ферритин под маской воспаления." in level2_lines
    why = section(doctor, "Почему такой вывод")
    assert "Вывод сделан по общему анализу крови: анализы вне ОАК не сданы." in why
    assert not any("Уверенность понижена" in line for line in why)
    check_doctor(doctor)


def test_patient_line_for_b12_flag_passes_filter():
    """Флаг b12_gray_zone (числовой — из правил, или по неуверенности модели — из движка): нейтральная строка пациенту."""
    line = ("Общий витамин B12 сам по себе не даёт ответа — обсудите с врачом дополнительный анализ "
            "(активный B12 или гомоцистеин).")
    assert filter_patient_line(line, CFG.forbidden_patient_words) == line
    assert CFG.texts["patient"]["sections"]["meaning"]["flags"]["b12_gray_zone"] == line
    payload = EXAMPLES["case4_b12_gray_zone"]["input"]
    engine_flag = Flag(code="b12_gray_zone", title="B12 требует функционального подтверждения",
                       text="Общий витамин B12 230 пг/мл сам по себе дефицит не подтверждает и не исключает.",
                       source="условие кейса")
    local = assemble(payload, cfg=LOCAL, extra_flags=[engine_flag])              # флаг добавил движок
    numeric = assemble(payload, cfg=FOREIGN)                                     # флаг поставили правила по порогу
    for r in (local, numeric):
        assert "b12_gray_zone" in {f.code for f in r["flags"]}
        assert line in section(r["reports"].patient, "Что это значит")
        check_patient(r["reports"].patient, True)
        check_doctor(r["reports"].doctor)
        tactics = " ".join(section(r["reports"].doctor, "Тактика"))
        assert "B12 требует функционального подтверждения: активный B12 или гомоцистеин." in tactics
    assert "B12 требует функционального подтверждения" in local["reports"].doctor.headline
    # без числовых порогов пациенту не пишем «B12 в пограничной зоне»: программа число не оценивает
    assert not any("пограничной зоне" in text for text in all_lines(local["reports"].patient))
    assert any("пограничной зоне" in text for text in all_lines(numeric["reports"].patient))
    limits_local = " ".join(section(local["reports"].doctor, "Ограничения"))
    limits_numeric = " ".join(section(numeric["reports"].doctor, "Ограничения"))
    assert "Числовые пороги B12, активного B12 и гомоцистеина не применяются" in limits_local
    assert "NICE NG239 (зарубежный источник; в КР РФ числового порога нет)" in limits_numeric


def test_patient_unexplained_wording_is_consistent():
    """Чек-лист без найденной причины и предположение модели не должны спорить в тексте пациента."""
    payload = EXAMPLES["case4_b12_gray_zone"]["input"]
    rules_only = assemble(payload, cfg=LOCAL)["reports"].patient
    assert any("проверено не всё" in line for line in section(rules_only, "Что это значит"))
    b12 = manual_level2(cause="B12_deficiency", cls="B12_deficiency_anemia", group="vitamin_B12_deficiency_anemia",
                        confidence="moderate", ambiguous=True, p_top=0.7)
    named = assemble(payload, cfg=LOCAL, level2=b12)["reports"].patient
    assert "нехватка витамина B12" in " ".join(section(named, "Главное"))
    assert not any("проверено не всё" in line for line in section(named, "Что это значит"))
    check_patient(named, True)
    # «причина не установлена»: не утверждаем про B12, который программа по числу не оценивает
    other = manual_level2(cause="undetermined", cls="anemia_other", group="unexplained_anemia", confidence="moderate",
                          completeness="extended")
    case5 = assemble(EXAMPLES["case5_unexplained_kidney"]["input"], cfg=LOCAL, level2=other)["reports"].patient
    main = " ".join(section(case5, "Главное"))
    assert "программа не видит причину снижения гемоглобина" in main and "B12" not in main
    assert any("Витамин B12 программа по числу не оценивает" in line for line in section(case5, "Что это значит"))
    check_patient(case5, True)


def test_texts_call_case_file_training_set_not_synthetic():
    """Кейсодатель называет файл обезличенными данными пациентов: слова «синтетический» в текстах нет."""
    def walk(node):
        if isinstance(node, dict):
            for value in node.values():
                yield from walk(value)
        elif isinstance(node, list):
            for value in node:
                yield from walk(value)
        elif isinstance(node, str):
            yield node

    for name in ("doctor", "patient"):
        for text in walk(CFG.texts[name]):
            assert "синтетич" not in text.lower(), (name, text)
    doctor_text = " ".join(walk(CFG.texts["doctor"]))
    assert "по данным кейса (учебный набор)" in doctor_text


def test_no_reassurance_words_when_nothing_found():
    r = assemble({"sex": "F", "age_years": 34, "values": {**CBC1, "RDW": 13.0}},
                 hidden=manual_hidden(risk="usual", detected=False, p_any=0.08), next_tests=[])
    patient = r["reports"].patient
    check_patient(patient, False)
    text = " ".join(all_lines(patient)).lower()
    assert "здоров" not in text and "всё в порядке" not in text                      # это слово не используем вообще
    assert "Признаков скрытого дефицита по этим данным не видно." in section(patient, "Главное")
    assert "По этим данным программа не предлагает дополнительных анализов." in section(patient, "Какие анализы обсудить")


# ------------------------------------------------------------------ фильтр запрещённых слов
def test_filter_replaces_forbidden_lines():
    words = CFG.forbidden_patient_words
    assert len(words) > 30 and all(w == w.lower() for w in words)
    for bad in ("Принимайте железо по 100 мг в сутки", "Пропейте курс лечения", "Купите БАД", "Нужны таблетки",
                "ДИАГНОЗ: анемия", "Это может быть рак", "Похоже на лейкоз", "Риск 35 %", "Сорбифер помогает",
                "Соблюдайте диету", "Дозировка подбирается"):
        assert filter_patient_line(bad, words) == NEUTRAL_PATIENT_LINE, bad
    good = "Покажите результаты терапевту."
    assert filter_patient_line(good, words) == good
    assert filter_patient_line(DISCLAIMER, words) == DISCLAIMER
    assert filter_patient_line(NEUTRAL_PATIENT_LINE, words) == NEUTRAL_PATIENT_LINE


def test_filter_never_fails():
    assert filter_patient_line(None, None) == "None"
    assert filter_patient_line(123, ["12"]) == NEUTRAL_PATIENT_LINE
    assert filter_patient_line("текст", [None, "", 5]) == "текст"

    class Broken:
        def __str__(self):
            raise RuntimeError("нет строки")

    assert filter_patient_line(Broken(), ["а"]) == NEUTRAL_PATIENT_LINE
    lines = filter_patient_lines(["Принимайте препарат", "Курс лечения 3 месяца", "Обычная строка"], ["принимайте", "курс лечения"])
    assert lines == [NEUTRAL_PATIENT_LINE, "Обычная строка"]                        # подряд идущие замены схлопнуты


def test_filter_is_applied_inside_patient_report():
    bad = [NextTest(analyte="ferritin", name_ru="Сорбифер в таблетках", kind="rule", price_rub=845, reason="—")]
    r = assemble(EXAMPLES["case7_urgent"]["input"], next_tests=bad)
    tests_lines = section(r["reports"].patient, "Какие анализы обсудить")
    assert tests_lines[0] == NEUTRAL_PATIENT_LINE and "Сорбифер" not in " ".join(all_lines(r["reports"].patient))
    assert all_lines(r["reports"].patient)[-1] == DISCLAIMER
    # слово «здоров» запрещено, если есть анемия или сигнал скрытого дефицита
    extra = [NextTest(analyte="ferritin", name_ru="Вы здоровы", kind="rule", price_rub=1, reason="—")]
    with_finding = assemble(EXAMPLES["case7_urgent"]["input"], next_tests=extra)
    assert section(with_finding["reports"].patient, "Какие анализы обсудить")[0] == NEUTRAL_PATIENT_LINE


def test_patient_templates_and_analyte_names_have_no_forbidden_words():
    words = [w.lower().replace("ё", "е") for w in CFG.forbidden_patient_words]

    def walk(node, path=""):
        if isinstance(node, dict):
            for key, value in node.items():
                if key != "forbidden_if_finding":
                    yield from walk(value, f"{path}.{key}")
        elif isinstance(node, list):
            for i, value in enumerate(node):
                yield from walk(value, f"{path}[{i}]")
        elif isinstance(node, str):
            yield path, node

    for path, text in walk(CFG.texts["patient"]):
        low = text.lower().replace("ё", "е")
        for word in words:
            assert word not in low, (path, word, text)
        assert "здоров" not in low and "все в порядке" not in low, (path, text)
    for code, spec in CFG.analytes.items():
        low = spec["name_ru"].lower().replace("ё", "е")
        for word in words:
            assert word not in low, (code, word)
    assert CFG.texts["patient"]["disclaimer"] == DISCLAIMER == CFG.texts["doctor"]["disclaimer"]
    assert CFG.texts["doctor"]["treatment_line"] == TREATMENT_LINE


def test_reports_survive_missing_text_files():
    """Без config/texts_ru отчёты не падают, а обязательные строки (лечение, прототип) остаются."""
    import dataclasses

    from deficitlens_core.reports import build_reports as build
    bare = dataclasses.replace(CFG, texts={}, forbidden_patient_words=())
    for ex in EXAMPLES.values():
        r = assemble(ex["input"])
        reports = build(norm=r["norm"], level1=r["level1"], level2=Level2(enabled=False), hidden=r["hidden"],
                        flags=r["rules"].flags, checklist=r["rules"].checklist, next_tests=r["next_tests"],
                        explanation=Explanation(), values=r["values"], cfg=bare)
        assert all_lines(reports.doctor)[-1] == DISCLAIMER and all_lines(reports.patient)[-1] == DISCLAIMER
        assert TREATMENT_LINE in all_lines(reports.doctor)
        assert reports.doctor.headline and reports.patient.headline
        assert all(s.title and not s.title.startswith("sections.") for s in reports.doctor.sections + reports.patient.sections)


# ------------------------------------------------------------------ таблица значений
def table(values, sex="F", age=40, norms="who", pregnancy=None, contributions=None, cfg=None):
    cfg = cfg or LOCAL
    payload = {"sex": sex, "age_years": age, "norms": norms, "values": values}
    if pregnancy:
        payload["pregnancy"] = pregnancy
    norm = normalize_input(AnalysisInput.model_validate(payload), cfg)
    return {row.analyte: row for row in build_values_table(norm, cfg, contributions)}


def test_values_table_order_and_derived_rows():
    payload = EXAMPLES["case5_unexplained_kidney"]["input"]
    norm = normalize_input(AnalysisInput.model_validate(payload), CFG)
    rows = build_values_table(norm, CFG)
    order = [a for a in C.CBC_LABS + C.LABS if a in norm.values]
    assert [r.analyte for r in rows] == order
    assert rows[0].analyte == "hemoglobin" and rows[-1].analyte == "eGFR"
    assert "расчёт" in rows[-1].name_ru and rows[-1].norm.status == "low"
    t = table({"hemoglobin": 104, "serum_iron": 5.5, "TIBC": 50})
    assert t["TSAT"].value == 11.0 and "расчёт" in t["TSAT"].name_ru and t["TSAT"].norm.status == "low"
    assert t["TSAT"].unit == "%" and t["hemoglobin"].unit == "г/л"


def test_values_table_hemoglobin_follows_level1():
    assert table({"hemoglobin": 119.9})["hemoglobin"].norm.status == "low"
    row = table({"hemoglobin": 120})["hemoglobin"]
    assert row.norm.status == "normal" and "120 г/л" in row.norm.threshold_text and "ВОЗ 2024" in row.norm.threshold_text
    assert row.norm.source == CFG.source_title("who_hb_2024")
    assert table({"hemoglobin": 125}, sex="M")["hemoglobin"].norm.status == "low"
    preg = table({"hemoglobin": 108}, pregnancy={"status": "yes", "trimester": 2})["hemoglobin"]
    assert preg.norm.status == "normal" and "105 г/л" in preg.norm.threshold_text and "II триместр" in preg.norm.threshold_text


def test_values_table_ferritin_depends_on_norms_set_and_inflammation():
    low = table({"hemoglobin": 130, "ferritin": 9})["ferritin"].norm
    assert low.status == "low" and low.threshold_text == "ниже 15 мкг/л — дефицит железа; ВОЗ 2020"
    assert low.source == CFG.source_title("who_ferritin_2020")
    who = table({"hemoglobin": 130, "ferritin": 20}, norms="who")["ferritin"].norm
    assert who.status == "borderline" and "порог практики" in who.threshold_text
    ru = table({"hemoglobin": 130, "ferritin": 20}, norms="ru")["ferritin"].norm
    assert ru.status == "low" and "ниже 30 мкг/л" in ru.threshold_text and "порог практики" in ru.threshold_text
    assert ru.source == CFG.source_title("kr_ida_2024")
    assert table({"hemoglobin": 130, "ferritin": 30}, norms="ru")["ferritin"].norm.status == "normal"
    assert table({"hemoglobin": 130, "ferritin": 15}, norms="who")["ferritin"].norm.status == "borderline"
    masked = table({"hemoglobin": 130, "ferritin": 62, "CRP": 28})["ferritin"].norm
    assert masked.status == "low" and "70 мкг/л при воспалении" in masked.threshold_text
    assert table({"hemoglobin": 130, "ferritin": 100, "CRP": 28})["ferritin"].norm.status == "normal"
    no_crp = table({"hemoglobin": 130, "ferritin": 45})["ferritin"].norm
    assert no_crp.status == "normal" and "СРБ не сдан" in no_crp.threshold_text


def test_values_table_statuses_by_thresholds():
    t = table({"hemoglobin": 130, "MCV": 79.9, "MCH": 26.9, "RDW": 14.6, "vitamin_B12": 230, "active_B12": 20,
               "folate": 6, "CRP": 6, "eGFR": 45, "TSH": 12, "homocysteine": 16, "MMA": 0.5, "vitamin_B6": 19,
               "copper": 10, "ceruloplasmin": 0.1, "serum_iron": 40, "TIBC": 40, "transferrin": 4, "Ret_He": 28},
              cfg=FOREIGN)
    expected = {"MCV": "low", "MCH": "low", "RDW": "high", "vitamin_B12": "borderline", "active_B12": "low",
                "folate": "borderline", "CRP": "high", "eGFR": "low", "TSH": "high", "homocysteine": "high",
                "MMA": "high", "vitamin_B6": "low", "copper": "low", "ceruloplasmin": "low", "serum_iron": "high",
                "TIBC": "low", "transferrin": "high", "Ret_He": "low"}
    assert {a: t[a].norm.status for a in expected} == expected
    assert "выраженное повышение" in t["TSH"].norm.threshold_text
    assert "NICE NG239" in t["vitamin_B12"].norm.threshold_text and "рабочий порог" in t["vitamin_B12"].norm.threshold_text
    assert t["vitamin_B12"].norm.source == CFG.source_title("nice_ng239")
    ok = table({"hemoglobin": 130, "MCV": 80, "MCH": 27, "RDW": 14.5, "vitamin_B12": 350.1, "active_B12": 70.1,
                "folate": 8, "CRP": 5, "eGFR": 60, "TSH": 4.0, "homocysteine": 15, "serum_iron": 20, "TIBC": 60,
                "Ret_He": 30.6, "TSAT": 17.8}, cfg=FOREIGN)
    # НТЖ 17,8 % и рСКФ 60 не ниже диагностических порогов, но ниже референса проекта (20–45 %; ≥ 90): «пограничное»
    assert {a: r.norm.status for a, r in ok.items() if r.norm.status != "normal"} == {"TSAT": "borderline",
                                                                                     "eGFR": "borderline"}
    assert ok["vitamin_B12"].norm.threshold_text.startswith("выше 350 пг/мл")
    # серая зона включает обе границы (gray_zone_inclusive: true): 350 пг/мл и 70 пмоль/л — ещё серая зона
    edge = table({"hemoglobin": 130, "vitamin_B12": 350, "active_B12": 70}, cfg=FOREIGN)
    assert edge["vitamin_B12"].norm.status == "borderline" and edge["active_B12"].norm.status == "borderline"
    assert table({"hemoglobin": 130, "vitamin_B12": 180}, cfg=FOREIGN)["vitamin_B12"].norm.status == "borderline"
    assert table({"hemoglobin": 130, "vitamin_B12": 179.9}, cfg=FOREIGN)["vitamin_B12"].norm.status == "low"
    egfr = table({"hemoglobin": 130, "eGFR": 29})["eGFR"].norm
    assert egfr.threshold_text.startswith("ниже 30") and "КР «ХБП»" in egfr.threshold_text
    assert egfr.source == CFG.source_title("kr_ckd")


def test_values_table_source_policy():
    """Правило источников: пороги с origin: foreign при use_foreign: false не применяются — статус unknown."""
    values = {"hemoglobin": 130, "vitamin_B12": 100, "active_B12": 10, "homocysteine": 40, "MMA": 0.9, "folate": 3}
    local = table(values, cfg=LOCAL)
    for analyte in ("vitamin_B12", "active_B12", "homocysteine"):
        info = local[analyte].norm
        assert info.status == "unknown", analyte
        assert info.threshold_text == "числового порога в КР РФ нет; оценка по референсу лаборатории", analyte
        assert "NICE" not in info.source and "BCSH" not in info.source, analyte
    assert local["folate"].norm.status == "low" and local["MMA"].norm.status == "high"     # не зарубежные записи
    numeric = table(values, cfg=FOREIGN)
    assert [numeric[a].norm.status for a in ("vitamin_B12", "active_B12", "homocysteine")] == ["low", "low", "high"]
    assert "NICE NG239" in numeric["vitamin_B12"].norm.threshold_text
    assert numeric["vitamin_B12"].norm.source == CFG.source_title("nice_ng239")


def test_values_table_unknown_without_reference():
    values = {"hemoglobin": 130, "RBC": 4.5, "hematocrit": 40, "MCHC": 330, "platelets": 250, "WBC": 6, "LDH": 200,
              "indirect_bilirubin": 10, "haptoglobin": 1, "reticulocytes": 1, "albumin": 40, "ESR": 10, "creatinine": 80,
              "UIBC": 40, "sTfR": 3, "ceruloplasmin": 0.3}
    t = table(values)
    for analyte in CFG.norms["no_reference_yet"]:
        assert t[analyte].norm.status == "unknown", analyte
        assert t[analyte].norm.threshold_text == "оценка по референсу лаборатории", analyte


def test_values_table_contribution_and_warning():
    t = table({"hemoglobin": 30, "ferritin": 9}, contributions={"ferritin": "поддерживает вывод «железодефицитная анемия»"})
    assert t["ferritin"].contribution == "поддерживает вывод «железодефицитная анемия»"
    assert t["hemoglobin"].contribution is None
    assert t["hemoglobin"].warning and "нетипичное значение" in t["hemoglobin"].warning
    assert t["ferritin"].warning is None


# ------------------------------------------------------------------ файл кейса (без файла — SKIP)
def test_reports_are_clean_on_case_file():
    """Запрещённые слова и «%» в тексте пациента — на всех строках файла кейса (сборка на одних правилах)."""
    import pandas as pd

    df = pd.read_csv(helpers.case_data_path())
    sex_map = {str(v).lower(): code for code, variants in CFG.class_mapping["sex_values"].items() for v in variants}
    count = 0
    for _, row in df.iterrows():
        values = {a: float(row[a]) for a in C.ALL_ANALYTES if a in df.columns and not pd.isna(row[a])}
        r = assemble({"sex": sex_map[str(row["sex"]).strip().lower()], "age_years": int(row["age_years"]), "values": values})
        check_patient(r["reports"].patient, r["level1"].anemia or r["hidden"].detected, f"row {count}")
        check_doctor(r["reports"].doctor, f"row {count}")
        count += 1
    assert count >= 600


# ------------------------------------------------------------------ движок целиком (без моделей)
def test_engine_smoke_without_models():
    try:
        from deficitlens_core.engine import Models, analyze
    except Exception as e:  # noqa: BLE001 — движок принадлежит координатору; здесь только дымовая проверка
        helpers.skip(f"движок не импортируется: {type(e).__name__}: {e}")
    for ex in EXAMPLES.values():
        result = analyze(AnalysisInput.model_validate(ex["input"]), models=Models(), cfg=CFG)
        check_doctor(result.reports.doctor, ex["id"])
        check_patient(result.reports.patient, result.level1.anemia or result.hidden_deficiency.detected, ex["id"])
        assert result.level1.anemia is ex["expect"]["anemia"]
        assert result.model_dump_json()
