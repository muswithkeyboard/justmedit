"""Разбор текста лабораторного бланка (вставленный текст, TXT, PDF, распознанное фото и скан): строки
«название — значение — единица — норма» -> значения показателей.

Что делает parse_lab_text:
  * текст режется на строки; в строке значение — первое число после названия (десятичная запятая или точка;
    «< 5» — число со знаком, знак уходит в замечание). Перед числом стоит название: оно сопоставляется со
    справочником config/analytes.yaml ТОЛЬКО целиком (io/lab_blank.match_name: регистр, «ё», скобки, кириллица
    и латиница одинакового вида не важны), иначе — по аббревиатуре в скобках «(HGB)». Подстрока не ищется:
    «Средний объём тромбоцитов» не станет MCV, «Гликированный гемоглобин» — гемоглобином;
  * порядок после значения любой: «значение единица норма», «значение норма единица»; норма — «120-150»,
    «120 – 150», «от 120 до 150», «< 5», «до 5», «> 30», «более 30»; если норма стоит раньше значения
    («120-150 110 г/л»), значением берётся следующее отдельное число. Пометки «↓ ↑ H L * !», «Ниже нормы»,
    «отклонение от нормы» отбрасываются и не путаются со значением;
  * многострочные названия склеиваются: строка без числа перед строкой со значением («Насыщение трансферрина» /
    «железом 12 %») и после неё («Среднее содержание гемоглобина в 27 пг» / «эритроците»), в том числе когда
    название разорвано вокруг строки значения (вертикальное выравнивание ячейки в PDF). Из всех склеек, которые
    целиком совпали с названием из справочника, берётся самая длинная;
  * единица ищется в остатке строки среди допустимых единиц показателя (analytes.yaml -> units; формы
    «×10⁹/л», «10*9/л», «109/л» -> «10^9/л»). Значение НЕ пересчитывается: в ответ идёт число как в бланке и
    написание единицы из analytes.yaml, пересчёт множителем делает normalize.py при расчёте. По жёсткому диапазону
    (hard) с учётом множителя единицы отсекаются заведомо чужие строки;
  * extended=True (ответ /parse): ещё reference_ranges (нормы с бланка в канонической единице), ignored
    (известные, но не используемые показатели — только название из справочника) и date (дата анализа, ISO);
    tables — строки таблиц PDF (io/lab_tables.py), разбираются раньше текста.

Защита от чужого значения (ревизия разбора PDF и текста):
  * «1 210» — одно число с разделителем тысяч; «141 102», «1.210» — по правилам lab_blank.number_reading
    (несколько результатов или неоднозначное число: значение не берётся, замечание);
  * сразу за значением ещё одно отдельное число или стрелка с числом («Гемоглобин 141 102 г/л»,
    «Ферритин 45 8 мкг/л», «128 → 135») — это предыдущий и текущий результаты: значение не берётся, замечание
    «несколько результатов в строке». Норма («117-160», «< 5»), степень десяти («10*9/л») и дата за значением —
    не второй результат;
  * вместо значения диапазон («Эритроциты 1-2») — значения нет;
  * строки мочи и кала (название «… в моче», «в п/зр», раздел «Общий анализ мочи» до раздела крови, единица
    «в п/зр» / «кл/мкл» за значением) не становятся показателями крови — lab_blank.material_of / section_of /
    foreign_unit; RDW в фл — это RDW-SD (lab_blank.reroute); метод СОЭ — lab_blank.esr_method.

Персональные данные: пол, возраст, ФИО, полис, номер заказа, исполнитель, учреждение не извлекаются. Текст строк
бланка в ответ НЕ возвращается (ревизия волны 3, этап 4а): в соседних строках и даже в строке с результатом бывают
ФИО, телефон, номер полиса или СНИЛС. Запись в unrecognized собирается только из распознанных частей — номер
строки и, если найдено, название показателя из справочника («строка 9: название показателя не распознано»,
«строка 11: Гемоглобин — значение вне диапазона»); в замечаниях — названия из справочника и знаки «<», «>»,
но не слова из бланка. Дата берётся только с подписью «дата взятия / выполнения / регистрации …», дата рождения —
никогда. Функция ничего не хранит и не пишет в журнал.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from ..config import Config
from . import lab_blank as lb

MAX_TEXT_CHARS = 20000          # больше — ошибка: это не бланк одного анализа
MAX_LINES = 400
MAX_UNRECOGNIZED = lb.MAX_UNRECOGNIZED
UNRECOGNIZED_WIDTH = lb.UNRECOGNIZED_WIDTH

# Число: целая часть и необязательная дробная через запятую или точку; или группы тысяч через пробел («1 210»,
# «12 500,5»): за последней группой не должны идти цифра, «/» («4 109/л» — это 4 и степень десяти) или тире с числом
# («245 150-400» — значение и норма). Перед числом не должно быть буквы, цифры, «^» или разделителя — иначе это часть
# названия («B12») или единицы («10^12/л»).
_RANGE_DASH = r"(?:-|–|—|−|‑|\.\.\.?|…)"
_NUMBER = re.compile(
    r"(?<![0-9A-Za-zА-Яа-яЁё^.,/])([<>≤≥]\s*)?"
    rf"(\d{{1,3}}(?:{lb.THOUSANDS_SEP}\d{{3}})+(?:[.,]\d+)?(?![\d.,]|\s*/|\s*{_RANGE_DASH}\s*\d)|\d+(?:[.,]\d+)?)"
    r"(?![0-9]*[A-Za-zА-Яа-яЁё]{2,}\d)")
_DATE = re.compile(r"\b\d{1,2}[./-]\d{1,2}[./-]\d{2,4}\b|\b\d{4}-\d{2}-\d{2}\b")
_LIST_MARKER = re.compile(r"^\s*(?:\d{1,2}[.)]\s+|[-–—•*·]\s*)")
_HAS_DIGIT = re.compile(r"\d")
# Стрелка между результатами: «128 → 135», «128 -> 135».
_ARROW_NUMBER = re.compile(r"^\s*(?:→|⟶|➔|➜|➝|⇒|->|=>|>>)\s*[<>≤≥]?\s*\d")


def _to_float(number: str) -> float:
    """«1 210,5» -> 1210.5 (пробелы-разделители тысяч убираются, запятая — десятичная)."""
    return float(re.sub(lb.THOUSANDS_SEP, "", number).replace(",", "."))


@dataclass
class _Split:
    name: str                       # текст до значения (или до нормы, стоящей перед значением)
    value: float | None             # None — вместо значения диапазон («Эритроциты 1-2 в п/зр»)
    sign: str | None
    rest: str                       # текст после значения (или после диапазона, если значения нет)
    head_range: tuple[float | None, float | None] | None
    raw: str = ""                   # число как в бланке: «1 210», «1.210» (для lab_blank.number_reading)


def _split_value(line: str) -> _Split | None:
    """Строка -> название, значение, остаток. None — в строке нет числа-значения (только название или заголовок)."""
    m = _NUMBER.search(line)
    if m is None:
        return None
    r = lb.RANGE.match(line, m.start())
    if r is not None and (r.group("a") is not None or r.group("c") is not None):
        # Норма стоит раньше значения («Гемоглобин 120-150 110 г/л»): значение — следующее отдельное число.
        m2 = _NUMBER.search(line, r.end())
        if m2 is not None and lb.RANGE.match(line, m2.start()) is None and not lb.POW_UNIT.match(line, m2.start()):
            return _Split(name=line[:m.start()], value=_to_float(m2.group(2)), sign=m2.group(1),
                          rest=line[m2.end():], head_range=lb.range_from_match(r), raw=m2.group(2))
        # Числа-значения после диапазона нет: вместо значения диапазон («1-2 в п/зр») — значения нет.
        return _Split(name=line[:m.start()], value=None, sign=None, rest=line[r.end():], head_range=None)
    return _Split(name=line[:m.start()], value=_to_float(m.group(2)), sign=m.group(1), rest=line[m.end():],
                  head_range=None, raw=m.group(2))


def second_result(rest: str) -> bool:
    """Сразу за значением — ещё один результат: отдельное число без единицы и нормы между ними («141 102 г/л»,
    «45 8 мкг/л») или стрелка с числом («128 → 135»). Норма («117-160», «< 5», «до 10»), степень десяти («10*9/л»,
    «x10^12/л») и дата за значением вторым результатом не считаются."""
    if _ARROW_NUMBER.match(rest):
        return True
    masked = lb.POW_UNIT.sub(lambda m: " " * len(m.group(0)), rest)
    s = lb.strip_flags(masked)
    pos = len(s) - len(s.lstrip())
    if _NUMBER.match(s, pos) is None:
        return False
    return lb.RANGE.match(s, pos) is None and _DATE.match(s, pos) is None


def _match(name: str, index) -> tuple[lb.Target | None, str | None]:
    """Название -> (показатель, единица, приписанная к названию через запятую: «Гемоглобин, г/л 124»)."""
    t = lb.match_name(name, index)
    if t is None:
        return None, None
    _, unit = lb.split_name_unit(lb.clean_name(name))
    return t, unit


def _masked_rest(rest: str) -> tuple[str, re.Match[str] | None, tuple | None]:
    """Остаток после значения -> (остаток без степени десяти, первой нормы и пометок; степень десяти; норма)."""
    masked = rest
    pow_m = lb.POW_UNIT.search(masked)
    if pow_m:                                       # «10^9/л» не должно читаться как часть нормы
        masked = masked[:pow_m.start()] + " " * (pow_m.end() - pow_m.start()) + masked[pow_m.end():]
    rng = None
    found = lb.find_range(masked)
    if found is not None:
        rng, (a, b) = found
        masked = masked[:a] + " " * (b - a) + masked[b:]
    return lb.strip_flags(masked), pow_m, rng


def _unit_and_range(rest: str, spec: dict) -> tuple[str | None, bool, tuple | None]:
    """Остаток строки после значения -> (единица из analytes.yaml | None, неизвестная единица?, норма | None)."""
    masked, pow_m, rng = _masked_rest(rest)
    if pow_m:
        unit, unknown = lb.unit_in_rest(pow_m.group(0), spec)
    else:
        unit, unknown = lb.unit_in_rest(masked, spec)
    return unit, unknown, rng


def unit_text(rest: str) -> str:
    """Остаток после значения без нормы и пометок — с чего начинается единица (для lab_blank.reroute, foreign_unit)."""
    masked, pow_m, _ = _masked_rest(rest)
    return pow_m.group(0) if pow_m and not masked.strip() else masked


def _sections(prepared: list[str]) -> tuple[list[str | None], set[int]]:
    """Раздел бланка для каждой строки: "моча" / "кал" — под заголовком раздела другого биоматериала до заголовка
    раздела крови; None — кровь или раздел не указан. Второе — номера строк-заголовков разделов."""
    out: list[str | None] = []
    headers: set[int] = set()
    current: str | None = None
    for i, line in enumerate(prepared):
        sec = lb.section_of(line) if line else None
        if sec is not None:
            current = None if sec == "кровь" else sec
            headers.add(i)
        out.append(current)
    return out, headers


def _parse_lines(lines: list[str], col: lb.Collector, index, *, report_no_number: bool, quiet: bool) -> None:
    prepared = [" ".join(_LIST_MARKER.sub("", raw).split()) for raw in lines]
    splits = [_split_value(line) if line else None for line in prepared]
    sections, headers = _sections(prepared)
    consumed: set[int] = set(headers)
    pending: list[int] = []                         # строки без значения сразу перед текущей (до двух)

    def free_name_line(j: int) -> bool:
        return 0 <= j < len(prepared) and j not in consumed and bool(prepared[j]) and splits[j] is None

    for i, line in enumerate(prepared):
        if i in consumed:
            pending = [] if i in headers else pending
            continue
        if not line:
            pending = []
            continue
        sp = splits[i]
        if sp is None:
            pending = (pending + [i])[-2:]
            continue
        where, order = f"строка {i + 1}", (1, i + 1)
        own = sp.name
        cands: list[tuple[list[int], str]] = [([], own)]
        pend = [j for j in pending if free_name_line(j)]
        if pend:
            cands.append(([pend[-1]], prepared[pend[-1]] + " " + own))
        if len(pend) == 2:
            cands.append((pend, prepared[pend[0]] + " " + prepared[pend[1]] + " " + own))
        if free_name_line(i + 1):
            cands.append(([i + 1], own + " " + prepared[i + 1]))
            if pend:
                cands.append(([pend[-1], i + 1], prepared[pend[-1]] + " " + own + " " + prepared[i + 1]))
        pending = []
        best: tuple[int, lb.Target, list[int], str | None, str] | None = None
        for used, name in cands:
            if not name.strip():
                continue
            t, name_unit = _match(name, index)
            if t is not None and (best is None or len(used) > best[0]):
                best = (len(used), t, used, name_unit, name)
        if best is None:
            material = sections[i] or lb.material_of(sp.name)
            if material:                                 # строка мочи или кала («Гемоглобин в моче 0,3»)
                col.ignore(lb.material_display(lb.material_target(sp.name, index), col.cfg, material), order,
                           where, quiet=quiet or bool(_DATE.search(line)))
            elif not quiet and not _DATE.search(line):   # строки с датами (в том числе с ФИО) отбрасываются
                col.remember(order, where, "название показателя не распознано")
            continue
        _, target, used, name_unit, name_text = best
        consumed.update(used)
        material = sections[i] or lb.material_of(name_text)
        if material:                                     # моча, кал: не показатель крови
            col.ignore(lb.material_display(target, col.cfg, material), order, where, quiet=quiet)
            continue
        target = lb.reroute(target, name_text, unit_text(sp.rest) if sp.value is not None else "")
        if target.kind == "ignored":
            col.ignore(target.code, order, where, quiet=quiet)
            continue
        code = target.code
        spec = col.cfg.analytes[code]
        if lb.foreign_unit(unit_text(sp.rest)) or (name_unit is not None and lb.foreign_unit(name_unit)):
            col.foreign(code, order, where)
            continue
        if sp.value is None:
            col.remember(order, where, f"{col.name(code)} — нет числа")
            continue
        if second_result(sp.rest):
            col.multiple(code, order, where)
            continue
        unit, unknown, rng = _unit_and_range(sp.rest, spec)
        if unit is None and not unknown and name_unit:
            unit, unknown = lb.unit_for_cell(name_unit, spec)
        col.add(code, sp.value, sign=sp.sign, unit=unit, unit_unknown=unknown, rng=sp.head_range or rng,
                order=order, where=where, lineno=i + 1, raw=sp.raw,
                method=lb.esr_method(name_text) if code == "ESR" else None)

    # Строка с названием показателя, но без числа: на фото так выглядит нечитаемое значение («Ферритин FFs мкг/л»),
    # молча терять его нельзя. В тексте и PDF — только если в строке есть цифры, но числа-значения нет.
    for i, line in enumerate(prepared):
        if i in consumed or not line or splits[i] is not None or quiet:
            continue
        if sections[i] or lb.material_of(line):
            continue
        if not (report_no_number or _HAS_DIGIT.search(line)):
            continue
        code = lb.prefix_analyte(line, index)
        if code is not None:
            col.remember((1, i + 1), f"строка {i + 1}", f"{col.name(code)} — нет числа")


def parse_lab_text(text: str, cfg: Config, *, line_numbers: bool = False, report_no_number: bool = False,
                   extended: bool = False, tables: list | None = None, table_above: list | None = None) -> dict:
    """Текст бланка -> {"values": {код: {"value": число, "unit": строка | None}}, "unrecognized": [...], "notes": [...]}.

    unit — написание единицы из config/analytes.yaml (его понимает normalize.py) или None, если единица
    не указана или не распознана: тогда при расчёте принимается каноническая единица показателя.
    line_numbers=True — ещё "lines": {код: номер строки (с 1)}, откуда взято значение (для пометки
    «проверьте значение» у неуверенно распознанных строк фото; наружу сервис это поле не отдаёт).
    report_no_number=True — строка с названием показателя, но без цифр, попадает в unrecognized
    («строка N: Ферритин — нет числа»): на фото так выглядит нечитаемое значение, молча терять его нельзя.
    extended=True — ещё "reference_ranges", "ignored", "date" (см. docs/CONTRACTS.md, ответ /parse).
    tables — таблицы PDF [[[ячейка, …], …], …]: разбираются раньше текста; если в таблицах нашлись показатели,
    нераспознанные строки текста вне таблиц (шапка бланка) в unrecognized не попадают.
    table_above — для каждой таблицы текст страницы над ней (io/pdf_text.py): по заголовку раздела («Общий анализ
    мочи») строки таблицы не сопоставляются с показателями крови. Дата анализа, если подписанной даты нет, — дата
    столбца результата в таблице динамики (Collector.date_hint).
    """
    if not isinstance(text, str):
        raise ValueError("text должен быть строкой")
    if len(text) > MAX_TEXT_CHARS:
        raise ValueError(f"текст длиннее {MAX_TEXT_CHARS} символов: вставьте только строки с результатами анализов")
    col = lb.Collector(cfg, extended=extended)
    index = lb.name_index(cfg)
    lines = text.splitlines()
    if len(lines) > MAX_LINES:
        col.notes.append(f"Разобраны первые {MAX_LINES} строк.")
        lines = lines[:MAX_LINES]
    table_lines: list[str] = []
    if tables:
        from .lab_tables import parse_tables
        leftover, table_lines = parse_tables(tables, col, index, above=table_above)
        lines = lines + leftover                     # таблицы без понятных столбцов — как строки текста
    _parse_lines(lines, col, index, report_no_number=report_no_number, quiet=bool(tables) and bool(col.values))
    date = (lb.find_date([*lines, *table_lines]) or col.date_hint) if extended else None
    return col.result(date=date, line_numbers=line_numbers)


def parse_value_cell(value_text: str) -> tuple[float | None, str | None, str, str] | None:
    """Ячейка «Результат» -> (число, знак, остаток ячейки, число как в бланке) или None, если числа нет («отр.»,
    «не обнаружено»). Вместо значения диапазон («1-2») — число None: значения нет."""
    text = str(value_text or "")
    m = _NUMBER.search(text)
    if m is None:
        return None
    r = lb.RANGE.match(text, m.start())
    if r is not None and (r.group("a") is not None or r.group("c") is not None):
        return None, None, text[r.end():], ""
    return _to_float(m.group(2)), m.group(1), text[m.end():], m.group(2)


def unit_and_range(rest: str, spec: dict) -> tuple[str | None, bool, tuple | None]:
    """Единица и норма в остатке строки или ячейки значения (для таблиц без столбцов единицы и нормы)."""
    return _unit_and_range(rest, spec)
