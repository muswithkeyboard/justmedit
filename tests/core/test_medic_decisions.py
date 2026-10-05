"""Этап 5а: рабочие решения капитана по вопросам врачу (docs/review/medic_decisions.md).

Пункты брифа 5а:
  1 — анемия воспаления: класс врачу, группа кейса с пояснением, строка «возможна ЖДА на фоне воспаления»;
  2 — сигнал tsat_low_normal_ferritin (низкое НТЖ при несниженном ферритине);
  3 — блок «срочно»: подписи порогов, пациенту «обратитесь к врачу сегодня» без скорой;
  4 — чек-лист «Дефицит по сданным анализам не найден», гемолиз и церулоплазмин без чисел-референсов;
  5 — Ментцер, ферритин у беременных, B12 без чисел, точка 20/30/40 %, нет iron_stage, тактика одной строкой,
      золотой случай с рСКФ ≈ 29;
  6 — нет слова «синтетический» в коде, конфигурации и метаданных моделей.
Тесты, которым нужны модели из models/, без моделей дают SKIP.
"""
from __future__ import annotations

import copy
import dataclasses
import json
import re

import helpers
from helpers import ROOT, load_examples

from deficitlens_core.config import load_config
from deficitlens_core.level1 import assess_level1
from deficitlens_core.normalize import normalize_input
from deficitlens_core.reports import TREATMENT_LINE, build_reports
from deficitlens_core.rules import evaluate_rules, iron_deficiency_on_inflammation
from deficitlens_core.schemas import AnalysisInput, Explanation, HiddenDeficiency, Level2
from deficitlens_core.values_table import build_values_table

CFG = load_config()
EXAMPLES = {e["id"]: e for e in load_examples()}
# Числа B12 выключены (вариант до решения п. 10): нет ни use_foreign, ни поштучно разрешённых зарубежных записей.
LOCAL = dataclasses.replace(CFG, norms={**copy.deepcopy(CFG.norms), "policy": {"use_foreign": False}})
ANEMIA_PANEL = {"hemoglobin": 105, "MCV": 85, "RBC": 4.0, "vitamin_B12": 400, "folate": 10}


def run(values, sex="F", age=40, pregnancy=None, cfg=LOCAL):
    payload = {"sex": sex, "age_years": age, "values": values}
    if pregnancy:
        payload["pregnancy"] = pregnancy
    norm = normalize_input(AnalysisInput.model_validate(payload), cfg)
    level1 = assess_level1(norm, cfg)
    return norm, level1, evaluate_rules(norm, level1, cfg)


def models_or_skip():
    from deficitlens_core.engine import load_models
    models = load_models()
    if models.case is None or models.screen is None:
        helpers.skip("модели из models/ не загружены")
    return models


def analyze(payload, models=None):
    from deficitlens_core.engine import analyze as _analyze
    return _analyze(AnalysisInput.model_validate(payload), models=models or models_or_skip(), cfg=CFG)


# ------------------------------------------------------------------ п. 2: низкое НТЖ при несниженном ферритине
def test_tsat_low_normal_ferritin_signal():
    tsat_thr = CFG.norms["tsat"]["low_below"]
    fer_ok = CFG.norms["ferritin"]["deficiency_below"]["ru"] + 30          # не ниже порогов, СРБ не сдан
    _, _, out = run({"hemoglobin": 135, "ferritin": fer_ok, "TSAT": tsat_thr - 0.1})
    assert [s.code for s in out.rule_signals] == ["tsat_low_normal_ferritin"]
    text = out.rule_signals[0].text
    assert "возможен функциональный дефицит железа" in text
    assert "уточнить СРБ, Ret-He или sTfR" in text
    assert "показатель колеблется в течение дня" in text
    assert [t["analyte"] for t in out.mandatory_tests][:3] == ["CRP", "Ret_He", "sTfR"]
    # сданные уточняющие анализы из подсказки уходят
    _, _, out = run({"hemoglobin": 135, "ferritin": fer_ok, "TSAT": tsat_thr - 0.1, "CRP": 1, "Ret_He": 33})
    assert "уточнить sTfR" in out.rule_signals[0].text and "СРБ" not in out.rule_signals[0].text.split("уточнить")[1]
    # НТЖ на пороге — не снижено; ферритин не сдан или снижен — прежний сигнал tsat_low
    assert run({"hemoglobin": 135, "ferritin": fer_ok, "TSAT": tsat_thr})[2].rule_signals == []
    assert {s.code for s in run({"hemoglobin": 135, "TSAT": tsat_thr - 0.1})[2].rule_signals} == {"tsat_low"}
    low_fer = {s.code for s in run({"hemoglobin": 135, "ferritin": 10, "TSAT": tsat_thr - 0.1})[2].rule_signals}
    assert low_fer == {"ferritin_below_who", "tsat_low"}
    # при беременности сигналы не строятся
    assert run({"hemoglobin": 135, "ferritin": fer_ok, "TSAT": 10}, pregnancy={"status": "yes"})[2].rule_signals == []


def test_tsat_low_normal_ferritin_checklist_iron_suspected_without_unexplained_flag():
    _, _, out = run({**ANEMIA_PANEL, "ferritin": 60, "TSAT": 12})
    assert out.flags == []                                                      # флага «не найден» нет
    status = {i.code: i.status for i in out.checklist}
    assert status["iron"] == "suspected"
    iron = next(i for i in out.checklist if i.code == "iron")
    assert "возможен функциональный дефицит железа" in iron.detail and iron.analyte == "TSAT"
    assert not any("не найден" in t["reason"] for t in out.mandatory_tests)
    # без сигнала — прежнее поведение: железо «исключено» и флаг чек-листа
    _, _, plain = run({**ANEMIA_PANEL, "ferritin": 120, "TSAT": 25, "CRP": 1})
    assert {i.code: i.status for i in plain.checklist}["iron"] == "excluded"
    assert [f.code for f in plain.flags] == ["unexplained_checklist"]


def test_tsat_low_normal_ferritin_doctor_headline_and_patient_text():
    norm, level1, out = run({**ANEMIA_PANEL, "ferritin": 60, "TSAT": 12})
    reports = build_reports(norm=norm, level1=level1, level2=Level2(enabled=False, disabled_reason="нет модели"),
                            hidden=HiddenDeficiency(applicable=False), flags=out.flags, checklist=out.checklist,
                            next_tests=[], explanation=Explanation(), values=build_values_table(norm, LOCAL), cfg=LOCAL)
    assert "Под подозрением: дефицит железа (НТЖ 12 %)" in reports.doctor.headline
    assert "чек-лист исключений" not in reports.doctor.headline.lower()
    patient = " ".join(line for s in reports.patient.sections for line in s.lines)
    assert "насыщение трансферрина" in patient and "причина по сданным анализам не видна" not in patient


# ------------------------------------------------------------------ п. 1: анемия воспаления
def test_iron_deficiency_on_inflammation_rule():
    crp = CFG.norms["crp"]["inflammation_above"] + 1
    fer70 = CFG.norms["ferritin"]["inflammation_below"]

    def check(values, pregnancy=None):
        norm, _, _ = run(values, pregnancy=pregnancy)
        return iron_deficiency_on_inflammation(norm, LOCAL)

    assert check({"hemoglobin": 100, "CRP": crp, "ferritin": fer70 - 1})
    assert check({"hemoglobin": 100, "CRP": crp, "ferritin": 250, "TSAT": CFG.norms["tsat"]["low_below"] - 1})
    assert check({"hemoglobin": 100, "CRP": crp, "ferritin": 250, "Ret_He": CFG.norms["ret_he"]["low_below"] - 1})
    assert not check({"hemoglobin": 100, "CRP": crp, "ferritin": fer70})                   # ферритин не ниже 70
    assert not check({"hemoglobin": 100, "CRP": CFG.norms["crp"]["inflammation_above"], "ferritin": 20})  # СРБ = порог
    assert not check({"hemoglobin": 100, "ferritin": 20})                                   # СРБ не сдан
    assert not check({"hemoglobin": 100, "CRP": crp, "ferritin": 20}, pregnancy={"status": "yes"})


def test_inflammation_anemia_shown_as_class_with_group_note():
    """Класс «анемия воспаления» врачу; группа из пяти — unexplained_anemia с пояснением; без «причина: воспаление»."""
    from deficitlens_core.engine import _class_notes

    cm = CFG.class_mapping
    assert len(cm["groups5"]) == 5 and "latent_deficiency" not in cm["groups5"]          # скрытый дефицит — не группа
    assert cm["class_to_group5_if_anemia"]["inflammation_anemia"] == "unexplained_anemia"
    payload = {**ANEMIA_PANEL, "ferritin": 250, "CRP": 40}
    norm, level1, out = run(payload)
    level2 = Level2(enabled=True, mode="clinical", completeness="standard", completeness_ru="стандартная",
                    case_group="unexplained_anemia", case_group_ru=cm["groups5"]["unexplained_anemia"],
                    anemia_class="inflammation_anemia", anemia_class_ru=cm["classes12"]["inflammation_anemia"],
                    deficiency_cause="inflammation", deficiency_cause_ru=cm["causes11"]["inflammation"],
                    confidence="moderate", confidence_ru="средняя")
    _class_notes(level2, level1, norm, LOCAL)
    assert level2.case_group_note_ru and "анемия неясного генеза" in level2.case_group_note_ru
    assert level2.class_note_ru is None                                     # признаков дефицита железа нет
    reports = build_reports(norm=norm, level1=level1, level2=level2, hidden=HiddenDeficiency(applicable=False),
                            flags=out.flags, checklist=out.checklist, next_tests=[], explanation=Explanation(),
                            values=build_values_table(norm, LOCAL), cfg=LOCAL)
    head = reports.doctor.headline
    assert "Вероятнее всего: анемия воспаления" in head and "неясного генеза" not in head
    level2_lines = next(s.lines for s in reports.doctor.sections if s.title.startswith("Уровень 2"))
    assert level2_lines[0].startswith("Класс: анемия воспаления") and "Причина: воспаление" not in level2_lines[0]
    assert any("относится к группе «анемия неясного генеза»" in line for line in level2_lines)


def test_iron_deficiency_on_inflammation_line_in_engine():
    models = models_or_skip()
    r = analyze({"sex": "F", "age_years": 40, "values": {**ANEMIA_PANEL, "ferritin": 50, "CRP": 40, "TSAT": 12}},
                models)
    assert r.level2.class_note_ru == "Возможна железодефицитная анемия на фоне воспаления."
    assert "Возможна железодефицитная анемия на фоне воспаления." in r.reports.doctor.headline
    calm = analyze({"sex": "F", "age_years": 40, "values": {**ANEMIA_PANEL, "ferritin": 250, "CRP": 1}}, models)
    assert calm.level2.class_note_ru is None
    no_anemia = analyze({"sex": "F", "age_years": 40, "values": {"hemoglobin": 135, "ferritin": 50, "CRP": 40}}, models)
    assert no_anemia.level2.case_group == "healthy" and no_anemia.level2.class_note_ru is None
    patient = " ".join(line for s in r.reports.patient.sections for line in s.lines).lower()
    assert "железодефицитная анемия" not in patient                      # пациенту — без диагнозов


# ------------------------------------------------------------------ п. 3: «срочно»
def test_urgent_labels():
    _, level1, _ = run({"hemoglobin": 68, "WBC": 1.5, "platelets": 30}, sex="M")
    hb, wbc, plt = level1.urgent
    assert "(тяжёлая анемия по ВОЗ)" in hb.text and "тяжёлая анемия по ВОЗ — ВОЗ, 2024" in hb.source
    assert "(выбор проекта)" in wbc.text and "(выбор проекта)" in plt.text
    _, preg, _ = run({"hemoglobin": 69}, pregnancy={"status": "yes", "trimester": 2})
    assert "ниже 70 г/л (тяжёлая анемия по ВОЗ)" in preg.urgent[0].text
    assert CFG.norms["urgent"]["hemoglobin_below"] == {"default": 80, "pregnant": 70}      # пороги прежние
    assert (CFG.norms["urgent"]["wbc_below"], CFG.norms["urgent"]["platelets_below"]) == (2.0, 50)


def test_patient_urgent_without_ambulance():
    """Пациенту — «обратитесь к врачу сегодня»; нигде нет скорой, 103 и 112."""
    banned = re.compile(r"скор(ая|ую|ой)|\b103\b|\b112\b", re.IGNORECASE)
    files = [ROOT / "config" / "texts_ru" / "patient.yaml"]
    files += sorted((ROOT / "src" / "deficitlens_api" / "templates").glob("*.html"))
    files += sorted((ROOT / "src" / "deficitlens_api" / "static").glob("*.js"))
    for path in files:
        for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            assert not banned.search(line), (path.name, i, line)
    norm, level1, out = run({"hemoglobin": 68, "WBC": 1.5}, sex="M")
    reports = build_reports(norm=norm, level1=level1, level2=Level2(enabled=False, disabled_reason="нет модели"),
                            hidden=HiddenDeficiency(applicable=False), flags=out.flags, checklist=out.checklist,
                            next_tests=[], explanation=Explanation(), values=build_values_table(norm, LOCAL), cfg=LOCAL)
    patient = reports.patient
    assert patient.headline.startswith("Важно: обратитесь к врачу сегодня.")
    urgent = patient.sections[0]
    assert urgent.title == "Когда обращаться срочно"
    assert all("сегодня" in line for line in urgent.lines)
    assert not any(banned.search(line) for s in patient.sections for line in s.lines)


# ------------------------------------------------------------------ п. 4: чек-лист исключений
def test_checklist_title_and_reference_items():
    """П. 8 (название флага) и п. 10 (отменяет п. 5 в части чисел): гемолиз и медь — по референсам проекта."""
    from deficitlens_core.rules import FLAG_TITLES
    assert FLAG_TITLES["unexplained_checklist"] == "Дефицит по сданным анализам не найден — чек-лист исключений"
    base = {**ANEMIA_PANEL, "ferritin": 120, "CRP": 2}
    _, _, out = run(base)
    items = {i.code: i for i in out.checklist}
    hem = items["hemolysis"]
    assert hem.status == "not_checked"
    assert hem.detail == "ЛДГ — не сдан; непрямой билирубин — не сдан; гаптоглобин — не сдан; ретикулоциты — не сданы"
    assert items["copper"].detail.endswith("церулоплазмин — не сдан")
    _, _, out = run({**base, "LDH": 300, "reticulocytes": 1.2, "ceruloplasmin": 0.15})
    items = {i.code: i for i in out.checklist}
    assert "ЛДГ 300 Ед/л — выше референса 135–214 Ед/л" in items["hemolysis"].detail   # ответ врача, п. 11
    # без Hct поправку на гематокрит посчитать нельзя — врачу об этом сказано (ревизия содержания 05.10.2026, п. 16)
    assert ("ретикулоциты 1,2 % (без поправки на гематокрит: Hct не сдан) — в пределах референса 0,68–1,86 %"
            in items["hemolysis"].detail)
    assert items["hemolysis"].status == "not_checked"                       # без гаптоглобина гемолиз не оценивается
    assert "церулоплазмин 0,15 г/л — ниже референса 0,16–0,45 г/л" in items["copper"].detail
    assert "оценивается только вместе с медью" in items["copper"].detail        # медь не сдана (ответ врача, п. 11)
    # у пяти показателей теперь есть референс проекта по умолчанию; диагностического порога у церулоплазмина нет
    for analyte in ("LDH", "indirect_bilirubin", "haptoglobin", "reticulocytes", "ceruloplasmin"):
        assert analyte not in CFG.norms["no_reference_yet"], analyte
        assert analyte in CFG.norms["reference_ranges"]["ranges"], analyte
    cer = CFG.norms["ceruloplasmin"]
    assert not any(k in cer for k in ("low_below", "high_above", "deficiency_below"))
    norm, _, _ = run({"hemoglobin": 130, "ceruloplasmin": 0.1})
    row = next(r for r in build_values_table(norm, LOCAL) if r.analyte == "ceruloplasmin")
    assert row.norm.status == "low" and "референс проекта по умолчанию" in row.norm.threshold_text


# ------------------------------------------------------------------ п. 5: проверки
def test_mentzer_only_microcytosis_doctor_only_phrase():
    from deficitlens_core.rules import mentzer_index, mentzer_phrase
    norm, _, _ = run({"hemoglobin": 101, "MCV": 72, "RBC": 4.39})
    phrase = mentzer_phrase(mentzer_index(norm, LOCAL), LOCAL)
    # ≥ 13 (ревизия 05.10.2026, п. 1 и UX (г)): не «меньше 13 — вероятнее носительство»
    assert phrase == ("индекс Ментцера (MCV/RBC) 16,4 — не меньше 13: скорее дефицит железа, чем носительство "
                      "β-талассемии (меньше 13 — повод для электрофореза гемоглобина)")
    thal, _, _ = run({"hemoglobin": 110, "MCV": 70, "RBC": 5.5})
    assert mentzer_phrase(mentzer_index(thal, LOCAL), LOCAL) == (
        "индекс Ментцера (MCV/RBC) 12,7: меньше 13 — вероятнее носительство β-талассемии; "
        "подтверждение — электрофорез гемоглобина")
    assert mentzer_index(run({"hemoglobin": 101, "MCV": 80, "RBC": 3.0})[0], LOCAL) is None     # нет микроцитоза
    patient_texts = (ROOT / "config" / "texts_ru" / "patient.yaml").read_text(encoding="utf-8").lower()
    assert "ментцер" not in patient_texts


def test_ferritin_not_assessed_in_pregnancy():
    norm, level1, out = run({"hemoglobin": 100, "ferritin": 8}, pregnancy={"status": "yes", "trimester": 2})
    row = next(r for r in build_values_table(norm, LOCAL) if r.analyte == "ferritin")
    assert row.norm.status == "unknown" and "беременности" in row.norm.threshold_text
    assert out.rule_signals == [] and out.flags == [] and out.checklist == []
    norm, _, _ = run({"hemoglobin": 100, "ferritin": 8})
    assert next(r for r in build_values_table(norm, LOCAL) if r.analyte == "ferritin").norm.status == "low"


def test_b12_without_numbers_and_flag_by_model_band():
    import numpy as np
    from deficitlens_core import constants as C
    from deficitlens_core.engine import _b12_flag

    # Решение п. 10 (пересмотр правки 1): use_foreign не включается глобально; три записи NICE разрешены поштучно.
    assert CFG.norms["policy"]["use_foreign"] is False
    assert set(CFG.norms["policy"]["foreign_allowed"]) == {"vitamin_b12", "active_b12", "homocysteine"}
    for key in ("vitamin_b12", "active_b12", "homocysteine"):
        assert CFG.norms[key]["origin"] == "foreign"
    assert CFG.class_mapping["model_flags"]["b12_uncertain"] == [0.15, 0.85]
    # ниже — вариант «числа B12 выключены» (LOCAL): тогда работает только флаг по неуверенности модели
    norm, _, _ = run({"hemoglobin": 118, "vitamin_B12": 230})
    row = next(r for r in build_values_table(norm, LOCAL) if r.analyte == "vitamin_B12")
    assert row.norm.status == "unknown"
    b12 = [i for i, p in enumerate(C.PROFILES) if "b12" in C.PROFILE_NUTRIENTS[p]]
    other = [i for i in range(len(C.PROFILES)) if i not in b12]

    def p14(p_b12: float):
        p = np.zeros(len(C.PROFILES))
        p[b12[0]], p[other[0]] = p_b12, 1 - p_b12
        return p

    assert _b12_flag(norm, p14(0.5), [], LOCAL)[0] is not None
    assert _b12_flag(norm, p14(0.15), [], LOCAL)[0] is not None                   # граница полосы входит
    assert _b12_flag(norm, p14(0.1), [], LOCAL)[0] is None
    assert _b12_flag(norm, p14(0.9), [], LOCAL)[0] is None
    functional, _, _ = run({"hemoglobin": 118, "vitamin_B12": 230, "homocysteine": 12})
    assert _b12_flag(functional, p14(0.5), [], LOCAL)[0] is None                  # функциональный маркер сдан
    # рабочая конфигурация: число B12 есть (NICE NG239) — модельный флаг не нужен, флаг ставят правила
    assert _b12_flag(norm, p14(0.5), [], CFG)[0] is None


def test_refer_share_switch_20_30_40():
    """Точка направления на ферритин: 30 % по умолчанию; 20 / 30 / 40 % меняют отсечку и рекомендацию."""
    import pydantic

    assert CFG.screen["refer_share"] == 0.3
    models = models_or_skip()
    cuts = [models.screen.threshold("full_cbc", CFG.screen["threshold_group"], s) for s in (0.2, 0.3, 0.4)]
    assert cuts[0] > cuts[1] > cuts[2]
    cbc = {"RBC": 4.3, "MCV": 88, "MCH": 29.5, "RDW": 13.0, "hematocrit": 38, "MCHC": 330, "platelets": 250, "WBC": 6,
           "hemoglobin": 128}
    res = {}
    for share in (None, 0.2, 0.3, 0.4):
        payload = {"sex": "F", "age_years": 30, "values": cbc}
        if share is not None:
            payload["refer_share"] = share
        res[share] = analyze(payload, models).hidden_deficiency.screening
    assert res[None].refer_share == 0.3 and res[0.4].refer_share == 0.4 and res[0.2].refer_share == 0.2
    p = res[0.3].p_ferritin_lt15
    assert cuts[2] <= p < cuts[1]                                     # пример лежит между отсечками 40 % и 30 %
    assert res[0.4].recommend_ferritin is True
    assert res[0.3].recommend_ferritin is False and res[0.2].recommend_ferritin is False
    try:
        AnalysisInput.model_validate({"sex": "F", "age_years": 30, "values": cbc, "refer_share": 0.25})
        raise AssertionError("refer_share 0.25 должен отклоняться")
    except pydantic.ValidationError:
        pass


def test_refer_share_in_api_reference_and_page():
    from deficitlens_api import service
    ref = service.reference()
    scr = ref["screening"]                                # + referral: что даёт каждый вариант (tests/api/test_ui_result.py)
    assert {k: scr[k] for k in ("refer_share", "refer_share_options")} == {"refer_share": 0.3, "refer_share_options": [0.2, 0.3, 0.4]}
    html = (ROOT / "src" / "deficitlens_api" / "templates" / "index.html").read_text(encoding="utf-8")
    for value in ("0.2", "0.3", "0.4"):
        assert f'<option value="{value}"' in html
    js = (ROOT / "src" / "deficitlens_api" / "static" / "app.js").read_text(encoding="utf-8")
    assert 'refer_share: parseFloat($("refer-share").value)' in js


def test_no_iron_stage_and_single_treatment_line():
    for path in list((ROOT / "src").rglob("*.py")) + list((ROOT / "config").rglob("*.yaml")):
        assert "iron_stage" not in path.read_text(encoding="utf-8"), path
    assert "по клиническим рекомендациям Минздрава" in TREATMENT_LINE and "дозы прототип не приводит" in TREATMENT_LINE
    assert CFG.texts["doctor"]["treatment_line"] == TREATMENT_LINE


def test_golden_egfr_29_headline_suspected_kidney():
    ex = EXAMPLES["case5_unexplained_kidney"]
    r = analyze(ex["input"])
    assert abs(next(v.value for v in r.values if v.analyte == "eGFR") - 29) < 0.6
    assert "Под подозрением: почки (рСКФ 29" in r.reports.doctor.headline


# ------------------------------------------------------------------ п. 6: «учебный набор кейса»
def test_no_word_synthetic_in_code_config_and_model_metadata():
    paths = [p for p in (ROOT / "src").rglob("*") if p.suffix in (".py", ".html", ".js", ".css")]
    paths += [p for p in (ROOT / "config").rglob("*") if p.suffix in (".yaml", ".yml")]
    paths += sorted((ROOT / "models").glob("*/metadata.json"))
    assert len(paths) > 20
    for path in paths:
        assert "синтетич" not in path.read_text(encoding="utf-8").lower(), path
    meta = json.loads((ROOT / "models" / "case-v1" / "metadata.json").read_text(encoding="utf-8"))
    assert "учебный набор кейса (840 строк)" in meta["data"]["note"]
    assert "учебном наборе кейса (840 строк)" in meta["summary"]["limitations"]


def test_case_model_loads_after_metadata_text_edit():
    """Правка текстовых полей metadata.json не ломает загрузку модели и сверку sha256 файлов модели."""
    from deficitlens_core.ml.case_model import CaseModel, sha256_file
    try:
        model = CaseModel.load()
    except FileNotFoundError:
        helpers.skip("нет файлов модели кейса")
    meta = model.metadata
    for name, info in meta["files"].items():
        assert sha256_file(ROOT / "models" / "case-v1" / name) == info["sha256"], name


def test_cbc_only_no_anemia_hides_class_variants():
    """Правка 4 (ревизия волн 4–5, HIGH-2): по одному ОАК без анемии нет вариантов и вероятностей классов."""
    from deficitlens_core.engine import analyze
    from deficitlens_core.schemas import AnalysisInput

    r = analyze(AnalysisInput.model_validate(
        {"sex": "M", "age_years": 40, "values": {"hemoglobin": 150, "RBC": 5.0, "hematocrit": 44, "MCV": 88,
                                                 "MCH": 30, "MCHC": 340, "RDW": 16.5, "platelets": 250, "WBC": 6.5}}))
    d = r.level2.model_dump()
    assert d["plausible"] == [] and "classes12" not in d["probabilities"] and "causes11" not in d["probabilities"]
