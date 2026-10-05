"""Вход в личный кабинет (ADR 0009): пароли, токены, сессии, CSRF.

  * Пароль — hashlib.scrypt (n=2^14, r=8, p=1, 32 байта, соль 16 байт), формат `scrypt$n$r$p$соль$хэш`
    (base64), сравнение — hmac.compare_digest. Для неизвестного логина считается хэш фиктивного пароля: ответ
    «Неверный логин или пароль» одинаков и по тексту, и по времени.
  * Логин — 3–64 символа: латиница, кириллица, цифры и «._-@»; поиск без учёта регистра. При регистрации латиница
    и кириллица в одном логине не смешиваются (иначе «ivan» и «іvаn» с кириллическими буквами — два разных логина,
    которые выглядят одинаково). Пароль — 8–128 символов.
  * Токены — secrets.token_urlsafe(32); в базе только SHA-256. Cookie сайта `jm_session` (за HTTPS при
    DL_COOKIE_SECURE=1 — `__Host-jm_session` с Secure; HttpOnly, SameSite=Strict, Path=/, 7 дней). Токен входа
    «Вход» живёт столько же, сколько cookie этого входа (7 дней), именованный токен API — 30 дней (созданный по
    Bearer-токену — не дольше этого токена). Cookie принимается только как cookie, токен API — только как Bearer.
  * CSRF: изменяющий запрос (POST, PUT, PATCH, DELETE), авторизованный cookie, обязан нести заголовок
    `X-Justmedit: 1` (чужой сайт не может его добавить без разрешения CORS, а CORS для cookie выключен) и, если
    браузер прислал Origin (или Referer), — свой источник: схема, хост и порт совпадают с адресом запроса (схема
    и хост — из X-Forwarded-Proto / -Host только от доверенного прокси, DL_TRUSTED_PROXIES). Исключение одно:
    запрос пришёл по HTTP, а Origin — https с тем же хостом (TLS снят прокси, который не объявлен доверенным).
    Origin «null» (страница с Referrer-Policy: no-referrer, песочница) считается отсутствующим: защиту тогда
    держат заголовок X-Justmedit и SameSite=Strict.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
import unicodedata
from typing import Any, Optional
from urllib.parse import urlsplit

from .service import ServiceError

SCRYPT_N, SCRYPT_R, SCRYPT_P, SCRYPT_DKLEN, SALT_BYTES = 2 ** 14, 8, 1, 32, 16
SCRYPT_MAXMEM = 64 * 1024 * 1024
LOGIN_RE = re.compile(r"[A-Za-zА-Яа-яЁё0-9._@-]{3,64}")    # проверяется fullmatch: перевод строки в конце не проходит
LATIN_RE = re.compile(r"[A-Za-z]")
CYRILLIC_RE = re.compile(r"[А-Яа-яЁё]")
PASSWORD_MIN, PASSWORD_MAX = 8, 128
CLINIC_MAX = 120
ROLES = ("patient", "doctor")
COOKIE_TTL = 7 * 86400
LOGIN_TTL = COOKIE_TTL                     # токен «Вход» истекает вместе с cookie своего входа
API_TTL = 30 * 86400                       # именованный токен API
# Юникод-категории, запрещённые в коротких строках (клиника, имя набора, имя токена): управляющие (Cc), форматные
# (Cf — в том числе смена направления текста U+202E и невидимые U+200B), суррогаты, частные и неназначенные
# символы, разделители строк и абзацев (Zl, Zp). Такая строка показывается пациенту и врачу на странице.
BAD_TEXT_CATEGORIES = frozenset({"Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp"})
CSRF_HEADER = "x-justmedit"
MUTATING = frozenset({"POST", "PUT", "PATCH", "DELETE"})
BAD_CREDENTIALS = "Неверный логин или пароль."


# --------------------------------------------------------------------------------------------
# Пароли
# --------------------------------------------------------------------------------------------
def _b64(b: bytes) -> str:
    return base64.b64encode(b).decode("ascii")


def hash_password(password: str, *, salt: Optional[bytes] = None) -> str:
    salt = salt or secrets.token_bytes(SALT_BYTES)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P,
                            dklen=SCRYPT_DKLEN, maxmem=SCRYPT_MAXMEM)
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${_b64(salt)}${_b64(digest)}"


def verify_password(password: str, stored: str) -> bool:
    """Пересчитывает scrypt с параметрами и солью из stored и сравнивает за постоянное время."""
    try:
        scheme, n, r, p, salt, digest = stored.split("$")
        if scheme != "scrypt":
            return False
        expected = base64.b64decode(digest)
        got = hashlib.scrypt(password.encode("utf-8", "surrogateescape"), salt=base64.b64decode(salt), n=int(n),
                             r=int(r), p=int(p), dklen=len(expected), maxmem=SCRYPT_MAXMEM)
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(got, expected)


_DUMMY_HASH: Optional[str] = None


def ensure_dummy_hash() -> str:
    """Хэш случайного пароля для проверки «неизвестного логина» (считается один раз на процесс)."""
    global _DUMMY_HASH
    if _DUMMY_HASH is None:
        _DUMMY_HASH = hash_password(secrets.token_urlsafe(16))
    return _DUMMY_HASH


def dummy_verify(password: str) -> None:
    """Неизвестный логин: та же работа scrypt, что и при проверке настоящего пароля (ответ не быстрее)."""
    verify_password(password if isinstance(password, str) else "", ensure_dummy_hash())


# --------------------------------------------------------------------------------------------
# Проверка полей регистрации
# --------------------------------------------------------------------------------------------
def login_key(login: str) -> str:
    """Ключ поиска логина: без учёта регистра (Ivan и ivan — один логин)."""
    return login.casefold()


def valid_login(login) -> bool:
    return isinstance(login, str) and bool(LOGIN_RE.fullmatch(login))


def check_login(login) -> str:
    """Логин при регистрации. Вход проверяет только valid_login: правило о смешении алфавитов появилось позже,
    и уже зарегистрированные логины должны входить как раньше."""
    if not valid_login(login):
        raise ServiceError(422, "invalid_login", "Логин — от 3 до 64 символов: буквы, цифры и знаки «._-@».", "login")
    if LATIN_RE.search(login) and CYRILLIC_RE.search(login):
        raise ServiceError(422, "invalid_login", "Логин — либо латиницей, либо кириллицей: смешивать алфавиты "
                                                 "нельзя (похожие буквы делают логины неотличимыми).", "login")
    return login


def clean_text(value: Any, field: str, max_len: int, *, code: str = "invalid_value",
               default: Optional[str] = None, required: bool = True) -> Optional[str]:
    """Короткая строка для показа на странице (клиника, имя набора референсов, имя токена) — одна проверка для всех:
    строка, после обрезки пробелов не длиннее max_len, без управляющих, форматных и невидимых символов
    (BAD_TEXT_CATEGORIES). Пусто: default, если он задан; иначе None при required=False или 422 missing_field."""
    if value is None or (isinstance(value, str) and not value.strip()):
        if default is not None:
            return default
        if not required:
            return None
        raise ServiceError(422, "missing_field", "Не указано обязательное поле.", field)
    if not isinstance(value, str):
        raise ServiceError(422, code, f"Строка до {max_len} символов без управляющих знаков.", field)
    text = value.strip()
    if len(text) > max_len or any(unicodedata.category(ch) in BAD_TEXT_CATEGORIES for ch in text):
        raise ServiceError(422, code, f"Строка до {max_len} символов без управляющих и невидимых знаков.", field)
    return text


def check_password(password, field: str = "password") -> str:
    if not isinstance(password, str) or not PASSWORD_MIN <= len(password) <= PASSWORD_MAX:
        raise ServiceError(422, "invalid_password",
                           f"Пароль — от {PASSWORD_MIN} до {PASSWORD_MAX} символов.", field)
    try:
        password.encode("utf-8")
    except UnicodeEncodeError:
        raise ServiceError(422, "invalid_password", "Пароль содержит недопустимые символы.", field) from None
    return password


def check_role(role) -> str:
    if role not in ROLES:
        raise ServiceError(422, "invalid_role", "Роль: patient (пациент) или doctor (врач).", "role")
    return role


def check_clinic(clinic, role: str) -> Optional[str]:
    """Клиника — только у врача, до 120 символов (clean_text); у пациента не сохраняется."""
    if role != "doctor" or clinic is None:
        return None
    return clean_text(clinic, "clinic", CLINIC_MAX, code="invalid_clinic", required=False)


def login_hash(login) -> str:
    """Начало SHA-256 логина (без учёта регистра) — ключ лимитеров: сам логин в памяти не хранится."""
    lk = login_key(login) if isinstance(login, str) else ""
    return hashlib.sha256(lk.encode("utf-8", "surrogateescape")).hexdigest()[:16]


def rate_key(client: str, login) -> str:
    """Ключ лимита попыток «адрес + логин»."""
    return client + "|" + login_hash(login)


# --------------------------------------------------------------------------------------------
# Токены в запросе
# --------------------------------------------------------------------------------------------
def bearer_token(headers) -> Optional[str]:
    """Значение Bearer из заголовка Authorization; None — заголовка нет; "" — заголовок есть, но не Bearer."""
    raw = headers.get("authorization")
    if raw is None:
        return None
    scheme, _, value = raw.strip().partition(" ")
    if scheme.lower() != "bearer" or not value.strip():
        return ""
    return value.strip()


def unauthorized(message: str = "Войдите в кабинет: нужен действующий токен (Authorization: Bearer …) "
                                "или вход на сайте.") -> ServiceError:
    err = ServiceError(401, "unauthorized", message)
    err.www_authenticate = 'Bearer realm="Justmedit"'  # type: ignore[attr-defined]
    return err


def forbidden(message: str = "Нет доступа.") -> ServiceError:
    return ServiceError(403, "forbidden", message)


# --------------------------------------------------------------------------------------------
# CSRF
# --------------------------------------------------------------------------------------------
DEFAULT_PORTS = {"http": 80, "https": 443}


def origin_of(url: str) -> Optional[tuple[str, str, int]]:
    """«https://Jm.Example:443/путь» -> ("https", "jm.example", 443): схема, хост, порт (по умолчанию — порт схемы).
    Не http(s), нет хоста, есть логин в адресе или неверный порт — None."""
    try:
        parts = urlsplit(url.strip())
        scheme = parts.scheme.lower()
        host = parts.hostname
        port = parts.port
    except ValueError:
        return None
    if scheme not in DEFAULT_PORTS or not host or parts.username is not None or parts.password is not None:
        return None
    return scheme, host.lower(), port or DEFAULT_PORTS[scheme]


def _same_origin(url: str, scheme: str, host_header: str) -> bool:
    """Origin (или Referer) — наш источник: та же схема, хост и порт, что у запроса. Запрос по HTTP и Origin
    https с тем же хостом и портом по умолчанию — тоже наш (TLS снят прокси, не объявленным в DL_TRUSTED_PROXIES):
    так страница не ломается за HTTPS-прокси; обратное (запрос по HTTPS, Origin http) — чужой."""
    if not host_header:
        return False
    own, got = origin_of(f"{scheme}://{host_header}"), origin_of(url)
    if own is None or got is None:
        return False
    if got == own:
        return True
    try:
        explicit_port = urlsplit(f"{scheme}://{host_header}").port is not None
    except ValueError:
        return False
    return own[0] == "http" and got[0] == "https" and got[1] == own[1] and got[2] == 443 and not explicit_port


def check_csrf(method: str, headers, scheme: str = "http") -> None:
    """Для изменяющих запросов с cookie: заголовок X-Justmedit: 1 и свой Origin (или Referer), иначе 403 csrf.
    scheme — схема запроса (от доверенного прокси — из X-Forwarded-Proto), хост — заголовок Host."""
    if method.upper() not in MUTATING:
        return
    err = ServiceError(403, "csrf", "Запрос отклонён: нет защиты от подделки запроса (заголовок X-Justmedit) "
                                    "или он пришёл с чужого сайта.")
    if headers.get(CSRF_HEADER) != "1":
        raise err
    host = (headers.get("host") or "").strip()
    origin = headers.get("origin")
    if origin and origin.strip().lower() != "null":
        if not _same_origin(origin, scheme, host):
            raise err
        return
    referer = headers.get("referer")
    if referer and not _same_origin(referer, scheme, host):
        raise err
