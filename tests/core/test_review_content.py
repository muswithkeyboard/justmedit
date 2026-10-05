"""Ревизия содержания медицинской логики и текстов отчётов (05.10.2026): пп. 1–16 и дополнения UX-ревизии (а)–(е).

Модель уровня 2 здесь — заглушка с заданными вероятностями (FakeCase): проверяется сборка выводов и текстов,
а не качество модели. Тексты пациенту проверяются фильтром запрещённых слов.
"""
from __future__ import annotations

import json

import numpy as np

import helpers  # noqa: F401 — добавляет src и tests в sys.path
from helpers import load_examples

from deficitlens_core import constants as C
from deficitlens_core.config import load_config
from deficitlens_core.engine import Models, analyze
from deficitlens_core.next_test import MAX_INFORMATION, MAX_ITEMS, rank_next_tests
from deficitlens_core.normalize import normalize_input
from deficitlens_core.reports import NEUTRAL_PATIENT_LINE, filter_patient_line
from deficitlens_core.rules import mentzer_phrase, threshold_deficits
from deficitlens_core.schemas import AnalysisInput, HiddenDeficiency

CFG = load_config()
RULES = Models()                         # без моделей: уровень 1, правила, отчёты


class FakeCase:
    """Уровень 2 с заданными вероятностями профилей, вкладами и приростом информации."""
    version = "fake"
    metadata: dict = {}

    def __init__(self, probs: dict[str, float], contribs: list[dict] | None = None,
                 gains: dict[str, float] | None = None):
        rest = (1.0 - sum(probs.values())) / (len(C.PROFILES) - len(probs))
        self.P = np.array([probs.get(p, rest) for p in C.PROFILES])
        self.contribs = contribs or []
        self.gains = gains or {}

    def predict_profiles(self, X, mode="clinical"):
        return np.tile(self.P, (len(X), 1))

    def contributions(self, x_row, top, runner):
        return list(self.contribs)

    def hidden_threshold(self, level, mode="clinical"):
        return 0.99

    def information_gain(self, x_row):
        return dict(self.gains)


def run(values, sex="F", age=35, case=None, **extra):
    models = Models(case=case) if case is not None else RULES
    return analyze(AnalysisInput.model_validate({"sex": sex, "age_years": age, "values": values, **extra}),
                   models=models, cfg=CFG)


def lines(report) -> list[str]:
    return [report.headline] + [ln for s in report.sections for ln in s.lines]


def section(report, title: str) -> list[str]:
    return next((s.lines for s in report.sections if s.title == title), [])


def patient_clean(report) -> None:
    for line in lines(report):
        assert filter_patient_line(line, CFG.forbidden_patient_words) == line, line
        assert line != NEUTRAL_PATIENT_LINE, line
        assert "%" not in line, line


# Низкая уверенность: смешанный дефицит 0,42, «анемия без выявленного дефицита» 0,30, ЖДА 0,19 (как в находке п. 2).
LOW_MIXED = {"mixed:iron_B12": 0.42, "anemia_other": 0.30, "iron_deficiency_anemia": 0.19}


# ------------------------------------------------------------------ п. 1 и UX (г): индекс Ментцера
def test_mentzer_phrase_depends_on_value():
    below = mentzer_phrase(12.7, CFG)
    assert below == ("индекс Ментцера (MCV/RBC) 12,7: меньше 13 — вероятнее носительство β-талассемии; "
                     "подтверждение — электрофорез гемоглобина")
    assert "неопределённо" in mentzer_phrase(13.0, CFG) and "вероятнее" not in mentzer_phrase(13.0, CFG)
    for value in (16.4, 19.3, 26.9):
        above = mentzer_phrase(value, CFG)
        assert "не меньше 13: скорее дефицит железа, чем носительство β-талассемии" in above, above
        assert "меньше 13 — вероятнее" not in above and "повод для электрофореза" in above
        excluded = mentzer_phrase(value, CFG, iron_excluded=True)      # флаг «микроцитоз не от железа»
        assert "на носительство β-талассемии не указывает" in excluded and "дефицит железа" not in excluded
    # во всех местах вызова: флаг микроцитоза (железо не снижено) и строка «Почему такой вывод»
    flag = next(f for f in run({"hemoglobin": 101, "RBC": 4.39, "MCV": 72, "MCH": 23, "ferritin": 180}).flags
                if f.code == "microcytosis_not_iron")
    assert "16,4 — не меньше 13: на носительство β-талассемии не указывает" in flag.text
    why = section(run({"hemoglobin": 104, "MCV": 79, "RBC": 4.1, "ferritin": 62, "CRP": 28}, sex="M").reports.doctor,
                  "Почему такой вывод")
    assert any(ln.startswith("Индекс Ментцера (MCV/RBC) 19,3 — не меньше 13: скорее дефицит железа") for ln in why)


# ------------------------------------------------------------------ п. 2: дефицит по порогу при анемии
def test_threshold_deficit_reaches_doctor_and_patient():
    r = run({"hemoglobin": 118, "ferritin": 12, "CRP": 2}, case=FakeCase(LOW_MIXED))
    assert r.level1.anemia and r.level2.confidence == "low"
    head = r.reports.doctor.headline
    assert "По порогу: ферритин 12 мкг/л ниже 15 мкг/л (ВОЗ 2020) — дефицит железа." in head
    assert "причина не определяется" not in head and "анемия без выявленного дефицита" not in head
    assert ("Класс по модели уверенно не определяется; варианты: анемия смешанного дефицита, "
            "железодефицитная анемия.") in head
    main = section(r.reports.patient, "Главное")
    assert "Похоже на нехватку железа (запас железа снижен) — обсудите с врачом." in main
    assert not any("Назвать причину по этим данным трудно" in ln for ln in main)
    patient_clean(r.reports.patient)
    # «уровень 1 не меняется моделью» и класс модели не меняется
    assert r.level1.threshold_g_l == 120 and r.level2.anemia_class == "mixed_deficiency"


def test_threshold_deficit_b12_folate_and_caveats():
    b12 = run({"hemoglobin": 100, "MCV": 112, "vitamin_B12": 120, "folate": 10}, sex="M",
              case=FakeCase({"B12_deficiency_anemia": 0.5, "mixed:iron_B12": 0.45}))
    assert ("По порогу: витамин B12 120 пг/мл ниже 180 пг/мл (NICE NG239, зарубежный источник; в КР РФ числового "
            "порога нет) — дефицит витамина B12.") in b12.reports.doctor.headline
    assert "Похоже на нехватку витамина B12 (витамин B12 снижен) — обсудите с врачом." in section(
        b12.reports.patient, "Главное")
    both = run({"hemoglobin": 100, "ferritin": 9, "folate": 3})          # без модели, СРБ не сдан
    head = both.reports.doctor.headline
    assert "ферритин 9 мкг/л ниже 15 мкг/л (ВОЗ 2020) — дефицит железа; СРБ не сдан" in head
    assert "фолаты 3 нг/мл ниже 4 нг/мл (КР «Фолиеводефицитная анемия» 2024) — дефицит фолатов" in head
    main = " ".join(section(both.reports.patient, "Главное"))
    assert "Похоже на нехватку железа и фолатов (запас железа снижен; фолаты снижены) — обсудите с врачом." in main
    errors = " ".join(section(both.reports.patient, "Возможна ошибка — почему"))
    assert "не оценивала" not in errors                                  # нехватку назвал порог
    for r in (b12, both):
        patient_clean(r.reports.patient)
    # при СРБ выше порога — строка «на фоне воспаления» (а не «По порогу»); при беременности и без анемии — нет строки
    assert "По порогу" not in run({"hemoglobin": 100, "ferritin": 12, "CRP": 20}).reports.doctor.headline
    preg = run({"hemoglobin": 95, "ferritin": 5}, pregnancy={"status": "yes", "trimester": 2})
    assert "По порогу" not in preg.reports.doctor.headline and not threshold_deficits(
        normalize_input(AnalysisInput(sex="F", age_years=30, values={"hemoglobin": 95, "ferritin": 5},
                                      pregnancy={"status": "yes", "trimester": 2}), CFG), CFG)
    assert "По порогу" not in run({"hemoglobin": 130, "ferritin": 12}).reports.doctor.headline
    # модель уверенно называет ту же нехватку — пациенту её строка, без повтора «Похоже на нехватку»
    ida = run({"hemoglobin": 100, "ferritin": 6, "CRP": 1}, case=FakeCase({"iron_deficiency_anemia": 0.9}))
    patient_main = " ".join(section(ida.reports.patient, "Главное"))
    assert "нехватка железа" in patient_main and "Похоже на нехватку" not in patient_main


# ------------------------------------------------------------------ п. 3: Ret-He
def test_ret_he_wording_is_accurate():
    r = run({"hemoglobin": 104, "ferritin": 50, "CRP": 15}, sex="M")
    reasons = {t.analyte: t.reason for t in r.next_tests}
    assert "хватает ли железа кроветворению сейчас" in reasons["Ret_He"]
    assert "и при абсолютном, и при функциональном" in reasons["Ret_He"]
    assert "Мало зависит от воспаления" in reasons["sTfR"]
    assert not any("Не зависит от воспаления" in ln for ln in lines(r.reports.doctor))
    low = run({"hemoglobin": 104, "ferritin": 50, "CRP": 15, "Ret_He": 25}, sex="M")
    text = next(f for f in low.flags if f.code == "ferritin_masked_by_inflammation").text
    assert "абсолютный или функциональный дефицит" in text


# ------------------------------------------------------------------ п. 4: B6, медь, церулоплазмин
def test_microcytosis_lists_only_unchecked_case_set():
    base = {"hemoglobin": 100, "MCV": 72, "RBC": 4.4, "ferritin": 180}
    r = run({**base, "copper": 8, "ceruloplasmin": 0.1})
    text = next(f for f in r.flags if f.code == "microcytosis_not_iron").text
    assert "проверить B6;" in text and "проверить B6, медь" not in text
    assert "медь 8 мкмоль/л снижена (референс 11–22 мкмоль/л)" in text
    assert "церулоплазмин 0,1 г/л снижен (референс 0,16–0,45 г/л)" in text
    assert [t.analyte for t in r.next_tests if t.kind == "rule"] == ["vitamin_B6"]
    tactics = " ".join(section(r.reports.doctor, "Тактика"))
    assert "электрофорез гемоглобина (вне набора анализов прототипа); B6." in tactics and "медь" not in tactics
    full = run({**base, "vitamin_B6": 45, "copper": 15, "ceruloplasmin": 0.3})
    text = next(f for f in full.flags if f.code == "microcytosis_not_iron").text
    assert "проверить" not in text and "медь 15 мкмоль/л не снижена" in text
    assert "витамин B6 45 нмоль/л не снижен" in text


# ------------------------------------------------------------------ п. 5: «против» и веса
def test_against_only_for_plausible_runner_and_no_weights():
    assert CFG.class_mapping["explanation"]["against_min_runner_p"] == 0.10
    contribs = [{"analyte": "ferritin", "delta": 0.5}, {"analyte": "folate", "delta": -0.3}]
    values = {"hemoglobin": 105, "MCV": 88, "ferritin": 62, "folate": 12, "CRP": 2, "vitamin_B12": 420}
    implausible = run(values, case=FakeCase({"anemia_other": 0.99, "copper_deficiency": 0.002}, contribs))
    assert implausible.explanation.items_for and not implausible.explanation.items_against
    why = section(implausible.reports.doctor, "Почему такой вывод")
    assert not any(ln.startswith("Против:") for ln in why) and not any("(вес" in ln for ln in why)
    rows = {v.analyte: v for v in implausible.values}
    assert rows["folate"].contribution is None                         # «против» нет и в таблице значений
    plausible = run(values, case=FakeCase({"anemia_other": 0.6, "B12_deficiency_anemia": 0.35}, contribs))
    assert plausible.explanation.items_against
    why = section(plausible.reports.doctor, "Почему такой вывод")
    assert any(ln.startswith("Против: Фолаты 12 нг/мл") for ln in why)


# ------------------------------------------------------------------ п. 6 и UX (в): низкая уверенность, только ОАК
def test_low_confidence_names_most_likely_class_without_cause():
    r = run({"hemoglobin": 105, "MCV": 88, "ferritin": 60, "CRP": 2},
            case=FakeCase({"mixed:iron_folate": 0.45, "anemia_other": 0.35, "iron_deficiency_anemia": 0.15}))
    assert r.level2.confidence == "low" and r.level2.deficiency_cause == "iron_folate"
    assert r.level2.deficiency_cause_ru == "по этим данным не определяется"
    level2 = section(r.reports.doctor, "Уровень 2: группа и причина")
    assert level2[0] == "Наиболее вероятный класс: анемия смешанного дефицита."
    assert not any("Причина:" in ln for ln in level2)
    cbc = run({"hemoglobin": 68, "MCV": 78, "RBC": 2.9}, sex="M",
              case=FakeCase({"mixed:iron_folate": 0.6, "anemia_other": 0.35}))
    assert cbc.level2.completeness == "cbc" and cbc.level2.deficiency_cause == "iron_folate"
    assert cbc.level2.deficiency_cause_ru == "по одному общему анализу крови не определяется"
    assert "сочетание" not in cbc.reports.doctor.headline


# ------------------------------------------------------------------ пп. 7, 11: тексты истории
def test_history_texts_threshold_label_and_never():
    texts = CFG.texts["history"]
    label = texts["events"]["ferritin_drop"]["doctor"]["label_ru"]
    assert label == "порог практики РФ" and "КР" not in label
    assert "(порог практики РФ)" in texts["completeness"]["doctor"]["stale_ferritin_low"]
    assert texts["completeness"]["doctor"]["never"] == "Не сдавали."
    assert texts["events"]["cbc_stale"]["doctor"]["title"] == "ОАК давно не сдавали"
    assert "сдавался" not in json.dumps(texts, ensure_ascii=False)


# ------------------------------------------------------------------ п. 8: без дубля «Варианты»
def test_cbc_only_without_anemia_has_no_variants_line():
    cbc = load_examples()[0]["input"]["values"]                         # case1: только ОАК, анемии нет
    r = run(cbc, case=FakeCase({"latent_deficiency": 0.6, "no_anemia_no_deficiency": 0.3}))
    level2 = section(r.reports.doctor, "Уровень 2: группа и причина")
    assert level2[0] == "По одному общему анализу крови класс не определяется: данных мало."
    assert not any(ln.startswith("Варианты:") for ln in level2)


# ------------------------------------------------------------------ п. 9: «что досдать»
def _rank(values, mandatory, gains):
    norm = normalize_input(AnalysisInput(sex="F", age_years=40, values=values), CFG)
    return rank_next_tests(norm=norm, hidden=HiddenDeficiency(applicable=False), mandatory_tests=mandatory,
                           case_model=FakeCase({}, gains=gains), x_row=np.zeros(3), cfg=CFG, allow_information=True)


def test_information_tests_are_limited_and_tsat_not_with_iron_and_tibc():
    gains = {"TSAT": 0.45, "serum_iron": 0.4, "TIBC": 0.38, "vitamin_B12": 0.3, "homocysteine": 0.5, "folate": 0.2}
    out = _rank({"hemoglobin": 110, "ferritin": 12}, [], gains)
    info = [t for t in out if t.kind == "information"]
    assert len(info) <= MAX_INFORMATION == 3
    names = {t.analyte for t in info}
    assert not ({"serum_iron", "TIBC"} <= names and "TSAT" in names)
    for t in info:
        assert t.reason.startswith("Помогает различить оставшиеся варианты: прирост информации ")
        assert "бит (порядок в списке — с учётом цены)." in t.reason and "Лучше остальных" not in t.reason
    # железо и ОЖСС по правилу — НТЖ по приросту информации не предлагается
    rule_pair = [{"analyte": "serum_iron", "reason": "по правилу", "source": "КР"},
                 {"analyte": "TIBC", "reason": "по правилу", "source": "КР"}]
    assert "TSAT" not in {t.analyte for t in _rank({"hemoglobin": 110}, rule_pair, gains)}
    # весь список — не длиннее MAX_ITEMS: правила первыми в своём порядке, остальное врач видит в чек-листе
    many = [{"analyte": a, "reason": "по правилу", "source": "КР"}
            for a in ("ferritin", "CRP", "serum_iron", "TIBC", "folate", "vitamin_B12", "TSH")]
    out = _rank({"hemoglobin": 110}, many, gains)
    assert [t.analyte for t in out] == [m["analyte"] for m in many][:MAX_ITEMS]


# ------------------------------------------------------------------ пп. 10, 12, 16: двоеточия, тактика, гемолиз
def test_single_colon_wording_and_open_checklist_tactics():
    case5 = load_examples()[4]["input"]
    r = run(case5["values"], age=74, case=FakeCase({"anemia_other": 0.95}))
    assert "Уверенность понижена до средней: под подозрением — почки." in r.explanation.note
    assert "под подозрением: Почки" not in r.explanation.note
    tactics = " ".join(section(r.reports.doctor, "Тактика"))
    assert ("Дефицит по сданным анализам не найден — пройти незакрытые пункты чек-листа: почки — под подозрением; "
            "щитовидная железа, дефицит меди, гемолиз — не проверено.") in tactics
    assert "воспаление" not in tactics.split("Дообследование")[0]       # воспаление исключено — в тактике его нет
    crp = next(ln for ln in section(run({"hemoglobin": 100}).reports.patient, "Какие анализы обсудить")
               if ln.startswith("С-реактивный белок"))
    assert crp.count(":") == 1
    no_hct = {k: v for k, v in case5["values"].items() if k != "hematocrit"}
    hem = run({**no_hct, "TSH": 2, "haptoglobin": 0.1, "LDH": 400, "reticulocytes": 3.5}, age=74)
    item = next(i for i in hem.checklist if i.code == "hemolysis")
    assert item.detail.startswith("сильное подозрение; ЛДГ 400 Ед/л")
    assert "(без поправки на гематокрит: Hct не сдан)" in item.detail          # п. 16
    flag = next(f for f in hem.flags if f.code == "unexplained_checklist")
    assert "Под подозрением: почки — " in flag.text and "; гемолиз — сильное подозрение; ЛДГ" in flag.text
    assert "— Сильное" not in flag.text
    with_hct = run({**no_hct, "TSH": 2, "haptoglobin": 0.1, "LDH": 400, "reticulocytes": 3.5, "hematocrit": 31.7},
                   age=74)
    detail = next(i for i in with_hct.checklist if i.code == "hemolysis").detail
    assert "скорректированные по Hct 31,7 %" in detail and "без поправки" not in detail


def test_screening_off_line_has_one_colon():
    r = run({"hemoglobin": 135, "ferritin": 80}, case=FakeCase({"no_anemia_no_deficiency": 0.9}))
    line = next(ln for ln in section(r.reports.doctor, "Скрытый дефицит") if ln.startswith("Скрининг по ОАК не"))
    assert line.startswith("Скрининг по ОАК не применялся — ") and line.count(":") <= 1


# ------------------------------------------------------------------ п. 13 и пути файлов: источники без жаргона
def test_source_titles_have_no_jargon_or_repo_paths():
    for key, src in CFG.norms["sources"].items():
        title = src["title"]
        for bad in ("капитан", "требует решения врача команды", "docs/", ".md", ".yaml", "config/"):
            assert bad not in title, (key, title)
    assert CFG.source_title("project") == "решение команды проекта, ожидает подтверждения врачом"
    assert CFG.source_title("lab_reference").startswith("общепринятый диапазон (проект)")
    doctor = CFG.texts["doctor"]
    assert "капитан" not in json.dumps(doctor, ensure_ascii=False)
    for ex in load_examples():
        r = run(ex["input"]["values"], sex=ex["input"]["sex"], age=ex["input"]["age_years"],
                **({"pregnancy": ex["input"]["pregnancy"]} if "pregnancy" in ex["input"] else {}))
        text = " ".join(lines(r.reports.doctor))
        assert "капитан" not in text and "docs/" not in text and "врача команды;" not in text, ex["id"]


# ------------------------------------------------------------------ п. 14: аббревиатуры пациенту
def test_patient_abbreviations_are_explained():
    r = run({"hemoglobin": 68, "MCV": 78, "RBC": 2.9}, sex="M")
    tests = section(r.reports.patient, "Какие анализы обсудить")
    assert any(ln.startswith("ОЖСС (железосвязывающая способность):") for ln in tests)
    assert "ОЖСС (железосвязывающая способность)" in " ".join(section(r.reports.patient, "Возможна ошибка — почему"))
    assert "ОАК" not in json.dumps(CFG.texts["patient"], ensure_ascii=False)
    patient_clean(r.reports.patient)


# ------------------------------------------------------------------ п. 15: строка про модель — при названной причине
def test_model_training_line_only_when_cause_named():
    values = {"hemoglobin": 105, "MCV": 88, "ferritin": 60, "CRP": 2}
    note = "Предположение о причине сделано моделью"
    low = run(values, case=FakeCase({"mixed:iron_folate": 0.45, "anemia_other": 0.35}))
    assert not any(note in ln for ln in lines(low.reports.patient))
    named = run(values, case=FakeCase({"inflammation_anemia": 0.7}))
    assert named.level2.confidence == "moderate"
    assert any(note in ln for ln in lines(named.reports.patient))


# ------------------------------------------------------------------ UX (а): переключатель порога ферритина
def test_ferritin_norms_switch_changes_output():
    who = run({"hemoglobin": 130, "ferritin": 20}, age=34)
    ru = run({"hemoglobin": 130, "ferritin": 20}, age=34, norms="ru")
    assert not who.hidden_deficiency.rule_signals and not who.hidden_deficiency.detected
    assert [s.code for s in ru.hidden_deficiency.rule_signals] == ["ferritin_below_practice"]
    assert ru.hidden_deficiency.detected
    row_who = next(v for v in who.values if v.analyte == "ferritin")
    row_ru = next(v for v in ru.values if v.analyte == "ferritin")
    assert row_who.norm.status == "borderline" and "ниже 30 мкг/л" in row_who.norm.threshold_text
    assert row_ru.norm.status == "low"
    assert who.reports.doctor.headline != ru.reports.doctor.headline


# ------------------------------------------------------------------ UX (б): беременность и скрытый дефицит
def test_pregnancy_hidden_summary():
    r = run({"hemoglobin": 115}, age=29, pregnancy={"status": "yes", "trimester": 2})
    assert not r.level1.anemia
    assert r.hidden_deficiency.summary_ru == "При беременности скрытый дефицит не оценивается (только уровень 1)."


# ------------------------------------------------------------------ UX (д): «вклад в вывод» в таблице значений
def test_value_contributions_only_top3_or_out_of_norm():
    contribs = [{"analyte": a, "delta": d} for a, d in
                (("vitamin_B12", 0.9), ("folate", 0.8), ("CRP", 0.7), ("TSH", 0.6), ("ferritin", 0.2))]
    values = {"hemoglobin": 105, "MCV": 88, "ferritin": 5, "vitamin_B12": 420, "folate": 12, "CRP": 2, "TSH": 2}
    r = run(values, case=FakeCase({"anemia_other": 0.9}, contribs))
    rows = {v.analyte: v for v in r.values}
    for a in ("vitamin_B12", "folate", "CRP"):                         # топ-3 по весу
        assert rows[a].contribution == "в модели сдвигает к «анемия без выявленного дефицита»", a
    assert rows["TSH"].contribution is None                            # четвёртый по весу, значение в норме
    assert rows["ferritin"].contribution is not None                   # вне нормы — показываем


# ------------------------------------------------------------------ UX (е): диапазон с одной границей, термины
def test_one_sided_reference_wording():
    esr = next(v for v in run({"hemoglobin": 130, "ESR": 40}).values if v.analyte == "ESR")
    assert esr.norm.threshold_text.startswith("выше референса (верхняя граница 30 мм/ч)")
    assert "референса не выше" not in esr.norm.threshold_text
    within = next(v for v in run({"hemoglobin": 130, "ESR": 10}).values if v.analyte == "ESR")
    assert within.norm.threshold_text.startswith("в пределах референса (не выше 30 мм/ч)")
    lab = run({"hemoglobin": 130, "LDH": 300}, reference_ranges={"LDH": {"low": 100, "high": 250}})
    assert lab.references.lab_name == "request"
    row = next(v for v in lab.values if v.analyte == "LDH")
    assert "референсы с бланка / из настроек" in row.norm.threshold_text


# ------------------------------------------------------------------ восемь примеров: тексты пациента чистые
def test_eight_examples_patient_texts_pass_filter():
    for ex in load_examples():
        inp = ex["input"]
        r = run(inp["values"], sex=inp["sex"], age=inp["age_years"],
                **({"pregnancy": inp["pregnancy"]} if "pregnancy" in inp else {}))
        patient_clean(r.reports.patient)
