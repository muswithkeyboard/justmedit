"""Этап 7: загрузка фото бланка в /api/v1/parse и /ui/parse — тот же ответ, что у текста и PDF, без ПДн и без
распознанного текста; неуверенные строки — с пометкой «проверьте значение»; ошибки — 422 с понятным кодом;
в журнале нет ни значений, ни ПДн. Тесты, которым нужен Tesseract, — SKIP без него. Данные придуманы."""
from __future__ import annotations

import io
import json
import logging
import re

import helpers

from deficitlens_api import service
from deficitlens_api.app import create_app
from deficitlens_api.settings import Settings
from deficitlens_core.io import photo_ocr

KEY = "photo-key-1"
HEADERS = {"X-API-Key": KEY}


def _client():
    from starlette.testclient import TestClient
    return TestClient(create_app(Settings(api_keys=(KEY,), ui_rate_per_min=10000)))


def _files(data: bytes, name: str = "IMG_0001.jpg", ctype: str = "image/jpeg"):
    return {"file": (name, io.BytesIO(data), ctype)}


def _no_pii(body: dict) -> None:
    text = json.dumps(body, ensure_ascii=False)
    leaked = [p for p in helpers.PHOTO_PII if p in text]
    assert not leaked, leaked
    assert "CANARY-CAMERA" not in text and "лаборатория" not in text.lower()


def test_parse_photo_errors_are_422_with_codes():
    c = _client()
    from PIL import Image
    bomb = io.BytesIO()
    Image.new("L", (6000, 6000), 255).save(bomb, "PNG")
    cases = ((_files(b"this is not a picture at all"), "bad_image"),
             (_files(b"\0\0\0\x18ftypheic" + b"\0" * 64, "IMG_0002.HEIC", "image/heic"), "heic_unsupported"),
             (_files(bomb.getvalue(), "bomb.png", "image/png"), "image_too_large"))
    for files, code in cases:
        r = c.post("/api/v1/parse", files=files, headers=HEADERS)
        assert r.status_code == 422 and r.json()["error"]["code"] == code, r.text
        assert r.json()["error"]["field"] == "file"
    # Камера телефона без расширения: фото узнаётся по сигнатуре; без tesseract — ocr_unavailable.
    saved = photo_ocr.TESSERACT_CMD
    photo_ocr.TESSERACT_CMD = "tesseract-not-installed-xyz"
    try:
        r = c.post("/ui/parse", files=_files(helpers.photo_blank(), "blob", "application/octet-stream"))
        assert r.status_code == 422 and r.json()["error"]["code"] == "ocr_unavailable", r.text
        assert "Docker" in r.json()["error"]["message"]
    finally:
        photo_ocr.TESSERACT_CMD = saved


def test_low_confidence_line_gets_check_mark():
    """Сервис: значение из неуверенно распознанной строки -> warnings и check; распознанный текст не возвращается."""
    fake = photo_ocr.OcrText(text="ФИО Иванова Мария\nГемоглобин 118 г/л\nФерритин 9 мкг/л\nТелефон +7 912 345-67-89",
                             low_confidence_lines={3})
    saved = photo_ocr.extract_text
    photo_ocr.extract_text = lambda data: fake
    try:
        body = service.parse_file(b"\xff\xd8\xff\xe0 fake", "IMG.jpg")
    finally:
        photo_ocr.extract_text = saved
    assert body["values"] == {"hemoglobin": {"value": 118.0, "unit": "г/л"},
                              "ferritin": {"value": 9.0, "unit": "мкг/л"}}
    assert body["check"] == ["ferritin"]
    assert body["warnings"] == ["строка 3: Ферритин — проверьте значение"]
    assert body["source"] == {"format": "photo"}
    assert body["notes"][0] == service.PHOTO_NOTE
    assert "lines" not in body and "text" not in body
    assert all(u.startswith("строка ") for u in body["unrecognized"])
    _no_pii(body)


def test_parse_photo_end_to_end_without_pii_and_log_clean():
    helpers.tesseract_or_skip()
    c = _client()
    records: list[str] = []

    class Grab(logging.Handler):
        def emit(self, record):
            records.append(record.getMessage())

    handler = Grab()
    root = logging.getLogger()
    root.addHandler(handler)
    app_log = logging.getLogger("deficitlens")
    app_log.addHandler(handler)
    try:
        r = c.post("/api/v1/parse", files=_files(helpers.photo_blank(exif_rotated=True, noise=True)), headers=HEADERS)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["source"] == {"format": "photo"}
        got = {k: v["value"] for k, v in body["values"].items()}
        right = [k for k, v in helpers.PHOTO_VALUES.items() if got.get(k) == v]
        assert len(right) >= 6, got
        assert isinstance(body["warnings"], list) and isinstance(body["check"], list)
        assert set(body["check"]) <= set(body["values"])
        _no_pii(body)
        r = c.post("/ui/parse", files=_files(helpers.photo_blank(fmt="PNG"), "scan.png", "image/png"))
        assert r.status_code == 200 and len(r.json()["values"]) >= 7, r.text
    finally:
        root.removeHandler(handler)
        app_log.removeHandler(handler)
    log = "\n".join(records)
    # время ответа («1181.3ms») может случайно содержать «118» — из проверки его убираем
    log_wo_timing = re.sub(r"\d+(?:\.\d+)?\s*ms", "", log)
    assert not [p for p in ("Иванова", "118", "4,1", "Гемоглобин", "CANARY") if p in log_wo_timing], log
