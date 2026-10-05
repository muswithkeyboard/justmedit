#!/usr/bin/env python3
"""Замер производительности DeficitLens и запись результата в docs/perf.md.

Что измеряется (всё — на той машине, где запущен скрипт; сеть и веб-сервер не участвуют):
  1. engine.analyze — полный разбор одного анализа (нормализация, уровень 1, правила, уровень 2, скрининг,
     следующий анализ, оба отчёта) на восьми примерах из data/demo/examples.json: N повторов каждого примера,
     p50 и p95 времени в миллисекундах;
  2. io.table.predict_table — пакетная расшифровка таблицы в схеме кейса на 840, 10 000 и 100 000 строк.
     Если есть файл кейса (data/case/deficiency_anemia.csv или DL_CASE_DATA) — его строки повторяются до нужного
     размера; иначе таблица собирается из восьми примеров (тогда анализов вне ОАК меньше, чем в файле кейса);
  3. пиковая память процесса (ru_maxrss) к концу каждого этапа.

Значения анализов никуда не пишутся: в docs/perf.md попадают только времена, размеры и параметры машины.

Запуск:  PYTHONPATH=src python3 scripts/perf.py [--repeats 200] [--sizes 840,10000,100000] [--out docs/perf.md]
                                                 [--note "условия замера"] [--json raw.json]
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import resource
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

LABEL_COLUMNS = ["anemia", "anemia_class", "deficiency_cause", "anemia_cause"]


def peak_rss_mb() -> float:
    """Пиковая резидентная память процесса, МБ (в Linux ru_maxrss — в килобайтах, в macOS — в байтах)."""
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return rss / (1024 * 1024) if sys.platform == "darwin" else rss / 1024


def machine() -> dict:
    cpu = platform.processor() or ""
    mem_gb = None
    try:
        for line in Path("/proc/cpuinfo").read_text().splitlines():
            if line.startswith("model name"):
                cpu = line.split(":", 1)[1].strip()
                break
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemTotal"):
                mem_gb = round(int(line.split()[1]) / 1024 / 1024, 1)
                break
    except OSError:
        pass
    import joblib
    import pydantic
    import sklearn
    return {
        "cpu": cpu or "не определён", "cpus": os.cpu_count(), "mem_gb": mem_gb,
        "os": f"{platform.system()} {platform.release()}", "python": platform.python_version(),
        "numpy": np.__version__, "pandas": pd.__version__, "scikit-learn": sklearn.__version__,
        "joblib": joblib.__version__, "pydantic": pydantic.__version__,
    }


def pct(a, q: float) -> float:
    return float(np.percentile(np.asarray(a, dtype=float), q))


def fmt(x: float, digits: int = 1) -> str:
    """Число по-русски: десятичная запятая, тысячи через пробел."""
    s = f"{x:,.{digits}f}".replace(",", " ").replace(".", ",")
    return s


def measure_analyze(repeats: int) -> dict:
    from deficitlens_core.engine import analyze, load_models
    from deficitlens_core.schemas import AnalysisInput

    examples = json.loads((ROOT / "data" / "demo" / "examples.json").read_text(encoding="utf-8"))["examples"]
    t0 = time.perf_counter()
    models = load_models()
    load_s = time.perf_counter() - t0
    inputs = [(e["id"], AnalysisInput.model_validate(e["input"])) for e in examples]

    cold = []
    for _id, inp in inputs:                       # первый вызов каждого примера: прогрев кэшей и ленивых импортов
        t = time.perf_counter()
        analyze(inp, models=models)
        cold.append(1000 * (time.perf_counter() - t))

    per_example, all_ms = [], []
    for ex_id, inp in inputs:
        ms = []
        for _ in range(repeats):
            t = time.perf_counter()
            analyze(inp, models=models)
            ms.append(1000 * (time.perf_counter() - t))
        per_example.append({"id": ex_id, "p50": pct(ms, 50), "p95": pct(ms, 95), "max": max(ms)})
        all_ms += ms
    return {
        "models_loaded": {"case": models.case is not None, "screen": models.screen is not None},
        "load_models_s": load_s, "first_call_ms": cold[0], "repeats": repeats, "calls": len(all_ms),
        "p50": pct(all_ms, 50), "p95": pct(all_ms, 95), "max": max(all_ms),
        "per_second_one_thread": 1000.0 / (sum(all_ms) / len(all_ms)),
        "per_example": per_example, "peak_rss_mb": peak_rss_mb(),
    }


def base_table() -> tuple[pd.DataFrame, str]:
    """Исходная таблица для повторения: файл кейса, если он есть, иначе восемь примеров (без беременности)."""
    case = os.environ.get("DL_CASE_DATA") or str(ROOT / "data" / "case" / "deficiency_anemia.csv")
    if Path(case).exists():
        from deficitlens_core.io.table import read_table
        return read_table(case), "строки учебного набора кейса (840 строк), повторённые до нужного размера"
    examples = json.loads((ROOT / "data" / "demo" / "examples.json").read_text(encoding="utf-8"))["examples"]
    rows = [{"patient_id": e["id"], "sex": e["input"]["sex"], "age_years": e["input"]["age_years"],
             **e["input"]["values"]} for e in examples]
    return pd.DataFrame(rows), ("восемь демонстрационных примеров, повторённые до нужного размера "
                                "(файла кейса нет: анализов вне ОАК в таких строках меньше)")


def tile(df: pd.DataFrame, n: int) -> pd.DataFrame:
    reps = -(-n // len(df))
    out = pd.concat([df] * reps, ignore_index=True).iloc[:n].copy()
    out.attrs = dict(df.attrs)
    return out


def measure_predict(sizes: list[int]) -> dict:
    from deficitlens_core.io.table import predict_table

    base, source = base_table()
    has_labels = any(c in base.columns for c in LABEL_COLUMNS)
    no_labels = base.drop(columns=[c for c in LABEL_COLUMNS if c in base.columns])
    predict_table(tile(no_labels, min(64, len(base))), mode="clinical")          # прогрев: загрузка модели
    variants = [("clinical", "клинический, без меток", no_labels), ("benchmark", "бенчмарка, без меток", no_labels)]
    if has_labels:
        variants.append(("auto", "auto, файл с метками (считаются оба режима и метрики)", base))
    rows = []
    for n in sizes:
        for mode, title, table in variants:
            df = tile(table, n)
            t = time.perf_counter()
            result, summary = predict_table(df, mode=mode)
            sec = time.perf_counter() - t
            rows.append({"rows": n, "mode": mode, "title": title, "seconds": sec,
                         "rows_per_second": n / sec, "ms_per_row": 1000 * sec / n,
                         "chosen_mode": summary["mode"], "done": summary["rows_done"],
                         "rejected": summary["rows_rejected"], "peak_rss_mb": peak_rss_mb()})
            del df, result
    return {"source": source, "rows": rows}


def render(m: dict, a: dict, p: dict, argv: str, note: str = "") -> str:
    now = datetime.now(timezone.utc).strftime("%d.%m.%Y, %H:%M UTC")
    L: list[str] = []
    L += ["# Производительность", "",
          "Этот файл создаёт `scripts/perf.py`. Числа ниже — фактический замер, а не оценка. "
          "Правьте скрипт, а не файл.", "",
          f"Замер: {now}.", "",
          "## Машина", "",
          "| Параметр | Значение |", "|---|---|",
          f"| Процессор | {m['cpu']} |",
          f"| Ядер (логических) | {m['cpus']} |",
          f"| Память | {fmt(m['mem_gb'])} ГБ |" if m["mem_gb"] else "| Память | не определена |",
          f"| ОС | {m['os']} |",
          f"| Python | {m['python']} |",
          f"| numpy / pandas / scikit-learn | {m['numpy']} / {m['pandas']} / {m['scikit-learn']} |",
          f"| joblib / pydantic | {m['joblib']} / {m['pydantic']} |", "",
          "Замер идёт в одном процессе, без веб-сервера и сети. Предсказание бустинга работает в один поток "
          "(так сделано в коде: при занятых ядрах два потока медленнее одного).", ""]
    if note:
        L += [f"Условия замера: {note}", ""]

    L += ["## Один анализ: `engine.analyze`", "",
          "Полный разбор: нормализация, уровень 1, правила, уровень 2, скрининг, следующий анализ, оба отчёта. "
          f"Восемь примеров из `data/demo/examples.json`, по {a['repeats']} повторов каждого "
          f"({a['calls']} вызовов).", "",
          "| Что | Значение |", "|---|---|",
          f"| Модели загружены | case-v1: {'да' if a['models_loaded']['case'] else 'нет'}; "
          f"screen-v1: {'да' if a['models_loaded']['screen'] else 'нет'} |",
          f"| Загрузка моделей при старте | {fmt(a['load_models_s'], 2)} с |",
          f"| Первый вызов после загрузки | {fmt(a['first_call_ms'])} мс |",
          f"| p50 | {fmt(a['p50'])} мс |",
          f"| p95 | {fmt(a['p95'])} мс |",
          f"| Наибольшее время | {fmt(a['max'])} мс |",
          f"| Анализов в секунду, один поток | {fmt(a['per_second_one_thread'], 0)} |",
          f"| Пиковая память процесса после этапа | {fmt(a['peak_rss_mb'], 0)} МБ |", "",
          "По примерам:", "", "| Пример | p50, мс | p95, мс |", "|---|---|---|"]
    L += [f"| `{e['id']}` | {fmt(e['p50'])} | {fmt(e['p95'])} |" for e in a["per_example"]]
    L += ["", "Пример 8 (беременность) быстрее остальных: при беременности уровень 2 и модели выключены.", ""]

    L += ["## Пакет: `predict_table`", "",
          "Таблица в схеме кейса → класс, группа, причина и флаг скрытого дефицита для каждой строки. "
          "Вероятности считаются одним векторным вызовом модели; отчёты в пакете не строятся.", "",
          f"Данные: {p['source']}.", "",
          "| Строк | Режим | Время, с | Строк в секунду | мс на строку | Принято / отклонено | Пик памяти, МБ |",
          "|---|---|---|---|---|---|---|"]
    for r in p["rows"]:
        L.append(f"| {fmt(r['rows'], 0)} | {r['title']} | {fmt(r['seconds'], 2)} | {fmt(r['rows_per_second'], 0)} | "
                 f"{fmt(r['ms_per_row'], 3)} | {fmt(r['done'], 0)} / {fmt(r['rejected'], 0)} | "
                 f"{fmt(r['peak_rss_mb'], 0)} |")
    L += ["",
          "«Пик памяти» — наибольшая резидентная память процесса с начала замера до конца этой строки таблицы "
          "(значение не убывает). Чтение и запись файла в замер не входят.", "",
          "Режим `auto` на файле с метками считает оба режима и метрики, поэтому он медленнее.", ""]

    L += ["## Сравнение с целями плана", "",
          "Цели проекта: p95 `engine.analyze` меньше 150 мс; `predict` на 10 000 строк меньше 30 с.", ""]
    ten = [r for r in p["rows"] if r["rows"] == 10000]
    worst = max((r["seconds"] for r in ten), default=None)
    L += ["| Цель | Факт | Выполнено |", "|---|---|---|",
          f"| p95 `engine.analyze` < 150 мс | {fmt(a['p95'])} мс | {'да' if a['p95'] < 150 else 'нет'} |"]
    if worst is not None:
        L.append(f"| 10 000 строк < 30 с | {fmt(worst, 2)} с (самый медленный режим) | "
                 f"{'да' if worst < 30 else 'нет'} |")
    L += ["",
          "## Чего в этом замере нет", "",
          "- Нет замера через HTTP: время сети, разбора JSON на входе и сериализации ответа сюда не входит.",
          "- Нет нагрузочного теста с параллельными запросами.",
          "- Нет замера в контейнере Docker.",
          "- Маршрут `/api/v1/batch` вызывает `engine.analyze` для каждой записи; его скорость оценивается "
          "по строке «анализов в секунду», а не по таблице `predict_table`.", "",
          "## Масштабирование", "",
          "Расчёты без состояния: каждый запрос анализа обрабатывается независимо, общих данных между запросами "
          "нет, кроме моделей и конфигурации, которые загружаются при старте и только читаются. Поэтому расчёты "
          "можно делить между несколькими копиями (репликами) контейнера за балансировщиком. Кабинет — SQLite "
          "в одном процессе: всё, что связано со входом, обслуживает один экземпляр (`docs/architecture.md`, "
          "«Как масштабируется»). Ограничение: лимиты запросов и слоты тяжёлых расчётов считаются в памяти "
          "процесса, то есть отдельно в каждой реплике. Работа в несколько реплик в этой среде не проверялась.", "",
          "## Как воспроизвести", "", "```bash", f"PYTHONPATH=src python3 scripts/perf.py {argv}".rstrip(), "```", "",
          "Файл кейса берётся из `data/case/deficiency_anemia.csv` или из переменной `DL_CASE_DATA`. "
          "Без него таблица собирается из восьми примеров.", ""]
    return "\n".join(L)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--repeats", type=int, default=200)
    ap.add_argument("--sizes", default="840,10000,100000")
    ap.add_argument("--out", default=str(ROOT / "docs" / "perf.md"))
    ap.add_argument("--json", default=None, help="куда дополнительно записать сырые числа (JSON)")
    ap.add_argument("--note", default="", help="фраза об условиях замера (попадает в отчёт)")
    args = ap.parse_args()
    sizes = [int(s) for s in args.sizes.split(",") if s.strip()]

    m = machine()
    print(f"[perf] машина: {m['cpu']}, ядер {m['cpus']}", file=sys.stderr)
    a = measure_analyze(args.repeats)
    print(f"[perf] analyze: p50 {a['p50']:.1f} мс, p95 {a['p95']:.1f} мс", file=sys.stderr)
    p = measure_predict(sizes)
    for r in p["rows"]:
        print(f"[perf] predict_table {r['rows']} строк, {r['mode']}: {r['seconds']:.2f} с, "
              f"пик {r['peak_rss_mb']:.0f} МБ", file=sys.stderr)
    argv = f"--repeats {args.repeats} --sizes {args.sizes}"
    Path(args.out).write_text(render(m, a, p, argv, args.note), encoding="utf-8")
    if args.json:
        Path(args.json).write_text(json.dumps({"machine": m, "analyze": a, "predict": p}, ensure_ascii=False,
                                              indent=2), encoding="utf-8")
    print(f"[perf] записано: {args.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
