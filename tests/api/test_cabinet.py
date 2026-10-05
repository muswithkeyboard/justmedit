"""Личный кабинет (ADR 0009, docs/CONTRACTS.md — «Хранение и кабинет»): вход, сессии, CSRF, записи, шифрование
в базе, выгрузка и загрузка, доступ врача по ссылке, журнал доступа, полное удаление, референсы врача, Bearer на
маршрутах анализа, CORS. Каждый тест — своё приложение и свой временный каталог базы. Все значения придуманы.
"""
from __future__ import annotations

import contextlib
import io
import logging
import re
import sqlite3
import stat
import sys
import tempfile
import time
import types
from pathlib import Path

import helpers

from deficitlens_api.app import create_app
from deficitlens_api.settings import Settings

KEY = "cabinet-key-1"
PASSWORD = "correct-horse-1"
CSRF = {"X-Justmedit": "1", "Origin": "http://testserver"}
INPUT = {"sex": "F", "age_years": 34,
         "values": {"hemoglobin": 118, "MCV": 78, "MCH": 25.4, "RDW": 15.6, "RBC": 4.4, "ferritin": 11}}


def _app(**overrides):
    """Новое приложение с базой во временном каталоге: (app, каталог данных)."""
    data_dir = tempfile.mkdtemp(prefix="jm-cabinet-")
    settings = {"api_keys": (KEY,), "data_dir": data_dir, "ui_rate_per_min": 10000, "auth_rate_per_min": 1000,
                "auth_ip_rate_per_min": 1000, "cabinet_rate_per_min": 100000, **overrides}
    return create_app(Settings(**settings)), Path(data_dir)


def _client(app):
    try:
        from starlette.testclient import TestClient
    except Exception:  # noqa: BLE001 — нет httpx
        helpers.skip("нет httpx: тесты кабинета идут через starlette.testclient (uv sync --extra dev)")
    return TestClient(app)


def _bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _register(c, login: str, role: str = "patient", password: str = PASSWORD, **extra) -> tuple[str, dict]:
    body = {"login": login, "password": password, "role": role, "consent": True, **extra}
    r = c.post("/api/v1/auth/register", json=body)
    assert r.status_code == 201, r.text
    return r.json()["token"], r.json()["user"]


def _share(c, token: str, days: int = 7, password: str = PASSWORD) -> dict:
    """Ссылка врачу по Bearer-токену — с паролем аккаунта (без пароля токен API ссылок не создаёт)."""
    r = c.post("/api/v1/me/shares", json={"days": days, "password": password}, headers=_bearer(token))
    assert r.status_code == 200, r.text
    return r.json()


def _add_record(c, token: str, date: str = "2026-03-01", inp: dict | None = None, **extra) -> dict:
    r = c.post("/api/v1/me/records", json={"date": date, "input": inp or INPUT, "source": "form", **extra},
               headers=_bearer(token))
    assert r.status_code == 201, r.text
    return r.json()


def _db_bytes(data_dir: Path) -> bytes:
    """Все файлы базы (основной, WAL, SHM) одной строкой байтов — для поиска открытого текста."""
    return b"".join(p.read_bytes() for p in data_dir.iterdir() if p.name.startswith("justmedit.db"))


@contextlib.contextmanager
def _history_stub():
    """Подмена history.analyze (модуль пишет поток П4): записываются вызовы, ответ — короткий словарь."""
    calls: list[dict] = []

    def fake(records, *, cfg=None, models=None, role="patient", region_prices=None, **kw):
        calls.append({"records": records, "role": role})
        return {"role": role, "n_records": len(records), "stub": True}

    try:
        from deficitlens_core import history as module
        created = False
    except ImportError:
        module, created = types.ModuleType("deficitlens_core.history"), True
        sys.modules["deficitlens_core.history"] = module
        import deficitlens_core
        deficitlens_core.history = module
    saved = getattr(module, "analyze", None)
    module.analyze = fake
    try:
        yield calls
    finally:
        if created:
            sys.modules.pop("deficitlens_core.history", None)
            import deficitlens_core
            if getattr(deficitlens_core, "history", None) is module:
                del deficitlens_core.history
        else:
            module.analyze = saved


# --------------------------------------------------------------------------------------------
# Вход, выход, профиль
# --------------------------------------------------------------------------------------------
def test_register_login_logout():
    app, _ = _app()
    c = _client(app)
    r = c.post("/api/v1/auth/register", json={"login": "Patient.One", "password": PASSWORD, "role": "patient",
                                               "clinic": "не сохраняется у пациента", "consent": True})
    assert r.status_code == 201, r.text
    body = r.json()
    assert set(body["user"]) == {"id", "login", "role", "clinic", "created"}
    assert body["user"]["role"] == "patient" and body["user"]["clinic"] is None and body["token"]
    cookie = r.headers["set-cookie"]
    assert cookie.startswith("jm_session=") and "HttpOnly" in cookie and "Path=/" in cookie
    assert "SameSite=strict" in cookie and "Max-Age=604800" in cookie and "Secure" not in cookie
    assert c.get("/api/v1/me").json()["login"] == "Patient.One"                          # по cookie
    fresh = _client(app)
    assert fresh.get("/api/v1/me").status_code == 401
    assert fresh.get("/api/v1/me", headers=_bearer(body["token"])).status_code == 200     # по Bearer
    # логин без учёта регистра: вход и повторная регистрация
    r = fresh.post("/api/v1/auth/login", json={"login": "patient.one", "password": PASSWORD})
    assert r.status_code == 200 and r.json()["user"]["id"] == body["user"]["id"] and r.json()["token"]
    login_token = r.json()["token"]
    r = fresh.post("/api/v1/auth/register", json={"login": "PATIENT.ONE", "password": PASSWORD, "role": "patient",
                                                   "consent": True})
    assert r.status_code == 409 and r.json()["error"]["code"] == "login_taken"
    # выход по cookie: нужна защита от CSRF; cookie стирается, токен API этого входа тоже закрыт
    assert c.post("/api/v1/auth/logout").status_code == 403
    r = c.post("/api/v1/auth/logout", headers=CSRF)
    assert r.status_code == 200 and r.json() == {"ok": True} and 'jm_session=""' in r.headers["set-cookie"]
    assert c.get("/api/v1/me").status_code == 401
    assert c.get("/api/v1/me", headers=_bearer(body["token"])).status_code == 401, "токен входа закрыт вместе с cookie"
    assert c.get("/api/v1/me", headers=_bearer(login_token)).status_code == 200, "другой вход не затронут"
    # выход по Bearer
    assert c.post("/api/v1/auth/logout", headers=_bearer(login_token)).status_code == 200
    assert c.get("/api/v1/me", headers=_bearer(login_token)).status_code == 401
    r = c.get("/api/v1/me", headers={"Authorization": "Bearer nope"})
    assert r.status_code == 401 and r.json()["error"]["code"] == "unauthorized"
    assert r.headers["www-authenticate"].startswith("Bearer")


def test_register_validation():
    app, _ = _app()
    c = _client(app)
    base = {"login": "doc_1", "password": PASSWORD, "role": "doctor", "clinic": "Клиника №1", "consent": True}
    for change, code, field in (({"login": "ab"}, "invalid_login", "login"),
                                ({"login": "имя с пробелом"}, "invalid_login", "login"),
                                ({"login": "doc_1\n"}, "invalid_login", "login"),
                                ({"login": "doc<b>"}, "invalid_login", "login"),
                                ({"login": 12345}, "invalid_login", "login"),
                                ({"login": "x" * 65}, "invalid_login", "login"),
                                ({"password": "short"}, "invalid_password", "password"),
                                ({"password": "p" * 129}, "invalid_password", "password"),
                                ({"role": "admin"}, "invalid_role", "role"),
                                ({"clinic": "к" * 121}, "invalid_clinic", "clinic"),
                                ({"consent": False}, "consent_required", "consent"),
                                ({"consent": "yes"}, "consent_required", "consent")):
        r = c.post("/api/v1/auth/register", json={**base, **change})
        assert r.status_code == 422, (change, r.text)
        assert r.json()["error"]["code"] == code and r.json()["error"]["field"] == field, (change, r.json())
    # тело не JSON-типа (простая HTML-форма чужого сайта) — 415
    r = c.post("/api/v1/auth/register", content=b'{"login":"doc_1"}', headers={"Content-Type": "text/plain"})
    assert r.status_code == 415 and r.json()["error"]["code"] == "unsupported_media_type"
    r = c.post("/api/v1/auth/register", json=base)
    assert r.status_code == 201 and r.json()["user"]["clinic"] == "Клиника №1" and r.json()["user"]["role"] == "doctor"


def test_wrong_login_and_wrong_password_look_the_same():
    app, _ = _app()
    c = _client(app)
    _register(c, "real_user")
    answers = []
    for body in ({"login": "real_user", "password": "wrong-password"},
                 {"login": "no_such_user", "password": "wrong-password"},
                 {"login": "x", "password": "wrong-password"},
                 {"login": "real_user", "password": ""}):
        t0 = time.perf_counter()
        r = c.post("/api/v1/auth/login", json=body)
        answers.append((r.status_code, r.json(), time.perf_counter() - t0))
    assert all(a[0] == 401 for a in answers), answers
    assert all(a[1] == answers[0][1] for a in answers), "одинаковый ответ"
    assert answers[0][1]["error"]["message"] == "Неверный логин или пароль."
    # время: неизвестный логин тоже считает scrypt (не быстрее неверного пароля в разы)
    assert answers[1][2] > answers[0][2] / 5, answers


def test_login_rate_limit_429():
    app, _ = _app(auth_rate_per_min=3)
    c = _client(app)
    _register(c, "limited")
    codes = [c.post("/api/v1/auth/login", json={"login": "limited", "password": "bad-password"}).status_code
             for _ in range(4)]
    assert codes == [401, 401, 429, 429], codes                    # регистрация — первая из трёх попыток
    r = c.post("/api/v1/auth/login", json={"login": "limited", "password": PASSWORD})
    assert r.status_code == 429 and int(r.headers["retry-after"]) >= 1
    assert r.json()["error"]["code"] == "rate_limited"
    # другой логин с того же адреса — своё окно
    assert c.post("/api/v1/auth/login", json={"login": "other", "password": "bad-password"}).status_code == 401
    app, _ = _app(auth_ip_rate_per_min=2)
    c = _client(app)
    codes = [c.post("/api/v1/auth/login", json={"login": f"user{i}", "password": "bad-password"}).status_code
             for i in range(3)]
    assert codes == [401, 401, 429], codes                         # и общий предел на адрес


def test_sessions_expire():
    app, _ = _app()
    c = _client(app)
    token, _ = _register(c, "expiring")
    named = c.post("/api/v1/me/tokens", json={"name": "Интеграция"}, headers=CSRF).json()["value"]   # по cookie
    store = app.state.store
    real = store.clock
    try:
        store.clock = lambda: real() + 6 * 86400
        assert c.get("/api/v1/me").status_code == 200 and c.get("/api/v1/me", headers=_bearer(token)).status_code == 200
        store.clock = lambda: real() + 8 * 86400                   # cookie живёт 7 дней — и токен «Вход» с ней
        assert c.get("/api/v1/me").status_code == 401
        assert c.get("/api/v1/me", headers=_bearer(token)).status_code == 401, "токен входа истекает с cookie"
        assert c.get("/api/v1/me", headers=_bearer(named)).status_code == 200, "именованный токен — 30 дней"
        store.clock = lambda: real() + 31 * 86400
        assert c.get("/api/v1/me", headers=_bearer(named)).status_code == 401
    finally:
        store.clock = real


def test_cookie_secure_flag():
    app, _ = _app(cookie_secure=True)
    r = _client(app).post("/api/v1/auth/register", json={"login": "secure_one", "password": PASSWORD,
                                                         "role": "patient", "consent": True})
    cookie = r.headers["set-cookie"]
    # за HTTPS — префикс __Host-: браузер примет cookie только с Secure, Path=/ и без Domain
    assert cookie.startswith("__Host-jm_session=") and "Secure" in cookie and "Path=/" in cookie
    assert "domain" not in cookie.lower() and "HttpOnly" in cookie and "SameSite=strict" in cookie


# --------------------------------------------------------------------------------------------
# CSRF и Bearer
# --------------------------------------------------------------------------------------------
def test_cookie_requests_need_csrf_header_and_own_origin():
    app, _ = _app()
    c = _client(app)
    token, _ = _register(c, "csrf_user")
    body = {"date": "2026-01-15", "input": INPUT}
    r = c.post("/api/v1/me/records", json=body)
    assert r.status_code == 403 and r.json()["error"]["code"] == "csrf"
    r = c.post("/api/v1/me/records", json=body, headers={"X-Justmedit": "1", "Origin": "https://evil.example"})
    assert r.status_code == 403 and r.json()["error"]["code"] == "csrf"
    r = c.post("/api/v1/me/records", json=body, headers={"X-Justmedit": "1", "Referer": "https://evil.example/x"})
    assert r.status_code == 403
    r = c.post("/api/v1/me/records", json=body, headers={"X-Justmedit": "0", "Origin": "http://testserver"})
    assert r.status_code == 403
    r = c.post("/api/v1/me/records", json=body, headers=CSRF)
    assert r.status_code == 201, r.text
    rid = r.json()["record"]["id"]
    # Origin: null (страница с Referrer-Policy: no-referrer) — держат заголовок X-Justmedit и SameSite=Strict
    assert c.post("/api/v1/me/records", json=body, headers={"X-Justmedit": "1", "Origin": "null"}).status_code == 201
    # X-Forwarded-Host от не доверенного прокси не учитывается (DL_TRUSTED_PROXIES пуст; доверенный —
    # tests/api/test_cabinet_security.py)
    r = c.post("/api/v1/me/records", json=body, headers={"X-Justmedit": "1", "Origin": "https://jm.example",
                                                          "X-Forwarded-Host": "jm.example"})
    assert r.status_code == 403 and r.json()["error"]["code"] == "csrf"
    assert c.get("/api/v1/me/records").status_code == 200, "чтение по cookie — без заголовка"
    assert c.delete(f"/api/v1/me/records/{rid}").status_code == 403
    assert c.delete(f"/api/v1/me/records/{rid}", headers=CSRF).status_code == 200
    # Bearer — без заголовка CSRF и без cookie
    bearer_only = _client(app)
    r = bearer_only.post("/api/v1/me/records", json=body, headers=_bearer(token))
    assert r.status_code == 201, r.text


def test_bearer_on_analysis_routes_without_api_key():
    app, _ = _app()
    c = _client(app)
    token, _ = _register(c, "api_user")
    api = _client(app)                                             # без cookie
    payload = helpers.load_examples()[0]["input"]
    r = api.post("/api/v1/analyze", json=payload, headers=_bearer(token))
    assert r.status_code == 200 and r.json()["level1"]["anemia"] is False
    assert api.get("/api/v1/reference", headers=_bearer(token)).status_code == 200
    assert api.post("/api/v1/parse", json={"text": "Гемоглобин 120"}, headers=_bearer(token)).status_code == 200
    r = api.post("/api/v1/batch", json={"records": [payload]}, headers=_bearer(token))
    assert r.status_code == 200 and r.json()["summary"]["done"] == 1
    r = api.post("/api/v1/analyze", json=payload, headers=_bearer("not-a-token"))
    assert r.status_code == 401 and r.json()["error"]["code"] == "unauthorized"
    assert api.post("/api/v1/analyze", json=payload).status_code == 401
    assert api.post("/api/v1/analyze", json=payload, headers={"X-API-Key": KEY}).status_code == 200
    # Authorization другого вида (Basic от прокси) не мешает ключу API
    r = api.post("/api/v1/analyze", json=payload, headers={"X-API-Key": KEY, "Authorization": "Basic eDp5"})
    assert r.status_code == 200


def test_api_tokens():
    app, _ = _app()
    c = _client(app)
    token, _ = _register(c, "token_owner")
    r = c.post("/api/v1/me/tokens", json={"name": "Сайт клиники", "password": PASSWORD}, headers=_bearer(token))
    assert r.status_code == 200, r.text
    created = r.json()
    value, tid = created["value"], created["token"]["id"]
    assert created["token"]["name"] == "Сайт клиники" and "value" not in created["token"]
    listing = c.get("/api/v1/me/tokens", headers=_bearer(value))
    assert listing.status_code == 200
    names = {t["name"]: t for t in listing.json()["tokens"]}
    assert {"Сайт клиники", "Вход"} <= set(names) and names["Сайт клиники"]["current"] is True
    assert names["Сайт клиники"]["kind"] == "named" and names["Вход"]["kind"] == "login"
    # по cookie сайта текущий — токен «Вход» этой сессии (интерфейс не показывает токены входа в списке)
    by_cookie = c.get("/api/v1/me/tokens").json()["tokens"]
    assert [t["kind"] for t in by_cookie if t["current"]] == ["login"]
    assert value not in listing.text and token not in listing.text, "значение токена — только в ответе на создание"
    assert c.delete(f"/api/v1/me/tokens/{tid}", headers=_bearer(token)).status_code == 200
    assert c.get("/api/v1/me", headers=_bearer(value)).status_code == 401
    assert c.delete(f"/api/v1/me/tokens/{tid}", headers=_bearer(token)).status_code == 404
    assert c.get("/api/v1/me").status_code == 200, "удаление токена API не закрывает сайт"


def test_roles_are_enforced():
    app, _ = _app()
    c = _client(app)
    patient, _ = _register(c, "role_patient")
    doctor, _ = _register(_client(app), "role_doctor", role="doctor")
    for path in ("/api/v1/doctor/patients", "/api/v1/doctor/lab-refs"):
        r = c.get(path, headers=_bearer(patient))
        assert r.status_code == 403 and r.json()["error"]["code"] == "forbidden", path
    for path in ("/api/v1/me/records", "/api/v1/me/history", "/api/v1/me/shares", "/api/v1/me/access-log",
                 "/api/v1/me/export"):
        assert c.get(path, headers=_bearer(doctor)).status_code == 403, path
    assert c.post("/api/v1/me/records", json={"input": INPUT}, headers=_bearer(doctor)).status_code == 403
    assert c.post("/api/v1/shares/accept", json={"token": "x"}, headers=_bearer(patient)).status_code == 403
    assert c.get("/api/v1/me", headers=_bearer(doctor)).json()["role"] == "doctor"


def test_me_with_stale_cookie_clears_it():
    """Cookie сессии больше не действует (выход везде, смена пароля на другом устройстве) — /api/v1/me отвечает 401
    и стирает cookie: следующая страница сразу рисует в шапке «Войти», без мигания «Кабинет» -> «Войти»."""
    app, _ = _app()
    here, there = _client(app), _client(app)
    token, _ = _register(here, "stale_cookie")
    assert there.post("/api/v1/auth/login", json={"login": "stale_cookie", "password": PASSWORD}).status_code == 200
    assert 'data-session="1"' in there.get("/").text
    assert here.post("/api/v1/me/logout-all", json={}, headers=CSRF).status_code == 200      # закрыть другие входы
    r = there.get("/api/v1/me")
    assert r.status_code == 401 and r.json()["error"]["code"] == "unauthorized"
    assert 'jm_session=""' in r.headers["set-cookie"] and "max-age=0" in r.headers["set-cookie"].lower()
    assert 'data-session="0"' in there.get("/").text and there.get("/api/v1/me").status_code == 401
    # без cookie и по неверному Bearer — 401 без Set-Cookie
    for headers in ({}, _bearer("x" * 40)):
        r = _client(app).get("/api/v1/me", headers=headers)
        assert r.status_code == 401 and "set-cookie" not in r.headers, headers
    assert here.get("/api/v1/me").status_code == 200 and here.get("/api/v1/me", headers=_bearer(token)).status_code == 200


# --------------------------------------------------------------------------------------------
# Записи пациента
# --------------------------------------------------------------------------------------------
def test_records_crud():
    app, _ = _app()
    c = _client(app)
    token, _ = _register(c, "crud_user")
    created = _add_record(c, token, "2026-02-01")
    rec, result = created["record"], created["result"]
    assert set(rec) == {"id", "date", "source", "headline", "anemia", "created"}
    assert rec["date"] == "2026-02-01" and rec["anemia"] is True and rec["headline"]
    assert rec["headline"] == result["reports"]["patient"]["headline"] and result["level1"]["anemia"] is True
    _add_record(c, token, "2026-05-01")
    listing = c.get("/api/v1/me/records", headers=_bearer(token)).json()["records"]
    assert [x["date"] for x in listing] == ["2026-05-01", "2026-02-01"], "новые сверху"
    r = c.get(f"/api/v1/me/records/{rec['id']}", headers=_bearer(token))
    assert r.status_code == 200
    assert r.json()["record"]["input"]["values"]["hemoglobin"] == 118.0 and r.json()["result"]["level1"]["anemia"]
    # чужой пациент запись не видит и не удалит
    other, _ = _register(_client(app), "crud_other")
    assert c.get(f"/api/v1/me/records/{rec['id']}", headers=_bearer(other)).status_code == 404
    assert c.delete(f"/api/v1/me/records/{rec['id']}", headers=_bearer(other)).status_code == 404
    assert c.get("/api/v1/me/records/not-an-id", headers=_bearer(token)).status_code == 404
    assert c.get(f"/api/v1/me/records/{rec['id']}%0A", headers=_bearer(token)).status_code == 404
    assert c.delete(f"/api/v1/me/records/{rec['id']}", headers=_bearer(token)).json() == {"deleted": True}
    assert c.get(f"/api/v1/me/records/{rec['id']}", headers=_bearer(token)).status_code == 404
    # ошибки ввода — как у /api/v1/analyze, с полем
    r = c.post("/api/v1/me/records", json={"date": "2026-02-01", "input": {**INPUT, "age_years": 10}},
               headers=_bearer(token))
    assert r.status_code == 422 and r.json()["error"]["field"] == "age_years"
    for bad_date in ("01.02.2026", "2026-02-30", "1800-01-01", "2999-01-01", 20260201, "2026-02-01\n",
                     "٢٠٢٦-٠٢-٠١"):
        r = c.post("/api/v1/me/records", json={"date": bad_date, "input": INPUT}, headers=_bearer(token))
        assert r.status_code == 422 and r.json()["error"]["code"] == "invalid_date", bad_date
    r = c.post("/api/v1/me/records", json={"date": "2026-02-01", "input": INPUT, "source": "fax"},
               headers=_bearer(token))
    assert r.status_code == 422 and r.json()["error"]["field"] == "source"
    r = c.post("/api/v1/me/records", json={"date": "2026-02-01", "input": "нет"}, headers=_bearer(token))
    assert r.status_code == 422 and r.json()["error"]["field"] == "input"


def test_records_limit_500():
    app, _ = _app()
    c = _client(app)
    token, user = _register(c, "many_records")
    item = {"date": "2025-01-01", "source": "import", "input": INPUT, "result": None, "version": "old",
            "summary": {"headline_doctor": "x", "headline_patient": "y", "anemia": False, "date": "2025-01-01"}}
    app.state.store.add_records(user["id"], [item] * 499, 500)
    _add_record(c, token)                                          # пятисотая — ещё можно
    r = c.post("/api/v1/me/records", json={"date": "2026-01-01", "input": INPUT}, headers=_bearer(token))
    assert r.status_code == 422 and r.json()["error"]["code"] == "too_many_records"
    r = c.post("/api/v1/me/import", json={"records": [{"date": "2026-01-01", "input": INPUT}]}, headers=_bearer(token))
    assert r.status_code == 422 and r.json()["error"]["code"] == "too_many_records"
    assert len(c.get("/api/v1/me/records", headers=_bearer(token)).json()["records"]) == 500


def test_database_has_no_plaintext_values():
    """В файлах базы (основной, WAL, SHM) нет значений анализов, меток и текстов ответа — только шифртекст."""
    app, data_dir = _app()
    c = _client(app)
    token, _ = _register(c, "plain_check")
    inp = {**INPUT, "patient_ref": "CANARYQZX7", "values": {**INPUT["values"], "hemoglobin": 123.456,
                                                            "ferritin": {"value": 77.531, "unit": "ng/mL"}}}
    created = _add_record(c, token, "2026-04-01", inp)
    headline = created["result"]["reports"]["doctor"]["headline"]
    doc, _ = _register(_client(app), "plain_doc", role="doctor")
    r = c.post("/api/v1/doctor/lab-refs", json={"name": "Лаб", "default": True,
                                                "ranges": {"hemoglobin": {"low": 111.777, "high": 149.333}}},
               headers=_bearer(doc))
    assert r.status_code == 200, r.text
    raw = _db_bytes(data_dir)
    assert raw, "база создана"
    for canary in (b"123.456", b"123,456", b"77.531", b"111.777", b"149.333", b"CANARYQZX7", b"hemoglobin",
                   "Гемоглобин".encode(), headline.encode("utf-8")[:40], token.encode()):
        assert canary not in raw, canary
    assert b"plain_check" in raw, "логин хранится открыто (нужен для поиска)"
    # ключ — отдельным файлом с правами 0600, база — 0600
    key = data_dir / "key"
    assert key.exists() and stat.S_IMODE(key.stat().st_mode) == 0o600
    assert key.read_bytes().strip() not in raw
    assert stat.S_IMODE((data_dir / "justmedit.db").stat().st_mode) == 0o600


def test_data_key_from_env_and_wrong_key():
    from cryptography.fernet import Fernet
    key = Fernet.generate_key().decode()
    app, data_dir = _app(data_key=key)
    c = _client(app)
    token, _ = _register(c, "env_key")
    _add_record(c, token)
    assert not (data_dir / "key").exists(), "ключ из DL_DATA_KEY: файл ключа не создаётся"
    app.state.store.close()
    # та же база с другим ключом: записи не расшифровываются — понятная ошибка без содержимого
    app2 = create_app(Settings(api_keys=(KEY,), data_dir=str(data_dir), data_key=Fernet.generate_key().decode()))
    r = _client(app2).get("/api/v1/me/records", headers=_bearer(token))
    assert r.status_code == 500 and r.json()["error"]["code"] == "storage_key"
    # и ротация: новый ключ первым, старый — для чтения
    app3 = create_app(Settings(api_keys=(KEY,), data_dir=str(data_dir),
                               data_key=Fernet.generate_key().decode() + "," + key))
    assert _client(app3).get("/api/v1/me/records", headers=_bearer(token)).status_code == 200
    app4 = create_app(Settings(api_keys=(KEY,), data_dir=str(data_dir), data_key="not-a-key"))
    r = _client(app4).get("/api/v1/me", headers=_bearer(token))
    assert r.status_code == 503 and r.json()["error"]["code"] == "storage_unavailable"
    # анонимный расчёт от хранилища не зависит
    assert _client(app4).post("/ui/analyze", json=INPUT).status_code == 200


# --------------------------------------------------------------------------------------------
# Выгрузка и загрузка
# --------------------------------------------------------------------------------------------
def test_export_import_roundtrip():
    app, _ = _app()
    c = _client(app)
    token, _ = _register(c, "exporter")
    _add_record(c, token, "2026-01-10")
    inp2 = {"sex": "F", "age_years": 34, "values": {"hemoglobin": {"value": 11.2, "unit": "g/dL"},
                                                    "ferritin": {"value": 9, "unit": "ng/mL"}}}
    _add_record(c, token, "2026-06-10", inp2)
    r = c.get("/api/v1/me/export?format=json", headers=_bearer(token))
    assert r.status_code == 200 and "attachment" in r.headers["content-disposition"]
    exported = r.json()
    assert exported["format"] == "justmedit-export" and len(exported["records"]) == 2
    r = c.get("/api/v1/me/export?format=csv", headers=_bearer(token))
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
    lines = r.text.strip().split("\n")
    header = lines[0].split(",")
    assert header[:3] == ['"sex"', '"age_years"', '"hemoglobin"'] and header[-1] == '"date"'
    assert len(lines) == 3
    second = dict(zip([h.strip('"') for h in header], [v.strip('"') for v in lines[2].split(",")]))
    assert float(second["hemoglobin"]) == 112.0, "в канонических единицах (г/дл -> г/л)"
    assert second["date"] == "2026-06-10" and second["MCV"] == ""
    assert c.get("/api/v1/me/export?format=xml", headers=_bearer(token)).status_code == 422
    # имя файла — по местной дате страницы (?day=), если она в пределах суток от даты сервера; иначе — дата сервера
    store = app.state.store
    today = time.strftime("%Y-%m-%d", time.gmtime(store.now()))
    tomorrow = time.strftime("%Y-%m-%d", time.gmtime(store.now() + 86400))
    for day, expect in ((tomorrow, tomorrow), ("2001-01-01", today), ("x", today), ("2026-02-31", today)):
        r = c.get(f"/api/v1/me/export?format=json&day={day}", headers=_bearer(token))
        assert f'filename="justmedit-{expect}.json"' in r.headers["content-disposition"], day
    # в другой аккаунт
    other, _ = _register(_client(app), "importer")
    r = c.post("/api/v1/me/import", json=exported, headers=_bearer(other))
    assert r.status_code == 200 and r.json() == {"imported": 2, "skipped": 0}, r.text
    a = {x["date"]: x for x in c.get("/api/v1/me/records", headers=_bearer(token)).json()["records"]}
    b = {x["date"]: x for x in c.get("/api/v1/me/records", headers=_bearer(other)).json()["records"]}
    assert set(a) == set(b) and all(b[d]["headline"] == a[d]["headline"] for d in a)
    assert all(x["source"] == "import" for x in b.values())
    back = c.get("/api/v1/me/export?format=json", headers=_bearer(other)).json()["records"]
    assert [x["input"] for x in back] == [x["input"] for x in exported["records"]]
    # ошибка в одной записи — не загружается ничего
    broken = {"records": [exported["records"][0], {"date": "2026-01-01", "input": {**INPUT, "sex": "X"}}]}
    r = c.post("/api/v1/me/import", json=broken, headers=_bearer(other))
    assert r.status_code == 422 and r.json()["error"]["field"] == "records[1].sex"
    r = c.post("/api/v1/me/import", json={"records": [{"input": INPUT}]}, headers=_bearer(other))
    assert r.status_code == 422 and r.json()["error"]["field"] == "records[0].date"
    assert c.post("/api/v1/me/import", json={"format": "other", "records": []}, headers=_bearer(other)).status_code == 422
    assert len(c.get("/api/v1/me/records", headers=_bearer(other)).json()["records"]) == 2


def test_import_own_export_skips_duplicates():
    """Загрузка своей же выгрузки ничего не удваивает: записи с той же датой и тем же входом (пол, возраст,
    беременность, значения — после нормализации единиц) пропускаются. Повторы считаются как мультимножество:
    в новый кабинет выгрузка переносится целиком, с одинаковыми анализами одного дня."""
    app, _ = _app()
    c = _client(app)
    token, _ = _register(c, "dedup_user")
    _add_record(c, token, "2026-01-10")
    _add_record(c, token, "2026-01-10")                                   # тот же анализ дважды в один день
    g_dl = {"sex": "F", "age_years": 34, "values": {"hemoglobin": {"value": 11.2, "unit": "g/dL"}, "ferritin": 9}}
    _add_record(c, token, "2026-06-10", g_dl)
    exported = c.get("/api/v1/me/export?format=json", headers=_bearer(token)).json()
    assert len(exported["records"]) == 3
    r = c.post("/api/v1/me/import", json=exported, headers=_bearer(token))
    assert r.status_code == 200 and r.json() == {"imported": 0, "skipped": 3}, r.text
    assert len(c.get("/api/v1/me/records", headers=_bearer(token)).json()["records"]) == 3
    # тот же анализ в других единицах (112 г/л = 11,2 г/дл) — дубль; другая дата, возраст или значение — новый
    variants = [{"date": "2026-06-10", "input": {**g_dl, "values": {"hemoglobin": 112, "ferritin": 9}}},
                {"date": "2026-06-11", "input": g_dl},
                {"date": "2026-06-10", "input": {**g_dl, "age_years": 35}},
                {"date": "2026-06-10", "input": {**g_dl, "values": {"hemoglobin": 113, "ferritin": 9}}},
                {"date": "2026-06-10", "input": {**g_dl, "pregnancy": {"status": "yes"}}}]
    r = c.post("/api/v1/me/import", json={"records": variants}, headers=_bearer(token))
    assert r.status_code == 200 and r.json() == {"imported": 4, "skipped": 1}, r.text
    # в пустой кабинет — всё, включая два одинаковых анализа одного дня
    other, _ = _register(_client(app), "dedup_other")
    r = c.post("/api/v1/me/import", json=exported, headers=_bearer(other))
    assert r.json() == {"imported": 3, "skipped": 0}, r.text
    # в файле три одинаковых, в кабинете их два — загружается один
    triple = {"records": [exported["records"][0]] * 3}
    assert c.post("/api/v1/me/import", json=triple, headers=_bearer(other)).json() == {"imported": 1, "skipped": 2}
    # предел 500 считается по новым записям: файл из одних дублей при полном кабинете — не ошибка
    assert c.post("/api/v1/me/import", json=exported, headers=_bearer(token)).json()["imported"] == 0


# --------------------------------------------------------------------------------------------
# История (модуль history — поток П4)
# --------------------------------------------------------------------------------------------
def test_history_calls_history_module_with_contract_records():
    app, _ = _app()
    c = _client(app)
    token, _ = _register(c, "hist_user")
    _add_record(c, token, "2026-06-01")
    first = _add_record(c, token, "2025-12-01")["record"]["id"]
    with _history_stub() as calls:
        r = c.get("/api/v1/me/history", headers=_bearer(token))
    assert r.status_code == 200, r.text
    assert r.json() == {"role": "patient", "n_records": 2, "stub": True}
    records = calls[0]["records"]
    assert [x["date"] for x in records] == ["2025-12-01", "2026-06-01"] and records[0]["id"] == first
    for x in records:
        assert set(x) == {"id", "date", "created", "input", "result"}
        assert x["created"].endswith("Z"), "время создания записи — для порядка анализов одного дня"
        assert x["input"]["sex"] == "F" and x["result"]["level1"]["anemia"] is True, "кеш ответа движка передаётся"


def test_history_real_module():
    try:
        from deficitlens_core import history  # noqa: F401
    except ImportError:
        helpers.skip("модуль deficitlens_core.history ещё не влит (поток П4)")
    app, _ = _app()
    c = _client(app)
    token, _ = _register(c, "hist_real")
    _add_record(c, token, "2026-01-01")
    _add_record(c, token, "2026-06-01", {**INPUT, "values": {**INPUT["values"], "hemoglobin": 105}})
    r = c.get("/api/v1/me/history", headers=_bearer(token))
    assert r.status_code == 200, r.text
    assert r.json()["role"] == "patient" and r.json()["n_records"] == 2
    assert r.json()["first_date"] == "2026-01-01" and r.json()["last_date"] == "2026-06-01"
    # врач по ссылке — та же история, вид врача
    doctor, _ = _register(_client(app), "hist_doc", role="doctor")
    link = _share(c, token, 7)["link_token"]
    pid = c.post("/api/v1/shares/accept", json={"token": link}, headers=_bearer(doctor)).json()["patient"]["id"]
    r = c.get(f"/api/v1/doctor/patients/{pid}/history", headers=_bearer(doctor))
    assert r.status_code == 200 and r.json()["role"] == "doctor" and r.json()["n_records"] == 2


# --------------------------------------------------------------------------------------------
# Доступ врача по ссылке
# --------------------------------------------------------------------------------------------
def test_share_link_flow():
    app, _ = _app()
    c = _client(app)
    patient, puser = _register(c, "share_patient")
    doctor, _ = _register(_client(app), "share_doc", role="doctor", clinic="Клиника «Тест»")
    stranger, _ = _register(_client(app), "other_doc", role="doctor")
    rid = _add_record(c, patient, "2026-03-01")["record"]["id"]
    pid = puser["id"]
    # врач без связи — 403 (и на несуществующего пациента тоже 403, без подсказки)
    assert c.get(f"/api/v1/doctor/patients/{pid}/history", headers=_bearer(doctor)).status_code == 403
    assert c.get(f"/api/v1/doctor/patients/{'0' * 32}/history", headers=_bearer(doctor)).status_code == 403
    for days in (0, 2, "7", True, None):
        r = c.post("/api/v1/me/shares", json={"days": days, "password": PASSWORD}, headers=_bearer(patient))
        assert r.status_code == 422 and r.json()["error"]["field"] == "days", days
    r = c.post("/api/v1/me/shares", json={"days": 7, "password": PASSWORD}, headers=_bearer(patient))
    assert r.status_code == 200, r.text
    link, share = r.json()["link_token"], r.json()["share"]
    assert share["status"] == "pending" and share["doctor"] is None and r.json()["link"] == f"/share#{link}"
    # пациент принять ссылку не может
    assert c.post("/api/v1/shares/accept", json={"token": link}, headers=_bearer(patient)).status_code == 403
    r = c.post("/api/v1/shares/accept", json={"token": link}, headers=_bearer(doctor))
    assert r.status_code == 200, r.text
    assert r.json()["patient"] == {"id": pid, "login": "share_patient"} and r.json()["expires"]
    # токен одноразовый: ни этот, ни другой врач не примут его повторно
    for who in (doctor, stranger):
        r = c.post("/api/v1/shares/accept", json={"token": link}, headers=_bearer(who))
        assert r.status_code == 404 and r.json()["error"]["code"] == "share_invalid"
    assert c.get(f"/api/v1/doctor/patients/{pid}/history", headers=_bearer(stranger)).status_code == 403
    patients = c.get("/api/v1/doctor/patients", headers=_bearer(doctor)).json()["patients"]
    assert len(patients) == 1 and patients[0]["id"] == pid and patients[0]["n_records"] == 1
    assert patients[0]["last_date"] == "2026-03-01" and patients[0]["last_headline"]
    with _history_stub() as calls:
        r = c.get(f"/api/v1/doctor/patients/{pid}/history", headers=_bearer(doctor))
    assert r.status_code == 200 and calls[0]["role"] == "doctor" and len(calls[0]["records"]) == 1
    r = c.get(f"/api/v1/doctor/patients/{pid}/records/{rid}", headers=_bearer(doctor))
    assert r.status_code == 200 and r.json()["record"]["input"]["values"]["hemoglobin"] == 118.0
    assert r.json()["record"]["headline"] == r.json()["result"]["reports"]["doctor"]["headline"]
    assert c.get(f"/api/v1/doctor/patients/{pid}/records/{'f' * 32}", headers=_bearer(doctor)).status_code == 404
    # журнал доступа у пациента: что и когда открывал врач
    items = c.get("/api/v1/me/access-log", headers=_bearer(patient)).json()["items"]
    assert [(i["what"], i["doctor"]["login"], i["doctor"]["clinic"]) for i in items] == [
        ("record", "share_doc", "Клиника «Тест»"), ("history", "share_doc", "Клиника «Тест»")]
    shares = c.get("/api/v1/me/shares", headers=_bearer(patient)).json()["shares"]
    assert shares[0]["status"] == "active" and shares[0]["doctor"] == {"login": "share_doc", "clinic": "Клиника «Тест»"}
    # отзыв — доступ закрыт сразу
    r = c.delete(f"/api/v1/me/shares/{share['id']}", headers=_bearer(patient))
    assert r.status_code == 200 and r.json()["shares"][0]["status"] == "revoked"
    assert c.get(f"/api/v1/doctor/patients/{pid}/history", headers=_bearer(doctor)).status_code == 403
    assert c.get(f"/api/v1/doctor/patients/{pid}/records/{rid}", headers=_bearer(doctor)).status_code == 403
    assert c.get("/api/v1/doctor/patients", headers=_bearer(doctor)).json()["patients"] == []
    assert c.delete(f"/api/v1/me/shares/{share['id']}", headers=_bearer(patient)).status_code == 404
    # отзыв ещё не принятой ссылки — её токен больше не работает
    link2 = _share(c, patient, 30)
    c.delete(f"/api/v1/me/shares/{link2['share']['id']}", headers=_bearer(patient))
    assert c.post("/api/v1/shares/accept", json={"token": link2["link_token"]},
                  headers=_bearer(doctor)).status_code == 404


def test_share_expiry():
    app, _ = _app()
    c = _client(app)
    patient, puser = _register(c, "exp_patient")
    doctor, _ = _register(_client(app), "exp_doc", role="doctor")
    store, real = app.state.store, app.state.store.clock
    try:
        # связь на 1 день: через 2 дня доступа нет, статус «истекла»
        link = _share(c, patient, 1)["link_token"]
        assert c.post("/api/v1/shares/accept", json={"token": link}, headers=_bearer(doctor)).status_code == 200
        with _history_stub():
            assert c.get(f"/api/v1/doctor/patients/{puser['id']}/history", headers=_bearer(doctor)).status_code == 200
            store.clock = lambda: real() + 2 * 86400
            r = c.get(f"/api/v1/doctor/patients/{puser['id']}/history", headers=_bearer(doctor))
            assert r.status_code == 403 and r.json()["error"]["code"] == "forbidden"
        assert c.get("/api/v1/me/shares", headers=_bearer(patient)).json()["shares"][0]["status"] == "expired"
        # непринятая ссылка живёт 48 ч
        store.clock = real
        link = _share(c, patient, 90)["link_token"]
        store.clock = lambda: real() + 49 * 3600
        assert c.post("/api/v1/shares/accept", json={"token": link}, headers=_bearer(doctor)).status_code == 404
    finally:
        store.clock = real


# --------------------------------------------------------------------------------------------
# Полное удаление
# --------------------------------------------------------------------------------------------
def test_delete_account_erases_everything():
    app, data_dir = _app()
    c = _client(app)
    login = "erase-me-QZX9"
    patient, puser = _register(c, login)
    doctor, duser = _register(_client(app), "erase_doc", role="doctor", clinic="Клиника-ERASECLINIC")
    _add_record(c, patient, "2026-01-01")
    _add_record(c, patient, "2026-02-01")
    c.post("/api/v1/me/tokens", json={"name": "ERASETOKENNAME", "password": PASSWORD}, headers=_bearer(patient))
    link = _share(c, patient, 7)["link_token"]
    c.post("/api/v1/shares/accept", json={"token": link}, headers=_bearer(doctor))
    with _history_stub():
        c.get(f"/api/v1/doctor/patients/{puser['id']}/history", headers=_bearer(doctor))
    assert login.encode() in _db_bytes(data_dir)
    # неверный пароль — ничего не удалено
    r = c.request("DELETE", "/api/v1/me", json={"password": "wrong-password"}, headers=CSRF)
    assert r.status_code == 403 and r.json()["error"]["code"] == "wrong_password"
    assert c.get("/api/v1/me").status_code == 200
    r = c.request("DELETE", "/api/v1/me", json={"password": PASSWORD}, headers=CSRF)
    assert r.status_code == 200 and r.json() == {"deleted": True}
    assert 'jm_session=""' in r.headers["set-cookie"]
    assert c.get("/api/v1/me", headers=_bearer(patient)).status_code == 401
    assert _client(app).post("/api/v1/auth/login", json={"login": login, "password": PASSWORD}).status_code == 401
    assert c.get("/api/v1/doctor/patients", headers=_bearer(doctor)).json()["patients"] == []
    db = sqlite3.connect(str(data_dir / "justmedit.db"))
    try:
        for table, column in (("users", "id"), ("sessions", "user_id"), ("records", "user_id"),
                              ("shares", "patient_id"), ("access_log", "patient_id")):
            n = db.execute(f"SELECT COUNT(*) FROM {table} WHERE {column} = ?", (puser["id"],)).fetchone()[0]
            assert n == 0, table
        # без полного VACUUM: удалённое затёрто secure_delete, свободные страницы отданы порциями incremental_vacuum
        assert db.execute("PRAGMA auto_vacuum").fetchone()[0] == 2, "новая база — auto_vacuum=INCREMENTAL"
        assert db.execute("PRAGMA freelist_count").fetchone()[0] == 0, "свободные страницы отданы"
    finally:
        db.close()
    wal = data_dir / "justmedit.db-wal"
    assert not wal.exists() or wal.stat().st_size == 0, "WAL усечён"
    raw = _db_bytes(data_dir)
    for canary in (login.encode(), login.lower().encode(), b"ERASETOKENNAME", puser["id"].encode()):
        assert canary not in raw, canary
    assert b"erase_doc" in raw, "врач остался"
    # удаление врача: его референсы и связи тоже стираются
    c.post("/api/v1/doctor/lab-refs", json={"name": "ERASELABNAME", "ranges": {"ferritin": {"low": 20}}},
           headers=_bearer(doctor))
    r = c.request("DELETE", "/api/v1/me", json={"password": PASSWORD}, headers=_bearer(doctor))
    assert r.status_code == 200
    raw = _db_bytes(data_dir)
    for canary in (b"erase_doc", b"ERASECLINIC", b"ERASELABNAME", duser["id"].encode()):
        assert canary not in raw, canary


def test_doctor_deleted_keeps_login_in_patient_log_and_shares():
    """Врач удалил аккаунт: в журнале просмотров и в связях пациента остаётся снимок логина и клиники на момент
    события (зашифрован, в базе открытым текстом его нет) с пометкой deleted; связь закрыта и видна в «Прошлых»."""
    app, data_dir = _app()
    c = _client(app)
    patient, puser = _register(c, "log_patient")
    rid = _add_record(c, patient, "2026-03-01")["record"]["id"]
    doctor, _ = _register(_client(app), "gone_doc_QX7", role="doctor", clinic="Клиника GONECLINIC")
    keeper, _ = _register(_client(app), "kept_doc", role="doctor")
    for who in (doctor, keeper):
        link = _share(c, patient, 7)["link_token"]
        assert c.post("/api/v1/shares/accept", json={"token": link}, headers=_bearer(who)).status_code == 200
    with _history_stub():
        assert c.get(f"/api/v1/doctor/patients/{puser['id']}/history", headers=_bearer(doctor)).status_code == 200
    assert c.get(f"/api/v1/doctor/patients/{puser['id']}/records/{rid}", headers=_bearer(doctor)).status_code == 200
    assert c.request("DELETE", "/api/v1/me", json={"password": PASSWORD}, headers=_bearer(doctor)).status_code == 200
    log = c.get("/api/v1/me/access-log", headers=_bearer(patient)).json()["items"]
    assert {i["what"] for i in log} == {"history", "record"}
    for item in log:
        assert item["doctor"] == {"login": "gone_doc_QX7", "clinic": "Клиника GONECLINIC", "deleted": True}, item
    shares = {(s["doctor"] or {}).get("login"): s for s in c.get("/api/v1/me/shares", headers=_bearer(patient)).json()["shares"]}
    gone = shares["gone_doc_QX7"]
    assert gone["status"] == "revoked" and gone["accepted"] and gone["doctor"]["deleted"] is True
    assert shares["kept_doc"]["status"] == "active" and "deleted" not in shares["kept_doc"]["doctor"]
    raw = _db_bytes(data_dir)
    for canary in (b"gone_doc_QX7", b"GONECLINIC"):
        assert canary not in raw, canary
    # пациент удаляет себя — его журнал и связи (со снимками) уходят вместе с ним
    assert c.request("DELETE", "/api/v1/me", json={"password": PASSWORD}, headers=_bearer(patient)).status_code == 200
    db = sqlite3.connect(str(data_dir / "justmedit.db"))
    try:
        for table in ("shares", "access_log"):
            assert db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0, table
    finally:
        db.close()


def test_record_source_text():
    """Вставленный текст бланка сохраняется с источником text (подпись «Текст» в ленте)."""
    app, _ = _app()
    c = _client(app)
    token, _ = _register(c, "text_source")
    rec = _add_record(c, token, "2026-03-01", source="text")
    assert rec["record"]["source"] == "text"
    r = c.post("/api/v1/me/records", json={"input": INPUT, "source": "fax"}, headers=_bearer(token))
    assert r.status_code == 422 and r.json()["error"]["field"] == "source"


# --------------------------------------------------------------------------------------------
# Референсы врача
# --------------------------------------------------------------------------------------------
def test_doctor_lab_refs_csv_applied_in_analyze():
    app, _ = _app()
    c = _client(app)
    doctor, _ = _register(c, "lab_doc", role="doctor")
    patient, _ = _register(_client(app), "lab_patient")
    csv_text = ("показатель;нижняя;верхняя;единица\n"
                "Гемоглобин;11,5;15,5;г/дл\n"
                "ферритин;20;;нг/мл\n"
                "LDH;120;250;\n")
    files = {"file": ("Моя лаборатория.csv", io.BytesIO(csv_text.encode("utf-8")), "text/csv")}
    r = c.post("/api/v1/doctor/lab-refs", files=files, data={"default": "1"}, headers=_bearer(doctor))
    assert r.status_code == 200, r.text
    sets = r.json()["sets"]
    assert len(sets) == 1 and sets[0]["name"] == "Моя лаборатория" and sets[0]["default"] is True
    ranges = sets[0]["ranges"]
    assert ranges["hemoglobin"] == {"low": 115.0, "high": 155.0, "unit": "g/L"}, "г/дл пересчитаны в г/л"
    assert ranges["ferritin"]["low"] == 20.0 and ranges["ferritin"]["high"] is None and set(ranges) == {
        "hemoglobin", "ferritin", "LDH"}
    payload = dict(INPUT)
    api = _client(app)
    r = api.post("/api/v1/analyze", json=payload, headers=_bearer(doctor))          # врач, без X-API-Key
    assert r.status_code == 200, r.text
    refs = r.json()["references"]
    assert refs and refs["warning"] and set(refs["local_analytes"]) == {"hemoglobin", "ferritin", "LDH"}
    # набор врача подписан своим именем (главная показывает «ваш набор «…»»), а не «референсы из запроса»
    assert refs["lab_name"] == "doctor_default" and refs["lab_title"] == "Моя лаборатория"
    assert refs["lab_id"] == sets[0]["id"] and "референсы из запроса" not in r.text
    for headers in ({"X-API-Key": KEY}, _bearer(patient)):                           # аноним и пациент — как раньше
        r = api.post("/api/v1/analyze", json=payload, headers=headers)
        assert r.status_code == 200 and not (r.json().get("references") or {}).get("local_analytes"), headers
    # референсы в запросе важнее набора врача
    own = {**payload, "reference_ranges": {"LDH": {"high": 240, "unit": "U/L"}}}
    r = api.post("/api/v1/analyze", json=own, headers=_bearer(doctor))
    assert r.json()["references"]["local_analytes"] == ["LDH"]
    assert r.json()["references"]["lab_name"] == "request" and "lab_title" not in r.json()["references"]
    # страница сайта: /ui/analyze с cookie врача
    r = c.post("/ui/analyze", json=payload)
    assert r.status_code == 200 and "hemoglobin" in r.json()["references"]["local_analytes"]
    assert r.json()["references"]["lab_title"] == "Моя лаборатория"
    # второй набор по умолчанию снимает отметку с первого; удаление
    r = c.post("/api/v1/doctor/lab-refs", json={"name": "Вторая", "default": True,
                                                "ranges": {"hb": {"low": 120, "high": 160, "unit": "g/L"}}},
               headers=_bearer(doctor))
    assert r.status_code == 200, r.text
    sets = r.json()["sets"]
    assert [s["default"] for s in sets] == [False, True] and sets[1]["ranges"]["hemoglobin"]["low"] == 120
    r = api.post("/api/v1/analyze", json=payload, headers=_bearer(doctor))
    assert r.json()["references"]["local_analytes"] == ["hemoglobin"]
    r = c.delete(f"/api/v1/doctor/lab-refs/{sets[1]['id']}", headers=_bearer(doctor))
    assert r.status_code == 200 and [s["name"] for s in r.json()["sets"]] == ["Моя лаборатория"]
    r = api.post("/api/v1/analyze", json=payload, headers=_bearer(doctor))
    assert not (r.json().get("references") or {}).get("local_analytes"), "набора по умолчанию больше нет"
    assert c.get("/api/v1/doctor/lab-refs", headers=_bearer(doctor)).json()["sets"][0]["default"] is False
    # ошибки: неизвестный показатель, единица, нижняя больше верхней, нечисловая граница
    for bad in ({"ranges": {"unobtanium": {"low": 1}}}, {"ranges": {"hemoglobin": {"low": 1, "unit": "кг"}}},
                {"ranges": {"hemoglobin": {"low": 200, "high": 100}}}, {"ranges": {"hemoglobin": {"low": "x"}}},
                {"ranges": {}}, {"ranges": {"hemoglobin": {"low": 100, "extra": 1}}}):
        r = c.post("/api/v1/doctor/lab-refs", json={"name": "Плохой", **bad}, headers=_bearer(doctor))
        assert r.status_code == 422 and r.json()["error"]["field"].startswith("ranges"), (bad, r.text)
    bad_csv = {"file": ("x.csv", io.BytesIO("Гемоглобин;abc;150;г/л\n".encode()), "text/csv")}
    r = c.post("/api/v1/doctor/lab-refs", files=bad_csv, headers=_bearer(doctor))
    assert r.status_code == 422 and r.json()["error"]["field"] == "file"
    bad_csv = {"file": ("x.csv", io.BytesIO("показатель;нижняя;верхняя\nunobtanium;1;2\n".encode()), "text/csv")}
    r = c.post("/api/v1/doctor/lab-refs", files=bad_csv, headers=_bearer(doctor))
    assert r.status_code == 422 and r.json()["error"]["field"] == "file" and "unobtanium" not in r.json()["error"]["field"]


def test_doctor_lab_refs_xlsx():
    import openpyxl
    wb = openpyxl.Workbook()
    ws = wb.active
    for row in (("Показатель", "Нижняя", "Верхняя", "Единица"), ("Ферритин", 15, 150, "мкг/л"), ("MCV", 80, 100, "фл")):
        ws.append(row)
    buf = io.BytesIO()
    wb.save(buf)
    app, _ = _app()
    c = _client(app)
    doctor, _ = _register(c, "xlsx_doc", role="doctor")
    files = {"file": ("lab.xlsx", io.BytesIO(buf.getvalue()),
                      "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")}
    r = c.post("/api/v1/doctor/lab-refs", files=files, data={"name": "Из XLSX"}, headers=_bearer(doctor))
    assert r.status_code == 200, r.text
    s = r.json()["sets"][0]
    assert s["name"] == "Из XLSX" and s["default"] is False
    assert s["ranges"]["ferritin"] == {"low": 15.0, "high": 150.0, "unit": "µg/L"} and s["ranges"]["MCV"]["high"] == 100


# --------------------------------------------------------------------------------------------
# CORS, журнал, страницы
# --------------------------------------------------------------------------------------------
def test_cors_only_for_listed_origins():
    app, _ = _app(cors_origins=("https://clinic.example",))
    c = _client(app)
    pre = {"Access-Control-Request-Method": "POST", "Access-Control-Request-Headers": "authorization,content-type"}
    r = c.options("/api/v1/analyze", headers={"Origin": "https://clinic.example", **pre})
    assert r.status_code == 204, r.text
    assert r.headers["access-control-allow-origin"] == "https://clinic.example"
    assert r.headers["access-control-allow-methods"] == "GET, POST, PATCH, DELETE"
    assert r.headers["access-control-allow-headers"] == "Authorization, Content-Type, X-API-Key"
    assert "access-control-allow-credentials" not in r.headers
    r = c.options("/api/v1/me/records", headers={"Origin": "https://evil.example", **pre})
    assert r.status_code == 403 and "access-control-allow-origin" not in r.headers
    r = c.get("/api/v1/reference", headers={"Origin": "https://clinic.example", "X-API-Key": KEY})
    assert r.status_code == 200 and r.headers["access-control-allow-origin"] == "https://clinic.example"
    assert "access-control-allow-credentials" not in r.headers and "Origin" in r.headers["vary"]
    r = c.get("/api/v1/reference", headers={"Origin": "https://evil.example", "X-API-Key": KEY})
    assert r.status_code == 200 and "access-control-allow-origin" not in r.headers
    r = c.get("/ui/reference", headers={"Origin": "https://clinic.example"})
    assert "access-control-allow-origin" not in r.headers, "только /api/v1/*"
    # без списка — никому
    app, _ = _app()
    r = _client(app).options("/api/v1/analyze", headers={"Origin": "https://clinic.example", **pre})
    assert r.status_code == 403 and "access-control-allow-origin" not in r.headers


class _Capture(logging.Handler):
    def __init__(self):
        super().__init__()
        self.lines: list[str] = []

    def emit(self, record):
        self.lines.append(record.getMessage())


def test_log_has_no_logins_tokens_or_values():
    app, _ = _app()
    c = _client(app)
    capture = _Capture()
    logger = logging.getLogger("deficitlens")
    logger.addHandler(capture)
    try:
        token, user = _register(c, "LOGCANARYUSER", password="LOGCANARYPASS")
        c.post("/api/v1/auth/login", json={"login": "LOGCANARYUSER", "password": "wrong-LOGCANARY"})
        rec = _add_record(c, token, "2026-01-01", {**INPUT, "patient_ref": "LOGCANARYREF",
                                                   "values": {**INPUT["values"], "hemoglobin": 111.222}})
        link = _share(c, token, 7, "LOGCANARYPASS")["link_token"]
        c.get(f"/api/v1/me/records/{rec['record']['id']}", headers=_bearer(token))
    finally:
        logger.removeHandler(capture)
    text = "\n".join(capture.lines)
    assert "POST '/api/v1/auth/register' 201" in text and f"GET '/api/v1/me/records/{rec['record']['id']}' 200" in text
    for canary in ("LOGCANARY", "111.222", token, link, user["id"]):
        assert canary not in text, canary


def test_cabinet_pages_without_inline_code():
    """Страницы кабинета (поток П5; подробнее — tests/api/test_cabinet_pages.py): без встроенных скриптов и стилей."""
    app, _ = _app()
    c = _client(app)
    for path in ("/login", "/cabinet", "/share"):
        r = c.get(path)
        assert r.status_code == 200 and 'lang="ru"' in r.text and "/static/cabinet.js" in r.text, path
        assert not re.search(r"<script(?![^>]*\bsrc=)[^>]*>", r.text) and "<style" not in r.text, path


def test_anonymous_mode_writes_nothing():
    """ADR 0005 для работы без входа: расчёт без кабинета не создаёт ни каталога, ни файла базы."""
    data_dir = Path(tempfile.mkdtemp(prefix="jm-anon-")) / "never"
    app = create_app(Settings(api_keys=(KEY,), data_dir=str(data_dir), ui_rate_per_min=10000))
    c = _client(app)
    assert c.post("/ui/analyze", json=INPUT).status_code == 200
    assert c.post("/api/v1/analyze", json=INPUT, headers={"X-API-Key": KEY}).status_code == 200
    assert c.get("/").status_code == 200 and c.get("/cabinet").status_code == 200
    assert not data_dir.exists(), "без кабинета на диск ничего не пишется"
