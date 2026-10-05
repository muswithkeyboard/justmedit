"""Этап 4а: защита веб-слоя — пределы файлов, слоты тяжёлых расчётов, лимит по ключу, CSV без формул, PDF,
журнал без значений (канарейка) и без поддельных строк. Все значения придуманы."""
from __future__ import annotations

import csv
import io
import logging
import zipfile

import helpers

from deficitlens_api.app import create_app
from deficitlens_api.settings import Settings

KEY = "hardening-key-1"
HEADERS = {"X-API-Key": KEY}


def _client(**settings):
    """Новое приложение на каждый вызов: свои лимитеры и слоты."""
    from starlette.testclient import TestClient
    app = create_app(Settings(api_keys=(KEY,), **{"ui_rate_per_min": 10000, **settings}))
    return TestClient(app), app


def _csv_file(text: str, name: str = "x.csv"):
    return {"file": (name, io.BytesIO(text.encode("utf-8")), "text/csv")}


GOOD_CSV = "patient_id,sex,age_years,hemoglobin,MCV,ferritin\nP1,F,30,118,78,8\nP2,M,40,150,90,120\n"


def test_benchmark_rejects_wide_csv_and_xlsx_bomb():
    c, _ = _client()
    wide = ",".join(f"c{i}" for i in range(201)) + "\n" + ",".join("1" for _ in range(201)) + "\n"
    r = c.post("/api/v1/benchmark", files=_csv_file(wide), headers=HEADERS)
    assert r.status_code == 422 and r.json()["error"]["code"] == "too_many_columns", r.text
    assert "200" in r.json()["error"]["message"]
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("xl/sharedStrings.xml", b"\0" * (60 * 1024 * 1024))
    files = {"file": ("bomb.xlsx", io.BytesIO(buf.getvalue()), "application/octet-stream")}
    r = c.post("/ui/benchmark", files=files)
    assert r.status_code == 422 and r.json()["error"]["code"] == "file_too_large", r.text
    assert r.json()["error"]["field"] == "file"


def test_benchmark_rows_limit_from_settings():
    c, _ = _client(max_batch=1)
    r = c.post("/api/v1/benchmark", files=_csv_file(GOOD_CSV), headers=HEADERS)
    assert r.status_code == 422 and r.json()["error"]["code"] == "too_many_rows", r.text
    c, _ = _client(max_batch=2)
    assert c.post("/api/v1/benchmark", files=_csv_file(GOOD_CSV), headers=HEADERS).status_code == 200


def test_formula_in_patient_id_is_quoted_and_escaped():
    c, _ = _client()
    text = ("patient_id,sex,age_years,hemoglobin\n"
            "\"=HYPERLINK(\"\"http://evil\"\")\",F,30,118\n"
            "\" @SUM(A1)\",F,30,118\n"
            "＝cmd,F,30,118\n"
            "\"\t-1\",F,30,118\n")
    r = c.post("/api/v1/benchmark?format=csv", files=_csv_file(text), headers=HEADERS)
    assert r.status_code == 200, r.text
    body = r.text
    rows = list(csv.reader(io.StringIO(body)))
    ids = [row[0] for row in rows[1:]]
    # идентификатор при импорте обрезается от пробелов; «формула после пробелов» — в tests/core/test_hardening_io.py
    assert ids == ["'=HYPERLINK(\"http://evil\")", "'@SUM(A1)", "'＝cmd", "'-1"], ids
    assert all(line.startswith('"') for line in body.split("\n") if line), "все ячейки в кавычках"
    r = c.post("/api/v1/benchmark", files=_csv_file(text), headers=HEADERS)
    assert r.json()["preview"][0]["patient_id"].startswith("'=")
    assert r.json()["csv"] == body


def test_heavy_routes_busy_when_slots_taken():
    c, app = _client()
    gate = app.state.heavy_gate
    assert gate.slots == 2
    with gate.slot(), gate.slot():
        for path, kw in (("/api/v1/benchmark", {"files": _csv_file(GOOD_CSV)}),
                         ("/api/v1/batch", {"json": {"records": []}}),
                         ("/api/v1/columns", {"files": _csv_file(GOOD_CSV)}),
                         ("/api/v1/parse", {"files": _csv_file(GOOD_CSV)})):
            r = c.post(path, headers=HEADERS, **kw)
            assert r.status_code == 503, (path, r.status_code)
            assert r.json()["error"]["code"] == "service_busy" and int(r.headers["retry-after"]) >= 1
        # разбор вставленного текста (~2 мс) слотов не занимает — и в /api/v1, и на странице
        for path, hdrs in (("/api/v1/parse", HEADERS), ("/ui/parse", {})):
            r = c.post(path, json={"text": "Гемоглобин 120"}, headers=hdrs)
            assert r.status_code == 200 and r.json()["values"]["hemoglobin"]["value"] == 120.0, (path, r.text)
        files = {"file": ("x.txt", io.BytesIO("Гемоглобин 120".encode()), "text/plain")}
        assert c.post("/ui/parse", files=files).status_code == 503, "файл — под слотом"
        # лёгкий расчёт одного анализа слотов не занимает
        r = c.post("/api/v1/analyze", json={"sex": "F", "age_years": 30, "values": {"hemoglobin": 120}},
                   headers=HEADERS)
        assert r.status_code == 200
    assert c.post("/api/v1/parse", json={"text": "Гемоглобин 120"}, headers=HEADERS).status_code == 200, \
        "слоты освобождаются"


def test_heavy_api_rate_limit_per_key():
    c, _ = _client(ui_rate_per_min=2)
    codes = [c.post("/api/v1/parse", json={"text": "Гемоглобин 120"}, headers=HEADERS).status_code
             for _ in range(3)]
    assert codes == [200, 200, 429], codes
    r = c.post("/api/v1/batch", json={"records": []}, headers=HEADERS)
    assert r.status_code == 429 and int(r.headers["retry-after"]) >= 1
    assert "на ключ" in r.json()["error"]["message"]
    # одиночный расчёт под лимит тяжёлых запросов не попадает
    r = c.post("/api/v1/analyze", json={"sex": "F", "age_years": 30, "values": {"hemoglobin": 120}},
               headers=HEADERS)
    assert r.status_code == 200


def test_key_compare_uses_compare_digest():
    import inspect

    from deficitlens_api import security
    src = inspect.getsource(security.check_api_key)
    assert "hmac.compare_digest" in src
    security.check_api_key("a", ("a", "b"))
    for bad, code in ((None, 401), ("", 401), ("ab", 403), ("a" * 1000, 403), ("ключ", 403)):
        try:
            security.check_api_key(bad, ("a", "b"))
        except security.ServiceError as e:
            assert e.status == code
        else:
            raise AssertionError(bad)
    assert KEY not in security.key_id(KEY) and security.key_id(KEY) == security.key_id(" " + KEY + " ")


def _pdf(lines):
    return helpers.pdf_with_text(lines)


def test_parse_pdf_and_txt_upload():
    c, _ = _client()
    files = {"file": ("blank.pdf", io.BytesIO(_pdf(["Hb 124 g/l", "Ferritin 9", "Ivanov 8 912 345 67 89"])),
                      "application/pdf")}
    r = c.post("/api/v1/parse", files=files, headers=HEADERS)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["values"]["hemoglobin"]["value"] == 124.0 and body["values"]["ferritin"]["value"] == 9.0
    assert body["source"] == {"format": "pdf", "pages_total": 1, "pages_read": 1}
    assert "Ivanov" not in r.text and "912" not in r.text
    files = {"file": ("blank.txt", io.BytesIO("Гемоглобин 124 г/л\n".encode("cp1251")), "text/plain")}
    r = c.post("/ui/parse", files=files)
    assert r.status_code == 200 and r.json()["values"]["hemoglobin"]["value"] == 124.0
    for name, data, code in (("x.pdf", b"not a pdf", "bad_pdf"), ("x.pdf", _pdf([]), "no_text_layer"),
                             ("x.docx", b"PK..", "unsupported_file"), ("x.pdf", b"", "empty_file")):
        r = c.post("/api/v1/parse", files={"file": (name, io.BytesIO(data), "application/pdf")}, headers=HEADERS)
        assert r.status_code == 422 and r.json()["error"]["code"] == code, (name, r.text)


def test_upload_not_spooled_to_disk():
    """Файл больше 1 МБ (порог Starlette) не сбрасывается во временный файл: SpooledTemporaryFile в памяти."""
    from deficitlens_api.app import _InMemoryMultiPartParser
    assert _InMemoryMultiPartParser.spool_max_size > 100 * 1024 * 1024
    c, _ = _client()
    big = "patient_id,sex,age_years,hemoglobin\n" + ("P" * 140 + ",F,30,118\n") * 9_000     # ~1,4 МБ
    r = c.post("/api/v1/benchmark", files=_csv_file(big), headers=HEADERS)
    assert r.status_code == 200 and r.json()["summary"]["rows_total"] == 9_000


class _Capture(logging.Handler):
    def __init__(self):
        super().__init__()
        self.lines: list[str] = []

    def emit(self, record):
        self.lines.append(record.getMessage())


def test_log_canary_for_files_and_forged_path():
    """Канарейка: приметные значения и идентификатор из файла, PDF и текста не попадают в журнал;
    перевод строки в пути не создаёт поддельную строку журнала."""
    c, _ = _client()
    capture = _Capture()
    logger = logging.getLogger("deficitlens")
    logger.addHandler(capture)
    try:
        csv_text = "patient_id,sex,age_years,hemoglobin\nCANARY-PID-31,F,30,117.31\n"
        assert c.post("/api/v1/benchmark", files=_csv_file(csv_text), headers=HEADERS).status_code == 200
        files = {"file": ("CANARY-NAME.pdf", io.BytesIO(_pdf(["Hb 131.77 g/l CANARYPDF"])), "application/pdf")}
        assert c.post("/api/v1/parse", files=files, headers=HEADERS).status_code == 200
        assert c.post("/ui/parse", json={"text": "Ферритин 7,77 CANARY-TXT"}).status_code == 200
        c.get("/healthz%0a2026-10-04 FAKE GET /admin 200")
    finally:
        logger.removeHandler(capture)
    text = "\n".join(capture.lines)
    assert "POST '/api/v1/benchmark' 200" in text and "POST '/api/v1/parse' 200" in text
    for canary in ("CANARY", "117.31", "131.77", "7,77", "7.77", KEY):
        assert canary not in text, canary
    assert all("\n" not in line for line in capture.lines), "строка журнала не должна содержать перевод строки"
    assert "\\n2026-10-04 FAKE" in text


def test_lis_batch_output_quoted_and_escaped():
    """examples/lis_batch.py: идентификатор из выгрузки ЛИС с формулой не исполнится в Excel."""
    import importlib.util
    import tempfile
    from pathlib import Path

    spec = importlib.util.spec_from_file_location("lis_batch", helpers.ROOT / "examples" / "lis_batch.py")
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
    except ImportError:
        helpers.skip("нет пакета requests")
    rows = []
    for i, local_id in enumerate(["=1+1", " @cmd", "＋x", "\t=1", "ok-1"], start=1):
        row = dict.fromkeys(mod.OUT_COLUMNS, "")
        row.update(row=i, local_id=local_id, status="rejected", error="-ошибка")
        rows.append(row)
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "out.csv"
        mod.write_result_csv(path, rows)
        text = path.read_text(encoding="utf-8")
    parsed = list(csv.reader(io.StringIO(text)))
    assert parsed[0] == mod.OUT_COLUMNS
    assert [r[1] for r in parsed[1:]] == ["'=1+1", "' @cmd", "'＋x", "'\t=1", "ok-1"]
    assert all(r[-1] == "'-ошибка" for r in parsed[1:])
    assert all(line.startswith('"') for line in text.splitlines() if line), "все ячейки в кавычках"


def _values_text(body: dict) -> str:
    return " ".join(f"{k}={v['value']}" for k, v in body["values"].items())


def test_parse_table_one_analysis_and_xlsx_bomb():
    """Таблица одного анализа: «по строке» (коды в заголовке) и «по столбцам» (название — число — единица);
    XLSX больше 5 МБ в распакованном виде — отказ до разбора; текст ячеек (ФИО, телефон) в ответ не попадает."""
    import pandas as pd
    c, _ = _client()
    row = "patient_id;sex;age_years;hemoglobin;MCV;ferritin;CRP\nИванов;F;34;118;76;8,5;н/д\n"
    r = c.post("/api/v1/parse", files={"file": ("one.csv", io.BytesIO(row.encode("cp1251")), "text/csv")},
               headers=HEADERS)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["source"] == {"format": "csv", "layout": "row"}
    assert body["values"] == {"hemoglobin": {"value": 118.0, "unit": None}, "MCV": {"value": 76.0, "unit": None},
                              "ferritin": {"value": 8.5, "unit": None}}
    assert any("значение не число" in u for u in body["unrecognized"])
    assert "Иванов" not in r.text and "34" not in _values_text(body), "ФИО и возраст не извлекаются"
    buf = io.BytesIO()
    pd.DataFrame({"Показатель": ["Пациент Иванов И.И.", "Гемоглобин", "Ферритин", "Телефон"],
                  "Значение": ["", "124", "9,0", "+7 912 345-67-89"], "Единица": ["", "г/л", "мкг/л", ""]}
                 ).to_excel(buf, index=False, engine="openpyxl")
    r = c.post("/ui/parse", files={"file": ("blank.xlsx", io.BytesIO(buf.getvalue()), "application/octet-stream")})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["source"]["layout"] == "lines"
    assert body["values"]["hemoglobin"] == {"value": 124.0, "unit": "г/л"}
    assert body["values"]["ferritin"]["value"] == 9.0
    for leak in ("Иванов", "912", "345"):
        assert leak not in r.text, leak
    bomb = io.BytesIO()
    with zipfile.ZipFile(bomb, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("xl/sharedStrings.xml", b"a" * (6 * 1024 * 1024))
    r = c.post("/api/v1/parse", files={"file": ("b.xlsx", io.BytesIO(bomb.getvalue()), "application/octet-stream")},
               headers=HEADERS)
    assert r.status_code == 422 and r.json()["error"]["code"] == "file_too_large", r.text
    assert "5 МБ" in r.json()["error"]["message"]
