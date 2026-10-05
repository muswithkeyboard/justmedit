"""Помощники тестов. Тесты — обычные функции test_* с assert, без фикстур pytest:
так их можно запускать и pytest, и встроенным scripts/run_tests.py (когда pytest недоступен)."""
from __future__ import annotations

import io
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for _p in (ROOT / "src", ROOT / "tests"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))


class SkipTest(Exception):
    """Тест пропущен (нет данных). scripts/run_tests.py считает это SKIP, а не FAIL."""


def skip(reason: str):
    try:
        import pytest  # type: ignore
    except ImportError:
        raise SkipTest(reason)
    pytest.skip(reason)


def case_data_path() -> str:
    """Путь к файлу кейса (840 строк) или SKIP, если его нет: в репозитории данных кейса нет."""
    p = os.environ.get("DL_CASE_DATA") or str(ROOT / "data" / "case" / "deficiency_anemia.csv")
    if not Path(p).exists():
        skip("нет файла кейса: задайте DL_CASE_DATA=/abs/path/deficiency_anemia.csv")
    return p


def nhanes_data_path() -> str:
    p = os.environ.get("DL_NHANES_DATA") or str(ROOT / "data" / "nhanes" / "nhanes_deficiency_real.csv")
    if not Path(p).exists():
        skip("нет файла NHANES: задайте DL_NHANES_DATA=/abs/path/nhanes_deficiency_real.csv")
    return p


def load_examples() -> list[dict]:
    return json.loads((ROOT / "data" / "demo" / "examples.json").read_text(encoding="utf-8"))["examples"]


def pdf_with_text(lines: list[str]) -> bytes:
    """Минимальный PDF с текстовым слоем (шрифт Helvetica, латиница) — без внешних библиотек."""
    content = "BT /F1 12 Tf 50 750 Td 14 TL " + " ".join(f"({ln}) Tj T*" for ln in lines) + " ET"
    objs = ["<< /Type /Catalog /Pages 2 0 R >>",
            "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            ("<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 4 0 R >> >> "
             "/Contents 5 0 R >>"),
            "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
            f"<< /Length {len(content)} >>\nstream\n{content}\nendstream"]
    out, offsets = b"%PDF-1.4\n", []
    for i, body in enumerate(objs, start=1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n{body}\nendobj\n".encode("latin-1")
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    out += "".join(f"{o:010d} 00000 n \n" for o in offsets).encode()
    out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return out


# ---- фото бланка (этап 7): синтетический снимок, все данные придуманы --------------------------------------
PHOTO_PII = ("Иванова", "Мария", "Петровна", "1988", "12.03", "+7", "912", "345-67-89", "СНИЛС", "123-456-789")
PHOTO_LINES = [
    "Клинико-диагностическая лаборатория",
    "ФИО: Иванова Мария Петровна",
    "Дата рождения: 12.03.1988",
    "Телефон: +7 912 345-67-89  СНИЛС 123-456-789 00",
    "Показатель      Результат   Ед.        Норма",
    "Гемоглобин      118         г/л        120-140",
    "Эритроциты      4,1         10^12/л    3,8-5,1",
    "MCV             78          фл         80-100",
    "MCH             25,4        пг         27-34",
    "RDW             16,2        %          11,5-14,5",
    "Ферритин        9           мкг/л      10-120",
    "Витамин B12     210         пг/мл      190-900",
    "С-реактивный белок  3       мг/л       0-5",
]
PHOTO_VALUES = {"hemoglobin": 118.0, "RBC": 4.1, "MCV": 78.0, "MCH": 25.4, "RDW": 16.2, "ferritin": 9.0,
                "vitamin_B12": 210.0, "CRP": 3.0}


def dejavu_font_path() -> str:
    """Шрифт DejaVu Sans: системный (fonts-dejavu-core в образе verify) или из matplotlib; нет — SKIP."""
    cands = ["/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"]
    try:
        import matplotlib
        cands.append(str(Path(matplotlib.__file__).parent / "mpl-data" / "fonts" / "ttf" / "DejaVuSans.ttf"))
    except ImportError:
        pass
    for p in cands:
        if Path(p).is_file():
            return p
    skip("нет шрифта DejaVu Sans (fonts-dejavu-core или matplotlib)")
    return ""


def photo_blank(*, exif_rotated: bool = False, noise: bool = False, fmt: str = "JPEG",
                lines: list[str] | None = None) -> bytes:
    """Синтетическое «фото» бланка, нарисованное Pillow шрифтом DejaVu: шапка с ПДн и 8 показателей.
    exif_rotated — пиксели повёрнуты на 90°, а EXIF Orientation=6 говорит «поверни обратно» (как у телефона);
    noise — гауссов шум и серый фон с низким контрастом (дешёвая камера, плохой свет)."""
    from PIL import Image, ImageDraw, ImageFont

    font = ImageFont.truetype(dejavu_font_path(), 30)
    lines = PHOTO_LINES if lines is None else lines
    im = Image.new("L", (1400, 120 + 52 * len(lines)), 255)
    draw = ImageDraw.Draw(im)
    for i, line in enumerate(lines):
        draw.text((60, 60 + 52 * i), line, fill=0, font=font)
    if noise:
        grain = Image.effect_noise(im.size, 40)
        im = Image.blend(im, grain, 0.22)
        im = im.point(lambda v: int(40 + v * 0.75))           # серый фон, низкий контраст
    exif = Image.Exif()
    if exif_rotated:
        im = im.rotate(90, expand=True)                        # пиксели «лёжа», как с датчика телефона
        exif[0x0112] = 6                                       # Orientation: показать, повернув на 90° по часовой
    exif[0x010F] = "CANARY-CAMERA"                             # Make: метаданные не должны никуда уйти
    buf = io.BytesIO()
    if fmt == "JPEG":
        im.convert("RGB").save(buf, "JPEG", quality=85, exif=exif)
    else:
        im.save(buf, fmt, exif=exif)
    return buf.getvalue()


def tesseract_or_skip() -> None:
    """SKIP, если нет бинарника tesseract (локальная разработка на macOS); в контейнере verify он есть."""
    import shutil
    if shutil.which("tesseract") is None:
        skip("нет tesseract: распознавание фото проверяется в контейнере verify")
