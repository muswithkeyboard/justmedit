#!/usr/bin/env python3
"""Запуск тестов без pytest: находит tests/**/test_*.py, вызывает функции test_*, печатает PASS / FAIL / SKIP.
Те же файлы запускаются обычным `pytest`. Использование: python3 scripts/run_tests.py [подстрока_имени ...]"""
from __future__ import annotations

import importlib.util
import sys
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "tests")]
from helpers import SkipTest  # noqa: E402


def main(filters: list[str]) -> int:
    passed = failed = skipped = 0
    t0 = time.time()
    for path in sorted((ROOT / "tests").rglob("test_*.py")):
        spec = importlib.util.spec_from_file_location(path.stem, path)
        mod = importlib.util.module_from_spec(spec)
        try:
            spec.loader.exec_module(mod)
        except SkipTest as e:
            print(f"SKIP  {path.relative_to(ROOT)} — {e}")
            skipped += 1
            continue
        except BaseException as e:  # pytest.skip — BaseException (Skipped), если pytest установлен
            if type(e).__name__ != "Skipped":
                if not isinstance(e, Exception):
                    raise
                print(f"FAIL  {path.relative_to(ROOT)} (ошибка импорта)\n{traceback.format_exc()}")
                failed += 1
                continue
            print(f"SKIP  {path.relative_to(ROOT)} — {e}")
            skipped += 1
            continue
        except Exception:
            print(f"FAIL  {path.relative_to(ROOT)} (ошибка импорта)\n{traceback.format_exc()}")
            failed += 1
            continue
        for name in sorted(n for n in dir(mod) if n.startswith("test_") and callable(getattr(mod, n))):
            full = f"{path.relative_to(ROOT)}::{name}"
            if filters and not any(f in full for f in filters):
                continue
            try:
                getattr(mod, name)()
                passed += 1
                print(f"PASS  {full}")
            except SkipTest as e:
                skipped += 1
                print(f"SKIP  {full} — {e}")
            except BaseException as e:  # pytest.skip — BaseException (Skipped), если pytest установлен
                if not isinstance(e, Exception) and type(e).__name__ != "Skipped":
                    raise  # KeyboardInterrupt, SystemExit — не глушить
                if type(e).__name__ == "Skipped":
                    skipped += 1
                    print(f"SKIP  {full} — {e}")
                else:
                    failed += 1
                    print(f"FAIL  {full}\n{traceback.format_exc()}")
    print(f"\nИтог: PASS {passed}, FAIL {failed}, SKIP {skipped}, {time.time() - t0:.1f} с")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
