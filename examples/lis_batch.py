#!/usr/bin/env python3
"""Пакетная обработка выгрузки ЛИС через API DeficitLens.

Что делает: читает CSV выгрузки лабораторной информационной системы, отправляет строки пакетами
в POST /api/v1/batch и пишет CSV с результатом по каждой строке: анемия по ВОЗ, группа, класс, причина,
сигнал скрытого дефицита, нужно ли досдать ферритин, что досдать.

Персональные данные:
  * на сервер уходят только пол, возраст в годах и значения анализов из белого списка показателей;
  * столбцы с ФИО, датой рождения, полисом, телефоном, адресом не читаются и никуда не передаются;
  * идентификатор пациента на сервер НЕ отправляется: вместо него идёт номер строки. Если задан --id-column,
    идентификатор копируется из входного файла в выходной на вашей машине, минуя сервер;
  * значения анализов в выходной файл не пишутся и на экран не выводятся;
  * выходной CSV безопасно открывать в Excel: все ячейки в кавычках, текст, начинающийся (в том числе после
    пробелов) с = + - @, табуляции, возврата каретки или полноширинных ＝ ＋ － ＠, получает апостроф в начале —
    формула из идентификатора во входном файле не исполнится;
  * сам сервис ничего не хранит.

Формат входа: CSV с заголовком, разделитель «,», «;» или табуляция, кодировка utf-8 или cp1251,
десятичная запятая допускается. Обязательные столбцы: пол (sex / пол / gender) и возраст (age_years / age /
возраст). Столбцы анализов — канонические коды (hemoglobin, MCV, ferritin …) или свои названия через --map.
Значения — в канонических единицах (гемоглобин — г/л, ферритин — мкг/л; см. GET /api/v1/reference).
Пустая ячейка = анализ не сдан.

Установка:  pip install requests
Пример:
    export DL_API_KEY=dl-demo-key
    python3 examples/lis_batch.py --in lis_export.csv --out result.csv --id-column "Номер заказа" \\
        --map "Гемоглобин=hemoglobin" --map "Ферритин=ferritin"
"""
from __future__ import annotations

import argparse
import csv
import io
import os
import re
import sys
import time
from pathlib import Path

import requests

# Белый список показателей: канонические коды (имена столбцов файла кейса). Всё остальное игнорируется.
ANALYTES = [
    "hemoglobin", "RBC", "hematocrit", "MCV", "MCH", "MCHC", "RDW", "platelets", "WBC",
    "ferritin", "serum_iron", "transferrin", "TIBC", "UIBC", "TSAT", "sTfR", "Ret_He",
    "vitamin_B12", "active_B12", "MMA", "homocysteine", "folate", "copper", "ceruloplasmin", "vitamin_B6",
    "CRP", "ESR", "LDH", "indirect_bilirubin", "haptoglobin", "reticulocytes", "creatinine", "eGFR", "TSH", "albumin",
]
SEX_COLUMNS = ("sex", "пол", "gender")
AGE_COLUMNS = ("age_years", "age", "возраст")
SEX_VALUES = {"f": "F", "female": "F", "ж": "F", "жен": "F", "женский": "F",
              "m": "M", "male": "M", "м": "M", "муж": "M", "мужской": "M"}
OUT_COLUMNS = ["row", "local_id", "status", "anemia", "severity", "case_group", "anemia_class", "deficiency_cause",
               "confidence", "hidden_deficiency", "screening_risk", "recommend_ferritin", "flags", "next_tests",
               "error"]


FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")
FORMULA_CHARS = frozenset("=+-@\uff1d\uff0b\uff0d\uff20")


def escape_cell(v):
    """Текстовая ячейка, похожая на формулу, получает апостроф в начале (защита от CSV injection). Числа — как есть."""
    if not isinstance(v, str) or not v:
        return v
    stripped = v.lstrip()
    if v.startswith(FORMULA_PREFIXES) or (stripped and stripped[0] in FORMULA_CHARS):
        return "'" + v
    return v


def write_result_csv(path, rows: list[dict]) -> None:
    """Результат -> CSV: все ячейки в кавычках, формулы экранированы."""
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=OUT_COLUMNS, quoting=csv.QUOTE_ALL)
        w.writeheader()
        w.writerows({k: escape_cell(v) for k, v in row.items()} for row in rows)


def norm(s: str) -> str:
    return re.sub(r"\s+", "", str(s)).lower()


def read_csv(path: Path) -> tuple[list[str], list[dict]]:
    raw = path.read_bytes()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("cp1251")
    header = next((ln for ln in text.splitlines() if ln.strip()), "")
    sep = max((";", ",", "\t"), key=header.count)
    reader = csv.DictReader(io.StringIO(text), delimiter=sep)
    return [c.strip() for c in (reader.fieldnames or [])], list(reader)


def to_number(cell: str) -> float | None:
    """Ячейка -> число. Пустая ячейка -> None (анализ не сдан). Нечисловой текст -> ValueError."""
    s = (cell or "").strip()
    if not s or s.lower() in ("na", "n/a", "nan", "null", "none", "-"):
        return None
    return float(s.replace(",", ".") if re.fullmatch(r"[+-]?\d+,\d+", s) else s)


def build_records(columns: list[str], rows: list[dict], mapping: dict[str, str]) -> tuple[list[dict | None], list[str]]:
    """Строки файла -> записи AnalysisInput. Возвращает (записи или None для строк с локальной ошибкой, ошибки)."""
    by_norm = {norm(c): c for c in columns}
    sex_col = next((by_norm[n] for n in SEX_COLUMNS if n in by_norm), None)
    age_col = next((by_norm[n] for n in AGE_COLUMNS if n in by_norm), None)
    if not sex_col or not age_col:
        raise SystemExit("В файле нет столбца пола (sex / пол) или возраста (age_years / age / возраст).")
    analyte_cols: dict[str, str] = {}
    canon = {norm(a): a for a in ANALYTES}
    for col in columns:
        code = mapping.get(col) or canon.get(norm(col))
        if code in ANALYTES and code not in analyte_cols:
            analyte_cols[code] = col
    if "hemoglobin" not in analyte_cols:
        raise SystemExit("В файле нет столбца гемоглобина (hemoglobin). Задайте его через --map \"Название=hemoglobin\".")
    print(f"Распознано показателей: {len(analyte_cols)} ({', '.join(analyte_cols)})", file=sys.stderr)

    records: list[dict | None] = []
    errors: list[str] = []
    for i, row in enumerate(rows, start=1):
        try:
            sex = SEX_VALUES.get(norm(row.get(sex_col) or ""))
            if sex is None:
                raise ValueError("пол не распознан")
            age = to_number(row.get(age_col) or "")
            if age is None:
                raise ValueError("возраст не указан")
            values = {}
            for code, col in analyte_cols.items():
                try:
                    v = to_number(row.get(col) or "")
                except ValueError:
                    raise ValueError(f"{code}: значение не является числом") from None
                if v is not None:
                    values[code] = v
            records.append({"patient_ref": f"row-{i}", "sex": sex, "age_years": int(age), "values": values})
            errors.append("")
        except ValueError as e:
            records.append(None)          # строка не отправляется; значение в сообщение об ошибке не попадает
            errors.append(str(e))
    return records, errors


def result_row(i: int, local_id: str, item: dict) -> dict:
    out = dict.fromkeys(OUT_COLUMNS, "")
    out.update(row=i, local_id=local_id, status=item["status"])
    if item["status"] != "done":
        out["error"] = "; ".join(e.get("message", "") for e in item.get("errors", []))
        return out
    r = item["result"]
    l1, l2, hid = r["level1"], r["level2"], r["hidden_deficiency"]
    scr = hid.get("screening") or {}
    out.update(
        anemia=int(bool(l1["anemia"])), severity=l1.get("severity") or "",
        case_group=l2.get("case_group") or "", anemia_class=l2.get("anemia_class") or "",
        deficiency_cause=l2.get("deficiency_cause") or "", confidence=l2.get("confidence") or "",
        hidden_deficiency=int(bool(hid.get("detected"))) if hid.get("applicable") else "",
        screening_risk=scr.get("risk") or "", recommend_ferritin=int(bool(scr.get("recommend_ferritin"))),
        flags=";".join(f["code"] for f in r.get("flags", [])),
        next_tests=";".join(t["analyte"] for t in r.get("next_tests", [])),
    )
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="Выгрузка ЛИС (CSV) -> API DeficitLens -> CSV с результатом")
    ap.add_argument("--in", dest="inp", required=True, help="CSV выгрузки ЛИС")
    ap.add_argument("--out", required=True, help="куда записать CSV с результатом")
    ap.add_argument("--base-url", default=os.environ.get("DL_BASE_URL", "http://127.0.0.1:8080"))
    ap.add_argument("--chunk", type=int, default=500, help="записей в одном запросе (по умолчанию 500)")
    ap.add_argument("--id-column", help="столбец с идентификатором: копируется в результат локально, на сервер не уходит")
    ap.add_argument("--map", action="append", default=[], metavar="СТОЛБЕЦ=КОД",
                    help="соответствие столбца файла коду показателя, например \"Гемоглобин=hemoglobin\"")
    ap.add_argument("--timeout", type=float, default=300.0)
    a = ap.parse_args()

    key = os.environ.get("DL_API_KEY")
    if not key:
        print("Задайте ключ API: export DL_API_KEY=<ключ>", file=sys.stderr)
        return 2
    mapping = dict(m.split("=", 1) for m in a.map if "=" in m)
    bad = [code for code in mapping.values() if code not in ANALYTES]
    if bad:
        print(f"Неизвестные коды показателей в --map: {', '.join(bad)}", file=sys.stderr)
        return 2

    columns, rows = read_csv(Path(a.inp))
    if a.id_column and a.id_column not in columns:
        print(f"Столбца «{a.id_column}» нет в файле.", file=sys.stderr)
        return 2
    records, local_errors = build_records(columns, rows, mapping)
    local_ids = [(row.get(a.id_column) or "").strip() if a.id_column else "" for row in rows]

    results: dict[int, dict] = {}                   # номер строки (с 1) -> элемент ответа
    to_send = [(i, rec) for i, rec in enumerate(records, start=1) if rec is not None]
    session = requests.Session()
    session.headers["X-API-Key"] = key
    t0 = time.perf_counter()
    for start in range(0, len(to_send), a.chunk):
        part = to_send[start:start + a.chunk]
        r = session.post(a.base_url.rstrip("/") + "/api/v1/batch", timeout=a.timeout,
                         json={"records": [rec for _, rec in part], "with_reports": False})
        if r.status_code != 200:
            try:
                msg = r.json().get("error", {}).get("message", "")
            except ValueError:
                msg = ""
            print(f"Сервис ответил HTTP {r.status_code}: {msg}", file=sys.stderr)
            return 1
        for (i, _), item in zip(part, r.json()["items"]):
            results[i] = item
        print(f"Отправлено {min(start + a.chunk, len(to_send))} из {len(to_send)}", file=sys.stderr)
    seconds = time.perf_counter() - t0

    out_rows = []
    for i in range(1, len(rows) + 1):
        if i in results:
            out_rows.append(result_row(i, local_ids[i - 1], results[i]))
        else:
            row = dict.fromkeys(OUT_COLUMNS, "")
            row.update(row=i, local_id=local_ids[i - 1], status="rejected", error=local_errors[i - 1])
            out_rows.append(row)
    write_result_csv(a.out, out_rows)

    done = sum(1 for r in out_rows if r["status"] == "done")
    print(f"Строк: {len(rows)}; посчитано: {done}; отклонено: {len(rows) - done}; время запросов: {seconds:.1f} с")
    print(f"Анемия по ВОЗ: {sum(1 for r in out_rows if r['anemia'] == 1)}")
    print(f"Сигнал скрытого дефицита при нормальном гемоглобине: {sum(1 for r in out_rows if r['hidden_deficiency'] == 1)}")
    print(f"Рекомендован ферритин по скринингу: {sum(1 for r in out_rows if r['recommend_ferritin'] == 1)}")
    print(f"Результат: {a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
