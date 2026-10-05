"""Настройки веб-сервиса из переменных окружения (docs/CONTRACTS.md, «Контракт HTTP»).

  DL_API_KEYS         — ключи API через запятую; пусто — при старте создаётся случайный ключ и печатается
                        в stdout одной строкой `DL_API_KEY=<ключ>`;
  DL_UI_RATE_PER_MIN  — лимит запросов страницы демо (/ui/*) с одного адреса в минуту, по умолчанию 30;
  DL_MAX_UPLOAD_MB    — предел размера тела запроса и файла, МБ, по умолчанию 10;
  DL_MAX_BATCH        — предел числа записей в пакете, по умолчанию 10000;
  DL_HOST, DL_PORT    — адрес и порт (0.0.0.0:8080).

Личный кабинет (ADR 0009, docs/CONTRACTS.md — «Хранение и кабинет»):
  DL_DATA_DIR         — каталог базы SQLite кабинета (по умолчанию <репозиторий>/data/app, в Docker — том /data);
  DL_DATA_KEY         — ключ шифрования Fernet (base64, 32 байта); пусто — ключ создаётся файлом <DL_DATA_DIR>/key
                        (права 0600, только для демо);
  DL_DATA_KEY_FILE    — путь к файлу с ключом (секрет Docker и т. п.; права не шире 0600, не в томе с базой);
                        вместе с DL_DATA_KEY не задаётся;
  DL_DATA_MAX_MB      — предел размера базы кабинета, МБ (по умолчанию 512): дальше новые записи и регистрации —
                        507 storage_full; вход, чтение, выгрузка и удаление работают;
  DL_MAX_SIGNUPS_PER_DAY — регистраций за сутки на весь сервис (по умолчанию 1000), дальше 503 signups_paused;
  DL_SIGNUP_IP_PER_HOUR — регистраций в час с одного адреса (IPv6 — с сети /64), по умолчанию 30; дальше 429
                        (на общем Wi-Fi площадки или за NAT клиники все гости делят один адрес);
  DL_COOKIE_SECURE    — 1: cookie сессии `__Host-jm_session` с флагом Secure (сайт за HTTPS) и заголовок
                        Strict-Transport-Security; по умолчанию 0 — `jm_session` без Secure (локальный запуск по HTTP);
  DL_CORS_ORIGINS     — источники через запятую, которым разрешены запросы к /api/v1/* из браузера (без cookie);
  DL_TRUSTED_PROXIES  — адреса и сети обратных прокси через запятую (127.0.0.1, 10.0.0.0/8): только от них
                        принимаются X-Forwarded-For / -Proto / -Host. Пусто (по умолчанию) — не доверять никому.
"""
from __future__ import annotations

import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_DATA_DIR = str(Path(__file__).resolve().parents[2] / "data" / "app")


def _flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


def _origins(raw: str) -> tuple[str, ...]:
    """«https://a.ru, https://b.ru/» -> ("https://a.ru", "https://b.ru"): без пробелов и завершающей «/»."""
    return tuple(o.strip().rstrip("/") for o in raw.split(",") if o.strip() and o.strip() != "*")


def _proxies(raw: str) -> tuple[str, ...]:
    """«127.0.0.1, 10.0.0.0/8» -> ("127.0.0.1", "10.0.0.0/8"). Каждая запись проверяется сразу: опечатка в
    настройке безопасности останавливает запуск (ValueError), а не выключает проверку молча."""
    from .security import parse_networks

    items = tuple(x.strip() for x in raw.split(",") if x.strip())
    parse_networks(items)
    return items


def _int(name: str, default: int, minimum: int = 1) -> int:
    try:
        return max(minimum, int(os.environ.get(name, "").strip() or default))
    except ValueError:
        return default


@dataclass
class Settings:
    api_keys: tuple[str, ...] = field(default=(), repr=False)
    ui_rate_per_min: int = 30
    max_upload_mb: int = 10
    max_batch: int = 10000
    host: str = "0.0.0.0"
    port: int = 8080
    max_json_kb: int = 256                 # предел тела для одиночных JSON-запросов (analyze, parse)
    generated_key: bool = field(default=False)
    # ---- личный кабинет (ADR 0009) ----
    data_dir: str = DEFAULT_DATA_DIR
    # None — ключ из файла <data_dir>/key (создаётся при первом обращении); в repr не попадает
    data_key: str | None = field(default=None, repr=False)
    data_key_file: str | None = None       # файл с ключом (DL_DATA_KEY_FILE)
    data_max_mb: int = 512                 # предел размера базы кабинета (DL_DATA_MAX_MB)
    max_signups_per_day: int = 1000        # регистраций в сутки на весь сервис (DL_MAX_SIGNUPS_PER_DAY)
    signup_ip_per_hour: int = 30           # регистраций в час с одного адреса (IPv6 — с одной сети /64)
    cookie_secure: bool = False
    cors_origins: tuple[str, ...] = ()
    trusted_proxies: tuple[str, ...] = ()  # DL_TRUSTED_PROXIES: адреса и сети прокси, которым верим X-Forwarded-*
    auth_rate_per_min: int = 10            # попыток входа и регистрации в минуту на пару (адрес, логин)
    auth_ip_rate_per_min: int = 60         # всех попыток входа и регистрации в минуту с одного адреса
    cabinet_rate_per_min: int = 240        # запросов кабинета в минуту на пользователя

    @property
    def cookie_name(self) -> str:
        """За HTTPS — префикс __Host- (браузер примет cookie только с Secure, Path=/ и без Domain: её не подменит
        поддомен и не поставит страница по HTTP); локально по HTTP такой cookie не работает — обычное имя."""
        return "__Host-jm_session" if self.cookie_secure else "jm_session"

    @property
    def max_upload_bytes(self) -> int:
        return self.max_upload_mb * 1024 * 1024

    @classmethod
    def from_env(cls) -> "Settings":
        keys = tuple(k.strip() for k in os.environ.get("DL_API_KEYS", "").split(",") if k.strip())
        return cls(
            api_keys=keys,
            ui_rate_per_min=_int("DL_UI_RATE_PER_MIN", 30),
            max_upload_mb=_int("DL_MAX_UPLOAD_MB", 10),
            max_batch=_int("DL_MAX_BATCH", 10000),
            host=os.environ.get("DL_HOST", "0.0.0.0").strip() or "0.0.0.0",
            port=_int("DL_PORT", 8080),
            data_dir=os.environ.get("DL_DATA_DIR", "").strip() or DEFAULT_DATA_DIR,
            data_key=os.environ.get("DL_DATA_KEY", "").strip() or None,
            data_key_file=os.environ.get("DL_DATA_KEY_FILE", "").strip() or None,
            data_max_mb=_int("DL_DATA_MAX_MB", 512),
            max_signups_per_day=_int("DL_MAX_SIGNUPS_PER_DAY", 1000),
            signup_ip_per_hour=_int("DL_SIGNUP_IP_PER_HOUR", 30),
            cookie_secure=_flag("DL_COOKIE_SECURE"),
            cors_origins=_origins(os.environ.get("DL_CORS_ORIGINS", "")),
            trusted_proxies=_proxies(os.environ.get("DL_TRUSTED_PROXIES", "")),
        )

    def ensure_key(self) -> "Settings":
        """Если ключей нет — создать случайный (его печатает app.py при старте)."""
        if not self.api_keys:
            self.api_keys = (secrets.token_urlsafe(24),)
            self.generated_key = True
        return self
