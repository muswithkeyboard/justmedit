"""Таблица значений: по строке на каждый сданный и расчётный показатель — значение, статус, порог с источником.

Порядок строк: общий анализ крови (constants.CBC_LABS), затем группы constants.GROUPS.
Статус (low / normal / high / borderline / unknown) считается только по порогам config/norms_ru.yaml:
  - гемоглобин — по порогу уровня 1 (ВОЗ 2024, с учётом пола и беременности);
  - ферритин — по выбранному набору порогов (norms_set: "who" -> порог ВОЗ, "ru" -> порог практики) и по воспалению
    (СРБ выше порога -> действует порог ВОЗ «при воспалении»);
  - MCV, MCH, RDW — по разделу indices;
  - остальные — по своей записи в norms_ru.yaml (ключ = код показателя в нижнем регистре);
  - записи с origin: foreign применяются, если они в policy.foreign_allowed (B12, активный B12, гомоцистеин — NICE NG239,
    решение п. 10); выключенная запись — статус unknown и подпись «числового порога в КР РФ нет; оценка по референсу
    лаборатории»;
  - референс (references.py: проект по умолчанию или лаборатория) дополняет диагностический порог: если порог есть,
    он решает свою сторону, а значение между референсом и порогом — «пограничное»; где порога нет — «ниже / выше
    референса»; где нет ни порога, ни референса — «оценка по референсу лаборатории» (unknown).
Верхних порогов, которых нет в конфигурации (например, для ферритина и НТЖ), таблица не оценивает.
"""
from __future__ import annotations

from typing import Any

from . import constants as C
from .config import Config
from .level1 import hemoglobin_rule
from .normalize import Normalized, fmt_num
from .references import RefRange, ref_phrase, references_for
from .rules import NO_RU_THRESHOLD_TEXT, foreign_blocked, has_inflammation, norm_value
from .schemas import NormInfo, ValueRow

LAB_REFERENCE_TEXT = "оценка по референсу лаборатории"
TOP_CONTRIBUTIONS = 3      # «вклад в вывод» показывается у стольких самых весомых анализов, даже если значение в норме
PREGNANCY_FERRITIN_TEXT = "при беременности программа ферритин не оценивает — оценка врача, ведущего беременность"
# Порог есть только в зарубежном руководстве, а правило источников его не применяет.
NO_RU_TEXT = f"{NO_RU_THRESHOLD_TEXT}; {LAB_REFERENCE_TEXT}"

# Короткие подписи источников для столбца «порог» (полное название — в NormInfo.source).
# Можно переопределить в config/texts_ru/doctor.yaml -> source_short.
SOURCE_SHORT = {
    "who_hb_2024": "ВОЗ 2024",
    "who_ferritin_2020": "ВОЗ 2020",
    "kr_ida_2024": "КР «ЖДА» 2024",
    "kr_b12_2024": "КР «B12-дефицитная анемия» 2024",
    "kr_folate_2024": "КР «Фолиеводефицитная анемия» 2024",
    "nice_ng239": "NICE NG239 (зарубежный источник; в КР РФ числового порога нет)",
    "bcsh_2014": "BCSH 2014",
    "ckd_epi_2021": "CKD-EPI 2021",
    "kr_ckd": "КР «ХБП»",
    "mentzer_1973": "Mentzer, 1973",
    "lab_reference": "общепринятый диапазон (проект)",
    "project": "решение команды проекта",
    "project_reference": "референс проекта по умолчанию",
    "cdc_iron": "CDC",
    "hemasphere_2024": "HemaSphere 2024",
    "masac_296": "MASAC 296",
}
# Ответ врача 04.10.2026 (п. 11, вопрос 5): обычные взрослые референсы у беременных не применять; без референса
# лаборатории или триместра показатель не флагируется.
PREGNANCY_REFERENCE_TEXT = ("не оценено — отсутствует референс для беременности: нужен референс с бланка лаборатории "
                            "или по триместру")

# Что означает выход за порог (подпись в столбце «порог»).
MEANING: dict[str, dict[str, str]] = {
    "serum_iron": {"low": "ниже референса КР", "high": "выше референса КР"},
    "TIBC": {"low": "ниже референса КР", "high": "выше референса КР"},
    "transferrin": {"low": "ниже референса КР", "high": "выше референса КР"},
    "TSAT": {"low": "ниже референса КР"},
    "Ret_He": {"low": "снижен (критерий КР)"},
    "vitamin_B12": {"low": "дефицит B12 вероятен", "borderline": "серая зона (нужен функциональный маркер)"},
    "active_B12": {"low": "дефицит B12 вероятен", "borderline": "серая зона"},
    "MMA": {"high": "повышена"},
    "homocysteine": {"high": "повышен"},
    "folate": {"low": "дефицит фолатов", "borderline": "пограничный уровень"},
    "vitamin_B6": {"low": "снижен"},
    "copper": {"low": "снижена"},
    "ceruloplasmin": {"low": "снижен"},
    "CRP": {"high": "признак воспаления"},
    "eGFR": {"low": "снижена", "severe": "выраженное снижение"},
    "TSH": {"high": "повышен", "marked": "выраженное повышение"},
}
# Индексы ОАК: (ключ порога в разделе indices, статус, подпись).
INDEX_RULES: dict[str, list[tuple[str, str, str]]] = {
    "MCV": [("mcv_micro_below", "low", "микроцитоз"), ("mcv_macro_above", "high", "макроцитоз")],
    "MCH": [("mch_low_below", "low", "гипохромия")],
    "RDW": [("rdw_high_above", "high", "эритроциты неоднородны по размеру (анизоцитоз)")],
}
DERIVED_SUFFIX = {"TSAT": " (расчёт: железо / ОЖСС × 100)", "eGFR": " (расчёт по креатинину, CKD-EPI 2021)"}


def short_source(cfg: Config, key: str | None) -> str:
    """Короткая подпись источника: из текстов конфигурации, из SOURCE_SHORT или полное название."""
    if not key:
        return ""
    override = ((cfg.texts or {}).get("doctor") or {}).get("source_short") or {}
    return str(override.get(key) or SOURCE_SHORT.get(key) or cfg.source_title(key))


def _working(entry_status: Any) -> str:
    """Пометка для порогов со статусом needs_medical_expert: значение рабочее, окончательно решает врач команды."""
    return "; рабочий порог" if entry_status == "needs_medical_expert" else ""


def _num(x: Any) -> str:
    return fmt_num(float(x))


def _hemoglobin(value: float, norm: Normalized, cfg: Config) -> NormInfo:
    rule = hemoglobin_rule(norm, cfg)
    key = cfg.norms["hemoglobin"]["source"]
    thr, who = _num(rule["threshold"]), short_source(cfg, key)
    if value < rule["threshold"]:
        return NormInfo(status="low", threshold_text=f"ниже {thr} г/л ({rule['context']}) — анемия; {who}",
                        source=cfg.source_title(key))
    return NormInfo(status="normal", threshold_text=f"не ниже {thr} г/л ({rule['context']}); {who}",
                    source=cfg.source_title(key))


def _ferritin(value: float, norm: Normalized, cfg: Config) -> NormInfo:
    """Ферритин, мкг/л. Пороги: ВОЗ 2020 (дефицит; при воспалении — отдельный порог) и порог практики (КР «ЖДА» 2024)."""
    f = cfg.norms.get("ferritin") or {}
    who, ru = (f.get("deficiency_below") or {}).get("who"), (f.get("deficiency_below") or {}).get("ru")
    infl_thr = f.get("inflammation_below")
    sources = f.get("sources") or {}
    label = f.get("ru_label") or "порог практики"
    who_key, ru_key, infl_key = sources.get("who"), sources.get("ru"), sources.get("inflammation")
    inflamed = has_inflammation(norm, cfg)
    crp_thr = norm_value(cfg, "crp", "inflammation_above")
    if who is None:
        return NormInfo(status="unknown", threshold_text=LAB_REFERENCE_TEXT, source=cfg.source_title("lab_reference"))

    if value < float(who):
        return NormInfo(status="low", source=cfg.source_title(who_key),
                        threshold_text=f"ниже {_num(who)} мкг/л — дефицит железа; {short_source(cfg, who_key)}")
    if inflamed and infl_thr is not None:
        src_s, src_f = short_source(cfg, infl_key), cfg.source_title(infl_key)
        if value < float(infl_thr):
            return NormInfo(status="low", source=src_f, threshold_text=(
                f"ниже {_num(infl_thr)} мкг/л при воспалении (СРБ выше {_num(crp_thr)} мг/л) — "
                f"дефицит железа вероятен; {src_s}"))
        return NormInfo(status="normal", source=src_f,
                        threshold_text=f"не ниже {_num(infl_thr)} мкг/л (порог при воспалении); {src_s}")
    if ru is not None and value < float(ru):
        if norm.norms_set == "ru":
            return NormInfo(status="low", source=cfg.source_title(ru_key),
                            threshold_text=f"ниже {_num(ru)} мкг/л — {label}; {short_source(cfg, ru_key)}")
        return NormInfo(status="borderline", source=cfg.source_title(who_key), threshold_text=(
            f"не ниже {_num(who)} мкг/л ({short_source(cfg, who_key)}), но ниже {_num(ru)} мкг/л — {label}"))
    # не ниже порогов дефицита; верхняя граница в конфигурации не задана и не оценивается
    if norm.norms_set == "ru" and ru is not None:
        thr, key, tail = ru, ru_key, f"{short_source(cfg, ru_key)}, порог практики"
    else:
        thr, key, tail = who, who_key, short_source(cfg, who_key)
    text = f"не ниже {_num(thr)} мкг/л; {tail}"
    if inflamed is None and infl_thr is not None and value < float(infl_thr):
        text += f" (СРБ не сдан: при воспалении порог {_num(infl_thr)} мкг/л)"
    return NormInfo(status="normal", threshold_text=text, source=cfg.source_title(key))


def _index(analyte: str, value: float, cfg: Config) -> NormInfo:
    """MCV, MCH, RDW — по разделу indices (рабочие значения, решает врач команды)."""
    idx = cfg.norms.get("indices") or {}
    key = idx.get("source", "lab_reference")
    tail = f"{short_source(cfg, key)}{_working(idx.get('status'))}"
    unit = cfg.unit_ru(analyte)
    known = []
    for thr_key, status, meaning in INDEX_RULES[analyte]:
        thr = idx.get(thr_key)
        if thr is None:
            continue
        known.append((float(thr), status))
        if (status == "low" and value < float(thr)) or (status == "high" and value > float(thr)):
            word = "ниже" if status == "low" else "выше"
            return NormInfo(status=status, threshold_text=f"{word} {_num(thr)} {unit} — {meaning}; {tail}",
                            source=cfg.source_title(key))
    if not known:
        return NormInfo(status="unknown", threshold_text=LAB_REFERENCE_TEXT, source=cfg.source_title("lab_reference"))
    lows = [t for t, s in known if s == "low"]
    highs = [t for t, s in known if s == "high"]
    if lows and highs:
        text = f"в пределах {_num(lows[0])}–{_num(highs[0])} {unit}; {tail}"
    elif lows:
        text = f"не ниже {_num(lows[0])} {unit}; {tail}"
    else:
        text = f"не выше {_num(highs[0])} {unit}; {tail}"
    return NormInfo(status="normal", threshold_text=text, source=cfg.source_title(key))


def _generic(analyte: str, value: float, cfg: Config) -> NormInfo:
    """Показатель с собственной записью в norms_ru.yaml (ключ — код в нижнем регистре)."""
    entry = cfg.norms.get(analyte.lower())
    unknown = NormInfo(status="unknown", threshold_text=LAB_REFERENCE_TEXT, source=cfg.source_title("lab_reference"))
    if not isinstance(entry, dict):
        return unknown
    if foreign_blocked(cfg, analyte.lower()):      # правило источников: зарубежный порог не применяется
        return NormInfo(status="unknown", threshold_text=NO_RU_TEXT, source=cfg.source_title("lab_reference"))
    low = next((entry[k] for k in ("deficiency_below", "low_below", "reduced_below") if entry.get(k) is not None), None)
    severe = entry.get("severe_below")
    high = next((entry[k] for k in ("high_above", "inflammation_above") if entry.get(k) is not None), None)
    marked = entry.get("marked_above")
    zone = entry.get("gray_zone") or entry.get("borderline")
    zone_inclusive = bool(entry.get("gray_zone_inclusive"))      # верхняя граница зоны тоже входит в зону
    if low is None and high is None:
        return unknown
    key = entry.get("source")
    src_full = cfg.source_title(key) if key else ""
    tail = f"{short_source(cfg, key)}{_working(entry.get('status'))}"
    unit = cfg.unit_ru(analyte)
    meaning = MEANING.get(analyte, {})

    def info(status: str, text: str) -> NormInfo:
        return NormInfo(status=status, threshold_text=f"{text}; {tail}", source=src_full)

    if severe is not None and value < float(severe):
        return info("low", f"ниже {_num(severe)} {unit} — {meaning.get('severe', meaning.get('low', 'ниже порога'))}")
    if low is not None and value < float(low):
        return info("low", f"ниже {_num(low)} {unit} — {meaning.get('low', 'ниже порога')}")
    if marked is not None and value > float(marked):
        return info("high", f"выше {_num(marked)} {unit} — {meaning.get('marked', meaning.get('high', 'выше порога'))}")
    if high is not None and value > float(high):
        return info("high", f"выше {_num(high)} {unit} — {meaning.get('high', 'выше порога')}")
    if zone and (float(zone[0]) <= value <= float(zone[1]) if zone_inclusive
                 else float(zone[0]) <= value < float(zone[1])):
        label = meaning.get("borderline", "пограничная зона")
        return info("borderline", f"{_num(zone[0])}–{_num(zone[1])} {unit} — {label}")
    if low is not None and high is not None:
        return info("normal", f"в пределах {_num(low)}–{_num(high)} {unit}")
    if low is not None:
        if zone and zone_inclusive:
            return info("normal", f"выше {_num(zone[1])} {unit}")
        return info("normal", f"не ниже {_num(zone[1] if zone else low)} {unit}")
    return info("normal", f"не выше {_num(high)} {unit}")


def _diag_sides(analyte: str, cfg: Config) -> set[str]:
    """Стороны, на которых у показателя есть ДИАГНОСТИЧЕСКИЙ порог (low / high): там решает порог, а не референс."""
    if analyte in ("hemoglobin", "ferritin"):
        return {"low"}
    if analyte in INDEX_RULES:
        idx = cfg.norms.get("indices") or {}
        return {status for key, status, _ in INDEX_RULES[analyte] if idx.get(key) is not None}
    entry = cfg.norms.get(analyte.lower())
    if not isinstance(entry, dict) or foreign_blocked(cfg, analyte.lower()):
        return set()
    sides = set()
    if any(entry.get(k) is not None for k in ("deficiency_below", "low_below", "reduced_below", "severe_below")):
        sides.add("low")
    if any(entry.get(k) is not None for k in ("high_above", "inflammation_above", "marked_above")):
        sides.add("high")
    return sides


def _reference_label(ref: RefRange, norm: Normalized, cfg: Config) -> tuple[str, str]:
    """(короткая подпись, полный источник) референса: проекта по умолчанию (с первоисточником) или лаборатории."""
    if ref.local:
        refs = references_for(norm, cfg)
        title = refs.lab_title or "лаборатория"
        return f"референс лаборатории ({title})", f"референс лаборатории: {title}"
    section = cfg.norms.get("reference_ranges") or {}
    label = str(section.get("label") or "референс проекта по умолчанию")
    table = str(section.get("table") or "")
    full = cfg.source_title(section.get("source") or "project_reference")
    if ref.primary:
        short = f"{label} ({table}; {short_source(cfg, ref.primary)})" if table else f"{label} ({short_source(cfg, ref.primary)})"
        full = f"{full}; первоисточник: {cfg.source_title(ref.primary)}"
    else:
        short = f"{label} ({table}; первоисточник в таблице не указан)" if table else label
    return short + _working(section.get("status")), full


def _with_reference(analyte: str, value: float, diag: NormInfo | None, norm: Normalized, cfg: Config) -> NormInfo:
    """Статус с учётом референса (решение п. 10). diag — статус по диагностическому порогу (None — порога нет).

    Порог решает свою сторону: «ниже порога» / «выше порога» / серая зона остаются как есть. Значение вне референса
    на стороне, где порога нет, — «ниже / выше референса»; между референсом и порогом — «пограничное». В пределах
    референса текст диагностического порога не меняется."""
    ref = references_for(norm, cfg).get(analyte)
    if ref is None:
        if diag is not None:
            return diag
        if norm.pregnancy_status == "yes" and analyte in ((cfg.norms.get("reference_ranges") or {}).get("ranges") or {}):
            return NormInfo(status="unknown", threshold_text=PREGNANCY_REFERENCE_TEXT,
                            source=cfg.source_title("project_reference"))
        return NormInfo(status="unknown", threshold_text=LAB_REFERENCE_TEXT, source=cfg.source_title("lab_reference"))
    if diag is not None and diag.status in ("low", "high", "borderline"):
        return diag
    sides = _diag_sides(analyte, cfg) if diag is not None else set()
    label, full = _reference_label(ref, norm, cfg)
    unit = cfg.unit_ru(analyte)
    # ref_phrase: с одной границей — «выше референса (верхняя граница 10 мкмоль/л)», без склейки
    # «выше референса не выше …».
    for side, outside in (("low", ref.below(value)), ("high", ref.above(value))):
        if not outside:
            continue
        if side in sides:          # порог на этой стороне есть, но не достигнут: между референсом и порогом
            return NormInfo(status="borderline", source=full, threshold_text=(
                f"{ref_phrase(side, ref, unit)} ({label}); диагностический порог не достигнут: {diag.threshold_text}"))
        return NormInfo(status=side, source=full, threshold_text=f"{ref_phrase(side, ref, unit)}; {label}")
    if diag is not None:
        return diag
    return NormInfo(status="normal", source=full, threshold_text=f"{ref_phrase('within', ref, unit)}; {label}")


def norm_info(analyte: str, value: float, norm: Normalized, cfg: Config) -> NormInfo:
    """Статус одного показателя: диагностический порог norms_ru.yaml и референс (references.py)."""
    if analyte == "hemoglobin":
        return _with_reference(analyte, value, _hemoglobin(value, norm, cfg), norm, cfg)
    if analyte in (cfg.norms.get("no_reference_yet") or []):
        return _with_reference(analyte, value, None, norm, cfg)
    if analyte == "ferritin":
        # Ферритин у беременных не оценивается: для них — только уровень 1 (решение капитана 04.10.2026,
        # docs/review/medic_decisions.md, п. 8); пороги ВОЗ и КР в norms_ru.yaml даны для небеременных взрослых.
        if norm.pregnancy_status == "yes":
            return NormInfo(status="unknown", threshold_text=PREGNANCY_FERRITIN_TEXT,
                            source=cfg.source_title("lab_reference"))
        return _with_reference(analyte, value, _ferritin(value, norm, cfg), norm, cfg)
    if analyte in INDEX_RULES:
        diag = _index(analyte, value, cfg)
    else:
        diag = _generic(analyte, value, cfg)
        if foreign_blocked(cfg, analyte.lower()):       # правило источников: число не применяется вовсе
            return diag
    return _with_reference(analyte, value, None if diag.status == "unknown" else diag, norm, cfg)


def build_values_table(norm: Normalized, cfg: Config, contributions: dict[str, str] | None = None) -> list[ValueRow]:
    """Строки таблицы. contributions — «вклад в вывод» по модели (engine.py), упорядочен по убыванию веса: он
    показывается у значений вне нормы (ниже, выше, пограничные) и у TOP_CONTRIBUTIONS самых весомых (ревизия UX
    05.10.2026: у каждого нормального значения подпись «вклад» — шум)."""
    contributions = contributions or {}
    top = set(list(contributions)[:TOP_CONTRIBUTIONS])
    warnings: dict[str, str] = {}
    for note in norm.notes:
        if note.kind == "soft_range" and note.analyte not in warnings:
            warnings[note.analyte] = note.text
    order = C.CBC_LABS + C.LABS
    extra = [a for a in norm.values if a not in order]     # на случай показателей вне списков constants
    rows: list[ValueRow] = []
    for analyte in order + extra:
        if analyte not in norm.values:
            continue
        value = float(norm.values[analyte])
        name = cfg.name_ru(analyte) + (DERIVED_SUFFIX.get(analyte, " (расчёт)") if norm.is_derived(analyte) else "")
        info = norm_info(analyte, value, norm, cfg)
        shown = analyte in top or info.status in ("low", "high", "borderline")
        rows.append(ValueRow(
            analyte=analyte, name_ru=name, value=round(value, 4), unit=cfg.unit_ru(analyte), norm=info,
            contribution=contributions.get(analyte) if shown else None, warning=warnings.get(analyte)))
    return rows
