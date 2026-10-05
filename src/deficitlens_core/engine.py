"""Движок DeficitLens: один анализ -> полный ответ.

Конвейер:
  1. нормализация ввода (единицы, допустимые диапазоны, расчётные НТЖ и рСКФ)      — normalize.py
  2. уровень 1: анемия по правилу ВОЗ 2024 (модель этот вывод не меняет)             — level1.py
  3. правила-подсказки под пять «болей» кейса (только флаги, класс не меняют)        — rules.py
  4. уровень 2: 14 профилей -> 12 классов -> причина -> 5 групп кейса               — ml/case_model.py
       клинический режим: слияние без утечки (несданный анализ не несёт информации);
       режим бенчмарка: ансамбль «бустинг × слияние» для файлов в схеме кейса
  5. скрытый дефицит при нормальном гемоглобине: правила, модель кейса, скрининг по ОАК (NHANES)
  6. следующий анализ с ценой                                                         — next_test.py
  7. таблица значений и отчёты врачу и пациенту                                       — values_table.py, reports/

Сервис ничего не хранит. Исследовательский прототип, не медицинское изделие.
"""
from __future__ import annotations

import sys
import time
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Optional

import numpy as np

from . import constants as C
from .config import Config, load_config
from .next_test import rank_next_tests
from .schemas import (AnalysisInput, AnalysisResult, ClassProb, Contribution, Explanation, Flag, HiddenDeficiency, Level2, ReferencesInfo, Screening, VersionInfo)

MAX_EXPLANATION_ITEMS = 4
CBC_UNDETERMINED = "по одному общему анализу крови не определяется"
LOW_UNDETERMINED = "по этим данным не определяется"
# Классы уровня 2 с дефицитом нутриента (у класса есть профиль с непустым списком нутриентов, constants.py).
DEFICIENCY_CLASSES = frozenset(C.PROFILE_TO_CLASS[p] for p in C.PROFILES if C.PROFILE_NUTRIENTS[p])
CONFIDENT = ("moderate", "high")


@dataclass
class Models:
    case: Any = None     # ml.case_model.CaseModel
    screen: Any = None   # ml.screen.ScreenModel


def _lower_first(text: str) -> str:
    """«Почки» -> «почки»; сокращения («B12 требует…», «MCV») не трогаем."""
    return text[:1].lower() + text[1:] if len(text) >= 2 and text[0].isupper() and text[1].islower() else text


@lru_cache(maxsize=1)
def load_models() -> Models:
    """Загружает модели один раз на процесс. Если артефактов нет — движок работает на правилах."""
    m = Models()
    try:
        from .ml.case_model import CaseModel
        m.case = CaseModel.load()
    except Exception as e:  # noqa: BLE001 — без модели сервис должен отвечать правилами
        print(f"[deficitlens] модель по кейсу не загружена: {type(e).__name__}: {e}", file=sys.stderr)
    try:
        from .ml.screen import ScreenModel
        m.screen = ScreenModel.load()
    except Exception as e:  # noqa: BLE001
        print(f"[deficitlens] модель скрининга не загружена: {type(e).__name__}: {e}", file=sys.stderr)
    return m


# --------------------------------------------------------------------------------------------
# Уровень 2
# --------------------------------------------------------------------------------------------
def _level2(P14: np.ndarray, x_row: np.ndarray, level1, norm, case_model, cfg: Config, mode: str,
            cap_reasons: Optional[list[str]] = None, cap_low_reasons: Optional[list[str]] = None):
    """Свёртки вероятностей 14 профилей и словесная уверенность. Возвращает (Level2, Explanation, вклад по анализам).

    cap_reasons — названия сработавших правил-масок и пунктов чек-листа «под подозрением»: при них словесная
    уверенность не выше «средней» (модель обучена на файле кейса, а правило видит, что показатель обманывает).
    """
    from .ml.features import completeness

    cm = cfg.class_mapping
    P12 = P14 @ C.A12
    P11 = P14 @ C.A11
    order = np.argsort(-P12)
    top = int(order[0])
    cls = C.CLASSES12[top]
    # Причина = причина самого вероятного профиля ВНУТРИ самого вероятного класса: класс и причина не противоречат.
    in_class = [i for i, p in enumerate(C.PROFILES) if C.PROFILE_TO_CLASS[p] == cls]
    top_prof = max(in_class, key=lambda i: P14[i])
    cause = C.PROFILE_TO_CAUSE[C.PROFILES[top_prof]]

    to_group = cm["class_to_group5_if_anemia"]
    anemia = bool(level1.anemia)
    group = to_group.get(cls, "unexplained_anemia") if anemia else "healthy"
    g5 = {g: 0.0 for g in C.GROUPS5}
    for i, c in enumerate(C.CLASSES12):
        g5[to_group.get(c, "unexplained_anemia") if anemia else "healthy"] += float(P12[i])

    # Правдоподобные альтернативы: классы по убыванию вероятности до суммы cumulative, не больше max_items.
    pl_cfg = cm["plausible"]
    plausible, cum = [], 0.0
    for i in order:
        p = float(P12[i])
        if plausible and p < 0.03:
            break
        plausible.append(ClassProb(code=C.CLASSES12[i], name_ru=cm["classes12"][C.CLASSES12[i]], p=round(p, 3)))
        cum += p
        if cum >= pl_cfg["cumulative"] or len(plausible) >= pl_cfg["max_items"]:
            break
    p_top = float(P12[top])
    ambiguous = len(plausible) > 1 and p_top < pl_cfg["ambiguous_if_top_below"]

    # Словесная уверенность. «Высокая» — только если сдан ключевой анализ для этого класса.
    available = set(norm.values)
    key_ok = all(any(a in available for a in alternatives) for alternatives in cm["key_tests"].get(cls, []))
    conf_cfg = cm["confidence"]
    if p_top >= conf_cfg["high_from"] and key_ok:
        conf = "high"
    elif p_top >= conf_cfg["moderate_from"]:
        conf = "moderate"
    else:
        conf = "low"
    capped = bool(cap_reasons) and conf == "high"
    if capped:
        conf = "moderate"
    # Ответ врача 04.10.2026 (п. 11, вопрос 3.3): подозрение на гемолиз при недостающем ключевом показателе или при
    # оценке по референсу проекта вместо лабораторного — уверенность не выше «низкой».
    capped_low = bool(cap_low_reasons) and conf != "low"
    if capped_low:
        conf = "low"

    level = completeness(x_row)
    # Анемии нет и сдан только ОАК: класс по файлу кейса здесь не определяется — название не показываем.
    cls_ru, cause_ru = cm["classes12"][cls], cm["causes11"][cause]
    cbc_only_no_anemia = not anemia and level == "cbc"
    if cbc_only_no_anemia:
        # Правка 4: по одному ОАК без анемии класс и его варианты — это доли классов учебного набора, а не свойство
        # человека; о скрытом дефиците железа здесь говорит только скрининг. Варианты и вероятности классов не отдаём.
        cls_ru = cause_ru = CBC_UNDETERMINED
        plausible, ambiguous = [], False
    elif anemia and level == "cbc":
        # Ревизия UX 05.10.2026: при анемии и одном ОАК причина («сочетание: железо и фолаты» при несданных фолатах)
        # спорила с заголовком «причина не определяется». Класс остаётся (вероятный вариант — в списке вариантов),
        # а словесная причина — как без анемии. Код причины (deficiency_cause) не меняется: он нужен бенчмарку.
        cause_ru = CBC_UNDETERMINED
    elif anemia and conf == "low":
        # Ревизия содержания 05.10.2026, п. 6: при низкой уверенности причина словами не называется.
        cause_ru = LOW_UNDETERMINED
    level2 = Level2(
        enabled=True, mode=mode, completeness=level, completeness_ru=cm["completeness_ru"][level],
        case_group=group, case_group_ru=cm["groups5"][group],
        anemia_class=cls, anemia_class_ru=cls_ru,
        deficiency_cause=cause, deficiency_cause_ru=cause_ru,
        probabilities=({"groups5": {g: round(v, 4) for g, v in g5.items()}} if cbc_only_no_anemia else {
            "classes12": {c: round(float(P12[i]), 4) for i, c in enumerate(C.CLASSES12)},
            "groups5": {g: round(v, 4) for g, v in g5.items()},
            "causes11": {c: round(float(P11[i]), 4) for i, c in enumerate(C.CAUSES11)},
        }),
        plausible=plausible, ambiguous=ambiguous, confidence=conf, confidence_ru=conf_cfg["ru"][conf],
    )

    # Объяснение «за и против»: вклад каждого сданного анализа вне ОАК в выбор лучшего класса против второго.
    explanation = Explanation(top_class=cls)
    contrib_text: dict[str, str] = {}
    outside = [i for i in range(C.NP_) if i not in in_class]
    if cbc_only_no_anemia:
        outside = []  # сравнение двух классов по одному ОАК без анемии не показываем (правка 4)
        explanation.note = "По одному общему анализу крови без анемии класс не определяется; см. скрининг."
    if outside:
        runner_prof = max(outside, key=lambda i: P14[i])
        runner_cls = C.PROFILE_TO_CLASS[C.PROFILES[runner_prof]]
        explanation.runner_up = runner_cls
        top_ru, runner_ru = cm["classes12"][cls], cm["classes12"][runner_cls]
        # Ревизия содержания 05.10.2026, п. 5: «против» — только если альтернатива правдоподобна (вероятность класса
        # не меньше class_mapping.yaml → explanation.against_min_runner_p). Иначе «ферритин 62 — скорее в пользу
        # «дефицит витамина B6»» при вероятности B6 меньше 1 % вводит врача в заблуждение.
        against_min = float((cm.get("explanation") or {}).get("against_min_runner_p", 0.10))
        show_against = float(P12[C.CLASSES12.index(runner_cls)]) >= against_min
        try:
            contribs = case_model.contributions(x_row, top_prof, runner_prof)
        except Exception:  # noqa: BLE001 — объяснение не должно ломать ответ
            contribs = []
        # Самые весомые — первыми: таблица значений показывает «вклад» у первых по весу и у значений вне нормы.
        for item in sorted(contribs, key=lambda it: -abs(float(it["delta"]))):
            a, d = item["analyte"], float(item["delta"])
            if a not in norm.values or abs(d) < 0.05 or (d < 0 and not show_against):
                continue
            direction = "for" if d > 0 else "against"
            text = (f"поддерживает вывод «{top_ru}»" if d > 0
                    else f"говорит скорее в пользу варианта «{runner_ru}»")
            # Таблица значений (ревизия UX 05.10.2026): «в модели сдвигает к «…»» — без слов «за» и «против».
            contrib_text[a] = f"в модели сдвигает к «{top_ru if d > 0 else runner_ru}»"
            c = Contribution(analyte=a, name_ru=cfg.short_ru(a), value=round(float(norm.values[a]), 3),
                             unit=cfg.unit_ru(a), direction=direction, weight=round(abs(d), 3), text=text)
            target = explanation.items_for if d > 0 else explanation.items_against
            if len(target) < MAX_EXPLANATION_ITEMS:
                target.append(c)
        if not contribs and not explanation.note:
            explanation.note = "Вывод сделан по общему анализу крови: анализы вне ОАК не сданы."
    if capped_low:
        why = "Уверенность понижена до низкой: " + "; ".join(cap_low_reasons[:3]) + "."
        explanation.note = f"{explanation.note} {why}" if explanation.note else why
    elif capped:
        why = "Уверенность понижена до средней: " + "; ".join(cap_reasons[:3]) + "."
        explanation.note = f"{explanation.note} {why}" if explanation.note else why
    return level2, explanation, contrib_text


# --------------------------------------------------------------------------------------------
# Скрытый дефицит
# --------------------------------------------------------------------------------------------
def _per_100(p: float) -> str:
    n = int(round(100 * p))
    return "менее чем у 1" if n < 1 else f"примерно у {n}"


def _screening(norm, level1, models: Models, cfg: Config, inp: AnalysisInput) -> Screening:
    """Скрининг скрытого дефицита железа по ОАК: только взрослые без анемии, не беременные, ферритин не сдан."""
    if level1.anemia:
        return Screening(applicable=False, reason_not_applicable=(
            "При анемии ферритин и показатели обмена железа назначаются по клиническим рекомендациям; скрининг не нужен."))
    if norm.pregnancy_status == "yes":
        return Screening(applicable=False, reason_not_applicable="При беременности модель не применяется.")
    if "ferritin" in norm.measured:
        return Screening(applicable=False, reason_not_applicable="Ферритин сдан: запас железа оценивается по измеренному значению.")
    if models.screen is None:
        return Screening(applicable=False, reason_not_applicable="Модель скрининга не загружена.")

    refs = cfg.lab_reference.get("references", {})
    ref_name = inp.lab_reference if inp.lab_reference in refs else "default"
    # Точка направления на ферритин: из запроса (переключатель 20 / 30 / 40 %) или из config/screen_thresholds.yaml.
    share = float(inp.refer_share) if inp.refer_share is not None else float(cfg.screen.get("refer_share", 0.30))
    try:
        r = models.screen.predict(norm.values, norm.sex, norm.age_years, reference=refs.get(ref_name), refer_share=share)
    except TypeError:
        r = models.screen.predict(norm.values, norm.sex, norm.age_years, reference=refs.get(ref_name))
    p15, p30 = float(r["p_lt15"]), float(r["p_lt30"])
    thr = dict(r["thresholds"])
    # Порог вероятности берём по проверенной группе (config/screen_thresholds.yaml → threshold_group) и применяем
    # ко всем: у мужчин и женщин 50+ «верхние 30 % своей группы» — это вероятность ниже 1 %, направлять по ней нельзя.
    ref_group = cfg.screen.get("threshold_group")
    if ref_group and ref_group != r.get("group"):
        try:
            high_share = min(float(cfg.screen.get("high_share", 0.10)), share)
            thr = {"refer": models.screen.threshold(r["features"], ref_group, share),
                   "high": models.screen.threshold(r["features"], ref_group, high_share)}
        except Exception:  # noqa: BLE001 — нет порогов группы: остаются пороги собственной группы
            pass
    risk = "high" if p15 >= thr["high"] else ("elevated" if p15 >= thr["refer"] else "usual")
    validated = r["group"] in cfg.screen.get("validated_groups", [])
    text = (f"Ферритин ниже 15 мкг/л находят {_per_100(p15)} из 100 людей с похожим общим анализом крови, "
            f"ниже 30 мкг/л — {_per_100(p30)} из 100. Это оценка по модели, а не измерение; "
            "модель проверена на данных США (NHANES).")
    if r.get("features") == "hb_only":
        text += " Оценка упрощённая: использованы только гемоглобин, возраст и пол."
    if not validated:
        text += (" Модель проверена на женщинах 18–49 лет; в этой группе случаев мало, преимущество перед простым "
                 "правилом по индексам не показано, порог направления взят по проверенной группе.")
    return Screening(
        applicable=True, model=getattr(models.screen, "version", "screen-v1"),
        p_ferritin_lt15=round(p15, 3), p_ferritin_lt30=round(p30, 3),
        risk=risk, risk_ru=cfg.screen["risk_ru"][risk], explanation_ru=text,
        features=r.get("features"), group=r.get("group"), validated_group=validated,
        lab_reference=ref_name, recommend_ferritin=risk in ("elevated", "high"),
        refer_share=round(float(r.get("refer_share", share)), 3),
    )


def _hidden(norm, level1, rules, P14: Optional[np.ndarray], level2: Level2, models: Models, cfg: Config,
            inp: AnalysisInput) -> tuple[HiddenDeficiency, bool]:
    """Скрытый дефицит при нормальном гемоглобине. Возвращает (HiddenDeficiency, сработала ли модель кейса)."""
    if norm.pregnancy_status == "yes":
        # Ревизия UX 05.10.2026: при беременности скрытый дефицит не оценивается при любом гемоглобине
        # (только уровень 1), а не «здесь действует основной вывод».
        return HiddenDeficiency(applicable=False, summary_ru=(
            "При беременности скрытый дефицит не оценивается (только уровень 1).")), False
    if level1.anemia:
        return HiddenDeficiency(applicable=False, summary_ru=(
            "Скрытый дефицит оценивается при нормальном гемоглобине; здесь действует основной вывод.")), False

    p_any, by = None, {}
    model_detected = False
    # Модель кейса участвует только когда сданы анализы вне ОАК. По одному ОАК её «вероятность дефицита» — это
    # просто доля классов с дефицитом в файле кейса (0,77), а не свойство человека: такое число не показываем,
    # по одному ОАК решает скрининг, проверенный на реальных людях.
    if P14 is not None and models.case is not None and level2.completeness and level2.completeness != "cbc":
        p_any = float(1.0 - P14[C.P_IDX["no_anemia_no_deficiency"]])
        for nut in C.NUTRIENTS:
            by[nut] = round(float(sum(P14[i] for i, p in enumerate(C.PROFILES) if nut in C.PROFILE_NUTRIENTS[p])), 3)
        try:
            tau = models.case.hidden_threshold(level2.completeness, mode=level2.mode or "clinical")
        except TypeError:       # модель без параметра mode
            tau = models.case.hidden_threshold(level2.completeness)
        except Exception:  # noqa: BLE001
            tau = None
        model_detected = tau is not None and p_any >= float(tau)

    screening = _screening(norm, level1, models, cfg, inp)
    signals = list(rules.rule_signals)
    detected = bool(signals) or screening.recommend_ferritin or model_detected

    if signals:
        summary = "Гемоглобин в норме, но есть признаки скрытого дефицита: " + "; ".join(s.text for s in signals[:2])
    elif screening.recommend_ferritin:
        summary = ("Гемоглобин в норме, но по общему анализу крови риск скрытого дефицита железа "
                   f"{screening.risk_ru}: стоит досдать ферритин.")
    elif model_detected:
        nut = max(by, key=by.get) if by else None
        nut_ru = cfg.class_mapping["nutrients"].get(nut, "") if nut else ""
        summary = (f"Гемоглобин в норме, но сочетание показателей повышает вероятность скрытого дефицита"
                   f"{' (' + nut_ru + ')' if nut_ru else ''} — оценка модели по данным кейса.")
    else:
        summary = "Признаков скрытого дефицита по этим данным не видно."
    return HiddenDeficiency(applicable=True, detected=detected, p_any=None if p_any is None else round(p_any, 3),
                            by_nutrient=by, rule_signals=signals, screening=screening, summary_ru=summary), model_detected


# --------------------------------------------------------------------------------------------
# Пограничный B12 без числового порога
# --------------------------------------------------------------------------------------------
B12_FUNCTIONAL = ("active_B12", "MMA", "homocysteine")


def _b12_flag(norm, P14: Optional[np.ndarray], existing: list, cfg: Config) -> tuple[Optional[Flag], list[dict]]:
    """Флаг «B12 требует функционального подтверждения», когда числового порога B12 нет.

    По решению капитана 04.10.2026 (medic_decisions.md, п. 10) общий B12 оценивается числом по NICE NG239
    (norms_ru.yaml → policy.foreign_allowed): серую зону 180–350 пг/мл помечает rules.py, и эта функция ничего
    не добавляет. Если число выключено (запись B12 убрана из foreign_allowed или порог не задан), «пограничный B12»
    из условия кейса определяется без порога: общий B12 сдан, ни один функциональный маркер (активный B12, ММК,
    гомоцистеин) не сдан, а модель по данным кейса не уверена — вероятность участия B12 лежит в полосе
    config/class_mapping.yaml → model_flags.b12_uncertain.
    """
    from .rules import b12_numeric

    if P14 is None or b12_numeric(cfg):
        return None, []
    if any(f.code == "b12_gray_zone" for f in existing):
        return None, []
    if "vitamin_B12" not in norm.measured or any(a in norm.measured for a in B12_FUNCTIONAL):
        return None, []
    lo, hi = cfg.class_mapping.get("model_flags", {}).get("b12_uncertain", [0.15, 0.85])
    p_b12 = float(sum(P14[i] for i, p in enumerate(C.PROFILES) if "b12" in C.PROFILE_NUTRIENTS[p]))
    if not (lo <= p_b12 <= hi):
        return None, []
    value = norm.values["vitamin_B12"]
    src = cfg.source_title("kr_b12_2024")
    flag = Flag(
        code="b12_gray_zone", title="B12 требует функционального подтверждения",
        text=(f"Общий витамин B12 {value:g} {cfg.unit_ru('vitamin_B12')} сам по себе дефицит не подтверждает и не "
              "исключает: модель по данным кейса не уверена. Нужен функциональный маркер — активный B12 "
              "(голотранскобаламин) или гомоцистеин; метилмалоновая кислота в рознице Москвы отдельным анализом "
              "недоступна. Числового порога B12 в клинических рекомендациях РФ нет — оценивать по референсу лаборатории."),
        source=f"условие кейса; {src}", severity="attention")
    reason = "Функциональный маркер: общий B12 сам по себе дефицит не подтверждает и не исключает."
    tests = [{"analyte": a, "reason": reason, "source": "условие кейса"} for a in ("active_B12", "homocysteine")]
    return flag, tests


# --------------------------------------------------------------------------------------------
# Строки к классу для врача (анемия воспаления)
# --------------------------------------------------------------------------------------------
CLASS_NOTES_DEFAULT = {
    "iron_on_inflammation": "Возможна железодефицитная анемия на фоне воспаления.",
    "group_inflammation": "В пяти группах кейса анемия воспаления относится к группе «анемия неясного генеза».",
}


def _class_notes(level2: Level2, level1, norm, cfg: Config) -> None:
    """Решение капитана 04.10.2026 (medic_decisions.md, п. 1). Анемия воспаления в пяти группах кейса — «анемия
    неясного генеза», но врачу называется класс «анемия воспаления»; группа — с пояснением. При СРБ выше порога и
    признаках дефицита железа (rules.iron_deficiency_on_inflammation) — строка «возможна железодефицитная анемия
    на фоне воспаления». Тексты — config/texts_ru/doctor.yaml → level2_notes."""
    from .rules import iron_deficiency_on_inflammation

    if not level1.anemia:
        return
    texts = ((cfg.texts or {}).get("doctor") or {}).get("level2_notes") or {}

    def text(key: str) -> str:
        return str(texts.get(key) or CLASS_NOTES_DEFAULT[key])

    if iron_deficiency_on_inflammation(norm, cfg):
        level2.class_note_ru = text("iron_on_inflammation")
    if level2.enabled and level2.anemia_class == "inflammation_anemia":
        level2.case_group_note_ru = text("group_inflammation")


# --------------------------------------------------------------------------------------------
# Точка входа
# --------------------------------------------------------------------------------------------
def analyze(inp: AnalysisInput, *, models: Optional[Models] = None, cfg: Optional[Config] = None) -> AnalysisResult:
    """Полный разбор одного анализа. При ошибке ввода — InputError (ответ 422)."""
    from .level1 import assess_level1
    from .normalize import normalize_input
    from .reports import build_reports
    from .rules import evaluate_rules
    from .values_table import build_values_table

    t0 = time.perf_counter()
    cfg = cfg or load_config()
    models = models or load_models()

    norm = normalize_input(inp, cfg)
    level1 = assess_level1(norm, cfg)
    rules = evaluate_rules(norm, level1, cfg)
    pregnant = norm.pregnancy_status == "yes"

    P14: Optional[np.ndarray] = None
    x_row: Optional[np.ndarray] = None
    explanation = Explanation()
    contrib_text: dict[str, str] = {}
    if pregnant:
        level2 = Level2(enabled=False, disabled_reason=(
            "При беременности уровень 2 и модели выключены: пороги и причины анемии другие, нужна оценка врача."))
    elif models.case is None:
        level2 = Level2(enabled=False, disabled_reason="Модель уровня 2 не загружена; работают правило ВОЗ и подсказки.")
    else:
        from .ml.features import to_matrix
        x_row = to_matrix([{**norm.values, "age_years": norm.age_years, "sex": norm.sex}])[0]
        mode = "benchmark" if inp.mode == "benchmark" else "clinical"   # auto для одного анализа = клинический
        P14 = np.asarray(models.case.predict_profiles(x_row[None, :], mode=mode))[0]

    flags = list(rules.flags)
    mandatory = list(rules.mandatory_tests)
    b12_flag, b12_tests = _b12_flag(norm, P14, flags, cfg)
    if b12_flag is not None:
        flags.append(b12_flag)
        mandatory = b12_tests + mandatory

    if P14 is not None:
        caps_cfg = cfg.class_mapping.get("confidence_caps", {})
        # Причины понижения — строчными и без второго двоеточия: «Уверенность понижена до средней: под подозрением —
        # почки» (ревизия содержания 05.10.2026, п. 10).
        cap_reasons = [_lower_first(f.title) for f in flags if f.code in caps_cfg.get("flags", [])]
        if caps_cfg.get("checklist_suspected", True):
            cap_reasons += [f"под подозрением — {_lower_first(c.title)}" for c in rules.checklist
                            if c.status == "suspected"]
        # MEDIUM-1: индекс Ментцера (MCV/RBC) ниже порога indices.mentzer_thalassemia_like_below — вероятнее
        # носительство β-талассемии; модель кейса обучена без талассемий, поэтому уверенность не выше «средней».
        from .rules import mentzer_index, mentzer_thalassemia_like
        if not pregnant and mentzer_thalassemia_like(norm, cfg):
            from .normalize import fmt_num
            cap_reasons.append(f"индекс Ментцера {fmt_num(mentzer_index(norm, cfg))} — вероятнее носительство "
                               "β-талассемии")
        hm = rules.hemolysis
        cap_low = []
        if hm is not None and hm.found and (hm.missing or hm.default_refs):
            why = []
            if hm.missing:
                why.append("не оценены " + hm.names(hm.missing))
            if hm.default_refs:
                why.append("референс проекта по умолчанию вместо лабораторного")
            cap_low.append("подозрение на гемолиз (" + ", ".join(why) + ")")
        level2, explanation, contrib_text = _level2(P14, x_row, level1, norm, models.case, cfg, mode, cap_reasons,
                                                    cap_low)

    # Ревизия волн 4–5, MEDIUM-2: флаг «Дефицит по сданным анализам не найден» не ставим, если лучший класс уровня 2 —
    # дефицитный с уверенностью не ниже «средней» (пример: B12 90 пг/мл, MCV 112 фл при выключенных порогах B12):
    # иначе заголовок «вероятнее всего: B12-дефицитная анемия» спорит с флагом. Чек-лист исключений сохраняется.
    if (level2.enabled and level2.anemia_class in DEFICIENCY_CLASSES and level2.confidence in CONFIDENT
            and any(f.code == "unexplained_checklist" for f in flags)):
        flags = [f for f in flags if f.code != "unexplained_checklist"]
    _class_notes(level2, level1, norm, cfg)
    hidden, model_detected = _hidden(norm, level1, rules, P14, level2, models, cfg, inp)
    # Анализы «по приросту информации» предлагаем, только когда есть что уточнять: анемия, сигнал правила
    # или сигнал модели по сданным анализам. Одного результата скрининга мало: там нужен только ферритин.
    allow_information = bool(level1.anemia or hidden.rule_signals or model_detected)
    next_tests = rank_next_tests(norm=norm, hidden=hidden, mandatory_tests=mandatory, case_model=models.case,
                                 x_row=x_row, cfg=cfg, allow_information=allow_information and level2.enabled)
    values = build_values_table(norm, cfg, contributions=contrib_text)
    reports = build_reports(norm=norm, level1=level1, level2=level2, hidden=hidden, flags=flags,
                            checklist=rules.checklist, next_tests=next_tests, explanation=explanation,
                            values=values, cfg=cfg)

    limitations = [
        "Исследовательский прототип, не медицинское изделие. Диагноз и лечение определяет врач.",
        "Уровень 2 обучен на учебном наборе кейса (840 строк): точность на других пациентах не проверялась.",
        "Скрининг по общему анализу крови проверен на данных США (NHANES); для своей лаборатории нужна проверка на её данных.",
    ]
    if norm.age_years > cfg.norms["hemoglobin"].get("age_max", 65):
        limitations.append(cfg.norms["hemoglobin"]["age_note"])
    if pregnant:
        limitations.append("При беременности выполняется только уровень 1 по порогам ВОЗ для триместра.")
    if x_row is not None and any(np.isnan(x_row[C.F_IDX[a]]) for a in C.CBC_LABS):
        limitations.append("Общий анализ крови неполный: вывод уровня 2 менее надёжен.")
    # Референсы (решение п. 10): по умолчанию — проекта; локальные референсы лаборатории — с предупреждением.
    from .references import local_warning
    refs = norm.references
    references = ReferencesInfo()
    if refs is not None and refs.local:
        references = ReferencesInfo(set="lab", lab_name=refs.lab_name, local_analytes=list(refs.local_analytes),
                                    warning=local_warning(cfg, refs))
        limitations.insert(0, references.warning)

    return AnalysisResult(
        version=VersionInfo(engine=C.ENGINE_VERSION,
                            case_model=getattr(models.case, "version", "нет") if models.case else "нет",
                            screen_model=getattr(models.screen, "version", "нет") if models.screen else "нет",
                            norms=str(cfg.norms.get("version", ""))),
        patient_ref=inp.patient_ref, level1=level1, level2=level2, hidden_deficiency=hidden,
        flags=flags, checklist=rules.checklist, next_tests=next_tests, explanation=explanation,
        values=values, reports=reports, normalization=norm.notes, limitations=limitations,
        processing_ms=round(1000 * (time.perf_counter() - t0), 2), references=references,
    )
