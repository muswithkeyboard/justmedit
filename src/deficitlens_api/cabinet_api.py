"""HTTP-маршруты личного кабинета (ADR 0009; docs/CONTRACTS.md — «Хранение и кабинет»).

Обработчики — async-функции Starlette; ошибки — ServiceError, их переводит в ответ {"error": {code, message, field}}
обёртка endpoint() из app.py. Хранилище (store.py) синхронное и вызывается из пула потоков.

Авторизация: `Authorization: Bearer <токен API>` или cookie `jm_session` (`__Host-jm_session` за HTTPS; auth.py).
Изменяющие запросы с cookie — только с `X-Justmedit: 1` и своим Origin (403 csrf). Нет или просрочен токен — 401
unauthorized; не та роль или нет действующей связи с пациентом — 403 forbidden. Тела JSON принимаются только
с Content-Type: application/json (простую HTML-форму с чужого сайта так не отправить — защита входа и регистрации
от подделки запроса).

Область токена API (Bearer): анализ, чтение кабинета, запись анализов, референсы врача. Управление доступом —
новый токен API, ссылка врачу, «выйти везде» — по Bearer только с паролем аккаунта в теле (поле password; иначе
403 password_required / wrong_password): украденный токен не размножается и не открывает данные врачу. Сессии
сайта (cookie, вход с паролем) пароль повторно не спрашивают. Смена пароля и удаление аккаунта — всегда с паролем.

Что считается и хранится: запись пациента — вход AnalysisInput (проверка схемы — как у /api/v1/analyze, плюс
правила кабинета: показатели — только известные коды и синонимы, patient_ref до 64 знаков, provenance не хранится,
вход после сериализации не больше 16 КБ) и ответ движка (engine.analyze) как кеш; при смене версии движка,
моделей или порогов ответ пересчитывается. Анализ истории — deficitlens_core.history.analyze (поток П4) по всем
записям; слот тяжёлых расчётов (с ожиданием до 3 с) — только для пересчёта устаревшего кеша. Журнал сервера — только метод, путь, код и время: логины, токены, пароли
и значения анализов в него не попадают.
"""
from __future__ import annotations

import asyncio
import contextlib
import csv
import io
import json
import math
import re
import sqlite3
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, Optional

from pydantic import ValidationError
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from deficitlens_core.schemas import AnalysisInput, InputError, ReferenceRangeInput

from . import service
from .auth import (
    API_TTL,
    BAD_CREDENTIALS,
    LOGIN_TTL,
    PASSWORD_MAX,
    bearer_token,
    check_clinic,
    check_csrf,
    check_login,
    check_password,
    check_role,
    clean_text,
    dummy_verify,
    ensure_dummy_hash,
    forbidden,
    hash_password,
    login_hash,
    login_key,
    rate_key,
    unauthorized,
    valid_login,
    verify_password,
)
from .security import LoginGuard, RateLimiter, key_id, parse_json_body, rate_limit_error, read_body, request_client
from .service import ServiceError
from .store import SHARE_DAYS, Store, StoreError, iso

MAX_RECORDS = 500                       # анализов в кабинете одного пациента (docs/CONTRACTS.md)
RECORD_MAX_BYTES = 16 * 1024            # вход одной записи после проверки, JSON UTF-8
PATIENT_REF_MAX = 64
LAB_REFERENCE_MAX = 64
SOURCES = ("form", "photo", "pdf", "file", "text", "import")   # text — вставленный текст бланка
EXPORT_FORMAT = "justmedit-export"
ID_RE = re.compile(r"[0-9a-f]{32}")                       # fullmatch: id — 32 шестнадцатеричных знака
NAME_MAX = 80
TOKEN_NAME_MAX = 60
MAX_RANGES = 100                        # показателей в одном наборе референсов врача
LAB_FILE_MAX_ROWS = 200
LAB_FILE_MAX_BYTES = 256 * 1024
HISTORY_SLOT_WAIT = 3.0                 # с: сколько история ждёт слот тяжёлых расчётов, прежде чем ответить 503
SLOT_POLL = 0.05                        # с: шаг ожидания слота (цикл событий не блокируется)
DOCTOR_SET_NAME = "doctor_default"      # references.lab_name, когда в расчёт подставлен набор врача по умолчанию
REQUEST_REFS_TITLE = "референсы из запроса"   # подпись движка (references.resolve) для референсов из запроса
TRUE_WORDS = ("1", "true", "yes", "on", "да")
STATUS_BY_CODE = {"storage_unavailable": 503, "storage_key": 500, "login_taken": 409,
                  "too_many_records": 422, "too_many_shares": 422, "too_many_sets": 422,
                  "too_many_tokens": 422, "quota_exceeded": 422, "storage_full": 507, "signups_paused": 503}


@dataclass
class Ctx:
    """Кто сделал запрос: пользователь, его сессия и способ входа ("bearer" | "cookie")."""
    user: sqlite3.Row
    session: sqlite3.Row
    via: str

    @property
    def uid(self) -> str:
        return self.user["id"]

    @property
    def role(self) -> str:
        return self.user["role"]


def _today(store: Store) -> date:
    return datetime.fromtimestamp(store.now(), tz=timezone.utc).date()


def _text(value: Any, field: str, max_len: int, default: Optional[str] = None) -> str:
    """Короткая строка для показа (имя набора, имя токена) — общая проверка auth.clean_text."""
    return clean_text(value, field, max_len, default=default)


def _flag(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return isinstance(value, str) and value.strip().lower() in TRUE_WORDS


def _versions_key() -> str:
    """Ключ кеша ответа движка: версии движка, моделей и набора порогов."""
    v = service._versions()
    return "|".join(str(v.get(k, "")) for k in ("engine", "case_model", "screen_model", "norms"))


def _model(raw: Any, field_prefix: str = "") -> AnalysisInput:
    """Проверка схемы AnalysisInput — как у /api/v1/analyze: ошибка — 422 с полем, значения в сообщение не попадают."""
    if not isinstance(raw, dict):
        raise ServiceError(422, "invalid_input", "Поле input — объект анализа: sex, age_years, values.",
                           f"{field_prefix}input")
    try:
        return AnalysisInput.model_validate(raw)
    except ValidationError as e:
        err = service._validation_error(e, service._cfg())
        if field_prefix and err.field:
            err.field = field_prefix + err.field
        raise err from None


def _validate_input(raw: Any, field_prefix: str = "") -> AnalysisInput:
    """Вход, который кабинет сохраняет (без расчёта): схема AnalysisInput и правила хранения.
      * показатели в values — только известные коды и синонимы config/analytes.yaml (normalize.resolve_analyte):
        неизвестный ключ движок молча пропустил бы, а в кабинете он попал бы в базу и в столбец выгрузки CSV
        (например «=HYPERLINK(…)»); имя ключа в ответ не возвращается;
      * patient_ref — до 64 знаков, lab_reference — до 64 знаков, без управляющих символов;
      * provenance не хранится (произвольный объект без предела вложенности и размера);
      * вход после сериализации (JSON UTF-8) — не больше RECORD_MAX_BYTES, иначе 413 record_too_large."""
    from deficitlens_core.normalize import resolve_analyte

    inp = _model(raw, field_prefix)
    cfg = service._cfg()
    for name, keys in (("values", inp.values), ("reference_ranges", inp.reference_ranges or {})):
        if any(resolve_analyte(key, cfg) is None for key in keys):
            raise ServiceError(422, "unknown_analyte", "В записи кабинета — только известные показатели (код или "
                                                       "название из справочника /api/v1/reference).",
                               f"{field_prefix}{name}")
    for name, limit in (("patient_ref", PATIENT_REF_MAX), ("lab_reference", LAB_REFERENCE_MAX)):
        value = getattr(inp, name)
        if value is not None:
            clean_text(value, f"{field_prefix}{name}", limit, required=False)
    inp = inp.model_copy(update={"provenance": None})
    size = len(json.dumps(inp.model_dump(mode="json", exclude_none=True), ensure_ascii=False,
                          separators=(",", ":")).encode("utf-8"))
    if size > RECORD_MAX_BYTES:
        raise ServiceError(413, "record_too_large", f"Запись анализа больше {RECORD_MAX_BYTES // 1024} КБ.",
                           f"{field_prefix}input")
    return inp


def _compute(inp: AnalysisInput, field_prefix: str = "") -> tuple[dict, dict]:
    """Проверенный вход -> (вход как dict, ответ движка как dict)."""
    from deficitlens_core.engine import analyze

    try:
        result = analyze(inp, models=service._models(), cfg=service._cfg())
    except InputError as e:
        raise ServiceError(422, e.code, e.message, (field_prefix + e.field) if e.field else e.field) from None
    return inp.model_dump(mode="json", exclude_none=True), result.model_dump(mode="json")


def _analyze_input(raw: Any, field_prefix: str = "") -> tuple[dict, dict]:
    """Новая запись: проверка по правилам кабинета и расчёт."""
    return _compute(_validate_input(raw, field_prefix), field_prefix)


def _analyze_stored(raw: Any) -> tuple[dict, dict]:
    """Пересчёт уже сохранённой записи (сменилась версия движка): только схема AnalysisInput — запись, сохранённая
    до правил кабинета, должна по-прежнему открываться."""
    return _compute(_model(raw))


def _summary(result: dict, day: str) -> dict:
    reports = result.get("reports") or {}
    return {"headline_doctor": ((reports.get("doctor") or {}).get("headline")),
            "headline_patient": ((reports.get("patient") or {}).get("headline")),
            "anemia": bool((result.get("level1") or {}).get("anemia")), "date": day}


def _record_json(meta: dict, role: str = "patient", with_input: bool = False) -> dict:
    s = meta.get("summary") or {}
    out = {"id": meta["id"], "date": meta["date"], "source": meta["source"],
           "headline": s.get("headline_doctor" if role == "doctor" else "headline_patient"),
           "anemia": s.get("anemia"), "created": meta["created"]}
    if with_input:
        out["input"] = meta.get("input")
    return out


def _share_json(store: Store, row: sqlite3.Row) -> dict:
    """Связь пациента с врачом. doctor: null — ссылку ещё не приняли; {login, clinic, deleted: true} — врач удалил
    аккаунт (снимок на момент принятия, store.doctor_from_snapshot; такая связь закрыта)."""
    return {"id": row["id"], "doctor": store.doctor_from_snapshot(row, "shares"), "status": store.share_status(row),
            "expires": iso(row["expires"]), "days": row["days"], "created": iso(row["created"]),
            "accepted": iso(row["accepted"])}


def _token_json(row: sqlite3.Row, current: set) -> dict:
    """Токен API. kind: login — токен входа (пара cookie сайта или ответ /auth/login), named — именованный токен
    приложения. current — токен этого запроса (Bearer) или токен входа этой cookie-сессии."""
    return {"id": row["id"], "name": row["name"], "created": iso(row["created"]), "expires": iso(row["expires"]),
            "last_used": iso(row["last_used"]), "current": row["id"] in current,
            "kind": "login" if row["parent_id"] else "named"}


def _dedup_key(day: str, inp: Any) -> str:
    """Ключ «тот же анализ» для загрузки выгрузки: дата + пол, возраст, беременность и значения показателей.
    Значения сравниваются после нормализации движком (normalize.normalize_input: канонический код и единица,
    например 11,2 г/дл = 112 г/л), с точностью до 6 знаков; не нормализуется (ошибка ввода у старой записи) — как
    записано. Референсы, метка patient_ref и пороги показа в ключ не входят."""
    from deficitlens_core.normalize import normalize_input

    model = inp if isinstance(inp, AnalysisInput) else AnalysisInput.model_validate(inp)
    try:
        norm = normalize_input(model, service._cfg())
        vals = {k: round(float(v), 6) for k, v in norm.values.items() if k in norm.measured}
    except (InputError, TypeError, ValueError):
        vals = {k: (v if isinstance(v, (int, float)) else [v.value, v.unit]) for k, v in model.values.items()}
    preg = model.pregnancy.status if model.pregnancy is not None else None
    return json.dumps([day, model.sex, model.age_years, preg, sorted(vals.items())], ensure_ascii=False,
                      separators=(",", ":"), default=str)


# --------------------------------------------------------------------------------------------
# Референсы врача: JSON и файл «показатель;нижняя;верхняя;единица»
# --------------------------------------------------------------------------------------------
def _canonical_ranges(raw: Any, field: str = "ranges") -> dict:
    """{показатель: {low, high, unit}} -> {код: {low, high, unit}} в канонической единице сервиса.
    Показатель — код или синоним (normalize.resolve_analyte), единица пересчитывается тем же множителем, что и
    значения анализов (references.ranges_from_request). Ошибка — 422 с полем, ничего молча не исправляется."""
    from deficitlens_core.references import ranges_from_request

    if not isinstance(raw, dict) or not raw:
        raise ServiceError(422, "invalid_ranges", "Референсы: словарь «показатель -> {low, high, unit}».", field)
    if len(raw) > MAX_RANGES:
        raise ServiceError(422, "invalid_ranges", f"В наборе не больше {MAX_RANGES} показателей.", field)
    checked: dict[str, ReferenceRangeInput] = {}
    for key, spec in raw.items():
        try:
            checked[str(key)] = ReferenceRangeInput.model_validate(spec)
        except ValidationError:
            raise ServiceError(422, "invalid_reference", "Референс показателя: {\"low\": число, \"high\": число, "
                                                         "\"unit\": \"единица\"}.", f"{field}.{key}") from None
    cfg = service._cfg()
    try:
        canon = ranges_from_request(checked, cfg)
    except InputError as e:
        f = e.field.replace("reference_ranges", field, 1) if e.field else field
        raise ServiceError(422, e.code, e.message, f) from None
    if len(canon) != len(checked):
        raise ServiceError(422, "duplicate_analyte", "Один и тот же показатель указан дважды (код и синоним).", field)
    return {code: {"low": lo, "high": hi, "unit": cfg.analytes[code].get("unit")} for code, (lo, hi) in canon.items()}


def _num(cell: Any) -> Optional[float]:
    """Ячейка границы -> число или None (пусто, «-»). Десятичная запятая допускается."""
    if cell is None:
        return None
    if isinstance(cell, (int, float)) and not isinstance(cell, bool):
        return float(cell) if math.isfinite(float(cell)) else None
    s = str(cell).strip().replace(" ", "").replace(" ", "")
    if s in ("", "-", "—", "–", "nan", "None"):
        return None
    try:
        v = float(s.replace(",", "."))
    except ValueError:
        raise ValueError("not a number") from None
    return v if math.isfinite(v) else None


def _lab_rows(content: bytes, filename: str) -> list[list[Any]]:
    """Файл CSV / TXT / XLSX -> строки ячеек (не больше LAB_FILE_MAX_ROWS). Файл разбирается в памяти."""
    ext = (filename.rsplit(".", 1)[-1].lower() if "." in filename else "")
    if ext in ("xlsx", "xlsm"):
        import pandas as pd

        from deficitlens_core.io.table import TableLimitError, _check_xlsx
        try:
            _check_xlsx(content, 5 * 1024 * 1024)
            df = pd.read_excel(io.BytesIO(content), header=None, dtype=object, engine="openpyxl",
                               nrows=LAB_FILE_MAX_ROWS + 1)
        except TableLimitError as e:
            raise ServiceError(422, e.code, f"Не удалось прочитать файл: {e.message}.", "file") from None
        except Exception:  # noqa: BLE001 — повреждённый XLSX
            raise ServiceError(422, "invalid_file", "Не удалось прочитать файл XLSX.", "file") from None
        rows = [[None if (isinstance(v, float) and math.isnan(v)) else v for v in r] for r in df.values.tolist()]
    elif ext in ("csv", "txt", ""):
        if len(content) > LAB_FILE_MAX_BYTES:
            raise ServiceError(413, "payload_too_large", "Файл референсов больше 256 КБ.", "file")
        try:
            text = content.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = content.decode("cp1251", errors="replace")
        first = next((ln for ln in text.splitlines() if ln.strip()), "")
        sep = ";" if ";" in first else ("\t" if "\t" in first else ",")
        rows = list(csv.reader(io.StringIO(text), delimiter=sep))
    else:
        raise ServiceError(422, "unsupported_file", "Референсы: файл CSV или XLSX «показатель;нижняя;верхняя;единица».",
                           "file")
    rows = [r for r in rows if any(c is not None and str(c).strip() for c in r)]
    if len(rows) > LAB_FILE_MAX_ROWS:
        raise ServiceError(422, "too_many_rows", f"В файле референсов больше {LAB_FILE_MAX_ROWS} строк.", "file")
    return rows


def _ranges_from_file(content: bytes, filename: str) -> dict:
    """Строки «показатель;нижняя;верхняя;единица» (первая строка может быть заголовком) -> канонические референсы."""
    rows = _lab_rows(content, filename)
    raw: dict[str, dict] = {}
    for i, row in enumerate(rows, start=1):
        cells = list(row) + [None] * (4 - len(row))
        name = str(cells[0]).strip() if cells[0] is not None else ""
        try:
            low, high = _num(cells[1]), _num(cells[2])
        except ValueError:
            if i == 1:
                continue                                   # строка заголовка: «показатель; нижняя; верхняя; единица»
            raise ServiceError(422, "invalid_reference", f"Строка {i}: границы должны быть числами.", "file") from None
        if not name:
            raise ServiceError(422, "invalid_reference", f"Строка {i}: не указан показатель.", "file")
        unit = str(cells[3]).strip() if cells[3] is not None and str(cells[3]).strip() else None
        if name in raw:
            raise ServiceError(422, "duplicate_analyte", f"Строка {i}: показатель указан дважды.", "file")
        raw[name] = {"low": low, "high": high, "unit": unit}
    if not raw:
        raise ServiceError(422, "invalid_ranges", "В файле нет строк «показатель;нижняя;верхняя;единица».", "file")
    try:
        return _canonical_ranges(raw, "file")
    except ServiceError as e:
        if e.field and e.field.startswith("file."):                 # имя показателя из файла в поле не возвращаем
            e.field = "file"
        raise


# --------------------------------------------------------------------------------------------
# Выгрузка CSV в схеме кейса
# --------------------------------------------------------------------------------------------
def _export_csv(records: list[dict]) -> str:
    """Записи -> CSV: sex, age_years, коды показателей (канонические единицы, как в файле кейса), date.
    Значения пересчитываются в каноническую единицу нормализацией движка (normalize.normalize_input); производные
    показатели (TSAT, eGFR), которых пациент не сдавал, не выгружаются. Формулы в ячейках экранируются."""
    import pandas as pd

    from deficitlens_core.constants import ALL_ANALYTES
    from deficitlens_core.io.table import to_safe_csv
    from deficitlens_core.normalize import normalize_input

    cfg = service._cfg()
    rows, extra = [], set()
    for rec in records:
        inp = AnalysisInput.model_validate(rec["input"])
        try:
            norm = normalize_input(inp, cfg)
            vals = {k: float(v) for k, v in norm.values.items() if k in norm.measured}
        except InputError:
            vals = {k: (v if isinstance(v, (int, float)) else v.value) for k, v in inp.values.items()}
        # Столбцы — только коды показателей: ключ, который не код (запись, сохранённая до проверки показателей
        # в кабинете), в заголовок CSV не попадает — заголовок формулами не экранируется.
        vals = {k: v for k, v in vals.items() if k in cfg.analytes}
        extra.update(k for k in vals if k not in ALL_ANALYTES)
        rows.append({"sex": inp.sex, "age_years": inp.age_years, **vals, "date": rec["date"]})
    columns = ["sex", "age_years", *ALL_ANALYTES, *sorted(extra), "date"]
    return to_safe_csv(pd.DataFrame(rows, columns=columns))


# --------------------------------------------------------------------------------------------
class Cabinet:
    """Маршруты кабинета и проверки доступа для остальных маршрутов (Bearer в /api/v1/*, референсы врача)."""

    def __init__(self, settings, store: Store, gate, heavy_limiter: Optional[RateLimiter] = None):
        self.settings, self.store, self.gate = settings, store, gate
        self.cookie_name = settings.cookie_name
        self.auth_pair_limiter = RateLimiter(settings.auth_rate_per_min)
        self.auth_ip_limiter = RateLimiter(settings.auth_ip_rate_per_min)
        self.user_limiter = RateLimiter(settings.cabinet_rate_per_min)
        # регистраций с одного адреса (IPv6 — сети /64) в час; общий суточный предел — в базе (store.create_user)
        self.signup_limiter = RateLimiter(settings.signup_ip_per_hour, window=3600)
        self.login_guard = LoginGuard()          # неудачи входа на логин с любых адресов (security.LoginGuard)
        # тяжёлые запросы в минуту на пользователя (загрузка выгрузки) — общий с /api/v1 лимитер app.py
        self.heavy_limiter = heavy_limiter or RateLimiter(settings.ui_rate_per_min)
        ensure_dummy_hash()       # хэш фиктивного пароля — заранее: первый вход с неизвестным логином не медленнее

    # ---- общие помощники --------------------------------------------------------------------
    async def db(self, fn: Callable, *args, **kwargs):
        """Вызов хранилища в пуле потоков; ошибки хранилища -> ServiceError."""
        try:
            return await run_in_threadpool(fn, *args, **kwargs)
        except StoreError as e:
            err = ServiceError(STATUS_BY_CODE.get(e.code, 500), e.code, e.message)
            if getattr(e, "retry_after", None):
                err.retry_after = e.retry_after  # type: ignore[attr-defined]
            raise err from None

    async def body(self, request: Request, limit: Optional[int] = None, allow_empty: bool = False) -> dict:
        """Тело JSON-объектом. Только Content-Type: application/json (кроме пустого тела, если allow_empty)."""
        raw = await read_body(request, limit or self.settings.max_json_kb * 1024)
        if allow_empty and not raw.strip():
            return {}
        ctype = request.headers.get("content-type", "").split(";")[0].strip().lower()
        if ctype != "application/json":
            raise ServiceError(415, "unsupported_media_type",
                               "Тело запроса — JSON с заголовком Content-Type: application/json.")
        payload = parse_json_body(raw)
        if not isinstance(payload, dict):
            raise ServiceError(422, "invalid_body", "Тело запроса должно быть объектом JSON.")
        return payload

    def _resolve_sync(self, headers, cookies) -> Optional[Ctx]:
        token = bearer_token(headers)
        if token is not None:
            if not token:
                raise unauthorized("Заголовок Authorization: нужен вид «Bearer <токен>».")
            found = self.store.session_by_token(token, "api")
            if found is None:
                raise unauthorized("Токен недействителен или истёк: войдите заново или создайте новый токен.")
            return Ctx(found[1], found[0], "bearer")
        cookie = cookies.get(self.cookie_name)
        if cookie:
            found = self.store.session_by_token(cookie, "cookie")
            if found is not None:
                return Ctx(found[1], found[0], "cookie")
        return None

    async def require(self, request: Request, role: Optional[str] = None) -> Ctx:
        """Вход обязателен: 401 без действующего токена; CSRF для cookie; роль; лимит запросов на пользователя."""
        ctx = await self.db(self._resolve_sync, request.headers, request.cookies)
        if ctx is None:
            raise unauthorized()
        return self._checked(ctx, request, role)

    def _checked(self, ctx: Ctx, request: Request, role: Optional[str] = None) -> Ctx:
        if ctx.via == "cookie":
            check_csrf(request.method, request.headers, request.url.scheme)
        if role is not None and ctx.role != role:
            raise forbidden("Раздел только для " + ("пациента." if role == "patient" else "врача."))
        retry = self.user_limiter.check(ctx.uid)
        if retry is not None:
            raise rate_limit_error(retry, f"Слишком много запросов к кабинету. Повторите через {retry} с.")
        return ctx

    def _auth_limit(self, request: Request, login: Any) -> None:
        client = request_client(request)
        retry = self.auth_ip_limiter.check(client)
        if retry is None:
            retry = self.auth_pair_limiter.check(rate_key(client, login))
        if retry is not None:
            raise rate_limit_error(retry, f"Слишком много попыток входа. Повторите через {retry} с.")

    def _guard_check(self, login: Any) -> str:
        """Общий счётчик неудач на логин (с любых адресов): логин на паузе — 429. Возвращает ключ счётчика."""
        key = login_hash(login)
        retry = self.login_guard.check(key)
        if retry is not None:
            raise rate_limit_error(retry, "Слишком много неудачных попыток входа в этот аккаунт. "
                                          f"Повторите через {retry} с.")
        return key

    async def _confirm_password(self, request: Request, ctx: Ctx, password: Any, *, always: bool = False,
                                field: str = "password", message: str = "Неверный пароль.") -> None:
        """Пароль аккаунта для действий, которые токен API сам по себе не разрешает (always=False — только для
        Bearer; сессия сайта уже вошла с паролем) или которые всегда требуют пароль (смена пароля, удаление).
        Лимиты — как у входа: адрес + логин, адрес, общий счётчик неудач на логин."""
        if ctx.via == "cookie" and not always:
            return
        if (password is None or password == "") and not always:
            raise ServiceError(403, "password_required", "Для этого действия по токену API нужен пароль "
                                                         "аккаунта (поле password).", field)
        self._auth_limit(request, ctx.user["login"])
        key = self._guard_check(ctx.user["login"])
        ok = isinstance(password, str) and 0 < len(password) <= PASSWORD_MAX and \
            await run_in_threadpool(verify_password, password, ctx.user["pw_hash"])
        if not ok:
            self.login_guard.failure(key)
            raise ServiceError(403, "wrong_password", message, field)

    def _set_cookie(self, response: Response, token: str) -> None:
        response.set_cookie(self.cookie_name, token, max_age=LOGIN_TTL, path="/", secure=self.settings.cookie_secure,
                            httponly=True, samesite="strict")

    def _clear_cookie(self, response: Response) -> None:
        response.delete_cookie(self.cookie_name, path="/", secure=self.settings.cookie_secure, httponly=True,
                               samesite="strict")

    async def _issue(self, user: sqlite3.Row, status: int) -> Response:
        """Вход: cookie сайта и парный токен API «Вход» — оба на 7 дней (LOGIN_TTL), выход или истечение закрывают
        оба (store.create_login)."""
        cookie_token, api_token, _ = await self.db(self.store.create_login, user["id"], LOGIN_TTL)
        response = JSONResponse({"user": Store.public_user(user), "token": api_token}, status_code=status)
        self._set_cookie(response, cookie_token)
        return response

    @staticmethod
    def _path_id(request: Request, name: str) -> str:
        value = request.path_params.get(name, "")
        if not ID_RE.fullmatch(value):
            raise ServiceError(404, "not_found", "Не найдено.")
        return value

    # ---- доступ к маршрутам анализа и референсы врача -----------------------------------------
    async def authorize_api(self, request: Request, api_keys) -> str:
        """/api/v1/analyze|batch|benchmark|parse|columns|reference: X-API-Key или Bearer-токен пользователя.
        Возвращает метку для лимита тяжёлых запросов («ключ» или «пользователь»)."""
        from .security import check_api_key

        token = bearer_token(request.headers)
        if token:
            ctx = await self.db(self._resolve_sync, request.headers, request.cookies)
            request.state.cab_user = ctx.user
            return "user:" + ctx.uid
        check_api_key(request.headers.get("x-api-key"), api_keys)
        return key_id(request.headers.get("x-api-key"))

    async def optional_cookie_user(self, request: Request) -> Optional[sqlite3.Row]:
        """Страница демо (/ui/analyze): пользователь по cookie, если он вошёл; ошибки хранилища — как аноним."""
        if not request.cookies.get(self.cookie_name):
            return None
        try:
            found = await run_in_threadpool(self.store.session_by_token, request.cookies[self.cookie_name], "cookie")
        except StoreError:
            return None
        return found[1] if found else None

    async def apply_doctor_refs(self, user: Optional[sqlite3.Row], payload: Any) -> Optional[dict]:
        """Врач, в запросе нет reference_ranges -> подставить его набор референсов «по умолчанию» (как поле
        reference_ranges: движок применит их и предупредит об этом). Пациенту и анониму — без изменений.
        Возвращает {"id", "name"} подставленного набора (для подписи в ответе — label_doctor_refs) или None."""
        if user is None or user["role"] != "doctor" or not isinstance(payload, dict):
            return None
        if payload.get("reference_ranges"):
            return None
        found = await self.db(self.store.default_lab_ref, user["id"])
        if found and found.get("ranges"):
            payload["reference_ranges"] = {code: {k: v for k, v in r.items() if v is not None}
                                           for code, r in found["ranges"].items()}
            return {"id": found["id"], "name": found["name"]}
        return None

    @staticmethod
    def label_doctor_refs(result: Any, applied: Optional[dict]) -> Any:
        """Ответ движка, в который подставлен набор врача: движок видит его как «референсы из запроса»
        (references.lab_name = "request"). Подпись меняется на набор врача: references.lab_name = "doctor_default",
        lab_title = имя набора, lab_id = id набора; в таблице значений «референсы из запроса» -> «ваш набор «имя»».
        Остальное (границы, предупреждение) — как посчитал движок."""
        if not applied or not isinstance(result, dict):
            return result
        refs = result.get("references")
        if isinstance(refs, dict) and refs.get("set") == "lab":
            refs.update(lab_name=DOCTOR_SET_NAME, lab_title=applied["name"], lab_id=applied["id"])
            title = f"ваш набор «{applied['name']}»"
            for item in result.get("values") or []:
                norm = item.get("norm") if isinstance(item, dict) else None
                if isinstance(norm, dict):
                    for key in ("threshold_text", "source"):
                        if isinstance(norm.get(key), str):
                            norm[key] = norm[key].replace(REQUEST_REFS_TITLE, title)
        return result

    # ---- вход, выход, профиль -----------------------------------------------------------------
    async def register(self, request: Request) -> Response:
        """Регистрация. Пределы: попытки входа и регистрации (адрес + логин, адрес), не больше
        signup_ip_per_hour новых аккаунтов в час с адреса (IPv6 — с сети /64; неудачная регистрация в счёт не
        идёт) и DL_MAX_SIGNUPS_PER_DAY в сутки на весь сервис (503 signups_paused), предел размера базы (507)."""
        body = await self.body(request)
        self._auth_limit(request, body.get("login"))
        login = check_login(body.get("login"))
        password = check_password(body.get("password"))
        role = check_role(body.get("role"))
        clinic = check_clinic(body.get("clinic"), role)
        if body.get("consent") is not True:
            raise ServiceError(422, "consent_required", "Нужно согласие на хранение данных в кабинете "
                                                        "(consent: true).", "consent")
        client = request_client(request)
        retry = self.signup_limiter.check(client)
        if retry is not None:
            raise rate_limit_error(retry, f"Слишком много регистраций с вашего адреса. Повторите через {retry} с.")
        try:
            pw_hash = await run_in_threadpool(hash_password, password)
            user = await self.db(self.store.create_user, login, login_key(login), role, pw_hash, clinic)
        except BaseException:
            self.signup_limiter.release(client)              # аккаунт не создан — попытка не в счёт
            raise
        return await self._issue(user, 201)

    async def login(self, request: Request) -> Response:
        body = await self.body(request)
        login, password = body.get("login"), body.get("password")
        self._auth_limit(request, login)
        guard = self._guard_check(login)
        bad = ServiceError(401, "invalid_credentials", BAD_CREDENTIALS)
        bad.www_authenticate = 'Bearer realm="Justmedit"'  # type: ignore[attr-defined]
        password_ok = isinstance(password, str) and 0 < len(password) <= PASSWORD_MAX
        user = None
        if valid_login(login) and password_ok:
            user = await self.db(self.store.user_by_login, login_key(login))
        if user is None:
            await run_in_threadpool(dummy_verify, password if password_ok else "")
            self.login_guard.failure(guard)
            raise bad
        if not await run_in_threadpool(verify_password, password, user["pw_hash"]):
            self.login_guard.failure(guard)
            raise bad
        self.login_guard.success(guard)
        return await self._issue(user, 200)

    async def logout(self, request: Request) -> Response:
        ctx = await self.db(self._resolve_sync, request.headers, request.cookies)
        if ctx is not None:
            if ctx.via == "cookie":
                check_csrf(request.method, request.headers, request.url.scheme)
            await self.db(self.store.delete_session, ctx.uid, ctx.session["id"])
        response = JSONResponse({"ok": True})
        self._clear_cookie(response)
        return response

    async def logout_all(self, request: Request) -> Response:
        """«Выйти на всех устройствах»: отзывает все сессии сайта и токены API, кроме текущего входа (cookie и её
        токен «Вход» или текущий токен), с {"all": true} — и текущий (cookie стирается). По Bearer — с паролем."""
        ctx = await self.require(request)
        body = await self.body(request, allow_empty=True)
        await self._confirm_password(request, ctx, body.get("password"))
        everything = body.get("all") is True
        revoked = await self.db(self.store.revoke_sessions, ctx.uid, None if everything else ctx.session["id"])
        response = JSONResponse({"ok": True, "revoked": revoked})
        if everything:
            self._clear_cookie(response)
        return response

    async def change_password(self, request: Request) -> Response:
        """Смена пароля {"old", "new"}: старый пароль обязателен всегда; новый — по правилам регистрации. Все
        остальные сессии и токены (в том числе именованные токены API) отзываются — текущий вход остаётся."""
        ctx = await self.require(request)
        body = await self.body(request)
        await self._confirm_password(request, ctx, body.get("old"), always=True, field="old",
                                     message="Неверный текущий пароль: пароль не изменён.")
        new = check_password(body.get("new"), "new")
        pw_hash = await run_in_threadpool(hash_password, new)
        revoked = await self.db(self.store.set_password, ctx.uid, pw_hash, ctx.session["id"])
        return JSONResponse({"ok": True, "revoked": revoked})

    async def me(self, request: Request) -> Response:
        """Профиль. Cookie сессии есть, но она недействительна (выход везде, смена пароля на другом устройстве,
        истёк срок) — 401 и Set-Cookie на удаление: следующая страница сразу рисует в шапке «Войти»."""
        ctx = await self.db(self._resolve_sync, request.headers, request.cookies)
        if ctx is None:
            err = unauthorized()
            if request.cookies.get(self.cookie_name) and bearer_token(request.headers) is None:
                response = JSONResponse(err.body(), status_code=401,
                                        headers={"WWW-Authenticate": err.www_authenticate})  # type: ignore[attr-defined]
                self._clear_cookie(response)
                return response
            raise err
        ctx = self._checked(ctx, request)
        return JSONResponse(Store.public_user(ctx.user))

    async def delete_me(self, request: Request) -> Response:
        """Полное удаление аккаунта по паролю: всё в одной транзакции, затем затирание и усечение WAL
        (store.delete_user)."""
        ctx = await self.require(request)
        body = await self.body(request)
        await self._confirm_password(request, ctx, body.get("password"), always=True,
                                     message="Неверный пароль: аккаунт не удалён.")
        await self.db(self.store.delete_user, ctx.uid)
        response = JSONResponse({"deleted": True})
        self._clear_cookie(response)
        return response

    # ---- токены API -------------------------------------------------------------------------
    async def _tokens(self, ctx: Ctx) -> dict:
        """Токены API. current: по Bearer — сам этот токен; по cookie — токен «Вход» этой cookie-сессии."""
        rows = await self.db(self.store.list_tokens, ctx.uid)
        sid = ctx.session["id"]
        current = {sid} if ctx.via == "bearer" else {r["id"] for r in rows if r["parent_id"] == sid}
        return {"tokens": [_token_json(r, current) for r in rows]}

    async def tokens(self, request: Request) -> Response:
        ctx = await self.require(request)
        return JSONResponse(await self._tokens(ctx))

    async def create_token(self, request: Request) -> Response:
        """Именованный токен API на 30 дней. По Bearer — только с паролем и не дольше срока этого токена:
        украденный токен не продлевает себя новым. Не больше store.MAX_NAMED_TOKENS (422 too_many_tokens)."""
        ctx = await self.require(request)
        body = await self.body(request, allow_empty=True)
        name = _text(body.get("name"), "name", TOKEN_NAME_MAX, default="Токен API")
        await self._confirm_password(request, ctx, body.get("password"))
        cap = ctx.session["expires"] if ctx.via == "bearer" else None
        token, row = await self.db(self.store.create_session, ctx.uid, "api", API_TTL, name=name, expires=cap)
        return JSONResponse({"token": _token_json(row, set()), "value": token})

    async def delete_token(self, request: Request) -> Response:
        ctx = await self.require(request)
        tid = self._path_id(request, "id")
        if not await self.db(self.store.delete_session, ctx.uid, tid, "api", with_pair=False):
            raise ServiceError(404, "not_found", "Токен не найден.")
        return JSONResponse(await self._tokens(ctx))

    # ---- записи пациента --------------------------------------------------------------------
    def _check_date(self, raw: Any, field: str = "date") -> str:
        if raw is None:
            return _today(self.store).isoformat()
        # Запас в сутки: сервер считает «сегодня» по UTC, а у пациента восточнее Гринвича дата уже следующая.
        err = ServiceError(422, "invalid_date", "Дата анализа — ГГГГ-ММ-ДД, не раньше 1900 года и не позже "
                                                "завтрашнего дня (запас на часовой пояс).", field)
        if not isinstance(raw, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", raw):
            raise err
        try:
            day = date.fromisoformat(raw)
        except ValueError:
            raise err from None
        if day.year < 1900 or day > _today(self.store) + timedelta(days=1):
            raise err
        return day.isoformat()

    async def records(self, request: Request) -> Response:
        ctx = await self.require(request, "patient")
        metas = await self.db(self.store.list_records, ctx.uid)
        return JSONResponse({"records": [_record_json(m) for m in metas]})

    async def create_record(self, request: Request) -> Response:
        ctx = await self.require(request, "patient")
        body = await self.body(request)
        day = self._check_date(body.get("date"))
        source = body.get("source") or "form"
        if source not in SOURCES:
            raise ServiceError(422, "invalid_value", "Источник (source): form, photo, pdf, file, text или import.",
                               "source")
        if await self.db(self.store.count_records, ctx.uid) >= MAX_RECORDS:
            raise ServiceError(422, "too_many_records",
                               f"В кабинете можно хранить не больше {MAX_RECORDS} анализов: удалите старые.")
        inp, result = await run_in_threadpool(_analyze_input, body.get("input"))
        version = await run_in_threadpool(_versions_key)
        item = {"date": day, "source": source, "input": inp, "result": result, "summary": _summary(result, day),
                "version": version}
        (rid,) = await self.db(self.store.add_records, ctx.uid, [item], MAX_RECORDS)
        meta = await self.db(self.store.get_record, ctx.uid, rid)
        return JSONResponse({"record": _record_json(meta), "result": result}, status_code=201)

    def _fresh_sync(self, uid: str, rec: dict, version: str) -> dict:
        """Ответ движка для записи: кеш, если версия та же; иначе пересчёт и обновление кеша."""
        if rec.get("result") is not None and rec.get("version") == version:
            return rec["result"]
        inp, result = _analyze_stored(rec["input"])
        self.store.update_record_result(uid, rec["id"], inp, result, version)
        return result

    @staticmethod
    def _stale(rec: dict, version: str) -> bool:
        """Кеш ответа движка устарел (сменилась версия движка, моделей или порогов) или его нет."""
        return rec.get("result") is None or rec.get("version") != version

    def _refresh_sync(self, uid: str, records: list[dict], version: str) -> None:
        """Пересчёт устаревших записей движком (на месте в records) и обновление кеша в базе."""
        for rec in records:
            if self._stale(rec, version):
                rec["result"] = self._fresh_sync(uid, rec, version)
                rec["version"] = version

    @staticmethod
    def _history_analyze_sync(records: list[dict], role: str) -> dict:
        try:
            from deficitlens_core import history
        except ImportError:
            raise ServiceError(503, "history_unavailable", "Анализ истории пока недоступен.") from None
        # created — время создания записи: порядок анализов одного дня (history сортирует по дате и created).
        items = [{"id": r["id"], "date": r["date"], "created": r.get("created"), "input": r["input"],
                  "result": r["result"]} for r in records]
        return history.analyze(items, cfg=service._cfg(), models=service._models(), role=role)

    def _record_sync(self, uid: str, rid: str) -> Optional[tuple[dict, dict]]:
        rec = self.store.get_record(uid, rid)
        if rec is None:
            return None
        return rec, self._fresh_sync(uid, rec, _versions_key())

    async def record(self, request: Request) -> Response:
        ctx = await self.require(request, "patient")
        rid = self._path_id(request, "id")
        found = await self.db(self._record_sync, ctx.uid, rid)
        if found is None:
            raise ServiceError(404, "not_found", "Запись не найдена.")
        rec, result = found
        return JSONResponse({"record": _record_json(rec, with_input=True), "result": result})

    async def delete_record(self, request: Request) -> Response:
        ctx = await self.require(request, "patient")
        rid = self._path_id(request, "id")
        if not await self.db(self.store.delete_record, ctx.uid, rid):
            raise ServiceError(404, "not_found", "Запись не найдена.")
        return JSONResponse({"deleted": True})

    @contextlib.asynccontextmanager
    async def _slot_wait(self, timeout: Optional[float] = None):
        """Слот тяжёлых расчётов с ожиданием до timeout секунд (по умолчанию HISTORY_SLOT_WAIT): пробуем взять слот
        без блокировки (HeavyGate), между попытками — asyncio.sleep, цикл событий не блокируется. Не дождались —
        503 service_busy с Retry-After (как у HeavyGate)."""
        deadline = time.monotonic() + (HISTORY_SLOT_WAIT if timeout is None else timeout)
        while True:
            holder = self.gate.slot()
            try:
                holder.__enter__()
            except ServiceError:
                if time.monotonic() >= deadline:
                    raise
                await asyncio.sleep(SLOT_POLL)
                continue
            break
        try:
            yield
        finally:
            holder.__exit__(None, None, None)

    async def _history(self, uid: str, role: str) -> dict:
        """Анализ истории. Записи читаются и расшифровываются без слота; сам анализ истории по кешу ответов движка
        лёгкий (5–55 мс на 200 записей) и слота не занимает. Слот тяжёлых расчётов нужен, только если кеш
        устарел (сменилась версия движка, моделей или порогов) и записи надо пересчитать: тогда ждём слот до
        HISTORY_SLOT_WAIT секунд и лишь потом отвечаем 503 service_busy с Retry-After."""
        version = await run_in_threadpool(_versions_key)
        records = await self.db(self.store.all_records, uid)
        if any(self._stale(r, version) for r in records):
            async with self._slot_wait():
                await self.db(self._refresh_sync, uid, records, version)
        return await self.db(self._history_analyze_sync, records, role)

    async def history(self, request: Request) -> Response:
        ctx = await self.require(request, "patient")
        return JSONResponse(await self._history(ctx.uid, "patient"))

    # ---- выгрузка и загрузка ----------------------------------------------------------------
    async def export(self, request: Request) -> Response:
        ctx = await self.require(request, "patient")
        fmt = request.query_params.get("format", "json")
        if fmt not in ("json", "csv"):
            raise ServiceError(422, "invalid_format", "Формат выгрузки: json или csv.", "format")
        records = await self.db(self.store.all_records, ctx.uid)
        stamp = self._stamp(request.query_params.get("day"))
        if fmt == "csv":
            text = await run_in_threadpool(_export_csv, records)
            return Response(text, media_type="text/csv; charset=utf-8", headers={
                "Content-Disposition": f'attachment; filename="justmedit-{stamp}.csv"'})
        data = {"format": EXPORT_FORMAT, "version": 1, "exported": iso(self.store.now()),
                "records": [{"date": r["date"], "source": r["source"], "created": r["created"], "input": r["input"]}
                            for r in records]}
        return Response(json.dumps(data, ensure_ascii=False, indent=1), media_type="application/json",
                        headers={"Content-Disposition": f'attachment; filename="justmedit-{stamp}.json"'})

    def _stamp(self, raw: Any) -> str:
        """Дата в имени файла выгрузки: ?day=ГГГГ-ММ-ДД — местная дата пользователя (страница передаёт свою), если
        она в пределах суток от даты сервера (UTC); иначе — дата сервера."""
        today = _today(self.store)
        if isinstance(raw, str) and re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", raw):
            try:
                day = date.fromisoformat(raw)
            except ValueError:
                day = None
            if day is not None and abs((day - today).days) <= 1:
                return day.isoformat()
        return today.isoformat()

    def _import_check_sync(self, items: list) -> list[tuple[str, AnalysisInput]]:
        """Шаг 1 загрузки — без расчёта: каждая запись выгрузки — объект с датой и входом по правилам кабинета
        (_validate_input). Одна ошибка — 422 с полем records[i]…, ничего не считается и не сохраняется."""
        out = []
        for i, it in enumerate(items):
            prefix = f"records[{i}]."
            if not isinstance(it, dict):
                raise ServiceError(422, "invalid_record", "Запись выгрузки — объект {date, input}.", f"records[{i}]")
            if it.get("date") is None:
                raise ServiceError(422, "invalid_date", "У записи нет даты (ГГГГ-ММ-ДД).", prefix + "date")
            day = self._check_date(it.get("date"), prefix + "date")
            out.append((day, _validate_input(it.get("input"), prefix)))
        return out

    def _import_new_sync(self, uid: str, checked: list[tuple[str, AnalysisInput]]) -> list[tuple[str, AnalysisInput]]:
        """Записи файла, которых ещё нет в кабинете. Совпадение — тот же _dedup_key (дата, пол, возраст,
        беременность, нормализованные значения). Повторы считаются как мультимножество: если в кабинете один такой
        анализ, а в файле два, пропускается один — так загрузка своей же выгрузки ничего не удваивает, а выгрузка
        в новый кабинет переносится целиком, с повторами одного дня."""
        have: dict[str, int] = {}
        for rec in self.store.all_records(uid):
            try:
                key = _dedup_key(rec["date"], rec["input"])
            except (ValidationError, TypeError, ValueError):
                continue                              # старая запись не по схеме — ни с чем не совпадает
            have[key] = have.get(key, 0) + 1
        fresh = []
        for day, model in checked:
            key = _dedup_key(day, model)
            if have.get(key, 0) > 0:
                have[key] -= 1
                continue
            fresh.append((day, model))
        return fresh

    def _import_compute_sync(self, checked: list[tuple[str, AnalysisInput]]) -> list[dict]:
        """Шаг 2 — расчёт движком всех проверенных записей (всё или ничего)."""
        version, out = _versions_key(), []
        for i, (day, model) in enumerate(checked):
            inp, result = _compute(model, f"records[{i}].")
            out.append({"date": day, "source": "import", "input": inp, "result": result,
                        "summary": _summary(result, day), "version": version})
        return out

    async def import_(self, request: Request) -> Response:
        """JSON выгрузки -> записи (всё или ничего). Сначала проверяются все записи (схема, правила кабинета —
        без расчёта); записи, которые уже есть в кабинете (та же дата и тот же вход — _import_new_sync), не
        загружаются второй раз; остальные считаются движком под слотом тяжёлых расчётов. Ответ
        {"imported": N, "skipped": M}. Загрузка — тяжёлый запрос: не больше DL_UI_RATE_PER_MIN в минуту на
        пользователя (общий лимит с /api/v1/batch|benchmark|parse)."""
        ctx = await self.require(request, "patient")
        body = await self.body(request, self.settings.max_upload_bytes)
        if body.get("format") not in (None, EXPORT_FORMAT):
            raise ServiceError(422, "invalid_format", "Это не выгрузка кабинета Justmedit.", "format")
        items = body.get("records")
        if not isinstance(items, list) or not items:
            raise ServiceError(422, "invalid_body", "Поле records — непустой список записей {date, input}.", "records")
        if len(items) > MAX_RECORDS:
            raise ServiceError(422, "too_many_records", f"В кабинете можно хранить не больше {MAX_RECORDS} анализов: "
                                                        f"в файле {len(items)}.", "records")
        retry = self.heavy_limiter.check("user:" + ctx.uid)
        if retry is not None:
            raise rate_limit_error(retry, f"Слишком много тяжёлых запросов (загрузка, пакет, файл): не больше "
                                          f"{self.heavy_limiter.limit} в минуту. Повторите через {retry} с.")
        checked = await run_in_threadpool(self._import_check_sync, items)
        fresh = await self.db(self._import_new_sync, ctx.uid, checked)
        skipped = len(checked) - len(fresh)
        if not fresh:
            return JSONResponse({"imported": 0, "skipped": skipped})
        have = await self.db(self.store.count_records, ctx.uid)
        if have + len(fresh) > MAX_RECORDS:
            raise ServiceError(422, "too_many_records",
                               f"В кабинете можно хранить не больше {MAX_RECORDS} анализов: сейчас {have}, "
                               f"новых в файле {len(fresh)}.", "records")
        async with self._slot_wait():
            prepared = await run_in_threadpool(self._import_compute_sync, fresh)
        ids = await self.db(self.store.add_records, ctx.uid, prepared, MAX_RECORDS)
        return JSONResponse({"imported": len(ids), "skipped": skipped})

    # ---- доступ врача по ссылке ---------------------------------------------------------------
    async def _shares(self, uid: str) -> dict:
        rows = await self.db(self.store.list_shares, uid)
        return {"shares": [_share_json(self.store, r) for r in rows]}

    async def shares(self, request: Request) -> Response:
        ctx = await self.require(request, "patient")
        return JSONResponse(await self._shares(ctx.uid))

    async def create_share(self, request: Request) -> Response:
        """Ссылка врачу. По Bearer — только с паролем аккаунта (поле password): украденный токен API не откроет
        историю пациента постороннему «врачу»."""
        ctx = await self.require(request, "patient")
        body = await self.body(request)
        days = body.get("days")
        if isinstance(days, bool) or days not in SHARE_DAYS:
            raise ServiceError(422, "invalid_days", "Срок доступа (days): 1, 7, 30 или 90 дней.", "days")
        await self._confirm_password(request, ctx, body.get("password"))
        token, row = await self.db(self.store.create_share, ctx.uid, int(days))
        row = await self.db(self.store.get_share, ctx.uid, row["id"])
        return JSONResponse({"share": _share_json(self.store, row), "link_token": token, "link": f"/share#{token}"})

    async def delete_share(self, request: Request) -> Response:
        ctx = await self.require(request, "patient")
        sid = self._path_id(request, "id")
        if not await self.db(self.store.revoke_share, ctx.uid, sid):
            raise ServiceError(404, "not_found", "Ссылка не найдена или уже отозвана.")
        return JSONResponse(await self._shares(ctx.uid))

    async def access_log(self, request: Request) -> Response:
        ctx = await self.require(request, "patient")
        rows = await self.db(self.store.access_log, ctx.uid)
        # doctor: {login, clinic}; врач удалил аккаунт — снимок на момент просмотра с deleted: true; null — строка
        # записана до снимков (store.doctor_from_snapshot)
        items = await self.db(lambda: [{"doctor": self.store.doctor_from_snapshot(r, "access_log"), "what": r["what"],
                                        "record_id": r["record_id"], "at": iso(r["at"])} for r in rows])
        return JSONResponse({"items": items})

    async def accept_share(self, request: Request) -> Response:
        ctx = await self.require(request, "doctor")
        body = await self.body(request)
        token = body.get("token")
        row = await self.db(self.store.accept_share, token, ctx.uid) if isinstance(token, str) else None
        if row is None:
            raise ServiceError(404, "share_invalid", "Ссылка недействительна: она уже использована, отозвана "
                                                     "или истекла. Попросите пациента создать новую.", "token")
        return JSONResponse({"patient": {"id": row["patient_id"], "login": row["patient_login"]},
                             "expires": iso(row["expires"])})

    # ---- врач: пациенты, история, записи ------------------------------------------------------
    def _patients_sync(self, doctor_id: str) -> list[dict]:
        """Список пациентов врача. last_headline — без значений и выводов движка: только «есть ли анемия по
        последнему анализу». Сам анализ (значения, заголовок отчёта) врач видит, открыв запись или историю, —
        это пишется в журнал доступа пациента; список в журнал не пишется."""
        out = []
        for row in self.store.doctor_patients(doctor_id):
            last = self.store.last_record(row["id"])
            label = None
            if last:
                anemia = (last["summary"] or {}).get("anemia")
                label = "Последний анализ: есть признаки анемии." if anemia else "Последний анализ: анемии нет."
            out.append({"id": row["id"], "login": row["login"], "expires": iso(row["expires"]),
                        "n_records": row["n_records"], "last_date": last["date"] if last else None,
                        "last_headline": label})
        return out

    async def patients(self, request: Request) -> Response:
        ctx = await self.require(request, "doctor")
        return JSONResponse({"patients": await self.db(self._patients_sync, ctx.uid)})

    async def _patient_access(self, ctx: Ctx, request: Request) -> str:
        pid = request.path_params.get("id", "")
        if not ID_RE.fullmatch(pid) or not await self.db(self.store.has_access, ctx.uid, pid):
            raise forbidden("Нет действующего доступа к данным этого пациента: попросите новую ссылку.")
        return pid

    async def patient_history(self, request: Request) -> Response:
        ctx = await self.require(request, "doctor")
        pid = await self._patient_access(ctx, request)
        await self.db(self.store.log_access, pid, ctx.uid, "history")
        return JSONResponse(await self._history(pid, "doctor"))

    async def patient_records(self, request: Request) -> Response:
        """Список записей пациента для врача (заголовок — врачебный): только при действующей связи; запрос пишется
        в журнал доступа пациента (what = records, повторы за час — одна строка)."""
        ctx = await self.require(request, "doctor")
        pid = await self._patient_access(ctx, request)
        metas = await self.db(self.store.list_records, pid)
        await self.db(self.store.log_access, pid, ctx.uid, "records")
        return JSONResponse({"records": [_record_json(m, role="doctor") for m in metas]})

    async def patient_record(self, request: Request) -> Response:
        ctx = await self.require(request, "doctor")
        pid = await self._patient_access(ctx, request)
        rid = self._path_id(request, "rid")
        found = await self.db(self._record_sync, pid, rid)
        if found is None:
            raise ServiceError(404, "not_found", "Запись не найдена.")
        await self.db(self.store.log_access, pid, ctx.uid, "record", rid)
        rec, result = found
        return JSONResponse({"record": _record_json(rec, role="doctor", with_input=True), "result": result})

    # ---- врач: референсы своей лаборатории ----------------------------------------------------
    async def _sets(self, uid: str) -> dict:
        return {"sets": await self.db(self.store.list_lab_refs, uid)}

    async def lab_refs(self, request: Request) -> Response:
        ctx = await self.require(request, "doctor")
        return JSONResponse(await self._sets(ctx.uid))

    async def create_lab_refs(self, request: Request) -> Response:
        """JSON {"name", "ranges", "default"} или multipart: file (CSV / XLSX «показатель;нижняя;верхняя;единица»),
        name, default. Границы хранятся в канонической единице сервиса, зашифрованно."""
        ctx = await self.require(request, "doctor")
        if "multipart/form-data" in request.headers.get("content-type", ""):
            from .app import MULTIPART_OVERHEAD, _read_upload
            fields, upload = await _read_upload(request, LAB_FILE_MAX_BYTES * 20 + MULTIPART_OVERHEAD)
            if upload is None:
                raise ServiceError(422, "file_required", "Передайте файл в поле file (multipart/form-data).", "file")
            filename, content = upload
            ranges = await run_in_threadpool(_ranges_from_file, content, filename)
            # имя набора по умолчанию — из имени файла: та же проверка текста, что у имени из поля name
            stem = filename.replace("\\", "/").rsplit("/", 1)[-1].rsplit(".", 1)[0].strip()
            given = fields.get("name")
            if not (given and given.strip()):
                given = stem[:NAME_MAX] or None
            name = _text(given, "name", NAME_MAX, default="Лаборатория")
            default = _flag(fields.get("default"))
        else:
            body = await self.body(request)
            name = _text(body.get("name"), "name", NAME_MAX)
            ranges = await run_in_threadpool(_canonical_ranges, body.get("ranges"))
            default = _flag(body.get("default"))
        await self.db(self.store.add_lab_ref, ctx.uid, name, ranges, default)
        return JSONResponse(await self._sets(ctx.uid))

    async def update_lab_refs(self, request: Request) -> Response:
        """PATCH {"name"?, "default"?: bool, "ranges"?} — правка набора без пересоздания (например, переключить
        «по умолчанию»). Поля проверяются так же, как при создании; пустое тело — 422."""
        ctx = await self.require(request, "doctor")
        lid = self._path_id(request, "id")
        body = await self.body(request)
        if not any(k in body for k in ("name", "default", "ranges")):
            raise ServiceError(422, "missing_field", "Укажите, что изменить: name, default или ranges.")
        name = _text(body.get("name"), "name", NAME_MAX) if "name" in body else None
        default = body.get("default")
        if "default" in body and not isinstance(default, bool):
            raise ServiceError(422, "invalid_value", "default — true или false.", "default")
        ranges = await run_in_threadpool(_canonical_ranges, body.get("ranges")) if "ranges" in body else None
        if not await self.db(self.store.update_lab_ref, ctx.uid, lid, name=name, ranges=ranges, default=default):
            raise ServiceError(404, "not_found", "Набор референсов не найден.")
        return JSONResponse(await self._sets(ctx.uid))

    async def delete_lab_refs(self, request: Request) -> Response:
        ctx = await self.require(request, "doctor")
        lid = self._path_id(request, "id")
        if not await self.db(self.store.delete_lab_ref, ctx.uid, lid):
            raise ServiceError(404, "not_found", "Набор референсов не найден.")
        return JSONResponse(await self._sets(ctx.uid))

    # ---- таблица маршрутов ----------------------------------------------------------------------
    def routes(self) -> list[tuple[str, Callable, list[str]]]:
        p = "/api/v1"
        return [
            (f"{p}/auth/register", self.register, ["POST"]),
            (f"{p}/auth/login", self.login, ["POST"]),
            (f"{p}/auth/logout", self.logout, ["POST"]),
            (f"{p}/me", self.me, ["GET"]),
            (f"{p}/me", self.delete_me, ["DELETE"]),
            (f"{p}/me/logout-all", self.logout_all, ["POST"]),
            (f"{p}/me/password", self.change_password, ["POST"]),
            (f"{p}/me/tokens", self.tokens, ["GET"]),
            (f"{p}/me/tokens", self.create_token, ["POST"]),
            (f"{p}/me/tokens/{{id}}", self.delete_token, ["DELETE"]),
            (f"{p}/me/records", self.records, ["GET"]),
            (f"{p}/me/records", self.create_record, ["POST"]),
            (f"{p}/me/records/{{id}}", self.record, ["GET"]),
            (f"{p}/me/records/{{id}}", self.delete_record, ["DELETE"]),
            (f"{p}/me/history", self.history, ["GET"]),
            (f"{p}/me/export", self.export, ["GET"]),
            (f"{p}/me/import", self.import_, ["POST"]),
            (f"{p}/me/shares", self.shares, ["GET"]),
            (f"{p}/me/shares", self.create_share, ["POST"]),
            (f"{p}/me/shares/{{id}}", self.delete_share, ["DELETE"]),
            (f"{p}/me/access-log", self.access_log, ["GET"]),
            (f"{p}/shares/accept", self.accept_share, ["POST"]),
            (f"{p}/doctor/patients", self.patients, ["GET"]),
            (f"{p}/doctor/patients/{{id}}/history", self.patient_history, ["GET"]),
            (f"{p}/doctor/patients/{{id}}/records", self.patient_records, ["GET"]),
            (f"{p}/doctor/patients/{{id}}/records/{{rid}}", self.patient_record, ["GET"]),
            (f"{p}/doctor/lab-refs", self.lab_refs, ["GET"]),
            (f"{p}/doctor/lab-refs", self.create_lab_refs, ["POST"]),
            (f"{p}/doctor/lab-refs/{{id}}", self.update_lab_refs, ["PATCH"]),
            (f"{p}/doctor/lab-refs/{{id}}", self.delete_lab_refs, ["DELETE"]),
        ]
