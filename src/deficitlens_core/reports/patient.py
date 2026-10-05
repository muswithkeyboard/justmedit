"""Отчёт пациенту: сборка из готовых результатов движка. Тексты — config/texts_ru/patient.yaml.

Правила текста пациента:
  - без процентов и чисел вероятности — только слова («вероятно», «возможно», «данных мало»);
  - без диагнозов как утверждений («похоже на…», «может говорить о…»);
  - без лекарств, доз, добавок и диет; лечение подбирает только врач;
  - слова «здоров» и «всё в порядке» запрещены, если есть анемия или сигнал скрытого дефицита;
  - при беременности — только гемоглобин и «нужна оценка врача»;
  - каждая строка проходит фильтр запрещённых слов (config/forbidden_patient_words.yaml);
  - блок «Когда обращаться срочно» при срочных признаках стоит первым и попадает в заголовок;
  - при анемии нехватка, видная по порогу анализа (rules.threshold_deficits), называется нейтрально «Похоже на
    нехватку железа (запас железа снижен) — обсудите с врачом», если модель её не назвала; строка «предположение
    о причине сделано моделью» — только когда причину назвала модель (ревизия содержания 05.10.2026, пп. 2, 15);
  - аббревиатуры анализов без расшифровки (ОЖСС, ЛЖСС) — с пояснением (patient.yaml → names).
Значения показателей с единицей «%» в текст не выводятся (в тексте пациента знака процента нет вообще).
"""
from __future__ import annotations

from ..config import Config
from ..level1 import WHO_HB_AGE_MAX
from ..normalize import fmt_num
from ..rules import ThresholdDeficit, mentzer_thalassemia_like, threshold_deficits
from ..schemas import (ChecklistItem, Explanation, Flag, HiddenDeficiency, Level1, Level2, NextTest, Report,
                       ReportSection, ValueRow)
from . import DISCLAIMER, NEUTRAL_PATIENT_LINE, Lines, Texts, decap, filter_patient_line, filter_patient_lines

# Какие нехватки называет причина модели (коды deficiency_cause): если порог показывает ту же нехватку, а модель её уже
# назвала, отдельная строка «Похоже на нехватку …» не нужна.
CAUSE_NUTRIENTS = {"iron_deficiency": {"iron"}, "B12_deficiency": {"b12"}, "folate_deficiency": {"folate"},
                   "iron_B12": {"iron", "b12"}, "iron_folate": {"iron", "folate"}, "B12_folate": {"b12", "folate"}}
# Запасные тексты строки «по порогу» (основные — config/texts_ru/patient.yaml → threshold).
THRESHOLD_WHAT = {"iron": "железа", "b12": "витамина B12", "folate": "фолатов"}
THRESHOLD_WHY = {"iron": "запас железа снижен", "b12": "витамин B12 снижен", "folate": "фолаты снижены"}

# Порядок пояснений к показателям в разделе «Что это значит».
VALUE_NOTE_ORDER = ["ferritin", "TSAT", "Ret_He", "vitamin_B12", "active_B12", "folate", "vitamin_B6", "copper",
                    "ceruloplasmin", "CRP", "eGFR", "TSH", "homocysteine", "MMA", "MCV", "MCH", "RDW"]
# Показатели, которые при этом флаге объясняет сам флаг (отдельное пояснение было бы повтором или путаницей).
EXPLAINED_BY_FLAG = {
    "ferritin_masked_by_inflammation": {"ferritin", "CRP"},
    "microcytosis_not_iron": {"MCV", "MCH"},
    "normal_mcv_high_rdw": {"RDW"},
}
MAIN_AXES = ("iron", "b12", "folate")     # оси чек-листа, по которым анемия считается «неясной» после проверки


def build_patient_report(*, norm, level1: Level1, level2: Level2, hidden: HiddenDeficiency, flags: list[Flag],
                         checklist: list[ChecklistItem], next_tests: list[NextTest], explanation: Explanation,
                         values: list[ValueRow], cfg: Config) -> Report:
    T = Texts((cfg.texts or {}).get("patient"))
    neutral = T.t("neutral_line") or NEUTRAL_PATIENT_LINE
    hb, thr = fmt_num(level1.hemoglobin), fmt_num(level1.threshold_g_l)
    pregnant = norm.pregnancy_status == "yes"
    anemia = bool(level1.anemia)
    urgent = bool(level1.urgent)
    flag_codes = {f.code for f in flags}
    rows = {r.analyte: r for r in values}
    finding = anemia or bool(hidden.detected)
    screening = hidden.screening if hidden.screening is not None and hidden.screening.applicable else None
    iron_assessed = "ferritin" in norm.measured or screening is not None

    forbidden = list(cfg.forbidden_patient_words or ())
    if finding:
        forbidden += list(T.raw("forbidden_if_finding", []) or [])

    if pregnant:
        context = T.t("context.pregnant")
    else:
        context = T.t(f"context.{norm.sex}")

    # Пояснения к отдельным показателям (по статусам таблицы значений). При беременности пороги остальных
    # показателей не применяются, поэтому пояснений нет.
    value_notes = Lines()
    if not pregnant:
        skip = set().union(*(EXPLAINED_BY_FLAG.get(code, set()) for code in flag_codes)) if flag_codes else set()
        for analyte in VALUE_NOTE_ORDER:
            row = rows.get(analyte)
            if row is None or analyte in skip:
                continue
            value_part = "" if "%" in (row.unit or "") else f" ({fmt_num(row.value)} {row.unit})"
            value_notes.add(T.t(f"sections.meaning.values.{analyte}.{row.norm.status}", value_part=value_part))
    notable = bool(flags) or bool(value_notes)        # есть что обсудить и без анемии или скрытого дефицита

    # ---------------------------------------------------------------- заголовок
    if anemia:
        headline = T.t("headline.anemia", hb=hb, thr=thr, severity=level1.severity_ru or "")
    elif hidden.detected:
        headline = T.t("headline.no_anemia_hidden")
    elif notable:
        headline = T.t("headline.no_anemia_flags")
    else:
        headline = T.t("headline.no_anemia", hb=hb, thr=thr)
    if pregnant:
        headline += T.t("headline.pregnancy")
    if urgent:
        headline = T.t("headline.urgent_prefix") + headline
    headline = headline or f"Гемоглобин {hb} г/л; порог ВОЗ {thr} г/л."     # запасной вариант, если нет файла текстов

    # ---------------------------------------------------------------- главное (одна-две фразы)
    main = Lines()
    cause_named = False       # в «Главном» названа возможная причина анемии (по модели уровня 2)
    deficits = threshold_deficits(norm, cfg) if anemia and not pregnant else []
    if anemia:
        main.add(T.t("sections.main.anemia", hb=hb, thr=thr, context=context, severity=level1.severity_ru or ""))
        if pregnant:
            main.add(T.t("sections.main.pregnancy"))
        else:
            # MEDIUM-1: индекс Ментцера ниже порога (вероятнее носительство β-талассемии) — по одному ОАК причину
            # пациенту не называем; подсказку Ментцера видит только врач.
            cause_text = _cause_line(T, level2, bool(flags), thal_like=mentzer_thalassemia_like(norm, cfg))
            cause_name = T.t(f"causes.{level2.deficiency_cause}") if level2.enabled and level2.deficiency_cause else ""
            cause_named = bool(cause_name) and cause_name in cause_text
            # Ревизия содержания 05.10.2026, п. 2: нехватка видна по порогу (ферритин ниже порога ВОЗ и др.), а модель
            # её не назвала («назвать причину трудно») — пациенту нейтрально «Похоже на нехватку …», без диагноза.
            named = CAUSE_NUTRIENTS.get(level2.deficiency_cause or "", set())
            covered = cause_named and {d.nutrient for d in deficits} <= named
            if deficits and not covered:
                cause_text, cause_named = _threshold_line(T, deficits), False
            main.add(cause_text)
    else:
        main.add(T.t("sections.main.no_anemia", hb=hb, thr=thr, context=context))
        if pregnant:
            main.add(T.t("sections.main.pregnancy"))
        elif hidden.detected:
            main.add(T.t("sections.main.hidden_detected"))
        elif notable:
            main.add(T.t("sections.main.flags"))
        elif hidden.applicable and not iron_assessed:
            main.add(T.t("sections.main.iron_not_assessed"))
        elif hidden.applicable:
            main.add(T.t("sections.main.hidden_none"))

    # ---------------------------------------------------------------- что это значит
    meaning = Lines()
    if anemia:
        meaning.add(T.t("sections.meaning.about_anemia"))
    elif hidden.detected:
        meaning.add(T.t("sections.meaning.about_hidden"))
    if not pregnant:
        meaning.extend(value_notes)
        for flag in flags:
            if flag.code == "unexplained_checklist":
                kind = _unexplained_kind(checklist, norm)
                # «проверено не всё» не повторяем, если в «Главном» уже названа возможная причина и нужны анализы
                if not (kind == "unexplained_partial" and cause_named):
                    meaning.add(T.t(f"sections.meaning.{kind}"))
            else:
                meaning.add(T.t(f"sections.meaning.flags.{flag.code}"))
        # Гемолиз «под подозрением» в чек-листе: пациенту — одна строка без названий анализов и чисел (решение п. 10).
        if any(i.code == "hemolysis" and i.status == "suspected" for i in checklist):
            meaning.add(T.t("sections.meaning.hemolysis_suspected"))
        if screening is not None and not anemia:
            if screening.recommend_ferritin:
                meaning.add(T.t("sections.meaning.screening_elevated", risk=screening.risk_ru or "повышенный"))
            else:
                meaning.add(T.t("sections.meaning.screening_usual"))
        if hidden.detected and not hidden.rule_signals and not (screening is not None and screening.recommend_ferritin):
            meaning.add(T.t("sections.meaning.model_only"))
        if not anemia and hidden.applicable and not iron_assessed and (hidden.detected or notable):
            meaning.add(T.t("sections.main.iron_not_assessed"))      # иначе эта фраза стоит в «Главном»
    if pregnant:
        meaning.add(T.t("sections.meaning.pregnancy"))
    if norm.pregnancy_status == "unknown":
        meaning.add(T.t("sections.meaning.pregnancy_unknown"))
    if not meaning:
        meaning.add(T.t("sections.meaning.nothing"))

    # ---------------------------------------------------------------- что обсудить с врачом
    discuss = Lines()
    something = finding or notable or urgent
    if urgent:
        discuss.add(T.t("sections.discuss.urgent"))
    if pregnant:
        discuss.add(T.t("sections.discuss.pregnancy"))
    elif something:
        discuss.add(T.t("sections.discuss.therapist"))
    else:
        discuss.add(T.t("sections.discuss.no_findings"))
    reasons = _hematologist_reasons(level1, level2, flag_codes, checklist, rows, norm)
    if reasons and not pregnant:
        texts = [T.t(f"sections.discuss.hematologist_reasons.{r}") for r in reasons]
        discuss.add(T.t("sections.discuss.hematologist", reasons="; ".join(t for t in texts if t)))
    if something:
        discuss.add(T.t("sections.discuss.questions"))
        discuss.add(T.t("sections.discuss.treatment"))

    # ---------------------------------------------------------------- какие анализы обсудить
    # Цены не пишем (решение 04.10.2026): вместо цены — где сдать рядом (блок «Где сдать рядом» на странице).
    # Пометку оставляем только для анализа, который в рознице отдельно не сдаётся.
    tests = Lines()

    def patient_name(test: NextTest) -> str:
        """Название анализа пациенту: аббревиатуры с расшифровкой («ОЖСС (железосвязывающая способность)»)."""
        return T.t(f"names.{test.analyte}") or test.name_ru

    for test in next_tests:
        extra = "" if test.available else " " + T.t("sections.tests.unavailable")
        why = T.t(f"sections.tests.why.{test.analyte}") or T.t("sections.tests.why_default")
        tests.add(T.t("sections.tests.item", name=patient_name(test), why=why, extra=extra))
    if next_tests:
        tests.add(T.t("sections.tests.note"))
    else:
        tests.add(T.t("sections.tests.none"))

    # ---------------------------------------------------------------- возможна ошибка — почему
    errors = Lines()
    errors.add(T.t("sections.errors.base"))
    rule_tests = [decap(patient_name(t)) for t in next_tests if t.kind == "rule"]
    cbc_only = not any(a in norm.measured for a in cfg.analytes if cfg.analytes[a].get("group") != "cbc")
    if cbc_only and something:
        errors.add(T.t("sections.errors.cbc_only"))
    if rule_tests:
        errors.add(T.t("sections.errors.missing_tests", names=", ".join(rule_tests)))
    soft = [decap(cfg.name_ru(n.analyte)) for n in norm.notes if n.kind == "soft_range"]
    if soft:
        errors.add(T.t("sections.errors.soft_range", names=", ".join(dict.fromkeys(soft))))
    assumed = [decap(cfg.name_ru(n.analyte)) for n in norm.notes if n.kind == "unit_assumed"]
    if assumed:
        errors.add(T.t("sections.errors.unit_assumed", names=", ".join(dict.fromkeys(assumed))))
    if anemia and not pregnant:
        # П. 15: «предположение о причине сделано моделью» — только если модель причину назвала;
        # «причину не оценивала» — только если и порог нехватку не показал.
        if level2.enabled and cause_named:
            errors.add(T.t("sections.errors.training"))
        elif not level2.enabled and not deficits:
            errors.add(T.t("sections.errors.level2_off"))
    if screening is not None and not anemia:
        errors.add(T.t("sections.errors.screening"))
    if norm.age_years > int(cfg.norms.get("hemoglobin", {}).get("age_max", WHO_HB_AGE_MAX)):
        errors.add(T.t("sections.errors.age"))
    if norm.pregnancy_status == "unknown":
        errors.add(T.t("sections.errors.pregnancy_unknown"))
    errors.add(T.t("sections.errors.single"))

    # ---------------------------------------------------------------- когда обращаться срочно
    urgent_lines = Lines()
    if urgent:
        for item in level1.urgent:
            urgent_lines.add(T.t(f"sections.urgent.items.{item.code}", hb=hb) or T.t("sections.urgent.items.default"))
        urgent_lines.add(T.t("sections.urgent.emergency"))
    else:
        urgent_lines.add(T.t("sections.urgent.general"))
        urgent_lines.add(T.t("sections.urgent.planned"))

    # ---------------------------------------------------------------- порядок разделов и фильтр
    blocks = [("sections.main.title", "Главное", main), ("sections.meaning.title", "Что это значит", meaning),
              ("sections.discuss.title", "Что обсудить с врачом", discuss),
              ("sections.tests.title", "Какие анализы обсудить", tests)]
    urgent_block = ("sections.urgent.title", "Когда обращаться срочно", urgent_lines)
    errors_block = ("sections.errors.title", "Возможна ошибка — почему", errors)
    # Срочные признаки — первым блоком; раздел о возможной ошибке — последним (за ним только строка о прототипе).
    blocks = [urgent_block, *blocks, errors_block] if urgent else [*blocks, urgent_block, errors_block]

    sections: list[ReportSection] = []
    for path, default_title, lines in blocks:
        safe = filter_patient_lines(lines, forbidden, neutral)
        if safe:
            sections.append(ReportSection(title=T.t(path) or default_title, lines=safe))
    disclaimer = T.t("disclaimer") or DISCLAIMER
    if sections:
        sections[-1].lines.append(disclaimer)                 # последняя строка отчёта
    else:
        sections.append(ReportSection(title="Важно", lines=[disclaimer]))
    return Report(headline=filter_patient_line(headline, forbidden, neutral), sections=sections)


def _cause_line(T: Texts, level2: Level2, has_flags: bool = False, thal_like: bool = False) -> str:
    """Причина анемии словами, без чисел: формулировка зависит от уверенности и полноты панели.
    Если сработал флаг-подсказка (картина требует уточнения), формулировка не сильнее «возможная причина».
    thal_like — индекс Ментцера ниже порога: при полноте «только ОАК» причина не называется."""
    if not level2.enabled:
        return T.t("sections.main.cause_unknown")
    cause = T.t(f"causes.{level2.deficiency_cause}") if level2.deficiency_cause else ""
    if not cause:      # «причина не установлена» и прочее без названия для пациента
        full_panel = level2.completeness in ("standard", "extended")
        return T.t("sections.main.cause_undetermined" if full_panel else "sections.main.cause_unknown")
    weak = level2.confidence not in ("high", "moderate")      # низкая уверенность: причину пациенту не называем
    if level2.completeness == "cbc":
        if weak or level2.ambiguous or thal_like:
            return T.t("sections.main.cause_cbc_unknown")
        return T.t("sections.main.cause_cbc_only", cause=cause)
    if weak:
        return T.t("sections.main.cause_low")
    if level2.ambiguous:
        return T.t("sections.main.cause_ambiguous", cause=cause)
    confidence = "moderate" if has_flags else level2.confidence
    return T.t(f"sections.main.cause_{confidence}", cause=cause)


def _threshold_line(T: Texts, deficits: list[ThresholdDeficit]) -> str:
    """«Похоже на нехватку железа (запас железа снижен) — обсудите с врачом.» Без диагноза, чисел и лечения."""
    nutrients = list(dict.fromkeys(d.nutrient for d in deficits))
    what = [T.t(f"threshold.what.{n}") or THRESHOLD_WHAT[n] for n in nutrients]
    why = [T.t(f"threshold.why.{n}") or THRESHOLD_WHY[n] for n in nutrients]
    what_text = what[0] if len(what) == 1 else ", ".join(what[:-1]) + " и " + what[-1]
    return (T.t("sections.main.threshold", what=what_text, why="; ".join(why))
            or f"Похоже на нехватку {what_text} ({'; '.join(why)}) — обсудите с врачом.")


def _unexplained_kind(checklist: list[ChecklistItem], norm) -> str:
    """Какой текст про «неясную» анемию показать пациенту (ключ шаблона в sections.meaning).

    unexplained_full        — железо, B12 и фолаты исключены по порогам;
    unexplained_b12_unrated — железо и фолаты исключены, B12 сдан, но числового порога для него нет
                              (правило источников: зарубежные пороги не применяются, B12 оценивает врач);
    unexplained_partial     — проверено не всё."""
    status = {i.code: i.status for i in checklist}
    if all(status.get(axis) == "excluded" for axis in MAIN_AXES):
        return "unexplained_full"
    if (status.get("iron") == "excluded" and status.get("folate") == "excluded"
            and "vitamin_B12" in norm.measured):
        return "unexplained_b12_unrated"
    return "unexplained_partial"


def _hematologist_reasons(level1: Level1, level2: Level2, flag_codes: set[str], checklist: list[ChecklistItem],
                          rows: dict[str, ValueRow], norm=None) -> list[str]:
    """Когда пациенту стоит обсудить консультацию гематолога: умеренная или тяжёлая анемия, смешанный дефицит,
    неясная анемия (основные дефициты исключены), микроцитоз без дефицита железа."""
    reasons: list[str] = []
    if level1.anemia and level1.severity in ("moderate", "severe"):
        reasons.append("moderate_severe")

    def low(analyte: str) -> bool:
        return analyte in rows and rows[analyte].norm.status == "low"

    mixed_by_model = (level2.enabled and level2.anemia_class == "mixed_deficiency"
                      and level2.confidence in ("high", "moderate") and level2.completeness != "cbc")
    mixed_by_values = low("ferritin") and (low("vitamin_B12") or low("folate"))
    if level1.anemia and (mixed_by_model or mixed_by_values):
        reasons.append("mixed")
    status = {i.code: i.status for i in checklist}
    worked_up = norm is not None and _unexplained_kind(checklist, norm) != "unexplained_partial"
    if ("unexplained_checklist" in flag_codes and worked_up
            and "suspected" not in status.values()):       # есть подозрение (почки…) — причина не «неясная»
        reasons.append("unexplained")
    if "microcytosis_not_iron" in flag_codes:
        reasons.append("microcytosis")
    return reasons
