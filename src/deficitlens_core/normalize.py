"""Нормализация ввода: коды показателей, единицы, допустимые диапазоны, расчётные НТЖ и рСКФ.

Что делает normalize_input:
  1. приводит код показателя к каноническому (имя столбца файла кейса); неизвестный код ищется среди
     алиасов из config/analytes.yaml без учёта регистра; не найден — заметка kind="ignored", не ошибка;
  2. пересчитывает значение в каноническую единицу: множитель берётся из analytes.yaml -> units
     (B12 пмоль/л × 1,355 = пг/мл; фолаты нмоль/л × 0,4413 = нг/мл; Hb г/дл × 10 = г/л; железо мкг/дл × 0,179 = мкмоль/л);
     каждый пересчёт попадает в заметки (kind="converted");
  3. проверяет диапазоны из analytes.yaml: вне hard — ошибка 422 (out_of_range), вне soft — заметка «проверьте значение»;
  4. считает производные показатели (в measured они не входят):
       НТЖ  = железо сыворотки / ОЖСС × 100, %                       (оба в мкмоль/л; показатель из перечня КР «ЖДА» 2024);
       рСКФ = CKD-EPI 2021 по креатинину, возрасту и полу, мл/мин/1,73 м².
     Сданные НТЖ и рСКФ важнее расчётных: если показатель прислан, он не пересчитывается.

Тихих поправок нет: всё, что изменено или не использовано, записано в notes. Сервис ничего не хранит.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from .config import Config
from .schemas import AnalysisInput, InputError, LabValue, NormalizationNote


@dataclass
class Normalized:
    sex: str                      # "F" | "M"
    age_years: int
    pregnancy_status: str         # "no" | "yes" | "unknown"
    trimester: int | None
    values: dict[str, float]      # канонические коды и единицы; включает производные TSAT и eGFR (если посчитаны)
    measured: set[str]            # коды, реально присланные пользователем (без производных)
    notes: list[NormalizationNote] = field(default_factory=list)
    norms_set: str = "who"        # "who" | "ru"
    references: Any = None        # references.References: действующие референсы (проект по умолчанию / лаборатория)

    def is_derived(self, analyte: str) -> bool:
        """Показатель рассчитан движком (НТЖ, рСКФ), а не прислан пользователем."""
        return analyte in self.values and analyte not in self.measured


# --------------------------------------------------------------------------------------------
# Форматирование чисел для русских текстов (используют правила, таблица значений и отчёты)
# --------------------------------------------------------------------------------------------
def fmt_num(x: float, digits: int | None = None) -> str:
    """Число для русского текста: десятичная запятая, без хвостовых нулей (124 / 15,2 / 0,25 / 0,0005)."""
    x = float(x)
    if not math.isfinite(x):
        return str(x)
    if digits is None:
        ax = abs(x)
        if ax == 0:
            digits = 0
        elif ax >= 10:
            digits = 1
        elif ax >= 1:
            digits = 2
        else:  # для малых величин (ТТГ, ММК) — три значащие цифры
            digits = min(6, 2 - int(math.floor(math.log10(ax))))
    s = f"{x:.{digits}f}"
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    if s in ("-0", ""):
        s = "0"
    return s.replace(".", ",")


# --------------------------------------------------------------------------------------------
# Единицы и коды показателей
# --------------------------------------------------------------------------------------------
# Символы заданы кодами, а не буквами: в редакторе они неотличимы, а нормализация Юникода может их подменить.
_MICRO = chr(0x00B5)   # знак «микро» (U+00B5)
_MU = chr(0x03BC)      # греческая буква мю (U+03BC) — в единицах считается тем же символом


def unit_key(unit: Any) -> str:
    """Единица для сравнения: нижний регистр, без пробелов; «μ» и «µ» — один символ."""
    return "".join(str(unit).split()).lower().replace(_MU, _MICRO)


def _name_key(name: Any) -> str:
    """Название показателя для сравнения: без учёта регистра и лишних пробелов; «ё» = «е»."""
    return " ".join(str(name).split()).casefold().replace("ё", "е")


_INDEX_CACHE: dict[int, tuple[Any, dict[str, str]]] = {}


def _alias_index(cfg: Config) -> dict[str, str]:
    """Словарь «написание -> канонический код»: сами коды, алиасы, короткие и полные русские названия."""
    cached = _INDEX_CACHE.get(id(cfg.analytes))
    if cached is not None and cached[0] is cfg.analytes:
        return cached[1]
    index: dict[str, str] = {}
    for code in cfg.analytes:                       # канонические коды важнее алиасов
        index.setdefault(_name_key(code), code)
    for code, spec in cfg.analytes.items():
        for alias in [*(spec.get("aliases") or []), spec.get("short_ru"), spec.get("name_ru")]:
            if alias:
                index.setdefault(_name_key(alias), code)
    _INDEX_CACHE[id(cfg.analytes)] = (cfg.analytes, index)
    return index


def resolve_analyte(key: Any, cfg: Config) -> str | None:
    """Канонический код показателя по коду или алиасу (без учёта регистра); None — показатель неизвестен."""
    if key in cfg.analytes:
        return key
    return _alias_index(cfg).get(_name_key(key))


def _unit_table(spec: dict) -> dict[str, float]:
    """Допустимые единицы показателя -> множитель к канонической единице (из analytes.yaml)."""
    table = {unit_key(u): float(m) for u, m in (spec.get("units") or {}).items()}
    for canonical in (spec.get("unit"), spec.get("unit_ru")):   # сама каноническая единица и её русская запись
        if canonical:
            table.setdefault(unit_key(canonical), 1.0)
    return table


def _allowed_units(spec: dict) -> list[str]:
    """Перечень допустимых единиц для сообщения об ошибке (без дублей «μ» / «µ»)."""
    seen: set[str] = set()
    out: list[str] = []
    for u in (spec.get("units") or {}):
        k = unit_key(u)
        if k not in seen:
            seen.add(k)
            out.append(str(u))
    return out


def _other_units(spec: dict, used_multiplier: float) -> list[tuple[str, float]]:
    """Единицы с другим множителем (по одной на множитель; русское написание — в приоритете)."""
    by_mult: dict[float, str] = {}
    for u, m in (spec.get("units") or {}).items():
        m = float(m)
        if math.isclose(m, used_multiplier):
            continue
        is_cyrillic = any("а" <= ch <= "я" for ch in str(u).lower())
        if m not in by_mult or is_cyrillic:
            by_mult[m] = str(u)
    return [(u, m) for m, u in by_mult.items()]


def _split_value(raw: Any) -> tuple[Any, Any]:
    """Значение и единица из LabValue, словаря {value, unit} или числа."""
    if isinstance(raw, LabValue):
        return raw.value, raw.unit
    if isinstance(raw, dict):
        return raw.get("value"), raw.get("unit")
    return raw, None


# --------------------------------------------------------------------------------------------
# Производные показатели
# --------------------------------------------------------------------------------------------
# CKD-EPI 2021 (формула рСКФ по креатинину без поправки на расу).
# Источник: Inker L.A. et al. New Creatinine- and Cystatin C–Based Equations to Estimate GFR without Race.
#           N Engl J Med 2021; 385: 1737–1749.
#   рСКФ = 142 × min(Scr/κ, 1)^α × max(Scr/κ, 1)^(−1,200) × 0,9938^возраст × (1,012 для женщин),
#   Scr — креатинин сыворотки в мг/дл (мкмоль/л ÷ 88,4); κ = 0,7 (Ж) / 0,9 (М); α = −0,241 (Ж) / −0,302 (М).
# Результат — мл/мин/1,73 м². Это коэффициенты формулы, а не пороги показа (пороги — в config/norms_ru.yaml).
_CKD_EPI_BASE = 142.0
_CKD_EPI_KAPPA = {"F": 0.7, "M": 0.9}
_CKD_EPI_ALPHA = {"F": -0.241, "M": -0.302}
_CKD_EPI_EXPONENT_ABOVE = -1.200
_CKD_EPI_AGE_FACTOR = 0.9938
_CKD_EPI_FEMALE_FACTOR = 1.012
_UMOL_PER_MGDL_CREATININE = 88.4   # запасное значение; основное — множитель «mg/dl» креатинина в analytes.yaml


def ckd_epi_2021(creatinine_umol_l: float, age_years: float, sex: str,
                 umol_per_mgdl: float = _UMOL_PER_MGDL_CREATININE) -> float:
    """рСКФ по CKD-EPI 2021, мл/мин/1,73 м². Вход: креатинин в мкмоль/л, возраст в годах, пол "F" | "M"."""
    scr = creatinine_umol_l / umol_per_mgdl            # мг/дл
    ratio = scr / _CKD_EPI_KAPPA[sex]
    egfr = (_CKD_EPI_BASE
            * min(ratio, 1.0) ** _CKD_EPI_ALPHA[sex]
            * max(ratio, 1.0) ** _CKD_EPI_EXPONENT_ABOVE
            * _CKD_EPI_AGE_FACTOR ** age_years)
    if sex == "F":
        egfr *= _CKD_EPI_FEMALE_FACTOR
    return egfr


def transferrin_saturation(serum_iron_umol_l: float, tibc_umol_l: float) -> float:
    """НТЖ (насыщение трансферрина железом), %: железо сыворотки / ОЖСС × 100; оба показателя в мкмоль/л.
    Показатель входит в перечень исследований обмена железа КР «Железодефицитная анемия» 2024."""
    return serum_iron_umol_l / tibc_umol_l * 100.0


def _in_range(value: float, bounds: Any) -> bool:
    return bool(bounds) and float(bounds[0]) <= value <= float(bounds[1])


def _add_derived(norm: Normalized, cfg: Config) -> None:
    """Добавляет расчётные НТЖ и рСКФ в norm.values (в measured они не попадают) и пишет заметки."""
    values = norm.values

    # --- НТЖ: только если не сдано, а железо и ОЖСС есть ---
    if "TSAT" not in values and "serum_iron" in values and "TIBC" in values and values["TIBC"] > 0:
        spec = cfg.analytes.get("TSAT", {})
        tsat = round(transferrin_saturation(values["serum_iron"], values["TIBC"]), 1)
        formula = (f"железо {fmt_num(values['serum_iron'])} {cfg.unit_ru('serum_iron')} / "
                   f"ОЖСС {fmt_num(values['TIBC'])} {cfg.unit_ru('TIBC')} × 100")
        if not _in_range(tsat, spec.get("hard", [0, float("inf")])):
            norm.notes.append(NormalizationNote(
                analyte="TSAT", kind="soft_range",
                text=(f"НТЖ не рассчитано: {formula} = {fmt_num(tsat)} % — вне допустимого диапазона. "
                      "Проверьте значения и единицы железа и ОЖСС.")))
        else:
            values["TSAT"] = tsat
            norm.notes.append(NormalizationNote(
                analyte="TSAT", kind="derived", value=tsat, to_unit=spec.get("unit", "%"),
                text=f"НТЖ рассчитано: {formula} = {fmt_num(tsat)} %."))
            if spec.get("soft") and not _in_range(tsat, spec["soft"]):
                norm.notes.append(NormalizationNote(
                    analyte="TSAT", kind="soft_range", value=tsat,
                    text=f"Расчётное НТЖ {fmt_num(tsat)} % — нетипичное значение: проверьте железо и ОЖСС."))

    # --- рСКФ: только если не сдана, а креатинин есть ---
    if "eGFR" not in values and "creatinine" in values:
        spec = cfg.analytes.get("eGFR", {})
        source = cfg.source_title("ckd_epi_2021")
        if norm.pregnancy_status == "yes":
            # При беременности формула CKD-EPI не проверялась: расчётное значение не выводим.
            norm.notes.append(NormalizationNote(
                analyte="eGFR", kind="derived",
                text="рСКФ не рассчитана: при беременности формула CKD-EPI не применяется."))
            return
        factor = float((cfg.analytes.get("creatinine", {}).get("units") or {}).get("mg/dl", _UMOL_PER_MGDL_CREATININE))
        egfr = round(ckd_epi_2021(values["creatinine"], norm.age_years, norm.sex, factor), 1)
        if not _in_range(egfr, spec.get("hard", [0, float("inf")])):
            norm.notes.append(NormalizationNote(
                analyte="eGFR", kind="soft_range",
                text=(f"рСКФ не рассчитана: по креатинину {fmt_num(values['creatinine'])} {cfg.unit_ru('creatinine')} "
                      "получается значение вне допустимого диапазона. Проверьте значение и единицу креатинина.")))
            return
        values["eGFR"] = egfr
        norm.notes.append(NormalizationNote(
            analyte="eGFR", kind="derived", value=egfr, to_unit=spec.get("unit", ""),
            text=(f"рСКФ рассчитана по креатинину {fmt_num(values['creatinine'])} {cfg.unit_ru('creatinine')}, "
                  f"возрасту и полу: {fmt_num(egfr)} {cfg.unit_ru('eGFR')}. Источник: {source}.")))


# --------------------------------------------------------------------------------------------
# Точка входа
# --------------------------------------------------------------------------------------------
def normalize_input(inp: AnalysisInput, cfg: Config) -> Normalized:
    """Ввод -> канонические значения. При ошибке ввода — InputError(code, message, field)."""
    sex = inp.sex
    status = inp.pregnancy.status
    trimester = inp.pregnancy.trimester
    if sex == "M" and status == "yes":
        raise InputError("pregnancy_male",
                         "Для мужчины указана беременность. Исправьте пол или уберите отметку о беременности.",
                         field="pregnancy")
    if sex == "M":
        status = "no"            # для мужчин «не знаю» не имеет смысла
    if status != "yes":
        trimester = None

    norm = Normalized(sex=sex, age_years=int(inp.age_years), pregnancy_status=status, trimester=trimester,
                      values={}, measured=set(), notes=[], norms_set=inp.norms)

    # Сначала значения с каноническими кодами, потом с алиасами: при повторе показателя канонический код важнее.
    items = sorted((inp.values or {}).items(), key=lambda kv: kv[0] not in cfg.analytes)
    for raw_key, raw in items:
        value, unit = _split_value(raw)
        if value is None or (isinstance(value, float) and math.isnan(value)):
            continue                                    # нет значения = анализ не сдан
        code = resolve_analyte(raw_key, cfg)
        if code is None:
            norm.notes.append(NormalizationNote(
                analyte=str(raw_key), kind="ignored",
                text=f"Показатель «{raw_key}» не распознан и не учтён."))
            continue
        spec = cfg.analytes[code]
        name, unit_ru = spec.get("name_ru", code), spec.get("unit_ru", "")
        if code in norm.measured:
            norm.notes.append(NormalizationNote(
                analyte=code, kind="ignored",
                text=f"{name} указан дважды: значение из поля «{raw_key}» не использовано."))
            continue
        try:
            original = float(value)
        except (TypeError, ValueError):
            raise InputError("out_of_range", f"{name}: значение «{value}» не является числом.", field=code) from None

        # --- единица ---
        unit_given = unit is not None and str(unit).strip() != ""
        multiplier = 1.0
        if unit_given:
            table = _unit_table(spec)
            key = unit_key(unit)
            if key not in table:
                raise InputError(
                    "unknown_unit",
                    f"{name}: единица «{unit}» не распознана. Допустимые единицы: {', '.join(_allowed_units(spec))}.",
                    field=code)
            multiplier = table[key]
        converted = round(original * multiplier, 6)

        # --- жёсткий диапазон: физически невозможное значение ---
        hard = spec.get("hard")
        if not math.isfinite(converted) or (hard and not _in_range(converted, hard)):
            hint = ""
            soft = spec.get("soft") or hard
            candidates = [u for u, m in _other_units(spec, multiplier) if soft and _in_range(original * m, soft)]
            if candidates:
                hint = f" Возможно, значение указано в «{candidates[0]}» — укажите единицу."
            bounds = f"{fmt_num(hard[0])}–{fmt_num(hard[1])} {unit_ru}" if hard else ""
            raise InputError(
                "out_of_range",
                f"{name}: значение {fmt_num(converted)} {unit_ru} вне допустимого диапазона {bounds}. "
                f"Проверьте значение и единицу измерения.{hint}",
                field=code)

        norm.values[code] = converted
        norm.measured.add(code)

        # --- заметки ---
        if unit_given and not math.isclose(multiplier, 1.0):
            norm.notes.append(NormalizationNote(
                analyte=code, kind="converted", original=original, value=converted,
                from_unit=str(unit).strip(), to_unit=spec.get("unit"),
                text=f"{name}: {fmt_num(original)} {str(unit).strip()} пересчитано в {fmt_num(converted)} {unit_ru}."))
        if not unit_given and spec.get("unit_ambiguous"):
            others = ", ".join(u for u, _ in _other_units(spec, 1.0))
            norm.notes.append(NormalizationNote(
                analyte=code, kind="unit_assumed", value=converted, to_unit=spec.get("unit"),
                text=(f"{name}: единица не указана, принято {unit_ru}. "
                      f"У этого показателя в ходу и другие единицы ({others}) — проверьте.")))
        soft = spec.get("soft")
        if soft and not _in_range(converted, soft):
            norm.notes.append(NormalizationNote(
                analyte=code, kind="soft_range", value=converted,
                text=(f"{name}: {fmt_num(converted)} {unit_ru} — нетипичное значение "
                      f"(обычно {fmt_num(soft[0])}–{fmt_num(soft[1])} {unit_ru}). Проверьте значение и единицу.")))

    if "hemoglobin" not in norm.values:
        raise InputError("missing_hemoglobin",
                         "Не указан гемоглобин: без него нельзя оценить анемию. Добавьте значение гемоглобина (г/л).",
                         field="hemoglobin")

    _add_derived(norm, cfg)
    # Референсы: проект по умолчанию (norms_ru.yaml → reference_ranges), поверх — набор лаборатории по имени
    # lab_reference (norms_ru.yaml → local_references) и референсы из запроса (reference_ranges). references.py.
    from .references import ranges_from_request, resolve
    norm.references = resolve(cfg, sex, status, lab_name=inp.lab_reference,
                              request_ranges=ranges_from_request(inp.reference_ranges, cfg))
    return norm
