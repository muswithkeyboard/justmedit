"""Референсы («норма») показателей: референс проекта по умолчанию и локальные референсы лаборатории.

Откуда числа (решение капитана 04.10.2026, docs/review/medic_decisions.md, п. 10):
  * config/norms_ru.yaml → reference_ranges — референс проекта по умолчанию по сводной таблице команды
    (docs/facts/reference_table.md); у каждой строки — первоисточник из листа «Источники» (ключ primary), статус
    needs_medical_expert. Единицы — канонические единицы сервиса (config/analytes.yaml);
  * config/norms_ru.yaml → local_references.<имя> — референсы конкретной лаборатории; выбираются полем запроса
    lab_reference (тем же, что выбирает справочник ОАК скрининга);
  * поле запроса reference_ranges — референсы с бланка, присланные вместе с анализом (единица — любая допустимая
    для показателя по analytes.yaml; пересчитывается в каноническую).
Локальный референс заменяет референс по умолчанию только для своих показателей; при любом локальном референсе
в ответе и в отчёте врачу — предупреждение (текст — config/texts_ru/doctor.yaml → references.local_warning).

Референс — это не диагностический порог. Диагностические пороги (ВОЗ по Hb, ВОЗ 2020 по ферритину, КР, NICE NG239
по B12) остаются в своих записях norms_ru.yaml; референс используется для статусов таблицы значений («в пределах /
ниже / выше») и для чек-листа исключений (гемолиз, медь). Граница входит в референс: «ниже» — строго меньше нижней
границы, «выше» — строго больше верхней.
При беременности референсы проекта по умолчанию не применяются (reference_ranges.apply_in_pregnancy: false;
ответ врача 04.10.2026, п. 11: обычные взрослые референсы у беременных не применять); локальные референсы лаборатории
применяются: на бланке — референс для беременности или триместра.

Ответ врача 04.10.2026 (п. 11, вопрос 4): референс лаборатории заменяет значение по умолчанию для флагирования;
значение по умолчанию сохраняется для аудита (References.defaults) и в выводе не используется. Предупреждение —
всегда при локальном референсе; дополнительно — если граница отличается от значения по умолчанию больше чем на 10 %
(порог только интерфейсный, DIVERGENCE). Референс гаптоглобина по умолчанию — запасной (fallback: true): без
референса лаборатории вывод о гемолизе сопровождается предупреждением.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Optional

from .config import Config
from .normalize import fmt_num

LOCAL_WARNING_DEFAULT = ("Использованы референсные интервалы лаборатории. Они имеют приоритет над значениями "
                         "по умолчанию проекта; проверьте единицы, метод и возрастно-половую группу.")
DIVERGENCE = 0.10          # «отличается больше чем на 10 %» — порог интерфейса, не медицинский (ответ врача, п. 11)


@dataclass(frozen=True)
class RefRange:
    """Референс одного показателя в канонической единице. None — границы с этой стороны нет."""
    low: Optional[float]
    high: Optional[float]
    primary: Optional[str] = None       # ключ первоисточника в norms_ru.yaml → sources (только для референса проекта)
    local: bool = False                 # референс лаборатории, а не проекта
    fallback: bool = False              # запасной референс проекта (гаптоглобин): не универсальный норматив

    def below(self, value: Optional[float]) -> bool:
        return value is not None and self.low is not None and value < self.low

    def above(self, value: Optional[float]) -> bool:
        return value is not None and self.high is not None and value > self.high


@dataclass
class References:
    """Референсы, действующие для одного анализа (пол уже учтён)."""
    ranges: dict[str, RefRange] = field(default_factory=dict)
    lab_name: Optional[str] = None      # имя набора local_references или "request" (референсы из запроса)
    lab_title: Optional[str] = None
    local_analytes: list[str] = field(default_factory=list)
    defaults: dict[str, RefRange] = field(default_factory=dict)   # референсы проекта по умолчанию — для аудита

    @property
    def local(self) -> bool:
        return bool(self.local_analytes)

    def get(self, analyte: str) -> Optional[RefRange]:
        return self.ranges.get(analyte)


def _bound(x: Any) -> Optional[float]:
    if x is None:
        return None
    f = float(x)
    return f if math.isfinite(f) else None


def _parse_entry(entry: Any, sex: str) -> Optional[tuple[Optional[float], Optional[float]]]:
    """Строка раздела ranges -> (нижняя, верхняя) для пола. Формат: {range: [lo, hi]} или {F: [...], M: [...]}."""
    if not isinstance(entry, dict):
        return None
    pair = entry.get(sex) if sex in entry else entry.get("range")
    if not isinstance(pair, (list, tuple)) or len(pair) != 2:
        return None
    lo, hi = _bound(pair[0]), _bound(pair[1])
    if lo is None and hi is None:
        return None
    return lo, hi


def project_ranges(cfg: Config, sex: str) -> dict[str, RefRange]:
    """Референсы проекта по умолчанию для пола (norms_ru.yaml → reference_ranges.ranges)."""
    section = cfg.norms.get("reference_ranges") or {}
    out: dict[str, RefRange] = {}
    for analyte, entry in (section.get("ranges") or {}).items():
        pair = _parse_entry(entry, sex)
        if pair is not None:
            out[analyte] = RefRange(pair[0], pair[1], primary=entry.get("primary"), fallback=bool(entry.get("fallback")))
    return out


def _applies_in_pregnancy(cfg: Config) -> bool:
    return bool((cfg.norms.get("reference_ranges") or {}).get("apply_in_pregnancy", False))


def resolve(cfg: Config, sex: str, pregnancy_status: str = "no", lab_name: Optional[str] = None,
            request_ranges: Optional[dict[str, tuple[Optional[float], Optional[float]]]] = None) -> References:
    """Действующие референсы: проект по умолчанию, поверх — набор лаборатории из конфигурации (по имени), поверх —
    референсы из запроса. request_ranges уже в канонических единицах (см. ranges_from_request)."""
    refs = References()
    refs.defaults = project_ranges(cfg, sex)
    if pregnancy_status != "yes" or _applies_in_pregnancy(cfg):
        refs.ranges.update(refs.defaults)
    local_sets = cfg.norms.get("local_references") or {}
    if lab_name and lab_name != "default" and isinstance(local_sets.get(lab_name), dict):
        spec = local_sets[lab_name]
        for analyte, entry in (spec.get("ranges") or {}).items():
            pair = _parse_entry(entry, sex)
            if pair is not None:
                refs.ranges[analyte] = RefRange(pair[0], pair[1], local=True)
                refs.local_analytes.append(analyte)
        if refs.local_analytes:
            refs.lab_name, refs.lab_title = lab_name, str(spec.get("title") or lab_name)
    for analyte, (lo, hi) in (request_ranges or {}).items():
        refs.ranges[analyte] = RefRange(lo, hi, local=True)
        if analyte not in refs.local_analytes:
            refs.local_analytes.append(analyte)
        refs.lab_name = refs.lab_name or "request"
        refs.lab_title = refs.lab_title or "референсы с бланка / из настроек"
    return refs


def ranges_from_request(raw: Any, cfg: Config) -> dict[str, tuple[Optional[float], Optional[float]]]:
    """Поле запроса reference_ranges -> {канонический код: (нижняя, верхняя)} в канонической единице.

    Код показателя — как в values (код или синоним из analytes.yaml). Единица — любая допустимая для показателя;
    пересчёт тем же множителем, что и значения. Ошибки (неизвестный показатель или единица, нижняя граница больше
    верхней, обе границы пусты) — InputError: референс с бланка нельзя молча исправить."""
    from .normalize import _unit_table, resolve_analyte, unit_key
    from .schemas import InputError

    out: dict[str, tuple[Optional[float], Optional[float]]] = {}
    for key, spec in (raw or {}).items():
        code = resolve_analyte(key, cfg)
        field_name = f"reference_ranges.{key}"
        if code is None:
            raise InputError("unknown_analyte", f"Референс: показатель «{key}» не распознан.", field=field_name)
        low = getattr(spec, "low", None) if not isinstance(spec, dict) else spec.get("low")
        high = getattr(spec, "high", None) if not isinstance(spec, dict) else spec.get("high")
        unit = getattr(spec, "unit", None) if not isinstance(spec, dict) else spec.get("unit")
        name = cfg.name_ru(code)
        multiplier = 1.0
        if unit is not None and str(unit).strip():
            table = _unit_table(cfg.analytes[code])
            if unit_key(unit) not in table:
                raise InputError("unknown_unit", f"Референс показателя «{name}»: единица «{unit}» не распознана.",
                                 field=field_name)
            multiplier = table[unit_key(unit)]
        lo = None if low is None else round(float(low) * multiplier, 6)
        hi = None if high is None else round(float(high) * multiplier, 6)
        if (lo is None and hi is None) or any(x is not None and not math.isfinite(x) for x in (lo, hi)):
            raise InputError("invalid_reference", f"Референс показателя «{name}»: укажите нижнюю и/или верхнюю границу.",
                             field=field_name)
        if lo is not None and hi is not None and lo > hi:
            raise InputError("invalid_reference",
                             f"Референс показателя «{name}»: нижняя граница больше верхней.", field=field_name)
        out[code] = (lo, hi)
    return out


def references_for(norm: Any, cfg: Config) -> References:
    """Референсы анализа: посчитанные при нормализации (norm.references) или проект по умолчанию для пола."""
    refs = getattr(norm, "references", None)
    if isinstance(refs, References):
        return refs
    return resolve(cfg, norm.sex, getattr(norm, "pregnancy_status", "no"))


def range_text(r: RefRange, unit: str) -> str:
    """«120–250 Ед/л», «не ниже 350 пг/мл», «не выше 0,3 мкмоль/л»."""
    u = f" {unit}" if unit else ""
    if r.low is not None and r.high is not None:
        return f"{fmt_num(r.low)}–{fmt_num(r.high)}{u}"
    if r.low is not None:
        return f"не ниже {fmt_num(r.low)}{u}"
    return f"не выше {fmt_num(r.high)}{u}"


def ref_phrase(state: str, r: RefRange, unit: str) -> str:
    """Положение значения против референса словами, без склейки «выше референса не выше 10 мкмоль/л».

    state: low / high / within. Две границы — «ниже референса 120–250 Ед/л»; одна граница — «выше референса (верхняя
    граница 10 мкмоль/л)», «ниже референса (нижняя граница 350 пг/мл)», «в пределах референса (не выше 10 мкмоль/л)»."""
    word = {"low": "ниже", "high": "выше", "within": "в пределах"}[state]
    if r.low is not None and r.high is not None:
        return f"{word} референса {range_text(r, unit)}"
    u = f" {unit}" if unit else ""
    if state == "low" and r.low is not None:
        return f"ниже референса (нижняя граница {fmt_num(r.low)}{u})"
    if state == "high" and r.high is not None:
        return f"выше референса (верхняя граница {fmt_num(r.high)}{u})"
    return f"{word} референса ({range_text(r, unit)})"


def _differs(a: Optional[float], b: Optional[float]) -> bool:
    if a is None or b is None:
        return (a is None) != (b is None)
    base = abs(b) if b else 1.0
    return abs(a - b) / base > DIVERGENCE


def divergences(refs: References, cfg: Config) -> list[str]:
    """Локальные референсы, граница которых отличается от значения по умолчанию проекта больше чем на 10 %:
    «ЛДГ 100–300 Ед/л (по умолчанию проекта 135–214 Ед/л)»."""
    out = []
    for a in refs.local_analytes:
        loc, d = refs.ranges.get(a), refs.defaults.get(a)
        if loc is None or d is None:
            continue
        if _differs(loc.low, d.low) or _differs(loc.high, d.high):
            unit = cfg.unit_ru(a)
            out.append(f"{cfg.short_ru(a)} {range_text(loc, unit)} (по умолчанию проекта {range_text(d, unit)})")
    return out


def local_warning(cfg: Config, refs: Optional[References] = None) -> str:
    """Текст предупреждения о локальных референсах (config/texts_ru/doctor.yaml → references.local_warning) и, если
    передан refs, — список показателей, где референс лаборатории отличается от значения по умолчанию больше 10 %."""
    texts = ((cfg.texts or {}).get("doctor") or {}).get("references") or {}
    text = str(texts.get("local_warning") or LOCAL_WARNING_DEFAULT)
    diff = divergences(refs, cfg) if refs is not None else []
    if diff:
        lead = str(texts.get("divergence") or "Отличается от значений по умолчанию больше чем на 10 %: {items}.")
        text += " " + lead.format(items="; ".join(diff))
    return text
