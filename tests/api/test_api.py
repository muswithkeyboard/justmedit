"""Тесты веб-слоя (deficitlens_api): ключ API, коды ответов, формат ошибок, лимит /ui/*, заголовки безопасности,
журнал без значений анализов, страницы без встроенных скриптов, время ответа.

Клиент: starlette.testclient.TestClient; если он недоступен (нет httpx2) — uvicorn в потоке и requests.
Все значения анализов придуманы (data/demo/examples.json).
"""
from __future__ import annotations

import io
import logging
import re
import socket
import threading
import time

import helpers
from helpers import load_examples

from deficitlens_api.app import create_app
from deficitlens_api.settings import Settings

KEY = "test-key-1"
HEADERS = {"X-API-Key": KEY}
_CLIENTS: dict = {}


class _LiveClient:
    """Запасной клиент: сервис на свободном порту в потоке, запросы через requests."""

    def __init__(self, app):
        import requests
        import uvicorn
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        self.base = f"http://127.0.0.1:{port}"
        self.session = requests.Session()
        server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
        threading.Thread(target=server.run, daemon=True).start()
        for _ in range(150):
            try:
                self.session.get(self.base + "/healthz", timeout=1)
                break
            except Exception:  # noqa: BLE001
                time.sleep(0.1)

    def get(self, url, **kw):
        return self.session.get(self.base + url, **kw)

    def post(self, url, **kw):
        return self.session.post(self.base + url, **kw)


def _client(**settings):
    """Клиент к приложению с заданными настройками (по одному приложению на набор настроек)."""
    key = tuple(sorted(settings.items()))
    if key not in _CLIENTS:
        app = create_app(Settings(api_keys=(KEY, "test-key-2"), **{"ui_rate_per_min": 10000, **settings}))
        try:
            from starlette.testclient import TestClient
            _CLIENTS[key] = TestClient(app)
        except Exception:  # noqa: BLE001 — нет httpx2: поднимаем настоящий сервер
            _CLIENTS[key] = _LiveClient(app)
    return _CLIENTS[key]


def _example(i: int = 0) -> dict:
    return load_examples()[i]["input"]


# --------------------------------------------------------------------------------------------
def test_healthz():
    r = _client().get("/healthz")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert set(body) == {"status", "engine", "case_model", "screen_model", "norms"}


def test_analyze_requires_key():
    c = _client()
    r = c.post("/api/v1/analyze", json=_example())
    assert r.status_code == 401 and r.json()["error"]["code"] == "unauthorized"
    r = c.post("/api/v1/analyze", json=_example(), headers={"X-API-Key": "wrong"})
    assert r.status_code == 403 and r.json()["error"]["code"] == "forbidden"
    for path in ("/api/v1/batch", "/api/v1/parse", "/api/v1/benchmark"):
        assert c.post(path, json={}).status_code == 401, path
    assert c.get("/api/v1/reference").status_code == 401


def test_analyze_ok_fields():
    r = _client().post("/api/v1/analyze", json=_example(), headers=HEADERS)
    assert r.status_code == 200
    body = r.json()
    for field in ("version", "level1", "level2", "hidden_deficiency", "flags", "checklist", "next_tests",
                  "explanation", "values", "reports", "normalization", "limitations", "processing_ms"):
        assert field in body, field
    assert body["level1"]["anemia"] is False
    assert body["reports"]["doctor"]["headline"] and body["reports"]["patient"]["headline"]
    assert r.headers["cache-control"] == "no-store"
    # второй ключ из списка тоже подходит
    assert _client().post("/api/v1/analyze", json=_example(), headers={"X-API-Key": "test-key-2"}).status_code == 200


def test_analyze_422_with_field():
    c = _client()
    bad = {**_example(), "values": {**_example()["values"], "hemoglobin": 500}}
    r = c.post("/api/v1/analyze", json=bad, headers=HEADERS)
    assert r.status_code == 422
    err = r.json()["error"]
    assert err["field"] == "hemoglobin" and err["code"] and err["message"]
    r = c.post("/api/v1/analyze", json={**_example(), "age_years": 10}, headers=HEADERS)
    assert r.status_code == 422 and r.json()["error"]["field"] == "age_years"
    r = c.post("/api/v1/analyze", content=b"{not json", headers={**HEADERS, "Content-Type": "application/json"}) \
        if hasattr(c, "app") else c.post("/api/v1/analyze", data=b"{not json", headers=HEADERS)
    assert r.status_code == 422 and r.json()["error"]["code"] == "invalid_json"


def test_batch_done_and_rejected():
    records = [{**_example(0), "patient_ref": "row-1"}, {**_example(1), "values": {"hemoglobin": 500}}]
    r = _client().post("/api/v1/batch", json={"records": records, "with_reports": False}, headers=HEADERS)
    assert r.status_code == 200
    body = r.json()
    assert body["summary"]["total"] == 2 and body["summary"]["done"] == 1 and body["summary"]["rejected"] == 1
    assert [it["status"] for it in body["items"]] == ["done", "rejected"]
    assert body["items"][0]["patient_ref"] == "row-1" and "reports" not in body["items"][0]["result"]
    assert body["items"][1]["errors"][0]["field"] == "hemoglobin"
    r = _client().post("/api/v1/batch", json={"records": records[:1], "with_reports": True}, headers=HEADERS)
    assert "reports" in r.json()["items"][0]["result"]


def test_batch_limit():
    c = _client(max_batch=2)
    r = c.post("/api/v1/batch", json={"records": [_example()] * 3}, headers=HEADERS)
    assert r.status_code == 413 and r.json()["error"]["code"] == "batch_too_large"


def _examples_csv() -> bytes:
    """Маленький CSV в схеме кейса, собранный из демонстрационных примеров."""
    examples = load_examples()
    codes = sorted({code for ex in examples for code in ex["input"]["values"]})
    lines = [",".join(["patient_id", "sex", "age_years", *codes])]
    for ex in examples:
        inp = ex["input"]
        lines.append(",".join([ex["id"], inp["sex"], str(inp["age_years"]),
                               *[str(inp["values"].get(code, "")) for code in codes]]))
    return ("\n".join(lines) + "\n").encode("utf-8")


def test_benchmark_small_csv_auto_is_clinical():
    c = _client()
    files = {"file": ("examples.csv", io.BytesIO(_examples_csv()), "text/csv")}
    r = c.post("/api/v1/benchmark", files=files, data={"mode": "auto"}, headers=HEADERS)
    assert r.status_code == 200, r.text
    body = r.json()
    s = body["summary"]
    assert s["mode"] == "clinical" and s["mode_requested"] == "auto"
    assert s["rows_total"] == 8 and s["rows_done"] == 8 and s["rows_rejected"] == 0
    assert len(body["preview"]) == 8 and body["preview"][0]["status"] == "done"
    assert body["csv"].splitlines()[0].startswith('"patient_id","anemia","case_group"')   # все ячейки в кавычках
    assert "cv_metrics" not in body, "файл не обучающий: блока кросс-валидации быть не должно"
    files = {"file": ("examples.csv", io.BytesIO(_examples_csv()), "text/csv")}
    r = c.post("/api/v1/benchmark?format=csv", files=files, data={"mode": "clinical"}, headers=HEADERS)
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
    files = {"file": ("examples.pdf", io.BytesIO(b"%PDF"), "application/pdf")}
    assert c.post("/api/v1/benchmark", files=files, headers=HEADERS).status_code == 422


def test_benchmark_training_file_gets_cv_metrics():
    path = helpers.case_data_path()                    # нет файла кейса — SKIP
    with open(path, "rb") as f:
        r = _client().post("/ui/benchmark", files={"file": ("deficiency_anemia.csv", f, "text/csv")},
                           data={"mode": "auto"})
    assert r.status_code == 200
    body = r.json()
    if not body["summary"]["metrics"].get("same_as_training_data"):
        helpers.skip("файл кейса отличается от набора, на котором обучена модель")
    cv = body["cv_metrics"]
    assert set(cv["modes"]) == {"benchmark", "clinical"}
    for mode in cv["modes"].values():
        assert 0 < mode["classes12"]["accuracy"] < 1 and 0 < mode["groups5"]["accuracy"] < 1


def test_upload_too_large_is_413():
    c = _client(max_upload_mb=1)
    files = {"file": ("big.csv", io.BytesIO(b"a,b\n" + b"1,2\n" * 400000), "text/csv")}
    assert c.post("/ui/benchmark", files=files, data={"mode": "auto"}).status_code == 413
    r = c.post("/api/v1/analyze", json={**_example(), "patient_ref": "x" * 400000}, headers=HEADERS)
    assert r.status_code == 413


def test_ui_works_without_key():
    c = _client()
    r = c.post("/ui/analyze", json=_example(4))
    assert r.status_code == 200 and r.json()["level1"]["anemia"] is True
    ref = c.get("/ui/reference").json()
    assert {"analytes", "form_groups", "thresholds", "prices", "versions", "policy"} <= set(ref)
    assert all({"code", "name_ru", "short_ru", "unit_ru", "group"} <= set(a) for a in ref["analytes"])
    assert all(t["sources"] and "status" in t["sources"][0] for t in ref["thresholds"])
    ex = c.get("/ui/examples").json()["examples"]
    assert len(ex) == 8 and all("expect" not in e for e in ex) and "followup" in ex[0]
    r = c.post("/ui/parse", json={"text": "Гемоглобин 124 г/л\nФерритин: 9,0 мкг/л (норма 10–120)"})
    assert r.status_code == 200 and r.json()["values"]["ferritin"]["value"] == 9.0


def test_ui_rate_limit_429():
    c = _client(ui_rate_per_min=3)
    codes = [c.post("/ui/analyze", json=_example()).status_code for _ in range(5)]
    assert codes[:3] == [200, 200, 200] and codes[3:] == [429, 429], codes
    r = c.post("/ui/analyze", json=_example())
    assert r.status_code == 429 and int(r.headers["retry-after"]) >= 1
    assert r.json()["error"]["code"] == "rate_limited"
    assert c.post("/api/v1/analyze", json=_example(), headers=HEADERS).status_code == 200, "лимит только для /ui/*"


def test_security_headers():
    c = _client()
    for r in (c.get("/"), c.get("/healthz"), c.get("/static/app.js"), c.post("/api/v1/analyze", json={}),
              c.get("/no-such-page")):
        h = r.headers
        assert h["content-security-policy"] == ("default-src 'self'; img-src 'self' data: https://tile.openstreetmap.org; "
                                                "frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        assert h["permissions-policy"] == "camera=(), microphone=(), geolocation=(self)"   # только своя страница
        assert h["x-content-type-options"] == "nosniff"
        assert h["referrer-policy"] == "no-referrer"
    assert c.get("/no-such-page").status_code == 404


class _Capture(logging.Handler):
    def __init__(self):
        super().__init__()
        self.lines: list[str] = []

    def emit(self, record):
        self.lines.append(record.getMessage())


def test_log_has_no_values():
    """«Канарейка»: приметные значение и метка не должны появиться в журнале; строка доступа — должна."""
    c = _client()
    capture = _Capture()
    logger = logging.getLogger("deficitlens")
    logger.addHandler(capture)
    try:
        payload = {**_example(), "values": {**_example()["values"], "hemoglobin": 123.456}, "patient_ref": "CANARY-7"}
        assert c.post("/api/v1/analyze?debug=CANARY-7", json=payload, headers=HEADERS).status_code == 200
        assert c.post("/api/v1/analyze", json={**payload, "values": {"hemoglobin": 987.654}}, headers=HEADERS).status_code == 422
        assert c.post("/ui/parse", json={"text": "Гемоглобин 123,456 г/л CANARY-7"}).status_code == 200
    finally:
        logger.removeHandler(capture)
    text = "\n".join(capture.lines)
    assert "POST '/api/v1/analyze' 200" in text and "POST '/api/v1/analyze' 422" in text
    for canary in ("123.456", "123,456", "987.654", "CANARY-7", KEY):
        assert canary not in text, canary


def test_pages_have_no_inline_code():
    c = _client()
    for path in ("/", "/benchmark", "/developers", "/login", "/cabinet", "/share"):
        r = c.get(path)
        assert r.status_code == 200 and 'lang="ru"' in r.text
        html = r.text
        assert not re.search(r"<script(?![^>]*\bsrc=)[^>]*>", html), "встроенный <script> без src"
        assert not re.search(r"<script[^>]*>\s*\S[^<]*</script>", html), "код внутри <script>"
        assert not re.search(r"<[^>]+\son[a-z]+\s*=", html, flags=re.IGNORECASE), "атрибут-обработчик on*"
        assert "<style" not in html and not re.search(r"<[^>]+\sstyle\s*=", html), "встроенные стили"
        # внешние ресурсы (то, что браузер загружает сам): src=… и <link href=…>; обычная ссылка <a href> — не ресурс
        assert not re.search(r"\bsrc=\"https?://", html), "внешние ресурсы"
        assert not re.search(r"<link[^>]+href=\"https?://", html), "внешние стили"
        for a in re.findall(r"<a\b[^>]*href=\"https?://[^>]*>", html):          # внешние ссылки — в новой вкладке без Referer
            assert 'rel="noopener noreferrer"' in a, a
    js = (c.get("/static/app.js").text + c.get("/static/benchmark.js").text + c.get("/static/export.js").text
          + c.get("/static/developers.js").text + c.get("/static/cabinet.js").text + c.get("/static/nav.js").text
          + c.get("/static/home-cabinet.js").text)
    assert "localStorage" not in js.replace("ни localStorage", "") and "document.cookie" not in js
    assert "sessionStorage" not in js.replace("ни sessionStorage", "")
    assert "innerHTML" not in js


def test_response_time_p95():
    """p95 времени ответа POST /api/v1/analyze на 50 запросах (восемь примеров по кругу) — меньше 150 мс."""
    c = _client()
    inputs = [ex["input"] for ex in load_examples()]
    c.post("/api/v1/analyze", json=inputs[0], headers=HEADERS)           # прогрев
    times = []
    for i in range(50):
        t0 = time.perf_counter()
        r = c.post("/api/v1/analyze", json=inputs[i % len(inputs)], headers=HEADERS)
        times.append(1000 * (time.perf_counter() - t0))
        assert r.status_code == 200
    times.sort()
    p50, p95 = times[len(times) // 2], times[int(len(times) * 0.95) - 1]
    print(f"      /api/v1/analyze: p50 {p50:.1f} мс, p95 {p95:.1f} мс, max {times[-1]:.1f} мс (50 запросов)")
    assert p95 < 150, f"p95 = {p95:.1f} мс"


def test_ui_reference_has_default_reference_ranges_by_sex():
    """Форма «Референсы лаборатории»: справочник отдаёт референсы проекта по умолчанию по полу."""
    r = _client().get("/ui/reference")
    rr = r.json()["reference_ranges"]
    assert set(rr["by_sex"]) == {"F", "M"} and rr["label"]
    assert rr["by_sex"]["F"]["haptoglobin"] == [0.83, 2.67] and rr["by_sex"]["M"]["LDH"] == [135, 225]
    assert rr["by_sex"]["F"]["LDH"] == [135, 214]


def test_ui_analyze_with_lab_reference_ranges_gives_warning():
    """Как отправляет страница: reference_ranges с единицей формы → предупреждение о референсах лаборатории."""
    body = dict(_example())
    body["reference_ranges"] = {"haptoglobin": {"low": 0.4, "high": 1.8, "unit": "g/L"}, "LDH": {"high": 240, "unit": "U/L"}}
    r = _client().post("/ui/analyze", json=body)
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["references"]["warning"] and set(d["references"]["local_analytes"]) == {"haptoglobin", "LDH"}
    bad = dict(body, reference_ranges={"haptoglobin": {"low": 2, "high": 1, "unit": "g/L"}})
    r = _client().post("/ui/analyze", json=bad)
    assert r.status_code == 422 and r.json()["error"]["field"].startswith("reference_ranges")
