"""Надёжность и скорость веб-слоя (ревизия производительности): лимит только на расчёты страницы, кеш и ETag
справочника, один тяжёлый слот на адрес, разбор текста без слота, сжатие без персональных данных, кеш статики,
страница 404, кабинет без входа, HSTS, значок, внешняя обёртка ошибок, ограничение потоков analyze, пределы PDF,
настройка регистраций. Все значения придуманы."""
from __future__ import annotations

import io
import logging
import re
import tempfile
import threading
import time
from pathlib import Path

import helpers

from deficitlens_api.app import create_app
from deficitlens_api.settings import Settings

KEY = "reliability-key-1"
HEADERS = {"X-API-Key": KEY}
BROWSER = {"Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"}
INPUT = {"sex": "F", "age_years": 34, "values": {"hemoglobin": 124, "MCV": 82, "RDW": 15.2}}


def _app(**settings):
    data_dir = Path(tempfile.mkdtemp(prefix="jm-rel-")) / "db"
    return create_app(Settings(api_keys=(KEY,), data_dir=str(data_dir),
                               **{"ui_rate_per_min": 10000, "auth_rate_per_min": 1000, "auth_ip_rate_per_min": 1000,
                                  "cabinet_rate_per_min": 100000, **settings}))


def _client(app, **kw):
    try:
        from starlette.testclient import TestClient
    except Exception:  # noqa: BLE001 — нет httpx2
        helpers.skip("нет httpx2: веб-слой проверяется через starlette.testclient (uv sync --extra dev)")
    return TestClient(app, **kw)


class _Capture(logging.Handler):
    def __init__(self):
        super().__init__()
        self.lines: list[str] = []

    def emit(self, record):
        self.lines.append(record.getMessage())


# ---- 1. Справочник страницы: без лимита, кеш 5 мин, ETag; лимит — только на POST /ui/* ----------------
def test_ui_reference_and_examples_not_rate_limited():
    c = _client(_app(ui_rate_per_min=2))
    codes = [c.get(p).status_code for _ in range(10) for p in ("/ui/reference", "/ui/examples")]
    assert codes == [200] * 20, codes
    codes = [c.post("/ui/analyze", json=INPUT).status_code for _ in range(3)]
    assert codes == [200, 200, 429], codes
    assert c.get("/ui/reference").status_code == 200, "упёрся в лимит расчётов — справочник всё равно отдаётся"


def test_ui_reference_cache_control_and_etag():
    c = _client(_app())
    for path in ("/ui/reference", "/ui/examples"):
        r = c.get(path)
        assert r.status_code == 200 and r.headers["cache-control"] == "public, max-age=300", r.headers
        etag = r.headers["etag"]
        assert re.fullmatch(r'"[0-9a-f]{20}"', etag), etag
        assert r.json(), path
        assert c.get(path).headers["etag"] == etag, "метка не меняется между запросами"
        for inm in (etag, "W/" + etag, '"other", ' + etag, "*"):
            r2 = c.get(path, headers={"If-None-Match": inm})
            assert r2.status_code == 304 and r2.content == b"", (path, inm)
            assert r2.headers["etag"] == etag and r2.headers["cache-control"] == "public, max-age=300"
        assert c.get(path, headers={"If-None-Match": '"stale"'}).status_code == 200
    # расчёты /ui/* — как раньше: no-store
    assert c.post("/ui/analyze", json=INPUT).headers["cache-control"] == "no-store"


# ---- 3–4. Один тяжёлый слот на адрес; разбор текста — без слота -------------------------------------
def test_one_heavy_slot_per_client_address():
    app = _app()
    gate = app.state.heavy_gate
    gate.client_wait = 0.3                                     # тест не ждёт 6 с
    pdf = helpers.pdf_with_text(["Hb 124 g/l"])
    files = {"file": ("a.pdf", io.BytesIO(pdf), "application/pdf")}
    with gate.slot("testclient"):                              # первый файл этого адреса ещё считается
        t0 = time.perf_counter()
        r = _client(app).post("/ui/parse", files=files)
        waited = time.perf_counter() - t0
        assert r.status_code == 503 and r.json()["error"]["code"] == "service_busy", r.text
        assert "предыдущий файл" in r.json()["error"]["message"] and int(r.headers["retry-after"]) >= 1
        assert waited >= 0.25, f"второй запрос того же адреса сначала ждёт ({waited:.2f} с)"
        # другой адрес в то же время получает второй слот процесса
        other = _client(app, client=("198.51.100.7", 4000))
        r = other.post("/ui/parse", files={"file": ("a.pdf", io.BytesIO(pdf), "application/pdf")})
        assert r.status_code == 200, r.text
        # вставленный текст слот не занимает — и этому же адресу отвечает сразу
        r = _client(app).post("/ui/parse", json={"text": "Гемоглобин 118 г/л"})
        assert r.status_code == 200 and r.json()["values"]["hemoglobin"]["value"] == 118.0
    assert _client(app).post("/ui/parse", files={"file": ("a.pdf", io.BytesIO(pdf), "application/pdf")}) \
        .status_code == 200, "слот адреса освобождается"
    assert gate._active == {}, "в памяти не остаётся адресов без расчётов"


def test_second_request_of_same_client_waits_for_first():
    app = _app()
    gate = app.state.heavy_gate
    gate.client_wait = 5.0
    held = threading.Event()
    release = threading.Event()

    def holder():
        with gate.slot("testclient"):
            held.set()
            release.wait(5)

    t = threading.Thread(target=holder)
    t.start()
    held.wait(5)
    threading.Timer(0.4, release.set).start()                 # первый расчёт закончится через 0,4 с
    r = _client(app).post("/ui/parse", files={"file": ("a.pdf", io.BytesIO(helpers.pdf_with_text(["Hb 124"])),
                                                       "application/pdf")})
    t.join(5)
    assert r.status_code == 200, r.text


def test_heavy_gate_unit():
    from deficitlens_api.security import HeavyGate, ServiceError

    g = HeavyGate(slots=2, per_client=1)
    with g.slot("a"):
        try:
            with g.slot("a"):
                raise AssertionError("второй слот тому же адресу")
        except ServiceError as e:
            assert e.status == 503 and e.code == "service_busy"
        with g.slot("b"):
            try:
                with g.slot("c"):
                    raise AssertionError("третий слот процессу")
            except ServiceError as e:
                assert e.status == 503
        with g.slot():                                         # вне запроса адрес не учитывается — только слоты
            pass
    assert g._active == {} and g._sem.acquire(blocking=False) and g._sem.acquire(blocking=False)


# ---- 5. Сжатие: статика, страницы, описание API — да; персональные данные — нет (BREACH) ----------------
def test_gzip_for_static_and_pages_only():
    from deficitlens_api.security import gzip_allowed

    c = _client(_app())
    gz = {"Accept-Encoding": "gzip"}
    for path in ("/static/app.js", "/static/app.css", "/", "/developers", "/api/v1/openapi.json", "/ui/reference"):
        r = c.get(path, headers=gz)
        assert r.status_code == 200 and r.headers.get("content-encoding") == "gzip", path
        assert "Accept-Encoding" in r.headers.get("vary", ""), path
    raw = c.get("/static/app.js", headers={"Accept-Encoding": "identity"})
    assert "content-encoding" not in raw.headers
    # woff2 и png уже сжаты — как есть
    assert "content-encoding" not in c.get("/static/fonts/ibm-plex-sans-cyrillic-400-normal.woff2", headers=gz).headers
    # ответы с данными пользователя — без сжатия, даже большие
    r = c.post("/ui/analyze", json=INPUT, headers=gz)
    assert r.status_code == 200 and len(r.content) > 1024 and "content-encoding" not in r.headers
    r = c.post("/api/v1/analyze", json=INPUT, headers={**gz, **HEADERS})
    assert r.status_code == 200 and "content-encoding" not in r.headers
    r = c.post("/api/v1/auth/register", json={"login": "gzip.user", "password": "gzip-pass-123", "role": "patient",
                                              "consent": True}, headers=gz)
    assert r.status_code == 201 and "content-encoding" not in r.headers
    token = r.json()["token"]
    r = c.get("/api/v1/me", headers={**gz, "Authorization": f"Bearer {token}"})
    assert r.status_code == 200 and "content-encoding" not in r.headers
    for path in ("/api/v1/me", "/api/v1/me/records", "/api/v1/auth/login", "/api/v1/doctor/patients",
                 "/api/v1/shares/accept", "/ui/analyze", "/ui/parse", "/ui/benchmark", "/api/v1/analyze",
                 "/api/v1/batch", "/healthz"):
        assert not gzip_allowed(path), path


# ---- 8. Кеш статики: ?v= — год, без метки — no-cache, шрифты — неделя; метка — хеш всего static/ --------
def test_static_cache_headers_and_version():
    c = _client(_app())
    html = c.get("/").text
    versions = set(re.findall(r'/static/[^"?]+\?v=([^"&]+)"', html))
    assert len(versions) == 1, versions
    v = versions.pop()
    assert re.fullmatch(r"[0-9a-f]{12}", v), v
    assert c.get("/developers").text.count(f"?v={v}") >= 2, "одна метка на все страницы"
    r = c.get(f"/static/app.js?v={v}")
    assert r.headers["cache-control"] == "public, max-age=31536000, immutable"
    assert c.get("/static/app.js").headers["cache-control"] == "no-cache"
    r = c.get("/static/fonts/ibm-plex-sans-cyrillic-700-normal.woff2")
    assert r.status_code == 200 and r.headers["cache-control"] == "public, max-age=604800"
    assert r.headers["content-type"] == "font/woff2"
    r = c.get(f"/static/no-such-file.js?v={v}")
    assert r.status_code == 404 and r.headers["cache-control"] == "no-cache", "ошибку нельзя кешировать на год"
    assert c.get("/healthz").headers["cache-control"] == "no-store"


def test_asset_version_tracks_nested_files():
    """Метка зависит от содержимого вложенных файлов (fonts, vendor, labs), а не только от верхнего уровня."""
    from deficitlens_api import app as app_module

    tmp = Path(tempfile.mkdtemp(prefix="jm-static-"))
    (tmp / "sub").mkdir()
    (tmp / "a.js").write_text("a", encoding="utf-8")
    (tmp / "sub" / "b.css").write_text("b", encoding="utf-8")
    saved, saved_ttl = app_module.HERE, app_module.STATIC_VERSION_TTL
    try:
        # HERE/static — каталог статики приложения; подменяем на время создания приложения
        fake = Path(tempfile.mkdtemp(prefix="jm-here-"))
        (fake / "static").symlink_to(tmp, target_is_directory=True)
        (fake / "templates").symlink_to(saved / "templates", target_is_directory=True)
        app_module.HERE = fake
        app_module.STATIC_VERSION_TTL = 0.0                    # перепроверять файлы на каждой странице
        c = _client(_app())

        def version() -> str:
            return re.search(r"\?v=([0-9a-f]+)", c.get("/").text).group(1)

        v1 = version()
        assert version() == v1, "без изменений метка та же"
        (tmp / "sub" / "b.css").write_text("b2", encoding="utf-8")
        v2 = version()
        assert v1 != v2, "изменился вложенный файл — метка другая"
        (tmp / "sub" / "b.css").write_text("b", encoding="utf-8")
        assert version() == v1, "метка — по содержимому: вернули файл — вернулась метка"
    finally:
        app_module.HERE, app_module.STATIC_VERSION_TTL = saved, saved_ttl


# ---- 9. Шрифты заранее; 13. значок -----------------------------------------------------------------
def test_font_preload_and_favicon():
    c = _client(_app())
    html = c.get("/").text
    for weight in ("400", "700"):
        href = f"/static/fonts/ibm-plex-sans-cyrillic-{weight}-normal.woff2"
        assert re.search(rf'<link rel="preload" href="{re.escape(href)}" as="font" type="font/woff2" crossorigin>',
                         html), weight
        assert c.get(href).status_code == 200
        css = c.get("/static/app.css").text
        assert f'url("fonts/ibm-plex-sans-cyrillic-{weight}-normal.woff2")' in css, "адрес как в @font-face"
    assert 'href="data:,"' not in html
    assert re.search(r'<link rel="icon" type="image/png" href="/static/favicon\.png', html)
    for path in ("/favicon.ico", "/static/favicon.png"):
        r = c.get(path)
        assert r.status_code == 200 and r.headers["content-type"] == "image/png", path
        assert r.content[:8] == b"\x89PNG\r\n\x1a\n", path


# ---- 10. Страница 404 браузеру, JSON остальным -------------------------------------------------------
def test_404_page_for_browser_json_for_api():
    c = _client(_app())
    r = c.get("/no-such-page", headers=BROWSER)
    assert r.status_code == 404 and r.headers["content-type"].startswith("text/html")
    assert "Страница" in r.text and 'href="/"' in r.text and 'lang="ru"' in r.text
    assert "no-such-page" not in r.text, "адрес из запроса в страницу не попадает"
    assert not re.search(r"<script(?![^>]*\bsrc=)[^>]*>", r.text) and "<style" not in r.text
    assert r.headers["content-security-policy"]
    for path in ("/no-such-page", "/api/v1/nope", "/ui/nope", "/static/nope.js", "/api", "/ui"):
        hdrs = {} if path == "/no-such-page" else BROWSER
        r = c.get(path, headers=hdrs)
        assert r.status_code == 404 and r.headers["content-type"] == "application/json", path
        assert r.json()["error"]["code"] in ("not_found", "http_error"), path


# ---- 12. /cabinet без cookie — на вход ---------------------------------------------------------------
def test_cabinet_without_session_redirects_to_login():
    app = _app()
    c = _client(app)
    r = c.get("/cabinet", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login?next=/cabinet"
    r = c.get("/cabinet")
    assert r.status_code == 200 and 'id="auth"' in r.text, "после перехода — страница входа"
    c.cookies.set("jm_session", "any")                         # страница смотрит только, есть ли cookie
    r = c.get("/cabinet", follow_redirects=False)
    assert r.status_code == 200 and 'id="cab"' in r.text
    secure = _client(_app(cookie_secure=True))
    assert secure.get("/cabinet", follow_redirects=False).status_code == 303
    secure.cookies.set("__Host-jm_session", "any")
    assert secure.get("/cabinet", follow_redirects=False).status_code == 200


# ---- 13. HSTS только за HTTPS ------------------------------------------------------------------------
def test_hsts_only_with_cookie_secure():
    c = _client(_app())
    for path in ("/", "/healthz", "/static/app.js"):
        assert "strict-transport-security" not in c.get(path).headers, path
    c = _client(_app(cookie_secure=True))
    for path in ("/", "/healthz", "/static/app.js", "/no-such-page"):
        assert c.get(path).headers["strict-transport-security"] == "max-age=31536000", path


# ---- 11. Внешняя обёртка: исключение в middleware — 500 JSON, в журнале только тип и место -------------
def test_unhandled_exception_outside_routes_is_500_json_without_trace():
    app = _app()

    class Boom:
        def __init__(self, inner):
            self.inner = inner

        async def __call__(self, scope, receive, send):
            if scope["type"] == "http" and scope["path"] == "/boom":
                raise ValueError("CANARY-SECRET hemoglobin 123.456")
            await self.inner(scope, receive, send)

    app.add_middleware(Boom)
    capture = _Capture()
    logger = logging.getLogger("deficitlens")
    logger.addHandler(capture)
    try:
        r = _client(app).get("/boom")
        ok = _client(app).get("/healthz")
    finally:
        logger.removeHandler(capture)
    assert r.status_code == 500 and r.json()["error"]["code"] == "internal", r.text
    assert "CANARY" not in r.text and "Traceback" not in r.text and "123.456" not in r.text
    assert ok.status_code == 200, "сервис работает дальше"
    text = "\n".join(capture.lines)
    assert "internal error ValueError at test_reliability.py:" in text, text
    assert "CANARY" not in text and "123.456" not in text
    assert text.count("internal error") == 1, "одна строка на ошибку"


# ---- 15. analyze: не больше ANALYZE_CONCURRENCY расчётов в потоках одновременно ---------------------------
def test_analyze_concurrency_is_limited():
    from deficitlens_api import app as app_module
    from deficitlens_api import service

    state = {"now": 0, "max": 0}
    lock = threading.Lock()
    original = service.analyze_one

    def slow(payload):
        with lock:
            state["now"] += 1
            state["max"] = max(state["max"], state["now"])
        time.sleep(0.05)
        with lock:
            state["now"] -= 1
        return {"ok": True}

    service.analyze_one = slow
    try:
        with _client(_app()) as c:                             # один цикл событий на все потоки
            results = []

            def call():
                results.append(c.post("/ui/analyze", json=INPUT).status_code)

            threads = [threading.Thread(target=call) for _ in range(12)]
            [t.start() for t in threads]
            [t.join(30) for t in threads]
    finally:
        service.analyze_one = original
    assert results == [200] * 12, results
    assert 1 < state["max"] <= app_module.ANALYZE_CONCURRENCY == 4, state


# ---- 3. Пределы PDF: 4 с на разбор, 5 МБ на файл, битый файл — сразу ----------------------------------
def test_pdf_limits():
    from deficitlens_core.io import pdf_text

    assert pdf_text.MAX_SECONDS == 4.0 and pdf_text.MAX_PDF_BYTES == 5 * 1024 * 1024
    c = _client(_app())
    big = b"%PDF-1.4\n" + b"0" * (5 * 1024 * 1024)
    r = c.post("/ui/parse", files={"file": ("big.pdf", io.BytesIO(big), "application/pdf")})
    assert r.status_code == 413 and r.json()["error"]["code"] == "pdf_too_large", r.text
    assert "5 МБ" in r.json()["error"]["message"] and r.json()["error"]["field"] == "file"
    broken = b"%PDF-1.4\n" + b"0" * (4 * 1024 * 1024)        # pdfminer разбирал бы такой файл ~0,5–2 с
    t0 = time.perf_counter()
    r = c.post("/ui/parse", files={"file": ("broken.pdf", io.BytesIO(broken), "application/pdf")})
    took = time.perf_counter() - t0
    assert r.status_code == 422 and r.json()["error"]["code"] == "bad_pdf", r.text
    assert took < 1.5, f"битый PDF отклоняется проверкой pypdfium2 до pdfminer ({took:.2f} с)"


def test_pdf_worker_does_not_import_engine_or_pydantic():
    """Дочерний процесс разбора PDF (и фото) не тянет numpy и pydantic: запуск быстрее."""
    import subprocess
    import sys

    code = ("import sys, deficitlens_core.io.pdf_text, deficitlens_core.io.photo_ocr; "
            "print(sorted(m for m in ('numpy', 'pydantic', 'pandas', 'sklearn') if m in sys.modules))")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60,
                         env={"PYTHONPATH": str(helpers.ROOT / "src")})
    assert out.returncode == 0 and out.stdout.strip() == "[]", (out.stdout, out.stderr[-300:])
    import deficitlens_core

    assert deficitlens_core.__version__, "версия движка по-прежнему доступна"


# ---- 2. Регистрации с адреса — настройка ------------------------------------------------------------
def test_signup_ip_per_hour_setting():
    import os

    saved = os.environ.get("DL_SIGNUP_IP_PER_HOUR")
    try:
        os.environ.pop("DL_SIGNUP_IP_PER_HOUR", None)
        assert Settings.from_env().signup_ip_per_hour == 30 == Settings().signup_ip_per_hour
        os.environ["DL_SIGNUP_IP_PER_HOUR"] = "7"
        assert Settings.from_env().signup_ip_per_hour == 7
    finally:
        if saved is None:
            os.environ.pop("DL_SIGNUP_IP_PER_HOUR", None)
        else:
            os.environ["DL_SIGNUP_IP_PER_HOUR"] = saved


def test_gzip_body_is_valid():
    c = _client(_app())
    r = c.get("/static/app.js", headers={"Accept-Encoding": "gzip"})
    plain = c.get("/static/app.js", headers={"Accept-Encoding": "identity"})
    assert r.headers.get("content-encoding") == "gzip" and "content-encoding" not in plain.headers
    # httpx распаковывает сам; сверяем с исходным файлом
    assert r.content == plain.content == (helpers.ROOT / "src" / "deficitlens_api" / "static" / "app.js").read_bytes()
