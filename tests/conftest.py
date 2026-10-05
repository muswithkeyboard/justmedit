"""Общая настройка тестов: пути к src и tests, чтобы тесты запускались и pytest, и scripts/run_tests.py."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for p in (ROOT / "src", ROOT / "tests"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))
