"""Командная строка DeficitLens.

  deficitlens train         — обучить модели (по файлу кейса и по NHANES)
  deficitlens evaluate      — посчитать метрики (кросс-валидация 5×3 по кейсу, временная проверка по NHANES)
  deficitlens predict       — пакетная расшифровка файла (CSV/XLSX в схеме кейса) -> pred.csv и сводка
  deficitlens analyze       — один анализ из JSON -> полный ответ (JSON)
  deficitlens lab-reference — справочник ОАК лаборатории для скрининга (по выгрузке ОАК, без меток)
  deficitlens serve         — запустить веб-сервис
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from .config import REPO_ROOT, models_dir


def _case_path(arg: str | None) -> str | None:
    return arg or os.environ.get("DL_CASE_DATA") or (
        str(REPO_ROOT / "data/case/deficiency_anemia.csv") if (REPO_ROOT / "data/case/deficiency_anemia.csv").exists() else None)


def _nhanes_path(arg: str | None) -> str | None:
    return arg or os.environ.get("DL_NHANES_DATA") or (
        str(REPO_ROOT / "data/nhanes/nhanes_deficiency_real.csv")
        if (REPO_ROOT / "data/nhanes/nhanes_deficiency_real.csv").exists() else None)


def _print(obj) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2, default=str))


def cmd_train(a) -> int:
    out = Path(a.out or models_dir())
    case, nhanes = _case_path(a.case), _nhanes_path(a.nhanes)
    if not case and not nhanes:
        print("Нет данных: задайте --case и/или --nhanes (или DL_CASE_DATA / DL_NHANES_DATA).", file=sys.stderr)
        return 2
    if case and a.what in ("all", "case"):
        from .ml.train_case import train as train_case
        _print({"case": train_case(case, str(out / "case-v1"))})
    if nhanes and a.what in ("all", "screen"):
        from .ml.train_screen import train as train_screen
        _print({"screen": train_screen(nhanes, str(out / "screen-v1"))})
    return 0


def cmd_evaluate(a) -> int:
    out = Path(a.out or REPO_ROOT / "docs" / "metrics")
    out.mkdir(parents=True, exist_ok=True)
    case, nhanes = _case_path(a.case), _nhanes_path(a.nhanes)
    if case and a.what in ("all", "case"):
        from .ml.evaluate_case import evaluate as eval_case
        r = eval_case(case, str(out / "case_metrics.json"))
        _print({"case": r.get("summary", r)})
    if nhanes and a.what in ("all", "screen"):
        from .ml.evaluate_screen import evaluate as eval_screen
        r = eval_screen(nhanes, str(out / "screen_metrics.json"), case)
        _print({"screen": r.get("summary", r)})
    return 0


def cmd_predict(a) -> int:
    from .io.table import predict_file
    summary = predict_file(a.inp, a.out, mode=a.mode, summary_path=a.summary, mapping_path=a.mapping)
    print(json.dumps(summary, ensure_ascii=False, indent=2, default=str), file=sys.stderr)
    return 0


def cmd_analyze(a) -> int:
    from .engine import analyze
    from .schemas import AnalysisInput, InputError
    raw = Path(a.inp).read_text(encoding="utf-8") if a.inp and a.inp != "-" else sys.stdin.read()
    try:
        res = analyze(AnalysisInput.model_validate_json(raw))
    except InputError as e:
        _print(e.body())
        return 1
    print(res.model_dump_json(indent=2))
    return 0


def cmd_lab_reference(a) -> int:
    from .ml.screen import build_lab_reference
    _print(build_lab_reference(a.inp, a.name, write=not a.dry_run))
    return 0


def cmd_serve(a) -> int:
    import uvicorn
    uvicorn.run("deficitlens_api.app:app", host=a.host, port=a.port, log_level="warning")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="deficitlens", description="Justmedit (движок DeficitLens): анемия по ВОЗ, группа и причина, скрытые дефициты")
    sub = p.add_subparsers(dest="cmd", required=True)

    t = sub.add_parser("train", help="обучить модели")
    t.add_argument("--case"); t.add_argument("--nhanes"); t.add_argument("--out")
    t.add_argument("--what", choices=["all", "case", "screen"], default="all"); t.set_defaults(fn=cmd_train)

    e = sub.add_parser("evaluate", help="посчитать метрики")
    e.add_argument("--case"); e.add_argument("--nhanes"); e.add_argument("--out")
    e.add_argument("--what", choices=["all", "case", "screen"], default="all"); e.set_defaults(fn=cmd_evaluate)

    pr = sub.add_parser("predict", help="пакетная расшифровка файла в схеме кейса")
    pr.add_argument("--in", dest="inp", required=True); pr.add_argument("--out", required=True)
    pr.add_argument("--mode", choices=["auto", "clinical", "benchmark"], default="auto")
    pr.add_argument("--summary"); pr.add_argument("--mapping"); pr.set_defaults(fn=cmd_predict)

    an = sub.add_parser("analyze", help="один анализ из JSON")
    an.add_argument("--in", dest="inp", default="-"); an.set_defaults(fn=cmd_analyze)

    lr = sub.add_parser("lab-reference", help="справочник ОАК лаборатории")
    lr.add_argument("--in", dest="inp", required=True); lr.add_argument("--name", required=True)
    lr.add_argument("--dry-run", action="store_true"); lr.set_defaults(fn=cmd_lab_reference)

    s = sub.add_parser("serve", help="запустить веб-сервис")
    s.add_argument("--host", default="127.0.0.1"); s.add_argument("--port", type=int, default=8080); s.set_defaults(fn=cmd_serve)

    a = p.parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
