"""Анализ всей истории анализов одного человека (поток П4, ADR 0009; контракт — docs/CONTRACTS.md, «Анализ истории»).

Чистая функция: на входе — записи кабинета [{"id", "date", "created"?, "input", "result"}], на выходе — JSON-совместимый
dict с рядами показателей, событиями динамики, «полнотой картины», последним выводом и строкой для шапки кабинета.
Модуль ничего не хранит и не пишет в логи. Каждая запись разбирается движком (engine.analyze),
если посчитанного ответа (кеша) нет; модели загружаются один раз на вызов (engine.load_models кеширует их на процесс).

Что считается:
  * порядок записей — (дата анализа, время создания записи created, порядок во входе): кабинет отдаёт записи уже в
    порядке (дата, created, rowid), а id — случайный uuid и порядка не несёт. Одинаковые записи одного дня (те же
    значения) схлопываются в одну (остаётся более поздняя): дубль не должен давать «подряд» и «изменение за один день»;
  * ряды (series) — канонические значения из таблицы значений ответа движка (единицы — config/analytes.yaml);
    изменение «последнее − первое» в единице показателя и в % от первого; направление up / down / flat / single,
    где flat — |изменение| меньше «шума» (norms_ru.yaml → history.flat); промежуток в месяцах (дни / 30,4375);
    в ряду — не больше history.limits.max_points последних точек (изменение и направление — по показанным точкам);
    ref — действующий референс последней записи (references.resolve: пол, беременность, референсы лаборатории);
    при беременности референсы проекта не применяются, поэтому ref = null, если бланк не дал свой;
  * события (events) — пороги norms_ru.yaml → history (выбор проекта, статус needs_medical_expert), ферритин —
    ещё и пороги ferritin.deficiency_below (30 — практика РФ, 15 — ВОЗ 2020; здесь не дублируются), Hb — порог
    анемии ВОЗ 2024 из ответа движка (level1.threshold_g_l: пол, беременность, триместр — hemoglobin в norms_ru.yaml);
  * полнота картины (completeness) — базовый набор BASE_SET: сдавался ли анализ и давно ли (history.stale);
  * строка шапки (summary) учитывает и события, и сам последний анализ: «срочно» (level1.urgent), анемия и сигналы
    скрытого дефицита в нём не дают «спокойного» хвоста;
  * тексты — config/texts_ru/history.yaml; врачу — числа, даты, источник; пациенту — без диагнозов, без чисел
    риска и «%», каждая строка проходит фильтр config/forbidden_patient_words.yaml.

Месяцы окон и сроков — календарные (дата + N месяцев), граница входит в окно. Записи с непонятной датой или
с ошибкой ввода (и без готового ответа) пропускаются; нечисловые и бесконечные значения (NaN, inf) в кешированном
ответе отбрасываются: история не должна падать из-за одной записи.
"""
from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any, Optional

from .config import Config, load_config
from .reports import NEUTRAL_PATIENT_LINE, Texts, cap, filter_patient_line
from .rules import norm_value

ROLES = ("patient", "doctor")
DAYS_PER_MONTH = 30.4375          # средняя длина месяца (365,25 / 12) — только для «months» ряда и фраз «за N мес»

# Порядок рядов: сначала главные для анемий и дефицитов, затем остальные — в порядке config/analytes.yaml.
SERIES_PRIORITY = ("hemoglobin", "ferritin", "MCV", "RDW", "MCH", "RBC", "hematocrit", "serum_iron", "TSAT",
                   "vitamin_B12", "folate", "CRP")

# Сигналы скрытого дефицита (rules.py → hidden_deficiency.rule_signals[].code), сведённые в группы для события
# persistent_signal: ферритин ниже порога ВОЗ и ниже порога практики — один и тот же сигнал «ферритин ниже порога»;
# «ферритин под маской воспаления» — отдельный сигнал (другой смысл и текст пациенту).
SIGNAL_GROUPS = {
    "ferritin_below_who": "iron_stores", "ferritin_below_practice": "iron_stores",
    "ferritin_masked_by_inflammation": "iron_masked",
    "tsat_low": "tsat", "tsat_low_normal_ferritin": "tsat",
    "ret_he_low": "ret_he",
    "b12_low": "b12", "b12_gray_zone": "b12",
    "active_b12_low": "active_b12",
    "folate_low": "folate",
    "b6_low": "b6",
    "copper_low": "copper",
}
# По какому показателю видно, что сигнал вообще мог появиться: анализы без этого показателя серию «подряд» не рвут.
GROUP_ANALYTE = {"iron_stores": "ferritin", "iron_masked": "ferritin", "tsat": "TSAT", "ret_he": "Ret_He",
                 "b12": "vitamin_B12", "active_b12": "active_B12", "folate": "folate", "b6": "vitamin_B6",
                 "copper": "copper"}

# Коды блока «срочно» (level1.py → level1.urgent[].code) -> показатель и ключ порога в norms_ru.yaml → urgent.
URGENT_ANALYTE = {"hemoglobin_very_low": ("hemoglobin", "hemoglobin_below"), "wbc_very_low": ("WBC", "wbc_below"),
                  "platelets_very_low": ("platelets", "platelets_below")}

SEVERITY_ORDER = {"attention": 0, "info": 1}
EVENT_ORDER = ("hb_drop", "anemia_new", "anemia_resolved", "hb_context_changed", "ferritin_drop", "mcv_drift",
               "persistent_signal", "screening_repeat_no_ferritin", "cbc_stale")

# Короткие подписи источников в текстах врачу (полные названия — norms_ru.yaml → sources).
SOURCE_SHORT = {"who_hb_2024": "ВОЗ 2024", "who_ferritin_2020": "ВОЗ 2020", "kr_ida_2024": "КР ЖДА 2024",
                "lab_reference": "общепринятый диапазон (проект)",
                "project": "решение команды проекта, ожидает подтверждения врачом"}


@dataclass(frozen=True)
class BaseItem:
    """Строка «полноты картины».

    code — код строки в ответе; markers — показатели, по которым строка считается сданной; price_items — из чего
    складывается цена (config/prices.yaml); suggest — коды next_tests, которые поднимают строку наверх."""
    code: str
    markers: tuple[str, ...]
    price_items: tuple[str, ...]
    suggest: tuple[str, ...]


# Базовый набор (план П4): ОАК (маркер — гемоглобин, цена — «ОАК с формулой»), ферритин, СРБ, железо и насыщение
# трансферрина одной строкой (код serum_iron; цена — железо + ОЖСС, НТЖ из них рассчитывается), B12, фолаты,
# креатинин (рСКФ считается по нему), ТТГ.
BASE_SET = (
    BaseItem("cbc", ("hemoglobin",), ("cbc",), ()),
    BaseItem("ferritin", ("ferritin",), ("ferritin",), ("ferritin",)),
    BaseItem("CRP", ("CRP",), ("CRP",), ("CRP",)),
    BaseItem("serum_iron", ("serum_iron", "TSAT"), ("serum_iron", "TIBC"),
             ("serum_iron", "TIBC", "UIBC", "TSAT", "transferrin")),
    BaseItem("vitamin_B12", ("vitamin_B12", "active_B12"), ("vitamin_B12",), ("vitamin_B12",)),
    BaseItem("folate", ("folate",), ("folate",), ("folate",)),
    BaseItem("creatinine", ("creatinine", "eGFR"), ("creatinine",), ("creatinine", "eGFR")),
    BaseItem("TSH", ("TSH",), ("TSH",), ("TSH",)),
)

MONTHS_NOM = ("", "январь", "февраль", "март", "апрель", "май", "июнь", "июль", "август", "сентябрь", "октябрь",
              "ноябрь", "декабрь")
MONTHS_GEN = ("", "января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября", "октября",
              "ноября", "декабря")
MONTHS_PREP = ("", "январе", "феврале", "марте", "апреле", "мае", "июне", "июле", "августе", "сентябре", "октябре",
               "ноябре", "декабре")


# --------------------------------------------------------------------------------------------
# Даты, числа и слова
# --------------------------------------------------------------------------------------------
def _today() -> date:
    """Сегодняшняя дата (отдельной функцией, чтобы тесты могли передать свою через параметр today)."""
    return date.today()


def _parse_date(x: Any) -> Optional[date]:
    if isinstance(x, datetime):
        return x.date()
    if isinstance(x, date):
        return x
    try:
        return date.fromisoformat(str(x).strip()[:10])
    except (TypeError, ValueError):
        return None


def _parse_created(x: Any) -> Optional[float]:
    """Время создания записи (поле created кабинета, ISO 8601 «2026-10-04T18:00:00Z») -> секунды Unix (UTC).
    Нет или непонятно — None: такая запись встаёт раньше записей того же дня с известным временем."""
    if x is None or isinstance(x, bool):
        return None
    if isinstance(x, (int, float)):
        return float(x) if math.isfinite(x) else None
    if isinstance(x, datetime):
        dt = x
    else:
        try:
            dt = datetime.fromisoformat(str(x).strip())       # Python 3.11+ понимает и суффикс «Z»
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.timestamp()


def _finite(x: Any) -> Optional[float]:
    """Конечное число или None (NaN, inf, строки и прочее мусорное — None)."""
    if isinstance(x, bool):
        return None
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def _add_months(d: date, months: int) -> date:
    """Дата + N календарных месяцев (31 января + 1 месяц = 28/29 февраля)."""
    y, m = divmod(d.month - 1 + int(months), 12)
    year, month = d.year + y, m + 1
    days_in = (date(year + (month == 12), month % 12 + 1, 1) - date(year, month, 1)).days
    return date(year, month, min(d.day, days_in))


def _within(early: date, late: date, months: Any) -> bool:
    """Ранний анализ попадает в окно «не дальше N месяцев до позднего» (граница входит)."""
    return months is not None and _add_months(early, int(months)) >= late


def _older_than(d: date, months: Any, today: date) -> bool:
    """Анализ «старше N месяцев» на сегодня: дата + N календарных месяцев раньше сегодняшней."""
    return months is not None and _add_months(d, int(months)) < today


def _full_months(early: date, late: date) -> int:
    """Целых календарных месяцев между датами."""
    n = (late.year - early.year) * 12 + late.month - early.month
    if late.day < early.day:
        n -= 1
    return max(n, 0)


def plural(n: int, one: str, few: str, many: str) -> str:
    """Форма слова после числа: 1 анализ, 3 анализа, 5 анализов (11–14 — «многие»)."""
    n = abs(int(n))
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


def _count_nom(n: int) -> str:
    return f"{n} {plural(n, 'анализ', 'анализа', 'анализов')}"


def _count_prep(n: int) -> str:
    """«в 2 анализах», «в 21 анализе»."""
    return f"{n} {plural(n, 'анализе', 'анализах', 'анализах')}"


def _months_word(n: int) -> str:
    return f"{n} {plural(n, 'месяц', 'месяца', 'месяцев')}"


def _period(d1: date, d2: date, role: str) -> str:
    """Промежуток: врачу «6 мес», пациенту «6 месяцев»; меньше месяца — «1 день», «20 дней» (обеим ролям);
    одна и та же дата — «0 дней» (во фразах вместо него — _span: «в тот же день»)."""
    days = (d2 - d1).days
    if days < 30:
        days = max(days, 0)
        return f"{days} {plural(days, 'день', 'дня', 'дней')}"
    months = max(1, int(round(days / DAYS_PER_MONTH)))
    return f"{months} мес" if role == "doctor" else _months_word(months)


def _span(d1: date, d2: date, role: str) -> str:
    """Обстоятельство времени для фраз: «за 6 мес», «за 1 день»; одна и та же дата — «в тот же день»."""
    if (d2 - d1).days <= 0:
        return "в тот же день"
    return f"за {_period(d1, d2, role)}"


def _ddmmyyyy(d: date) -> str:
    return d.strftime("%d.%m.%Y")


def _when(d: date) -> str:
    """«в марте 2025»."""
    return f"в {MONTHS_PREP[d.month]} {d.year}"


def _month_year(d: date) -> str:
    """«сентябрь 2026»."""
    return f"{MONTHS_NOM[d.month]} {d.year}"


def _range_ru(d1: date, d2: date) -> str:
    """«с марта по сентябрь 2026», «с марта 2025 по сентябрь 2026», «за сентябрь 2026»."""
    if (d1.year, d1.month) == (d2.year, d2.month):
        return f"за {_month_year(d2)}"
    if d1.year == d2.year:
        return f"с {MONTHS_GEN[d1.month]} по {MONTHS_NOM[d2.month]} {d2.year}"
    return f"с {MONTHS_GEN[d1.month]} {d1.year} по {MONTHS_NOM[d2.month]} {d2.year}"


def _prep(x: float) -> str:
    """Предлог перед числом: «со 132» (сто…), иначе «с 98», «с 45»."""
    return "со" if 100 <= abs(x) < 200 else "с"


def _num(x: float) -> str:
    from .normalize import fmt_num
    return fmt_num(x)


def _round(x: float, digits: int = 3) -> float:
    return float(round(float(x), digits))


def _lower_first(t: str) -> str:
    """Первая буква — строчная, если это не аббревиатура («Снижение…» -> «снижение…», «Hb …», «MCV …» — как есть)."""
    return t[:1].lower() + t[1:] if len(t) > 1 and t[1].islower() and not t[:2].isascii() else t


# --------------------------------------------------------------------------------------------
# Конфигурация раздела history и тексты
# --------------------------------------------------------------------------------------------
def _h(cfg: Config, *path: str) -> Any:
    """Значение из norms_ru.yaml → history. None — ключа нет: соответствующее событие, срок или ограничение не
    применяются (чисел по умолчанию в коде нет)."""
    node: Any = cfg.norms.get("history") or {}
    for key in path:
        if not isinstance(node, dict) or key not in node:
            return None
        node = node[key]
    return node


def _source(cfg: Config, key: Optional[str]) -> str:
    if not key:
        return SOURCE_SHORT["project"]
    return SOURCE_SHORT.get(key) or cfg.source_title(key)


class _Text:
    """Шаблоны config/texts_ru/history.yaml для одной роли. Строки пациенту — через фильтр запрещённых слов."""

    def __init__(self, cfg: Config, role: str):
        texts = cfg.texts or {}
        self.T = Texts(texts.get("history"))
        self.P = Texts(texts.get("patient"))
        self.role = role
        self.forbidden = tuple(cfg.forbidden_patient_words or ())
        self.neutral = str(self.P.raw("neutral_line") or NEUTRAL_PATIENT_LINE)

    def raw(self, path: str, default: Any = None) -> Any:
        return self.T.raw(path, default)

    def __call__(self, path: str, default: str = "", **kw: Any) -> str:
        text = self.T.t(path, **kw) or default
        return self.safe(text)

    def safe(self, text: str) -> str:
        text = " ".join(str(text or "").split())
        if self.role == "patient":
            text = filter_patient_line(text, self.forbidden, self.neutral) if text else self.neutral
        return text


# --------------------------------------------------------------------------------------------
# Записи
# --------------------------------------------------------------------------------------------
def _d(x: Any) -> dict:
    return x if isinstance(x, dict) else {}


@dataclass
class _Rec:
    id: str
    day: date
    inp: dict
    res: dict
    values: dict[str, float]      # канонические значения таблицы значений ответа (сданные и расчётные), конечные
    created: Optional[float] = None
    idx: int = 0                  # порядок во входе

    @property
    def _pregnancy(self) -> dict:
        """Блок беременности входа: dict {"status", "trimester"}; строка «yes» (старые или ручные записи) — статус."""
        p = self.inp.get("pregnancy")
        if hasattr(p, "model_dump"):
            p = p.model_dump(mode="json")
        if isinstance(p, dict):
            return p
        if isinstance(p, str):
            return {"status": p}
        return {}

    @property
    def pregnant(self) -> bool:
        return str(self._pregnancy.get("status") or "no").strip().lower() == "yes"

    @property
    def trimester(self) -> Optional[int]:
        try:
            t = int(self._pregnancy.get("trimester"))
        except (TypeError, ValueError):
            return None
        return t if t in (1, 2, 3) else None

    @property
    def sex(self) -> str:
        return str(self.inp.get("sex") or "F")

    @property
    def level1(self) -> dict:
        return _d(self.res.get("level1"))

    @property
    def hidden(self) -> dict:
        return _d(self.res.get("hidden_deficiency"))

    @property
    def screening(self) -> dict:
        return _d(self.hidden.get("screening"))

    @property
    def signals(self) -> list[dict]:
        return [s for s in (self.hidden.get("rule_signals") or []) if isinstance(s, dict)]

    @property
    def urgent(self) -> list[dict]:
        return [u for u in (self.level1.get("urgent") or []) if isinstance(u, dict)]

    @property
    def hb(self) -> Optional[float]:
        """Гемоглобин, г/л: из таблицы значений, иначе из level1."""
        v = self.values.get("hemoglobin")
        return v if v is not None else _finite(self.level1.get("hemoglobin"))

    @property
    def hb_threshold(self) -> Optional[float]:
        """Порог анемии ВОЗ 2024 для этой записи (пол, беременность, триместр), г/л — из ответа движка
        (level1.threshold_g_l; сами числа — norms_ru.yaml → hemoglobin)."""
        return _finite(self.level1.get("threshold_g_l"))


def _as_dict(x: Any) -> Any:
    return x.model_dump(mode="json") if hasattr(x, "model_dump") else x


def _prepare(records: Any, cfg: Config, models: Any) -> list[_Rec]:
    """Записи -> разобранные, отсортированные по (дата, created, порядок во входе), без дублей одного дня.

    Не больше history.limits.max_records последних записей (защита от огромной истории: движок считает только их).
    Нет ответа — считаем движком (модели — один раз)."""
    from . import engine
    from .schemas import AnalysisInput, InputError

    if records is None:
        return []
    if not isinstance(records, (list, tuple)):
        raise TypeError("records: ожидается список записей")
    raw_ok: list[tuple[date, Optional[float], int, dict, dict]] = []
    for idx, raw in enumerate(records):
        if not isinstance(raw, dict):
            continue
        day = _parse_date(raw.get("date"))
        inp = _as_dict(raw.get("input"))
        if day is None or not isinstance(inp, dict):
            continue
        raw_ok.append((day, _parse_created(raw.get("created")), idx, raw, inp))
    raw_ok.sort(key=lambda t: (t[0], -math.inf if t[1] is None else t[1], t[2]))
    max_records = _h(cfg, "limits", "max_records")
    if max_records is not None and len(raw_ok) > int(max_records):
        raw_ok = raw_ok[-int(max_records):]

    out: list[_Rec] = []
    for day, created, idx, raw, inp in raw_ok:
        res = _as_dict(raw.get("result"))
        if not isinstance(res, dict):
            if models is None:
                models = engine.load_models()
            try:
                res = engine.analyze(AnalysisInput.model_validate(inp), models=models, cfg=cfg).model_dump(mode="json")
            except (InputError, ValueError, TypeError):     # ValidationError pydantic — подкласс ValueError
                continue
        values: dict[str, float] = {}
        rows = res.get("values")
        for row in rows if isinstance(rows, list) else []:
            try:
                v = _finite(row["value"])
                if v is not None:
                    values[str(row["analyte"])] = v
            except (KeyError, TypeError):
                continue
        out.append(_Rec(id=str(raw.get("id", "")), day=day, inp=inp, res=res, values=values, created=created, idx=idx))

    # Дубли: та же дата и те же значения — одна запись (остаётся более поздняя по порядку).
    seen: dict[tuple, int] = {}
    keep: list[Optional[_Rec]] = []
    for r in out:
        key = (r.day, tuple(sorted((a, round(v, 6)) for a, v in r.values.items())))
        if key in seen:
            keep[seen[key]] = None
        seen[key] = len(keep)
        keep.append(r)
    return [r for r in keep if r is not None]


def _points(recs: list[_Rec], analyte: str, *, skip_pregnant: bool = False) -> list[tuple[_Rec, float]]:
    return [(r, r.values[analyte]) for r in recs if analyte in r.values and not (skip_pregnant and r.pregnant)]


def _distinct_days(recs: list[_Rec]) -> list[date]:
    out: list[date] = []
    for r in recs:
        if r.day not in out:
            out.append(r.day)
    return out


# --------------------------------------------------------------------------------------------
# Ряды
# --------------------------------------------------------------------------------------------
def _noise(cfg: Config, analyte: str, first: float) -> float:
    """«Шум» для направления flat (norms_ru.yaml → history.flat): абсолютный в единице показателя (abs), иначе
    доля первого значения (pct, затем default_pct), %."""
    absolute = (_h(cfg, "flat", "abs") or {}).get(analyte)
    if absolute is not None:
        return float(absolute)
    pct = (_h(cfg, "flat", "pct") or {}).get(analyte)
    if pct is None:
        pct = _h(cfg, "flat", "default_pct")
    return abs(first) * float(pct) / 100.0 if pct is not None else 0.0


def _refs(last: _Rec, cfg: Config):
    """Действующие референсы последней записи: пол, беременность, набор лаборатории и референсы с бланка."""
    from .references import ranges_from_request, resolve
    from .schemas import InputError

    try:
        request = ranges_from_request(last.inp.get("reference_ranges"), cfg)
    except (InputError, ValueError, TypeError, AttributeError):
        request = {}
    return resolve(cfg, last.sex, "yes" if last.pregnant else "no", lab_name=last.inp.get("lab_reference"),
                   request_ranges=request)


def _series_points(recs: list[_Rec], cfg: Config) -> dict[str, list[tuple[_Rec, float]]]:
    """Точки рядов по показателям (не больше history.limits.max_points последних на показатель)."""
    present: dict[str, list[tuple[_Rec, float]]] = {}
    for r in recs:                      # записи уже отсортированы — точки ряда тоже
        for a, v in r.values.items():
            present.setdefault(a, []).append((r, v))
    max_points = _h(cfg, "limits", "max_points")
    if max_points is not None:
        present = {a: pts[-int(max_points):] for a, pts in present.items()}
    return present


def _direction(cfg: Config, analyte: str, pts: list[tuple[_Rec, float]]) -> str:
    """Направление ряда: single — одна точка; flat — |последнее − первое| меньше «шума»; иначе up / down."""
    if len(pts) < 2:
        return "single"
    first, last = pts[0][1], pts[-1][1]
    delta = _round(last - first)
    if delta == 0 or abs(delta) < _noise(cfg, analyte, first):
        return "flat"
    return "up" if delta > 0 else "down"


def _series(recs: list[_Rec], cfg: Config) -> list[dict]:
    if not recs:
        return []
    present = _series_points(recs, cfg)
    order = [a for a in SERIES_PRIORITY if a in present]
    order += [a for a in cfg.analytes if a in present and a not in order]
    order += sorted(a for a in present if a not in order)
    refs = _refs(recs[-1], cfg)

    out = []
    for a in order:
        pts = present[a]
        (r0, first), (r1, last) = pts[0], pts[-1]
        n = len(pts)
        delta = _round(last - first) if n > 1 else None
        delta_pct = _round(100.0 * (last - first) / first, 1) if n > 1 and first else None
        rng = refs.get(a)
        out.append({
            "analyte": a, "name_ru": cfg.name_ru(a), "unit": cfg.unit_ru(a),
            "points": [{"date": r.day.isoformat(), "value": _round(v, 4), "record_id": r.id} for r, v in pts],
            "last": _round(last, 4), "delta": delta, "delta_pct": delta_pct, "direction": _direction(cfg, a, pts),
            "months": _round((r1.day - r0.day).days / DAYS_PER_MONTH, 1),
            "ref": None if rng is None else {"low": rng.low, "high": rng.high},
        })
    return out


# --------------------------------------------------------------------------------------------
# События
# --------------------------------------------------------------------------------------------
def _event(code: str, severity: str, day: date, analytes: list[str], title: str, text: str) -> dict:
    return {"code": code, "severity": severity, "date": day.isoformat(), "title": title, "text": text,
            "analytes": list(analytes)}


def _best_earlier(pts: list[tuple[_Rec, float]], window: Any, key) -> Optional[tuple[_Rec, float]]:
    """Ранняя точка в окне до последней, лучшая по key (при равенстве — более поздняя)."""
    if len(pts) < 2 or window is None:
        return None
    last_day = pts[-1][0].day
    cands = [(i, r, v) for i, (r, v) in enumerate(pts[:-1]) if _within(r.day, last_day, window)]
    if not cands:
        return None
    _, r, v = max(cands, key=lambda p: (key(p[2]), p[0]))
    return r, v


def _earlier_in_window(pts: list[tuple[_Rec, float]], window: Any) -> list[tuple[_Rec, float]]:
    """Точки до последней, попадающие в окно history.<событие>.window_months до последнего анализа."""
    if len(pts) < 2 or window is None:
        return []
    last_day = pts[-1][0].day
    return [(r, v) for r, v in pts[:-1] if _within(r.day, last_day, window)]


def _hb_drop(recs: list[_Rec], cfg: Config, tx: _Text, direction: str) -> Optional[dict]:
    """Снижение Hb (г/л) к последнему анализу. Пороги history.hb_drop — выбор проекта: снижение не меньше
    min_drop_g_l г/л ИЛИ не меньше min_drop_pct % от базы.

    База устойчива к «шуму»: МЕДИАНА предыдущих анализов в окне window_months (а не максимум — максимум шумного ряда
    ±5 г/л почти всегда на 10 г/л выше последнего значения). Согласование с направлением ряда (series.direction):
      * ряд down и снижение от медианы набрано — «снизился» (вариант window), «внимание»;
      * иначе снижение от ПРЕДЫДУЩЕГО анализа (в окне) набрано — «по сравнению с предыдущим анализом» (previous):
        «внимание», если набрано и от медианы, иначе «к сведению» (одиночный скачок в шумном ряду);
      * иначе (ряд up/flat за всю историю, но последнее ниже медианы окна) — «ниже, чем в предыдущих анализах»
        (median), «внимание»: слово «снизился» тут не пишется, чтобы не спорить с направлением ряда.
    Последний анализ при беременности — пациентке мягкий текст (снижение Hb при беременности часто связано
    с разведением крови), врачу — пометка про гемодилюцию."""
    min_g_l, min_pct = _h(cfg, "hb_drop", "min_drop_g_l"), _h(cfg, "hb_drop", "min_drop_pct")
    window = _h(cfg, "hb_drop", "window_months")
    if min_g_l is None and min_pct is None:
        return None
    pts = _points(recs, "hemoglobin")
    earlier = _earlier_in_window(pts, window)
    if not earlier:
        return None
    last_r, last_v = pts[-1]
    median = float(statistics.median(v for _, v in earlier))
    prev_r, prev_v = earlier[-1]

    def enough(base: float) -> bool:
        drop = base - last_v
        pct = 100.0 * drop / base if base > 0 else 0.0
        return drop > 0 and ((min_g_l is not None and drop >= float(min_g_l))
                             or (min_pct is not None and pct >= float(min_pct)))

    by_median, by_prev = enough(median), enough(prev_v)
    if by_median and direction == "down":
        kind, severity = "window", "attention"
    elif by_prev:
        kind, severity = "previous", ("attention" if by_median else "info")
    elif by_median:
        kind, severity = "median", "attention"
    else:
        return None

    if kind == "previous":
        base, ref_r, n = prev_v, prev_r, 1
    else:
        base, ref_r, n = median, earlier[0][0], len(earlier)
    drop = _round(base - last_v, 4)
    pct = _round(100.0 * drop / base, 4) if base > 0 else 0.0
    span = _span(ref_r.day, last_r.day, tx.role)
    pregnant_any = any(r.pregnant for r, _ in earlier) or last_r.pregnant
    if tx.role == "doctor":
        first_r, first_v = pts[0]
        kw: dict[str, Any] = {
            "prep": _prep(base), "from_value": _num(base), "to_value": _num(last_v), "span": span,
            "period": _period(ref_r.day, last_r.day, "doctor"), "drop": _num(drop), "drop_pct": f"{pct:.0f}",
            "date_from": _ddmmyyyy(ref_r.day), "date_to": _ddmmyyyy(last_r.day), "date_prev": _ddmmyyyy(prev_r.day),
            "n": n, "median": _num(median), "window": window, "min_g_l": _num(min_g_l or 0),
            "min_pct": _num(min_pct or 0), "first_value": _num(first_v), "date_first": _ddmmyyyy(first_r.day),
            "trend": tx.raw(f"events.hb_drop.doctor.trend_{direction}", direction),
            "source": _source(cfg, _h(cfg, "source"))}
        body_key = "text_window_one" if kind == "window" and n == 1 else f"text_{kind}"
        parts = [tx(f"events.hb_drop.doctor.{body_key}", f"Hb {_num(base)} → {_num(last_v)} г/л.", **kw)]
        if kind != "window" and direction in ("up", "flat"):
            parts.append(tx("events.hb_drop.doctor.trend_note", "", **kw))
        if kind == "previous" and not by_median:
            parts.append(tx("events.hb_drop.doctor.weak_note", "", **kw))
        parts.append(tx("events.hb_drop.doctor.rule", "", **kw))
        if pregnant_any:
            parts.append(tx("events.hb_drop.doctor.pregnancy_note", ""))
        title = tx(f"events.hb_drop.doctor.title_{kind}", "Снижение гемоглобина")
        text = tx.safe(" ".join(p for p in parts if p))
    else:
        title = tx(f"events.hb_drop.patient.title_{kind}")
        if last_r.pregnant:
            text = tx("events.hb_drop.patient.text_pregnancy")
        else:
            text = tx(f"events.hb_drop.patient.text_{kind}", span=span)
    return _event("hb_drop", severity, last_r.day, ["hemoglobin"], title, text)


def _hb_context(r: _Rec, tx: _Text) -> str:
    """Подпись контекста оценки Hb врачу: «женщины», «мужчины», «беременность, II триместр»."""
    if r.pregnant:
        t = r.trimester
        trimester = tx.raw(f"events.hb_context_changed.doctor.trimester_{t}" if t else
                           "events.hb_context_changed.doctor.trimester_unknown", "")
        return str(tx.raw("events.hb_context_changed.doctor.context_pregnant", "беременность")).format(
            trimester=trimester)
    return str(tx.raw(f"events.hb_context_changed.doctor.context_{r.sex}", r.sex))


def _anemia_change(recs: list[_Rec], cfg: Config, tx: _Text) -> Optional[dict]:
    """Смена критерия анемии ВОЗ 2024 (Hb строго ниже порога; порог — пол, беременность, триместр из norms_ru.yaml →
    hemoglobin, как в уровне 1) между предпоследним и последним анализом.

    Оба значения Hb сравниваются с порогом ПОСЛЕДНЕЙ записи: смена порога (например, другой триместр) сама по себе
    не даёт «появилась» / «ушла». anemia_new — Hb перешёл порог вниз (последний ниже предыдущего), anemia_resolved —
    вверх (последний выше предыдущего). Если между анализами сменился контекст оценки (беременность ↔ нет или пол),
    пороги разные и прямое сравнение условно: вместо «появилась / ушла» — событие hb_context_changed, и только если
    по своим порогам критерий анемии в двух анализах разный («внимание», если в последнем он есть)."""
    hb_recs = [r for r in recs if r.hb is not None and r.hb_threshold is not None]
    if len(hb_recs) < 2:
        return None
    prev, last = hb_recs[-2], hb_recs[-1]
    hb_prev, hb_last = float(prev.hb), float(last.hb)
    thr_prev, thr_last = float(prev.hb_threshold), float(last.hb_threshold)
    source = _source(cfg, (cfg.norms.get("hemoglobin") or {}).get("source"))
    changed = [k for k, differs in (("pregnancy", prev.pregnant != last.pregnant), ("sex", prev.sex != last.sex))
               if differs]
    if changed:
        a_prev, a_last = hb_prev < thr_prev, hb_last < thr_last
        if a_prev == a_last:
            return None
        what_key = "_".join(changed)
        if tx.role == "doctor":
            what = tx.raw(f"events.hb_context_changed.doctor.what_{what_key}", what_key)
            title = tx("events.hb_context_changed.doctor.title", "Сменился контекст оценки гемоглобина")
            text = tx("events.hb_context_changed.doctor.text",
                      f"Hb {_num(hb_prev)} → {_num(hb_last)} г/л; контекст оценки изменился.",
                      prev_hb=_num(hb_prev), last_hb=_num(hb_last), thr_prev=_num(thr_prev), thr_last=_num(thr_last),
                      ctx_prev=_hb_context(prev, tx), ctx_last=_hb_context(last, tx), what=what,
                      date_prev=_ddmmyyyy(prev.day), date_last=_ddmmyyyy(last.day), source=source,
                      status=tx.raw("events.hb_context_changed.doctor.status_yes" if a_last else
                                    "events.hb_context_changed.doctor.status_no", ""))
        else:
            what = tx.raw(f"events.hb_context_changed.patient.what_{what_key}", "")
            title = tx("events.hb_context_changed.patient.title")
            text = tx("events.hb_context_changed.patient.text", what=what)
        return _event("hb_context_changed", "attention" if a_last else "info", last.day, ["hemoglobin"], title, text)

    a_prev, a_last = hb_prev < thr_last, hb_last < thr_last
    if a_prev == a_last:
        return None
    # «Появилась» — только если Hb снизился, «поднялся до порога» — только если вырос (с одним порогом это и так
    # следует из перехода; проверка — страховка от несогласованного кеша).
    if (a_last and not hb_last < hb_prev) or (not a_last and not hb_last > hb_prev):
        return None
    code, severity = ("anemia_new", "attention") if a_last else ("anemia_resolved", "info")
    if tx.role == "doctor":
        note = ""
        if thr_prev != thr_last:
            note = " " + tx("events.anemia_threshold_note", "", ctx_last=_hb_context(last, tx),
                            ctx_prev=_hb_context(prev, tx), thr_prev=_num(thr_prev))
        title = tx(f"events.{code}.doctor.title", code)
        text = tx(f"events.{code}.doctor.text", f"Hb {_num(hb_prev)} → {_num(hb_last)} г/л.",
                  prev_hb=_num(hb_prev), last_hb=_num(hb_last), threshold=_num(thr_last),
                  date_prev=_ddmmyyyy(prev.day), date_last=_ddmmyyyy(last.day), source=source) + note.rstrip()
    else:
        title, text = tx(f"events.{code}.patient.title"), tx(f"events.{code}.patient.text")
    return _event(code, severity, last.day, ["hemoglobin"], title, text)


def _ferritin_drop(recs: list[_Rec], cfg: Config, tx: _Text) -> Optional[dict]:
    """Ферритин (мкг/л): от наибольшего значения в окне history.ferritin_drop.window_months до последнего —
    падение не меньше min_drop_pct % ИЛИ переход порога ferritin.deficiency_below (ru 30 — практика РФ,
    who 15 — ВОЗ 2020). Переход порога засчитывается, только если само снижение не меньше «шума» ферритина
    (history.flat, при history.ferritin_drop.crossing_beyond_flat: true): 31 → 29 мкг/л — колебание, а не событие
    (правило проекта, не решение врача).

    Воспаление: ферритин — белок острой фазы. Если СРБ выше crp.inflammation_above (ВОЗ 2020) в любой из двух
    сравниваемых точек — пациенту мягкий текст «на ферритин влияло воспаление», врачу — СРБ в обеих точках; если
    раннее значение было при воспалении, а последнее не ниже порога практики — только «к сведению».
    При беременности ферритин не оценивается (решение капитана 04.10.2026): такие анализы не участвуют, а если
    последний ферритин сдан при беременности — события нет."""
    min_pct, window = _h(cfg, "ferritin_drop", "min_drop_pct"), _h(cfg, "ferritin_drop", "window_months")
    all_pts = _points(recs, "ferritin")
    if not all_pts or all_pts[-1][0].pregnant:
        return None
    pts = _points(recs, "ferritin", skip_pregnant=True)
    best = _best_earlier(pts, window, key=lambda v: v)
    if best is None:
        return None
    (ref_r, ref_v), (last_r, last_v) = best, pts[-1]
    if ref_v <= last_v:
        return None
    pct = _round(100.0 * (ref_v - last_v) / ref_v, 4) if ref_v > 0 else 0.0
    thr_ru = norm_value(cfg, "ferritin", "deficiency_below", "ru")
    thr_who = norm_value(cfg, "ferritin", "deficiency_below", "who")
    beyond_noise = (not _h(cfg, "ferritin_drop", "crossing_beyond_flat")
                    or (ref_v - last_v) >= _noise(cfg, "ferritin", ref_v))
    crossed = [(k, float(t)) for k, t in (("ru", thr_ru), ("who", thr_who))
               if t is not None and ref_v >= float(t) > last_v] if beyond_noise else []
    if not crossed and not (min_pct is not None and pct >= float(min_pct)):
        return None
    below_practice = thr_ru is not None and last_v < float(thr_ru)
    severity = "attention" if below_practice or crossed else "info"

    crp_thr = norm_value(cfg, "crp", "inflammation_above")
    crp_ref, crp_last = ref_r.values.get("CRP"), last_r.values.get("CRP")
    inflamed_ref = crp_thr is not None and crp_ref is not None and crp_ref > float(crp_thr)
    inflamed_last = crp_thr is not None and crp_last is not None and crp_last > float(crp_thr)
    if inflamed_ref and not below_practice:
        severity = "info"           # раннее значение завышено воспалением, а сейчас ферритин не ниже порога практики

    if tx.role == "doctor":
        crossing = ""
        if crossed:
            items = " и ".join(tx("events.ferritin_drop.doctor.threshold_item", f"{_num(t)} мкг/л", value=_num(t),
                                  label=tx.raw(f"events.ferritin_drop.doctor.label_{k}", k)) for k, t in crossed)
            key = "crossing_many" if len(crossed) > 1 else "crossing"
            crossing = tx(f"events.ferritin_drop.doctor.{key}", f"; ниже порога {items}", thresholds=items)
        crp_note = ""
        if inflamed_ref or inflamed_last:
            def crp_text(v: Optional[float]) -> str:
                if v is None:
                    return str(tx.raw("events.ferritin_drop.doctor.crp_missing", "не сдан"))
                return tx("events.ferritin_drop.doctor.crp_value", f"{_num(v)} мг/л", value=_num(v))
            crp_note = " " + tx("events.ferritin_drop.doctor.crp_note", "", crp_from=crp_text(crp_ref),
                                crp_to=crp_text(crp_last), date_from=_ddmmyyyy(ref_r.day),
                                date_to=_ddmmyyyy(last_r.day), threshold=_num(crp_thr),
                                source=_source(cfg, norm_value(cfg, "crp", "source")))
        title = tx("events.ferritin_drop.doctor.title", "Снижение ферритина")
        text = tx("events.ferritin_drop.doctor.text", f"Ферритин {_num(ref_v)} → {_num(last_v)} мкг/л.",
                  prep=_prep(ref_v), from_value=_num(ref_v), to_value=_num(last_v),
                  span=_span(ref_r.day, last_r.day, "doctor"), period=_period(ref_r.day, last_r.day, "doctor"),
                  drop_pct=f"{pct:.0f}", date_from=_ddmmyyyy(ref_r.day), date_to=_ddmmyyyy(last_r.day),
                  crossing=crossing, min_pct=_num(min_pct or 0), window=window, source=_source(cfg, _h(cfg, "source")),
                  thr_ru=_num(thr_ru or 0), thr_who=_num(thr_who or 0), crp_note=crp_note.rstrip())
    elif inflamed_ref or inflamed_last:
        title = tx("events.ferritin_drop.patient.title_inflammation")
        text = tx("events.ferritin_drop.patient.text_inflammation")
    else:
        title, text = tx("events.ferritin_drop.patient.title"), tx("events.ferritin_drop.patient.text")
    return _event("ferritin_drop", severity, last_r.day, ["ferritin"], title, text)


def _mcv_drift(recs: list[_Rec], cfg: Config, tx: _Text) -> Optional[dict]:
    """MCV (фл): последнее значение против МЕДИАНЫ предыдущих анализов в окне history.mcv_drift.window_months —
    направление по тренду, а не от самой дальней точки; изменение не меньше min_change_fl: вниз — к микроцитозу,
    вверх — к макроцитозу. «Внимание», если последний MCV вне indices.mcv_micro_below / mcv_macro_above
    (лабораторный референс), иначе «к сведению»."""
    min_fl, window = _h(cfg, "mcv_drift", "min_change_fl"), _h(cfg, "mcv_drift", "window_months")
    pts = _points(recs, "MCV")
    if min_fl is None:
        return None
    earlier = _earlier_in_window(pts, window)
    if not earlier:
        return None
    last_r, last_v = pts[-1]
    base = float(statistics.median(v for _, v in earlier))
    ref_r, prev_r, n = earlier[0][0], earlier[-1][0], len(earlier)
    change = _round(last_v - base, 4)
    if abs(change) < float(min_fl):
        return None
    micro, macro = norm_value(cfg, "indices", "mcv_micro_below"), norm_value(cfg, "indices", "mcv_macro_above")
    outside = (micro is not None and last_v < float(micro)) or (macro is not None and last_v > float(macro))
    way = "down" if change < 0 else "up"
    span = _span(ref_r.day, last_r.day, tx.role)
    if tx.role == "doctor":
        title = tx("events.mcv_drift.doctor.title", "Сдвиг MCV")
        text = tx(f"events.mcv_drift.doctor.{'text' if n == 1 else 'text_median'}",
                  f"MCV {_num(base)} → {_num(last_v)} фл.",
                  verb=tx.raw(f"events.mcv_drift.doctor.verb_{way}", ""), prep=_prep(base), from_value=_num(base),
                  to_value=_num(last_v), span=span, period=_period(ref_r.day, last_r.day, "doctor"), n=n,
                  change=("+" if change > 0 else "−") + _num(abs(change)),
                  date_from=_ddmmyyyy(ref_r.day), date_to=_ddmmyyyy(last_r.day), date_prev=_ddmmyyyy(prev_r.day),
                  target=tx.raw(f"events.mcv_drift.doctor.target_{way}", ""), micro=_num(micro or 0),
                  macro=_num(macro or 0), min_fl=_num(min_fl), window=window, source=_source(cfg, _h(cfg, "source")))
    else:
        title = tx("events.mcv_drift.patient.title")
        text = tx(f"events.mcv_drift.patient.text_{way}", span=span)
    return _event("mcv_drift", "attention" if outside else "info", last_r.day, ["MCV"], title, text)


def _groups(rec: _Rec) -> dict[str, dict]:
    """Группы сигналов записи -> последний сигнал группы (code, text, source)."""
    out: dict[str, dict] = {}
    for s in rec.signals:
        g = SIGNAL_GROUPS.get(str(s.get("code")))
        if g:
            out[g] = s
    return out


def _persistent(recs: list[_Rec], cfg: Config, tx: _Text) -> list[dict]:
    """Один и тот же сигнал скрытого дефицита (группа SIGNAL_GROUPS) в последних анализах подряд — не меньше
    history.persistent_signal.min_records РАЗНЫХ дат (два анализа одного дня — один раз). Считаются анализы, где
    сдан показатель группы (GROUP_ANALYTE): анализ без ферритина серию по ферритину не рвёт и не продолжает. Разрыв
    между соседними анализами серии больше history.persistent_signal.max_gap_months — серия прерывается: через годы
    это уже не «подряд». При анемии и беременности движок сигналов скрытого дефицита не выдаёт (там действует
    основной вывод), поэтому такой анализ с показателем серию прерывает."""
    need = _h(cfg, "persistent_signal", "min_records")
    max_gap = _h(cfg, "persistent_signal", "max_gap_months")
    if need is None:
        return []
    groups_by_rec = [_groups(r) for r in recs]
    seen: list[str] = []
    for gr in groups_by_rec:
        seen += [g for g in gr if g not in seen]
    out = []
    for g in seen:
        analyte = GROUP_ANALYTE[g]
        streak: list[int] = []
        for i in range(len(recs) - 1, -1, -1):
            if analyte not in recs[i].values:
                continue
            if g not in groups_by_rec[i]:
                break
            if streak and max_gap is not None and not _within(recs[i].day, recs[streak[-1]].day, max_gap):
                break
            streak.append(i)
        streak.reverse()
        days = _distinct_days([recs[i] for i in streak])
        if len(days) < int(need):
            continue
        last_i = streak[-1]
        sig = groups_by_rec[last_i][g]
        count = _count_prep(len(days))
        if tx.role == "doctor":
            topic = tx.raw(f"events.persistent_signal.topics_doctor.{g}", g)
            title = tx("events.persistent_signal.doctor.title", topic, topic=topic)
            text = tx("events.persistent_signal.doctor.text", str(sig.get("text", "")), topic=topic, count=count,
                      dates=", ".join(_ddmmyyyy(d) for d in days), signal=str(sig.get("text", "")),
                      source=str(sig.get("source", "")))
        else:
            topic = tx.raw(f"events.persistent_signal.topics_patient.{g}", "")
            title = tx("events.persistent_signal.patient.title")
            text = tx("events.persistent_signal.patient.text", topic=topic, count=count) if topic else tx.safe("")
        out.append(_event("persistent_signal", "attention", recs[last_i].day, [analyte], title, text))
    return out


def _screening_repeat(recs: list[_Rec], cfg: Config, tx: _Text) -> Optional[dict]:
    """Риск скрининга по ОАК (hidden_deficiency.screening.risk) из history.screening_repeat.risks — не меньше чем в
    min_records анализах РАЗНЫХ дат, а ферритин ни разу не сдан ни в одной записи."""
    need, risks = _h(cfg, "screening_repeat", "min_records"), _h(cfg, "screening_repeat", "risks")
    if need is None or not risks:
        return None
    if any("ferritin" in r.values for r in recs):
        return None
    risky = [r for r in recs if r.screening.get("risk") in set(risks)]
    days = _distinct_days(risky)
    if len(days) < int(need):
        return None
    count = _count_prep(len(days))
    if tx.role == "doctor":
        words: list[str] = []
        for r in risky:
            w = str(r.screening.get("risk_ru") or r.screening.get("risk"))
            if w not in words:
                words.append(w)
        title = tx("events.screening_repeat_no_ferritin.doctor.title", "Риск по скринингу ОАК повторяется")
        text = tx("events.screening_repeat_no_ferritin.doctor.text", f"Риск по скринингу ОАК: {count}.",
                  risks=" / ".join(words), count=count, dates=", ".join(_ddmmyyyy(d) for d in days))
    else:
        title = tx("events.screening_repeat_no_ferritin.patient.title")
        text = tx("events.screening_repeat_no_ferritin.patient.text", count=count)
    return _event("screening_repeat_no_ferritin", "attention", risky[-1].day, ["ferritin"], title, text)


def _cbc_stale(recs: list[_Rec], cfg: Config, tx: _Text, today: date) -> Optional[dict]:
    """Последний ОАК (маркер — гемоглобин) старше history.stale.months календарных месяцев на сегодня."""
    months = _h(cfg, "stale", "months")
    pts = _points(recs, "hemoglobin")
    if not pts or not _older_than(pts[-1][0].day, months, today):
        return None
    last_r = pts[-1][0]
    ago_n = _full_months(last_r.day, today)
    if tx.role == "doctor":
        title = tx("events.cbc_stale.doctor.title", "ОАК давно не сдавали")
        text = tx("events.cbc_stale.doctor.text", f"Последний ОАК — {_ddmmyyyy(last_r.day)}.",
                  date=_ddmmyyyy(last_r.day), ago=f"{ago_n} мес", months=months, source=_source(cfg, _h(cfg, "source")))
    else:
        title = tx("events.cbc_stale.patient.title")
        text = tx("events.cbc_stale.patient.text", ago=_months_word(ago_n))
    return _event("cbc_stale", "info", last_r.day, ["hemoglobin"], title, text)


def _events(recs: list[_Rec], cfg: Config, tx: _Text, today: date, directions: dict[str, str]) -> list[dict]:
    if not recs:
        return []
    found = [_hb_drop(recs, cfg, tx, directions.get("hemoglobin", "single")), _anemia_change(recs, cfg, tx),
             _ferritin_drop(recs, cfg, tx), _mcv_drift(recs, cfg, tx), *_persistent(recs, cfg, tx),
             _screening_repeat(recs, cfg, tx), _cbc_stale(recs, cfg, tx, today)]
    events = [e for e in found if e]
    events.sort(key=lambda e: (SEVERITY_ORDER.get(e["severity"], 9), -date.fromisoformat(e["date"]).toordinal(),
                               EVENT_ORDER.index(e["code"])))
    return events


# --------------------------------------------------------------------------------------------
# Полнота картины
# --------------------------------------------------------------------------------------------
def _one_price(code: str, cfg: Config, region_prices: Optional[dict]) -> Optional[int]:
    """Цена анализа: из region_prices (код -> цена или {"price", "available"}), иначе config/prices.yaml (Москва).
    «В рознице недоступен» или цены нет — None."""
    if region_prices is not None:
        entry = region_prices.get(code)
        if isinstance(entry, dict):
            price, available = entry.get("price"), bool(entry.get("available", True))
        else:
            price, available = entry, True
    else:
        price, available = cfg.price(code)
    if not available or price is None:
        return None
    try:
        return int(round(float(price)))
    except (TypeError, ValueError):
        return None


def _price(item: BaseItem, cfg: Config, region_prices: Optional[dict]) -> Optional[int]:
    total = 0
    for code in item.price_items:
        p = _one_price(code, cfg, region_prices)
        if p is None:
            return None
        total += p
    return total


def _completeness(recs: list[_Rec], cfg: Config, tx: _Text, today: date,
                  region_prices: Optional[dict]) -> list[dict]:
    """Базовый набор: never — ни разу не сдавался; stale — последний старше history.stale.months (ферритин —
    history.stale.ferritin_low_months, если последний был ниже порога практики ferritin.deficiency_below.ru);
    ok — иначе. Порядок: сначала строки из next_tests последнего анализа (в их порядке; кроме ok), затем never,
    затем stale, затем ok; внутри группы — порядок BASE_SET."""
    months = _h(cfg, "stale", "months")
    low_months = _h(cfg, "stale", "ferritin_low_months")
    thr_ru = norm_value(cfg, "ferritin", "deficiency_below", "ru")
    next_tests = recs[-1].res.get("next_tests") if recs else None
    next_codes = [str(t.get("analyte")) for t in (next_tests if isinstance(next_tests, list) else [])
                  if isinstance(t, dict)]
    rows = []
    for idx, item in enumerate(BASE_SET):
        hits = [r for r in recs if any(m in r.values for m in item.markers)]
        last = hits[-1] if hits else None
        low_ferritin = False
        if last is None:
            status = "never"
        else:
            limit = months
            fv = last.values.get("ferritin") if item.code == "ferritin" else None
            if fv is not None and thr_ru is not None and low_months is not None and not last.pregnant \
                    and fv < float(thr_ru):
                limit, low_ferritin = low_months, True
            status = "stale" if _older_than(last.day, limit, today) else "ok"
        suggested = [next_codes.index(c) for c in item.suggest if c in next_codes]
        rank = min(suggested) if suggested and status != "ok" else None
        group = 0 if rank is not None else {"never": 1, "stale": 2, "ok": 3}[status]

        if tx.role == "doctor":
            why = tx.raw(f"completeness.doctor.why.{item.code}") or cfg.name_ru(item.code)
            if status == "never":
                state = tx("completeness.doctor.never", "Не сдавали.")
            elif status == "stale" and low_ferritin:
                state = tx("completeness.doctor.stale_ferritin_low", "", value=_num(last.values["ferritin"]),
                           date=_ddmmyyyy(last.day), threshold=_num(thr_ru), months=low_months)
            elif status == "stale":
                state = tx("completeness.doctor.stale", "", date=_ddmmyyyy(last.day), months=limit)
            else:
                state = tx("completeness.doctor.ok", "", date=_ddmmyyyy(last.day))
            extra = tx("completeness.doctor.suggested", "") if rank is not None else ""
        else:
            why = (tx.raw(f"completeness.patient.why.{item.code}") or tx.P.raw(f"sections.tests.why.{item.code}")
                   or tx.raw("completeness.patient.why_default") or "")
            when = _when(last.day) if last is not None else ""
            if status == "never":
                state = tx("completeness.patient.never")
            elif status == "stale" and low_ferritin:
                state = tx("completeness.patient.stale_ferritin_low", when=when)
            elif status == "stale":
                state = tx("completeness.patient.stale", when=when)
            else:
                state = tx("completeness.patient.ok", when=when)
            extra = tx("completeness.patient.suggested") if rank is not None else ""
        why = str(why).strip().rstrip(".")
        reason = tx.safe(" ".join(p for p in ((cap(why) + ".") if why else "", state, extra) if p))
        rows.append((group, rank if rank is not None else 0, idx, {
            "analyte": item.code,
            "name_ru": tx.raw(f"completeness.names.{item.code}") or cfg.name_ru(item.code),
            "status": status, "last_date": last.day.isoformat() if last is not None else None,
            "reason": reason, "priority": 0, "price_rub": _price(item, cfg, region_prices),
        }))
    rows.sort(key=lambda t: t[:3])
    out = []
    for i, (_, _, _, row) in enumerate(rows, start=1):
        row["priority"] = i
        out.append(row)
    return out


# --------------------------------------------------------------------------------------------
# Последний анализ и строка для шапки
# --------------------------------------------------------------------------------------------
def _latest(recs: list[_Rec], tx: _Text) -> Optional[dict]:
    if not recs:
        return None
    last = recs[-1]
    headline = str(_d(_d(last.res.get("reports")).get(tx.role)).get("headline") or "")
    return {"record_id": last.id, "date": last.day.isoformat(), "headline": tx.safe(headline) if headline else ""}


def _urgent_items(last: _Rec, cfg: Config, tx: _Text) -> str:
    """Врачу: что в последнем анализе попало в блок «срочно» — «гемоглобин 75 г/л (< 80 г/л)». Пороги — norms_ru.yaml
    → urgent (тот же раздел, что у движка); незнакомый код — пропускается."""
    urgent_cfg = _d(cfg.norms.get("urgent"))
    items: list[str] = []
    for u in last.urgent:
        spec = URGENT_ANALYTE.get(str(u.get("code")))
        if spec is None:
            continue
        analyte, key = spec
        value = last.values.get(analyte)
        if value is None:
            continue
        thr = urgent_cfg.get(key)
        if isinstance(thr, dict):
            thr = thr.get("pregnant" if last.pregnant else "default")
        thr = _finite(thr)
        unit = cfg.unit_ru(analyte)
        items.append(tx("summary.doctor.urgent_item", f"{cfg.name_ru(analyte).lower()} {_num(value)} {unit}",
                        name=cfg.name_ru(analyte).lower(), value=_num(value), unit=unit,
                        threshold=_num(thr) if thr is not None else "—"))
    return ", ".join(items)


def _latest_findings(last: _Rec, tx: _Text) -> str:
    """Врачу: отклонения последнего анализа для шапки — анемия ВОЗ (Hb, порог, степень) и сигналы скрытого дефицита."""
    items: list[str] = []
    l1 = last.level1
    if l1.get("anemia") and last.hb is not None and last.hb_threshold is not None:
        sev = str(l1.get("severity_ru") or "").strip()
        items.append(tx("summary.doctor.latest_anemia", "анемия", hb=_num(last.hb), threshold=_num(last.hb_threshold),
                        severity=f", {sev}" if sev else ""))
    if last.hidden.get("detected"):
        items.append(tx("summary.doctor.latest_hidden", "сигналы скрытого дефицита"))
    return ", ".join(items)


def _summary(recs: list[_Rec], events: list[dict], tx: _Text, cfg: Config) -> str:
    """Строка для шапки кабинета: «N анализов за период» + хвост. Хвост учитывает и события «внимание», и сам
    последний анализ: «срочно» (level1.urgent) — первым, пациенту теми же словами, что в отчёте одного анализа
    («Важно: обратитесь к врачу сегодня»); анемия или сигналы скрытого дефицита — «в последнем анализе есть
    отклонения»; «спокойный» хвост — только если нет ни событий «внимание», ни отклонений последнего анализа."""
    role = tx.role
    if not recs:
        return tx(f"summary.{role}.empty", "История пуста.")
    first, last_day = recs[0].day, recs[-1].day
    last = recs[-1]
    one = len(recs) == 1
    attention = [e for e in events if e["severity"] == "attention"]
    if one:
        head = tx(f"summary.{role}.head_one", _count_nom(1), date=_ddmmyyyy(last_day), when=_month_year(last_day))
    elif first == last_day and role == "doctor":
        head = tx("summary.doctor.head_same_day", _count_nom(len(recs)), count=_count_nom(len(recs)),
                  date=_ddmmyyyy(last_day))
    else:
        head = tx(f"summary.{role}.head_many", _count_nom(len(recs)), count=_count_nom(len(recs)),
                  range=_range_ru(first, last_day), date_from=_ddmmyyyy(first), date_to=_ddmmyyyy(last_day),
                  period=_period(first, last_day, "doctor"))
    urgent = bool(last.urgent)
    abnormal = bool(last.level1.get("anemia")) or bool(last.hidden.get("detected"))

    prefix = ""
    parts: list[str] = []
    if role == "doctor":
        if urgent:
            items = _urgent_items(last, cfg, tx)
            prefix = tx("summary.doctor.urgent" if items else "summary.doctor.urgent_plain",
                        "Срочно: последний анализ требует срочной очной оценки.", date=_ddmmyyyy(last_day), items=items)
        if attention:
            titles: list[str] = []
            for e in attention:
                t = _lower_first(e["title"])
                if t not in titles:
                    titles.append(t)
            parts.append(tx("summary.doctor.attention", "", titles=", ".join(titles)))
        findings = _latest_findings(last, tx) if abnormal else ""
        if findings:
            parts.append(tx("summary.doctor.latest_one" if one else "summary.doctor.latest", "",
                            date=_ddmmyyyy(last_day), items=findings))
    else:
        if urgent:
            prefix = str(tx.P.raw("headline.urgent_prefix") or "").strip()
            parts.append(tx("summary.patient.urgent_one" if one else "summary.patient.urgent", ""))
        if attention and abnormal and not urgent:
            parts.append(tx("summary.patient.attention_latest", ""))
        elif attention:
            parts.append(tx("summary.patient.attention", ""))
        elif abnormal and not urgent:
            parts.append(tx("summary.patient.latest_one" if one else "summary.patient.latest", ""))
    if not parts and not urgent and not abnormal:
        parts.append(tx(f"summary.{role}.single" if one else f"summary.{role}.calm", ""))
    tail = "; ".join(p for p in parts if p)
    line = f"{head}; {tail}" if tail else head
    return tx.safe(f"{prefix} {line}" if prefix else line)


# --------------------------------------------------------------------------------------------
# Точка входа
# --------------------------------------------------------------------------------------------
def analyze(records: list[dict], *, cfg: Optional[Config] = None, models: Any = None, role: str = "patient",
            region_prices: Optional[dict] = None, today: Any = None) -> dict:
    """История анализов -> ответ по контракту docs/CONTRACTS.md («Анализ истории»).

    records — [{"id", "date": "YYYY-MM-DD", "created"?: ISO 8601, "input": AnalysisInput-dict,
    "result": AnalysisResult-dict | None}]; result=None — запись считается движком; created (необязательно) — время
    создания записи в кабинете: порядок записей одного дня. role — "patient" или "doctor" (тексты событий, причин,
    шапки). region_prices — цены региона {код: цена | {"price", "available"}}; None — config/prices.yaml (Москва).
    today — дата «сегодня» для сроков «давно» (по умолчанию — системная дата; параметр нужен тестам)."""
    if role not in ROLES:
        raise ValueError("role: patient или doctor")
    cfg = cfg or load_config()
    day = _parse_date(today) if today is not None else _today()
    if day is None:
        raise ValueError("today: дата в формате YYYY-MM-DD")
    recs = _prepare(records, cfg, models)
    tx = _Text(cfg, role)
    series = _series(recs, cfg)
    events = _events(recs, cfg, tx, day, {s["analyte"]: s["direction"] for s in series})
    return {
        "role": role,
        "n_records": len(recs),
        "first_date": recs[0].day.isoformat() if recs else None,
        "last_date": recs[-1].day.isoformat() if recs else None,
        "series": series,
        "events": events,
        "completeness": _completeness(recs, cfg, tx, day, region_prices),
        "latest": _latest(recs, tx),
        "summary": _summary(recs, events, tx, cfg),
    }


__all__ = ["analyze", "BASE_SET", "SIGNAL_GROUPS", "SERIES_PRIORITY", "plural"]
