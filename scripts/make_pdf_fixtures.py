#!/usr/bin/env python3
"""Собирает придуманные PDF-бланки (без ПДн) для тестов разбора PDF любой лаборатории (поток П2) из HTML через LibreOffice.

    python3 scripts/make_pdf_fixtures.py [путь_к_soffice] [имя_фикстуры …]

Источник — tests/fixtures/pdf/*.html (придуманные значения и «ПДн»: ФИО, полис, № заказа — ненастоящие),
результат — tests/fixtures/pdf/<имя>.pdf рядом. PDF собраны один раз и лежат в git: LibreOffice в тестах не нужен.
С именами (например, `g_urine_first` или `g_urine_first.html`) собираются только эти фикстуры: остальные PDF
не пересобираются и не меняются.
Профиль LibreOffice — временный каталог (настройки пользователя не трогаются и в PDF не попадают).
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "pdf"
SOFFICE_CANDIDATES = ("/Applications/LibreOffice.app/Contents/MacOS/soffice", "soffice", "libreoffice")


def find_soffice(explicit: str | None) -> str:
    for cand in ([explicit] if explicit else []) + list(SOFFICE_CANDIDATES):
        path = shutil.which(cand) or (cand if Path(cand).is_file() else None)
        if path:
            return path
    sys.exit("не найден soffice (LibreOffice): укажите путь первым аргументом")


def main(argv: list[str]) -> int:
    args = argv[1:]
    names = [a for a in args if (FIXTURES / Path(a).name).with_suffix(".html").is_file()]
    explicit = [a for a in args if a not in names]
    soffice = find_soffice(explicit[0] if explicit else None)
    wanted = {Path(a).stem for a in names}
    sources = sorted(p for p in FIXTURES.glob("*.html") if not wanted or p.stem in wanted)
    if not sources:
        sys.exit(f"нет HTML в {FIXTURES}")
    with tempfile.TemporaryDirectory(prefix="dl-lo-") as tmp:
        profile = Path(tmp) / "profile"
        out_dir = Path(tmp) / "out"
        out_dir.mkdir()
        cmd = [soffice, f"-env:UserInstallation={profile.as_uri()}", "--headless", "--convert-to", "pdf",
               "--outdir", str(out_dir), *map(str, sources)]
        subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=300)
        for src in sources:
            pdf = out_dir / (src.stem + ".pdf")
            if not pdf.is_file():
                sys.exit(f"LibreOffice не собрал {pdf.name}")
            shutil.copyfile(pdf, FIXTURES / pdf.name)
            print(f"{(FIXTURES / pdf.name).relative_to(ROOT)}  {pdf.stat().st_size} байт")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
