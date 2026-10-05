"""Мини-сайт клиники — пример интеграции DeficitLens: форма ОАК → свой сервер → POST /api/v1/analyze → отчёт пациенту.

Смысл примера: ключ API живёт только на сервере клиники (переменная окружения DL_API_KEY), браузер его не видит
и обращается только к своему origin. Сервер клиники проверяет форму, пересылает значения в DeficitLens с ключом
и отдаёт странице только reports.patient (отчёт простыми словами, без диагнозов и лекарств).
Ничего не сохраняется и не логируется: ни значения анализов, ни ответы.

Зависимости — только то, что уже есть в образе DeficitLens: Starlette, uvicorn и стандартная библиотека
(запрос к API — urllib.request, без httpx и requests).

Запуск локально (API DeficitLens уже поднят на 127.0.0.1:8080, демо-ключ dl-demo-key):
    DL_URL=http://127.0.0.1:8080 DL_API_KEY=dl-demo-key \\
        python -m uvicorn app:app --app-dir examples/clinic_site --host 127.0.0.1 --port 8081 --no-access-log
Откройте http://127.0.0.1:8081
"""
from __future__ import annotations

import json
import math
import os
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Optional

from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles
from starlette.types import ASGIApp, Message, Receive, Scope, Send

STATIC = Path(__file__).resolve().parent / "static"
TIMEOUT_S = 30.0                       # ожидание ответа DeficitLens, секунд
MAX_BODY = 16 * 1024                   # форма ОАК — несколько чисел; больше 16 КБ не бывает

# Строгая CSP: только свои скрипты и стили, без inline; fetch и форма — только на свой origin.
SECURITY_HEADERS = {
    "content-security-policy": "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; "
                               "img-src 'self'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'",
    "x-content-type-options": "nosniff",
    "referrer-policy": "no-referrer",
    "x-frame-options": "DENY",
    "cache-control": "no-store",
}

# Поля формы → коды показателей DeficitLens и допустимые значения. Единицы — канонические единицы DeficitLens
# (config/analytes.yaml): г/л, 10^12/л, %, фл, пг, г/л, %, 10^9/л, 10^9/л, мкг/л. Здесь только грубая защита
# от опечаток («1040» вместо «104» API примет как значение вне диапазона и объяснит сам); пороги — в DeficitLens.
CBC_FIELDS: dict[str, tuple[float, float]] = {
    "hemoglobin": (0, 1000), "RBC": (0, 100), "hematocrit": (0, 100), "MCV": (0, 1000), "MCH": (0, 1000),
    "MCHC": (0, 1000), "RDW": (0, 100), "platelets": (0, 10000), "WBC": (0, 1000), "ferritin": (0, 1_000_000),
}
FORM_KEYS = {"sex", "age_years", "pregnant", *CBC_FIELDS}
FORM_ERROR = "Проверьте форму: пол, возраст от 18 лет и гемоглобин обязательны, значения — числа"


class FormError(ValueError):
    """Форма заполнена неверно: сообщение показывается пациенту как есть."""


class BodyTooLarge(Exception):
    """Тело запроса больше MAX_BODY."""


def _number(raw: Any, lo: float, hi: float) -> Optional[float]:
    """Число из формы: пусто → None; запятая как десятичный разделитель допустима; вне (lo, hi) — ошибка."""
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return None
    if isinstance(raw, bool):
        raise FormError(FORM_ERROR)
    try:
        v = float(str(raw).strip().replace(",", "."))
    except ValueError:
        raise FormError(FORM_ERROR) from None
    if not math.isfinite(v) or not lo < v < hi:
        raise FormError(FORM_ERROR)
    return v


def build_payload(form: Any) -> dict:
    """Форма страницы → тело POST /api/v1/analyze (схема AnalysisInput из docs/CONTRACTS.md)."""
    if not isinstance(form, dict) or set(form) - FORM_KEYS:
        raise FormError(FORM_ERROR)
    sex = form.get("sex")
    if sex not in ("F", "M"):
        raise FormError("Выберите пол")
    age = _number(form.get("age_years"), 0, 1000)
    if age is None or age != int(age) or not 18 <= age <= 120:
        raise FormError("Возраст — целое число лет от 18 до 120: расшифровка рассчитана только на взрослых")
    values = {}
    for code, (lo, hi) in CBC_FIELDS.items():
        v = _number(form.get(code), lo, hi)
        if v is not None:
            values[code] = v
    if "hemoglobin" not in values:
        raise FormError("Укажите гемоглобин")
    pregnant = form.get("pregnant") is True and sex == "F"
    return {"sex": sex, "age_years": int(age), "values": values,
            "pregnancy": {"status": "yes" if pregnant else "no"},
            "provenance": {"source": "api", "client": "clinic_site"}}


def call_deficitlens(payload: dict, base_url: str, api_key: str, timeout: float = TIMEOUT_S) -> tuple[int, dict]:
    """POST {base_url}/api/v1/analyze с ключом клиники. Возвращает (код HTTP, JSON); 0 — сервис недоступен."""
    req = urllib.request.Request(
        base_url.rstrip("/") + "/api/v1/analyze", data=json.dumps(payload).encode("utf-8"), method="POST",
        headers={"Content-Type": "application/json", "Accept": "application/json", "X-API-Key": api_key})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            status, raw = r.status, r.read()
    except urllib.error.HTTPError as e:
        status, raw = e.code, e.read()
    except (urllib.error.URLError, OSError, TimeoutError):
        return 0, {}
    try:
        body = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        body = {}
    return status, body if isinstance(body, dict) else {}


class SecurityHeaders:
    """Заголовки безопасности на каждый ответ (чистое ASGI-промежуточное ПО, без буферизации тела)."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = [(k, v) for k, v in message.get("headers", [])
                           if k.decode("latin-1").lower() not in SECURITY_HEADERS]
                headers += [(k.encode("latin-1"), v.encode("latin-1")) for k, v in SECURITY_HEADERS.items()]
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, send_with_headers)


def _fail(status: int, message: str) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status)


async def _read_body(request: Request) -> bytes:
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > MAX_BODY:
        raise BodyTooLarge
    chunks, size = [], 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > MAX_BODY:
            raise BodyTooLarge
        chunks.append(chunk)
    return b"".join(chunks)


def create_app(base_url: Optional[str] = None, api_key: Optional[str] = None) -> Starlette:
    """Приложение сайта клиники. Адрес и ключ DeficitLens — из аргументов или окружения (DL_URL, DL_API_KEY)."""
    dl_url = base_url or os.environ.get("DL_URL", "http://127.0.0.1:8080")
    # Демо-ключ из docker-compose.yml; в реальной клинике — свой, из секрета окружения.
    dl_key = api_key or os.environ.get("DL_API_KEY", "dl-demo-key")

    async def index(request: Request) -> Response:
        return FileResponse(STATIC / "index.html", media_type="text/html; charset=utf-8")

    async def healthz(request: Request) -> Response:
        return JSONResponse({"status": "ok"})

    async def report(request: Request) -> Response:
        """Значения формы → DeficitLens /api/v1/analyze (с ключом клиники) → только отчёт пациенту."""
        if "application/json" not in request.headers.get("content-type", ""):
            return _fail(415, "Ожидается JSON")
        try:
            form = json.loads((await _read_body(request)).decode("utf-8"))
            payload = build_payload(form)
        except BodyTooLarge:
            return _fail(413, "Слишком большой запрос")
        except FormError as e:
            return _fail(422, str(e))
        except (UnicodeDecodeError, ValueError):
            return _fail(422, FORM_ERROR)
        status, body = await run_in_threadpool(call_deficitlens, payload, dl_url, dl_key)
        if status == 0:
            return _fail(502, "Сервис расшифровки недоступен — попробуйте позже")
        if status == 422:
            # Сообщение DeficitLens по-русски и без ПДн — его можно показать пациенту.
            err = body.get("error") if isinstance(body.get("error"), dict) else {}
            return _fail(422, str(err.get("message") or "Проверьте введённые значения"))
        patient = (body.get("reports") or {}).get("patient") if status == 200 else None
        if not isinstance(patient, dict):
            # 401/403 (ключ клиники не принят), 429, 5xx — пациенту без подробностей.
            return _fail(502, "Сервис расшифровки ответил ошибкой — попробуйте позже")
        return JSONResponse({"patient": patient})

    routes = [
        Route("/", index, methods=["GET"]),
        Route("/healthz", healthz, methods=["GET"]),
        Route("/api/report", report, methods=["POST"]),
        Mount("/static", app=StaticFiles(directory=str(STATIC)), name="static"),
    ]
    app = Starlette(routes=routes)
    app.add_middleware(SecurityHeaders)
    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=os.environ.get("CLINIC_HOST", "127.0.0.1"), port=int(os.environ.get("CLINIC_PORT", "8081")),
                access_log=False, server_header=False)
