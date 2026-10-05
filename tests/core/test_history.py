"""Анализ всей истории (deficitlens_core/history.py, поток П4): ряды, события, полнота картины, тексты по ролям.

Все значения придуманы (синтетические ряды, не данные пациентов). Большинство тестов разбирает записи движком без
моделей (Models() — только правила): так быстро и детерминированно; события по скринингу проверяются на готовых
ответах движка с подставленным уровнем риска. Дата «сегодня» передаётся параметром today.
"""
from __future__ import annotations

import copy
import dataclasses
import json
import time
from datetime import date

import helpers  # noqa: F401 — пути к src и tests
import yaml

from deficitlens_core import history
from deficitlens_core.config import config_dir, load_config
from deficitlens_core.engine import Models, load_models
from deficitlens_core.engine import analyze as engine_analyze
from deficitlens_core.reports import NEUTRAL_PATIENT_LINE, filter_patient_line
from deficitlens_core.schemas import AnalysisInput

CFG = load_config()
RULES = Models()                    # без моделей: уровень 1, правила, отчёты (скрининг и уровень 2 выключены)
TODAY = date(2026, 10, 4)
# Ровный ОАК без отклонений; гемоглобин, MCV, ферритин и прочее задаёт каждый тест.
CBC = {"RBC": 4.5, "hematocrit": 40, "MCV": 89, "MCH": 29, "MCHC": 330, "RDW": 13, "platelets": 250, "WBC": 6}
RISK_RU = {"usual": "обычный", "elevated": "повышенный", "high": "высокий"}


def rec(rid: str, day: str, values: dict, *, sex: str = "F", age: int = 34, pregnancy: dict | None = None,
        cached: bool = False, **extra) -> dict:
    inp = {"sex": sex, "age_years": age, "values": {**CBC, **values}, **extra}
    if pregnancy:
        inp["pregnancy"] = pregnancy
    out = {"id": rid, "date": day, "input": inp, "result": None}
    if cached:
        out["result"] = engine_analyze(AnalysisInput.model_validate(inp), models=RULES, cfg=CFG).model_dump(mode="json")
    return out


def with_risk(record: dict, risk: str) -> dict:
    """Готовый ответ движка с подставленным результатом скрининга по ОАК (модель в тестах не нужна)."""
    record["result"]["hidden_deficiency"]["screening"] = {
        "applicable": True, "risk": risk, "risk_ru": RISK_RU[risk], "recommend_ferritin": risk != "usual"}
    return record


def run(records: list[dict], role: str = "doctor", cfg=CFG, **kw) -> dict:
    kw.setdefault("today", TODAY)
    kw.setdefault("models", RULES)
    return history.analyze(records, cfg=cfg, role=role, **kw)


def codes(out: dict) -> list[str]:
    return [e["code"] for e in out["events"]]


def event(out: dict, code: str) -> dict:
    return next(e for e in out["events"] if e["code"] == code)


def cfg_with(**norms_patch) -> object:
    """Копия конфигурации с изменёнными записями norms_ru.yaml (файл не меняется)."""
    norms = copy.deepcopy(CFG.norms)
    for path, value in norms_patch.items():
        node = norms
        keys = path.split("__")
        for k in keys[:-1]:
            node = node[k]
        node[keys[-1]] = value
    return dataclasses.replace(CFG, norms=norms)


def patient_lines(out: dict) -> list[str]:
    lines = [out["summary"]]
    lines += [x for e in out["events"] for x in (e["title"], e["text"])]
    lines += [c["reason"] for c in out["completeness"]]
    if out["latest"]:
        lines.append(out["latest"]["headline"])
    return lines


# ------------------------------------------------------------------ пустая история и одна запись
def test_empty_history():
    for role in history.ROLES:
        out = history.analyze([], cfg=CFG, role=role, today=TODAY)      # моделей не нужно: записей нет
        assert out["role"] == role and out["n_records"] == 0
        assert out["first_date"] is None and out["last_date"] is None and out["latest"] is None
        assert out["series"] == [] and out["events"] == []
        assert [c["analyte"] for c in out["completeness"]] == [b.code for b in history.BASE_SET]
        assert all(c["status"] == "never" and c["last_date"] is None for c in out["completeness"])
        assert [c["priority"] for c in out["completeness"]] == list(range(1, len(history.BASE_SET) + 1))
        assert out["summary"]
        json.dumps(out, ensure_ascii=False)
    assert run([], role="patient")["summary"] == "Анализов в истории пока нет."


def test_single_record_direction_single_and_no_events():
    out = run([rec("r1", "2026-09-01", {"hemoglobin": 130})])
    assert out["n_records"] == 1 and out["first_date"] == out["last_date"] == "2026-09-01"
    assert out["series"] and all(s["direction"] == "single" for s in out["series"])
    assert all(s["delta"] is None and s["delta_pct"] is None and s["months"] == 0.0 for s in out["series"])
    assert out["events"] == []
    assert out["latest"]["record_id"] == "r1" and out["latest"]["headline"]
    assert run([rec("r1", "2026-09-01", {"hemoglobin": 130})], role="patient")["summary"] == (
        "1 анализ, сентябрь 2026; изменения можно будет увидеть после следующего анализа")


def test_cbc_stale_only_event_for_old_single_record():
    old = run([rec("r1", "2025-05-01", {"hemoglobin": 130})])
    assert codes(old) == ["cbc_stale"]
    e = event(old, "cbc_stale")
    assert e["severity"] == "info" and e["date"] == "2025-05-01" and e["analytes"] == ["hemoglobin"]
    assert "01.05.2025" in e["text"] and "17 мес" in e["text"] and "12 мес" in e["text"]
    patient = event(run([rec("r1", "2025-05-01", {"hemoglobin": 130})], role="patient"), "cbc_stale")
    assert patient["text"] == ("Общий анализ крови последний раз сдавали 17 месяцев назад — обсудите с врачом, "
                               "когда его повторить.")
    # Граница: ровно 12 календарных месяцев — ещё не «давно», на день больше — уже «давно».
    assert codes(run([rec("r1", "2025-10-04", {"hemoglobin": 130})])) == []
    assert codes(run([rec("r1", "2025-10-03", {"hemoglobin": 130})])) == ["cbc_stale"]
    # Дата «сегодня» по умолчанию — системная (через внутреннюю функцию).
    saved = history._today
    try:
        history._today = lambda: date(2025, 6, 1)
        assert codes(history.analyze([rec("r1", "2025-05-01", {"hemoglobin": 130})], cfg=CFG, models=RULES)) == []
    finally:
        history._today = saved


# ------------------------------------------------------------------ события
def test_hb_drop():
    two = [rec("a", "2026-03-12", {"hemoglobin": 132}), rec("c", "2026-09-12", {"hemoglobin": 118})]
    doc = event(run(two), "hb_drop")
    assert doc["severity"] == "attention" and doc["date"] == "2026-09-12" and doc["analytes"] == ["hemoglobin"]
    assert "Hb снизился со 132 до 118 г/л за 6 мес (−14 г/л, −11 %; 12.03.2026 → 12.09.2026)" in doc["text"]
    assert "решение команды проекта" in doc["text"] and "выбор проекта" not in doc["text"]
    # Несколько анализов в окне: база — медиана предыдущих (устойчива к «шуму»), а не максимум.
    recs = [rec("a", "2026-03-12", {"hemoglobin": 132}), rec("b", "2026-06-10", {"hemoglobin": 125}),
            rec("c", "2026-09-12", {"hemoglobin": 118})]
    doc = event(run(recs), "hb_drop")
    assert doc["severity"] == "attention" and doc["title"] == "Снижение гемоглобина"
    assert ("Hb снизился до 118 г/л за 6 мес: медиана предыдущих анализов за ≤ 12 мес — 128,5 г/л "
            "(n = 2, 12.03.2026–10.06.2026; −10,5 г/л, −8 %)") in doc["text"], doc["text"]
    pat = event(run(recs, role="patient"), "hb_drop")
    assert pat["text"] == "Гемоглобин за 6 месяцев снизился — покажите историю врачу."
    # Один высокий выброс в окне больше не даёт события (раньше база была максимумом окна: 145 -> 129 = −16 г/л).
    peak = [rec("a", "2026-01-10", {"hemoglobin": 130}), rec("b", "2026-03-01", {"hemoglobin": 145}),
            rec("c", "2026-05-01", {"hemoglobin": 131}), rec("d", "2026-09-01", {"hemoglobin": 129})]
    assert "hb_drop" not in codes(run(peak))
    # Вне окна (больше 12 мес) — не событие; маленькое снижение — не событие.
    assert "hb_drop" not in codes(run([rec("a", "2025-01-10", {"hemoglobin": 140}),
                                       rec("b", "2026-09-01", {"hemoglobin": 125})]))
    assert "hb_drop" not in codes(run([rec("a", "2026-03-01", {"hemoglobin": 130}),
                                       rec("b", "2026-09-01", {"hemoglobin": 124})]))
    # Только по доле: 90 -> 81 г/л — 9 г/л, но 10 %.
    pct_only = run([rec("a", "2026-03-01", {"hemoglobin": 90}), rec("b", "2026-09-01", {"hemoglobin": 81})])
    assert "с 90 до 81 г/л" in event(pct_only, "hb_drop")["text"]
    # Порог — из конфигурации (history.hb_drop), не из кода.
    strict = cfg_with(history__hb_drop={"min_drop_g_l": 20, "min_drop_pct": 20, "window_months": 12})
    assert "hb_drop" not in codes(run(recs, cfg=strict))


def test_hb_drop_noise_and_series_direction():
    """M3: шумный ровный ряд не даёт «внимания»; «снизился» не спорит с направлением ряда за всю историю."""
    noisy = [rec(f"n{i}", f"2026-0{i + 1}-05", {"hemoglobin": hb})
             for i, hb in enumerate((130, 135, 126, 133, 128, 138, 127))]
    out = run(noisy)
    assert out["series"][0]["direction"] == "flat"
    e = event(out, "hb_drop")              # скачок 138 -> 127 к предыдущему анализу — только «к сведению»
    assert e["severity"] == "info" and e["title"] == "Снижение гемоглобина к предыдущему анализу"
    assert "по сравнению с предыдущим анализом: 138 → 127 г/л" in e["text"] and "к сведению" in e["text"]
    assert "За всю историю ряд в пределах «шума» (130 → 127 г/л" in e["text"]
    assert run(noisy, role="patient")["summary"].endswith("изменений, требующих внимания, нет")
    # Ряд за всю историю вырос (118 -> 128), но к предыдущему анализу Hb упал на 12 г/л: текст — «по сравнению
    # с предыдущим анализом», а не «снизился за период».
    up = [rec("a", "2025-01-01", {"hemoglobin": 118}), rec("b", "2026-03-01", {"hemoglobin": 140}),
          rec("c", "2026-09-01", {"hemoglobin": 128})]
    out = run(up)
    assert out["series"][0]["direction"] == "up"
    e = event(out, "hb_drop")
    assert e["severity"] == "attention"
    assert "Hb снизился по сравнению с предыдущим анализом: 140 → 128 г/л (−12 г/л, −9 %; 01.03.2026 → 01.09.2026)" \
        in e["text"]
    assert "За всю историю ряд вырос (118 → 128 г/л" in e["text"] and "снизился со" not in e["text"]
    assert event(run(up, role="patient"), "hb_drop")["text"] == (
        "Гемоглобин снизился по сравнению с предыдущим анализом — покажите историю врачу.")
    # Ряд за всю историю вырос, к предыдущему анализу снижение маленькое, но последнее заметно ниже медианы окна:
    # событие есть, слова «снизился» нет.
    median = [rec("a", "2024-06-01", {"hemoglobin": 100}), rec("b", "2025-11-01", {"hemoglobin": 142}),
              rec("c", "2026-02-01", {"hemoglobin": 140}), rec("d", "2026-05-01", {"hemoglobin": 135}),
              rec("e", "2026-09-01", {"hemoglobin": 129})]
    e = event(run(median), "hb_drop")
    assert e["severity"] == "attention" and e["title"] == "Гемоглобин ниже медианы предыдущих анализов"
    assert "Hb 129 г/л — ниже медианы предыдущих анализов за ≤ 12 мес: 140 г/л (n = 3" in e["text"]
    assert "снизился" not in e["text"]
    pat = event(run(median, role="patient"), "hb_drop")
    assert "снизился" not in pat["title"] + pat["text"]
    assert pat["text"] == "Гемоглобин в последнем анализе ниже, чем в предыдущих, — покажите историю врачу."


def test_hb_drop_pregnancy_texts():
    """M7: снижение Hb к анализу при беременности — врачу пометка про гемодилюцию, пациентке мягкий текст."""
    recs = [rec("a", "2026-03-01", {"hemoglobin": 128}),
            rec("b", "2026-08-01", {"hemoglobin": 112}, pregnancy={"status": "yes", "trimester": 2})]
    assert "гемодилюция" in event(run(recs), "hb_drop")["text"]
    pat = event(run(recs, role="patient"), "hb_drop")
    assert "гемодилюция" not in pat["text"]
    assert pat["text"] == ("Гемоглобин ниже, чем в прошлых анализах. При беременности это часто связано "
                           "с разведением крови — обсудите это с врачом, ведущим беременность.")
    # Без беременности в последнем анализе — обычный текст.
    plain = [rec("a", "2026-03-01", {"hemoglobin": 128}), rec("b", "2026-08-01", {"hemoglobin": 112})]
    assert "беремен" not in event(run(plain, role="patient"), "hb_drop")["text"]


def test_anemia_new_and_resolved():
    new = run([rec("a", "2026-03-01", {"hemoglobin": 125}), rec("b", "2026-09-01", {"hemoglobin": 115})])
    e = event(new, "anemia_new")
    assert e["severity"] == "attention" and "ВОЗ 2024" in e["text"]
    assert "Hb 125 → 115 г/л при пороге 120 г/л" in e["text"]
    gone = run([rec("a", "2026-03-01", {"hemoglobin": 115}), rec("b", "2026-09-01", {"hemoglobin": 125})])
    assert event(gone, "anemia_resolved")["severity"] == "info"
    assert "anemia_new" not in codes(gone)
    both = run([rec("a", "2026-03-01", {"hemoglobin": 115}), rec("b", "2026-09-01", {"hemoglobin": 113})])
    assert not {"anemia_new", "anemia_resolved"} & set(codes(both))
    # Сравниваются только предпоследний и последний анализ.
    older = run([rec("a", "2026-01-01", {"hemoglobin": 125}), rec("b", "2026-04-01", {"hemoglobin": 115}),
                 rec("c", "2026-09-01", {"hemoglobin": 114})])
    assert "anemia_new" not in codes(older)
    pat = event(run([rec("a", "2026-03-01", {"hemoglobin": 125}), rec("b", "2026-09-01", {"hemoglobin": 115})],
                    role="patient"), "anemia_new")
    assert "анеми" not in (pat["title"] + pat["text"]).lower()


def test_anemia_change_uses_last_threshold_and_context():
    """H6: «появилась / ушла» — от перехода Hb через порог последней записи, а не от смены порога."""
    t1, t2, t3 = ({"status": "yes", "trimester": t} for t in (1, 2, 3))
    hb_codes = {"anemia_new", "anemia_resolved", "hb_context_changed"}

    def hb_events(recs, role="doctor"):
        return [e for e in run(recs, role=role)["events"] if e["code"] in hb_codes]

    # Начало беременности: 115 г/л (порог 120) -> 112 г/л (II триместр, порог 105) — Hb не «поднялся до порога».
    preg = [rec("a", "2026-01-01", {"hemoglobin": 115}), rec("b", "2026-06-01", {"hemoglobin": 112}, pregnancy=t2)]
    [e] = hb_events(preg)
    assert e["code"] == "hb_context_changed" and e["severity"] == "info"
    assert ("Hb 115 г/л (01.01.2026; женщины, порог 120 г/л) → 112 г/л (01.06.2026; беременность, II триместр, "
            "порог 105 г/л): изменился контекст оценки (беременность) — пороги разные, сравнение условно") in e["text"]
    assert "Критерий анемии в последнем анализе: нет (ВОЗ 2024)" in e["text"]
    [p] = hb_events(preg, role="patient")
    assert p["title"] == "Изменились условия оценки" and "поднялся" not in p["title"] + p["text"]
    assert p["text"] == ("С прошлого анализа изменились условия оценки (беременность): пороги для гемоглобина другие, "
                         "поэтому сравнивать результаты напрямую нельзя — покажите историю врачу.")
    # Смена пола M -> F при том же Hb: событие о контексте, не «поднялся до порога».
    sex = [rec("a", "2026-01-01", {"hemoglobin": 125}, sex="M"), rec("b", "2026-06-01", {"hemoglobin": 124})]
    [e] = hb_events(sex)
    assert e["code"] == "hb_context_changed" and "(пол)" in e["text"] and "мужчины, порог 130 г/л" in e["text"]
    assert "пол в анкете" in hb_events(sex, role="patient")[0]["text"]
    # После родов Hb вырос 108 -> 116, но порог стал 120: не «появилась», а смена контекста («внимание»: критерий есть).
    post = [rec("a", "2026-01-01", {"hemoglobin": 108}, pregnancy=t2), rec("b", "2026-06-01", {"hemoglobin": 116})]
    [e] = hb_events(post)
    assert e["code"] == "hb_context_changed" and e["severity"] == "attention"
    assert "Критерий анемии в последнем анализе: есть" in e["text"]
    # Смена триместра — не смена контекста: оба значения сравниваются с порогом последней записи.
    assert hb_events([rec("a", "2026-01-01", {"hemoglobin": 108}, pregnancy=t1),        # 108 < 110: анемия в I
                      rec("b", "2026-04-01", {"hemoglobin": 107}, pregnancy=t2)]) == []   # 107 ≥ 105: Hb не вырос
    assert hb_events([rec("a", "2026-01-01", {"hemoglobin": 107}, pregnancy=t2),
                      rec("b", "2026-04-01", {"hemoglobin": 108}, pregnancy=t3)]) == []   # 108 < 110: Hb не упал
    [e] = hb_events([rec("a", "2026-01-01", {"hemoglobin": 112}, pregnancy=t1),
                     rec("b", "2026-04-01", {"hemoglobin": 103}, pregnancy=t2)])
    assert e["code"] == "anemia_new" and "Hb 112 → 103 г/л при пороге 105 г/л" in e["text"]
    assert ("Оба значения сравнены с порогом последнего анализа (беременность, II триместр); в предыдущем "
            "(беременность, I триместр) порог был 110 г/л.") in e["text"]
    # Тот же контекст и тот же порог — как раньше.
    assert [x["code"] for x in hb_events([rec("a", "2026-01-01", {"hemoglobin": 112}, pregnancy=t2),
                                          rec("b", "2026-04-01", {"hemoglobin": 104}, pregnancy=t2)])] == ["anemia_new"]


def test_ferritin_drop():
    hb = {"hemoglobin": 130}
    crossed = run([rec("a", "2026-01-10", {**hb, "ferritin": 45}), rec("b", "2026-07-10", {**hb, "ferritin": 25})])
    e = event(crossed, "ferritin_drop")
    assert e["severity"] == "attention" and e["analytes"] == ["ferritin"]
    assert "с 45 до 25 мкг/л за 6 мес (−44 %" in e["text"] and "ниже порога 30 мкг/л (порог практики РФ)" in e["text"]
    assert "КР ЖДА 2024" not in e["text"]           # в КР числа для взрослых нет (norms_ru.yaml → ferritin.note)
    both = event(run([rec("a", "2026-01-10", {**hb, "ferritin": 40}), rec("b", "2026-07-10", {**hb, "ferritin": 12})]),
                 "ferritin_drop")
    assert "ниже порогов 30 мкг/л" in both["text"] and "15 мкг/л (ВОЗ 2020)" in both["text"]
    # Падение ≥ 30 % без перехода порога — «к сведению».
    pct = run([rec("a", "2026-01-10", {**hb, "ferritin": 80}), rec("b", "2026-07-10", {**hb, "ferritin": 50})])
    assert event(pct, "ferritin_drop")["severity"] == "info"
    # Переход порога 30 без большого падения — событие, если снижение больше «шума» ферритина (15 % раннего
    # значения, history.flat.pct): 34 -> 28 (−6 мкг/л при «шуме» 5,1) — событие.
    assert event(run([rec("a", "2026-01-10", {**hb, "ferritin": 34}),
                      rec("b", "2026-07-10", {**hb, "ferritin": 28})]), "ferritin_drop")["severity"] == "attention"
    # Небольшое снижение выше порогов — не событие.
    assert "ferritin_drop" not in codes(run([rec("a", "2026-01-10", {**hb, "ferritin": 60}),
                                             rec("b", "2026-07-10", {**hb, "ferritin": 50})]))
    # Пороги — из ferritin.deficiency_below (не продублированы в коде): поднимем порог практики до 50.
    cfg = cfg_with(ferritin__deficiency_below={"who": 15, "ru": 50})
    moved = run([rec("a", "2026-01-10", {**hb, "ferritin": 58}), rec("b", "2026-07-10", {**hb, "ferritin": 48})],
                cfg=cfg)
    assert "ниже порога 50 мкг/л" in event(moved, "ferritin_drop")["text"]
    # СРБ выше порога в раннем анализе — врачу пометка про воспаление (СРБ в обеих точках).
    infl = run([rec("a", "2026-01-10", {**hb, "ferritin": 60, "CRP": 12}),
                rec("b", "2026-07-10", {**hb, "ferritin": 28})])
    assert "СРБ 12 мг/л на 10.01.2026, не сдан на 10.07.2026" in event(infl, "ferritin_drop")["text"]
    # Пациенту — нейтрально.
    pat = event(run([rec("a", "2026-01-10", {**hb, "ferritin": 45}), rec("b", "2026-07-10", {**hb, "ferritin": 25})],
                    role="patient"), "ferritin_drop")
    assert pat["text"] == "Запас железа (ферритин) снижается — обсудите с врачом."
    # При беременности ферритин не оценивается.
    preg = run([rec("a", "2026-01-10", {**hb, "ferritin": 45}),
                rec("b", "2026-07-10", {**hb, "ferritin": 12}, pregnancy={"status": "yes", "trimester": 2})])
    assert "ferritin_drop" not in codes(preg)


def test_ferritin_crossing_needs_change_beyond_noise():
    """M4: переход порога практики без значимого изменения (31 -> 29, 32 -> 28) — колебание, а не событие."""
    hb = {"hemoglobin": 135}
    for a, b in ((31, 29), (32, 28)):
        out = run([rec("a", "2026-01-01", {**hb, "ferritin": a}), rec("b", "2026-06-01", {**hb, "ferritin": b})])
        assert "ferritin_drop" not in codes(out), (a, b)
        assert next(s for s in out["series"] if s["analyte"] == "ferritin")["direction"] == "flat"
    # Правило проекта выключается в norms_ru.yaml (history.ferritin_drop.crossing_beyond_flat) — тогда как раньше.
    loose = cfg_with(history__ferritin_drop={"min_drop_pct": 30, "window_months": 24, "crossing_beyond_flat": False})
    assert "ferritin_drop" in codes(run([rec("a", "2026-01-01", {**hb, "ferritin": 31}),
                                         rec("b", "2026-06-01", {**hb, "ferritin": 29})], cfg=loose))


def test_ferritin_drop_with_inflammation():
    """M5: СРБ выше порога в любой из двух точек — пациенту «на ферритин влияло воспаление», врачу СРБ в обеих;
    раннее значение при воспалении и сейчас ферритин не ниже порога практики — только «к сведению»."""
    hb = {"hemoglobin": 135}
    soft = "Ферритин изменился, но на него влияло воспаление — покажите историю врачу."
    early = [rec("a", "2026-01-01", {**hb, "ferritin": 200, "CRP": 60}),
             rec("b", "2026-06-01", {**hb, "ferritin": 60, "CRP": 2})]
    e = event(run(early), "ferritin_drop")
    assert e["severity"] == "info"
    assert "СРБ 60 мг/л на 01.01.2026, 2 мг/л на 01.06.2026 (воспаление — СРБ > 5 мг/л; ВОЗ 2020)" in e["text"]
    pat = event(run(early, role="patient"), "ferritin_drop")
    assert pat["title"] == "Ферритин изменился" and pat["text"] == soft and "снижается" not in pat["title"]
    late = [rec("a", "2026-01-01", {**hb, "ferritin": 80, "CRP": 2}),
            rec("b", "2026-06-01", {**hb, "ferritin": 50, "CRP": 40})]
    e = event(run(late), "ferritin_drop")
    assert e["severity"] == "info" and "СРБ 2 мг/л на 01.01.2026, 40 мг/л на 01.06.2026" in e["text"]
    assert event(run(late, role="patient"), "ferritin_drop")["text"] == soft
    # Без воспаления — прежний текст и без пометки СРБ.
    calm = [rec("a", "2026-01-01", {**hb, "ferritin": 80, "CRP": 2}),
            rec("b", "2026-06-01", {**hb, "ferritin": 50, "CRP": 3})]
    assert "СРБ" not in event(run(calm), "ferritin_drop")["text"]
    assert event(run(calm, role="patient"), "ferritin_drop")["title"] == "Запас железа снижается"
    # Сейчас ферритин ниже порога практики (и без воспаления) — «внимание» остаётся.
    low = [rec("a", "2026-01-01", {**hb, "ferritin": 90, "CRP": 30}),
           rec("b", "2026-06-01", {**hb, "ferritin": 20, "CRP": 1})]
    assert event(run(low), "ferritin_drop")["severity"] == "attention"


def test_mcv_drift():
    hb = {"hemoglobin": 130}
    down = run([rec("a", "2026-01-10", {**hb, "MCV": 88}), rec("b", "2026-07-10", {**hb, "MCV": 82})])
    e = event(down, "mcv_drift")
    assert e["severity"] == "info" and e["analytes"] == ["MCV"]
    assert "MCV снизился с 88 до 82 фл за 6 мес (−6 фл" in e["text"] and "к микроцитозу" in e["text"]
    assert event(run([rec("a", "2026-01-10", {**hb, "MCV": 88}), rec("b", "2026-07-10", {**hb, "MCV": 78})]),
                 "mcv_drift")["severity"] == "attention"                     # вышел за порог микроцитоза
    up = event(run([rec("a", "2026-01-10", {**hb, "MCV": 92}), rec("b", "2026-07-10", {**hb, "MCV": 98})]), "mcv_drift")
    assert "MCV вырос с 92 до 98 фл" in up["text"] and "к макроцитозу" in up["text"] and "(+6 фл" in up["text"]
    assert "mcv_drift" not in codes(run([rec("a", "2026-01-10", {**hb, "MCV": 88}),
                                         rec("b", "2026-07-10", {**hb, "MCV": 85})]))
    pat = run([rec("a", "2026-01-10", {**hb, "MCV": 88}), rec("b", "2026-07-10", {**hb, "MCV": 82})], role="patient")
    assert event(pat, "mcv_drift")["text"] == ("Средний размер эритроцитов за 6 месяцев заметно уменьшился — "
                                               "покажите историю врачу.")
    # Направление — по тренду: последнее против медианы предыдущих в окне, а не от самой дальней точки.
    # Раньше 80 -> 86 давало «вырос к макроцитозу», хотя три последних анализа ровные.
    flat = [rec("a", "2026-01-10", {**hb, "MCV": 80}), rec("b", "2026-03-10", {**hb, "MCV": 87}),
            rec("c", "2026-05-10", {**hb, "MCV": 86.5}), rec("d", "2026-07-10", {**hb, "MCV": 86})]
    assert "mcv_drift" not in codes(run(flat))
    trend = [rec("a", "2026-01-10", {**hb, "MCV": 90}), rec("b", "2026-03-10", {**hb, "MCV": 89}),
             rec("c", "2026-05-10", {**hb, "MCV": 88}), rec("d", "2026-07-10", {**hb, "MCV": 83})]
    e = event(run(trend), "mcv_drift")
    assert ("MCV снизился до 83 фл за 6 мес: медиана предыдущих анализов за ≤ 24 мес — 89 фл (n = 3, "
            "10.01.2026–10.05.2026; −6 фл): сдвиг к микроцитозу") in e["text"], e["text"]


def test_persistent_signal():
    hb = {"hemoglobin": 130}
    # Ферритин ниже порога практики, затем ОАК без ферритина (серию не рвёт), затем ниже порога ВОЗ — одна группа.
    # Порог практики РФ даёт сигнал только при наборе порогов «ru» (ревизия UX 05.10.2026).
    recs = [rec("a", "2026-03-01", {**hb, "ferritin": 25}, norms="ru"), rec("b", "2026-05-01", hb),
            rec("c", "2026-08-01", {**hb, "ferritin": 12})]
    who = [rec("a", "2026-03-01", {**hb, "ferritin": 25}), rec("c", "2026-08-01", {**hb, "ferritin": 12})]
    assert "persistent_signal" not in codes(run(who))             # при «who» ферритин 25 — не сигнал
    e = event(run(recs), "persistent_signal")
    assert e["severity"] == "attention" and e["analytes"] == ["ferritin"] and e["date"] == "2026-08-01"
    assert "в 2 анализах подряд (01.03.2026, 01.08.2026)" in e["text"] and "ферритин 12" in e["text"]
    pat = event(run(recs, role="patient"), "persistent_signal")
    assert pat["text"] == "Запас железа (ферритин) ниже обычного — так в 2 анализах подряд. Обсудите это с врачом."
    # Сигнал только в последнем анализе или прерван нормальным ферритином — события нет.
    assert "persistent_signal" not in codes(run([rec("a", "2026-03-01", {**hb, "ferritin": 60}),
                                                 rec("b", "2026-08-01", {**hb, "ferritin": 20}, norms="ru")]))
    assert "persistent_signal" not in codes(run([rec("a", "2026-01-01", {**hb, "ferritin": 20}, norms="ru"),
                                                 rec("b", "2026-04-01", {**hb, "ferritin": 60}, norms="ru"),
                                                 rec("c", "2026-08-01", {**hb, "ferritin": 20}, norms="ru")]))
    # Порог числа анализов — из конфигурации.
    assert "persistent_signal" not in codes(run(recs, cfg=cfg_with(history__persistent_signal={"min_records": 3})))
    # «Подряд» — только разные даты: два анализа одного дня (даже с разными значениями) — один раз.
    same_day = [rec("a", "2026-06-01", {**hb, "ferritin": 20}, norms="ru"),
                rec("b", "2026-06-01", {**hb, "ferritin": 22}, norms="ru")]
    assert "persistent_signal" not in codes(run(same_day))
    # Разрыв больше history.persistent_signal.max_gap_months (24 мес) — уже не «подряд»; в пределах срока — «подряд».
    assert "persistent_signal" not in codes(run([rec("a", "2023-01-01", {**hb, "ferritin": 20}, norms="ru"),
                                                 rec("b", "2026-06-01", {**hb, "ferritin": 22}, norms="ru")]))
    assert "persistent_signal" in codes(run([rec("a", "2025-01-01", {**hb, "ferritin": 20}, norms="ru"),
                                             rec("b", "2026-06-01", {**hb, "ferritin": 22}, norms="ru")]))


def test_screening_repeat_no_ferritin():
    hb = {"hemoglobin": 128}
    recs = [with_risk(rec("a", "2026-02-01", hb, cached=True), "elevated"),
            with_risk(rec("b", "2026-05-01", hb, cached=True), "usual"),
            with_risk(rec("c", "2026-09-01", hb, cached=True), "high")]
    # Готовые ответы не пересчитываются: «модели» здесь — объект, на котором движок упал бы.
    out = run(recs, models=object())
    e = event(out, "screening_repeat_no_ferritin")
    assert e["severity"] == "attention" and e["date"] == "2026-09-01" and e["analytes"] == ["ferritin"]
    assert "повышенный / высокий в 2 анализах (01.02.2026, 01.09.2026)" in e["text"]
    pat = event(run(recs, role="patient", models=object()), "screening_repeat_no_ferritin")
    assert pat["text"] == ("Уже в 2 анализах программа предлагает проверить запас железа, а анализ на ферритин ещё "
                           "не сдавали — обсудите его с врачом.")
    one = [with_risk(rec("a", "2026-02-01", hb, cached=True), "elevated"),
           with_risk(rec("b", "2026-09-01", hb, cached=True), "usual")]
    assert "screening_repeat_no_ferritin" not in codes(run(one, models=object()))
    with_ferritin = [rec("z", "2025-11-01", {**hb, "ferritin": 50}, cached=True)] + recs
    assert "screening_repeat_no_ferritin" not in codes(run(with_ferritin, models=object()))
    # Два анализа одного дня — одна дата, «повторяется» не набрано.
    same_day = [with_risk(rec("a", "2026-09-01", hb, cached=True), "elevated"),
                with_risk(rec("b", "2026-09-01", {"hemoglobin": 129}, cached=True), "high")]
    assert "screening_repeat_no_ferritin" not in codes(run(same_day, models=object()))


# ------------------------------------------------------------------ ряды
def test_series_order_fields_and_noise():
    recs = [rec("a", "2026-03-01", {"hemoglobin": 130, "MCV": 88, "ferritin": 40, "CRP": 2, "TSH": 2.0}),
            rec("b", "2026-09-01", {"hemoglobin": 133, "MCV": 91, "ferritin": 44, "CRP": 2.2, "TSH": 2.5})]
    out = run(recs)
    order = [s["analyte"] for s in out["series"]]
    assert order[:7] == ["hemoglobin", "ferritin", "MCV", "RDW", "MCH", "RBC", "hematocrit"]
    assert order.index("CRP") < order.index("MCHC") < order.index("TSH")      # приоритетные, затем analytes.yaml
    s = {x["analyte"]: x for x in out["series"]}
    hb = s["hemoglobin"]
    assert hb["name_ru"] == "Гемоглобин" and hb["unit"] == "г/л" and hb["last"] == 133.0
    assert hb["points"] == [{"date": "2026-03-01", "value": 130.0, "record_id": "a"},
                            {"date": "2026-09-01", "value": 133.0, "record_id": "b"}]
    assert hb["delta"] == 3.0 and hb["delta_pct"] == 2.3 and hb["months"] == 6.0
    assert hb["direction"] == "flat"                    # 3 г/л меньше «шума» 5 г/л (history.flat.abs)
    assert s["ferritin"]["direction"] == "flat"         # +10 % меньше 15 % (history.flat.pct)
    assert s["MCV"]["direction"] == "up"                # +3 фл больше 2 фл
    assert s["TSH"]["direction"] == "up"                # +25 % больше 5 % (history.flat.default_pct)
    assert hb["ref"] == {"low": 120.0, "high": 150.0}   # референс проекта для женщин
    assert s["CRP"]["ref"] == {"low": None, "high": 5.0}
    # «Шум» — из конфигурации.
    tight = cfg_with(history__flat={"default_pct": 5, "abs": {"hemoglobin": 2}})
    assert next(x for x in run(recs, cfg=tight)["series"] if x["analyte"] == "hemoglobin")["direction"] == "up"
    # Референс — для пола последней записи.
    male = run([rec("a", "2026-03-01", {"hemoglobin": 140}, sex="M"),
                rec("b", "2026-09-01", {"hemoglobin": 120}, sex="M")])
    assert male["series"][0]["ref"] == {"low": 130.0, "high": 170.0} and male["series"][0]["direction"] == "down"


def test_pregnancy_ref_null():
    recs = [rec("a", "2026-03-01", {"hemoglobin": 128}),
            rec("b", "2026-08-01", {"hemoglobin": 112}, pregnancy={"status": "yes", "trimester": 2})]
    out = run(recs)
    assert out["series"] and all(s["ref"] is None for s in out["series"])
    # Референс с бланка лаборатории при беременности остаётся (references.resolve).
    lab = run([recs[0], rec("b", "2026-08-01", {"hemoglobin": 112}, pregnancy={"status": "yes", "trimester": 2},
                            reference_ranges={"hemoglobin": {"low": 110, "high": 140}})])
    assert lab["series"][0]["ref"] == {"low": 110.0, "high": 140.0}


def test_sort_by_date_created_then_input_order():
    """M1: записи одного дня — по времени создания (created), затем по порядку во входе; случайный id порядка
    не задаёт. Одинаковые записи одного дня схлопываются."""
    recs = [rec("c", "2026-09-01", {"hemoglobin": 120}), rec("b", "2026-03-01", {"hemoglobin": 131}),
            rec("a", "2026-03-01", {"hemoglobin": 130})]
    out = run(recs)
    assert [p["record_id"] for p in out["series"][0]["points"]] == ["b", "a", "c"]       # created нет — порядок входа
    assert out["latest"]["record_id"] == "c" and out["first_date"] == "2026-03-01" and out["last_date"] == "2026-09-01"
    # created задан — он важнее порядка во входе и id.
    timed = [{**rec("ffff", "2026-06-01", {"hemoglobin": 118}), "created": "2026-06-01T12:00:00Z"},
             {**rec("0aaa", "2026-06-01", {"hemoglobin": 140}), "created": "2026-06-01T09:00:00Z"}]
    for records in (timed, timed[::-1]):
        out = run(records, role="patient")
        assert [p["value"] for p in out["series"][0]["points"]] == [140.0, 118.0]
        assert out["latest"]["record_id"] == "ffff" and "hb_drop" in codes(out)
    # Те же записи с другими (случайными) id — тот же ответ, кроме самих id.
    swapped = [{**timed[0], "id": "0aaa"}, {**timed[1], "id": "ffff"}]
    assert codes(run(swapped)) == codes(run(timed))
    # Дубль (та же дата и те же значения) — одна запись: остаётся более поздняя.
    dup = [rec("x1", "2026-06-01", {"hemoglobin": 128, "ferritin": 20}),
           rec("x2", "2026-06-01", {"hemoglobin": 128, "ferritin": 20})]
    out = run(dup)
    assert out["n_records"] == 1 and out["latest"]["record_id"] == "x2"
    assert out["events"] == [] and out["summary"].startswith("1 анализ (01.06.2026)")
    # Непонятный created не роняет разбор (запись встаёт раньше записей того же дня с известным временем).
    odd = [{**rec("p", "2026-06-01", {"hemoglobin": 130}), "created": "вчера"},
           {**rec("q", "2026-06-01", {"hemoglobin": 129}), "created": "2026-06-01T08:00:00Z"}]
    assert run(odd[::-1])["latest"]["record_id"] == "q"


def test_invalid_records_skipped_and_role_checked():
    good = rec("ok", "2026-09-01", {"hemoglobin": 130})
    bad_date = {**rec("x", "2026-13-40", {"hemoglobin": 130}), "date": "вчера"}
    bad_input = {"id": "y", "date": "2026-08-01", "input": {"sex": "F", "age_years": 10, "values": {"hemoglobin": 130}},
                 "result": None}
    out = run([good, bad_date, bad_input, "не запись"])
    assert out["n_records"] == 1 and out["latest"]["record_id"] == "ok"
    try:
        run([good], role="admin")
    except ValueError:
        pass
    else:
        raise AssertionError("ожидалась ошибка роли")


# ------------------------------------------------------------------ полнота картины
def test_completeness_order_statuses_prices():
    r3 = rec("r3", "2026-09-01", {"hemoglobin": 130}, cached=True)
    r3["result"]["next_tests"] = [{"analyte": a, "name_ru": a, "reason": "тест", "kind": "rule"}
                                  for a in ("folate", "TIBC", "CRP")]
    recs = [rec("r1", "2024-06-01", {"hemoglobin": 131, "TSH": 2.0, "creatinine": 70}, cached=True),
            rec("r2", "2026-02-01", {"hemoglobin": 129, "ferritin": 20, "CRP": 2}, cached=True), r3]
    out = run(recs, models=object())
    rows = out["completeness"]
    assert [r["analyte"] for r in rows] == ["folate", "serum_iron", "vitamin_B12", "ferritin", "creatinine", "TSH",
                                            "cbc", "CRP"]
    assert [r["priority"] for r in rows] == list(range(1, 9))
    st = {r["analyte"]: r for r in rows}
    assert [st[a]["status"] for a in ("folate", "serum_iron", "vitamin_B12")] == ["never"] * 3
    assert st["ferritin"]["status"] == "stale" and st["ferritin"]["last_date"] == "2026-02-01"   # низкий: срок 6 мес
    assert st["CRP"]["status"] == "ok" and st["cbc"]["status"] == "ok" and st["cbc"]["last_date"] == "2026-09-01"
    assert st["TSH"]["status"] == "stale" and st["creatinine"]["last_date"] == "2024-06-01"
    assert "20 мкг/л" in st["ferritin"]["reason"] and "6 мес" in st["ferritin"]["reason"]
    assert "Предложен разбором последнего анализа" in st["folate"]["reason"]
    assert st["serum_iron"]["name_ru"] == "Железо и насыщение трансферрина"
    # Цены: Москва из prices.yaml; строка железа — железо + ОЖСС.
    assert (st["ferritin"]["price_rub"], st["serum_iron"]["price_rub"], st["cbc"]["price_rub"]) == (845, 855, 820)
    # Ферритин не низкий — срок обычный (12 мес), статус ok.
    recs[1] = rec("r2", "2026-02-01", {"hemoglobin": 129, "ferritin": 60, "CRP": 2}, cached=True)
    assert next(r for r in run(recs, models=object())["completeness"] if r["analyte"] == "ferritin")["status"] == "ok"
    # Цены региона: своя цена; «в рознице недоступен» и нет цены в регионе — null.
    region = {"ferritin": 900, "TSH": {"price": 700, "available": False}, "serum_iron": 400, "TIBC": 500}
    st = {r["analyte"]: r for r in run(recs, models=object(), region_prices=region)["completeness"]}
    assert (st["ferritin"]["price_rub"], st["TSH"]["price_rub"], st["serum_iron"]["price_rub"],
            st["cbc"]["price_rub"]) == (900, None, 900, None)
    # Пациенту — причины понятным языком.
    pat = {r["analyte"]: r for r in run(recs, role="patient", models=object())["completeness"]}
    assert pat["ferritin"]["reason"].startswith("Чтобы оценить запас железа.")
    assert pat["cbc"]["reason"] == "Чтобы проверить гемоглобин и эритроциты. Последний раз сдавали в сентябре 2026."
    assert pat["TSH"]["reason"].endswith("Последний раз сдавали в июне 2024 — это было давно.")


# ------------------------------------------------------------------ роли и тексты
def _full_history(role: str) -> list[dict]:
    hb = {"hemoglobin": 130}
    recs = [rec("a", "2026-03-12", {"hemoglobin": 132, "MCV": 88, "ferritin": 45, "CRP": 12}),
            rec("b", "2026-06-10", {"hemoglobin": 125, "MCV": 85, "ferritin": 28}),
            rec("c", "2026-09-12", {"hemoglobin": 118, "MCV": 78, "ferritin": 12})]
    # ферритин 25 — сигнал по порогу практики РФ: только при наборе порогов «ru»
    persist = [rec("p1", "2026-03-01", {**hb, "ferritin": 25, "vitamin_B12": 250}, norms="ru"),
               rec("p2", "2026-08-01", {**hb, "ferritin": 12, "vitamin_B12": 240})]
    scr = [with_risk(rec("s1", "2025-02-01", hb, cached=True), "elevated"),
           with_risk(rec("s2", "2025-05-01", hb, cached=True), "high")]
    t2 = {"status": "yes", "trimester": 2}
    outs = [run(recs, role=role), run(persist, role=role), run(scr, role=role, models=object()),
            run([rec("o", "2024-01-15", {"hemoglobin": 112, "MCV": 104})], role=role),
            run([rec("g1", "2026-02-01", {"hemoglobin": 112}), rec("g2", "2026-08-01", {"hemoglobin": 126})],
                role=role),
            # смена контекста оценки Hb, снижение Hb при беременности, ферритин на фоне воспаления
            run([rec("k1", "2026-01-01", {"hemoglobin": 115}), rec("k2", "2026-06-01", {"hemoglobin": 112},
                                                                    pregnancy=t2)], role=role),
            run([rec("m1", "2026-01-01", {"hemoglobin": 128}), rec("m2", "2026-06-01", {"hemoglobin": 110},
                                                                    pregnancy=t2)], role=role),
            run([rec("f1", "2026-01-01", {**hb, "ferritin": 200, "CRP": 60}),
                 rec("f2", "2026-06-01", {**hb, "ferritin": 60, "CRP": 2})], role=role),
            # варианты hb_drop: к предыдущему анализу и ниже медианы окна при росте ряда за всю историю
            run([rec("u1", "2025-01-01", {"hemoglobin": 118}), rec("u2", "2026-03-01", {"hemoglobin": 140}),
                 rec("u3", "2026-09-01", {"hemoglobin": 128})], role=role),
            run([rec("w1", "2024-06-01", {"hemoglobin": 100}), rec("w2", "2025-11-01", {"hemoglobin": 142}),
                 rec("w3", "2026-02-01", {"hemoglobin": 140}), rec("w4", "2026-05-01", {"hemoglobin": 135}),
                 rec("w5", "2026-09-01", {"hemoglobin": 129})], role=role),
            # «срочно» в последнем анализе
            run([rec("v1", "2026-03-01", {"hemoglobin": 76}), rec("v2", "2026-09-01", {"hemoglobin": 75})], role=role)]
    return outs


def test_roles_patient_texts_safe():
    words = [w.lower().replace("ё", "е") for w in CFG.forbidden_patient_words]
    outs = _full_history("patient")
    seen = {e["code"] for o in outs for e in o["events"]}
    assert seen == set(history.EVENT_ORDER), seen               # каждое событие встретилось
    for out in outs:
        for line in patient_lines(out):
            low = line.lower().replace("ё", "е")
            assert line and line != NEUTRAL_PATIENT_LINE, ("сработал фильтр", line)
            assert filter_patient_line(line, CFG.forbidden_patient_words) == line, line
            assert not any(w in low for w in words), line
            assert "%" not in line and "процент" not in low, line
            assert "{" not in line and "}" not in line, line
        for e in out["events"]:
            text = (e["title"] + " " + e["text"]).lower()
            for banned in ("анеми", "дефицит", "диагноз", "вероятн", "риск", "микроцит", "макроцит", "воз", "мкг/л",
                           "г/л", "фл"):
                assert banned not in text, (banned, e)
    # Врачу — числа, даты и источник.
    doctor = _full_history("doctor")
    hb_drop = event(doctor[0], "hb_drop")["text"]
    assert "г/л" in hb_drop and "12.03.2026" in hb_drop and "решение команды проекта" in hb_drop
    assert any("%" in e["text"] for e in doctor[0]["events"])
    iron = next(e for e in doctor[1]["events"] if e["code"] == "persistent_signal" and e["analytes"] == ["ferritin"])
    assert "01.03.2026, 01.08.2026" in iron["text"] and "ВОЗ" in iron["text"]          # источник сигнала правила
    b12 = next(e for e in doctor[1]["events"] if e["code"] == "persistent_signal" and e["analytes"] == ["vitamin_B12"])
    assert "NICE NG239" in b12["text"]


def test_patient_templates_pass_forbidden_filter():
    """Все строки пациента в config/texts_ru/history.yaml проходят фильтр запрещённых слов (страховка шаблонов)."""
    data = yaml.safe_load((config_dir() / "texts_ru" / "history.yaml").read_text(encoding="utf-8"))

    def walk(node, path):
        if isinstance(node, dict):
            for k, v in node.items():
                yield from walk(v, path + (str(k),))
        elif isinstance(node, str):
            yield path, node

    patient = [(p, s) for p, s in walk(data, ()) if any("patient" in k for k in p)]
    assert len(patient) > 20
    for path, s in patient:
        assert filter_patient_line(s, CFG.forbidden_patient_words) == s, (path, s)
        assert "%" not in s, (path, s)


def test_summary_and_latest_by_role():
    recs = [rec("a", "2026-03-12", {"hemoglobin": 132}), rec("b", "2026-06-10", {"hemoglobin": 125}),
            rec("c", "2026-09-12", {"hemoglobin": 118})]
    pat, doc = run(recs, role="patient"), run(recs, role="doctor")
    assert pat["summary"] == ("3 анализа с марта по сентябрь 2026; есть изменения и отклонения в последнем анализе — "
                              "обсудите их с врачом")
    assert doc["summary"] == ("3 анализа, 12.03.2026–12.09.2026 (6 мес); требуют внимания: снижение гемоглобина, "
                              "появилась анемия по ВОЗ; в последнем анализе (12.09.2026): анемия по ВОЗ (Hb 118 г/л "
                              "при пороге 120 г/л, лёгкая)")
    assert pat["latest"]["record_id"] == doc["latest"]["record_id"] == "c" and pat["latest"]["date"] == "2026-09-12"
    assert pat["latest"]["headline"] != doc["latest"]["headline"]
    drop_only = [rec("a", "2026-03-12", {"hemoglobin": 140}), rec("b", "2026-09-12", {"hemoglobin": 125})]
    assert run(drop_only, role="patient")["summary"] == (
        "2 анализа с марта по сентябрь 2026; есть изменения, которые стоит обсудить с врачом")
    calm = run([rec("a", "2025-03-01", {"hemoglobin": 130}), rec("b", "2026-09-01", {"hemoglobin": 131})],
               role="patient")
    assert calm["summary"] == "2 анализа с марта 2025 по сентябрь 2026; изменений, требующих внимания, нет"


def test_summary_reflects_latest_record():
    """H5: шапка не успокаивает, если последний анализ «срочный», с анемией или с сигналами скрытого дефицита —
    даже когда событий динамики нет (стойкая анемия, стойко низкие тромбоциты)."""
    calm_tail = "изменений, требующих внимания, нет"
    urgent_prefix = CFG.texts["patient"]["headline"]["urgent_prefix"].strip()      # те же слова, что в отчёте анализа
    assert urgent_prefix == "Важно: обратитесь к врачу сегодня."

    def both(recs):
        return run(recs, role="patient"), run(recs, role="doctor")

    # Стойкая умеренная анемия: 96 -> 95 г/л, событий нет.
    pat, doc = both([rec("a", "2026-03-01", {"hemoglobin": 96}, age=40),
                     rec("b", "2026-09-01", {"hemoglobin": 95}, age=40)])
    assert pat["events"] == [] and calm_tail not in pat["summary"]
    assert pat["summary"] == "2 анализа с марта по сентябрь 2026; в последнем анализе есть отклонения — обсудите с врачом"
    assert doc["summary"] == ("2 анализа, 01.03.2026–01.09.2026 (6 мес); в последнем анализе (01.09.2026): анемия по "
                              "ВОЗ (Hb 95 г/л при пороге 120 г/л, умеренная)")
    # Стойкая тяжёлая анемия: 76 -> 75 г/л — «срочно» первым.
    pat, doc = both([rec("a", "2026-03-01", {"hemoglobin": 76}, age=40),
                     rec("b", "2026-09-01", {"hemoglobin": 75}, age=40)])
    assert pat["latest"]["headline"].startswith(urgent_prefix)
    assert pat["summary"] == (f"{urgent_prefix} 2 анализа с марта по сентябрь 2026; последний анализ требует срочной "
                              "очной оценки")
    assert doc["summary"].startswith("Срочно: в последнем анализе (01.09.2026) гемоглобин 75 г/л (< 80 г/л) — нужна "
                                     "срочная очная оценка. 2 анализа, 01.03.2026–01.09.2026 (6 мес); ")
    assert "анемия по ВОЗ (Hb 75 г/л при пороге 120 г/л, тяжёлая)" in doc["summary"]
    # Тромбоциты 30 -> 28 без анемии — тоже «срочно».
    pat, doc = both([rec("a", "2026-03-01", {"hemoglobin": 135, "platelets": 30}),
                     rec("b", "2026-09-01", {"hemoglobin": 135, "platelets": 28})])
    assert pat["summary"].startswith(urgent_prefix) and calm_tail not in pat["summary"]
    assert "тромбоциты 28 ×10⁹/л (< 50 ×10⁹/л)" in doc["summary"] and "событий" not in doc["summary"]
    # Одна запись с анемией.
    one = run([rec("a", "2026-09-01", {"hemoglobin": 110})], role="patient")
    assert one["summary"] == "1 анализ, сентябрь 2026; в анализе есть отклонения — обсудите с врачом"
    # Сигнал скрытого дефицита в последнем анализе без событий динамики.
    hidden = both([rec("a", "2026-03-01", {"hemoglobin": 130}), rec("b", "2026-09-01", {"hemoglobin": 131,
                                                                                       "ferritin": 12})])
    assert hidden[0]["summary"].endswith("в последнем анализе есть отклонения — обсудите с врачом")
    assert hidden[1]["summary"].endswith("в последнем анализе (01.09.2026): сигналы скрытого дефицита")
    # Отклонения были раньше, а последний анализ спокойный и событий нет — «спокойный» хвост.
    healed = run([rec("a", "2025-03-01", {"hemoglobin": 128, "ferritin": 12}),
                  rec("b", "2026-09-01", {"hemoglobin": 131, "ferritin": 60})], role="patient")
    assert healed["summary"].endswith(calm_tail), healed["summary"]


def test_json_serializable():
    for role in history.ROLES:
        for out in _full_history(role):
            text = json.dumps(out, ensure_ascii=False)
            assert json.loads(text) == out


def test_without_texts_and_history_config():
    """Без config/texts_ru/history.yaml и без раздела history ответ всё равно собирается (без чисел по умолчанию)."""
    recs = [rec("a", "2024-03-12", {"hemoglobin": 132, "ferritin": 45}),
            rec("b", "2024-09-12", {"hemoglobin": 115, "ferritin": 20})]
    bare = dataclasses.replace(CFG, texts={})
    pat = run(recs, role="patient", cfg=bare)
    assert pat["events"] and all(e["text"] == NEUTRAL_PATIENT_LINE for e in pat["events"])
    assert all(e["text"] for e in run(recs, cfg=bare)["events"])
    norms = {k: v for k, v in CFG.norms.items() if k != "history"}
    no_hist = run(recs, cfg=dataclasses.replace(CFG, norms=norms))
    assert codes(no_hist) == ["anemia_new"]                         # пороги динамики не заданы — их событий нет
    assert {c["status"] for c in no_hist["completeness"]} <= {"never", "ok"}


def test_robust_to_bad_cached_results():
    """NaN / inf в кешированной таблице значений отбрасываются; беременность строкой при готовом ответе не роняет."""
    r1 = rec("a", "2026-01-01", {"hemoglobin": 130}, cached=True)
    r1["result"]["values"] += [{"analyte": "ferritin", "value": float("nan")},
                               {"analyte": "TSH", "value": float("inf")}, {"analyte": "CRP", "value": "abc"}]
    r2 = rec("b", "2026-02-01", {"hemoglobin": 128, "ferritin": 20})
    for role in history.ROLES:
        out = run([r1, r2], role=role)
        json.dumps(out, allow_nan=False)
        fer = next(s for s in out["series"] if s["analyte"] == "ferritin")
        assert [p["record_id"] for p in fer["points"]] == ["b"]
        assert not any(s["analyte"] == "TSH" for s in out["series"])
    preg = rec("p", "2026-03-01", {"hemoglobin": 112}, cached=True, pregnancy={"status": "yes", "trimester": 2})
    preg["input"]["pregnancy"] = "yes"                     # старая или ручная запись: строка вместо объекта
    out = run([rec("o", "2026-01-01", {"hemoglobin": 128}), preg])
    assert out["n_records"] == 2 and all(s["ref"] is None for s in out["series"])     # беременность распознана


def test_text_cosmetics():
    """Шапка врача без двойного двоеточия; промежутки «за 1 день» / «в тот же день», а не «за 1 дн.»."""
    hb = {"hemoglobin": 130}
    persist = run([rec("a", "2026-03-01", {**hb, "ferritin": 25}, norms="ru"),
                   rec("b", "2026-08-01", {**hb, "ferritin": 12})])
    assert "повторяется сигнал «ферритин ниже порога»" in persist["summary"]
    assert "повторяется:" not in persist["summary"] and persist["summary"].count(":") == 2   # «внимания:», «(дата):»
    next_day = run([rec("a", "2026-09-01", {"hemoglobin": 135}), rec("b", "2026-09-02", {"hemoglobin": 118})])
    assert "за 1 день" in event(next_day, "hb_drop")["text"] and "дн." not in event(next_day, "hb_drop")["text"]
    same_day = run([rec("a", "2026-09-01", {"hemoglobin": 135}), rec("b", "2026-09-01", {"hemoglobin": 118})])
    assert "в тот же день" in event(same_day, "hb_drop")["text"] and "за один день" not in event(same_day, "hb_drop")["text"]
    assert "01.09.2026–01.09.2026" not in same_day["summary"]
    assert same_day["summary"].startswith("2 анализа, 01.09.2026 (в один день);")


def test_limits_points_and_records():
    """Большая история: в ряду — не больше history.limits.max_points последних точек; записей в разборе — не больше
    history.limits.max_records последних; 500 записей с готовыми ответами — быстро."""
    template = rec("t", "2020-01-01", {"hemoglobin": 130, "ferritin": 50}, cached=True)["result"]
    recs = []
    for i in range(500):
        res = copy.deepcopy(template)
        for row in res["values"]:
            if row["analyte"] == "hemoglobin":
                row["value"] = 125 + i % 9
        day = date.fromordinal(date(2010, 1, 1).toordinal() + 7 * i).isoformat()
        recs.append({"id": f"r{i:03d}", "date": day, "result": res,
                     "input": {"sex": "F", "age_years": 34, "values": {"hemoglobin": 130}}})
    t0 = time.perf_counter()
    out = run(recs, models=object())
    elapsed = time.perf_counter() - t0
    assert out["n_records"] == 500 and elapsed < 3.0, elapsed
    hb = out["series"][0]
    assert hb["analyte"] == "hemoglobin" and len(hb["points"]) == 100
    assert hb["points"][-1]["record_id"] == "r499" and hb["points"][0]["record_id"] == "r400"
    json.dumps(out, allow_nan=False)
    small = cfg_with(history__limits={"max_points": 10, "max_records": 50})
    out = run(recs, cfg=small, models=object())
    assert out["n_records"] == 50 and out["first_date"] == recs[450]["date"] and len(out["series"][0]["points"]) == 10


# ------------------------------------------------------------------ производительность
def test_performance_50_records():
    models = load_models()                                         # загрузка моделей — вне замера
    recs = []
    for i in range(50):
        day = date(2024, 1, 1).toordinal() + 14 * i
        recs.append(rec(f"r{i:02d}", date.fromordinal(day).isoformat(),
                        {"hemoglobin": 135 - (i % 7) * 2, "MCV": 90 - (i % 5), "ferritin": 60 - i % 40}))
    t0 = time.perf_counter()
    out = history.analyze(recs, cfg=CFG, models=models, role="doctor", today=TODAY)
    elapsed = time.perf_counter() - t0
    assert out["n_records"] == 50 and out["series"]
    assert elapsed < 3.0, f"50 записей за {elapsed:.2f} с"
