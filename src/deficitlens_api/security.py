"""Безопасность веб-слоя: ключ API, лимит запросов страницы демо, пределы размера, заголовки, журнал.

  * ключ `X-API-Key` сравнивается через hmac.compare_digest (время сравнения не зависит от места расхождения);
    нет ключа — 401, неверный — 403;
  * лимит запросов для POST /ui/* — скользящее окно 60 с на адрес клиента, в памяти процесса; превышение — 429
    и заголовок Retry-After (GET /ui/reference и /ui/examples — статичный справочник: без лимита, с кешем и ETag);
  * тяжёлые маршруты (benchmark, parse файла, batch): не больше HEAVY_CONCURRENCY расчётов одновременно на процесс
    и не больше HEAVY_PER_CLIENT на адрес клиента — иначе 503 service_busy с Retry-After; для /api/v1/* ещё и лимит
    тяжёлых запросов в минуту на ключ (демо-ключ публичен — без этого /api/v1 обходил бы лимит /ui). Ключ в памяти
    лимитера хранится только как хеш;
  * тело запроса читается потоком с пределом размера: больше предела — 413;
  * на каждом ответе — заголовки безопасности (CSP без inline, nosniff, no-referrer; HSTS — при DL_COOKIE_SECURE=1),
    на ответах с результатами — `Cache-Control: no-store`; статика с меткой версии (?v=) — на год (immutable),
    шрифты — на неделю, остальное — no-cache (браузер сверяет ETag);
  * сжатие gzip — только для статики, страниц и описания API (GZipByPathMiddleware): ответы с персональными данными
    (/api/v1/me, /auth, /ui/* с расчётами) не сжимаются — защита от BREACH;
  * журнал: время, метод, путь, код, длительность. Тела запросов, строка запроса и значения анализов
    в журнал не попадают; трассировки наружу не отдаются;
  * CORS для /api/v1/* (кабинет, ADR 0009): Access-Control-Allow-Origin — только источникам из DL_CORS_ORIGINS,
    без Access-Control-Allow-Credentials (cookie сайта чужим страницам не отдаётся; внешние сайты — Bearer или
    X-API-Key); предварительный запрос OPTIONS: методы GET, POST, PATCH, DELETE; заголовки Authorization, Content-Type,
    X-API-Key. Чужой источник — 403 без заголовков CORS;
  * адрес клиента для лимитов: адрес соединения; X-Forwarded-For / -Proto / -Host — только от доверенного прокси
    из DL_TRUSTED_PROXIES (по умолчанию никому не доверяем). IPv6 считается по сети /64 (у одного абонента
    обычно целая /64 — иначе каждый адрес из неё был бы «новым клиентом»);
  * вход: кроме окна «адрес + логин» — общий счётчик неудач на логин с любых адресов (LoginGuard): после 10
    неудач за 15 мин — пауза 1 мин, каждая следующая вдвое длиннее, не больше 15 мин; навсегда не блокирует.
"""
from __future__ import annotations

import contextvars
import hashlib
import hmac
import ipaddress
import json
import logging
import math
import re
import threading
import time
from collections import OrderedDict, deque
from contextlib import asynccontextmanager, contextmanager
from typing import Any, AsyncIterator, Iterable, Iterator, Optional

import anyio

from .service import ServiceError

LOGGER_NAME = "deficitlens.access"
log = logging.getLogger(LOGGER_NAME)

# Подложка карты «Где сдать рядом» — картинки с tile.openstreetmap.org (ADR 0008): браузер грузит их только после
# нажатия «Показать на карте». Скрипты, стили и данные — только свои ('self'); координаты пользователя не уходят.
# Подложка — OpenStreetMap (CARTO light без ключа API отдаёт заглушку «API KEY REQUIRED», проверено 04.10.2026;
# с ключом — добавить сюда https://*.basemaps.cartocdn.com и поменять TILES в static/app.js). Авторство — под картой.
TILES = "https://tile.openstreetmap.org"
CSP = (f"default-src 'self'; img-src 'self' data: {TILES}; frame-ancestors 'none'; base-uri 'none'; "
       "form-action 'self'")
SECURITY_HEADERS = {
    "content-security-policy": CSP,
    "x-content-type-options": "nosniff",
    "referrer-policy": "no-referrer",
    "x-frame-options": "DENY",
    "cross-origin-resource-policy": "same-origin",
    # геолокация — только для своей страницы («Определить моё положение»); результат остаётся в браузере
    "permissions-policy": "camera=(), microphone=(), geolocation=(self)",
}
NO_STORE_PREFIXES = ("/api/", "/ui/", "/healthz")
# Кеш статики: адрес с меткой версии (/static/app.js?v=…) меняется вместе с файлом — его можно хранить год
# и не перепроверять; шрифты (их адреса задаёт app.css, без метки) — неделю; остальное — no-cache (сверка ETag).
CACHE_IMMUTABLE = "public, max-age=31536000, immutable"
CACHE_FONTS = "public, max-age=604800"
HSTS = "max-age=31536000"
_VERSION_QUERY = re.compile(rb"(?:^|&)v=[^&]+")


def cache_control_for(path: str, query: bytes, status: int) -> str:
    """Cache-Control по умолчанию (если ответ не задал свой): no-store для данных (/api, /ui, /healthz);
    долгий кеш — только для успешных ответов статики; остальное — no-cache."""
    if path.startswith(NO_STORE_PREFIXES):
        return "no-store"
    if path.startswith("/static/") and status in (200, 304):
        if path.startswith("/static/fonts/"):
            return CACHE_FONTS
        if _VERSION_QUERY.search(query or b""):
            return CACHE_IMMUTABLE
    return "no-cache"


def setup_logging() -> None:
    """Журнал доступа в stderr: «время метод путь код длительность». Настраивается один раз."""
    logger = logging.getLogger("deficitlens")
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        logger.propagate = False


# --------------------------------------------------------------------------------------------
# Ключ API
# --------------------------------------------------------------------------------------------
def _digest(value: str) -> bytes:
    return hashlib.sha256(value.encode("utf-8", "surrogateescape")).digest()


def check_api_key(presented: Optional[str], keys: Iterable[str]) -> None:
    """Нет ключа — 401; ключ не из списка — 403. Сравниваются SHA-256 ключей через hmac.compare_digest:
    время сравнения не зависит ни от места расхождения, ни от длины ключа; проверяются все ключи, без раннего выхода."""
    if not presented:
        raise ServiceError(401, "unauthorized", "Нужен ключ API (заголовок X-API-Key) или токен кабинета "
                                                "(Authorization: Bearer <токен>).")
    given = _digest(presented)
    ok = False
    for key in keys:
        ok = hmac.compare_digest(given, _digest(key)) | ok
    if not ok:
        raise ServiceError(403, "forbidden", "Ключ API не подходит.")


def key_id(presented: Optional[str]) -> str:
    """Метка ключа для лимитера: первые 16 знаков SHA-256 — сам ключ в памяти лимитера не хранится."""
    return "key:" + hashlib.sha256((presented or "").strip().encode("utf-8", "surrogateescape")).hexdigest()[:16]


# --------------------------------------------------------------------------------------------
# Лимит запросов: скользящее окно в памяти
# --------------------------------------------------------------------------------------------
class RateLimiter:
    """Не больше `limit` запросов за `window` секунд с одного адреса. Хранит только адреса и отметки времени."""

    def __init__(self, limit: int, window: float = 60.0, max_clients: int = 20000):
        self.limit, self.window, self.max_clients = int(limit), float(window), max_clients
        self._hits: OrderedDict[str, deque] = OrderedDict()     # порядок — от давно не обращавшихся к недавним
        self._lock = threading.Lock()

    def check(self, client: str, now: Optional[float] = None) -> Optional[int]:
        """None — запрос разрешён и учтён; иначе — через сколько секунд повторить (для Retry-After)."""
        now = time.monotonic() if now is None else now
        with self._lock:
            q = self._hits.get(client)
            if q is None:
                if len(self._hits) >= self.max_clients:
                    self._purge(now)
                q = self._hits[client] = deque()
            else:
                self._hits.move_to_end(client)
            while q and now - q[0] >= self.window:
                q.popleft()
            if len(q) >= self.limit:
                return max(1, int(self.window - (now - q[0])) + 1)
            q.append(now)
            return None

    def release(self, client: str) -> None:
        """Вернуть последнюю учтённую попытку: действие не состоялось (например, регистрация не прошла проверку
        полей или логин занят) — в лимит «успешных» действий она не идёт."""
        with self._lock:
            q = self._hits.get(client)
            if q:
                q.pop()

    def _purge(self, now: float) -> None:
        """Под наплывом новых адресов: сначала удаляются ключи без отметок в окне (устаревшие). Если и так места
        нет — самые давно не обращавшиеся (LRU) до 90 % предела. Счётчики активных адресов не сбрасываются
        (раньше при переполнении сбрасывался весь лимитер — и с ним окна тех, кто уже упёрся в лимит)."""
        for key in [k for k, q in self._hits.items() if not q or now - q[-1] >= self.window]:
            del self._hits[key]
        target = max(0, int(self.max_clients * 0.9))
        while len(self._hits) > target:
            self._hits.popitem(last=False)


# --------------------------------------------------------------------------------------------
# Общий счётчик неудачных входов на логин (с любых адресов)
# --------------------------------------------------------------------------------------------
GUARD_FAILURES = 10           # неудач за окно — и логин на паузе
GUARD_WINDOW = 15 * 60.0      # окно счёта неудач, с
GUARD_BASE_LOCK = 60.0        # первая пауза, с; дальше вдвое длиннее
GUARD_MAX_LOCK = 15 * 60.0    # самая длинная пауза, с


class LoginGuard:
    """Защита от перебора пароля одного логина с многих адресов (лимит «адрес + логин» её не ловит).

    Ключ — начало SHA-256 логина (сам логин в памяти не хранится), счёт ведётся и для несуществующих логинов —
    по ответу нельзя узнать, есть ли логин. После GUARD_FAILURES неудач за GUARD_WINDOW логин на паузе
    GUARD_BASE_LOCK секунд: на паузе любой вход в него (и с верным паролем) — 429 с Retry-After. Каждая следующая
    пауза вдвое длиннее, не больше GUARD_MAX_LOCK; после окна без неудач ступень сбрасывается, удачный вход
    сбрасывает счёт. Навсегда логин не блокируется. Цена — злоумышленник может держать чужой логин на паузе
    (до 15 мин за раз), пока перебирает; уже открытые сессии и токены при этом работают."""

    def __init__(self, failures: int = GUARD_FAILURES, window: float = GUARD_WINDOW, base: float = GUARD_BASE_LOCK,
                 cap: float = GUARD_MAX_LOCK, max_keys: int = 20000):
        self.failures, self.window, self.base, self.cap, self.max_keys = int(failures), window, base, cap, max_keys
        self._state: OrderedDict[str, dict] = OrderedDict()
        self._lock = threading.Lock()

    def check(self, key: str, now: Optional[float] = None) -> Optional[int]:
        """None — вход можно проверять; иначе — через сколько секунд повторить."""
        now = time.monotonic() if now is None else now
        with self._lock:
            st = self._state.get(key)
            if st is None or now >= st["until"]:
                return None
            return max(1, math.ceil(st["until"] - now))

    def failure(self, key: str, now: Optional[float] = None) -> None:
        now = time.monotonic() if now is None else now
        with self._lock:
            st = self._state.get(key)
            if st is None:
                if len(self._state) >= self.max_keys:
                    self._purge(now)
                st = self._state[key] = {"hits": deque(), "until": 0.0, "level": 0, "last": now}
            else:
                self._state.move_to_end(key)
            if now - max(st["last"], st["until"]) >= self.window:
                st["level"] = 0                  # окно без неудач после конца паузы — ступень паузы сначала
            st["last"] = now
            q = st["hits"]
            while q and now - q[0] >= self.window:
                q.popleft()
            q.append(now)
            if len(q) >= self.failures:
                st["level"] += 1
                st["until"] = now + min(self.base * 2 ** (st["level"] - 1), self.cap)
                q.clear()

    def success(self, key: str) -> None:
        with self._lock:
            self._state.pop(key, None)

    def _purge(self, now: float) -> None:
        for key in [k for k, st in self._state.items() if now - max(st["last"], st["until"]) >= self.window]:
            del self._state[key]
        target = max(0, int(self.max_keys * 0.9))
        while len(self._state) > target:
            self._state.popitem(last=False)


# --------------------------------------------------------------------------------------------
# Адрес клиента и доверенный прокси
# --------------------------------------------------------------------------------------------
def client_key(host: Optional[str]) -> str:
    """Ключ лимитера по адресу: IPv4 — как есть; IPv6 — сеть /64 (провайдер выдаёт абоненту целую /64, и без
    этого каждый адрес из неё считался бы отдельным клиентом); IPv4 в IPv6 (::ffff:a.b.c.d) — как IPv4."""
    if not host:
        return "unknown"
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return str(host)[:64]
    if ip.version == 6:
        if ip.ipv4_mapped is not None:
            return str(ip.ipv4_mapped)
        return str(ipaddress.ip_network(f"{ip}/64", strict=False))
    return str(ip)


def request_client(request) -> str:
    """Ключ лимитера для запроса Starlette (адрес уже исправлен ProxyHeadersMiddleware, если прокси доверенный)."""
    return client_key(request.client.host if request.client else None)


def parse_networks(items: Iterable[str]) -> tuple:
    """«10.0.0.1, 172.16.0.0/12, ::1» -> сети ipaddress. Неверная запись — ValueError (опечатка в настройке
    безопасности должна остановить запуск, а не молча выключить проверку)."""
    out = []
    for item in items:
        item = item.strip()
        if not item:
            continue
        try:
            out.append(ipaddress.ip_network(item, strict=False))
        except ValueError:
            raise ValueError(f"DL_TRUSTED_PROXIES: «{item}» — не адрес и не сеть (пример: 127.0.0.1, 10.0.0.0/8)") \
                from None
    return tuple(out)


_HOST_RE = re.compile(r"[A-Za-z0-9.\-]{1,253}(:[0-9]{1,5})?|\[[0-9A-Fa-f:.]{2,45}\](:[0-9]{1,5})?")


def _strip_port(value: str) -> str:
    """Запись адреса из X-Forwarded-For без порта и скобок: «[2001:db8::1]:443» -> «2001:db8::1»,
    «203.0.113.5:1234» -> «203.0.113.5»."""
    value = value.strip()
    if value.startswith("["):
        return value[1:value.find("]")] if "]" in value else value
    if value.count(":") == 1:
        return value.split(":", 1)[0]
    return value


class ProxyHeadersMiddleware:
    """X-Forwarded-For / -Proto / -Host принимаются, только если соединение пришло от доверенного прокси
    (DL_TRUSTED_PROXIES; по умолчанию список пуст — заголовки игнорируются: подделать адрес для лимитов нельзя).

    Адрес клиента — первый справа адрес X-Forwarded-For, который сам не доверенный прокси (правые записи дописаны
    нашими прокси, левые мог прислать кто угодно). Proto и Host — последняя запись (её поставил ближайший прокси).
    Proto — только http или https; Host — только имя или адрес с портом. Uvicorn свои заголовки прокси не
    разбирает (запуск с --no-proxy-headers): иначе он доверял бы X-Forwarded-For от 127.0.0.1."""

    def __init__(self, app, trusted: Iterable = ()):
        self.app = app
        self.networks = tuple(trusted)

    def trusted(self, host: Optional[str]) -> bool:
        if not host or not self.networks:
            return False
        try:
            ip = ipaddress.ip_address(_strip_port(host))
        except ValueError:
            return False
        if ip.version == 6 and ip.ipv4_mapped is not None:
            ip = ip.ipv4_mapped
        return any(ip.version == n.version and ip in n for n in self.networks)

    async def __call__(self, scope, receive, send):
        if scope["type"] in ("http", "websocket") and self.networks:
            client = scope.get("client")
            if client and self.trusted(client[0]):
                scope = self._apply(scope)
        await self.app(scope, receive, send)

    def _apply(self, scope: dict) -> dict:
        values: dict[bytes, list[str]] = {}
        for k, v in scope.get("headers", []):
            values.setdefault(k.lower(), []).append(v.decode("latin-1"))

        def items(name: bytes) -> list[str]:
            return [x.strip() for x in ",".join(values.get(name, [])).split(",") if x.strip()]

        scope = dict(scope)
        chain = items(b"x-forwarded-for")
        if chain:
            real = None
            for entry in reversed(chain):
                if not self.trusted(entry):
                    real = entry
                    break
            real = _strip_port(real if real is not None else chain[0])
            try:
                ipaddress.ip_address(real)
            except ValueError:
                real = None                                  # мусор вместо адреса — остаётся адрес соединения
            if real is not None:
                scope["client"] = (real, 0)
        proto = items(b"x-forwarded-proto")
        if proto and proto[-1].lower() in ("http", "https"):
            scope["scheme"] = proto[-1].lower()
        host = items(b"x-forwarded-host")
        if host and _HOST_RE.fullmatch(host[-1]):
            headers = [(k, v) for k, v in scope.get("headers", []) if k.lower() != b"host"]
            scope["headers"] = [(b"host", host[-1].encode("latin-1"))] + headers
        return scope


# --------------------------------------------------------------------------------------------
# Разбор JSON тела
# --------------------------------------------------------------------------------------------
MAX_JSON_DEPTH = 32


def _depth_ok(value: Any, limit: int) -> bool:
    """Вложенность объектов и списков не глубже limit (обход без рекурсии)."""
    stack = [(value, 1)]
    while stack:
        node, depth = stack.pop()
        if isinstance(node, dict):
            children = node.values()
        elif isinstance(node, list):
            children = node
        else:
            continue
        if depth > limit:
            return False
        stack.extend((child, depth + 1) for child in children if isinstance(child, (dict, list)))
    return True


def parse_json_body(body: bytes) -> Any:
    """Тело -> JSON. Не UTF-8, не JSON, вложенность глубже MAX_JSON_DEPTH (в том числе такая, на которой
    разборщик падает с RecursionError) — 422 invalid_json, а не 500."""
    err = ServiceError(422, "invalid_json", "Тело запроса должно быть корректным JSON в кодировке UTF-8 "
                                            f"с вложенностью не глубже {MAX_JSON_DEPTH}.")
    try:
        data = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise err from None
    if not _depth_ok(data, MAX_JSON_DEPTH):
        raise err
    return data


def rate_limit_error(retry_after: int, message: Optional[str] = None) -> ServiceError:
    err = ServiceError(429, "rate_limited",
                       message or f"Слишком много запросов с вашего адреса. Повторите через {retry_after} с.")
    err.retry_after = retry_after  # type: ignore[attr-defined]
    return err


# --------------------------------------------------------------------------------------------
# Тяжёлые расчёты: не больше N одновременно на процесс
# --------------------------------------------------------------------------------------------
HEAVY_CONCURRENCY = 2
HEAVY_PER_CLIENT = 1          # тяжёлых расчётов одновременно с одного адреса (IPv6 — сети /64)
CLIENT_WAIT_SECONDS = 6.0     # сколько второй запрос того же адреса ждёт, пока закончится его первый
BUSY_RETRY_AFTER = 5

# Адрес клиента текущего запроса (ставит обёртка маршрутов app.py): HeavyGate.slot() без аргумента берёт его
# отсюда — так один слот на адрес действует и для расчётов кабинета (история, загрузка), которые вызывают slot().
current_client: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar("deficitlens_client", default=None)


def _busy(own: bool = False) -> ServiceError:
    if own:
        err = ServiceError(503, "service_busy", "Ваш предыдущий файл ещё обрабатывается: дождитесь результата "
                                                "и повторите.")
    else:
        err = ServiceError(503, "service_busy",
                           "Сервис занят обработкой других файлов. Повторите через несколько секунд.")
    err.retry_after = BUSY_RETRY_AFTER  # type: ignore[attr-defined]
    return err


class HeavyGate:
    """Слоты для тяжёлых расчётов (файл, пакет, PDF): не больше `slots` на процесс и не больше `per_client` на адрес
    клиента. Общий слот берётся без ожидания: свободного нет — 503 service_busy с Retry-After. Слот занимается
    только на время расчёта (после чтения тела): медленная загрузка не держит слот.

    Один клиент не может занять все слоты (например, двумя битыми PDF подряд): второй его запрос в slot() сразу
    получает 503, а в aslot() сначала ждёт до `client_wait` секунд, пока закончится первый, — отказ получает только
    он сам, остальные клиенты работают. В памяти — только адреса с идущими сейчас расчётами."""

    def __init__(self, slots: int = HEAVY_CONCURRENCY, per_client: int = HEAVY_PER_CLIENT,
                 client_wait: float = CLIENT_WAIT_SECONDS):
        self.slots = int(slots)
        self.per_client = max(1, int(per_client))
        self.client_wait = float(client_wait)
        self._sem = threading.BoundedSemaphore(self.slots)
        self._lock = threading.Lock()
        self._active: dict[str, int] = {}

    def _take_client(self, client: Optional[str]) -> bool:
        if client is None:
            return True
        with self._lock:
            n = self._active.get(client, 0)
            if n >= self.per_client:
                return False
            self._active[client] = n + 1
            return True

    def _release_client(self, client: Optional[str]) -> None:
        if client is None:
            return
        with self._lock:
            n = self._active.get(client, 0) - 1
            if n > 0:
                self._active[client] = n
            else:
                self._active.pop(client, None)

    def _take_slot(self, client: Optional[str]) -> None:
        if not self._sem.acquire(blocking=False):
            self._release_client(client)
            raise _busy()

    def _release(self, client: Optional[str]) -> None:
        self._sem.release()
        self._release_client(client)

    @contextmanager
    def slot(self, client: Optional[str] = None) -> Iterator[None]:
        """Без ожидания. client=None — адрес текущего запроса (current_client); вне запроса — без учёта адреса."""
        client = client if client is not None else current_client.get()
        if not self._take_client(client):
            raise _busy(own=True)
        self._take_slot(client)
        try:
            yield
        finally:
            self._release(client)

    @asynccontextmanager
    async def aslot(self, client: Optional[str] = None) -> AsyncIterator[None]:
        """То же, но второй запрос того же адреса ждёт (не дольше client_wait), пока закончится первый."""
        client = client if client is not None else current_client.get()
        deadline = time.monotonic() + self.client_wait
        while not self._take_client(client):
            if time.monotonic() >= deadline:
                raise _busy(own=True)
            await anyio.sleep(0.05)
        self._take_slot(client)
        try:
            yield
        finally:
            self._release(client)


# --------------------------------------------------------------------------------------------
# Предел размера тела
# --------------------------------------------------------------------------------------------
async def read_body(request, limit: int) -> bytes:
    """Читает тело запроса потоком; больше `limit` байт — 413 (проверяется и Content-Length, и фактический размер)."""
    too_large = ServiceError(413, "payload_too_large",
                             f"Слишком большой запрос: допустимо не больше {_human(limit)}.")
    declared = request.headers.get("content-length")
    if declared:
        try:
            if int(declared) > limit:
                raise too_large
        except ValueError:
            raise ServiceError(400, "bad_request", "Некорректный заголовок Content-Length.") from None
    chunks, total = [], 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > limit:
            raise too_large
        chunks.append(chunk)
    return b"".join(chunks)


def _human(n: int) -> str:
    return f"{n // (1024 * 1024)} МБ" if n >= 1024 * 1024 else f"{max(1, n // 1024)} КБ"


# --------------------------------------------------------------------------------------------
# Заголовки безопасности и журнал (ASGI-обёртка)
# --------------------------------------------------------------------------------------------
class SecurityMiddleware:
    """Добавляет заголовки безопасности к каждому ответу и пишет строку журнала без тел и значений.
    hsts=True (сайт за HTTPS, DL_COOKIE_SECURE=1) — ещё Strict-Transport-Security: браузер год ходит только по HTTPS."""

    def __init__(self, app, hsts: bool = False):
        self.app = app
        self.headers = [(k.encode(), v.encode()) for k, v in SECURITY_HEADERS.items()]
        if hsts:
            self.headers.append((b"strict-transport-security", HSTS.encode()))

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")
        method = scope.get("method", "")
        t0 = time.perf_counter()
        status = {"code": 500}

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                status["code"] = message["status"]
                present = {k.lower() for k, _ in message.get("headers", [])}
                headers = list(message.get("headers", []))
                headers.extend((k, v) for k, v in self.headers if k not in present)
                if b"cache-control" not in present:
                    cache = cache_control_for(path, scope.get("query_string", b""), message["status"])
                    headers.append((b"cache-control", cache.encode()))
                message = {**message, "headers": headers}
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            # Только путь (без строки запроса): в журнале не должно быть ни значений анализов, ни ПДн. Путь — через
            # repr: перевод строки или управляющий символ в пути (%0a) не подделает отдельную строку журнала.
            log.info("%s %r %d %.1fms", method, path[:200], status["code"], 1000 * (time.perf_counter() - t0))


# --------------------------------------------------------------------------------------------
# CORS для /api/v1/* (ADR 0009): только перечисленные источники, без credentials
# --------------------------------------------------------------------------------------------
CORS_PREFIX = "/api/v1/"
CORS_METHODS = "GET, POST, PATCH, DELETE"
CORS_HEADERS = "Authorization, Content-Type, X-API-Key"
CORS_EXPOSE = "Retry-After, Content-Disposition"
CORS_MAX_AGE = "600"


class CorsMiddleware:
    """Ответы /api/v1/* для страниц других сайтов из списка DL_CORS_ORIGINS.

    Источник сравнивается точно («схема://хост[:порт]», без завершающей «/»); «*» не поддерживается.
    Access-Control-Allow-Credentials не ставится никогда: браузер не пришлёт cookie сайта на чужой странице и не
    отдаст ей ответ на запрос с cookie — внешние сайты авторизуются заголовком Authorization: Bearer или X-API-Key.
    Предварительный запрос (OPTIONS с Access-Control-Request-Method) от источника из списка — 204 с разрешёнными
    методами и заголовками; от чужого — 403 без заголовков CORS. Остальные пути не затрагиваются."""

    def __init__(self, app, origins: Iterable[str] = ()):
        self.app = app
        self.origins = frozenset(o.strip().rstrip("/") for o in origins if o and o.strip())

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not scope.get("path", "").startswith(CORS_PREFIX):
            await self.app(scope, receive, send)
            return
        headers = {k.lower(): v for k, v in scope.get("headers", [])}
        raw_origin = headers.get(b"origin")
        if raw_origin is None:
            await self.app(scope, receive, send)
            return
        origin = raw_origin.decode("latin-1").strip()
        allowed = origin in self.origins
        if scope.get("method") == "OPTIONS" and b"access-control-request-method" in headers:
            if allowed:
                await _send_plain(send, 204, b"", [
                    (b"access-control-allow-origin", origin.encode("latin-1")), (b"vary", b"Origin"),
                    (b"access-control-allow-methods", CORS_METHODS.encode()),
                    (b"access-control-allow-headers", CORS_HEADERS.encode()),
                    (b"access-control-max-age", CORS_MAX_AGE.encode())])
            else:
                body = ('{"error":{"code":"cors_forbidden","message":"Запросы с этого сайта не разрешены '
                        '(DL_CORS_ORIGINS).","field":null}}').encode()
                await _send_plain(send, 403, body, [(b"content-type", b"application/json"), (b"vary", b"Origin")])
            return

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                extra = [(b"vary", b"Origin")]
                if allowed:
                    extra += [(b"access-control-allow-origin", origin.encode("latin-1")),
                              (b"access-control-expose-headers", CORS_EXPOSE.encode())]
                message = {**message, "headers": list(message.get("headers", [])) + extra}
            await send(message)

        await self.app(scope, receive, send_wrapper)


# --------------------------------------------------------------------------------------------
# Сжатие gzip — только для ответов без персональных данных (BREACH)
# --------------------------------------------------------------------------------------------
# Сжимаются: статика, HTML-страницы (в разметке нет ни данных, ни секретов: cookie сессии — только флаг «есть»),
# описание API и статичный справочник страницы. Не сжимаются: /api/v1/me*, /auth*, /doctor*, /shares*, расчёты
# /api/v1/* и /ui/* — ответ с данными пользователя рядом с тем, что может подставить чужая страница, при сжатии
# выдаёт содержимое по длине ответа (BREACH). Поэтому список разрешённых путей, а не запрещённых.
GZIP_PREFIXES = ("/static/",)
GZIP_PATHS = frozenset({"/", "/benchmark", "/developers", "/login", "/cabinet", "/share",
                        "/api/v1/openapi.json", "/ui/reference", "/ui/examples"})
GZIP_MIN_SIZE = 1024
GZIP_LEVEL = 6                # уровень 9 медленнее в 2–3 раза, а файл меньше на 1–2 %


def gzip_allowed(path: str) -> bool:
    return path in GZIP_PATHS or path.startswith(GZIP_PREFIXES)


class GZipByPathMiddleware:
    """GZipMiddleware Starlette только для путей из списка gzip_allowed (картинки и woff2 он сам не сжимает)."""

    def __init__(self, app, minimum_size: int = GZIP_MIN_SIZE, compresslevel: int = GZIP_LEVEL):
        from starlette.middleware.gzip import GZipMiddleware

        self.app = app
        self.gzip = GZipMiddleware(app, minimum_size=minimum_size, compresslevel=compresslevel)

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and gzip_allowed(scope.get("path", "")):
            await self.gzip(scope, receive, send)
        else:
            await self.app(scope, receive, send)


async def _send_plain(send, status: int, body: bytes, headers: list) -> None:
    await send({"type": "http.response.start", "status": status,
                "headers": headers + [(b"content-length", str(len(body)).encode())]})
    await send({"type": "http.response.body", "body": body})
