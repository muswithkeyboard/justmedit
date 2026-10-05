"""Фото бланка (JPEG, PNG, WEBP) -> строки распознанного текста для таблицы сверки (локальный Tesseract, ADR 0007).

Что делает extract_text:
  * формат определяется по первым байтам (расширение и Content-Type не важны): JPEG, PNG, WEBP; HEIC/HEIF —
    понятный отказ «сохраните как JPEG» (декодер HEVC в образ не ставим: лишняя поверхность атаки);
  * до декодирования пикселей (по заголовку) проверяется размер: не больше MAX_PIXELS (25 Мп) — защита от
    «бомбы» (маленький файл, огромная картинка); в дочернем процессе ещё Image.MAX_IMAGE_PIXELS и предупреждение
    Pillow о бомбе как ошибка;
  * разбор и распознавание — в дочернем процессе `python -m deficitlens_core.io.photo_ocr` в своей группе
    процессов: картинка — через stdin, результат — JSON через stdout; по жёсткому таймауту убивается вся группа
    (и Python, и tesseract). В Linux дочернему процессу ограничены память и процессорное время;
  * подготовка: поворот по EXIF, оттенки серого, увеличение мелких и уменьшение огромных снимков, медианный
    фильтр (шум), автоконтраст; метаданные (EXIF, GPS, модель телефона) отбрасываются и никуда не уходят;
  * Tesseract (rus+eng, --psm 6 — строки таблицы бланка читаются целиком): слова со своей уверенностью (conf).
    Строка, где у слова с цифрой conf < MIN_CONF, помечается как неуверенная — значение из неё показывается
    с пометкой «проверьте значение», а не принимается молча.

Временные файлы pytesseract (картинка и TSV) создаются только в /tmp (TMPDIR дочернего процесса; в Docker это
tmpfs в памяти) и удаляются сразу после распознавания. Распознанный текст не пишется в журнал и не возвращается
наружу: вызывающий код (service.parse_file) разбирает его тем же text_parser, что текст и PDF, и возвращает только
показатели. stderr дочернего процесса отбрасывается. Ошибки — InputError (ответ 422).
"""
from __future__ import annotations

import io
import json
import os
import signal
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

MAX_PIXELS = 25_000_000                 # 25 Мп: больше не бывает у фото бланка с телефона
MAX_SECONDS = 20.0                      # таймаут самого tesseract (pytesseract убивает его процесс)
HARD_TIMEOUT_EXTRA = 5.0                # запас на запуск Python, импорт Pillow и подготовку снимка
MAX_MEMORY_BYTES = 1536 * 1024 * 1024   # адресное пространство дочернего процесса (и tesseract)
MAX_TEXT_CHARS = 20_000                 # как text_parser.MAX_TEXT_CHARS
MAX_SIDE = 3200                         # длинная сторона больше — уменьшаем (время tesseract растёт с площадью)
MIN_SIDE = 1800                         # длинная сторона меньше — увеличиваем (буквы мельче ~20 px Tesseract путает)
MAX_UPSCALE = 3.0
# conf Tesseract (0–100) у слова с цифрой ниже порога — «проверьте значение». Подобрано на придуманном бланке
# (tesseract 5.5, rus+eng): чёткие числа 88–96; размытые и прочитанные неверно («9,8» -> «9») — 55–74.
MIN_CONF = 80.0
LANG = "rus+eng"
TESSERACT_CMD = "tesseract"             # имя бинарника; тесты подменяют, чтобы проверить ocr_unavailable
TMP_DIR = "/tmp"                        # только сюда пишутся временные файлы (в Docker — tmpfs)

FORMATS = ("JPEG", "PNG", "WEBP")
_HEIF_BRANDS = (b"heic", b"heix", b"hevc", b"hevx", b"heim", b"heis", b"hevm", b"hevs", b"mif1", b"msf1", b"avif")
_SRC_DIR = str(Path(__file__).resolve().parents[2])

UNAVAILABLE = ("Распознавание фото доступно в Docker-версии (там установлен Tesseract). Здесь: вставьте текст "
               "бланка, загрузите PDF с текстовым слоем или введите значения вручную.")


def _input_error(code: str, message: str, field: str | None = "file") -> Exception:
    """InputError из schemas — импорт здесь, а не в начале модуля: дочернему процессу распознавания pydantic
    не нужен (запуск процесса быстрее на ~0,1 с)."""
    from ..schemas import InputError

    return InputError(code, message, field)


@dataclass
class OcrText:
    text: str                                   # строки распознанного текста (только для text_parser, наружу не идёт)
    low_confidence_lines: set[int] = field(default_factory=set)   # номера строк (с 1) с неуверенными числами
    notes: list[str] = field(default_factory=list)


def image_format(data: bytes) -> str | None:
    """Формат по сигнатуре: JPEG, PNG, WEBP, HEIF или None (не изображение / не поддерживается)."""
    head = bytes(data[:16])
    if head.startswith(b"\xff\xd8\xff"):
        return "JPEG"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "PNG"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "WEBP"
    if head[4:8] == b"ftyp" and head[8:12] in _HEIF_BRANDS:
        return "HEIF"
    return None


def _bad_image() -> Exception:
    return _input_error("bad_image", "Не удалось прочитать фото: файл повреждён или это не изображение. "
                        "Сохраните снимок бланка как JPEG или PNG.", "file")


def _too_large(pixels: int) -> Exception:
    return _input_error("image_too_large", f"Слишком большое изображение ({pixels / 1e6:.0f} Мп): допустимо не больше "
                        f"{MAX_PIXELS // 1_000_000} Мп. Уменьшите снимок или сфотографируйте бланк заново.", "file")


def check_image(data: bytes) -> tuple[str, int, int]:
    """Формат и размер по заголовку — без декодирования пикселей. Ошибки — InputError."""
    fmt = image_format(data)
    if fmt == "HEIF":
        raise _input_error("heic_unsupported", "Фото в формате HEIC не поддерживается: сохраните снимок как JPEG "
                           "(на iPhone: Настройки → Камера → Форматы → «Наиболее совместимый») или снимите бланк "
                           "кнопкой «Сфотографировать бланк».", "file")
    if fmt is None:
        raise _bad_image()
    import warnings

    from PIL import Image
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(bytes(data)), formats=[fmt]) as im:    # читает только заголовок
                w, h = im.size
    except (Image.DecompressionBombError, Image.DecompressionBombWarning):
        raise _too_large(MAX_PIXELS * 2) from None
    except Exception:  # noqa: BLE001 — любой сбой чтения заголовка = нечитаемое изображение
        raise _bad_image() from None
    if w <= 0 or h <= 0:
        raise _bad_image()
    if w * h > MAX_PIXELS:
        raise _too_large(w * h)
    return fmt, w, h


# --------------------------------------------------------------------------------------------
# Дочерний процесс
# --------------------------------------------------------------------------------------------
def _limit_child_resources(max_seconds: float) -> None:
    """Память и процессорное время дочернего процесса (наследуются tesseract; Linux)."""
    try:
        import resource
        resource.setrlimit(resource.RLIMIT_AS, (MAX_MEMORY_BYTES, MAX_MEMORY_BYTES))
        cpu = int(max_seconds) + 5
        resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu))
    except (ImportError, ValueError, OSError):
        pass


def prepare_image(data: bytes):
    """Байты фото -> подготовленное изображение Pillow в оттенках серого, без метаданных.
    Пиксели проверяются до load(): заголовок -> размер -> только потом декодирование."""
    import warnings

    from PIL import Image, ImageFilter, ImageOps

    Image.MAX_IMAGE_PIXELS = MAX_PIXELS
    fmt = image_format(data)
    if fmt not in FORMATS:
        raise ValueError("format")
    with warnings.catch_warnings():
        warnings.simplefilter("error", Image.DecompressionBombWarning)   # «бомба» -> исключение, а не предупреждение
        im = Image.open(io.BytesIO(bytes(data)), formats=[fmt])
    if im.size[0] * im.size[1] > MAX_PIXELS:
        raise ValueError("pixels")
    if fmt == "JPEG":
        im.draft("L", im.size)                       # JPEG сразу декодируется в оттенки серого (меньше памяти)
    im = ImageOps.exif_transpose(im)                 # фото «боком» по EXIF -> правильная ориентация (тут load())
    if "A" in im.getbands():                         # прозрачный фон -> белый (иначе станет чёрным)
        bg = Image.new("RGBA", im.size, (255, 255, 255, 255))
        bg.alpha_composite(im.convert("RGBA"))
        im = bg
    im = im.convert("L")
    long_side = max(im.size)
    if long_side > MAX_SIDE:
        k = MAX_SIDE / long_side
    elif long_side < MIN_SIDE:
        k = min(MAX_UPSCALE, MIN_SIDE / long_side)
    else:
        k = 1.0
    if k != 1.0:
        im = im.resize((max(1, round(im.size[0] * k)), max(1, round(im.size[1] * k))), Image.Resampling.LANCZOS)
    im = im.filter(ImageFilter.MedianFilter(3))      # одиночные точки шума (зерно камеры) не станут «точками»
    im = ImageOps.autocontrast(im, cutoff=1)          # серый текст на сером фоне -> тёмный на светлом
    clean = Image.new("L", im.size)                   # новая картинка: ни EXIF, ни других метаданных
    clean.paste(im)
    return clean


def ocr_lines(image, max_seconds: float) -> tuple[list[str], set[int]]:
    """Tesseract по подготовленной картинке -> (строки текста, номера строк с неуверенными числами)."""
    import pytesseract

    pytesseract.pytesseract.tesseract_cmd = TESSERACT_CMD
    d = pytesseract.image_to_data(image, lang=LANG, config="--psm 6", output_type=pytesseract.Output.DICT,
                                  timeout=max_seconds)
    groups: dict[tuple, list[tuple[str, float]]] = {}
    for i, word in enumerate(d.get("text", [])):
        word = str(word or "").strip()
        if not word:
            continue
        try:
            conf = float(d["conf"][i])
        except (TypeError, ValueError):
            conf = -1.0
        if conf < 0:
            continue                                  # служебные строки TSV (блок, абзац) — не слова
        key = (d["page_num"][i], d["block_num"][i], d["par_num"][i], d["line_num"][i])
        groups.setdefault(key, []).append((word, conf))
    lines: list[str] = []
    low: set[int] = set()
    for words in groups.values():                     # порядок вставки = порядок чтения Tesseract
        lines.append(" ".join(w for w, _ in words))
        if any(c < MIN_CONF for w, c in words if any(ch.isdigit() for ch in w)):
            low.add(len(lines))
    return lines, low


def _worker_main() -> None:
    """Точка входа дочернего процесса: фото из stdin, результат — JSON в stdout. Тип ошибки наружу не отдаётся."""
    max_seconds = float(sys.argv[1])
    _limit_child_resources(max_seconds)
    try:
        image = prepare_image(sys.stdin.buffer.read())
    except Exception:  # noqa: BLE001 — нечитаемое фото или бомба
        sys.stdout.write(json.dumps({"status": "bad_image"}))
        return
    try:
        lines, low = ocr_lines(image, max_seconds)
        out = {"status": "ok", "lines": lines, "low": sorted(low)}
    except RuntimeError as e:                          # pytesseract: «Tesseract process timeout»
        out = {"status": "timeout" if "timeout" in str(e).lower() else "error"}
    except Exception as e:  # noqa: BLE001 — нет tesseract или языка; подробности могут содержать данные
        out = {"status": "unavailable" if type(e).__name__ == "TesseractNotFoundError" else "error"}
    sys.stdout.write(json.dumps(out, ensure_ascii=True))


# --------------------------------------------------------------------------------------------
# Вызов из сервиса
# --------------------------------------------------------------------------------------------
def tesseract_available() -> bool:
    import shutil
    return shutil.which(TESSERACT_CMD) is not None


def _child_cmd(max_seconds: float) -> list[str]:
    return [sys.executable, "-m", "deficitlens_core.io.photo_ocr", str(float(max_seconds))]


def _too_slow(max_seconds: float) -> Exception:
    return _input_error("ocr_too_slow", f"Фото не удалось распознать за {max_seconds:g} с: сфотографируйте только "
                        "таблицу с результатами, ближе и при хорошем свете, или вставьте текст анализа.", "file")


def _run_child(cmd: list[str], data: bytes, timeout: float) -> bytes:
    """Дочерний процесс в своей группе; по таймауту убивается вся группа (Python и tesseract). -> stdout."""
    env = {k: v for k, v in os.environ.items() if k in ("PATH", "LANG", "LC_ALL", "SYSTEMROOT", "TESSDATA_PREFIX")}
    env["PYTHONPATH"] = _SRC_DIR + (os.pathsep + os.environ["PYTHONPATH"] if os.environ.get("PYTHONPATH") else "")
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["OMP_THREAD_LIMIT"] = "1"                      # tesseract в один поток: два тяжёлых расчёта не мешают
    if os.path.isdir(TMP_DIR):
        env["TMPDIR"] = TMP_DIR                       # временные файлы pytesseract — только в /tmp
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                            env=env, start_new_session=(os.name == "posix"))
    try:
        out, _ = proc.communicate(input=bytes(data), timeout=timeout)
        return out
    except BaseException:                             # таймаут (TimeoutExpired) или прерывание — убить группу
        _kill_group(proc)
        raise


def _kill_group(proc: subprocess.Popen) -> None:
    try:
        if os.name == "posix":
            os.killpg(proc.pid, signal.SIGKILL)
        else:
            proc.kill()
    except (ProcessLookupError, PermissionError, OSError):
        pass
    try:
        proc.communicate(timeout=5)
    except Exception:  # noqa: BLE001
        pass


def extract_text(data: bytes, *, max_seconds: float = MAX_SECONDS) -> OcrText:
    """Фото бланка -> OcrText. Ошибки (InputError, ответ 422): bad_image, heic_unsupported, image_too_large,
    ocr_unavailable (нет tesseract), ocr_too_slow (таймаут, процесс убит), no_text (ничего не распознано)."""
    if not isinstance(data, (bytes, bytearray)) or not data:
        raise _bad_image()
    check_image(data)
    if not tesseract_available():
        raise _input_error("ocr_unavailable", UNAVAILABLE, "file")
    try:
        raw = _run_child(_child_cmd(max_seconds), bytes(data), max_seconds + HARD_TIMEOUT_EXTRA)
    except subprocess.TimeoutExpired:
        raise _too_slow(max_seconds) from None
    except OSError:
        raise _input_error("ocr_unavailable", UNAVAILABLE, "file") from None
    try:
        out = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise _input_error("ocr_failed", "Не удалось распознать фото: сфотографируйте бланк ещё раз или вставьте "
                           "текст анализа.", "file") from None
    status = out.get("status") if isinstance(out, dict) else None
    if status == "bad_image":
        raise _bad_image()
    if status == "timeout":
        raise _too_slow(max_seconds)
    if status == "unavailable":
        raise _input_error("ocr_unavailable", UNAVAILABLE, "file")
    if status != "ok":
        raise _input_error("ocr_failed", "Не удалось распознать фото: сфотографируйте бланк ещё раз или вставьте "
                           "текст анализа.", "file")
    lines = [str(x) for x in out.get("lines") or []]
    text = "\n".join(lines)
    notes: list[str] = []
    if len(text) > MAX_TEXT_CHARS:
        text = text[:MAX_TEXT_CHARS]
        notes.append(f"Разобраны первые {MAX_TEXT_CHARS} символов распознанного текста.")
    if not text.strip():
        raise _input_error("no_text", "На фото не найден текст: снимайте бланк целиком, при хорошем свете, без бликов "
                           "и размытия.", "file")
    low = {int(n) for n in out.get("low") or [] if isinstance(n, int)}
    return OcrText(text=text, low_confidence_lines=low, notes=notes)


if __name__ == "__main__":
    _worker_main()
