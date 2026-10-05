"""П2: PDF любой лаборатории и сканы. Синтетические бланки tests/fixtures/pdf/*.pdf (собраны из *.html рядом
скриптом scripts/make_pdf_fixtures.py; значения и «ПДн» придуманы): разные порядки столбцов, строки без таблицы,
стрелки и «< 5», две страницы. Проверяются значения, единицы, нормы с бланка, дата, ignored и отсутствие ПДн.
Скан: страница фикстуры -> PNG (pypdfium2) -> PDF без текста -> OCR; тесты с Tesseract — SKIP без него."""
from __future__ import annotations

import io
import json
import logging
import re
from pathlib import Path

import helpers

from deficitlens_api import service
from deficitlens_api.app import create_app
from deficitlens_api.settings import Settings
from deficitlens_core.io import pdf_text, photo_ocr

KEY = "pdf-key-1"
HEADERS = {"X-API-Key": KEY}
FIXTURES = Path(helpers.ROOT) / "tests" / "fixtures" / "pdf"
# «ПДн» из фикстур (придуманные): ни одна строка не должна попасть в ответ.
PII = ("Проверочная", "Анна", "Сергеевна", "07.02.1985", "1985", "7712345678901234", "4455667788", "Тестовый",
       "Пример", "Полис", "заказ", "14.06.2023", "2023-06-14", "7712 3456")

EXPECTED = {
    "a_emias_table.pdf": {
        "values": {"hemoglobin": (108.0, "г/л"), "RBC": (4.12, "10^12/л"), "hematocrit": (33.1, "%"),
                   "MCV": (79.5, "фл"), "MCH": (26.2, "пг"), "MCHC": (329.0, "г/л"), "RDW": (15.1, "%"),
                   "platelets": (251.0, "10^9/л"), "WBC": (6.1, "10^9/л"), "ESR": (14.0, "мм/ч")},
        "ranges": {"hemoglobin": (120.0, 150.0, "г/л"), "RBC": (3.9, 5.2, "×10¹²/л"), "ESR": (2.0, 20.0, "мм/ч"),
                   "platelets": (160.0, 370.0, "×10⁹/л")},
        "ignored": {"Средний объём тромбоцитов (MPV)", "Тромбокрит (PCT)", "Нейтрофилы",
                    "Ширина распределения тромбоцитов (PDW)", "Лимфоциты"},
        "date": "2026-03-12", "pages": 2},
    "b_other_columns.pdf": {
        "values": {"hemoglobin": (11.2, "г/дл"), "RBC": (4.05, "10^12/л"), "hematocrit": (34.0, "%"),
                   "MCV": (77.0, "фл"), "platelets": (230.0, "10^9/л"), "WBC": (5.4, "10^9/л"),
                   "ferritin": (12.0, "нг/мл"), "serum_iron": (45.0, "мкг/дл")},
        "ranges": {"hemoglobin": (117.0, 160.0, "г/л"), "serum_iron": (8.95, 30.43, "мкмоль/л"),
                   "ferritin": (10.0, 120.0, "мкг/л"), "WBC": (4.0, 10.5, "×10⁹/л")},
        "ignored": {"Тромбокрит (PCT)", "Средний объём тромбоцитов (MPV)", "Нейтрофилы"},
        "date": "2026-02-05", "pages": 1},
    "c_lines_abbr.pdf": {
        "values": {"hemoglobin": (112.0, "г/л"), "RBC": (3.95, "10^12/л"), "hematocrit": (35.1, "%"),
                   "MCV": (82.4, "фл"), "MCH": (28.3, "пг"), "platelets": (198.0, "10^9/л"),
                   "WBC": (7.2, "10^9/л"), "ESR": (18.0, "мм/ч")},
        "ranges": {"hemoglobin": (117.0, 160.0, "г/л"), "RBC": (3.8, 5.1, "×10¹²/л"), "MCH": (27.0, 34.0, "пг")},
        "ignored": {"Средний объём тромбоцитов (MPV)", "Тромбокрит (PCT)", "Нейтрофилы"},
        "date": "2026-01-21", "pages": 1},
    "d_lines_flags.pdf": {
        "values": {"hemoglobin": (109.0, "г/л"), "ferritin": (8.5, "мкг/л"), "serum_iron": (7.1, "мкмоль/л"),
                   "TIBC": (78.2, "мкмоль/л"), "TSAT": (9.0, "%"), "vitamin_B12": (180.0, "пг/мл"),
                   "folate": (3.1, "нг/мл"), "CRP": (3.2, "мг/л"), "homocysteine": (14.2, "мкмоль/л"),
                   "TSH": (2.15, "мме/л"), "creatinine": (71.0, "мкмоль/л")},
        "ranges": {"hemoglobin": (120.0, 150.0, "г/л"), "CRP": (None, 5.0, "мг/л"), "folate": (3.0, None, "нг/мл"),
                   "homocysteine": (None, 10.0, "мкмоль/л"), "TSAT": (20.0, 50.0, "%")},
        "ignored": {"Гликированный гемоглобин (HbA1c)", "АЛТ"},
        "date": "2026-03-02", "pages": 1},
    "e_two_pages.pdf": {
        "values": {"hemoglobin": (121.0, "г/л"), "RBC": (4.3, "10^12/л"), "MCV": (88.0, "фл"), "RDW": (12.9, "%"),
                   "platelets": (260.0, "10^9/л"), "WBC": (5.1, "10^9/л"), "ferritin": (25.0, "нг/мл"),
                   "serum_iron": (14.8, "мкмоль/л"), "vitamin_B12": (310.0, "пг/мл"), "CRP": (1.2, "мг/л"),
                   "folate": (15.0, "нмоль/л"), "TIBC": (60.0, "мкмоль/л"), "creatinine": (0.85, "мг/дл")},
        "ranges": {"folate": (3.0891, 19.8585, "нг/мл"), "creatinine": (44.2, 97.24, "мкмоль/л"),
                   "CRP": (0.0, 5.0, "мг/л"), "RDW": (11.5, 14.5, "%")},
        "ignored": {"RDW-SD", "Эозинофилы"},
        "date": "2026-03-09", "pages": 2},
    # Ревизия разбора (H1, H4, M2, СОЭ): «1 210» — одно число; «1.210 Ед/л» у ЛДГ не берётся; «RDW 42,1 фл» — RDW-SD,
    # а RDW-CV 13,2 % не теряется как дубль; СОЭ — по Вестергрену; «Дата р.» раньше даты анализа — не дата анализа.
    "f_thousands_methods.pdf": {
        "values": {"hemoglobin": (121.0, "г/л"), "RDW": (13.2, "%"), "platelets": (1210.0, "тыс/мкл"),
                   "WBC": (6.1, "10^9/л"), "ESR": (25.0, "мм/ч"), "ferritin": (1210.0, "мкг/л"),
                   "vitamin_B12": (1210.0, "пг/мл")},
        "ranges": {"ESR": (0.0, 20.0, "мм/ч"), "platelets": (150.0, 400.0, "×10⁹/л"), "RDW": (11.5, 14.5, "%"),
                   "ferritin": (10.0, 120.0, "мкг/л")},
        "ignored": {"RDW-SD"},
        "date": "2026-02-05", "pages": 1},
    # H3: общий анализ мочи раньше общего анализа крови; «кл/мкл» в разделе крови; «Гемоглобин в моче».
    "g_urine_first.pdf": {
        "values": {"hemoglobin": (98.0, "г/л"), "RBC": (3.9, "10^12/л"), "WBC": (11.5, "10^9/л"),
                   "ferritin": (6.0, "нг/мл")},
        "ranges": {"hemoglobin": (117.0, 160.0, "г/л"), "RBC": (3.8, 5.1, "×10¹²/л")},
        "ignored": {"Гемоглобин (моча)", "Эритроциты (моча)", "Лейкоциты (моча)", "Анализ мочи"},
        "date": "2026-02-05", "pages": 1},
    # H2, H4: «Предыдущий результат» левее «Результат»; таблица динамики с датами; «Дата р. | 14.06.2023» в ячейке.
    "h_previous_dynamics.pdf": {
        "values": {"hemoglobin": (102.0, "г/л"), "ferritin": (8.0, "мкг/л"), "MCV": (74.0, "фл"), "MCH": (24.0, "пг"),
                   "platelets": (255.0, "10^9/л")},
        "ranges": {"hemoglobin": (117.0, 160.0, "г/л"), "MCH": (27.0, 34.0, "пг")},
        "ignored": set(),
        "date": "2026-02-05", "pages": 1},
}


def _fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def _client():
    from starlette.testclient import TestClient
    return TestClient(create_app(Settings(api_keys=(KEY,), ui_rate_per_min=10000)))


def _no_pii(body: dict) -> None:
    text = json.dumps(body, ensure_ascii=False)
    leaked = [p for p in PII if p in text]
    assert not leaked, leaked


def _check(name: str, body: dict) -> None:
    exp = EXPECTED[name]
    got = {k: (v["value"], v["unit"]) for k, v in body["values"].items()}
    assert got == exp["values"], (name, got)
    for code, (lo, hi, unit) in exp["ranges"].items():
        assert body["reference_ranges"][code] == {"low": lo, "high": hi, "unit": unit}, (name, code, body)
    assert set(body["reference_ranges"]) <= set(body["values"])
    assert set(body["ignored"]) == exp["ignored"], (name, body["ignored"])
    assert body["date"] == exp["date"], (name, body["date"])
    assert body["source"] == {"format": "pdf", "pages_total": exp["pages"], "pages_read": exp["pages"]}
    assert all(u.startswith("строка") for u in body["unrecognized"]), body["unrecognized"]
    _no_pii(body)


def test_fixture_sources_are_committed_with_pdfs():
    htmls = sorted(p.stem for p in FIXTURES.glob("*.html"))
    pdfs = sorted(p.stem for p in FIXTURES.glob("*.pdf"))
    assert htmls == pdfs and len(pdfs) == len(EXPECTED) == 8, (htmls, pdfs)


def test_emias_style_table_with_wrapped_names_and_continuation():
    body = service.parse_file(_fixture("a_emias_table.pdf"), "a.pdf")
    _check("a_emias_table.pdf", body)
    # MPV не стал MCV, нейтрофилы — лейкоцитами, тромбокрит — гематокритом.
    assert body["values"]["MCV"]["value"] == 79.5 and body["values"]["WBC"]["value"] == 6.1
    assert body["values"]["hematocrit"]["value"] == 33.1


def test_other_column_order_and_unit_conversion_of_ranges():
    _check("b_other_columns.pdf", service.parse_file(_fixture("b_other_columns.pdf"), "b.pdf"))


def test_text_lines_with_abbreviations():
    _check("c_lines_abbr.pdf", service.parse_file(_fixture("c_lines_abbr.pdf"), "c.pdf"))


def test_text_lines_with_arrows_flags_and_open_ranges():
    _check("d_lines_flags.pdf", service.parse_file(_fixture("d_lines_flags.pdf"), "d.pdf"))


def test_two_pages_cbc_and_biochemistry():
    _check("e_two_pages.pdf", service.parse_file(_fixture("e_two_pages.pdf"), "e.pdf"))


def test_thousands_separator_rdw_sd_esr_method_and_birth_date():
    """Ревизия разбора H1, H4, M2, СОЭ: тромбоциты «1 210 тыс/мкл» — 1210, а не 1 (раньше — срочное направление);
    ЛДГ «1.210» — неоднозначно, значение не берётся; «Дата р.: 14.06.2023» (дата рождения ребёнка, моложе 5 лет)
    не стала датой анализа."""
    body = service.parse_file(_fixture("f_thousands_methods.pdf"), "f.pdf")
    _check("f_thousands_methods.pdf", body)
    assert "LDH" not in body["values"]
    assert body["unrecognized"] == ["строка 14: Лактатдегидрогеназа — проверьте значение"], body["unrecognized"]
    assert any(n.startswith("Лактатдегидрогеназа: число в бланке можно прочитать по-разному") for n in body["notes"])
    assert any(n.startswith("СОЭ: в бланке два метода — взято значение по Вестергрену") for n in body["notes"])
    assert not any("единица не распознана" in n or "несколько раз" in n for n in body["notes"]), body["notes"]


def test_urine_analysis_before_blood_is_not_blood():
    """Ревизия разбора H3: строки ОАМ («Эритроциты 1-2 в п/зр», «Лейкоциты 3 в п/зр») раньше ОАК не занимают
    места показателей крови; значение в «кл/мкл» в разделе крови отклоняется с замечанием."""
    body = service.parse_file(_fixture("g_urine_first.pdf"), "g.pdf")
    _check("g_urine_first.pdf", body)
    assert body["unrecognized"] == ["строка таблицы 9: Эритроциты — единица анализа мочи"], body["unrecognized"]
    assert any("это анализ мочи, а не крови" in n for n in body["notes"]), body["notes"]
    assert not any("несколько раз" in n for n in body["notes"]), body["notes"]


def test_previous_result_column_and_dynamics_table():
    """Ревизия разбора H2, H4: значение — из столбца «Результат», а не «Предыдущий результат» левее него; в таблице
    динамики — из столбца с последней датой (она же дата анализа); дата рождения из ячейки не берётся."""
    body = service.parse_file(_fixture("h_previous_dynamics.pdf"), "h.pdf")
    _check("h_previous_dynamics.pdf", body)
    assert any(n.startswith("В таблице результаты за несколько дат") for n in body["notes"]), body["notes"]
    assert body["unrecognized"] == []


def test_pdf_tables_extracted_in_child_process():
    r = pdf_text.extract_text(_fixture("a_emias_table.pdf"))
    assert r.pages_read == 2 and len(r.tables) == 2 and not r.scanned
    assert r.tables[0][0][:2] == ["Тест", "Результат"]
    assert "Дата: 12.03.2026" in r.text_outside and "Гемоглобин общий" not in r.text_outside
    r = pdf_text.extract_text(_fixture("c_lines_abbr.pdf"))
    assert r.tables == [] and "Гемоглобин (HGB) 112" in r.text
    # Текст над каждой таблицей — для заголовков разделов («Общий анализ мочи»).
    r = pdf_text.extract_text(_fixture("g_urine_first.pdf"))
    assert len(r.table_above) == len(r.tables) == 3
    assert r.table_above[0].splitlines()[-1] == "Общий анализ мочи" and r.table_above[1] == "Общий анализ крови"


def test_api_parse_pdf_new_fields_and_log_has_no_values():
    c = _client()
    records: list[str] = []

    class Grab(logging.Handler):
        def emit(self, record):
            records.append(record.getMessage())

    handler = Grab()
    logging.getLogger().addHandler(handler)
    logging.getLogger("deficitlens").addHandler(handler)
    try:
        r = c.post("/api/v1/parse", files={"file": ("бланк.pdf", io.BytesIO(_fixture("e_two_pages.pdf")),
                                                    "application/pdf")}, headers=HEADERS)
        assert r.status_code == 200, r.text
        _check("e_two_pages.pdf", r.json())
        r = c.post("/ui/parse", json={"text": "Дата взятия: 01.02.2026\nГемоглобин 118 г/л 120-140\nMPV 10,1 фл"})
        body = r.json()
        assert r.status_code == 200 and body["date"] == "2026-02-01" and body["ignored"] == [
            "Средний объём тромбоцитов (MPV)"]
        assert body["reference_ranges"] == {"hemoglobin": {"low": 120.0, "high": 140.0, "unit": "г/л"}}
    finally:
        logging.getLogger().removeHandler(handler)
        logging.getLogger("deficitlens").removeHandler(handler)
    log = re.sub(r"\d+(?:\.\d+)?\s*ms", "", "\n".join(records))
    for leak in ("Проверочная", "4455667788", "121", "14,8", "14.8", "Ферритин", "2026-03-09", "09.03.2026"):
        assert leak not in log, (leak, log)


def test_empty_pdf_is_still_no_text_layer():
    r = _client().post("/api/v1/parse", files={"file": ("x.pdf", io.BytesIO(helpers.pdf_with_text([])),
                                                         "application/pdf")}, headers=HEADERS)
    assert r.status_code == 422 and r.json()["error"]["code"] == "no_text_layer", r.text


# ---- скан без текстового слоя ------------------------------------------------------------------------------
def _scan_pdf(name: str = "c_lines_abbr.pdf") -> bytes:
    """Страницы фикстуры -> PNG (pypdfium2 в дочернем процессе) -> PDF только из картинок (как со сканера)."""
    from PIL import Image
    pages, _ = pdf_text.render_pages(_fixture(name))
    images = [Image.open(io.BytesIO(p)).convert("L") for p in pages]
    buf = io.BytesIO()
    images[0].save(buf, "PDF", resolution=float(pdf_text.SCAN_DPI), save_all=True, append_images=images[1:])
    return buf.getvalue()


def test_render_pages_limits():
    from PIL import Image
    pages, total = pdf_text.render_pages(_fixture("e_two_pages.pdf"))
    assert total == 2 and len(pages) == 2
    w, h = Image.open(io.BytesIO(pages[0])).size
    assert max(w, h) <= pdf_text.SCAN_MAX_SIDE and min(w, h) > 1000          # ~200 dpi для A4
    pages, _ = pdf_text.render_pages(_fixture("e_two_pages.pdf"), max_pages=1)
    assert len(pages) == 1
    scan = pdf_text.extract_text(_scan_pdf())
    assert scan.scanned and scan.text == "" and scan.images >= 1


def test_scan_without_tesseract_is_ocr_unavailable():
    saved = photo_ocr.TESSERACT_CMD
    photo_ocr.TESSERACT_CMD = "tesseract-not-installed-xyz"
    try:
        r = _client().post("/ui/parse", files={"file": ("scan.pdf", io.BytesIO(_scan_pdf()), "application/pdf")})
    finally:
        photo_ocr.TESSERACT_CMD = saved
    assert r.status_code == 422 and r.json()["error"]["code"] == "ocr_unavailable", r.text
    msg = r.json()["error"]["message"]
    assert "скан" in msg.lower() and "Docker" in msg


def test_scan_flow_with_stub_ocr_marks_and_no_pii():
    """Путь скана без Tesseract: распознавание подменено, проверяются склейка страниц, пометки и поля ответа."""
    pages_text = [("ФИО: Проверочная Анна Сергеевна\nДата взятия: 21.01.2026\nГемоглобин (HGB) 112 г/л 117 - 160\n"
                   "Средний объем тромбоцитов (MPV) 10,9 фл"),
                  "Ферритин 9 мкг/л 10 - 120\nСОЭ 18 мм/ч 2 - 20"]
    calls = []

    def fake(data, max_seconds=None):
        calls.append(max_seconds)
        k = len(calls) - 1
        return photo_ocr.OcrText(text=pages_text[k], low_confidence_lines={1} if k == 1 else set())

    saved = (photo_ocr.extract_text, photo_ocr.tesseract_available)
    photo_ocr.extract_text, photo_ocr.tesseract_available = fake, lambda: True
    try:
        body = service.parse_file(_scan_pdf("e_two_pages.pdf"), "scan.pdf")
    finally:
        photo_ocr.extract_text, photo_ocr.tesseract_available = saved
    assert len(calls) == 2 and all(0 < s <= photo_ocr.MAX_SECONDS for s in calls)
    assert body["source"] == {"format": "pdf_scan", "pages_total": 2, "pages_read": 2}
    assert {k: v["value"] for k, v in body["values"].items()} == {"hemoglobin": 112.0, "ferritin": 9.0, "ESR": 18.0}
    assert body["check"] == ["ferritin"] and body["warnings"] == ["строка 5: Ферритин — проверьте значение"]
    assert body["notes"][0] == service.SCAN_NOTE and body["date"] == "2026-01-21"
    assert body["ignored"] == ["Средний объём тромбоцитов (MPV)"]
    assert body["reference_ranges"]["hemoglobin"] == {"low": 117.0, "high": 160.0, "unit": "г/л"}
    assert "lines" not in body and "text" not in body
    _no_pii(body)


def test_scan_recognized_with_tesseract():
    helpers.tesseract_or_skip()
    r = _client().post("/api/v1/parse", files={"file": ("scan.pdf", io.BytesIO(_scan_pdf()), "application/pdf")},
                       headers=HEADERS)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["source"]["format"] == "pdf_scan" and body["source"]["pages_read"] == 1
    exp = EXPECTED["c_lines_abbr.pdf"]["values"]
    got = {k: v["value"] for k, v in body["values"].items()}
    right = [k for k, (v, _) in exp.items() if got.get(k) == v]
    assert len(right) >= 6, (got, right)
    assert "MCV" not in got or got["MCV"] == 82.4, "MPV не должен стать MCV"
    assert isinstance(body["warnings"], list) and set(body["check"]) <= set(body["values"])
    _no_pii(body)
