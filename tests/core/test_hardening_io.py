"""Этап 4а: защита чтения файлов, экранирование CSV, ПДн в разборе текста бланка, PDF.

Конкретный ввод -> ожидаемый безопасный вывод. Все значения и «персональные данные» придуманы.
"""
from __future__ import annotations

import csv
import io
import zipfile

import pandas as pd
from helpers import pdf_with_text as _pdf_with_text  # helpers добавляет src в sys.path

from deficitlens_core.config import load_config
from deficitlens_core.io.table import MAX_COLUMNS, TableLimitError, escape_cell, read_table_bytes, to_safe_csv
from deficitlens_core.io.text_parser import parse_lab_text
from deficitlens_core.schemas import InputError


def _raises(fn, exc_type):
    try:
        fn()
    except exc_type as e:
        return e
    raise AssertionError(f"ожидалось исключение {exc_type.__name__}")


def _xlsx_bytes(df: pd.DataFrame) -> bytes:
    buf = io.BytesIO()
    df.to_excel(buf, index=False, engine="openpyxl")
    return buf.getvalue()


# ---- п. 1: пределы чтения таблиц -------------------------------------------------------------
def test_wide_csv_rejected_by_header_before_parsing():
    header = ",".join(f"c{i}" for i in range(MAX_COLUMNS + 1))
    e = _raises(lambda: read_table_bytes((header + "\n" + "1," * MAX_COLUMNS + "1\n").encode(), ".csv"),
                TableLimitError)
    assert e.code == "too_many_columns" and str(MAX_COLUMNS) in e.message
    ok = ",".join(f"c{i}" for i in range(MAX_COLUMNS))
    assert len(read_table_bytes((ok + "\n").encode(), ".csv").columns) == MAX_COLUMNS
    semi = ";".join(f"c{i}" for i in range(MAX_COLUMNS + 5))                 # разделитель «;» — тот же предел
    assert _raises(lambda: read_table_bytes(semi.encode(), ".csv"), TableLimitError).code == "too_many_columns"


def test_rows_limit_is_reject_not_truncate():
    body = "sex,age_years,hemoglobin\n" + "F,30,120\n" * 11
    assert len(read_table_bytes(body.encode(), ".csv", max_rows=11)) == 11
    e = _raises(lambda: read_table_bytes(body.encode(), ".csv", max_rows=10), TableLimitError)
    assert e.code == "too_many_rows"
    xlsx = _xlsx_bytes(pd.DataFrame({"sex": ["F"] * 11, "age_years": [30] * 11}))
    assert _raises(lambda: read_table_bytes(xlsx, ".xlsx", max_rows=10), TableLimitError).code == "too_many_rows"


def test_xlsx_bomb_rejected_before_reading():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:            # 60 МБ нулей сжимаются в десятки КБ
        z.writestr("xl/sharedStrings.xml", b"\0" * (60 * 1024 * 1024))
    data = buf.getvalue()
    assert len(data) < 1024 * 1024
    e = _raises(lambda: read_table_bytes(data, ".xlsx", max_unpacked=50 * 1024 * 1024), TableLimitError)
    assert e.code == "file_too_large" and "50 МБ" in e.message
    e = _raises(lambda: read_table_bytes(b"not a zip", ".xlsx", max_unpacked=5 * 1024 * 1024), TableLimitError)
    assert e.code == "invalid_file"


def test_wide_xlsx_rejected():
    xlsx = _xlsx_bytes(pd.DataFrame([[1] * (MAX_COLUMNS + 1)], columns=[f"c{i}" for i in range(MAX_COLUMNS + 1)]))
    assert _raises(lambda: read_table_bytes(xlsx, ".xlsx"), TableLimitError).code == "too_many_columns"
    small = _xlsx_bytes(pd.DataFrame({"sex": ["F"], "age_years": [30], "hemoglobin": [120]}))
    df = read_table_bytes(small, ".xlsx", max_rows=10, max_unpacked=5 * 1024 * 1024)
    assert list(df.columns) == ["sex", "age_years", "hemoglobin"] and df.attrs["source"]["format"] == "xlsx"


# ---- п. 3: формулы в CSV ----------------------------------------------------------------------
FORMULAS = ["=HYPERLINK(\"http://x\")", "+1", "-1", "@SUM(A1)", "\t=1", "\r=1", "  =1+1", "　=1", "\xa0@x",
            "＝cmd|' /C calc'!A0", "＋1", "－1", "＠x", " ＝1"]


def test_escape_cell_variants():
    for v in FORMULAS:
        assert escape_cell(v) == "'" + v, repr(v)
    for v in ("P001", "row_1", "", "Иванов", "a=b", "1-2"):
        assert escape_cell(v) == v, repr(v)
    assert escape_cell(-1.5) == -1.5 and escape_cell(None) is None


def test_formula_in_patient_id_is_quoted_and_escaped():
    df = pd.DataFrame({"patient_id": FORMULAS, "confidence": [0.5] * len(FORMULAS)})
    text = to_safe_csv(df)
    rows = list(csv.reader(io.StringIO(text)))
    assert rows[0] == ["patient_id", "confidence"]
    assert [r[0] for r in rows[1:]] == ["'" + v for v in FORMULAS]
    # все ячейки в кавычках: каждая строка файла начинается с кавычки, числа — тоже в кавычках
    assert all(line.startswith('"') for line in text.split("\n") if line)
    assert '"0.5"' in text


# ---- п. 6: ПДн в разборе текста бланка -----------------------------------------------------------
HOSTILE = [
    "Гемоглобин Иванов 124 г/л",
    "Гемоглобин пациентки Сидоровой 124",
    "Иванов Иван (HGB) 124",
    "Ферритин (Коваль) 9",
    "Гемоглобин 124 г/л (120-140 Иванов)",
    "Ферритин 9 Сидорова/М.",
    "MCV 82 фл 112-233-445 95",
]
NEIGHBOURS = [
    "Пациент: Иванов Иван Иванович",
    "Телефон +7 912 345-67-89",
    "Полис ОМС 7700 1234 5678 9012",
    "СНИЛС 112-233-445 95",
    "Иванова Мария 34 года",
    "Гемоглобин Петрова 8 912 345 67 89",
    "Ферритин 9 мкг/л тел. 89123456789",
    "Иванов И.И. ферритин 9",
]
LEAKS = ("Иванов", "Сидоров", "Коваль", "Петров", "пациентки", "112-233", "445", "912", "345", "67-89", "7700",
         "1234", "5678", "9012", "89123456789", "тел.", "Полис", "СНИЛС", "Мария")


def _blob(res: dict) -> str:
    return " ".join(res["unrecognized"] + res["notes"]) + " " + " ".join(
        f"{k} {v['value']} {v['unit']}" for k, v in res["values"].items())


def test_hostile_lines_do_not_leak_names_or_numbers():
    cfg = load_config()
    for line in HOSTILE + NEIGHBOURS:
        res = parse_lab_text("Пол: ж Возраст: 34\n" + line, cfg)
        blob = _blob(res)
        for bad in LEAKS:
            assert bad not in blob, (line, bad, blob)
    res = parse_lab_text("\n".join(NEIGHBOURS + HOSTILE), cfg)
    blob = _blob(res)
    for bad in LEAKS:
        assert bad not in blob, (bad, blob)
    # значения из враждебных строк при этом распознаются
    assert res["values"]["ferritin"]["value"] == 9.0 and res["values"]["MCV"]["value"] == 82.0
    assert any(u.startswith("строка 2: ") for u in res["unrecognized"]), "телефон — строка 2 без текста"


# ---- п. 5: PDF ---------------------------------------------------------------------------------
def test_pdf_text_layer_extracted_in_child_process():
    from deficitlens_core.io.pdf_text import extract_text
    r = extract_text(_pdf_with_text(["Hb 124 g/l", "MCV 82 fl", "Ferritin 9"]))
    assert r.pages_total == 1 and r.pages_read == 1
    assert "Hb 124" in r.text and "Ferritin 9" in r.text
    res = parse_lab_text(r.text, load_config())
    assert res["values"]["hemoglobin"]["value"] == 124.0 and res["values"]["ferritin"]["value"] == 9.0


def test_pdf_bad_inputs_are_input_errors():
    from deficitlens_core.io.pdf_text import extract_text
    assert _raises(lambda: extract_text(b"not a pdf"), InputError).code == "bad_pdf"
    assert _raises(lambda: extract_text(b"%PDF-1.4\n garbage garbage"), InputError).code == "bad_pdf"
    assert _raises(lambda: extract_text(_pdf_with_text([])), InputError).code == "no_text_layer"


def test_pdf_hard_timeout_kills_child():
    """Жёсткий таймаут: дочерний процесс, который не успевает, убивается -> pdf_too_complex (без зависания)."""
    import time

    from deficitlens_core.io import pdf_text
    saved = pdf_text.HARD_TIMEOUT_EXTRA
    pdf_text.HARD_TIMEOUT_EXTRA = 0.0
    t0 = time.monotonic()
    try:
        e = _raises(lambda: pdf_text.extract_text(_pdf_with_text(["Hb 124"]), max_seconds=0.01), InputError)
    finally:
        pdf_text.HARD_TIMEOUT_EXTRA = saved
    assert e.code == "pdf_too_complex" and time.monotonic() - t0 < 5
