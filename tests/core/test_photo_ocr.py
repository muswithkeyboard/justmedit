"""Этап 7: разбор фото бланка (io/photo_ocr.py) — формат по сигнатуре, бомба по пикселям, EXIF-поворот,
метаданные отбрасываются, таймаут с убийством группы процессов, нет tesseract -> ocr_unavailable.
Тесты, которым нужен сам Tesseract, — SKIP без него (локально), в контейнере verify — PASS. Данные придуманы."""
from __future__ import annotations

import io
import os
import sys
import time

import helpers

from deficitlens_core.io import photo_ocr
from deficitlens_core.schemas import InputError


def _error_code(fn, *args, **kwargs) -> str:
    try:
        fn(*args, **kwargs)
    except InputError as e:
        return e.code
    raise AssertionError("ожидалась InputError")


def _big_png(w: int = 6000, h: int = 6000) -> bytes:
    """36 Мп белого PNG — файл ~100 КБ: «бомба» по пикселям при скромном размере файла."""
    from PIL import Image
    buf = io.BytesIO()
    Image.new("L", (w, h), 255).save(buf, "PNG", optimize=True)
    return buf.getvalue()


def test_image_format_by_signature():
    assert photo_ocr.image_format(b"\xff\xd8\xff\xe0" + b"\0" * 20) == "JPEG"
    assert photo_ocr.image_format(b"\x89PNG\r\n\x1a\n" + b"\0" * 20) == "PNG"
    assert photo_ocr.image_format(b"RIFF\0\0\0\0WEBPVP8 ") == "WEBP"
    assert photo_ocr.image_format(b"\0\0\0\x18ftypheic\0\0\0\0") == "HEIF"
    assert photo_ocr.image_format(b"%PDF-1.4") is None
    assert photo_ocr.image_format(b"Hemoglobin 120") is None


def test_not_an_image_and_heic_rejected_before_ocr():
    assert _error_code(photo_ocr.extract_text, b"Hemoglobin 120 g/l, it is not a jpeg") == "bad_image"
    assert _error_code(photo_ocr.extract_text, b"") == "bad_image"
    assert _error_code(photo_ocr.extract_text, b"\xff\xd8\xff\xe0 broken jpeg") == "bad_image"
    assert _error_code(photo_ocr.extract_text, b"\0\0\0\x18ftypheic" + b"\0" * 64) == "heic_unsupported"


def test_pixel_bomb_rejected_by_header_and_in_child():
    data = _big_png()
    assert len(data) < 1024 * 1024                         # файл маленький, а пикселей 36 млн
    assert _error_code(photo_ocr.check_image, data) == "image_too_large"
    assert _error_code(photo_ocr.extract_text, data) == "image_too_large"
    try:                                                   # вторая линия: та же проверка в дочернем процессе
        photo_ocr.prepare_image(data)
    except Exception:  # noqa: BLE001
        pass
    else:
        raise AssertionError("prepare_image должна отказать изображению больше 25 Мп")
    fmt, w, h = photo_ocr.check_image(_big_png(5000, 5000))  # ровно 25 Мп — допустимо
    assert (fmt, w, h) == ("PNG", 5000, 5000)


def test_prepare_image_exif_rotation_grayscale_and_no_metadata():
    from PIL import Image
    straight = photo_ocr.prepare_image(helpers.photo_blank())
    rotated = photo_ocr.prepare_image(helpers.photo_blank(exif_rotated=True))
    raw = Image.open(io.BytesIO(helpers.photo_blank(exif_rotated=True)))
    assert raw.size[1] > raw.size[0]                       # пиксели «лёжа»: высота больше ширины
    assert rotated.size == straight.size and straight.size[0] > straight.size[1]   # после EXIF — как бланк
    assert straight.mode == "L" and rotated.mode == "L"
    assert not straight.info and not straight.getexif()   # ни EXIF, ни других метаданных
    buf = io.BytesIO()
    straight.save(buf, "PNG")
    assert b"CANARY-CAMERA" not in buf.getvalue()          # в файл для tesseract метаданные не попадут
    # Мелкое фото увеличивается, огромное уменьшается; прозрачный фон становится белым.
    small = Image.new("RGBA", (600, 400), (0, 0, 0, 0))
    buf = io.BytesIO()
    small.save(buf, "PNG")
    up = photo_ocr.prepare_image(buf.getvalue())
    assert max(up.size) == photo_ocr.MIN_SIDE and up.getextrema()[0] > 200
    big = Image.new("L", (4800, 3600), 255)
    buf = io.BytesIO()
    big.save(buf, "JPEG")
    assert max(photo_ocr.prepare_image(buf.getvalue()).size) == photo_ocr.MAX_SIDE


def test_no_tesseract_gives_ocr_unavailable():
    saved = photo_ocr.TESSERACT_CMD
    photo_ocr.TESSERACT_CMD = "tesseract-not-installed-xyz"
    try:
        assert _error_code(photo_ocr.extract_text, helpers.photo_blank()) == "ocr_unavailable"
    finally:
        photo_ocr.TESSERACT_CMD = saved


def _is_zombie(pid: int) -> bool:
    try:
        with open(f"/proc/{pid}/stat") as f:
            return f.read().rsplit(")", 1)[1].split()[0] == "Z"
    except OSError:
        return False


def test_hard_timeout_kills_child_and_grandchild():
    """Зависший дочерний процесс (и его потомок, как tesseract) убиваются по жёсткому таймауту -> ocr_too_slow."""
    import tempfile
    if os.name != "posix":
        helpers.skip("группа процессов — только POSIX")
    pid_file = tempfile.NamedTemporaryFile(prefix="dl-ocr-test-", delete=False)
    pid_file.close()
    script = ("import subprocess, sys, time\n"
              "p = subprocess.Popen(['sleep', '30'])\n"
              f"open({pid_file.name!r}, 'w').write(str(p.pid))\n"
              "time.sleep(30)\n")
    saved = (photo_ocr.TESSERACT_CMD, photo_ocr._child_cmd, photo_ocr.HARD_TIMEOUT_EXTRA)
    photo_ocr.TESSERACT_CMD = sys.executable              # «tesseract есть»
    photo_ocr._child_cmd = lambda max_seconds: [sys.executable, "-c", script]
    photo_ocr.HARD_TIMEOUT_EXTRA = 1.5
    try:
        t0 = time.monotonic()
        code = _error_code(photo_ocr.extract_text, helpers.photo_blank(), max_seconds=0.5)
        took = time.monotonic() - t0
        assert code == "ocr_too_slow" and took < 20, (code, took)   # без убийства — 30 с
        grandchild = int(open(pid_file.name).read() or 0)
        assert grandchild > 0
        for _ in range(40):                                # потомок убит вместе с группой
            try:
                os.kill(grandchild, 0)
            except ProcessLookupError:
                break
            if _is_zombie(grandchild):                     # убит, но ещё не убран PID 1 (контейнер без init)
                break
            time.sleep(0.05)
        else:
            raise AssertionError("процесс-потомок остался жив")
    finally:
        photo_ocr.TESSERACT_CMD, photo_ocr._child_cmd, photo_ocr.HARD_TIMEOUT_EXTRA = saved
        os.unlink(pid_file.name)


# ---- с Tesseract (контейнер verify) ------------------------------------------------------------------------
def _parsed(data: bytes) -> tuple[photo_ocr.OcrText, dict]:
    from deficitlens_core.config import load_config
    from deficitlens_core.io.text_parser import parse_lab_text
    ocr = photo_ocr.extract_text(data)
    return ocr, parse_lab_text(ocr.text, load_config())


def _assert_values(values: dict, min_found: int) -> None:
    got = {code: v["value"] for code, v in values.items()}
    right = [c for c, v in helpers.PHOTO_VALUES.items() if got.get(c) == v]
    assert len(right) >= min_found, (sorted(got), right)
    wrong = [c for c in got if c in helpers.PHOTO_VALUES and got[c] != helpers.PHOTO_VALUES[c]]
    assert len(wrong) <= 1, wrong                          # неверно прочитанное — не больше одного


def test_ocr_clean_photo_finds_values():
    helpers.tesseract_or_skip()
    ocr, parsed = _parsed(helpers.photo_blank())
    _assert_values(parsed["values"], 8)


def test_ocr_exif_rotated_and_noisy_photo_finds_values():
    helpers.tesseract_or_skip()
    _, parsed = _parsed(helpers.photo_blank(exif_rotated=True))
    _assert_values(parsed["values"], 8)
    _, parsed = _parsed(helpers.photo_blank(exif_rotated=True, noise=True))
    _assert_values(parsed["values"], 6)
    _, parsed = _parsed(helpers.photo_blank(fmt="WEBP", noise=True))
    _assert_values(parsed["values"], 6)


def test_ocr_blurred_value_is_never_silently_wrong():
    """Размытое серое значение «9,8» Tesseract читает как «9,8», «9», «11» или не читает вовсе. Свойство: ферритин
    либо верный, либо помечен «проверьте значение», либо назван в нераспознанных — но не неверный молча."""
    helpers.tesseract_or_skip()
    from PIL import Image, ImageDraw, ImageFilter, ImageFont

    from deficitlens_api import service
    font = ImageFont.truetype(helpers.dejavu_font_path(), 30)
    outcomes = []
    for blur in (1.5, 2.0, 2.5, 3.5):
        for gray in (0, 90):
            im = Image.new("L", (1000, 200), 255)
            draw = ImageDraw.Draw(im)
            draw.text((40, 40), "Гемоглобин 118 г/л", fill=0, font=font)
            draw.text((40, 110), "Ферритин", fill=0, font=font)
            patch = Image.new("L", (160, 60), 255)
            ImageDraw.Draw(patch).text((10, 10), "9,8", fill=gray, font=font)
            im.paste(patch.filter(ImageFilter.GaussianBlur(blur)), (230, 100))
            draw.text((400, 110), "мкг/л", fill=0, font=font)
            buf = io.BytesIO()
            im.save(buf, "PNG")
            body = service.parse_file(buf.getvalue(), "blur.png")
            assert body["values"].get("hemoglobin", {}).get("value") == 118.0, body
            fer = body["values"].get("ferritin")
            if fer is None:
                assert any("Ферритин" in u for u in body["unrecognized"]), (blur, gray, body)
                outcomes.append("missing")
            elif fer["value"] != 9.8:
                assert "ferritin" in body["check"], (blur, gray, body)
                outcomes.append("flagged")
            else:
                outcomes.append("ok")
    assert "ok" in outcomes and len(set(outcomes)) >= 2, outcomes


def test_ocr_tesseract_timeout_gives_ocr_too_slow():
    helpers.tesseract_or_skip()
    assert _error_code(photo_ocr.extract_text, helpers.photo_blank(), max_seconds=0.01) == "ocr_too_slow"


def test_ocr_blank_page_gives_no_text():
    helpers.tesseract_or_skip()
    from PIL import Image
    buf = io.BytesIO()
    Image.new("L", (1200, 900), 255).save(buf, "PNG")
    assert _error_code(photo_ocr.extract_text, buf.getvalue()) == "no_text"
