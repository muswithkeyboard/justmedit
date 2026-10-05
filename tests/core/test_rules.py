"""Правила-подсказки: каждый сигнал и флаг — положительный и отрицательный случай; чек-лист; обязательные анализы."""
from __future__ import annotations

import copy
import dataclasses

import helpers  # noqa: F401 — добавляет src и tests в sys.path

from deficitlens_core.config import load_config
from deficitlens_core.level1 import assess_level1
from deficitlens_core.normalize import normalize_input
from deficitlens_core.rules import RulesOutcome, evaluate_rules
from deficitlens_core.schemas import AnalysisInput

CFG = load_config()
CBC_OK = {"RBC": 4.5, "MCV": 88, "MCH": 29.5, "RDW": 13.0}       # ОАК без отклонений по индексам


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


def run(values, sex="F", age=40, pregnancy=None, cfg=LOCAL, norms=None) -> RulesOutcome:
    payload = {"sex": sex, "age_years": age, "values": values}
    if norms:
        payload["norms"] = norms
    if pregnancy:
        payload["pregnancy"] = pregnancy
    norm = normalize_input(AnalysisInput.model_validate(payload), cfg)
    return evaluate_rules(norm, assess_level1(norm, cfg), cfg)


def signals(values, **kw) -> set[str]:
    return {s.code for s in run(values, **kw).rule_signals}


def flags(values, **kw) -> set[str]:
    return {f.code for f in run(values, **kw).flags}


def tests_of(outcome: RulesOutcome) -> list[str]:
    return [t["analyte"] for t in outcome.mandatory_tests]


# ------------------------------------------------------------------ сигналы скрытого дефицита
def test_signal_ferritin_below_who():
    assert "ferritin_below_who" in signals({"hemoglobin": 135, "ferritin": 14.9})
    assert "ferritin_below_who" not in signals({"hemoglobin": 135, "ferritin": 15})


def test_signal_ferritin_below_practice():
    # Порог практики РФ (30 мкг/л) — только при наборе порогов «ru» (ревизия UX 05.10.2026: переключатель
    # «ВОЗ 15 / практика РФ 30» раньше ни на что не влиял).
    assert signals({"hemoglobin": 135, "ferritin": 15}, norms="ru") == {"ferritin_below_practice"}
    assert signals({"hemoglobin": 135, "ferritin": 29.9, "CRP": 3}, norms="ru") == {"ferritin_below_practice"}
    assert signals({"hemoglobin": 135, "ferritin": 30}, norms="ru") == set()
    # набор «who» (по умолчанию): ферритин 15–30 — не дефицит по ВОЗ 2020, сигнала нет
    assert signals({"hemoglobin": 135, "ferritin": 20}) == set()
    assert signals({"hemoglobin": 130, "ferritin": 20}, sex="F", age=34, norms="who") == set()
    assert signals({"hemoglobin": 130, "ferritin": 14.9}, norms="who") == {"ferritin_below_who"}
    # при воспалении порог практики не применяется — работает сигнал маски
    assert "ferritin_below_practice" not in signals({"hemoglobin": 135, "ferritin": 20, "CRP": 12}, norms="ru")
    text = run({"hemoglobin": 135, "ferritin": 20}, norms="ru").rule_signals[0].text
    assert "порог практики РФ" in text and "20 мкг/л" in text


def test_ferritin_between_who_and_practice_in_anemia_checklist():
    """Набор «who», анемия, ферритин 20: сигнала нет, но в чек-листе железо не «исключено» — вывод зависит от
    выбранного порога; уточнить — железо и ОЖСС (НТЖ). При «ru» — сигнал, чек-листа нет."""
    panel = {"hemoglobin": 105, "MCV": 88, "ferritin": 20, "CRP": 2, "vitamin_B12": 420, "folate": 12}
    who = run(panel)
    iron = next(i for i in who.checklist if i.code == "iron")
    assert iron.status == "not_checked" and "вывод зависит от выбранного порога" in iron.detail
    assert "не ниже 15 мкг/л (ВОЗ 2020), но ниже 30 мкг/л (порог практики РФ)" in iron.detail
    assert {"serum_iron", "TIBC"} <= set(tests_of(who))
    ru = run(panel, norms="ru")
    assert {s.code for s in ru.rule_signals} == {"ferritin_below_practice"} and not ru.checklist


def test_signal_ferritin_masked_by_inflammation():
    assert signals({"hemoglobin": 135, "ferritin": 69.9, "CRP": 5.1}) == {"ferritin_masked_by_inflammation"}
    assert signals({"hemoglobin": 135, "ferritin": 15, "CRP": 28}) == {"ferritin_masked_by_inflammation"}
    assert signals({"hemoglobin": 135, "ferritin": 69.9, "CRP": 5.0}) == set()          # СРБ строго выше 5
    assert signals({"hemoglobin": 135, "ferritin": 70, "CRP": 28}) == set()
    assert signals({"hemoglobin": 135, "ferritin": 10, "CRP": 28}) == {"ferritin_below_who"}


def test_signal_tsat_low_measured_and_derived():
    assert "tsat_low" in signals({"hemoglobin": 135, "TSAT": 17.7})
    assert "tsat_low" not in signals({"hemoglobin": 135, "TSAT": 17.8})
    out = run({"hemoglobin": 135, "serum_iron": 5.5, "TIBC": 50})                        # расчётное НТЖ 11 %
    assert "tsat_low" in {s.code for s in out.rule_signals}
    assert out.derived["tsat_calc"] == 11.0


def test_signal_ret_he_low():
    assert "ret_he_low" in signals({"hemoglobin": 135, "Ret_He": 30.5})
    assert "ret_he_low" not in signals({"hemoglobin": 135, "Ret_He": 30.6})


def test_signal_b12_low_and_gray_zone_with_foreign_thresholds():
    assert signals({"hemoglobin": 135, "vitamin_B12": 179}, cfg=FOREIGN) == {"b12_low"}
    assert signals({"hemoglobin": 135, "vitamin_B12": 180}, cfg=FOREIGN) == {"b12_gray_zone"}
    assert signals({"hemoglobin": 135, "vitamin_B12": 349.9}, cfg=FOREIGN) == {"b12_gray_zone"}
    assert signals({"hemoglobin": 135, "vitamin_B12": 350}, cfg=FOREIGN) == {"b12_gray_zone"}   # обе границы в зоне
    assert signals({"hemoglobin": 135, "vitamin_B12": 350.1}, cfg=FOREIGN) == set()
    norms = copy.deepcopy(FOREIGN.norms)
    norms["vitamin_b12"]["gray_zone_inclusive"] = False                     # без признака — верхняя граница не входит
    exclusive = dataclasses.replace(FOREIGN, norms=norms)
    assert signals({"hemoglobin": 135, "vitamin_B12": 350}, cfg=exclusive) == set()
    assert signals({"hemoglobin": 135, "vitamin_B12": 349.9}, cfg=exclusive) == {"b12_gray_zone"}


def test_signal_active_b12_low_with_foreign_thresholds():
    assert "active_b12_low" in signals({"hemoglobin": 135, "active_B12": 24.9}, cfg=FOREIGN)
    assert "active_b12_low" not in signals({"hemoglobin": 135, "active_B12": 25}, cfg=FOREIGN)


def test_source_policy_blocks_foreign_thresholds():
    """Правило источников: при use_foreign: false пороги с origin: foreign не дают ни сигналов, ни флага, ни анализов."""
    for b12 in (60, 179, 230, 350, 900):
        out = run({"hemoglobin": 135, "vitamin_B12": b12, "active_B12": 10, "homocysteine": 40}, cfg=LOCAL)
        assert out.rule_signals == [] and out.flags == [] and out.mandatory_tests == [], b12
    anemic = run({"hemoglobin": 110, "MCV": 90, "ferritin": 90, "CRP": 1, "folate": 12, "vitamin_B12": 230}, cfg=LOCAL)
    assert "b12_gray_zone" not in {f.code for f in anemic.flags}
    assert not {"active_B12", "homocysteine"} & set(tests_of(anemic))
    # те же данные при включённых порогах дают сигнал, флаг и функциональные маркеры
    numeric = run({"hemoglobin": 110, "MCV": 90, "ferritin": 90, "CRP": 1, "folate": 12, "vitamin_B12": 230}, cfg=FOREIGN)
    assert "b12_gray_zone" in {f.code for f in numeric.flags} and "b12_gray_zone" in {s.code for s in numeric.rule_signals}
    assert tests_of(numeric)[:2] == ["active_B12", "homocysteine"]
    # правило «анемия и MCV > 100 -> B12 и фолаты» от порога B12 не зависит
    for cfg in (LOCAL, FOREIGN):
        assert tests_of(run({"hemoglobin": 110, "MCV": 104, "ferritin": 90, "CRP": 1}, cfg=cfg))[:2] == ["vitamin_B12", "folate"]
    # нет блока policy в конфигурации — зарубежные пороги не применяются
    norms = copy.deepcopy(CFG.norms)
    norms.pop("policy", None)
    assert signals({"hemoglobin": 135, "vitamin_B12": 100}, cfg=dataclasses.replace(CFG, norms=norms)) == set()


def test_signal_folate_b6_copper():
    assert "folate_low" in signals({"hemoglobin": 135, "folate": 3.9})
    assert "folate_low" not in signals({"hemoglobin": 135, "folate": 4})
    assert "b6_low" in signals({"hemoglobin": 135, "vitamin_B6": 19.9})
    assert "b6_low" not in signals({"hemoglobin": 135, "vitamin_B6": 20})
    assert "copper_low" in signals({"hemoglobin": 135, "copper": 10.9})
    assert "copper_low" not in signals({"hemoglobin": 135, "copper": 11})


def test_signals_do_not_depend_on_hemoglobin():
    for hb in (95, 135):
        assert "ferritin_below_who" in signals({"hemoglobin": hb, "ferritin": 9})
    out = run({"hemoglobin": 135, "ferritin": 9})
    assert out.rule_signals[0].source == CFG.source_title("who_ferritin_2020")
    assert not out.rule_signals[0].text.endswith(".")            # сигналы склеивает движок: точки в конце нет


def test_no_signals_on_clean_panel():
    out = run({"hemoglobin": 135, **CBC_OK, "ferritin": 80, "vitamin_B12": 500, "folate": 12, "CRP": 1, "TSAT": 30})
    assert out.rule_signals == [] and out.flags == [] and out.checklist == [] and out.mandatory_tests == []


# ------------------------------------------------------------------ флаг 1: маска воспаления
def test_flag_ferritin_masked_by_inflammation():
    out = run({"hemoglobin": 104, "MCV": 79, "RDW": 16, "ferritin": 62, "CRP": 28, "TSAT": 11}, sex="M", age=61)
    flag = next(f for f in out.flags if f.code == "ferritin_masked_by_inflammation")
    assert "ложно нормальным" in flag.text and "70 мкг/л" in flag.text and "62 мкг/л" in flag.text
    assert "sTfR или Ret-He" in flag.text and "поддерживает дефицит железа" in flag.text
    assert "условная рекомендация ВОЗ" in flag.text                # порог 70 мкг/л при воспалении
    assert flag.source == CFG.source_title("who_ferritin_2020")
    assert tests_of(out) == ["sTfR", "Ret_He"]                    # НТЖ сдано — повторно не предлагается
    out = run({"hemoglobin": 104, "ferritin": 62, "CRP": 28}, sex="M")
    assert tests_of(out)[:3] == ["TSAT", "sTfR", "Ret_He"]
    out = run({"hemoglobin": 104, "ferritin": 62, "CRP": 28, "serum_iron": 5.5, "TIBC": 50}, sex="M")
    assert "TSAT" not in tests_of(out)                            # расчётное НТЖ считается известным


def test_flag_mask_negative():
    assert "ferritin_masked_by_inflammation" not in flags({"hemoglobin": 104, "ferritin": 62, "CRP": 3}, sex="M")
    assert "ferritin_masked_by_inflammation" not in flags({"hemoglobin": 104, "ferritin": 62}, sex="M")
    assert "ferritin_masked_by_inflammation" not in flags({"hemoglobin": 104, "ferritin": 90, "CRP": 28}, sex="M")
    # флаг не зависит от анемии
    assert "ferritin_masked_by_inflammation" in flags({"hemoglobin": 150, "ferritin": 62, "CRP": 28}, sex="M")


# ------------------------------------------------------------------ флаг 2: нормальный MCV при высоком RDW
def test_flag_normal_mcv_high_rdw():
    out = run({"hemoglobin": 96, "MCV": 91, "RDW": 19.5, "ferritin": 8}, age=58)
    flag = next(f for f in out.flags if f.code == "normal_mcv_high_rdw")
    assert "B12" in flag.text and "фолат" in flag.text and "ферритин 8 мкг/л — снижен" in flag.text
    assert tests_of(out) == ["vitamin_B12", "folate"]
    out = run({"hemoglobin": 96, "MCV": 100, "RDW": 14.6})        # границы MCV включаются, ферритин не сдан
    assert "normal_mcv_high_rdw" in {f.code for f in out.flags}
    assert tests_of(out)[:3] == ["ferritin", "vitamin_B12", "folate"]


def test_flag_normal_mcv_high_rdw_negative():
    assert "normal_mcv_high_rdw" not in flags({"hemoglobin": 125, "MCV": 91, "RDW": 19.5})          # анемии нет
    assert "normal_mcv_high_rdw" not in flags({"hemoglobin": 96, "MCV": 79.9, "RDW": 19.5})         # микроцитоз
    assert "normal_mcv_high_rdw" not in flags({"hemoglobin": 96, "MCV": 100.1, "RDW": 19.5})        # макроцитоз
    assert "normal_mcv_high_rdw" not in flags({"hemoglobin": 96, "MCV": 91, "RDW": 14.5})           # RDW не выше порога
    assert "normal_mcv_high_rdw" not in flags({"hemoglobin": 96, "MCV": 91})                        # RDW не сдан


def test_flag_normal_mcv_high_rdw_not_raised_when_both_axes_are_checked_and_clean():
    values = {"hemoglobin": 96, "MCV": 91, "RDW": 19.5, "ferritin": 120, "vitamin_B12": 420, "folate": 12, "CRP": 2}
    for cfg in (LOCAL, FOREIGN):
        out = run(values, cfg=cfg)
        assert {f.code for f in out.flags} == {"unexplained_checklist"}      # подсказка не мешает чек-листу
    both = run({**values, "ferritin": 8, "vitamin_B12": 150}, cfg=FOREIGN)
    flag = next(f for f in both.flags if f.code == "normal_mcv_high_rdw")
    assert "поддерживается" in flag.text                                     # обе оси снижены — сочетанный дефицит
    # пороги B12 выключены: железо снижено, B12 сдан, но не оценён — сочетание не исключено, подсказка остаётся
    unrated = run({**values, "ferritin": 8, "vitamin_B12": 150}, cfg=LOCAL)
    flag = next(f for f in unrated.flags if f.code == "normal_mcv_high_rdw")
    assert "B12 150 пг/мл — оценка по референсу лаборатории" in flag.text and "поддерживается" not in flag.text
    only_iron = run({**values, "ferritin": 8}, cfg=FOREIGN)                 # B12 и фолаты в норме по порогам
    assert "normal_mcv_high_rdw" not in {f.code for f in only_iron.flags}


# ------------------------------------------------------------------ флаг 3: B12 в серой зоне
def test_flag_b12_gray_zone_with_foreign_thresholds():
    out = run({"hemoglobin": 118, "MCV": 104, "vitamin_B12": 230, "folate": 9}, sex="M", age=67, cfg=FOREIGN)
    flag = next(f for f in out.flags if f.code == "b12_gray_zone")
    assert "функциональный маркер" in flag.text and "активный B12" in flag.text and "гомоцистеин" in flag.text
    assert "в рознице Москвы отдельным анализом недоступна" in flag.text
    assert "рСКФ" not in flag.text
    assert tests_of(out)[:2] == ["active_B12", "homocysteine"]
    assert "b12_gray_zone" not in flags({"hemoglobin": 118, "vitamin_B12": 400}, sex="M", cfg=FOREIGN)
    assert "b12_gray_zone" not in flags({"hemoglobin": 118, "vitamin_B12": 150}, sex="M", cfg=FOREIGN)
    assert "b12_gray_zone" in flags({"hemoglobin": 150, "vitamin_B12": 230}, sex="M", cfg=FOREIGN)   # и без анемии
    assert "b12_gray_zone" in flags({"hemoglobin": 150, "vitamin_B12": 350}, sex="M", cfg=FOREIGN)   # граница входит
    # те же данные без зарубежных порогов: числового флага нет (пограничный B12 определяет движок по модели)
    local = run({"hemoglobin": 118, "MCV": 104, "vitamin_B12": 230, "folate": 9}, sex="M", age=67, cfg=LOCAL)
    assert "b12_gray_zone" not in {f.code for f in local.flags}


def test_flag_b12_gray_zone_functional_markers_and_kidney():
    out = run({"hemoglobin": 118, "vitamin_B12": 230, "active_B12": 20, "homocysteine": 22, "eGFR": 45}, sex="M", age=67,
              cfg=FOREIGN)
    text = next(f for f in out.flags if f.code == "b12_gray_zone").text
    assert "функциональный дефицит подтверждается" in text
    assert "поддерживает" in text
    assert "При рСКФ ниже 60" in text and "повышаются и без дефицита B12" in text
    assert "active_B12" not in tests_of(out) and "homocysteine" not in tests_of(out)
    ok = run({"hemoglobin": 118, "vitamin_B12": 230, "active_B12": 90, "homocysteine": 9, "eGFR": 90}, sex="M", cfg=FOREIGN)
    text = next(f for f in ok.flags if f.code == "b12_gray_zone").text
    assert "подтверждается" not in text and "поддерживает" not in text and "При рСКФ" not in text
    edge = run({"hemoglobin": 118, "vitamin_B12": 230, "active_B12": 70}, sex="M", cfg=FOREIGN)   # 25 ≤ активный B12 ≤ 70
    assert "тоже в серой зоне" in next(f for f in edge.flags if f.code == "b12_gray_zone").text
    # расчётная рСКФ (по креатинину) тоже учитывается
    calc = run({"hemoglobin": 105, "vitamin_B12": 230, "creatinine": 160}, sex="F", age=74, cfg=FOREIGN)
    assert "При рСКФ ниже 60" in next(f for f in calc.flags if f.code == "b12_gray_zone").text


# ------------------------------------------------------------------ флаг 4: микроцитоз не от железа
def test_flag_microcytosis_not_iron():
    out = run({"hemoglobin": 101, "RBC": 4.39, "MCV": 72, "MCH": 23.0, "ferritin": 180, "TSAT": 45}, age=45)
    flag = next(f for f in out.flags if f.code == "microcytosis_not_iron")
    assert out.derived["mentzer"] == 16.4
    assert "не объясняется дефицитом железа" in flag.text
    assert "талассемию и другие гемоглобинопатии" in flag.text and "электрофорез гемоглобина" in flag.text
    # Ментцер 16,4 ≥ 13 при несниженном железе: не «меньше 13 — вероятнее носительство» (п. 1 ревизии содержания)
    # и не «скорее дефицит железа» — это спорило бы с флагом
    assert ("Индекс Ментцера (MCV/RBC) 16,4 — не меньше 13: на носительство β-талассемии не указывает, но и не "
            "исключает его; проверка — электрофорез гемоглобина.") in flag.text
    assert "меньше 13 — вероятнее" not in flag.text
    assert "вероятнее дефицит железа" not in flag.text and "скорее дефицит железа" not in flag.text
    assert CFG.source_title(CFG.norms["indices"]["mentzer_source"]) in flag.source
    assert "анемию хронических заболеваний" in flag.text and "сидеробластную анемию" in flag.text
    assert "проверить B6, медь, церулоплазмин" in flag.text
    assert tests_of(out) == ["vitamin_B6", "copper", "ceruloplasmin"]
    assert "ферритин 180 мкг/л, НТЖ 45 % — не снижены" in flag.text
    # только гипохромия (MCH), НТЖ не сдано, анемии нет — флаг тоже ставится
    hypo = run({"hemoglobin": 135, "RBC": 4.5, "MCV": 85, "MCH": 26.9, "ferritin": 30})
    text = next(f for f in hypo.flags if f.code == "microcytosis_not_iron").text
    assert text.startswith("Гипохромия (MCH 26,9 пг) не объясняется дефицитом железа: ферритин 30 мкг/л — не снижен.")
    assert "Ментцера" not in text and "mentzer" not in hypo.derived          # микроцитоза нет — индекс не считается
    assert "Ментцера" not in next(f for f in hypo.flags if f.code == "microcytosis_not_iron").source
    no_rbc = run({"hemoglobin": 101, "MCV": 72, "MCH": 23.0, "ferritin": 180})
    assert "Индекс Ментцера не рассчитан" in next(f for f in no_rbc.flags if f.code == "microcytosis_not_iron").text


def test_flag_microcytosis_not_iron_negative():
    base = {"hemoglobin": 101, "RBC": 4.39, "MCV": 72, "MCH": 23.0}
    assert "microcytosis_not_iron" not in flags(base)                                        # ферритин не сдан
    assert "microcytosis_not_iron" not in flags({**base, "ferritin": 29.9})                  # ферритин снижен
    assert "microcytosis_not_iron" not in flags({**base, "ferritin": 180, "TSAT": 12})       # НТЖ снижено
    assert "microcytosis_not_iron" not in flags({**base, "ferritin": 50, "CRP": 20})         # маска воспаления
    assert "microcytosis_not_iron" not in flags({"hemoglobin": 101, "MCV": 80, "MCH": 27, "ferritin": 180})
    # ферритин выше порога «при воспалении» — маски нет, флаг ставится и упоминает воспаление
    out = run({**base, "ferritin": 180, "CRP": 20})
    assert "повышен" in next(f for f in out.flags if f.code == "microcytosis_not_iron").text


def test_crp_goes_first_when_ferritin_reading_depends_on_it():
    out = run({"hemoglobin": 101, "RBC": 4.39, "MCV": 72, "MCH": 23.0, "ferritin": 45})
    assert tests_of(out) == ["CRP", "vitamin_B6", "copper", "ceruloplasmin"]
    assert "СРБ не сдан" in next(f for f in out.flags if f.code == "microcytosis_not_iron").text


def test_mentzer_only_with_microcytosis():
    assert run({"hemoglobin": 135, "MCV": 72, "RBC": 4.39}).derived["mentzer"] == 16.4
    assert run({"hemoglobin": 135, "MCV": 79.9, "RBC": 4.0}).derived["mentzer"] == 20.0
    assert "mentzer" not in run({"hemoglobin": 135, "MCV": 80, "RBC": 4.0}).derived     # MCV не ниже порога
    assert "mentzer" not in run({"hemoglobin": 135, "MCV": 90, "RBC": 4.5}).derived
    assert "mentzer" not in run({"hemoglobin": 135, "MCV": 72}).derived                 # нет эритроцитов


# ------------------------------------------------------------------ флаг 5: чек-лист исключений
CASE5 = {"hemoglobin": 105, "MCV": 88, "RDW": 14.2, "ferritin": 120, "vitamin_B12": 420, "folate": 12, "CRP": 2,
         "creatinine": 160}


def checklist(values, **kw) -> dict[str, str]:
    return {i.code: i.status for i in run(values, **kw).checklist}


def test_flag_unexplained_checklist():
    out = run(CASE5, age=74, cfg=FOREIGN)
    assert [f.code for f in out.flags] == ["unexplained_checklist"]
    assert [i.code for i in out.checklist] == ["iron", "b12", "folate", "inflammation", "kidney", "thyroid", "copper",
                                                "hemolysis"]
    assert checklist(CASE5, age=74, cfg=FOREIGN) == {"iron": "excluded", "b12": "excluded", "folate": "excluded",
                                                     "inflammation": "excluded", "kidney": "suspected",
                                                     "thyroid": "not_checked", "copper": "not_checked",
                                                     "hemolysis": "not_checked"}
    kidney = next(i for i in out.checklist if i.code == "kidney")
    assert "выраженное снижение" in kidney.detail and "расчёт" in kidney.detail      # рСКФ ≈ 29 < 30
    assert abs(out.derived["egfr_calc"] - 29.0) < 0.5
    assert tests_of(out) == ["TSH", "LDH", "haptoglobin", "reticulocytes"]


def test_checklist_b12_without_foreign_thresholds():
    """Пороги B12 выключены: пункт «b12» всегда not_checked, а сам чек-лист это не блокирует."""
    out = run(CASE5, age=74, cfg=LOCAL)
    assert [f.code for f in out.flags] == ["unexplained_checklist"]
    status = {i.code: i.status for i in out.checklist}
    assert status == {"iron": "excluded", "b12": "not_checked", "folate": "excluded", "inflammation": "excluded",
                      "kidney": "suspected", "thyroid": "not_checked", "copper": "not_checked", "hemolysis": "not_checked"}
    b12 = next(i for i in out.checklist if i.code == "b12")
    assert b12.detail == "сдан: 420 пг/мл; числового порога в КР РФ нет — оценить по референсу лаборатории"
    assert tests_of(out) == ["TSH", "LDH", "haptoglobin", "reticulocytes"]                  # сданный B12 повторно не предлагается
    for b12_value in (100, 230, 900):                                        # любое значение B12 чек-лист не блокирует
        assert "unexplained_checklist" in flags({**CASE5, "vitamin_B12": b12_value}, age=74, cfg=LOCAL)
    missing = run({k: v for k, v in CASE5.items() if k != "vitamin_B12"}, age=74, cfg=LOCAL)
    item = next(i for i in missing.checklist if i.code == "b12")
    assert (item.status, item.detail) == ("not_checked", "не сдан")
    assert "vitamin_B12" in tests_of(missing)
    active = run({**{k: v for k, v in CASE5.items() if k != "vitamin_B12"}, "active_B12": 40}, age=74, cfg=LOCAL)
    item = next(i for i in active.checklist if i.code == "b12")
    assert item.status == "not_checked" and "активный B12 сдан: 40 пмоль/л" in item.detail


def test_flag_unexplained_checklist_negative():
    assert run({**CASE5, "hemoglobin": 125}, age=74).checklist == []                          # анемии нет
    assert "unexplained_checklist" not in flags({**CASE5, "ferritin": 8}, age=74)             # есть сигнал дефицита
    assert "unexplained_checklist" not in flags({**CASE5, "vitamin_B12": 230}, age=74, cfg=FOREIGN)   # другой флаг
    cbc_only = run({"hemoglobin": 68, "MCV": 78, "RDW": 18}, sex="M")                         # причину ещё не искали
    assert cbc_only.flags == [] and cbc_only.checklist == []
    assert tests_of(cbc_only) == ["ferritin", "CRP", "serum_iron", "TIBC"]


def test_checklist_statuses():
    base = {"hemoglobin": 105, "MCV": 88, "RDW": 13, "ferritin": 120, "vitamin_B12": 420, "folate": 12}
    assert checklist({**base, "CRP": 12})["inflammation"] == "suspected"
    assert "возможна анемия воспаления" in next(
        i for i in run({**base, "CRP": 12}).checklist if i.code == "inflammation").detail
    assert checklist(base)["inflammation"] == "not_checked"
    assert checklist({**base, "eGFR": 59})["kidney"] == "suspected"
    assert checklist({**base, "eGFR": 60})["kidney"] == "excluded"
    assert checklist(base)["kidney"] == "not_checked"
    assert checklist({**base, "TSH": 4.1})["thyroid"] == "suspected"
    assert checklist({**base, "TSH": 4.0})["thyroid"] == "excluded"
    marked = next(i for i in run({**base, "TSH": 12}).checklist if i.code == "thyroid")
    assert marked.status == "suspected" and "выраженное повышение" in marked.detail
    # гемолиз (ответ врача, п. 11): ЛДГ 214 — верхняя граница (Ж) входит в референс; без гаптоглобина — «не проверено»
    hem = next(i for i in run({**base, "LDH": 214, "reticulocytes": 1.2}).checklist if i.code == "hemolysis")
    assert hem.status == "not_checked" and "ЛДГ 214 Ед/л — в пределах референса 135–214 Ед/л" in hem.detail
    assert "гаптоглобин — не сдан" in hem.detail
    assert checklist({**base, "folate": 6})["folate"] == "suspected"                 # пограничный уровень 4–8
    partial = checklist({"hemoglobin": 105, "CRP": 2})                               # дефициты не проверялись
    assert partial["iron"] == partial["b12"] == partial["folate"] == "not_checked"


def test_checklist_iron_needs_crp_when_ferritin_below_inflammation_threshold():
    base = {"hemoglobin": 105, "MCV": 88, "vitamin_B12": 420, "folate": 12}
    assert checklist({**base, "ferritin": 45})["iron"] == "not_checked"              # без СРБ вывод ненадёжен
    assert checklist({**base, "ferritin": 45, "CRP": 2})["iron"] == "excluded"
    assert checklist({**base, "ferritin": 90})["iron"] == "excluded"
    assert tests_of(run({**base, "ferritin": 45}))[0] == "CRP"


def test_checklist_mandatory_tests_only_missing():
    out = run({"hemoglobin": 105, "MCV": 88, "CRP": 2})
    assert tests_of(out) == ["ferritin", "serum_iron", "TIBC", "vitamin_B12", "folate", "creatinine", "TSH", "LDH",
                             "haptoglobin", "reticulocytes"]
    assert len(set(tests_of(out))) == len(tests_of(out))                             # без повторов
    out = run({**CASE5, "TSH": 2, "LDH": 200, "haptoglobin": 1.0, "reticulocytes": 1}, age=74)
    assert tests_of(out) == []


# ------------------------------------------------------------------ обязательные анализы вне флагов
def test_mandatory_tests_for_anemia_without_ferritin():
    out = run({"hemoglobin": 110, "MCV": 85, "RDW": 13})
    assert tests_of(out) == ["ferritin", "CRP", "serum_iron", "TIBC"]
    assert out.mandatory_tests[0]["source"] == CFG.source_title("kr_ida_2024")
    assert "клиническим рекомендациям" in out.mandatory_tests[0]["reason"]
    assert set(out.mandatory_tests[0]) == {"analyte", "reason", "source"}
    assert "правильно прочитать ферритин" in out.mandatory_tests[1]["reason"]
    assert tests_of(run({"hemoglobin": 110, "MCV": 85, "RDW": 13, "CRP": 1, "TSAT": 30})) [0] == "ferritin"
    assert "serum_iron" not in tests_of(run({"hemoglobin": 110, "MCV": 85, "TSAT": 30}))    # НТЖ сдано напрямую
    assert run({"hemoglobin": 135, "MCV": 85}).mandatory_tests == []                        # без анемии — ничего


def test_mandatory_b12_and_folate_for_macrocytic_anemia():
    out = run({"hemoglobin": 110, "MCV": 104, "ferritin": 90, "CRP": 1})
    assert tests_of(out)[:2] == ["vitamin_B12", "folate"]
    assert "макроцитоз" in out.mandatory_tests[0]["reason"]
    assert "vitamin_B12" not in tests_of(run({"hemoglobin": 110, "MCV": 100, "RDW": 13}))   # MCV строго выше 100
    assert "vitamin_B12" not in tests_of(run({"hemoglobin": 135, "MCV": 104}))              # анемии нет


def test_pregnancy_only_guideline_tests():
    out = run({"hemoglobin": 100, "MCV": 72, "MCH": 23, "RDW": 19, "vitamin_B12": 230},
              pregnancy={"status": "yes", "trimester": 2})
    assert out.flags == [] and out.rule_signals == [] and out.checklist == []
    assert tests_of(out) == ["ferritin", "CRP", "serum_iron", "TIBC"]


def test_thresholds_come_from_config():
    norms = copy.deepcopy(FOREIGN.norms)
    norms["ferritin"]["deficiency_below"]["who"] = 12
    norms["vitamin_b12"]["gray_zone"] = [180, 300]
    cfg = dataclasses.replace(CFG, norms=norms)
    assert "ferritin_below_who" not in signals({"hemoglobin": 135, "ferritin": 13}, cfg=cfg)
    assert "ferritin_below_who" in signals({"hemoglobin": 135, "ferritin": 11}, cfg=cfg)
    assert "b12_gray_zone" not in flags({"hemoglobin": 135, "vitamin_B12": 320}, cfg=cfg)
    # порога нет в конфигурации — показатель не оценивается
    norms = copy.deepcopy(CFG.norms)
    del norms["copper"]
    assert "copper_low" not in signals({"hemoglobin": 135, "copper": 5}, cfg=dataclasses.replace(CFG, norms=norms))
