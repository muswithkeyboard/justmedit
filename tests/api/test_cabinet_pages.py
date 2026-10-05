"""Страницы личного кабинета (поток П5): /login, /cabinet, /share, пункт «Кабинет» в шапке и блок «Сохранить
в кабинет» на главной. Проверяется: страницы отдаются без встроенного кода, скрипты не пишут в хранилище браузера и
не вставляют разметку строками, запросы страницы проходят защиту сервера (cookie + X-Justmedit, Origin: null при
Referrer-Policy: no-referrer) — тем же форматом тел, что отправляет cabinet.js. Все значения придуманы.
"""
from __future__ import annotations

import re
import tempfile
from pathlib import Path

import helpers

from deficitlens_api.app import create_app
from deficitlens_api.settings import Settings

STATIC = helpers.ROOT / "src" / "deficitlens_api" / "static"
TEMPLATES = helpers.ROOT / "src" / "deficitlens_api" / "templates"
PASSWORD = "page-pass-123"
# Как отправляет страница: same-origin fetch под Referrer-Policy: no-referrer — браузер ставит Origin: null.
PAGE = {"X-Justmedit": "1", "Origin": "null"}
JS_FILES = ("cabinet.js", "nav.js", "home-cabinet.js")


def _app():
    data_dir = Path(tempfile.mkdtemp(prefix="jm-pages-")) / "db"
    settings = Settings(api_keys=("pages-key-1",), data_dir=str(data_dir), ui_rate_per_min=10000, auth_rate_per_min=1000,
                        auth_ip_rate_per_min=1000, cabinet_rate_per_min=100000)
    return create_app(settings), data_dir


def _client(app):
    try:
        from starlette.testclient import TestClient
    except Exception:  # noqa: BLE001 — нет httpx
        helpers.skip("нет httpx: страницы проверяются через starlette.testclient (uv sync --extra dev)")
    return TestClient(app)


def _js(name: str) -> str:
    return (STATIC / name).read_text(encoding="utf-8")


def _no_inline_code(html: str, path: str) -> None:
    assert not re.search(r"<script(?![^>]*\bsrc=)[^>]*>", html), path + ": встроенный <script>"
    assert not re.search(r"<[^>]+\son[a-z]+\s*=", html, flags=re.IGNORECASE), path + ": атрибут-обработчик on*"
    assert "<style" not in html and not re.search(r"<[^>]+\sstyle\s*=", html), path + ": встроенные стили"
    assert not re.search(r"\bsrc=\"https?://", html) and not re.search(r"<link[^>]+href=\"https?://", html), path


def test_cabinet_pages_render_without_inline_code():
    app, data_dir = _app()
    c = _client(app)
    expected = {"/login": ['id="auth"', 'id="tab-register"', 'name="role" value="doctor"', 'id="reg-consent"',
                           "Демо: не вводите реальные данные.", "Анализы хранятся зашифрованно до удаления"],
                "/cabinet": ['id="cab"', 'id="cab-patient"', 'id="cab-doctor"', 'id="sec-dyn"', 'id="sec-add"',
                             'id="sec-doctors"', 'id="sec-data"', 'id="sec-labrefs"', '<dialog class="dlg"',
                             'id="p-account"', 'id="d-account"'],
                "/share": ['id="share"', 'id="share-login-form"', 'id="share-ok"', 'id="share-patient"']}
    # /cabinet без cookie сессии — 303 на вход; страница смотрит только, есть ли cookie (без базы)
    r = c.get("/cabinet", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login?next=/cabinet"
    c.cookies.set("jm_session", "page-check")
    for path, needles in expected.items():
        r = c.get(path, follow_redirects=False)
        assert r.status_code == 200 and 'lang="ru"' in r.text, path
        _no_inline_code(r.text, path)
        assert "/static/cabinet.css" in r.text and "/static/cabinet.js" in r.text and "/static/nav.js" in r.text, path
        for n in needles:
            assert n in r.text, (path, n)
    assert "/static/export.js" in c.get("/cabinet").text                    # DOCX из ленты анализов
    assert not data_dir.exists(), "страницы кабинета без входа ничего не пишут на диск"


def test_header_link_depends_on_session_cookie():
    app, _ = _app()
    c = _client(app)
    for path in ("/", "/developers", "/cabinet"):
        html = c.get(path).text
        assert 'data-session="0"' in html and re.search(r'<a href="/login" class="nav__cabinet" id="nav-cabinet"[^>]*>Войти</a>', html), path
    r = c.post("/api/v1/auth/register", json={"login": "page.patient", "password": PASSWORD, "role": "patient", "consent": True})
    assert r.status_code == 201
    html = c.get("/").text                                                   # cookie сессии теперь у клиента
    assert 'data-session="1"' in html and re.search(r'<a href="/cabinet" class="nav__cabinet" id="nav-cabinet"[^>]*>Кабинет</a>', html)
    assert "page.patient" not in html, "логин в разметку не попадает: его показывает nav.js из /api/v1/me"


def test_cabinet_scripts_are_safe():
    for name in JS_FILES:
        js = _js(name)
        for bad in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "document.cookie", "eval(", "new Function"):
            assert bad not in js, (name, bad)
        assert "localStorage" not in js.replace("ни localStorage", ""), name
        assert "sessionStorage" not in js.replace("ни sessionStorage", ""), name
        assert 'credentials: "omit"' not in js and 'credentials: "same-origin"' in js, name
        assert not re.search(r"\.style\s*=|setAttribute\(\s*\"style\"", js), name            # CSP: только свойства style.*
    cab = _js("cabinet.js")
    # токен из ответа входа не используется и не сохраняется; изменяющие запросы — с X-Justmedit и JSON
    assert not re.search(r"data\.token\b", cab) and not re.search(r"data\.token\b", _js("home-cabinet.js"))
    assert 'headers["X-Justmedit"] = "1"' in cab and 'headers["Content-Type"] = "application/json"' in cab
    # переход после входа: только свой путь, не «//» и не «/\»
    assert 'raw.charAt(1) === "/"' in cab and 'raw.charAt(1) === "\\\\"' in cab and "u.origin !== window.location.origin" in cab
    # код ссылки врача — из фрагмента, только в теле POST, из адреса убирается после ответа
    assert "window.location.hash" in cab and '"/api/v1/shares/accept", { token: token }' in cab
    assert 'replaceState(null, "", "/share")' in cab and "?token" not in cab
    # пациенту не показываем проценты изменения (фильтр запрещённых слов, «%»)
    assert 'role === "doctor" && typeof s.delta_pct === "number"' in cab
    # П5b: лента врача — из списка записей пациента (не из точек истории); референсы — правкой PATCH на месте;
    # смена пароля и «выйти везде» — в «Моих данных»; пауза сервера — Retry-After
    assert 'base + "/records")' in cab and "recordsFromHistory" not in cab
    assert 'api("PATCH", "/api/v1/doctor/lab-refs/" + s.id, patch)' in cab
    assert '"DELETE", "/api/v1/doctor/lab-refs/" + old.id' not in cab, "правка набора без пересоздания"
    assert 'api("POST", "/api/v1/me/password", { old: old, "new": nw })' in cab
    assert 'api("POST", "/api/v1/me/logout-all", here ? { all: true } : {})' in cab
    assert "Пароль изменён, остальные сеансы закрыты" in cab
    assert 'resp.headers.get("Retry-After")' in cab
    for code in ("record_too_large", "quota_exceeded", "too_many_tokens", "storage_full", "signups_paused",
                 "service_busy", "unknown_analyte"):
        assert code in cab, code
    # у нового токена last_used = null: обходного сравнения с created больше нет
    assert "t.last_used !== t.created" not in cab


def test_main_page_cabinet_integration():
    html = (TEMPLATES / "index.html").read_text(encoding="utf-8")
    assert 'id="cab-save"' in html and "/static/home-cabinet.js" in html
    app_js = _js("app.js")
    assert 'credentials: "same-origin"' in app_js                            # врачу сервер подставит его референсы
    assert 'new CustomEvent("jm:result"' in app_js and "source: state.source" in app_js
    home = _js("home-cabinet.js")
    assert '"/api/v1/me/records"' in home and '"/api/v1/me/history"' in home
    assert 'td.hidden = true' in home and "Войдите" in home
    base = (TEMPLATES / "base.html").read_text(encoding="utf-8")
    assert '/static/nav.js' in base and 'id="nav-cabinet"' in base


def test_page_requests_pass_server_checks():
    """Те же запросы и тела, что шлёт cabinet.js: регистрация, 3 анализа, история, ссылка, врач, журнал, отзыв,
    референсы врача, удаление. Cookie сайта + X-Justmedit + Origin: null."""
    app, _ = _app()
    pat, doc = _client(app), _client(app)
    r = pat.post("/api/v1/auth/register", json={"login": "flow_patient", "password": PASSWORD, "role": "patient", "consent": True})
    assert r.status_code == 201
    # без X-Justmedit изменяющий запрос с cookie отклоняется — поэтому страница всегда его ставит
    assert pat.post("/api/v1/me/shares", json={"days": 7}).status_code == 403
    for day, hb, fer, mcv in (("2026-03-01", 132, 45, 88), ("2026-06-01", 125, 28, 85), ("2026-09-01", 118, 12, 81)):
        body = {"date": day, "source": "form", "input": {"sex": "F", "age_years": 34, "pregnancy": {"status": "no"},
                                                         "values": {"hemoglobin": hb, "ferritin": fer, "MCV": {"value": mcv, "unit": "фл"}}}}
        if day == "2026-09-01":
            body["source"] = "pdf"
            body["input"]["reference_ranges"] = {"hemoglobin": {"low": 120, "high": 150, "unit": "g/L"}}   # с бланка
        r = pat.post("/api/v1/me/records", json=body, headers=PAGE)
        assert r.status_code == 201, r.text
    h = pat.get("/api/v1/me/history").json()
    hb = [s for s in h["series"] if s["analyte"] == "hemoglobin"][0]
    assert [p["value"] for p in hb["points"]] == [132, 125, 118] and hb["delta"] == -14 and hb["ref"] == {"low": 120, "high": 150}
    assert any(e["code"] == "hb_drop" for e in h["events"]) and h["completeness"] and h["summary"]
    for e in h["events"]:
        assert "%" not in e["text"], e                                       # пациенту без процентов
    # ссылка врачу -> врач принимает -> история и отчёт -> журнал у пациента -> отзыв
    share = pat.post("/api/v1/me/shares", json={"days": 7}, headers=PAGE).json()
    assert share["link"] == "/share#" + share["link_token"]
    assert doc.post("/api/v1/auth/register", json={"login": "flow_doctor", "password": PASSWORD, "role": "doctor",
                                                   "clinic": "Клиника на Лесной", "consent": True}).status_code == 201
    acc = doc.post("/api/v1/shares/accept", json={"token": share["link_token"]}, headers=PAGE)
    assert acc.status_code == 200 and acc.json()["patient"]["login"] == "flow_patient"
    pid = acc.json()["patient"]["id"]
    assert doc.post("/api/v1/shares/accept", json={"token": share["link_token"]}, headers=PAGE).json()["error"]["code"] == "share_invalid"
    dh = doc.get(f"/api/v1/doctor/patients/{pid}/history").json()
    rid = dh["series"][0]["points"][-1]["record_id"]
    assert doc.get(f"/api/v1/doctor/patients/{pid}/records/{rid}").json()["result"]["reports"]["doctor"]["headline"]
    log = pat.get("/api/v1/me/access-log").json()["items"]
    assert {i["what"] for i in log} == {"history", "record"} and log[0]["doctor"]["clinic"] == "Клиника на Лесной"
    sid = [s for s in pat.get("/api/v1/me/shares").json()["shares"] if s["status"] == "active"][0]["id"]
    assert pat.delete(f"/api/v1/me/shares/{sid}", headers=PAGE).status_code == 200
    assert doc.get(f"/api/v1/doctor/patients/{pid}/history").status_code == 403
    # референсы врача: набор по умолчанию подставляется в расчёт страницы (/ui/analyze с cookie)
    r = doc.post("/api/v1/doctor/lab-refs", json={"name": "Лаборатория", "default": True,
                                                  "ranges": {"hemoglobin": {"low": 125, "high": 155, "unit": "g/L"}}}, headers=PAGE)
    assert r.status_code == 200 and r.json()["sets"][0]["default"] is True
    res = doc.post("/ui/analyze", json={"sex": "F", "age_years": 34, "values": {"hemoglobin": 130}}).json()
    assert res["references"]["warning"] and "hemoglobin" in res["references"]["local_analytes"]
    # токены API: значение — только в ответе на создание
    tok = pat.post("/api/v1/me/tokens", json={"name": "Мой сайт"}, headers=PAGE).json()
    assert tok["value"] and all("value" not in t for t in pat.get("/api/v1/me/tokens").json()["tokens"])
    # выгрузка — обычной ссылкой (GET с cookie), загрузка — JSON
    exp = pat.get("/api/v1/me/export?format=json")
    assert exp.status_code == 200 and "attachment" in exp.headers["content-disposition"]
    assert pat.get("/api/v1/me/export?format=csv").text.startswith('"sex"')
    # удаление всего: неверный пароль -> 403 wrong_password, верный -> всё стёрто
    bad = pat.request("DELETE", "/api/v1/me", json={"password": "wrong-pass-1"}, headers=PAGE)
    assert bad.status_code == 403 and bad.json()["error"]["code"] == "wrong_password"
    assert pat.request("DELETE", "/api/v1/me", json={"password": PASSWORD}, headers=PAGE).json() == {"deleted": True}
    assert pat.get("/api/v1/me").status_code == 401


def _patient_with_records(app, login: str):
    """Пациент с двумя анализами (второй — с анемией) и врач с принятой ссылкой. Те же тела, что шлёт страница."""
    pat, doc = _client(app), _client(app)
    assert pat.post("/api/v1/auth/register", json={"login": login, "password": PASSWORD, "role": "patient",
                                                   "consent": True}).status_code == 201
    for day, hb, src in (("2026-05-01", 131, "form"), ("2026-08-01", 104, "pdf")):
        body = {"date": day, "source": src, "input": {"sex": "F", "age_years": 41, "pregnancy": {"status": "no"},
                                                      "values": {"hemoglobin": hb, "ferritin": 11, "MCV": 79}}}
        assert pat.post("/api/v1/me/records", json=body, headers=PAGE).status_code == 201
    share = pat.post("/api/v1/me/shares", json={"days": 7}, headers=PAGE).json()
    assert doc.post("/api/v1/auth/register", json={"login": login + "_doc", "password": PASSWORD, "role": "doctor",
                                                   "consent": True}).status_code == 201
    pid = doc.post("/api/v1/shares/accept", json={"token": share["link_token"]}, headers=PAGE).json()["patient"]["id"]
    return pat, doc, pid


def test_doctor_feed_and_lab_refs_patch():
    """Лента врача — GET doctor/patients/{id}/records (вывод врачу, анемия, источник; новые сверху), раскрытие —
    doctor/patients/{id}/records/{rid}; референсы: «по умолчанию», переименование и границы — PATCH на месте."""
    app, _ = _app()
    pat, doc, pid = _patient_with_records(app, "feed_patient")
    r = doc.get(f"/api/v1/doctor/patients/{pid}/records")
    assert r.status_code == 200
    recs = r.json()["records"]
    assert [x["date"] for x in recs] == ["2026-08-01", "2026-05-01"]
    assert [x["source"] for x in recs] == ["pdf", "form"] and [x["anemia"] for x in recs] == [True, False]
    full = doc.get(f"/api/v1/doctor/patients/{pid}/records/{recs[0]['id']}").json()
    assert recs[0]["headline"] == full["result"]["reports"]["doctor"]["headline"]          # заголовок — врачебный
    mine = pat.get("/api/v1/me/records").json()["records"]
    assert {x["id"] for x in mine} == {x["id"] for x in recs}
    assert {i["what"] for i in pat.get("/api/v1/me/access-log").json()["items"]} == {"records", "record"}
    # другой врач без связи — 403
    other = _client(app)
    assert other.post("/api/v1/auth/register", json={"login": "feed_other_doc", "password": PASSWORD,
                                                     "role": "doctor", "consent": True}).status_code == 201
    assert other.get(f"/api/v1/doctor/patients/{pid}/records").status_code == 403

    # референсы: два набора, первый — по умолчанию
    hb = {"hemoglobin": {"low": 125, "high": 155, "unit": "g/L"}}
    doc.post("/api/v1/doctor/lab-refs", json={"name": "Основная", "default": True, "ranges": hb}, headers=PAGE)
    sets = doc.post("/api/v1/doctor/lab-refs", json={"name": "Филиал", "default": False, "ranges": hb},
                    headers=PAGE).json()["sets"]
    by = {s["name"]: s for s in sets}
    a_id, b_id = by["Основная"]["id"], by["Филиал"]["id"]
    assert by["Основная"]["default"] is True and by["Филиал"]["default"] is False
    # без X-Justmedit правка с cookie отклоняется; пустая правка — 422
    assert doc.patch(f"/api/v1/doctor/lab-refs/{b_id}", json={"default": True}).status_code == 403
    assert doc.patch(f"/api/v1/doctor/lab-refs/{b_id}", json={}, headers=PAGE).json()["error"]["code"] == "missing_field"
    # «Сделать по умолчанию» — PATCH {"default": true}: отметка переходит, наборов столько же, id прежние
    sets = doc.patch(f"/api/v1/doctor/lab-refs/{b_id}", json={"default": True}, headers=PAGE).json()["sets"]
    assert {s["id"]: s["default"] for s in sets} == {a_id: False, b_id: True}
    # переименование и правка границ — тот же набор
    new_hb = {"hemoglobin": {"low": 118, "high": 150, "unit": "g/L"}}
    sets = doc.patch(f"/api/v1/doctor/lab-refs/{b_id}", json={"name": "Филиал на Лесной", "ranges": new_hb},
                     headers=PAGE).json()["sets"]
    b = [s for s in sets if s["id"] == b_id][0]
    assert len(sets) == 2 and b["name"] == "Филиал на Лесной" and b["ranges"]["hemoglobin"]["low"] == 118 and b["default"]
    res = doc.post("/ui/analyze", json={"sex": "F", "age_years": 34, "values": {"hemoglobin": 130}}).json()
    assert "hemoglobin" in res["references"]["local_analytes"]
    # «Снять по умолчанию» — в расчётах снова референсы проекта
    sets = doc.patch(f"/api/v1/doctor/lab-refs/{b_id}", json={"default": False}, headers=PAGE).json()["sets"]
    assert not any(s["default"] for s in sets)
    res = doc.post("/ui/analyze", json={"sex": "F", "age_years": 34, "values": {"hemoglobin": 130}}).json()
    assert not res["references"]["local_analytes"]
    # чужой набор — 404
    assert other.patch(f"/api/v1/doctor/lab-refs/{a_id}", json={"default": True}, headers=PAGE).status_code == 404


def test_password_change_and_logout_all_from_page():
    """«Мои данные»: смена пароля {old, new} закрывает другие сеансы и токены API, текущий вход остаётся;
    «выйти на всех устройствах» {} — то же без смены пароля (пароль с cookie не нужен); {"all": true} — и этот вход."""
    app, _ = _app()
    here, there = _client(app), _client(app)
    assert here.post("/api/v1/auth/register", json={"login": "pw_patient", "password": PASSWORD, "role": "patient",
                                                    "consent": True}).status_code == 201
    assert there.post("/api/v1/auth/login", json={"login": "pw_patient", "password": PASSWORD}).status_code == 200
    named = here.post("/api/v1/me/tokens", json={"name": "Мой сайт"}, headers=PAGE).json()["value"]
    bearer = {"Authorization": f"Bearer {named}"}
    assert here.get("/api/v1/me", headers=bearer).status_code == 200
    # неверный текущий пароль -> 403 wrong_password (поле old), ничего не меняется
    bad = here.post("/api/v1/me/password", json={"old": "wrong-pass-1", "new": "new-pass-4567"}, headers=PAGE)
    assert bad.status_code == 403 and bad.json()["error"]["code"] == "wrong_password"
    assert bad.json()["error"]["field"] == "old" and there.get("/api/v1/me").status_code == 200
    # короткий новый пароль -> 422, поле new; без X-Justmedit — 403 csrf
    short = here.post("/api/v1/me/password", json={"old": PASSWORD, "new": "short"}, headers=PAGE)
    assert short.status_code == 422 and short.json()["error"]["field"] == "new"
    assert here.post("/api/v1/me/password", json={"old": PASSWORD, "new": "new-pass-4567"}).status_code == 403
    r = here.post("/api/v1/me/password", json={"old": PASSWORD, "new": "new-pass-4567"}, headers=PAGE)
    assert r.status_code == 200 and r.json()["ok"] is True and r.json()["revoked"] >= 2
    assert here.get("/api/v1/me").status_code == 200                         # этот вход остался
    assert there.get("/api/v1/me").status_code == 401                        # другой сеанс закрыт
    assert here.get("/api/v1/me", headers=bearer).status_code == 401         # именованный токен отозван
    assert [t["name"] for t in here.get("/api/v1/me/tokens").json()["tokens"]] == ["Вход"]
    assert there.post("/api/v1/auth/login", json={"login": "pw_patient", "password": PASSWORD}).status_code == 401
    assert there.post("/api/v1/auth/login", json={"login": "pw_patient", "password": "new-pass-4567"}).status_code == 200

    # «Выйти на всех устройствах»: {} — другие сеансы закрыты, этот остался; с cookie пароль не нужен
    assert here.post("/api/v1/me/logout-all", json={}).status_code == 403   # без X-Justmedit
    r = here.post("/api/v1/me/logout-all", json={}, headers=PAGE)
    assert r.status_code == 200 and r.json()["ok"] is True and r.json()["revoked"] >= 1
    assert here.get("/api/v1/me").status_code == 200 and there.get("/api/v1/me").status_code == 401
    # вариант «и на этом устройстве»: {"all": true} — закрыт и текущий вход, cookie стирается
    r = here.post("/api/v1/me/logout-all", json={"all": True}, headers=PAGE)
    assert r.status_code == 200 and r.json()["ok"] is True
    assert "set-cookie" in r.headers and here.get("/api/v1/me").status_code == 401


def test_ux_review_fixes_in_pages():
    """Ревизия UX кабинета (05.10.2026): поведение, которое видно только в браузере, — разметкой и наличием
    обработчиков; запросы этих сценариев проверяются в test_cabinet.py."""
    cab = _js("cabinet.js")
    # /share: новая ссылка на той же странице (меняется только «#») — код перечитывается, проверка заново
    assert 'window.addEventListener("hashchange"' in cab and "token = next;" in cab and "check();" in cab
    # 401 в кабинете (кроме /auth/*) — на вход с плашкой «Сеанс завершён»; текст плашки — по коду, не из адреса
    assert "r.status === 401 && onUnauthorized" in cab and "/^\\/api\\/v1\\/auth\\//" in cab
    assert '"&reason=expired"' in cab and "REASON_TEXT.hasOwnProperty(reason)" in cab
    # история: один автоповтор на 503 через Retry-After
    assert "function getHistory(url)" in cab and 'getHistory("/api/v1/me/history")' in cab
    # загрузка выгрузки: «Загружено N, дублей пропущено M», ошибка остаётся в диалоге, «Отмена» занята
    assert "r.data.skipped" in cab and "дублей пропущено" in cab and "if (!r.ok || !r.data) { return errText(r); }" in cab
    assert "cancel.disabled = true;" in cab and "dlg.jmBusy" in cab
    # «Добавить анализ»: замечания разбора, нераспознанное и «дату не нашли»
    assert "d.notes" in cab and "d.ignored" in cab and "Дату на бланке не нашли" in cab and "markDate(!dated)" in cab
    # лента — по 10, «Показать ещё»; точка графика за «Показать ещё» тоже открывается
    assert "FEED_PAGE = 10" in cab and '"Показать ещё "' in cab and "FEEDS[box.id].reveal(id)" in cab
    # врач: пациент закрыл доступ (403) — карточка закрывается, список заново
    assert "function accessLost()" in cab and "onForbidden: accessLost" in cab
    # токены входа не в списке «Токены API»; удаление — экран-подтверждение на странице входа
    assert 't.kind === "login"' in cab and '"/login?reason=deleted"' in cab
    # одно слово на понятие
    for old in ("Отвязать", "учётн", '"Проекта"', "Создайте ссылку слева", " дн.\"", '" мес"'):
        assert old not in cab, old
    assert cab.count('"Закрыть доступ"') >= 2
    home = _js("home-cabinet.js")
    assert '"/api/v1/auth/login"' in home and "loginForm(box)" in home and "setNav(me)" in home
    nav = _js("nav.js")
    assert "refresh: function" in nav and '"Кабинет: " + user.login' in nav

    app, _ = _app()
    c = _client(app)
    login, share = c.get("/login").text, c.get("/share").text
    c.cookies.set("jm_session", "page-check")              # /cabinet без cookie — 303 на вход (ревизия надёжности)
    cabinet = c.get("/cabinet", follow_redirects=False).text
    # «Перейти к содержимому» ведёт к карточке формы, а не мимо неё
    assert '<a class="skip" href="#auth">' in login and 'id="auth" tabindex="-1"' in login
    assert '<a class="skip" href="#share">' in share and 'id="share" tabindex="-1"' in share
    assert '<a class="skip" href="#main">' in cabinet
    assert 'id="auth-note"' in login and "Я согласен(на)" in login and "Я согласен(на)" in share
    assert "латиница или кириллица" in login and 'id="cab-clinic"' in cabinet
