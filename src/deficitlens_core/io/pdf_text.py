"""PDF бланка -> текст и таблицы для таблицы сверки (pdfplumber); скан без текстового слоя -> картинки страниц
для распознавания (pypdfium2, затем Tesseract тем же путём, что фото, io/photo_ocr.py).

Перенесено из прежней версии (тег prepitch, ревизия волны 3): одну страницу pdfminer изнутри прервать нельзя,
а PDF в пару килобайт может разбираться минутами и держать GIL. Поэтому разбор идёт в дочернем процессе
`python -m deficitlens_core.io.pdf_text`, который убивается по жёсткому таймауту. Файл передаётся через stdin,
результат возвращается JSON через stdout — на диск ничего не пишется; stderr дочернего процесса (предупреждения
pdfminer о шрифтах, где может быть содержимое файла) отбрасывается и в журнал не попадает.

Что возвращает extract_text (PDF любой лаборатории, этап П2):
  * text — весь текст страниц (как раньше);
  * tables — строки таблиц (`page.find_tables()` / `extract()`): [[[ячейка, …], …], …], для io/lab_tables.py;
  * text_outside — текст страниц вне найденных таблиц (шапка бланка: дата, подписи), чтобы строки таблиц не
    разбирались дважды;
  * table_above — для каждой таблицы текст страницы над ней (от предыдущей таблицы или верха страницы до верхнего
    края таблицы, не больше MAX_ABOVE_CHARS): заголовок раздела «Общий анализ мочи» / «Общий анализ крови» —
    io/lab_tables.py не сопоставляет строки таблицы мочи с показателями крови;
  * scanned — в PDF нет текстового слоя, но есть изображения или графика страниц (скан): вызывающий код
    распознаёт страницы render_pages + photo_ocr. Совсем пустой PDF (ни текста, ни графики) — no_text_layer.

render_pages — до SCAN_MAX_PAGES страниц в оттенках серого при SCAN_DPI (длинная сторона не больше
SCAN_MAX_SIDE px), PNG в памяти; тот же дочерний процесс, те же пределы и жёсткий таймаут.

Пределы: файл не больше MAX_PDF_BYTES (5 МБ; бланк анализа — десятки–сотни КБ, а pdfminer на битом файле
в 10 МБ занимал процессор ~9 с), не больше MAX_PAGES страниц, MAX_SECONDS на разбор (+ запас на запуск процесса),
текста не больше MAX_TEXT_CHARS; в Linux дочернему процессу ещё ограничены память (MAX_MEMORY_BYTES)
и процессорное время. До pdfminer — дешёвая проверка pypdfium2 (открывается ли файл, есть ли страницы, ~10 мс):
битый файл отклоняется сразу, без долгого разбора.
Ошибки — InputError (ответ 422; слишком большой файл — pdf_too_large, ответ 413): пустой PDF, повреждённый /
зашифрованный файл, слишком долгий разбор.

Дочерний процесс стартует быстро (~0,1 с вместо ~0,4 с): модуль не импортирует ни пакет движка (numpy), ни схемы
(pydantic) — InputError берётся из schemas только в родительском процессе, при ошибке (_input_error).
"""
from __future__ import annotations

import base64
import io
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

MAX_PAGES = 10
MAX_SECONDS = 4.0                   # разбор текстового бланка — доли секунды; дольше — это не бланк
MAX_PDF_BYTES = 5 * 1024 * 1024     # предел размера PDF (общий предел файла DL_MAX_UPLOAD_MB — 10 МБ)
MAX_TEXT_CHARS = 200_000
MAX_TABLES = 60                     # таблиц на документ
MAX_TABLE_CELLS = 8_000             # ячеек на документ (строки × столбцы)
MAX_CELL_CHARS = 300
MAX_ABOVE_CHARS = 400               # текст над таблицей: нужны только последние строки (заголовок раздела)
MAX_MEMORY_BYTES = 1536 * 1024 * 1024    # адресное пространство; запас на импорт pdfplumber
HARD_TIMEOUT_EXTRA = 3.0            # запас на запуск дочернего процесса (импорт pdfplumber)
PDF_MAGIC = b"%PDF-"
SCAN_MAX_PAGES = 5                  # скан: столько страниц рендерится и распознаётся
SCAN_DPI = 200                      # разрешение рендера скана (буквы бланка ~ 25–35 px — Tesseract читает уверенно)
SCAN_MAX_SIDE = 3200                # длинная сторона картинки страницы, px (как photo_ocr.MAX_SIDE)

NO_TEXT_LAYER = "В PDF не найден ни текст, ни изображение страницы: введите значения вручную или вставьте текст бланка."
_SRC_DIR = str(Path(__file__).resolve().parents[2])      # каталог с пакетом deficitlens_core — для дочернего процесса


@dataclass
class PdfText:
    text: str
    pages_total: int
    pages_read: int
    notes: list[str] = field(default_factory=list)
    tables: list = field(default_factory=list)          # [[[ячейка, …], …], …]
    text_outside: str = ""                              # текст вне таблиц
    table_above: list = field(default_factory=list)     # [текст страницы над таблицей, …] — по таблице
    images: int = 0                                     # изображений на прочитанных страницах
    scanned: bool = False                               # нет текста, но есть изображения / графика: нужен OCR


def _limit_child_resources(max_seconds: float) -> None:
    """Память и процессорное время дочернего процесса (Linux; в других ОС пределы могут не действовать)."""
    try:
        import resource
        resource.setrlimit(resource.RLIMIT_AS, (MAX_MEMORY_BYTES, MAX_MEMORY_BYTES))
        cpu = int(max_seconds) + 2
        resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu))
    except (ImportError, ValueError, OSError):
        pass


def _outside(bboxes: list[tuple]):
    """Фильтр pdfplumber: символ вне всех таблиц (по центру символа)."""
    def keep(obj) -> bool:
        if obj.get("object_type") != "char":
            return True
        cx = (obj["x0"] + obj["x1"]) / 2
        cy = (obj["top"] + obj["bottom"]) / 2
        return not any(x0 <= cx <= x1 and top <= cy <= bottom for x0, top, x1, bottom in bboxes)
    return keep


def _precheck(data: bytes) -> int:
    """Дешёвая проверка до pdfminer (pypdfium2, ~10 мс даже на 5 МБ): файл открывается и в нём есть страницы.
    Битый, зашифрованный паролем или пустой PDF -> исключение (в ответе — bad_pdf), без долгого разбора pdfminer.
    Возвращает число страниц."""
    import pypdfium2 as pdfium  # зависимость pdfplumber (uv.lock)

    pdf = pdfium.PdfDocument(data)
    try:
        total = len(pdf)
    finally:
        pdf.close()
    if total < 1:
        raise ValueError("в PDF нет страниц")
    return total


def _extract_in_process(data: bytes, max_pages: int, max_seconds: float) -> PdfText:
    """Разбор в текущем процессе (вызывается только в дочернем процессе)."""
    import logging

    _precheck(data)
    import pdfplumber  # импорт здесь: модуль тяжёлый и нужен только для PDF

    logging.getLogger("pdfminer").setLevel(logging.ERROR)
    t0 = time.monotonic()
    parts: list[str] = []
    outside: list[str] = []
    tables: list = []
    above: list[str] = []
    cells = 0
    notes: list[str] = []
    read = images = graphics = 0
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        total = len(pdf.pages)
        for page in pdf.pages[:max_pages]:
            if time.monotonic() - t0 > max_seconds:
                notes.append(f"PDF разбирался дольше {max_seconds:g} с — прочитаны первые {read} стр.; "
                             "проверьте, все ли показатели найдены.")
                break
            text = page.extract_text() or ""
            parts.append(text)
            images += len(page.images)
            graphics += len(page.images) + len(page.curves) + len(page.rects) + len(page.lines)
            bboxes = []
            if text.strip() and len(tables) < MAX_TABLES and cells < MAX_TABLE_CELLS:
                found = sorted(page.find_tables(), key=lambda t: (t.bbox[1], t.bbox[0]))
                for t in found:
                    rows = [[(c or "")[:MAX_CELL_CHARS] for c in row] for row in t.extract()]
                    n = sum(len(r) for r in rows)
                    if len(tables) >= MAX_TABLES or cells + n > MAX_TABLE_CELLS:
                        notes.append("В PDF много таблиц — разобраны не все; проверьте, все ли показатели найдены.")
                        break
                    tables.append(rows)
                    cells += n
                    bboxes.append(t.bbox)
                above.extend(_text_above(page, bboxes, [t.bbox for t in found]))
            outside.append((page.filter(_outside(bboxes)).extract_text() or "") if bboxes else text)
            read += 1
            if sum(len(p) for p in parts) > MAX_TEXT_CHARS:
                notes.append(f"Текст PDF слишком длинный — прочитаны первые {read} стр.")
                break
    if total > max_pages and not notes:
        notes.append(f"В PDF {total} стр. — прочитаны первые {max_pages}; проверьте, все ли показатели найдены.")
    text = "\n".join(parts)[:MAX_TEXT_CHARS]
    return PdfText(text=text, pages_total=total, pages_read=read, notes=notes, tables=tables,
                   text_outside="\n".join(outside)[:MAX_TEXT_CHARS], images=images,
                   scanned=not text.strip() and graphics > 0, table_above=above[:len(tables)])


def _text_above(page, taken: list[tuple], all_boxes: list[tuple]) -> list[str]:
    """Для каждой взятой таблицы страницы — текст между предыдущей таблицей (или верхом страницы) и её верхним
    краем, вне таблиц (последние MAX_ABOVE_CHARS символов). Ошибка вырезки — пустая строка: заголовок раздела
    необязателен, разбор таблицы от него не зависит."""
    x0, top0, x1, _ = page.bbox
    out: list[str] = []
    prev_bottom = top0
    for bbox in taken:
        top = bbox[1]
        text = ""
        if top - prev_bottom >= 1:
            try:
                region = page.crop((x0, prev_bottom, x1, top))
                text = (region.filter(_outside(all_boxes)).extract_text() or "")[-MAX_ABOVE_CHARS:]
            except Exception:  # noqa: BLE001 — геометрия страницы бывает некорректной; заголовок необязателен
                text = ""
        out.append(text)
        prev_bottom = max(prev_bottom, bbox[3])
    return out


def _render_in_process(data: bytes, max_pages: int, dpi: int) -> tuple[list[bytes], int]:
    """Страницы скана -> PNG в оттенках серого (вызывается только в дочернем процессе)."""
    import pypdfium2 as pdfium  # зависимость pdfplumber (uv.lock)

    pdf = pdfium.PdfDocument(data)
    try:
        total = len(pdf)
        out: list[bytes] = []
        for i in range(min(total, max_pages)):
            page = pdf[i]
            w, h = page.get_size()                   # пункты (1/72 дюйма)
            scale = min(dpi / 72.0, SCAN_MAX_SIDE / max(w, h, 1.0))
            image = page.render(scale=scale, grayscale=True).to_pil().convert("L")
            buf = io.BytesIO()
            image.save(buf, "PNG", optimize=False)
            out.append(buf.getvalue())
            page.close()
        return out, total
    finally:
        pdf.close()


def _worker_main() -> None:
    """Точка входа дочернего процесса: PDF из stdin, результат — JSON в stdout. Тип ошибки наружу не отдаётся."""
    if sys.argv[1] == "render":
        max_pages, max_seconds, dpi = int(sys.argv[2]), float(sys.argv[3]), int(sys.argv[4])
        _limit_child_resources(max_seconds)
        try:
            pages, total = _render_in_process(sys.stdin.buffer.read(), max_pages, dpi)
            out = {"status": "ok", "pages_total": total,
                   "pages": [base64.b64encode(p).decode("ascii") for p in pages]}
        except Exception:  # noqa: BLE001 — любой сбой = нечитаемый PDF; подробности могут содержать данные
            out = {"status": "error"}
        sys.stdout.write(json.dumps(out, ensure_ascii=True))
        return
    max_pages, max_seconds = int(sys.argv[1]), float(sys.argv[2])
    _limit_child_resources(max_seconds)
    try:
        r = _extract_in_process(sys.stdin.buffer.read(), max_pages, max_seconds)
        out = {"status": "ok", "text": r.text, "pages_total": r.pages_total, "pages_read": r.pages_read,
               "notes": r.notes, "tables": r.tables, "text_outside": r.text_outside, "images": r.images,
               "scanned": r.scanned, "table_above": r.table_above}
    except Exception:  # noqa: BLE001 — любой сбой разбора = нечитаемый PDF; подробности могут содержать данные
        out = {"status": "error"}
    sys.stdout.write(json.dumps(out, ensure_ascii=True))


def _input_error(code: str, message: str, field: str | None = "file") -> Exception:
    """InputError из schemas — импорт здесь, а не в начале модуля: дочернему процессу pydantic не нужен."""
    from ..schemas import InputError

    return InputError(code, message, field)


def _bad_pdf() -> Exception:
    return _input_error("bad_pdf", "Не удалось прочитать PDF: файл повреждён, зашифрован или это не PDF. "
                        "Сохраните бланк как PDF с текстом или вставьте текст анализа.")


def _run_child(args: list[str], data: bytes, max_seconds: float) -> dict:
    """Дочерний процесс с жёстким таймаутом max_seconds + HARD_TIMEOUT_EXTRA -> JSON-ответ (status == "ok")."""
    env = {k: v for k, v in os.environ.items() if k in ("PATH", "LANG", "LC_ALL", "SYSTEMROOT", "TMPDIR")}
    env["PYTHONPATH"] = _SRC_DIR + (os.pathsep + os.environ["PYTHONPATH"] if os.environ.get("PYTHONPATH") else "")
    env["PYTHONIOENCODING"] = "utf-8"
    cmd = [sys.executable, "-m", "deficitlens_core.io.pdf_text", *args]
    try:
        proc = subprocess.run(cmd, input=bytes(data), capture_output=True, timeout=max_seconds + HARD_TIMEOUT_EXTRA,
                              check=False, env=env)
    except subprocess.TimeoutExpired as e:  # subprocess.run убивает дочерний процесс при таймауте
        raise _input_error("pdf_too_complex", f"PDF не удалось разобрать за {max_seconds:g} с: вставьте текст "
                           "анализа или введите значения вручную.") from e
    except OSError as e:
        raise _input_error("pdf_unavailable", "Разбор PDF сейчас недоступен: вставьте текст анализа.") from e
    try:
        out = json.loads(proc.stdout.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as e:
        raise _bad_pdf() from e
    if not isinstance(out, dict) or out.get("status") != "ok":
        raise _bad_pdf()
    return out


def _check_magic(data: bytes) -> None:
    """Дешёвые проверки до запуска дочернего процесса: размер (pdf_too_large) и сигнатура %PDF- (bad_pdf)."""
    if not isinstance(data, (bytes, bytearray)) or not bytes(data[:1024]).lstrip().startswith(PDF_MAGIC):
        raise _bad_pdf()
    if len(data) > MAX_PDF_BYTES:
        raise _input_error("pdf_too_large", f"PDF больше {MAX_PDF_BYTES // (1024 * 1024)} МБ. Бланк анализа обычно "
                           "меньше 1 МБ: сохраните только страницы с результатами или вставьте текст анализа.")


def extract_text(data: bytes, *, max_pages: int = MAX_PAGES, max_seconds: float = MAX_SECONDS) -> PdfText:
    """Текст и таблицы первых max_pages страниц PDF. Разбор — в дочернем процессе с жёстким таймаутом (процесс
    убивается). Больше MAX_PDF_BYTES -> InputError pdf_too_large;
    не PDF, повреждённый или зашифрованный файл -> InputError bad_pdf; долгий разбор -> pdf_too_complex;
    ни текста, ни изображений -> no_text_layer; скан (изображения без текста) -> PdfText(scanned=True, text="")."""
    _check_magic(data)
    out = _run_child([str(int(max_pages)), str(float(max_seconds))], data, max_seconds)
    text = str(out.get("text") or "")
    scanned = bool(out.get("scanned"))
    if not text.strip() and not scanned:
        raise _input_error("no_text_layer", NO_TEXT_LAYER)
    tables = out.get("tables") or []
    if not isinstance(tables, list):
        tables = []
    above = out.get("table_above") or []
    above = [str(a or "") for a in above] if isinstance(above, list) else []
    above = (above + [""] * len(tables))[:len(tables)]
    return PdfText(text=text, pages_total=int(out.get("pages_total") or 0), pages_read=int(out.get("pages_read") or 0),
                   notes=[str(n) for n in out.get("notes") or []], tables=tables,
                   text_outside=str(out.get("text_outside") or ""), images=int(out.get("images") or 0),
                   scanned=scanned and not text.strip(), table_above=above)


def render_pages(data: bytes, *, max_pages: int = SCAN_MAX_PAGES, dpi: int = SCAN_DPI,
                 max_seconds: float = MAX_SECONDS) -> tuple[list[bytes], int]:
    """Страницы PDF -> ([PNG первых max_pages страниц], всего страниц). Рендер pypdfium2 в дочернем процессе
    с жёстким таймаутом и пределом памяти. Ошибки — InputError (bad_pdf, pdf_too_complex)."""
    _check_magic(data)
    out = _run_child(["render", str(int(max_pages)), str(float(max_seconds)), str(int(dpi))], data, max_seconds)
    pages = []
    for p in out.get("pages") or []:
        try:
            pages.append(base64.b64decode(str(p), validate=True))
        except ValueError:
            raise _bad_pdf() from None
    if not pages:
        raise _bad_pdf()
    return pages, int(out.get("pages_total") or len(pages))


if __name__ == "__main__":
    _worker_main()
