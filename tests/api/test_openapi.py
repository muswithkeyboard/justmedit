"""Описание API static/openapi.json и страница /developers (поток П6).

Проверяется: JSON корректен; у каждого маршрута есть summary, группа, «кто может вызывать» и пример; ссылки $ref
разрешаются; маршруты совпадают с app.py и с контрактом docs/CONTRACTS.md. Сверка — в две стороны, чтобы тест
проходил и до, и после слияния с кабинетом (поток П3):
  * каждый маршрут /api/v1/* и /healthz из app.py описан в openapi.json (новый маршрут без описания — FAIL);
  * каждый метод и путь openapi.json есть в контракте, и каждый маршрут контракта описан в openapi.json
    (маршруты кабинета описаны по контракту, даже если в этой ветке их ещё нет).
Примеры кабинета сверяются с живыми ответами (П6b): тело-пример из openapi.json отправляется в приложение на
временной базе, код ответа и ключи ответа должны совпасть с описанными; коды ошибок — с примерами ошибок.
"""
from __future__ import annotations

import json
import re
import tempfile
from pathlib import Path

import helpers

from deficitlens_api.app import create_app
from deficitlens_api.settings import Settings

ROOT = Path(__file__).resolve().parents[2]
SPEC_PATH = ROOT / "src" / "deficitlens_api" / "static" / "openapi.json"
CONTRACTS = ROOT / "docs" / "CONTRACTS.md"
METHODS = ("get", "post", "put", "patch", "delete")
GROUPS = ["Вход и токены", "Анализ", "Файлы", "Кабинет пациента", "Врач", "Служебное"]
KEY = "openapi-key-1"


def _spec() -> dict:
    return json.loads(SPEC_PATH.read_text(encoding="utf-8"))


def _operations(spec: dict):
    for path, item in spec["paths"].items():
        for method in METHODS:
            if method in item:
                yield method, path, item[method]


def _public(path: str) -> bool:
    """Маршруты для интеграции: /api/v1/* и /healthz (страницы и /ui/* в описание не входят)."""
    return path.startswith("/api/v1/") or path == "/healthz"


def _norm(path: str) -> str:
    """Starlette пишет параметр пути как {id} или {id:int}, OpenAPI — {id}."""
    return re.sub(r"\{(\w+)(?::\w+)?\}", r"{\1}", path)


def _app_routes() -> set[tuple[str, str]]:
    from starlette.routing import Mount, Route

    app = create_app(Settings(api_keys=(KEY,)))
    found: set[tuple[str, str]] = set()

    def walk(routes, prefix: str = "") -> None:
        for r in routes:
            if isinstance(r, Route):
                for m in r.methods or ():
                    if m.lower() in METHODS:
                        found.add((m.lower(), _norm(prefix + r.path)))
            elif isinstance(r, Mount):
                walk(getattr(r, "routes", None) or [], prefix + r.path)

    walk(app.routes)
    return {(m, p) for m, p in found if _public(p)}


def _contract_routes() -> set[tuple[str, str]]:
    """(метод, путь) из таблиц маршрутов docs/CONTRACTS.md. В строке «`GET /api/v1/me/tokens`, `POST` (…),
    `DELETE /api/v1/me/tokens/{id}`» метод без пути относится к последнему названному пути; строка запроса
    (`?format=json|csv`) отбрасывается."""
    found: set[tuple[str, str]] = set()
    for line in CONTRACTS.read_text(encoding="utf-8").splitlines():
        if not line.startswith("|"):
            continue
        last = None
        for method, path in re.findall(r"`(GET|POST|PUT|PATCH|DELETE)(?:\s+([^`\s]+))?`", line):
            if path:
                last = path.split("?")[0]
            if last and _public(last):
                found.add((method.lower(), last))
    return found


def _client():
    from starlette.testclient import TestClient
    return TestClient(create_app(Settings(api_keys=(KEY,), ui_rate_per_min=10000)))


# --------------------------------------------------------------------------------------------
def test_openapi_is_valid_and_has_groups_and_auth():
    spec = _spec()
    assert spec["openapi"].startswith("3.0.")
    assert spec["info"]["title"] and spec["info"]["version"] and spec["info"]["description"]
    assert [t["name"] for t in spec["tags"]] == GROUPS
    schemes = spec["components"]["securitySchemes"]
    key = schemes["ApiKeyAuth"]
    assert (key["type"], key["in"], key["name"]) == ("apiKey", "header", "X-API-Key")
    assert schemes["BearerAuth"]["type"] == "http" and schemes["BearerAuth"]["scheme"] == "bearer"
    ids = [op["operationId"] for _, _, op in _operations(spec)]
    assert len(ids) == len(set(ids)), "operationId повторяется"


def test_every_operation_has_summary_group_role_and_examples():
    spec = _spec()
    for method, path, op in _operations(spec):
        where = f"{method.upper()} {path}"
        assert isinstance(op.get("summary"), str) and op["summary"].strip(), f"{where}: нет summary"
        assert op.get("description"), f"{where}: нет description"
        assert op.get("tags") and op["tags"][0] in GROUPS, f"{where}: группа не из списка"
        assert op.get("x-role"), f"{where}: не указано, кто может вызывать (x-role)"
        assert isinstance(op.get("security"), list), f"{where}: не указана авторизация (security)"
        ok = [c for c in op["responses"] if c.startswith("2")]
        assert ok, f"{where}: нет успешного ответа"
        content = op["responses"][ok[0]].get("content", {})
        assert any(m.get("example") is not None for m in content.values()), f"{where}: нет примера ответа"
        body = op.get("requestBody", {}).get("content", {})
        if "application/json" in body:
            assert body["application/json"].get("example") is not None, f"{where}: нет примера тела"
        for p in op.get("parameters", []):
            if p["in"] == "path":
                assert "{" + p["name"] + "}" in path and p.get("example"), f"{where}: параметр {p['name']}"


def test_refs_resolve():
    spec = _spec()
    refs: list[str] = []

    def walk(node) -> None:
        if isinstance(node, dict):
            if isinstance(node.get("$ref"), str):
                refs.append(node["$ref"])
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(spec)
    assert refs
    for ref in set(refs):
        assert ref.startswith("#/"), ref
        node = spec
        for part in ref[2:].split("/"):
            assert part in node, f"не найдено: {ref}"
            node = node[part]


def test_app_routes_are_documented():
    documented = {(m, p) for m, p, _ in _operations(_spec())}
    missing = _app_routes() - documented
    assert not missing, f"маршруты app.py без описания в openapi.json: {sorted(missing)}"


def test_openapi_matches_contract():
    documented = {(m, p) for m, p, _ in _operations(_spec())}
    contract = _contract_routes()
    assert len(contract) >= 30, "таблица маршрутов в CONTRACTS.md не прочиталась"
    assert not documented - contract, f"в openapi.json есть маршруты не из контракта: {sorted(documented - contract)}"
    assert not contract - documented, f"маршруты контракта без описания: {sorted(contract - documented)}"


def test_openapi_route_serves_spec_without_key():
    r = _client().get("/api/v1/openapi.json")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/json")
    assert r.json() == _spec()


def test_developers_page():
    r = _client().get("/developers")
    assert r.status_code == 200 and 'lang="ru"' in r.text
    html = r.text
    assert "API для" in html and "/static/developers.js" in html and "/static/developers.css" in html
    assert re.search(r'<a href="/developers"[^>]*\saria-current="page"', html), "пункт «API» в шапке не выделен"
    for anchor in ("console", "code", "reference", "embed", "errors"):
        assert f'id="{anchor}"' in html, anchor
    assert "DL_CORS_ORIGINS" in html and "сервер к серверу" in html
    js = (ROOT / "src" / "deficitlens_api" / "static" / "developers.js").read_text(encoding="utf-8")
    assert 'credentials: "omit"' in js                        # консоль не отправляет cookie сайта
    assert "sessionStorage" not in js and "indexedDB" not in js


# --------------------------------------------------------------------------------------------
# Примеры кабинета против живых ответов (временная база, значения из примеров openapi.json — придуманные)
# --------------------------------------------------------------------------------------------
def _cabinet_client():
    """Приложение с базой кабинета во временном каталоге и снятыми лимитами; клиент без lifespan (как в
    tests/api/test_cabinet.py)."""
    try:
        from starlette.testclient import TestClient
    except Exception:  # noqa: BLE001 — нет httpx
        helpers.skip("нет httpx: сверка примеров кабинета идёт через starlette.testclient (uv sync --extra dev)")
    settings = Settings(api_keys=(KEY,), data_dir=tempfile.mkdtemp(prefix="jm-openapi-"), ui_rate_per_min=10000,
                        auth_rate_per_min=1000, auth_ip_rate_per_min=1000, cabinet_rate_per_min=100000)
    return TestClient(create_app(settings))


def _deref(spec: dict, node: dict) -> dict:
    """Ссылка «#/components/…» -> объект."""
    while isinstance(node, dict) and "$ref" in node:
        target = spec
        for part in node["$ref"][2:].split("/"):
            target = target[part]
        node = target
    return node


def _request_example(spec: dict, method: str, path: str):
    return spec["paths"][path][method]["requestBody"]["content"]["application/json"]["example"]


def _documented(spec: dict, method: str, path: str, status: int | None = None) -> tuple[int, object]:
    """(код, пример ответа): status=None — первый успешный ответ, иначе — ответ с этим кодом."""
    responses = spec["paths"][path][method]["responses"]
    code = str(status) if status is not None else min(c for c in responses if c.startswith("2"))
    assert code in responses, f"{method.upper()} {path}: код {code} не описан"
    media = _deref(spec, responses[code])["content"]["application/json"]
    return int(code), media["example"]


def _same_shape(real, example, where: str, loose: bool = False) -> None:
    """Ключи примера = ключи живого ответа (рекурсивно; у списков — первый элемент). Пример, сокращённый ключом
    «…», проверяется включением — каждый его ключ есть в ответе — и так во всех вложенных объектах. Типы значений
    совпадают (число с числом; null в примере или в ответе не сравнивается)."""
    if isinstance(real, dict):
        assert isinstance(example, dict), f"{where}: в примере не объект"
        loose = loose or "…" in example
        keys = set(example) - {"…"}
        if loose:
            assert keys <= set(real), f"{where}: в ответе нет ключей примера {sorted(keys - set(real))}"
        else:
            assert keys == set(real), f"{where}: ключи примера {sorted(keys)} ≠ ключи ответа {sorted(real)}"
        for k in sorted(keys & set(real)):
            _same_shape(real[k], example[k], f"{where}.{k}", loose)
    elif isinstance(real, list):
        assert isinstance(example, list), f"{where}: в примере не список"
        if real and example:
            _same_shape(real[0], example[0], f"{where}[0]", loose)
    elif real is not None and example is not None:
        if isinstance(real, bool) or isinstance(example, bool):
            assert isinstance(real, bool) and isinstance(example, bool), f"{where}: логическое значение"
        elif isinstance(real, (int, float)):
            assert isinstance(example, (int, float)), f"{where}: в примере не число"
        else:
            assert type(real) is type(example), f"{where}: тип {type(example).__name__} ≠ {type(real).__name__}"


def test_cabinet_response_examples_match_api():
    """По одному запросу на группу: регистрация, запись анализа, ссылка врачу, токен API, референсы врача.
    Тело — пример запроса из openapi.json; код и ключи ответа — как в примере ответа."""
    spec = _spec()
    c = _cabinet_client()

    def call(method: str, path: str, body, token: str | None = None, url: str | None = None):
        """url — путь с подставленными id, если в описании путь с параметрами ({id})."""
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        r = c.request(method.upper(), url or path, json=body, headers=headers)
        code, example = _documented(spec, method, path)
        where = f"{method.upper()} {path}"
        assert r.status_code == code, f"{where}: ответ {r.status_code}, в описании {code}: {r.text[:300]}"
        _same_shape(r.json(), example, where)
        return r.json()

    registered = call("post", "/api/v1/auth/register", _request_example(spec, "post", "/api/v1/auth/register"))
    patient, patient_id = registered["token"], registered["user"]["id"]
    call("post", "/api/v1/me/records", _request_example(spec, "post", "/api/v1/me/records"), patient)
    link = call("post", "/api/v1/me/shares", _request_example(spec, "post", "/api/v1/me/shares"), patient)["link_token"]
    call("post", "/api/v1/me/tokens", _request_example(spec, "post", "/api/v1/me/tokens"), patient)
    call("get", "/api/v1/me/tokens", None, patient)
    call("post", "/api/v1/me/import", _request_example(spec, "post", "/api/v1/me/import"), patient)   # imported, skipped
    # выйти везде (кроме текущего входа) и смена пароля — последними: они отзывают другие токены и меняют пароль
    call("post", "/api/v1/me/logout-all", _request_example(spec, "post", "/api/v1/me/logout-all"), patient)
    call("post", "/api/v1/me/password", _request_example(spec, "post", "/api/v1/me/password"), patient)
    doctor_body = {**_request_example(spec, "post", "/api/v1/auth/register"), "login": "dr.openapi", "role": "doctor"}
    doctor = call("post", "/api/v1/auth/register", doctor_body)["token"]
    sets = call("post", "/api/v1/doctor/lab-refs", _request_example(spec, "post", "/api/v1/doctor/lab-refs"), doctor)
    call("patch", "/api/v1/doctor/lab-refs/{id}", _request_example(spec, "patch", "/api/v1/doctor/lab-refs/{id}"),
         doctor, url=f"/api/v1/doctor/lab-refs/{sets['sets'][0]['id']}")
    assert c.post("/api/v1/shares/accept", json={"token": link},
                  headers={"Authorization": f"Bearer {doctor}"}).status_code == 200
    call("get", "/api/v1/doctor/patients/{id}/records", None, doctor,
         url=f"/api/v1/doctor/patients/{patient_id}/records")
    # дата анализа необязательна (RecordCreate.required — только input)
    assert _deref(spec, {"$ref": "#/components/schemas/RecordCreate"})["required"] == ["input"]
    body = {"input": _request_example(spec, "post", "/api/v1/me/records")["input"]}
    r = c.post("/api/v1/me/records", json=body, headers={"Authorization": f"Bearer {patient}"})
    assert r.status_code == 201, r.text


def test_cabinet_error_examples_match_api():
    """Коды ошибок кабинета в описании совпадают с живыми: 409 login_taken, 401 invalid_credentials, 415,
    404 share_invalid, 403 wrong_password."""
    spec = _spec()
    c = _cabinet_client()
    reg = _request_example(spec, "post", "/api/v1/auth/register")
    token = c.post("/api/v1/auth/register", json=reg).json()["token"]
    doctor = c.post("/api/v1/auth/register", json={**reg, "login": "dr.errors", "role": "doctor"}).json()["token"]
    # по Bearer-токену ссылку врачу создают только с паролем аккаунта
    link = c.post("/api/v1/me/shares", json={"days": 7, "password": reg["password"]},
                  headers={"Authorization": f"Bearer {token}"}).json()
    bearer = {"Authorization": f"Bearer {doctor}"}
    assert c.post("/api/v1/shares/accept", json={"token": link["link_token"]}, headers=bearer).status_code == 200
    cases = [
        ("post", "/api/v1/auth/register", c.post("/api/v1/auth/register", json={**reg, "login": reg["login"].upper()})),
        ("post", "/api/v1/auth/login", c.post("/api/v1/auth/login", json={"login": reg["login"], "password": "wrong-pass-1"})),
        ("post", "/api/v1/auth/login", c.post("/api/v1/auth/login", content=b"{}", headers={"Content-Type": "text/plain"})),
        ("post", "/api/v1/shares/accept", c.post("/api/v1/shares/accept", json={"token": link["link_token"]},
                                                 headers=bearer)),
        ("delete", "/api/v1/me", c.request("DELETE", "/api/v1/me", json={"password": "wrong-pass-1"},
                                           headers={"Authorization": f"Bearer {token}"})),
        ("post", "/api/v1/me/password", c.post("/api/v1/me/password",
                                               json={"old": "wrong-pass-1", "new": "new-pass-12"},
                                               headers={"Authorization": f"Bearer {token}"})),
    ]
    for method, path, r in cases:
        _, example = _documented(spec, method, path, r.status_code)
        assert example["error"]["code"] == r.json()["error"]["code"], (method, path, r.status_code, r.json())
    assert [r.status_code for _, _, r in cases] == [409, 401, 415, 404, 403, 403]


def test_developers_page_examples_after_ux_review():
    """/developers: пароль — только из переменной окружения, токен — не в коде страницы, ошибки кабинета в таблице
    и в подсказках консоли; консоль не создаёт общий демо-аккаунт с паролем из документации."""
    html = _client().get("/developers").text
    assert "anna.demo" not in html and "придумайте-длинный-пароль" not in html
    assert html.count("$JUSTMEDIT_PASSWORD") >= 4 and "password_required" in html
    assert "Не вставляйте токен в код страницы" in html and "USER_TOKEN" not in html and "examples/clinic_site" in html
    for code in ("password_required", "wrong_password", "record_too_large", "unknown_analyte", "missing_field",
                 "too_many_tokens", "quota_exceeded", "signups_paused", "storage_full"):
        assert f"<code>{code}</code>" in html, code
    js = (ROOT / "src" / "deficitlens_api" / "static" / "developers.js").read_text(encoding="utf-8")
    assert '507: "хранилище заполнено"' in js and "WRONG_PASSWORD_HINTS" in js and "randomLogin()" in js
    for code in ("password_required", "too_many_tokens", "quota_exceeded", "record_too_large", "storage_full",
                 "signups_paused", "unknown_analyte", "missing_field"):
        assert code + ":" in js, code
    assert "__JM_ENV_" in js and "jsonErrorText" in js and "e.message; r.bodyError" not in js
