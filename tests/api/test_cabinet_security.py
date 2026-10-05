"""Ревизия безопасности кабинета (ADR 0009; docs/security.md — «Личный кабинет», «Модель угроз»): по тесту-
доказательству на каждую находку H1, M1–M3, L1–L9. Каждый тест — своё приложение и свой временный каталог базы;
все логины, пароли и значения придуманы.
"""
from __future__ import annotations

import contextlib
import io
import logging
import os
import re
import sqlite3
import stat
import tempfile
import threading
import time
from pathlib import Path

import helpers

from deficitlens_api import cabinet_api
from deficitlens_api.app import create_app
from deficitlens_api.settings import Settings

KEY = "cabinet-sec-key-1"
PASSWORD = "correct-horse-1"
CSRF = {"X-Justmedit": "1", "Origin": "http://testserver"}
INPUT = {"sex": "F", "age_years": 34,
         "values": {"hemoglobin": 118, "MCV": 78, "MCH": 25.4, "RDW": 15.6, "RBC": 4.4, "ferritin": 11}}
ROOT = Path(__file__).resolve().parents[2]


def _app(**overrides):
    data_dir = tempfile.mkdtemp(prefix="jm-sec-")
    settings = {"api_keys": (KEY,), "data_dir": data_dir, "ui_rate_per_min": 10000, "auth_rate_per_min": 1000,
                "auth_ip_rate_per_min": 1000, "cabinet_rate_per_min": 100000, **overrides}
    return create_app(Settings(**settings)), Path(data_dir)


def _client(app, **kw):
    try:
        from starlette.testclient import TestClient
    except Exception:  # noqa: BLE001 — нет httpx
        helpers.skip("нет httpx: тесты кабинета идут через starlette.testclient (uv sync --extra dev)")
    return TestClient(app, **kw)


def _bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _register(c, login: str, role: str = "patient", password: str = PASSWORD, **extra):
    r = c.post("/api/v1/auth/register", json={"login": login, "password": password, "role": role, "consent": True,
                                               **extra})
    assert r.status_code == 201, r.text
    return r.json()["token"], r.json()["user"]


def _record(c, token: str, inp: dict | None = None, date: str = "2026-03-01"):
    return c.post("/api/v1/me/records", json={"date": date, "input": inp or INPUT}, headers=_bearer(token))


def _files(data_dir: Path) -> bytes:
    return b"".join(p.read_bytes() for p in data_dir.iterdir() if p.name.startswith("justmedit.db"))


def _wal_size(data_dir: Path) -> int:
    wal = data_dir / "justmedit.db-wal"
    return wal.stat().st_size if wal.exists() else 0


@contextlib.contextmanager
def _patched(module, name: str, value):
    saved = getattr(module, name)
    setattr(module, name, value)
    try:
        yield
    finally:
        setattr(module, name, saved)


class _Capture(logging.Handler):
    def __init__(self):
        super().__init__()
        self.lines: list[str] = []

    def emit(self, record):
        self.lines.append(record.getMessage())


# --------------------------------------------------------------------------------------------
# H1. Удаление без VACUUM под общим замком; пределы размера записи, данных пользователя, базы, регистраций
# --------------------------------------------------------------------------------------------
def test_h1_delete_account_without_full_vacuum():
    app, data_dir = _app()
    c = _client(app)
    token, user = _register(c, "vacuum-check-QZX")
    for day in ("2026-01-01", "2026-02-01", "2026-03-01"):
        assert _record(c, token, date=day).status_code == 201
    conn = app.state.store._open()
    statements: list[str] = []
    conn.set_trace_callback(statements.append)
    try:
        r = c.request("DELETE", "/api/v1/me", json={"password": PASSWORD}, headers=CSRF)
    finally:
        conn.set_trace_callback(None)
    assert r.status_code == 200, r.text
    assert not [s for s in statements if s.strip().upper().startswith("VACUUM")], "полного VACUUM нет"
    assert any("incremental_vacuum" in s for s in statements), "свободные страницы — порциями"
    assert any("wal_checkpoint(TRUNCATE)" in s for s in statements)
    assert _wal_size(data_dir) == 0
    raw = _files(data_dir)
    for canary in (b"vacuum-check-QZX", user["id"].encode()):
        assert canary not in raw, canary


def test_h1_record_rules_size_and_patient_ref():
    app, _ = _app()
    c = _client(app)
    token, _ = _register(c, "size_user")
    r = _record(c, token, {**INPUT, "patient_ref": "x" * 65})
    assert r.status_code == 422 and r.json()["error"]["field"] == "patient_ref", r.text
    r = _record(c, token, {**INPUT, "lab_reference": "x" * 65})
    assert r.status_code == 422 and r.json()["error"]["field"] == "lab_reference", r.text
    # provenance не хранится
    r = _record(c, token, {**INPUT, "patient_ref": "ref-1", "provenance": {"source": "PROVCANARY", "deep": {"a": 1}}})
    assert r.status_code == 201, r.text
    rid = r.json()["record"]["id"]
    stored = c.get(f"/api/v1/me/records/{rid}", headers=_bearer(token)).json()["record"]["input"]
    assert "provenance" not in stored and stored["patient_ref"] == "ref-1"
    # предел размера одной записи (здесь уменьшен, чтобы не собирать 16 КБ допустимого входа)
    with _patched(cabinet_api, "RECORD_MAX_BYTES", 100):
        r = _record(c, token)
    assert r.status_code == 413 and r.json()["error"]["code"] == "record_too_large", r.text
    assert cabinet_api.RECORD_MAX_BYTES == 16 * 1024


def test_h1_user_quota_and_database_limit():
    app, _ = _app()
    c = _client(app)
    token, _ = _register(c, "quota_user")
    assert _record(c, token).status_code == 201
    store = app.state.store
    store.user_quota = store.user_bytes(c.get("/api/v1/me", headers=_bearer(token)).json()["id"]) + 10
    r = _record(c, token)
    assert r.status_code == 422 and r.json()["error"]["code"] == "quota_exceeded", r.text
    r = c.post("/api/v1/me/import", json={"records": [{"date": "2026-01-01", "input": INPUT}]}, headers=_bearer(token))
    assert r.status_code == 422 and r.json()["error"]["code"] == "quota_exceeded", r.text
    store.user_quota = 5 * 1024 * 1024
    # база заполнена: новые данные и регистрации — 507; вход, чтение, выгрузка и удаление работают
    rid = _record(c, token).json()["record"]["id"]
    store.max_bytes = store.used_bytes()
    r = _record(c, token)
    assert r.status_code == 507 and r.json()["error"]["code"] == "storage_full", r.text
    r = c.post("/api/v1/auth/register", json={"login": "late_user", "password": PASSWORD, "role": "patient",
                                               "consent": True})
    assert r.status_code == 507, r.text
    assert c.post("/api/v1/auth/login", json={"login": "quota_user", "password": PASSWORD}).status_code == 200
    assert c.get("/api/v1/me/records", headers=_bearer(token)).status_code == 200
    assert c.get("/api/v1/me/export", headers=_bearer(token)).status_code == 200
    assert c.delete(f"/api/v1/me/records/{rid}", headers=_bearer(token)).status_code == 200


def test_h1_signup_limits_per_address_and_per_day():
    app, _ = _app(signup_ip_per_hour=2)
    c = _client(app)
    _register(c, "first_one")
    r = c.post("/api/v1/auth/register", json={"login": "FIRST_ONE", "password": PASSWORD, "role": "patient",
                                               "consent": True})
    assert r.status_code == 409, "занятый логин — не в счёт лимита"
    _register(c, "second_one")
    r = c.post("/api/v1/auth/register", json={"login": "third_one", "password": PASSWORD, "role": "patient",
                                               "consent": True})
    assert r.status_code == 429 and r.json()["error"]["code"] == "rate_limited" and int(r.headers["retry-after"]) > 60
    # IPv6: одна сеть /64 — один «адрес»
    app, _ = _app(signup_ip_per_hour=1)
    _register(_client(app, client=("2001:db8::1", 1000)), "v6_first")
    r = _client(app, client=("2001:db8::2", 1000)).post("/api/v1/auth/register", json={
        "login": "v6_second", "password": PASSWORD, "role": "patient", "consent": True})
    assert r.status_code == 429, r.text
    _register(_client(app, client=("2001:db8:0:1::1", 1000)), "v6_other_net")
    # общий суточный предел (по базе — переживает перезапуск)
    app, _ = _app(max_signups_per_day=2)
    c = _client(app)
    _register(c, "day_one")
    _register(c, "day_two")
    body = {"login": "day_three", "password": PASSWORD, "role": "patient", "consent": True}
    r = c.post("/api/v1/auth/register", json=body)
    assert r.status_code == 503 and r.json()["error"]["code"] == "signups_paused" and r.headers["retry-after"]
    store, real = app.state.store, app.state.store.clock
    try:
        store.clock = lambda: real() + 86401
        assert c.post("/api/v1/auth/register", json=body).status_code == 201
    finally:
        store.clock = real


def test_h1_settings_from_env():
    saved = {k: os.environ.get(k) for k in ("DL_DATA_MAX_MB", "DL_MAX_SIGNUPS_PER_DAY", "DL_TRUSTED_PROXIES",
                                            "DL_DATA_KEY_FILE")}
    try:
        os.environ.update({"DL_DATA_MAX_MB": "64", "DL_MAX_SIGNUPS_PER_DAY": "7",
                           "DL_TRUSTED_PROXIES": "127.0.0.1, 10.0.0.0/8, ::1", "DL_DATA_KEY_FILE": "/run/secrets/k"})
        s = Settings.from_env()
        assert (s.data_max_mb, s.max_signups_per_day, s.data_key_file) == (64, 7, "/run/secrets/k")
        assert s.trusted_proxies == ("127.0.0.1", "10.0.0.0/8", "::1")
        os.environ["DL_TRUSTED_PROXIES"] = "10.0.0.0/8, не-адрес"
        try:
            Settings.from_env()
            raise AssertionError("опечатка в DL_TRUSTED_PROXIES должна останавливать запуск")
        except ValueError as e:
            assert "DL_TRUSTED_PROXIES" in str(e)
        for k in saved:
            os.environ.pop(k, None)
        s = Settings.from_env()
        assert s.data_max_mb == 512 and s.max_signups_per_day == 1000 and s.trusted_proxies == ()
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


# --------------------------------------------------------------------------------------------
# M1. X-Forwarded-For — только от доверенного прокси; общий счётчик неудач входа; IPv6 /64
# --------------------------------------------------------------------------------------------
def test_m1_forwarded_for_is_ignored_by_default():
    # лимит на адрес — у расчётов страницы (POST /ui/*); справочник GET /ui/reference без лимита
    app, _ = _app(ui_rate_per_min=2)
    c = _client(app)
    codes = [c.post("/ui/analyze", json=INPUT, headers={"X-Forwarded-For": f"198.51.100.{i}"}).status_code
             for i in range(3)]
    assert codes == [200, 200, 429], codes


def test_m1_forwarded_for_from_trusted_proxy():
    app, _ = _app(ui_rate_per_min=1, trusted_proxies=("10.0.0.0/8",))
    proxy = _client(app, client=("10.0.0.5", 4000))

    def get(xff: str) -> int:
        return proxy.post("/ui/analyze", json=INPUT, headers={"X-Forwarded-For": xff}).status_code

    assert get("198.51.100.1") == 200
    assert get("198.51.100.2") == 200, "разные клиенты за прокси — разные окна"
    assert get("198.51.100.1") == 429
    assert get("203.0.113.77, 198.51.100.2") == 429, "левую запись прислал клиент — берётся правая не-прокси"
    assert get("198.51.100.2, 10.0.0.7") == 429, "адреса доверенных прокси справа пропускаются"
    # соединение не от доверенного прокси — заголовок не учитывается
    direct = _client(app, client=("203.0.113.9", 4000))
    assert direct.post("/ui/analyze", json=INPUT, headers={"X-Forwarded-For": "192.0.2.1"}).status_code == 200
    assert direct.post("/ui/analyze", json=INPUT, headers={"X-Forwarded-For": "192.0.2.2"}).status_code == 429


def test_m1_uvicorn_does_not_parse_proxy_headers():
    """Uvicorn по умолчанию доверяет X-Forwarded-For от 127.0.0.1 — во всех запусках он выключен."""
    for name in ("Dockerfile", "Makefile", "scripts/verify.sh"):
        path = ROOT / name
        if not path.exists():                     # образ verify: Dockerfile и Makefile в него не копируются
            continue
        launches = [ln for ln in path.read_text(encoding="utf-8").splitlines()
                    if "uvicorn" in ln and "deficitlens_api.app:app" in ln and not ln.lstrip().startswith("#")]
        assert launches, name
        assert all("no-proxy-headers" in ln for ln in launches), (name, launches)
    source = (ROOT / "src" / "deficitlens_api" / "app.py").read_text(encoding="utf-8")
    assert "proxy_headers=False" in source


def test_m1_login_failures_counted_per_login_from_any_address():
    app, _ = _app()
    _register(_client(app), "guarded_user")
    for i in range(10):
        r = _client(app, client=(f"198.51.100.{i + 1}", 5000)).post(
            "/api/v1/auth/login", json={"login": "guarded_user", "password": "wrong-password"})
        assert r.status_code == 401, (i, r.text)
    r = _client(app, client=("192.0.2.200", 5000)).post("/api/v1/auth/login",
                                                         json={"login": "Guarded_User", "password": PASSWORD})
    assert r.status_code == 429 and 1 <= int(r.headers["retry-after"]) <= 60, r.text
    # несуществующий логин — то же поведение (по ответу не узнать, есть ли логин)
    for i in range(10):
        _client(app, client=(f"198.51.100.{i + 1}", 5000)).post(
            "/api/v1/auth/login", json={"login": "ghost_user", "password": "wrong-password"})
    r = _client(app).post("/api/v1/auth/login", json={"login": "ghost_user", "password": "wrong-password"})
    assert r.status_code == 429
    # другой логин не затронут
    _register(_client(app), "other_user")
    r = _client(app).post("/api/v1/auth/login", json={"login": "other_user", "password": PASSWORD})
    assert r.status_code == 200


def test_m1_login_guard_backoff_is_bounded():
    from deficitlens_api.security import LoginGuard

    g = LoginGuard()
    t = 1000.0
    pauses = []
    for _ in range(6):
        for _ in range(10):
            assert g.check("k", t) is None
            g.failure("k", t)
        pauses.append(g.check("k", t))
        t += pauses[-1]                                      # пауза прошла — сразу новая серия неудач
    assert pauses == [60, 120, 240, 480, 900, 900], pauses   # вдвое длиннее, не больше 15 мин
    assert g.check("k", t) is None, "навсегда не блокирует"
    t += 16 * 60                                             # 15 мин без неудач — ступень сначала
    for _ in range(10):
        g.failure("k", t)
    assert g.check("k", t) == 60
    g.success("other")
    assert g.check("other", t) is None


def test_m1_ipv6_rate_limit_key_is_slash_64():
    from deficitlens_api.security import client_key

    assert client_key("2001:db8:1:2:aaaa::1") == client_key("2001:db8:1:2:ffff::9") == "2001:db8:1:2::/64"
    assert client_key("2001:db8:1:3::1") != client_key("2001:db8:1:2::1")
    assert client_key("::ffff:192.0.2.5") == "192.0.2.5" and client_key("192.0.2.5") == "192.0.2.5"
    assert client_key(None) == "unknown" and client_key("testclient") == "testclient"


# --------------------------------------------------------------------------------------------
# M2. Область токенов API: пароль для новых токенов и ссылок, сроки, предел, выйти везде, смена пароля
# --------------------------------------------------------------------------------------------
def test_m2_bearer_needs_password_for_tokens_and_shares():
    app, _ = _app()
    c = _client(app)
    token, _ = _register(c, "scope_user")
    api = _client(app)                                       # без cookie: только Bearer
    for path, body in (("/api/v1/me/tokens", {"name": "x"}), ("/api/v1/me/shares", {"days": 7}),
                       ("/api/v1/me/logout-all", {})):
        r = api.post(path, json=body, headers=_bearer(token))
        assert r.status_code == 403 and r.json()["error"]["code"] == "password_required", (path, r.text)
        r = api.post(path, json={**body, "password": "wrong-password"}, headers=_bearer(token))
        assert r.status_code == 403 and r.json()["error"]["code"] == "wrong_password", (path, r.text)
    # с паролем — можно; токен, созданный по токену, живёт не дольше него
    r = api.post("/api/v1/me/tokens", json={"name": "child", "password": PASSWORD}, headers=_bearer(token))
    assert r.status_code == 200, r.text
    tokens = {t["name"]: t for t in api.get("/api/v1/me/tokens", headers=_bearer(token)).json()["tokens"]}
    assert tokens["child"]["expires"] <= tokens["Вход"]["expires"]
    assert api.post("/api/v1/me/shares", json={"days": 7, "password": PASSWORD},
                    headers=_bearer(token)).status_code == 200
    # сессия сайта (cookie) — без пароля, как раньше; токен по cookie — на 30 дней
    r = c.post("/api/v1/me/tokens", json={"name": "site"}, headers=CSRF)
    assert r.status_code == 200 and r.json()["token"]["expires"] > tokens["Вход"]["expires"], r.text
    assert c.post("/api/v1/me/shares", json={"days": 7}, headers=CSRF).status_code == 200
    # чтение и запись анализов по Bearer — без пароля
    assert _record(api, token).status_code == 201
    assert api.get("/api/v1/me/records", headers=_bearer(token)).status_code == 200
    assert api.post("/api/v1/analyze", json=INPUT, headers=_bearer(token)).status_code == 200


def test_m2_named_tokens_limit_and_login_tokens_separate():
    app, _ = _app()
    c = _client(app)
    first_login, _ = _register(c, "many_tokens")
    named = []
    for i in range(20):
        r = c.post("/api/v1/me/tokens", json={"name": f"t{i}"}, headers=CSRF)
        assert r.status_code == 200, (i, r.text)
        named.append(r.json()["value"])
    r = c.post("/api/v1/me/tokens", json={"name": "t20"}, headers=CSRF)
    assert r.status_code == 422 and r.json()["error"]["code"] == "too_many_tokens", r.text
    # много входов: старые входы вытесняются, именованные токены — нет
    other = _client(app)
    for _ in range(21):
        assert other.post("/api/v1/auth/login", json={"login": "many_tokens", "password": PASSWORD}).status_code == 200
    assert other.get("/api/v1/me", headers=_bearer(first_login)).status_code == 401, "самый старый вход вытеснен"
    assert all(other.get("/api/v1/me", headers=_bearer(v)).status_code == 200 for v in (named[0], named[-1]))
    listing = other.get("/api/v1/me/tokens", headers=_bearer(named[0])).json()["tokens"]
    assert sum(1 for t in listing if t["name"] != "Вход") == 20
    assert sum(1 for t in listing if t["name"] == "Вход") == 20


def test_m2_login_token_dies_with_its_cookie():
    """Токен «Вход» без живой cookie своего входа (так жили токены прежних версий — 30 дней) не принимается."""
    app, _ = _app()
    c = _client(app)
    token, _ = _register(c, "orphan_user")
    named = c.post("/api/v1/me/tokens", json={"name": "n"}, headers=CSRF).json()["value"]
    store = app.state.store
    with store.tx() as conn:
        conn.execute("DELETE FROM sessions WHERE kind = 'cookie'")        # cookie входа исчезла (истекла, вытеснена)
    assert c.get("/api/v1/me", headers=_bearer(token)).status_code == 401
    names = [t["name"] for t in c.get("/api/v1/me/tokens", headers=_bearer(named)).json()["tokens"]]
    assert names == ["n"], names


def test_m2_logout_all():
    app, _ = _app()
    c1 = _client(app)
    t1, _ = _register(c1, "logout_all_user")
    c2 = _client(app)
    t2 = c2.post("/api/v1/auth/login", json={"login": "logout_all_user", "password": PASSWORD}).json()["token"]
    named = c1.post("/api/v1/me/tokens", json={"name": "n"}, headers=CSRF).json()["value"]
    r = c1.post("/api/v1/me/logout-all", headers=CSRF)
    assert r.status_code == 200 and r.json() == {"ok": True, "revoked": 3}, r.text
    assert c1.get("/api/v1/me").status_code == 200 and c1.get("/api/v1/me", headers=_bearer(t1)).status_code == 200
    for who, headers in ((c2, {}), (c2, _bearer(t2)), (c1, _bearer(named))):
        assert who.get("/api/v1/me", headers=headers).status_code == 401
    # all: true — и текущий вход
    r = c1.post("/api/v1/me/logout-all", json={"all": True, "password": PASSWORD}, headers=_bearer(t1))
    assert r.status_code == 200 and r.json()["revoked"] == 2, r.text
    assert c1.get("/api/v1/me").status_code == 401 and c1.get("/api/v1/me", headers=_bearer(t1)).status_code == 401


def test_m2_change_password_revokes_other_sessions():
    app, _ = _app()
    c1 = _client(app)
    t1, _ = _register(c1, "pw_user")
    c2 = _client(app)
    t2 = c2.post("/api/v1/auth/login", json={"login": "pw_user", "password": PASSWORD}).json()["token"]
    named = c1.post("/api/v1/me/tokens", json={"name": "n"}, headers=CSRF).json()["value"]
    r = c1.post("/api/v1/me/password", json={"old": "wrong-password", "new": "new-password-1"}, headers=CSRF)
    assert r.status_code == 403 and r.json()["error"] == {"code": "wrong_password", "field": "old",
                                                          "message": "Неверный текущий пароль: пароль не изменён."}
    r = c1.post("/api/v1/me/password", json={"old": PASSWORD, "new": "short"}, headers=CSRF)
    assert r.status_code == 422 and r.json()["error"]["field"] == "new"
    r = c1.post("/api/v1/me/password", json={"old": PASSWORD, "new": "new-password-1"}, headers=CSRF)
    assert r.status_code == 200 and r.json() == {"ok": True, "revoked": 3}, r.text
    assert c1.get("/api/v1/me").status_code == 200 and c1.get("/api/v1/me", headers=_bearer(t1)).status_code == 200
    for who, headers in ((c2, {}), (c2, _bearer(t2)), (c1, _bearer(named))):
        assert who.get("/api/v1/me", headers=headers).status_code == 401
    fresh = _client(app)
    assert fresh.post("/api/v1/auth/login", json={"login": "pw_user", "password": PASSWORD}).status_code == 401
    assert fresh.post("/api/v1/auth/login", json={"login": "pw_user", "password": "new-password-1"}).status_code == 200


# --------------------------------------------------------------------------------------------
# M3. WAL после удалений; ключ из файла DL_DATA_KEY_FILE
# --------------------------------------------------------------------------------------------
def test_m3_deleted_rows_do_not_stay_in_wal():
    app, data_dir = _app()
    c = _client(app)
    token, _ = _register(c, "wal_user")
    doctor, _ = _register(_client(app), "wal_doc", role="doctor")
    rid = _record(c, token).json()["record"]["id"]
    db = sqlite3.connect(str(data_dir / "justmedit.db"))
    try:
        blob = bytes(db.execute("SELECT enc_payload FROM records WHERE id = ?", (rid,)).fetchone()[0])
    finally:
        db.close()
    assert blob[40:104] in _files(data_dir)
    assert c.delete(f"/api/v1/me/records/{rid}", headers=_bearer(token)).status_code == 200
    assert _wal_size(data_dir) == 0, "WAL усечён после удаления записи"
    assert blob[40:104] not in _files(data_dir), "шифртекста удалённой записи нет ни в базе, ни в WAL"
    share = c.post("/api/v1/me/shares", json={"days": 7}, headers=CSRF).json()["share"]["id"]
    assert c.delete(f"/api/v1/me/shares/{share}", headers=CSRF).status_code == 200
    assert _wal_size(data_dir) == 0, "WAL усечён после отзыва ссылки"
    r = c.post("/api/v1/doctor/lab-refs", json={"name": "Лаб", "ranges": {"ferritin": {"low": 20}}},
               headers=_bearer(doctor))
    lid = r.json()["sets"][0]["id"]
    assert c.delete(f"/api/v1/doctor/lab-refs/{lid}", headers=_bearer(doctor)).status_code == 200
    assert _wal_size(data_dir) == 0, "WAL усечён после удаления референсов"


def test_m3_data_key_file():
    from cryptography.fernet import Fernet

    secrets_dir = Path(tempfile.mkdtemp(prefix="jm-secret-"))
    key_file = secrets_dir / "data.key"
    key_file.write_text(Fernet.generate_key().decode() + "\n", encoding="ascii")
    key_file.chmod(0o600)
    app, data_dir = _app(data_key_file=str(key_file))
    c = _client(app)
    token, _ = _register(c, "key_file_user")
    assert _record(c, token).status_code == 201
    assert not (data_dir / "key").exists(), "ключ из файла: демо-ключ рядом с базой не создаётся"
    app.state.store.close()
    # файл ключа доступен группе или всем — отказ (ключ уже не секрет)
    key_file.chmod(0o644)
    app2 = create_app(Settings(api_keys=(KEY,), data_dir=str(data_dir), data_key_file=str(key_file)))
    r = _client(app2).get("/api/v1/me", headers=_bearer(token))
    assert r.status_code == 503 and r.json()["error"]["code"] == "storage_unavailable"
    assert "0600" in r.json()["error"]["message"]
    key_file.chmod(0o600)
    app3 = create_app(Settings(api_keys=(KEY,), data_dir=str(data_dir), data_key_file=str(key_file)))
    assert _client(app3).get("/api/v1/me/records", headers=_bearer(token)).status_code == 200
    app3.state.store.close()
    # и ключ, и файл — неоднозначно: отказ
    app4 = create_app(Settings(api_keys=(KEY,), data_dir=str(data_dir), data_key_file=str(key_file),
                               data_key=Fernet.generate_key().decode()))
    assert _client(app4).get("/api/v1/me", headers=_bearer(token)).status_code == 503
    # файл ключа в каталоге базы — работает, но с предупреждением в журнале (без самого ключа)
    inside = Path(tempfile.mkdtemp(prefix="jm-sec-inside-"))
    (inside / "my.key").write_text(Fernet.generate_key().decode(), encoding="ascii")
    (inside / "my.key").chmod(0o600)
    capture = _Capture()
    logger = logging.getLogger("deficitlens")
    logger.addHandler(capture)
    try:
        app5 = create_app(Settings(api_keys=(KEY,), data_dir=str(inside), data_key_file=str(inside / "my.key")))
        _register(_client(app5), "inside_key")
    finally:
        logger.removeHandler(capture)
    assert any("outside the database volume" in ln for ln in capture.lines), capture.lines
    assert stat.S_IMODE((inside / "justmedit.db").stat().st_mode) == 0o600


# --------------------------------------------------------------------------------------------
# L1. Загрузка: сначала проверка всех записей, лимит тяжёлых запросов; история — под слотом расчётов
# --------------------------------------------------------------------------------------------
def test_l1_import_checks_everything_before_computing():
    app, _ = _app()
    c = _client(app)
    token, _ = _register(c, "import_user")
    calls = []
    real = cabinet_api._compute

    def counting(*a, **kw):
        calls.append(1)
        return real(*a, **kw)

    bad_sets = ([{"date": "2026-01-01", "input": INPUT}, {"date": "2026-01-02", "input": {**INPUT, "sex": "X"}}],
                [{"date": "2026-01-01", "input": INPUT},
                 {"date": "2026-01-02", "input": {**INPUT, "values": {"=HYPERLINK(\"x\")": 1}}}])
    with _patched(cabinet_api, "_compute", counting):
        for records, field in zip(bad_sets, ("records[1].sex", "records[1].values")):
            r = c.post("/api/v1/me/import", json={"records": records}, headers=_bearer(token))
            assert r.status_code == 422 and r.json()["error"]["field"] == field, r.text
        assert calls == [], "ошибка в записи найдена до расчёта движком"
        r = c.post("/api/v1/me/import", json={"records": bad_sets[0][:1]}, headers=_bearer(token))
        assert r.status_code == 200 and calls == [1]


def test_l1_import_is_a_heavy_request():
    app, _ = _app(ui_rate_per_min=1)
    c = _client(app)
    token, _ = _register(c, "heavy_import")
    body = {"records": [{"date": "2026-01-01", "input": INPUT}]}
    assert c.post("/api/v1/me/import", json=body, headers=_bearer(token)).status_code == 200
    r = c.post("/api/v1/me/import", json=body, headers=_bearer(token))
    assert r.status_code == 429 and r.json()["error"]["code"] == "rate_limited", r.text


def test_l1_history_slot_only_for_stale_cache():
    """История по свежему кешу ответов движка слота тяжёлых расчётов не занимает: оба слота заняты — всё равно 200.
    Кеш устарел (сменилась версия движка) — пересчёт под слотом: история ждёт слот до HISTORY_SLOT_WAIT секунд,
    освободился — 200, не дождалась — 503 service_busy с Retry-After."""
    app, _ = _app()
    c = _client(app)
    token, puser = _register(c, "gate_user")
    _record(c, token, INPUT)
    doctor, _ = _register(_client(app), "gate_doc", role="doctor")
    link = c.post("/api/v1/me/shares", json={"days": 7}, headers=CSRF).json()["link_token"]
    assert c.post("/api/v1/shares/accept", json={"token": link}, headers=_bearer(doctor)).status_code == 200
    paths = (("/api/v1/me/history", token), (f"/api/v1/doctor/patients/{puser['id']}/history", doctor))
    store, gate = app.state.store, app.state.heavy_gate

    def make_stale():
        with store.tx() as conn:
            conn.execute("UPDATE records SET engine_version = 'old'")

    held, saved = 0, cabinet_api.HISTORY_SLOT_WAIT
    try:
        while gate._sem.acquire(blocking=False):
            held += 1
        for path, who in paths:                                           # кеш свежий — слот не нужен
            r = c.get(path, headers=_bearer(who))
            assert r.status_code == 200 and r.json()["n_records"] == 1, (path, r.text)
        cabinet_api.HISTORY_SLOT_WAIT = 0.3
        for path, who in paths:                                           # кеш устарел, слоты заняты — 503
            make_stale()
            t0 = time.monotonic()
            r = c.get(path, headers=_bearer(who))
            assert r.status_code == 503 and r.json()["error"]["code"] == "service_busy", (path, r.text)
            assert r.headers["retry-after"] and time.monotonic() - t0 >= 0.25, "сначала ждёт слот"
        cabinet_api.HISTORY_SLOT_WAIT = 3.0
        make_stale()
        timer = threading.Timer(0.4, gate._sem.release)                  # слот освободится во время ожидания
        timer.start()
        r = c.get("/api/v1/me/history", headers=_bearer(token))
        timer.join()
        held -= 1
        assert r.status_code == 200 and r.json()["n_records"] == 1, r.text
        assert store.all_records(puser["id"])[0]["version"] != "old", "кеш пересчитан"
    finally:
        cabinet_api.HISTORY_SLOT_WAIT = saved
        for _ in range(held):
            gate._sem.release()


# --------------------------------------------------------------------------------------------
# L2. Глубокая вложенность JSON — 422, а не 500
# --------------------------------------------------------------------------------------------
def test_l2_deeply_nested_json_is_422():
    app, _ = _app()
    c = _client(app)
    token, _ = _register(c, "deep_user")
    very_deep = b"[" * 100000 + b"]" * 100000             # разборщик падает с RecursionError
    deep = b'{"a":' * 40 + b"1" + b"}" * 40                # разбирается, но глубже предела
    for body in (very_deep, deep):
        for path, headers in (("/api/v1/me/records", _bearer(token)), ("/api/v1/analyze", {"X-API-Key": KEY}),
                              ("/ui/analyze", {})):
            r = c.post(path, content=body, headers={**headers, "Content-Type": "application/json"})
            assert r.status_code == 422 and r.json()["error"]["code"] == "invalid_json", (path, len(body), r.text)
    nested_input = {**INPUT, "provenance": {"x": [[[[[[[[[[1]]]]]]]]]]}}
    assert _record(c, token, nested_input).status_code == 201, "обычная вложенность — как раньше"


# --------------------------------------------------------------------------------------------
# L3. Тексты (клиника, имя набора, имя токена) — одна проверка; показатели — только известные
# --------------------------------------------------------------------------------------------
def test_l3_text_fields_reject_control_and_invisible_characters():
    app, _ = _app()
    c = _client(app)
    base = {"login": "text_doc", "password": PASSWORD, "role": "doctor", "consent": True}
    for clinic in ("Клиника\u202eабв", "Клиника\u200b", "Клиника\x7f", "Клиника\u20282", "к" * 121):
        r = c.post("/api/v1/auth/register", json={**base, "clinic": clinic})
        assert r.status_code == 422 and r.json()["error"]["code"] == "invalid_clinic", (repr(clinic), r.text)
    doctor, user = _register(c, "text_doc", role="doctor", clinic="  Клиника «Тест» №1 ")
    assert user["clinic"] == "Клиника «Тест» №1"
    ranges = {"ferritin": {"low": 20}}
    for name in ("Лаб\u202e", "Лаб\u2028x", "Лаб\x00", "л" * 81):
        r = c.post("/api/v1/doctor/lab-refs", json={"name": name, "ranges": ranges}, headers=_bearer(doctor))
        assert r.status_code == 422 and r.json()["error"]["field"] == "name", (repr(name), r.text)
    csv_body = "ферритин;20;;нг/мл\n".encode()
    files = {"file": ("лаб\u202eвсх.csv", io.BytesIO(csv_body), "text/csv")}
    r = c.post("/api/v1/doctor/lab-refs", files=files, headers=_bearer(doctor))
    assert r.status_code == 422 and r.json()["error"]["field"] == "name", r.text
    files = {"file": ("Моя лаборатория.csv", io.BytesIO(csv_body), "text/csv")}
    r = c.post("/api/v1/doctor/lab-refs", files=files, headers=_bearer(doctor))
    assert r.status_code == 200 and r.json()["sets"][0]["name"] == "Моя лаборатория", r.text
    r = c.post("/api/v1/me/tokens", json={"name": "токен\u202e"}, headers=CSRF)
    assert r.status_code == 422 and r.json()["error"]["field"] == "name"


def test_l3_unknown_analyte_keys_are_rejected():
    app, _ = _app()
    c = _client(app)
    token, user = _register(c, "keys_user")
    for key in ('=HYPERLINK("http://evil.example","x")', "unobtanium", "@SUM(1)"):
        r = _record(c, token, {**INPUT, "values": {**INPUT["values"], key: 1}})
        assert r.status_code == 422 and r.json()["error"] == {
            "code": "unknown_analyte", "field": "values",
            "message": "В записи кабинета — только известные показатели (код или название из справочника "
                       "/api/v1/reference)."}, (key, r.text)
    r = _record(c, token, {**INPUT, "reference_ranges": {"=1+1": {"low": 1}}})
    assert r.status_code == 422 and r.json()["error"]["field"] == "reference_ranges", r.text
    r = _record(c, token, {"sex": "F", "age_years": 34, "values": {"Гемоглобин": 118, "hb": 118, "ferritin": 11}})
    assert r.status_code == 201, "синонимы из справочника принимаются"
    # запись, сохранённая до этого правила, открывается, а её ключ не попадает в заголовок CSV
    item = {"date": "2025-01-01", "source": "import", "result": None, "version": "old",
            "input": {**INPUT, "values": {**INPUT["values"], '=HYPERLINK("x")': 5}},
            "summary": {"headline_doctor": "x", "headline_patient": "y", "anemia": False, "date": "2025-01-01"}}
    (rid,) = app.state.store.add_records(user["id"], [item], 500)
    assert c.get(f"/api/v1/me/records/{rid}", headers=_bearer(token)).status_code == 200
    csv_text = c.get("/api/v1/me/export?format=csv", headers=_bearer(token)).text
    assert "HYPERLINK" not in csv_text


# --------------------------------------------------------------------------------------------
# L4. Логин — без смешения латиницы и кириллицы
# --------------------------------------------------------------------------------------------
def test_l4_login_does_not_mix_latin_and_cyrillic():
    app, _ = _app()
    c = _client(app)
    for login in ("iv\u0430n_1", "Пётр-petr", "m\u0430ria.demo"):     # \u0430 — кириллическая «а»
        r = c.post("/api/v1/auth/register", json={"login": login, "password": PASSWORD, "role": "patient",
                                                   "consent": True})
        assert r.status_code == 422 and r.json()["error"]["code"] == "invalid_login", (login, r.text)
    _register(c, "иван.петров-1")
    _register(_client(app), "ivan.petrov-1")
    # логин, заведённый до правила, по-прежнему входит
    from deficitlens_api.auth import hash_password, login_key
    app.state.store.create_user("oldmix\u0435d", login_key("oldmix\u0435d"), "patient", hash_password(PASSWORD), None)
    assert c.post("/api/v1/auth/login", json={"login": "oldmix\u0435d", "password": PASSWORD}).status_code == 200


# --------------------------------------------------------------------------------------------
# L5. Список пациентов врача — без значений; журнал доступа — повторы за час одной строкой
# --------------------------------------------------------------------------------------------
def test_l5_patient_list_without_values_and_access_log_merging():
    app, _ = _app()
    c = _client(app)
    token, puser = _register(c, "log_patient")
    doctor, _ = _register(_client(app), "log_doc", role="doctor")
    rid_a = _record(c, token, date="2026-01-01").json()["record"]["id"]
    rid_b = _record(c, token, date="2026-02-01").json()["record"]["id"]
    link = c.post("/api/v1/me/shares", json={"days": 7}, headers=CSRF).json()["link_token"]
    assert c.post("/api/v1/shares/accept", json={"token": link}, headers=_bearer(doctor)).status_code == 200
    patients = c.get("/api/v1/doctor/patients", headers=_bearer(doctor)).json()["patients"]
    headline = patients[0]["last_headline"]
    assert headline == "Последний анализ: есть признаки анемии." and not re.search(r"\d", headline), headline
    assert c.get("/api/v1/me/access-log", headers=_bearer(token)).json()["items"] == [], "список в журнал не пишется"
    base = f"/api/v1/doctor/patients/{puser['id']}"
    for _ in range(3):
        assert c.get(f"{base}/records/{rid_a}", headers=_bearer(doctor)).status_code == 200
    items = c.get("/api/v1/me/access-log", headers=_bearer(token)).json()["items"]
    assert [(i["what"], i["record_id"]) for i in items] == [("record", rid_a)], items
    c.get(f"{base}/records/{rid_b}", headers=_bearer(doctor))
    items = c.get("/api/v1/me/access-log", headers=_bearer(token)).json()["items"]
    assert [(i["what"], i["record_id"]) for i in items] == [("record", None)], "разные записи за час"
    store, real = app.state.store, app.state.store.clock
    try:
        store.clock = lambda: real() + 3601
        c.get(f"{base}/records/{rid_b}", headers=_bearer(doctor))
        items = c.get("/api/v1/me/access-log", headers=_bearer(token)).json()["items"]
        assert [(i["what"], i["record_id"]) for i in items] == [("record", rid_b), ("record", None)]
    finally:
        store.clock = real


# --------------------------------------------------------------------------------------------
# L6. Cookie __Host- за HTTPS
# --------------------------------------------------------------------------------------------
def test_l6_host_prefixed_cookie_over_https():
    app, _ = _app(cookie_secure=True)
    c = _client(app, base_url="https://testserver")
    r = c.post("/api/v1/auth/register", json={"login": "host_cookie", "password": PASSWORD, "role": "patient",
                                               "consent": True})
    cookie = r.headers["set-cookie"]
    assert cookie.startswith("__Host-jm_session=") and "Secure" in cookie and "Path=/" in cookie
    assert "domain=" not in cookie.lower()
    value = cookie.split(";", 1)[0].split("=", 1)[1]
    assert c.get("/api/v1/me").status_code == 200, "cookie __Host- принимается"
    plain = _client(app, base_url="https://testserver", cookies={"jm_session": value})
    assert plain.get("/api/v1/me").status_code == 401, "за HTTPS принимается только cookie с префиксом"
    r = c.post("/api/v1/auth/logout", headers={"X-Justmedit": "1", "Origin": "https://testserver"})
    assert r.status_code == 200 and r.headers["set-cookie"].startswith('__Host-jm_session=""')


# --------------------------------------------------------------------------------------------
# L7. CSRF: схема, хост и порт; X-Forwarded-Host/Proto — только от доверенного прокси
# --------------------------------------------------------------------------------------------
def test_l7_csrf_compares_scheme_host_and_port():
    app, _ = _app()
    c = _client(app)
    _register(c, "origin_user")
    body = {"date": "2026-01-15", "input": INPUT}

    def post(origin: str, client=c, **extra) -> int:
        return client.post("/api/v1/me/records", json=body,
                           headers={"X-Justmedit": "1", "Origin": origin, **extra}).status_code

    assert post("http://testserver") == 201
    assert post("http://testserver:8080") == 403, "другой порт"
    assert post("http://evil.example") == 403
    assert post("http://user@testserver") == 403
    assert post("https://testserver") == 201, "HTTPS-страница своего сайта за прокси, снявшим TLS"
    assert post("https://testserver:8443") == 403
    r = c.post("/api/v1/me/records", json=body, headers={"X-Justmedit": "1", "Referer": "http://testserver:81/x"})
    assert r.status_code == 403
    secure = _client(app, base_url="https://testserver")
    _register(secure, "origin_https")
    assert post("https://testserver", secure) == 201
    assert post("http://testserver", secure) == 403, "запрос по HTTPS, Origin по HTTP — чужой"


def test_l7_forwarded_host_and_proto_only_from_trusted_proxy():
    app, _ = _app(trusted_proxies=("10.0.0.0/8",))
    proxy = _client(app, client=("10.0.0.5", 4000))
    _register(proxy, "proxied_user")
    body = {"date": "2026-01-15", "input": INPUT}
    fwd = {"X-Forwarded-Host": "jm.example", "X-Forwarded-Proto": "https", "X-Forwarded-For": "198.51.100.7"}

    def post(origin: str, client=proxy, headers=fwd) -> int:
        return client.post("/api/v1/me/records", json=body,
                           headers={"X-Justmedit": "1", "Origin": origin, **headers}).status_code

    assert post("https://jm.example") == 201
    assert post("http://jm.example") == 403, "прокси сказал https — Origin http не свой"
    assert post("https://testserver") == 403, "хост — из X-Forwarded-Host"
    # тот же запрос не от доверенного прокси — заголовки X-Forwarded-* не учитываются
    direct = _client(app, client=("203.0.113.9", 4000))
    _register(direct, "direct_user")
    assert post("https://jm.example", direct) == 403
    assert post("http://testserver", direct) == 201


# --------------------------------------------------------------------------------------------
# L9. Лимитер под наплывом адресов не сбрасывает действующие окна
# --------------------------------------------------------------------------------------------
def test_l9_rate_limiter_purge_keeps_active_windows():
    from deficitlens_api.security import RateLimiter

    lim = RateLimiter(1, window=60, max_clients=3)
    assert lim.check("stale", 0.0) is None
    assert lim.check("a", 50.0) is None
    assert lim.check("b", 51.0) is None
    assert lim.check("c", 70.0) is None                  # переполнение: удаляется только устаревший «stale»
    assert lim.check("a", 71.0) is not None and lim.check("b", 71.0) is not None, "окна активных не сброшены"
    lim = RateLimiter(1, window=60, max_clients=3)
    for k, t in (("x", 0.0), ("y", 1.0), ("z", 2.0)):
        lim.check(k, t)
    lim.check("x", 3.0)                                  # x обращался недавно
    assert lim.check("new", 4.0) is None                 # места нет, устаревших нет: вытеснен самый давний — y
    assert lim.check("x", 5.0) is not None and lim.check("z", 5.0) is not None
    assert lim.check("y", 5.0) is None
    # release: попытка, которая не состоялась, не в счёт
    lim = RateLimiter(1, window=60)
    assert lim.check("k", 0.0) is None
    lim.release("k")
    assert lim.check("k", 1.0) is None


# --------------------------------------------------------------------------------------------
# Дата анализа: запас на часовой пояс совпадает с текстом ошибки
# --------------------------------------------------------------------------------------------
def test_record_date_allows_tomorrow_and_says_so():
    from datetime import datetime, timedelta, timezone

    app, _ = _app()
    c = _client(app)
    token, _ = _register(c, "date_user")
    today = datetime.fromtimestamp(app.state.store.now(), tz=timezone.utc).date()
    assert _record(c, token, date=(today + timedelta(days=1)).isoformat()).status_code == 201
    r = _record(c, token, date=(today + timedelta(days=2)).isoformat())
    assert r.status_code == 422 and "завтрашнего дня" in r.json()["error"]["message"], r.text


# --------------------------------------------------------------------------------------------
# Добавления по запросу страниц кабинета (П5): записи пациента у врача, правка набора референсов, last_used
# --------------------------------------------------------------------------------------------
def test_doctor_patient_records_list_is_logged_and_needs_access():
    app, _ = _app()
    c = _client(app)
    token, puser = _register(c, "rec_patient")
    doctor, _ = _register(_client(app), "rec_doc", role="doctor")
    stranger, _ = _register(_client(app), "rec_stranger", role="doctor")
    _record(c, token, date="2026-01-01")
    _record(c, token, date="2026-05-01")
    url = f"/api/v1/doctor/patients/{puser['id']}/records"
    assert c.get(url, headers=_bearer(doctor)).status_code == 403, "без связи"
    link = c.post("/api/v1/me/shares", json={"days": 7}, headers=CSRF).json()["link_token"]
    assert c.post("/api/v1/shares/accept", json={"token": link}, headers=_bearer(doctor)).status_code == 200
    assert c.get(url, headers=_bearer(stranger)).status_code == 403
    assert c.get(url, headers=_bearer(token)).status_code == 403, "пациенту — свой список /me/records"
    for _ in range(2):
        r = c.get(url, headers=_bearer(doctor))
        assert r.status_code == 200, r.text
    records = r.json()["records"]
    assert [x["date"] for x in records] == ["2026-05-01", "2026-01-01"]
    assert set(records[0]) == {"id", "date", "source", "headline", "anemia", "created"}
    mine = {x["id"]: x for x in c.get("/api/v1/me/records", headers=_bearer(token)).json()["records"]}
    assert records[0]["headline"] != mine[records[0]["id"]]["headline"], "заголовок — врачебный"
    items = c.get("/api/v1/me/access-log", headers=_bearer(token)).json()["items"]
    assert [(i["what"], i["record_id"]) for i in items] == [("records", None)], "повторы за час — одна строка"


def test_access_log_schema_migrates_to_records():
    """База, созданная до вида «records», пересоздаёт access_log с новым CHECK, строки сохраняются."""
    data_dir = Path(tempfile.mkdtemp(prefix="jm-sec-migrate-"))
    db = sqlite3.connect(str(data_dir / "justmedit.db"))
    db.executescript(
        "CREATE TABLE users (id TEXT PRIMARY KEY, login TEXT NOT NULL, login_key TEXT NOT NULL UNIQUE, role TEXT NOT "
        "NULL, pw_hash TEXT NOT NULL, clinic TEXT, consent_at INTEGER NOT NULL, created INTEGER NOT NULL);"
        "CREATE TABLE access_log (id INTEGER PRIMARY KEY, patient_id TEXT NOT NULL REFERENCES users(id) ON DELETE "
        "CASCADE, doctor_id TEXT REFERENCES users(id) ON DELETE SET NULL, what TEXT NOT NULL CHECK (what IN "
        "('history', 'record')), record_id TEXT, at INTEGER NOT NULL);"
        "INSERT INTO users VALUES ('p1', 'p', 'p', 'patient', 'x', NULL, 1, 1);"
        "INSERT INTO access_log (patient_id, doctor_id, what, record_id, at) VALUES ('p1', NULL, 'history', NULL, 5);")
    db.close()
    os.chmod(data_dir / "justmedit.db", 0o600)
    app, _ = _app(data_dir=str(data_dir))
    store = app.state.store
    store.log_access("p1", None, "records")
    rows = [tuple(r) for r in store._all("SELECT what, at FROM access_log ORDER BY id")]
    assert rows[0] == ("history", 5) and rows[1][0] == "records", rows


def test_doctor_lab_refs_patch():
    app, _ = _app()
    c = _client(app)
    doctor, _ = _register(c, "patch_doc", role="doctor")
    auth = _bearer(doctor)
    c.post("/api/v1/doctor/lab-refs", json={"name": "Первая", "default": True,
                                            "ranges": {"ferritin": {"low": 20}}}, headers=auth)
    sets = c.post("/api/v1/doctor/lab-refs", json={"name": "Вторая", "ranges": {"hb": {"low": 120, "unit": "g/L"}}},
                  headers=auth).json()["sets"]
    first, second = sets[0]["id"], sets[1]["id"]
    r = c.patch(f"/api/v1/doctor/lab-refs/{second}", json={"default": True}, headers=auth)
    assert r.status_code == 200 and [s["default"] for s in r.json()["sets"]] == [False, True], r.text
    r = c.patch(f"/api/v1/doctor/lab-refs/{second}", json={"name": "  Вторая, 2026 ",
                                                          "ranges": {"гемоглобин": {"low": 11.5, "unit": "г/дл"}}},
                headers=auth)
    s = r.json()["sets"][1]
    assert s["name"] == "Вторая, 2026" and s["ranges"] == {"hemoglobin": {"low": 115.0, "high": None, "unit": "g/L"}}
    r = c.patch(f"/api/v1/doctor/lab-refs/{second}", json={"default": False}, headers=auth)
    assert [s["default"] for s in r.json()["sets"]] == [False, False]
    for body, field in (({}, None), ({"default": "yes"}, "default"), ({"name": "x\u202e"}, "name"),
                        ({"ranges": {"unobtanium": {"low": 1}}}, "ranges.unobtanium")):
        r = c.patch(f"/api/v1/doctor/lab-refs/{first}", json=body, headers=auth)
        assert r.status_code == 422 and r.json()["error"]["field"] == field, (body, r.text)
    other, _ = _register(_client(app), "patch_other", role="doctor")
    r = c.patch(f"/api/v1/doctor/lab-refs/{first}", json={"default": True}, headers=_bearer(other))
    assert r.status_code == 404, "чужой набор"
    assert c.patch(f"/api/v1/doctor/lab-refs/{first}", json={"default": True}).status_code == 403, "cookie без CSRF"
    assert c.patch(f"/api/v1/doctor/lab-refs/{first}", json={"default": True}, headers=CSRF).status_code == 200


def test_new_token_has_no_last_used():
    app, _ = _app()
    c = _client(app)
    _register(c, "last_used_user")
    created = c.post("/api/v1/me/tokens", json={"name": "fresh"}, headers=CSRF).json()
    assert created["token"]["last_used"] is None
    value = created["value"]
    tokens = {t["name"]: t for t in c.get("/api/v1/me/tokens", headers=_bearer(value)).json()["tokens"]}
    assert tokens["fresh"]["last_used"] is not None, "отметка — с первого запроса этим токеном"
    assert tokens["Вход"]["last_used"] is None, "токеном входа ещё не пользовались (сайт ходит с cookie)"
