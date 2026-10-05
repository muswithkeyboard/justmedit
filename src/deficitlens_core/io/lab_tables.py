"""Таблицы PDF-бланка любой лаборатории (pdfplumber extract_tables в io/pdf_text.py) -> значения показателей.

Что делает parse_tables:
  * ячейки чистятся: перенос слова «гемогло-\\nбина» склеивается, перевод строки внутри ячейки — пробел;
  * столбцы определяются по словам заголовка в любом порядке:
      название — тест / исследование / показатель / наименование / анализ / параметр;
      значение — результат / значение;
      норма    — норма / референс / референсные (нормальные) значения / границы / интервал;
      единица  — ед. / единицы / ед. изм. / размерность;
    прочие столбцы («Отклонение», «Критичность», «Комментарий», «Метод») не используются;
  * столбцы прошлых результатов («Предыдущий результат», «Прошлый», «Ранее», «Пред.», «История», «Динамика»)
    роль значения не получают, даже если левее столбца «Результат» (ревизия разбора, H2);
  * таблица динамики — в заголовке вместо «Результат» несколько дат («12.01.2025 | 05.02.2026»): значение берётся
    из столбца с последней датой (замечание), эта дата — дата анализа, если подписанной даты в бланке нет; если
    последнюю дату не определить — значение не берётся, замечание «несколько результатов»;
  * таблица без заголовка: если столбцов столько же, сколько у предыдущей таблицы с заголовком, — это её
    продолжение на следующей странице (ЕМИАС), столбцы те же; иначе столбцы определяются по содержимому:
    числа — значение, диапазоны — норма, единицы — единица, слова — название;
  * строка с названием, но без значения, — продолжение названия (ячейка разорвана на две строки таблицы)
    или заголовок раздела: склеивается со следующей строкой без названия;
  * название сопоставляется со справочником строго (io/lab_blank.match_name), единица — по столбцу единицы
    (или по остатку ячейки значения), норма — по столбцу нормы. Дальше — тот же сбор, что у строк текста
    (lab_blank.Collector): жёсткий диапазон, пересчёт нормы в каноническую единицу, известные неиспользуемые
    показатели — в ignored;
  * другой биоматериал: таблица под заголовком «Общий анализ мочи / ОАМ / Моча / Осадок» (текст страницы над
    таблицей — above, или строка-заголовок внутри таблицы) до заголовка раздела крови, название «… в моче»,
    единица «в п/зр» / «кл/мкл» — не показатели крови (lab_blank.section_of, material_of, foreign_unit);
  * число «1 210» — одно число с разделителем тысяч; «1.210» и «141 102» — lab_blank.number_reading; в ячейке
    значения два результата («128 → 135») или диапазон («1-2») — значение не берётся.
Таблицы, где столбцы понять не удалось (один столбец, нет столбца с числами), возвращаются строками текста —
их разбирает text_parser. Текст ячеек в ответ не попадает: только коды, названия из справочника и числа.
"""
from __future__ import annotations

import re

from . import lab_blank as lb

MAX_TABLE_ROWS = 400
MAX_COLUMNS = 20

_ROLE_WORDS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("range", re.compile(r"норм|референс|\bреф\b|reference|\bref\b|границ|интервал|диапазон", re.IGNORECASE)),
    ("unit", re.compile(r"^ед\b|^ед\.|единиц|ед\.?\s*изм|размерност|^units?\b", re.IGNORECASE)),
    ("value", re.compile(r"результат|значени|^result|^value", re.IGNORECASE)),
    ("name", re.compile(r"тест|исследовани|показател|наименовани|анализ|назван|параметр|^test|^parameter|^analyte",
                        re.IGNORECASE)),
)
_NUMBER_CELL = re.compile(r"^\s*[<>≤≥]?\s*(?:\d{1,3}(?:" + lb.THOUSANDS_SEP + r"\d{3})+(?:[.,]\d+)?|\d+(?:[.,]\d+)?)"
                          r"\s*(?:[↑↓▲▼*!]|\b[HL]\b)*\s*$")
# Столбец прошлых результатов: роль значения не получает («Предыдущий результат», «Дата предыдущего результата»).
_PREV_WORDS = re.compile(r"предыдущ|прошл|\bранее\b|\bпред\.|истори|динамик|previous|prior\b|\bprev\b", re.IGNORECASE)
_HEADER_DATE = re.compile(r"(?<!\d)(\d{1,2})[./-](\d{1,2})[./-](\d{4}|\d{2})(?!\d)|(?<!\d)(\d{4})-(\d{2})-(\d{2})(?!\d)")
_UNIT_CELL = re.compile(r"/|%|‰|^(?:фл|пг|fl|pg)$", re.IGNORECASE)
_LETTERS = re.compile(r"[A-Za-zА-Яа-яЁё]")


def clean_cell(cell) -> str:
    """Ячейка -> одна строка: перенос слова по дефису склеивается, перевод строки — пробел."""
    s = "" if cell is None else str(cell)
    s = re.sub(r"(?<=[а-яёa-z])-\s*\n\s*(?=[а-яёa-z])", "", s)
    return " ".join(s.split())


def _roles(row: list[str]) -> dict[str, int]:
    """Роли столбцов по словам заголовка: {"name": 0, "value": 1, "range": 2, "unit": 5}. Столбец прошлого
    результата — роль "prev" (не значение)."""
    roles: dict[str, int] = {}
    for i, cell in enumerate(row):
        low = cell.casefold().replace("ё", "е")
        if not low or len(low) > 60:
            continue
        if _PREV_WORDS.search(low):
            roles.setdefault("prev", i)
            continue
        for role, rx in _ROLE_WORDS:
            if rx.search(low):
                roles.setdefault(role, i)
                break
    return roles


def _header_dates(row: list[str]) -> list[tuple[int, object]]:
    """Ячейки заголовка — даты (таблица динамики «Тест | 12.01.2025 | 05.02.2026»): [(столбец, дата | None)].
    Ячейка — только дата (допускается «г.»); «Дата взятия 05.02.2026» и «Дата предыдущего результата» не считаются."""
    import datetime as dt
    out: list[tuple[int, object]] = []
    for i, cell in enumerate(row):
        m = _HEADER_DATE.search(cell or "")
        if m is None or len(cell) > 40 or len(_LETTERS.findall(_HEADER_DATE.sub("", cell))) > 2:
            continue
        try:
            if m.group(1) is not None:
                y = int(m.group(3))
                d = dt.date(y + 2000 if y < 100 else y, int(m.group(2)), int(m.group(1)))
            else:
                d = dt.date(int(m.group(4)), int(m.group(5)), int(m.group(6)))
        except ValueError:
            d = None
        out.append((i, d))
    return out


def _is_header(roles: dict[str, int]) -> bool:
    return "value" in roles and len(roles) >= 2


def _by_content(rows: list[list[str]], ncols: int) -> dict[str, int] | None:
    """Столбцы по содержимому: доля чисел, диапазонов, единиц и слов в каждом столбце."""
    if ncols < 2 or not rows:
        return None
    share = {k: [0.0] * ncols for k in ("num", "range", "unit", "text")}
    for c in range(ncols):
        cells = [r[c] for r in rows if c < len(r) and r[c]]
        if not cells:
            continue
        n = len(rows)
        share["num"][c] = sum(bool(_NUMBER_CELL.match(x)) for x in cells) / n
        share["range"][c] = sum(lb.find_range(x) is not None and not _NUMBER_CELL.match(x) for x in cells) / n
        share["unit"][c] = sum(bool(_UNIT_CELL.search(x)) and len(x) <= 16 and lb.find_range(x) is None
                               for x in cells) / n
        share["text"][c] = sum(len(_LETTERS.findall(x)) >= 3 and not _UNIT_CELL.search(x) for x in cells) / n
    roles: dict[str, int] = {}
    name = max(range(ncols), key=lambda c: (share["text"][c], -c))
    if share["text"][name] < 0.5:
        return None
    roles["name"] = name
    others = [c for c in range(ncols) if c != name]
    value = max(others, key=lambda c: (share["num"][c], -c))
    if share["num"][value] < 0.5:
        return None
    roles["value"] = value
    rest = [c for c in others if c != value]
    if rest:
        rng = max(rest, key=lambda c: (share["range"][c], -c))
        if share["range"][rng] >= 0.3:
            roles["range"] = rng
            rest = [c for c in rest if c != rng]
    if rest:
        unit = max(rest, key=lambda c: (share["unit"][c], -c))
        if share["unit"][unit] >= 0.3:
            roles["unit"] = unit
    return roles


def _header(rows: list[list[str]]) -> tuple[dict[str, int] | None, int, list[int], object, int]:
    """Заголовок таблицы в первых строках -> (роли столбцов | None, номер первой строки данных, столбцы с
    несколькими результатами без ясного последнего, дата столбца результата | None, число столбцов-дат)."""
    for k in range(min(3, len(rows))):
        cand = _roles(rows[k])
        dates = _header_dates(rows[k])
        if not (_is_header(cand) or len(dates) >= 2
                or (dates and "name" in cand and ("unit" in cand or "range" in cand))):
            continue
        if "value" in cand or not dates:
            return cand, k + 1, [], None, 0
        # Таблица динамики: значение — столбец с последней датой; прочие даты — прошлые результаты.
        known = [(d, i) for i, d in dates if d is not None]
        latest = max(known) if known else None
        if latest is None or sum(1 for d, _ in known if d == latest[0]) > 1 or len(known) < len(dates):
            first = dates[0][0]
            cand["value"] = first
            return cand, k + 1, [i for i, _ in dates], None, len(dates)
        cand["value"] = latest[1]
        for i, _ in dates:
            if i != latest[1]:
                cand.setdefault("prev", i)
        return cand, k + 1, [], latest[0], len(dates)
    return None, 0, [], None, 0


def parse_tables(tables: list, col: lb.Collector, index, above: list | None = None) -> tuple[list[str], list[str]]:
    """Таблицы -> значения в col. Возвращает (строки таблиц без понятных столбцов — разобрать как текст,
    все строки таблиц одной строкой каждая — для поиска даты анализа в шапке-таблице). above — текст страницы
    над каждой таблицей (заголовки разделов «Общий анализ мочи» / «Общий анализ крови»)."""
    from .text_parser import parse_value_cell, second_result, unit_and_range, unit_text

    leftover: list[str] = []
    all_lines: list[str] = []
    prev_roles: dict[str, int] | None = None
    prev_multi: list[int] = []
    prev_header: list[str] | None = None
    prev_ncols = 0
    row_no = 0
    total_rows = 0
    material: str | None = None                      # раздел другого биоматериала ("моча", "кал") или None
    for t_no, table in enumerate(tables or []):
        for line in str((above or [])[t_no] if t_no < len(above or []) else "").splitlines():
            sec = lb.section_of(line)
            if sec is not None:
                material = None if sec == "кровь" else sec
        rows = [[clean_cell(c) for c in (r or [])][:MAX_COLUMNS] for r in (table or []) if isinstance(r, list)]
        rows = [r for r in rows if any(r)]
        if not rows:
            continue
        if total_rows + len(rows) > MAX_TABLE_ROWS:
            rows = rows[:max(0, MAX_TABLE_ROWS - total_rows)]
            col.notes.append(f"В таблицах PDF разобраны первые {MAX_TABLE_ROWS} строк.")
        total_rows += len(rows)
        all_lines.extend(" ".join(c for c in r if c) for r in rows)
        ncols = max(len(r) for r in rows)
        rows = [r + [""] * (ncols - len(r)) for r in rows]
        roles, start, multi, latest, n_dates = _header(rows)
        header_row = rows[start - 1] if start else None
        if roles is None and prev_roles is not None and ncols == prev_ncols:
            roles, multi = dict(prev_roles), list(prev_multi)   # продолжение таблицы на следующей странице
        if roles is None:
            roles = _by_content(rows, ncols)
        if roles is None or "value" not in roles:
            leftover.extend(" ".join(c for c in r if c) for r in rows)
            continue
        if "name" not in roles:
            roles["name"] = min(c for c in range(ncols) if c not in roles.values())
        if latest is not None:
            col.date_hint = col.date_hint or latest.isoformat()
        if latest is not None and n_dates >= 2:
            col.notes.append("В таблице результаты за несколько дат — взяты значения из столбца с последней датой. "
                             "Проверьте.")
        if header_row is not None:
            prev_header = header_row
        prev_roles, prev_multi, prev_ncols = roles, multi, ncols
        # Заголовок, повторённый на странице (в том числе заголовок с датами таблицы динамики).
        body = [r for r in rows[start:] if not _is_header(_roles(r)) and r != prev_header]
        name_only = [bool(r[roles["name"]]) and not r[roles["value"]] for r in body]
        used: set[int] = set()
        pending = ""
        for k, r in enumerate(body):
            if k in used:
                continue
            name = r[roles["name"]]
            value_text = r[roles["value"]]
            if not value_text and not multi:
                sec = lb.section_of(name) if name and not any(c for i, c in enumerate(r) if i != roles["name"]) \
                    else None
                if sec is not None:                  # строка-заголовок раздела внутри таблицы
                    material = None if sec == "кровь" else sec
                    pending = ""
                    continue
                pending = (pending + " " + name).strip() if name else pending
                continue
            row_no += 1
            where, order = f"строка таблицы {row_no}", (0, row_no)
            # Своё название; название, начатое в строке выше; продолженное в строке ниже (ячейка разорвана).
            # Из совпавших целиком склеек берётся самая длинная.
            nxt = body[k + 1][roles["name"]] if k + 1 < len(body) and name_only[k + 1] else ""
            target, full_name, best_len, take_next = None, name, 0, False
            for chunks, takes_next in (((name,), False), ((pending, name), False), ((name, nxt), True),
                                       ((pending, name, nxt), True)):
                chunks = tuple(c for c in chunks if c)
                if (takes_next and not nxt) or not chunks or len(chunks) <= best_len:
                    continue
                t = lb.match_name(" ".join(chunks), index)
                if t is not None:
                    target, full_name, best_len, take_next = t, " ".join(chunks), len(chunks), takes_next
            if take_next:
                used.add(k + 1)
            pending = ""
            row_material = material or lb.material_of(full_name)
            if target is None:
                if row_material:                     # строка мочи или кала («Гемоглобин в моче»)
                    col.ignore(lb.material_display(lb.material_target(full_name, index), col.cfg, row_material),
                               order, where, quiet=parse_value_cell(value_text) is None)
                elif parse_value_cell(value_text) is not None:
                    col.remember(order, where, "название показателя не распознано")
                continue
            if row_material:                         # моча, кал: не показатель крови
                col.ignore(lb.material_display(target, col.cfg, row_material), order, where)
                continue
            parsed = parse_value_cell(value_text)
            unit_cell = r[roles["unit"]] if "unit" in roles else ""
            value_rest = parsed[2] if parsed else ""
            target = lb.reroute(target, full_name, unit_cell or unit_text(value_rest))
            if target.kind == "ignored":
                col.ignore(target.code, order, where)
                continue
            code = target.code
            spec = col.cfg.analytes[code]
            if lb.foreign_unit(unit_cell) or lb.foreign_unit(unit_text(value_rest)):
                col.foreign(code, order, where)
                continue
            if multi:                                # таблица динамики без ясного последнего столбца
                if any(parse_value_cell(r[c]) is not None for c in multi):
                    col.multiple(code, order, where)
                continue
            if parsed is None or parsed[0] is None:
                col.remember(order, where, f"{col.name(code)} — нет числа")
                continue
            value, sign, value_rest, raw = parsed
            if second_result(value_rest):            # «128 → 135» в одной ячейке
                col.multiple(code, order, where)
                continue
            unit, unknown, rng = unit_and_range(value_rest, spec)   # «110 г/л» в одной ячейке
            if unit_cell:
                unit, unknown = lb.unit_for_cell(unit_cell, spec)
            if unit is None and not unknown:                         # «Гемоглобин, г/л» в ячейке названия
                name_unit = lb.split_name_unit(lb.clean_name(full_name))[1]
                if name_unit:
                    unit, unknown = lb.unit_for_cell(name_unit, spec)
            if "range" in roles and r[roles["range"]]:
                found = lb.find_range(r[roles["range"]])
                rng = found[0] if found else rng
            col.add(code, value, sign=sign, unit=unit, unit_unknown=unknown, rng=rng, order=order, where=where,
                    raw=raw, method=lb.esr_method(full_name) if code == "ESR" else None)
    return leftover, all_lines
