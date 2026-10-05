"""Тесты мини-сайта клиники (examples/clinic_site/app.py): ключ API только на сервере клиники, странице — только
отчёт пациенту, строгая CSP без inline-скриптов, разбор формы, ошибки DeficitLens → понятные пациенту ответы.

Оба сервиса (DeficitLens и сайт клиники) поднимаются настоящим uvicorn на свободных портах в потоках,
запросы — urllib из стандартной библиотеки (httpx и requests не нужны). Значения анализов придуманы.
"""
from __future__ import annotations

import importlib.util
import json
import re
import socket
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import helpers

ROOT = Path(__file__).resolve().parents[2]
SITE = ROOT / "examples" / "clinic_site"
KEY = "clinic-test-key-5f2c"
FORM = {"sex": "F", "age_years": 34, "pregnant": False, "hemoglobin": 104, "RBC": 4.21, "hematocrit": 33,
        "MCV": 78, "MCH": 24.7, "MCHC": 315, "RDW": 16.1, "platelets": 310, "WBC": 6.1}
_STATE: dict = {}


def _site_module():
    if "mod" not in _STATE:
        if not (SITE / "app.py").is_file():
            helpers.skip("нет examples/clinic_site/app.py")
        spec = importlib.util.spec_from_file_location("clinic_site_app", SITE / "app.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _STATE["mod"] = mod
    return _STATE["mod"]


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _serve(app) -> str:
    """Запускает ASGI-приложение uvicorn'ом в фоновом потоке, ждёт /healthz, возвращает базовый адрес."""
    import uvicorn
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", access_log=False))
    threading.Thread(target=server.run, daemon=True).start()
    base = f"http://127.0.0.1:{port}"
    for _ in range(300):
        try:
            urllib.request.urlopen(base + "/healthz", timeout=1)
            return base
        except Exception:  # noqa: BLE001 — сервер ещё стартует
            time.sleep(0.1)
    raise AssertionError("сервер не поднялся за 30 с")


def _api() -> str:
    """Настоящий DeficitLens с тестовым ключом."""
    if "api" not in _STATE:
        from deficitlens_api.app import create_app
        from deficitlens_api.settings import Settings
        _STATE["api"] = _serve(create_app(Settings(api_keys=(KEY,), ui_rate_per_min=10000)))
    return _STATE["api"]


def _site(name: str = "ok", base_url: str | None = None, api_key: str = KEY) -> str:
    if name not in _STATE:
        _STATE[name] = _serve(_site_module().create_app(base_url=base_url or _api(), api_key=api_key))
    return _STATE[name]


def _req(method: str, url: str, body=None, raw: bytes | None = None, ctype: str = "application/json"):
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    req = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", ctype)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, r.read(), {k.lower(): v for k, v in r.headers.items()}
    except urllib.error.HTTPError as e:
        return e.code, e.read(), {k.lower(): v for k, v in e.headers.items()}


# --------------------------------------------------------------------------------------------
# Разбор формы (без сети)
# --------------------------------------------------------------------------------------------
def test_build_payload_maps_form_to_analyze_input():
    m = _site_module()
    p = m.build_payload({**FORM, "RBC": "4,21", "ferritin": "", "pregnant": True})
    assert p["sex"] == "F" and p["age_years"] == 34 and isinstance(p["age_years"], int)
    assert p["values"]["RBC"] == 4.21 and "ferritin" not in p["values"]
    assert p["pregnancy"] == {"status": "yes"}
    assert p["provenance"]["client"] == "clinic_site"
    # беременность у мужчины не передаётся
    assert m.build_payload({**FORM, "sex": "M", "pregnant": True})["pregnancy"] == {"status": "no"}
    # схема входа DeficitLens принимает собранное тело
    from deficitlens_core.schemas import AnalysisInput
    AnalysisInput.model_validate(p)


def test_build_payload_rejects_bad_form():
    m = _site_module()
    bad = [
        {**FORM, "sex": "X"},
        {**FORM, "age_years": 12},
        {**FORM, "age_years": 34.5},
        {k: v for k, v in FORM.items() if k != "hemoglobin"},
        {**FORM, "hemoglobin": "abc"},
        {**FORM, "hemoglobin": -5},
        {**FORM, "hemoglobin": float("nan")},
        {**FORM, "MCV": True},
        {**FORM, "api_key": "x"},            # лишние поля не принимаются
        [FORM],
    ]
    for form in bad:
        try:
            m.build_payload(form)
        except m.FormError:
            continue
        raise AssertionError(f"форма принята: {form}")


# --------------------------------------------------------------------------------------------
# Страница: CSP, без inline-скриптов, без ключа
# --------------------------------------------------------------------------------------------
def test_page_has_strict_csp_and_no_inline_script_or_key():
    base = _site()
    s, raw, h = _req("GET", base + "/")
    html = raw.decode("utf-8")
    assert s == 200 and h["content-type"].startswith("text/html")
    csp = h["content-security-policy"]
    assert "script-src 'self'" in csp and "default-src 'none'" in csp and "connect-src 'self'" in csp
    assert "unsafe-inline" not in csp
    assert h["x-content-type-options"] == "nosniff" and h["referrer-policy"] == "no-referrer"
    # только внешние скрипты своего origin, без обработчиков в атрибутах
    for tag in re.findall(r"<script\b[^>]*>", html, flags=re.I):
        assert 'src="/static/' in tag
    assert not re.search(r"<script\b[^>]*>\s*[^<\s]", html, flags=re.I)      # тело <script> пустое
    assert not re.search(r"\son[a-z]+\s*=", html, flags=re.I)
    assert "<style" not in html.lower() and "style=" not in html.lower()
    for path in ("/", "/static/clinic.js", "/static/clinic.css"):
        s, raw, h = _req("GET", base + path)
        assert s == 200 and KEY not in raw.decode("utf-8") and "x-api-key" not in raw.decode("utf-8").lower()
        assert "content-security-policy" in h


# --------------------------------------------------------------------------------------------
# Отчёт: путь через настоящий DeficitLens
# --------------------------------------------------------------------------------------------
def test_report_returns_only_patient_report():
    s, raw, h = _req("POST", _site() + "/api/report", FORM)
    body = json.loads(raw)
    assert s == 200, body
    assert set(body) == {"patient"}
    p = body["patient"]
    assert p["headline"] and p["sections"] and all("title" in x and "lines" in x for x in p["sections"])
    text = raw.decode("utf-8")
    assert KEY not in text
    for leak in ('"doctor"', '"level2"', '"next_tests"', '"hidden_deficiency"'):
        assert leak not in text
    assert "content-security-policy" in h and h.get("cache-control") == "no-store"


def test_report_form_errors_are_422_413_415():
    url = _site() + "/api/report"
    s, raw, _ = _req("POST", url, {**FORM, "age_years": 12})
    assert s == 422 and "18" in json.loads(raw)["error"]
    s, raw, _ = _req("POST", url, raw=b"{not json")
    assert s == 422 and json.loads(raw)["error"]
    s, _, _ = _req("POST", url, raw=b"x" * (64 * 1024))
    assert s == 413
    s, _, _ = _req("POST", url, raw=b"sex=F", ctype="application/x-www-form-urlencoded")
    assert s == 415


def test_report_upstream_errors_are_502_without_details():
    # ключ клиники не принят DeficitLens (403) — пациенту без подробностей
    s, raw, _ = _req("POST", _site("wrong-key", api_key="not-" + KEY) + "/api/report", FORM)
    assert s == 502 and "ключ" not in json.loads(raw)["error"].lower()
    # DeficitLens недоступен
    dead = f"http://127.0.0.1:{_free_port()}"
    s, raw, _ = _req("POST", _site("down", base_url=dead) + "/api/report", FORM)
    assert s == 502 and json.loads(raw)["error"]


def test_report_passes_deficitlens_422_message():
    # гемоглобин в допустимом для формы диапазоне, но вне правдоподобного для DeficitLens → его сообщение
    s, raw, _ = _req("POST", _site() + "/api/report", {**FORM, "hemoglobin": 999})
    body = json.loads(raw)
    assert s in (200, 422), body
    if s == 422:
        assert body["error"] and KEY not in raw.decode("utf-8")
