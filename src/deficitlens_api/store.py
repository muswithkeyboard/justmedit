"""Хранилище личного кабинета (ADR 0009): SQLite в каталоге DL_DATA_DIR, шифрование Fernet.

Что где лежит (docs/CONTRACTS.md, «Хранение и кабинет»; docs/security.md, «Личный кабинет»):
  * users      — логин (как введён и в нижнем регистре для поиска), роль, клиника, хэш пароля scrypt, даты —
                 открытым текстом: они нужны для поиска и не являются результатами анализов;
  * sessions   — только SHA-256 токенов (cookie сайта и токены API), сроки;
  * records    — анализ пациента: enc_payload = {вход AnalysisInput, кеш ответа движка}, enc_summary = {заголовки
                 отчётов врачу и пациенту, анемия да/нет, дата}; оба поля зашифрованы. Открыто — дата, источник,
                 версия движка, время создания;
  * shares     — связи «пациент — врач»: SHA-256 одноразового токена ссылки, срок, отзыв; enc_doctor — снимок логина
                 и клиники врача на момент принятия (зашифрован): связь остаётся в истории пациента и после
                 удаления аккаунта врача;
  * access_log — кто из врачей и когда открывал историю или запись пациента (без содержимого); enc_doctor — снимок
                 логина и клиники врача на момент просмотра (зашифрован) — журнал пациента не теряет имя врача,
                 удалившего аккаунт;
  * lab_refs   — референсы лаборатории врача: имя открыто, сами границы зашифрованы.

Шифрование: Fernet (AES-128-CBC + HMAC-SHA256, библиотека cryptography). Ключ — DL_DATA_KEY или файл
DL_DATA_KEY_FILE (права не шире 0600; можно несколько ключей через запятую: первый шифрует, остальные только
расшифровывают — смена ключа без потери данных); если ключа нет, он создаётся файлом <DL_DATA_DIR>/key с правами
0600 (только для демо) и в журнал пишется одна строка-предупреждение без самого ключа. В открытый текст каждого
зашифрованного поля входит «привязка» (таблица, id строки, владелец): поле, перенесённое в чужую строку базы,
не расшифруется как своё.

SQLite: WAL, foreign_keys=ON (каскадное удаление), secure_delete=ON (удалённые строки и страницы затираются нулями
сразу при удалении), auto_vacuum=INCREMENTAL (для новой базы). Полного VACUUM нет: он переписывает всю базу под
общим замком и при большой базе останавливает кабинет для всех. После удалений — wal_checkpoint(TRUNCATE): копии
удалённых строк не остаются в журнале WAL; после удаления аккаунта свободные страницы отдаются ОС небольшими
порциями incremental_vacuum, замок берётся на каждую порцию отдельно. Одно соединение на процесс под замком:
uvicorn — один процесс, обработчики вызывают хранилище из пула потоков (run_in_threadpool). База открывается при
первом обращении: без кабинета сервис работает как раньше и ничего не пишет на диск.
Миграции — CREATE TABLE IF NOT EXISTS.

Пределы (защита диска от переполнения): размер базы — DL_DATA_MAX_MB (507 storage_full на новые записи, наборы
референсов, ссылки и регистрации; вход, чтение, выгрузка и удаление работают); данные одного пользователя —
USER_QUOTA_BYTES шифртекста (записи и наборы референсов); регистраций за сутки — DL_MAX_SIGNUPS_PER_DAY (считаются
по базе, переживают перезапуск); именованных токенов API — MAX_NAMED_TOKENS (сверх — 422, старые не вытесняются).
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import secrets
import sqlite3
import stat
import threading
import time
import uuid
import zlib
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Optional

log = logging.getLogger("deficitlens.store")

DB_NAME = "justmedit.db"
KEY_NAME = "key"
LINK_TTL = 48 * 3600                 # одноразовая ссылка для врача живёт 48 ч до принятия
SHARE_DAYS = (1, 7, 30, 90)
MAX_ACTIVE_SHARES = 50               # ожидающих и действующих связей у одного пациента
MAX_LAB_SETS = 20                    # наборов референсов у одного врача
MAX_LOGINS = 20                      # входов (cookie сайта + её токен «Вход»): сверх — вытесняются самые старые
MAX_NAMED_TOKENS = 20                # именованных токенов API: сверх — 422 too_many_tokens, никто не вытесняется
USER_QUOTA_BYTES = 5 * 1024 * 1024   # шифртекст записей и наборов референсов одного пользователя
DEFAULT_MAX_BYTES = 512 * 1024 * 1024
ACCESS_LOG_LIMIT = 500               # сколько последних записей журнала доступа отдаётся пациенту
ACCESS_LOG_MERGE = 3600              # повторы «врач — что» в пределах часа — одна строка журнала доступа
SIGNUP_WINDOW = 86400                # окно общего предела регистраций, с
VACUUM_STEP_PAGES = 64               # incremental_vacuum: страниц за одну порцию под замком
VACUUM_MAX_STEPS = 256               # не больше порций после одного удаления (64 × 256 × 4 КБ = 64 МБ)
TOUCH_EVERY = 60                     # last_used обновляется не чаще раза в минуту

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
  id          TEXT PRIMARY KEY,
  login       TEXT NOT NULL,
  login_key   TEXT NOT NULL UNIQUE,
  role        TEXT NOT NULL CHECK (role IN ('patient', 'doctor')),
  pw_hash     TEXT NOT NULL,
  clinic      TEXT,
  consent_at  INTEGER NOT NULL,
  created     INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
  id           TEXT PRIMARY KEY,
  token_sha256 TEXT NOT NULL UNIQUE,
  user_id      TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  kind         TEXT NOT NULL CHECK (kind IN ('cookie', 'api')),
  name         TEXT,
  parent_id    TEXT,
  created      INTEGER NOT NULL,
  expires      INTEGER NOT NULL,
  last_used    INTEGER
);
CREATE INDEX IF NOT EXISTS sessions_user ON sessions(user_id, kind);
CREATE TABLE IF NOT EXISTS records (
  id             TEXT PRIMARY KEY,
  user_id        TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  date           TEXT NOT NULL,
  source         TEXT NOT NULL,
  enc_payload    BLOB NOT NULL,
  engine_version TEXT NOT NULL,
  enc_summary    BLOB NOT NULL,
  created        INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS records_user ON records(user_id, date);
CREATE TABLE IF NOT EXISTS shares (
  id          TEXT PRIMARY KEY,
  patient_id  TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  doctor_id   TEXT REFERENCES users(id) ON DELETE CASCADE,
  link_sha256 TEXT UNIQUE,
  days        INTEGER NOT NULL,
  created     INTEGER NOT NULL,
  accepted    INTEGER,
  expires     INTEGER NOT NULL,
  revoked     INTEGER,
  enc_doctor  BLOB
);
CREATE INDEX IF NOT EXISTS shares_patient ON shares(patient_id);
CREATE INDEX IF NOT EXISTS shares_doctor ON shares(doctor_id);
CREATE TABLE IF NOT EXISTS access_log (
  id         INTEGER PRIMARY KEY,
  patient_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  doctor_id  TEXT REFERENCES users(id) ON DELETE SET NULL,
  what       TEXT NOT NULL CHECK (what IN ('history', 'record', 'records')),
  record_id  TEXT,
  at         INTEGER NOT NULL,
  enc_doctor BLOB
);
CREATE INDEX IF NOT EXISTS access_log_patient ON access_log(patient_id, at);
CREATE TABLE IF NOT EXISTS lab_refs (
  id         TEXT PRIMARY KEY,
  doctor_id  TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  name       TEXT NOT NULL,
  enc_ranges BLOB NOT NULL,
  is_default INTEGER NOT NULL DEFAULT 0,
  created    INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS lab_refs_doctor ON lab_refs(doctor_id);
"""


def _migrate(conn: sqlite3.Connection) -> None:
    """Миграции схемы, которые не делает CREATE TABLE IF NOT EXISTS.
      * access_log: в CHECK добавлен вид 'records' (врач открыл список записей пациента);
      * access_log, shares: столбец enc_doctor — зашифрованный снимок логина и клиники врача (у строк, записанных
        раньше, его нет: после удаления аккаунта врача они показываются как «аккаунт удалён» без логина)."""
    _migrate_access_log_check(conn)
    for table in ("access_log", "shares"):
        columns = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        if "enc_doctor" not in columns:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN enc_doctor BLOB")


def _migrate_access_log_check(conn: sqlite3.Connection) -> None:
    """access_log: вид 'records' в CHECK. Ограничение CHECK в SQLite не меняется на месте: таблица пересоздаётся
    с теми же строками в одной транзакции."""
    row = conn.execute("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'access_log'").fetchone()
    if row is None or "'records'" in (row[0] or ""):
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute("CREATE TABLE access_log_new (id INTEGER PRIMARY KEY, "
                     "patient_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE, "
                     "doctor_id TEXT REFERENCES users(id) ON DELETE SET NULL, "
                     "what TEXT NOT NULL CHECK (what IN ('history', 'record', 'records')), "
                     "record_id TEXT, at INTEGER NOT NULL)")
        conn.execute("INSERT INTO access_log_new (id, patient_id, doctor_id, what, record_id, at) "
                     "SELECT id, patient_id, doctor_id, what, record_id, at FROM access_log")
        conn.execute("DROP TABLE access_log")
        conn.execute("ALTER TABLE access_log_new RENAME TO access_log")
        conn.execute("CREATE INDEX IF NOT EXISTS access_log_patient ON access_log(patient_id, at)")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


class StoreError(Exception):
    """Ошибка хранилища, понятная веб-слою: code — машинный код ответа, message — текст по-русски."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code, self.message = code, message


class LoginTaken(StoreError):
    def __init__(self):
        super().__init__("login_taken", "Этот логин уже занят.")


class TooManyRecords(StoreError):
    def __init__(self, limit: int):
        super().__init__("too_many_records", f"В кабинете можно хранить не больше {limit} анализов: удалите старые.")


class StorageFull(StoreError):
    def __init__(self):
        super().__init__("storage_full", "Хранилище кабинета заполнено: новые данные сейчас не принимаются. "
                                         "Вход, просмотр, выгрузка и удаление работают.")


class QuotaExceeded(StoreError):
    def __init__(self, limit: int):
        super().__init__("quota_exceeded", f"Объём данных в кабинете — не больше {limit // (1024 * 1024)} МБ: "
                                           "удалите старые записи.")


class TooManyTokens(StoreError):
    def __init__(self, limit: int):
        super().__init__("too_many_tokens", f"Токенов API не больше {limit}: удалите ненужные в списке токенов.")


class SignupsPaused(StoreError):
    retry_after = 3600

    def __init__(self):
        super().__init__("signups_paused", "Регистрация временно приостановлена: достигнут суточный предел новых "
                                           "аккаунтов. Повторите позже.")


def sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", "surrogateescape")).hexdigest()


def iso(ts: Optional[int]) -> Optional[str]:
    """Секунды Unix -> «2026-10-04T18:00:00Z» (UTC)."""
    if ts is None:
        return None
    return datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def new_id() -> str:
    return uuid.uuid4().hex


class Store:
    """Хранилище кабинета. clock — источник времени (секунды Unix); тесты подменяют его, чтобы проверить сроки."""

    def __init__(self, data_dir: str | os.PathLike, data_key: Optional[str] = None,
                 clock: Callable[[], float] = time.time, *, key_file: Optional[str] = None,
                 max_bytes: int = DEFAULT_MAX_BYTES, max_signups_per_day: int = 1000):
        self.data_dir = Path(data_dir)
        self._key_env = data_key
        self._key_file = key_file
        self.clock = clock
        self.max_bytes = int(max_bytes)
        self.max_signups_per_day = int(max_signups_per_day)
        self.user_quota = USER_QUOTA_BYTES
        self._lock = threading.RLock()
        self._conn: Optional[sqlite3.Connection] = None
        self._fernet = None

    # ---- открытие, ключ, транзакции ---------------------------------------------------------
    def now(self) -> int:
        return int(self.clock())

    @property
    def db_path(self) -> Path:
        return self.data_dir / DB_NAME

    def _read_key_file(self) -> list[str]:
        """Ключи из DL_DATA_KEY_FILE (секрет Docker, файл из хранилища секретов). Права шире 0600 (файл читают группа
        или все) — отказ: такой ключ уже нельзя считать секретом. Файл в каталоге базы — предупреждение: ключ
        рядом с базой не защищает от утечки тома."""
        path = Path(self._key_file)
        try:
            mode = stat.S_IMODE(path.stat().st_mode)
            if mode & 0o077:
                log.error("cabinet: DL_DATA_KEY_FILE is readable by group or others (mode %o); chmod 600", mode)
                raise StoreError("storage_unavailable", "Хранилище кабинета недоступно: файл ключа доступен другим "
                                                        "пользователям (нужны права 0600).")
            text = path.read_text(encoding="ascii")
        except OSError:
            log.error("cabinet: DL_DATA_KEY_FILE cannot be read")
            raise StoreError("storage_unavailable", "Хранилище кабинета недоступно: не читается файл ключа "
                                                    "DL_DATA_KEY_FILE.") from None
        except UnicodeDecodeError:
            raise StoreError("storage_unavailable", "Хранилище кабинета недоступно: неверный ключ шифрования.") \
                from None
        try:
            inside = path.resolve().is_relative_to(self.data_dir.resolve())
        except OSError:
            inside = False
        if inside:
            log.warning("cabinet: DL_DATA_KEY_FILE lies in DL_DATA_DIR; keep the key outside the database volume")
        return [k.strip() for k in text.replace("\n", ",").split(",") if k.strip()]

    def _load_key(self):
        """Fernet / MultiFernet из DL_DATA_KEY, из файла DL_DATA_KEY_FILE или из файла <data_dir>/key (демо: создаётся
        с правами 0600)."""
        from cryptography.fernet import Fernet, MultiFernet

        if self._key_env and self._key_file:
            log.error("cabinet: both DL_DATA_KEY and DL_DATA_KEY_FILE are set")
            raise StoreError("storage_unavailable", "Хранилище кабинета недоступно: задайте либо DL_DATA_KEY, "
                                                    "либо DL_DATA_KEY_FILE.")
        if self._key_env:
            raw = [k.strip() for k in self._key_env.split(",") if k.strip()]
            source = "DL_DATA_KEY"
        elif self._key_file:
            raw = self._read_key_file()
            source = "DL_DATA_KEY_FILE"
        else:
            path = self.data_dir / KEY_NAME
            try:
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                raw = [path.read_text(encoding="ascii").strip()]
                log.warning("cabinet: encryption key is read from file %r; set DL_DATA_KEY outside the demo", str(path))
            else:
                key = Fernet.generate_key()
                with os.fdopen(fd, "wb") as f:
                    f.write(key + b"\n")
                raw = [key.decode("ascii")]
                log.warning("cabinet: DL_DATA_KEY is not set; a new encryption key was written to %r (demo only)",
                            str(path))
            source = "key file"
        try:
            keys = [Fernet(k) for k in raw]
        except (ValueError, TypeError):
            log.error("cabinet: %s is not a valid Fernet key (urlsafe base64 of 32 bytes)", source)
            raise StoreError("storage_unavailable", "Хранилище кабинета недоступно: неверный ключ шифрования.") from None
        if not keys:
            raise StoreError("storage_unavailable", "Хранилище кабинета недоступно: пустой ключ шифрования.")
        return MultiFernet(keys)

    def _open(self) -> sqlite3.Connection:
        with self._lock:
            if self._conn is not None:
                return self._conn
            try:
                self.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
                fernet = self._load_key()
                db = self.db_path
                if not db.exists():                       # файл базы — сразу с правами 0600 (WAL и SHM — те же)
                    os.close(os.open(db, os.O_WRONLY | os.O_CREAT, 0o600))
                conn = sqlite3.connect(str(db), check_same_thread=False, isolation_level=None, timeout=15)
                conn.row_factory = sqlite3.Row
                conn.execute("PRAGMA auto_vacuum=INCREMENTAL")   # действует только для новой (пустой) базы
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA foreign_keys=ON")
                conn.execute("PRAGMA secure_delete=ON")
                conn.execute("PRAGMA temp_store=MEMORY")
                conn.execute("PRAGMA synchronous=NORMAL")
                conn.executescript(SCHEMA)
                _migrate(conn)
            except StoreError:
                raise
            except (OSError, sqlite3.Error) as e:
                log.error("cabinet: storage is not available (%s)", type(e).__name__)
                raise StoreError("storage_unavailable",
                                 "Хранилище кабинета недоступно: проверьте каталог DL_DATA_DIR.") from None
            self._fernet, self._conn = fernet, conn
            return conn

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        """Транзакция с блокировкой записи с самого начала (BEGIN IMMEDIATE): проверка предела и вставка атомарны."""
        conn = self._open()
        with self._lock:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")

    def _all(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        conn = self._open()
        with self._lock:
            return conn.execute(sql, params).fetchall()

    def _one(self, sql: str, params: tuple = ()) -> Optional[sqlite3.Row]:
        conn = self._open()
        with self._lock:
            return conn.execute(sql, params).fetchone()

    # ---- место на диске и стирание ----------------------------------------------------------
    def used_bytes(self, c: Optional[sqlite3.Connection] = None) -> int:
        """Занятый объём базы: страницы минус свободные (свободные переиспользуются новыми записями)."""
        c = c or self._open()
        with self._lock:
            page_size = int(c.execute("PRAGMA page_size").fetchone()[0])
            pages = int(c.execute("PRAGMA page_count").fetchone()[0])
            free = int(c.execute("PRAGMA freelist_count").fetchone()[0])
        return max(0, pages - free) * page_size

    def _check_space(self, c: sqlite3.Connection, extra: int = 0) -> None:
        """Внутри транзакции записи: база с новыми данными больше DL_DATA_MAX_MB — 507 storage_full."""
        if self.used_bytes(c) + extra > self.max_bytes:
            log.warning("cabinet: database size limit reached (DL_DATA_MAX_MB)")
            raise StorageFull()

    def checkpoint(self) -> None:
        """Перенос WAL в базу и усечение WAL до нуля: копии удалённых строк не остаются в файле -wal (в основном
        файле их уже затёр secure_delete). Ошибка (база занята) — не повод отменять удаление: повторится позже."""
        conn = self._open()
        with self._lock:
            try:
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall()
            except sqlite3.Error as e:
                log.warning("cabinet: wal checkpoint failed (%s)", type(e).__name__)

    def compact(self, steps: int = VACUUM_MAX_STEPS) -> int:
        """Отдать ОС свободные страницы порциями по VACUUM_STEP_PAGES (auto_vacuum=INCREMENTAL): замок берётся на
        каждую порцию отдельно — другие запросы идут между порциями. В базе без auto_vacuum (созданной до этого
        изменения) ничего не делает: свободные страницы затёрты нулями и переиспользуются. Возвращает число порций."""
        conn = self._open()
        done = 0
        for _ in range(int(steps)):
            with self._lock:
                if int(conn.execute("PRAGMA auto_vacuum").fetchone()[0]) != 2:
                    break
                if int(conn.execute("PRAGMA freelist_count").fetchone()[0]) == 0:
                    break
                conn.execute(f"PRAGMA incremental_vacuum({VACUUM_STEP_PAGES})").fetchall()
            done += 1
        return done

    # ---- шифрование -------------------------------------------------------------------------
    def seal(self, obj: Any, bind: str) -> bytes:
        """JSON -> (сжатие zlib) -> Fernet. bind — «таблица:id:владелец», проверяется при расшифровке."""
        self._open()
        raw = json.dumps({"b": bind, "d": obj}, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        return self._fernet.encrypt(zlib.compress(raw, 6))

    def unseal(self, token: bytes, bind: str) -> Any:
        from cryptography.fernet import InvalidToken

        self._open()
        try:
            data = json.loads(zlib.decompress(self._fernet.decrypt(bytes(token))).decode("utf-8"))
        except (InvalidToken, zlib.error, ValueError):
            log.error("cabinet: a stored field cannot be decrypted (wrong DL_DATA_KEY?)")
            raise StoreError("storage_key", "Данные кабинета не расшифровываются: ключ DL_DATA_KEY не совпадает "
                                            "с ключом, которым они записаны.") from None
        if not isinstance(data, dict) or data.get("b") != bind:
            log.error("cabinet: a stored field is bound to another row")
            raise StoreError("storage_key", "Данные кабинета повреждены.")
        return data.get("d")

    # ---- пользователи -----------------------------------------------------------------------
    @staticmethod
    def public_user(row: sqlite3.Row) -> dict:
        return {"id": row["id"], "login": row["login"], "role": row["role"], "clinic": row["clinic"],
                "created": iso(row["created"])}

    def create_user(self, login: str, login_key: str, role: str, pw_hash: str, clinic: Optional[str]) -> sqlite3.Row:
        """Новый пользователь. В той же транзакции (BEGIN IMMEDIATE): общий предел регистраций за сутки (по базе —
        переживает перезапуск, параллельные запросы его не обойдут) и предел размера базы."""
        uid, now = new_id(), self.now()
        try:
            with self.tx() as c:
                n = int(c.execute("SELECT COUNT(*) FROM users WHERE created > ?", (now - SIGNUP_WINDOW,)).fetchone()[0])
                if n >= self.max_signups_per_day:
                    log.warning("cabinet: daily signup limit reached (DL_MAX_SIGNUPS_PER_DAY)")
                    raise SignupsPaused()
                self._check_space(c, 4096)
                c.execute("INSERT INTO users (id, login, login_key, role, pw_hash, clinic, consent_at, created) "
                          "VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (uid, login, login_key, role, pw_hash, clinic, now, now))
        except sqlite3.IntegrityError:
            raise LoginTaken() from None
        return self.user_by_id(uid)

    def set_password(self, uid: str, pw_hash: str, keep_session: str) -> int:
        """Новый хэш пароля и отзыв всех остальных сессий и токенов пользователя (кроме текущего входа) — одной
        транзакцией. Возвращает число отозванных."""
        with self.tx() as c:
            c.execute("UPDATE users SET pw_hash = ? WHERE id = ?", (pw_hash, uid))
            return self._revoke_others(c, uid, keep_session)

    def user_by_login(self, login_key: str) -> Optional[sqlite3.Row]:
        return self._one("SELECT * FROM users WHERE login_key = ?", (login_key,))

    def user_by_id(self, uid: str) -> Optional[sqlite3.Row]:
        return self._one("SELECT * FROM users WHERE id = ?", (uid,))

    def delete_user(self, uid: str) -> None:
        """Полное удаление: пользователь и всё его (сессии, записи, связи как пациента, референсы, журнал доступа
        к его данным) одной транзакцией по каскаду. secure_delete=ON затирает удалённые строки нулями прямо при
        удалении; затем свободные страницы отдаются ОС порциями (compact) и WAL усекается (checkpoint) — удалённых
        строк нет ни в файле базы, ни в журнале WAL. Полного VACUUM нет: он держал бы общий замок, пока
        переписывается вся база.
        Данные пациентов об удалённом враче — это данные пациентов: строки их журнала доступа и их связи с этим
        врачом остаются без ссылки на него (doctor_id = NULL; связь — закрыта, если ещё действовала) с зашифрованным
        снимком логина и клиники на момент события (enc_doctor) — пациент видит «логин (аккаунт удалён)»."""
        with self.tx() as c:
            c.execute("UPDATE shares SET doctor_id = NULL, link_sha256 = NULL, revoked = COALESCE(revoked, ?) "
                      "WHERE doctor_id = ? AND patient_id != ?", (self.now(), uid, uid))
            c.execute("DELETE FROM shares WHERE patient_id = ? OR doctor_id = ?", (uid, uid))
            c.execute("DELETE FROM users WHERE id = ?", (uid,))
        self.compact()
        self.checkpoint()

    # ---- сессии и токены --------------------------------------------------------------------
    @staticmethod
    def _insert_session(c: sqlite3.Connection, uid: str, kind: str, expires: int, now: int, *,
                        name: Optional[str] = None, parent_id: Optional[str] = None) -> tuple[str, str]:
        """Строка сессии; last_used = NULL — токеном ещё не пользовались (отметка ставится при первом запросе)."""
        token, sid = secrets.token_urlsafe(32), new_id()
        c.execute("INSERT INTO sessions (id, token_sha256, user_id, kind, name, parent_id, created, expires, "
                  "last_used) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL)",
                  (sid, sha256_hex(token), uid, kind, name, parent_id, now, int(expires)))
        return token, sid

    def create_login(self, uid: str, ttl: int, api_name: str = "Вход") -> tuple[str, str, sqlite3.Row]:
        """Вход: cookie сайта и её парный токен API «Вход» с тем же сроком (выход или истечение cookie закрывают
        оба). Это отдельный учёт от именованных токенов: сверх MAX_LOGINS входов вытесняются самые старые (cookie
        и её токен вместе) — именованные токены интеграций не трогаются. Возвращает (cookie, токен, строка cookie)."""
        now = self.now()
        with self.tx() as c:
            c.execute("DELETE FROM sessions WHERE expires <= ?", (now,))
            cookie, cid = self._insert_session(c, uid, "cookie", now + int(ttl), now)
            api, _ = self._insert_session(c, uid, "api", now + int(ttl), now, name=api_name, parent_id=cid)
            old = [r[0] for r in c.execute("SELECT id FROM sessions WHERE user_id = ? AND kind = 'cookie' "
                                           "ORDER BY created DESC, rowid DESC LIMIT -1 OFFSET ?", (uid, MAX_LOGINS))]
            for sid in old:
                c.execute("DELETE FROM sessions WHERE (id = ? OR parent_id = ?) AND user_id = ?", (sid, sid, uid))
            # токены входа без своей cookie (остались от прежних версий) — тот же предел
            c.execute("DELETE FROM sessions WHERE id IN (SELECT id FROM sessions WHERE user_id = ? AND kind = 'api' "
                      "AND parent_id IS NOT NULL ORDER BY created DESC, rowid DESC LIMIT -1 OFFSET ?)",
                      (uid, MAX_LOGINS))
        return cookie, api, self._one("SELECT * FROM sessions WHERE id = ?", (cid,))

    def create_session(self, uid: str, kind: str, ttl: int, *, name: Optional[str] = None,
                       parent_id: Optional[str] = None, expires: Optional[int] = None) -> tuple[str, sqlite3.Row]:
        """Новый токен: в базу — только SHA-256. Возвращает (значение токена, строку сессии). expires — срок не
        позже этого момента (токен, созданный по другому токену, не живёт дольше него).
        Именованный токен API (kind="api" без parent_id): сверх MAX_NAMED_TOKENS — 422 too_many_tokens, старые
        не вытесняются молча (это могут быть долгоживущие токены интеграций)."""
        now = self.now()
        end = now + int(ttl) if expires is None else min(now + int(ttl), int(expires))
        with self.tx() as c:
            c.execute("DELETE FROM sessions WHERE expires <= ?", (now,))
            if kind == "api" and parent_id is None:
                n = int(c.execute("SELECT COUNT(*) FROM sessions WHERE user_id = ? AND kind = 'api' AND parent_id "
                                  "IS NULL", (uid,)).fetchone()[0])
                if n >= MAX_NAMED_TOKENS:
                    raise TooManyTokens(MAX_NAMED_TOKENS)
            token_value, sid = self._insert_session(c, uid, kind, end, now, name=name, parent_id=parent_id)
        return token_value, self._one("SELECT * FROM sessions WHERE id = ?", (sid,))

    @staticmethod
    def _revoke_others(c: sqlite3.Connection, uid: str, keep: Optional[str]) -> int:
        """Удалить все сессии и токены пользователя, кроме входа keep (сама сессия, её пара и её дочерние)."""
        keep_ids: set[str] = set()
        if keep:
            row = c.execute("SELECT id, parent_id FROM sessions WHERE id = ? AND user_id = ?", (keep, uid)).fetchone()
            if row is not None:
                keep_ids.add(row["id"])
                if row["parent_id"]:
                    keep_ids.add(row["parent_id"])
                keep_ids.update(r[0] for r in c.execute("SELECT id FROM sessions WHERE parent_id IN (%s) AND "
                                                        "user_id = ?" % ",".join("?" * len(keep_ids)),
                                                        (*keep_ids, uid)))
        if keep_ids:
            marks = ",".join("?" * len(keep_ids))
            return c.execute(f"DELETE FROM sessions WHERE user_id = ? AND id NOT IN ({marks})",
                             (uid, *keep_ids)).rowcount
        return c.execute("DELETE FROM sessions WHERE user_id = ?", (uid,)).rowcount

    def revoke_sessions(self, uid: str, keep: Optional[str]) -> int:
        """«Выйти везде»: отозвать все сессии и токены, кроме текущего входа (keep=None — все)."""
        with self.tx() as c:
            return self._revoke_others(c, uid, keep)

    def session_by_token(self, token: str, kind: str) -> Optional[tuple[sqlite3.Row, sqlite3.Row]]:
        """(сессия, пользователь) для действующего токена этого вида; истёкший токен удаляется и не принимается."""
        if not token or len(token) > 256:
            return None
        row = self._one("SELECT * FROM sessions WHERE token_sha256 = ? AND kind = ?", (sha256_hex(token), kind))
        if row is None:
            return None
        now = self.now()
        orphan = False
        if row["parent_id"] and row["expires"] > now:
            # токен «Вход» живёт, пока жива cookie его входа (токены прежних версий жили 30 дней сами по себе)
            orphan = self._one("SELECT 1 FROM sessions WHERE id = ? AND expires > ?", (row["parent_id"], now)) is None
        if row["expires"] <= now or orphan:
            with self.tx() as c:
                c.execute("DELETE FROM sessions WHERE id = ?", (row["id"],))
            return None
        user = self.user_by_id(row["user_id"])
        if user is None:
            return None
        if row["last_used"] is None or now - row["last_used"] >= TOUCH_EVERY:
            with self.tx() as c:
                c.execute("UPDATE sessions SET last_used = ? WHERE id = ?", (now, row["id"]))
        return row, user

    def delete_session(self, uid: str, sid: str, kind: Optional[str] = None, *, with_pair: bool = True) -> bool:
        """Удаляет сессию. with_pair — и её пару из одного входа (cookie сайта и токен API «Вход»): выход на сайте
        или по токену входа закрывает обе. Удаление токена в списке «Токены API» (kind="api") пару не трогает."""
        with self.tx() as c:
            row = c.execute("SELECT * FROM sessions WHERE id = ? AND user_id = ?" + (" AND kind = ?" if kind else ""),
                            (sid, uid, kind) if kind else (sid, uid)).fetchone()
            if row is None:
                return False
            c.execute("DELETE FROM sessions WHERE id = ?", (sid,))
            if with_pair:
                c.execute("DELETE FROM sessions WHERE parent_id = ? AND user_id = ?", (sid, uid))
                if row["parent_id"]:
                    c.execute("DELETE FROM sessions WHERE id = ? AND user_id = ?", (row["parent_id"], uid))
            return True

    def list_tokens(self, uid: str) -> list[sqlite3.Row]:
        """Действующие токены API: именованные и токены «Вход», чья cookie ещё жива."""
        now = self.now()
        return self._all("SELECT s.* FROM sessions s WHERE s.user_id = ? AND s.kind = 'api' AND s.expires > ? AND "
                         "(s.parent_id IS NULL OR EXISTS (SELECT 1 FROM sessions p WHERE p.id = s.parent_id AND "
                         "p.expires > ?)) ORDER BY s.created DESC", (uid, now, now))

    # ---- записи пациента --------------------------------------------------------------------
    def _bind(self, table: str, rid: str, uid: str, part: str = "") -> str:
        return f"{table}{part}:{rid}:{uid}"

    def count_records(self, uid: str) -> int:
        return int(self._one("SELECT COUNT(*) AS n FROM records WHERE user_id = ?", (uid,))["n"])

    def user_bytes(self, uid: str, c: Optional[sqlite3.Connection] = None) -> int:
        """Объём шифртекста пользователя: записи (вход + кеш ответа + сводка) и наборы референсов врача."""
        c = c or self._open()
        with self._lock:
            rec = c.execute("SELECT COALESCE(SUM(LENGTH(enc_payload) + LENGTH(enc_summary)), 0) FROM records "
                            "WHERE user_id = ?", (uid,)).fetchone()[0]
            lab = c.execute("SELECT COALESCE(SUM(LENGTH(enc_ranges)), 0) FROM lab_refs WHERE doctor_id = ?",
                            (uid,)).fetchone()[0]
        return int(rec) + int(lab)

    def _check_quota(self, c: sqlite3.Connection, uid: str, extra: int) -> None:
        if self.user_bytes(uid, c) + extra > self.user_quota:
            raise QuotaExceeded(self.user_quota)
        self._check_space(c, extra)

    def add_records(self, uid: str, items: list[dict], max_records: int) -> list[str]:
        """items: [{"date", "source", "input", "result", "summary", "version"}]. Пределы — число записей, объём
        данных пользователя (USER_QUOTA_BYTES) и размер базы — проверяются в той же транзакции, что и вставка
        (BEGIN IMMEDIATE): параллельные запросы их не обойдут."""
        now = self.now()
        ids: list[str] = []
        rows = []
        for it in items:
            rid = new_id()
            payload = self.seal({"input": it["input"], "result": it.get("result")}, self._bind("records", rid, uid))
            summary = self.seal(it["summary"], self._bind("records", rid, uid, ".summary"))
            rows.append((rid, uid, it["date"], it["source"], payload, it["version"], summary, now))
        size = sum(len(r[4]) + len(r[6]) for r in rows)
        with self.tx() as c:
            n = int(c.execute("SELECT COUNT(*) FROM records WHERE user_id = ?", (uid,)).fetchone()[0])
            if n + len(items) > max_records:
                raise TooManyRecords(max_records)
            self._check_quota(c, uid, size)
            for row in rows:
                c.execute("INSERT INTO records (id, user_id, date, source, enc_payload, engine_version, enc_summary, "
                          "created) VALUES (?, ?, ?, ?, ?, ?, ?, ?)", row)
                ids.append(row[0])
        return ids

    def _record_meta(self, row: sqlite3.Row) -> dict:
        summary = self.unseal(row["enc_summary"], self._bind("records", row["id"], row["user_id"], ".summary")) or {}
        return {"id": row["id"], "date": row["date"], "source": row["source"], "created": iso(row["created"]),
                "summary": summary}

    def list_records(self, uid: str) -> list[dict]:
        """Записи без входа и ответа (только сводка), новые сверху."""
        rows = self._all("SELECT id, user_id, date, source, enc_summary, created FROM records WHERE user_id = ? "
                         "ORDER BY date DESC, created DESC, rowid DESC", (uid,))
        return [self._record_meta(r) for r in rows]

    def get_record(self, uid: str, rid: str) -> Optional[dict]:
        row = self._one("SELECT * FROM records WHERE id = ? AND user_id = ?", (rid, uid))
        return None if row is None else self._full(row)

    def _full(self, row: sqlite3.Row) -> dict:
        out = self._record_meta(row)
        payload = self.unseal(row["enc_payload"], self._bind("records", row["id"], row["user_id"])) or {}
        out.update(input=payload.get("input"), result=payload.get("result"), version=row["engine_version"])
        return out

    def all_records(self, uid: str) -> list[dict]:
        """Все записи со входом и кешем ответа, по дате (старые сначала) — для истории и выгрузки."""
        rows = self._all("SELECT * FROM records WHERE user_id = ? ORDER BY date ASC, created ASC, rowid ASC", (uid,))
        return [self._full(r) for r in rows]

    def update_record_result(self, uid: str, rid: str, inp: dict, result: dict, version: str) -> None:
        """Обновляет кеш ответа движка (после смены версии движка, моделей или порогов)."""
        payload = self.seal({"input": inp, "result": result}, self._bind("records", rid, uid))
        with self.tx() as c:
            c.execute("UPDATE records SET enc_payload = ?, engine_version = ? WHERE id = ? AND user_id = ?",
                      (payload, version, rid, uid))

    def delete_record(self, uid: str, rid: str) -> bool:
        """Удаление записи; затем усечение WAL — копия записи не остаётся в файле -wal."""
        with self.tx() as c:
            done = c.execute("DELETE FROM records WHERE id = ? AND user_id = ?", (rid, uid)).rowcount > 0
        if done:
            self.checkpoint()
        return done

    def last_record(self, uid: str) -> Optional[dict]:
        row = self._one("SELECT id, user_id, date, source, enc_summary, created FROM records WHERE user_id = ? "
                        "ORDER BY date DESC, created DESC, rowid DESC LIMIT 1", (uid,))
        return None if row is None else self._record_meta(row)

    # ---- связи «пациент — врач» -------------------------------------------------------------
    def share_status(self, row: sqlite3.Row) -> str:
        if row["revoked"] is not None:
            return "revoked"
        if row["expires"] <= self.now():
            return "expired"
        return "active" if row["accepted"] is not None else "pending"

    def create_share(self, patient_id: str, days: int) -> tuple[str, sqlite3.Row]:
        """Новая ссылка: токен одноразовый, в базе — только его SHA-256; до принятия живёт LINK_TTL."""
        token, sid, now = secrets.token_urlsafe(32), new_id(), self.now()
        with self.tx() as c:
            n = c.execute("SELECT COUNT(*) FROM shares WHERE patient_id = ? AND revoked IS NULL AND expires > ?",
                          (patient_id, now)).fetchone()[0]
            if n >= MAX_ACTIVE_SHARES:
                raise StoreError("too_many_shares", f"Действующих ссылок и связей не больше {MAX_ACTIVE_SHARES}: "
                                                    "отзовите ненужные.")
            self._check_space(c, 1024)
            c.execute("INSERT INTO shares (id, patient_id, doctor_id, link_sha256, days, created, accepted, expires, "
                      "revoked) VALUES (?, ?, NULL, ?, ?, ?, NULL, ?, NULL)",
                      (sid, patient_id, sha256_hex(token), int(days), now, now + LINK_TTL))
        return token, self._one("SELECT * FROM shares WHERE id = ?", (sid,))

    def list_shares(self, patient_id: str) -> list[sqlite3.Row]:
        return self._all("SELECT s.*, u.login AS doctor_login, u.clinic AS doctor_clinic FROM shares s "
                         "LEFT JOIN users u ON u.id = s.doctor_id WHERE s.patient_id = ? "
                         "ORDER BY s.created DESC, s.rowid DESC", (patient_id,))

    def get_share(self, patient_id: str, sid: str) -> Optional[sqlite3.Row]:
        return self._one("SELECT s.*, u.login AS doctor_login, u.clinic AS doctor_clinic FROM shares s "
                         "LEFT JOIN users u ON u.id = s.doctor_id WHERE s.patient_id = ? AND s.id = ?",
                         (patient_id, sid))

    def revoke_share(self, patient_id: str, sid: str) -> bool:
        """Отзыв: связь перестаёт действовать сразу; неиспользованный токен ссылки стирается (и из WAL)."""
        with self.tx() as c:
            done = c.execute("UPDATE shares SET revoked = ?, link_sha256 = NULL WHERE id = ? AND patient_id = ? "
                             "AND revoked IS NULL", (self.now(), sid, patient_id)).rowcount > 0
        if done:
            self.checkpoint()
        return done

    def accept_share(self, token: str, doctor_id: str) -> Optional[sqlite3.Row]:
        """Врач принимает ссылку: токен должен быть не использован, не отозван и не просрочен. Хэш токена стирается
        (повторно ссылкой воспользоваться нельзя), связь действует days дней с этого момента."""
        if not token or len(token) > 256:
            return None
        now = self.now()
        with self.tx() as c:
            row = c.execute("SELECT * FROM shares WHERE link_sha256 = ? AND accepted IS NULL AND revoked IS NULL "
                            "AND expires > ?", (sha256_hex(token), now)).fetchone()
            if row is None:
                return None
            snap = self._doctor_snapshot(c, doctor_id, self._bind("shares", row["id"], row["patient_id"], ".doctor"))
            c.execute("UPDATE shares SET doctor_id = ?, accepted = ?, expires = ?, link_sha256 = NULL, enc_doctor = ? "
                      "WHERE id = ?", (doctor_id, now, now + int(row["days"]) * 86400, snap, row["id"]))
        return self._one("SELECT s.*, u.login AS patient_login FROM shares s JOIN users u ON u.id = s.patient_id "
                         "WHERE s.id = ?", (row["id"],))

    def _doctor_snapshot(self, c: sqlite3.Connection, doctor_id: str, bind: str) -> Optional[bytes]:
        """Зашифрованный снимок {login, clinic} врача на момент события — для журнала и связей пациента."""
        user = c.execute("SELECT login, clinic FROM users WHERE id = ?", (doctor_id,)).fetchone()
        if user is None:
            return None
        return self.seal({"login": user["login"], "clinic": user["clinic"]}, bind)

    def doctor_from_snapshot(self, row: sqlite3.Row, table: str) -> Optional[dict]:
        """Врач строки журнала или связи: живой аккаунт — {login, clinic} из users; удалённый — снимок с пометкой
        deleted: true; строки без снимка (записаны до него) — None."""
        if row["doctor_id"] is not None:
            return {"login": row["doctor_login"], "clinic": row["doctor_clinic"]}
        if row["enc_doctor"] is None:
            return None
        snap = self.unseal(row["enc_doctor"], self._bind(table, row["id"], row["patient_id"], ".doctor")) or {}
        if not snap.get("login"):
            return None
        return {"login": snap.get("login"), "clinic": snap.get("clinic"), "deleted": True}

    def has_access(self, doctor_id: str, patient_id: str) -> bool:
        """Действующая связь: принята этим врачом, не отозвана, срок не истёк."""
        row = self._one("SELECT 1 FROM shares WHERE doctor_id = ? AND patient_id = ? AND accepted IS NOT NULL "
                        "AND revoked IS NULL AND expires > ? LIMIT 1", (doctor_id, patient_id, self.now()))
        return row is not None

    def doctor_patients(self, doctor_id: str) -> list[sqlite3.Row]:
        return self._all("SELECT u.id AS id, u.login AS login, MAX(s.expires) AS expires, "
                         "(SELECT COUNT(*) FROM records r WHERE r.user_id = u.id) AS n_records "
                         "FROM shares s JOIN users u ON u.id = s.patient_id "
                         "WHERE s.doctor_id = ? AND s.accepted IS NOT NULL AND s.revoked IS NULL AND s.expires > ? "
                         "GROUP BY u.id ORDER BY MAX(s.accepted) DESC", (doctor_id, self.now()))

    def log_access(self, patient_id: str, doctor_id: str, what: str, record_id: Optional[str] = None) -> None:
        """Открытие истории или записи врачом. Повторы «врач — что» в течение часа после первой строки
        (ACCESS_LOG_MERGE) — та же строка: журнал не растёт от обновления страницы. Если за этот час открывались
        разные записи, record_id строки становится null («несколько записей»)."""
        now = self.now()
        with self.tx() as c:
            row = c.execute("SELECT id, record_id, enc_doctor FROM access_log WHERE patient_id = ? AND doctor_id = ? "
                            "AND what = ? AND at > ? ORDER BY at DESC, id DESC LIMIT 1",
                            (patient_id, doctor_id, what, now - ACCESS_LOG_MERGE)).fetchone()
            if row is None:
                lid = c.execute("INSERT INTO access_log (patient_id, doctor_id, what, record_id, at) "
                                "VALUES (?, ?, ?, ?, ?)", (patient_id, doctor_id, what, record_id, now)).lastrowid
            else:
                lid = row["id"]
                if row["record_id"] is not None and row["record_id"] != record_id:
                    c.execute("UPDATE access_log SET record_id = NULL WHERE id = ?", (lid,))
            if row is None or row["enc_doctor"] is None:
                # снимок логина и клиники врача: строка журнала переживёт удаление его аккаунта
                snap = self._doctor_snapshot(c, doctor_id, self._bind("access_log", str(lid), patient_id, ".doctor"))
                c.execute("UPDATE access_log SET enc_doctor = ? WHERE id = ?", (snap, lid))

    def access_log(self, patient_id: str) -> list[sqlite3.Row]:
        return self._all("SELECT a.*, u.login AS doctor_login, u.clinic AS doctor_clinic FROM access_log a "
                         "LEFT JOIN users u ON u.id = a.doctor_id WHERE a.patient_id = ? "
                         "ORDER BY a.at DESC, a.id DESC LIMIT ?", (patient_id, ACCESS_LOG_LIMIT))

    # ---- референсы лаборатории врача ----------------------------------------------------------
    def _lab_set(self, row: sqlite3.Row) -> dict:
        ranges = self.unseal(row["enc_ranges"], self._bind("lab_refs", row["id"], row["doctor_id"])) or {}
        return {"id": row["id"], "name": row["name"], "default": bool(row["is_default"]), "ranges": ranges}

    def list_lab_refs(self, doctor_id: str) -> list[dict]:
        rows = self._all("SELECT * FROM lab_refs WHERE doctor_id = ? ORDER BY created ASC, rowid ASC", (doctor_id,))
        return [self._lab_set(r) for r in rows]

    def add_lab_ref(self, doctor_id: str, name: str, ranges: dict, default: bool) -> str:
        """Новый набор; default=True снимает отметку «по умолчанию» с остальных (набор по умолчанию — один)."""
        lid = new_id()
        enc = self.seal(ranges, self._bind("lab_refs", lid, doctor_id))
        with self.tx() as c:
            n = c.execute("SELECT COUNT(*) FROM lab_refs WHERE doctor_id = ?", (doctor_id,)).fetchone()[0]
            if n >= MAX_LAB_SETS:
                raise StoreError("too_many_sets", f"Наборов референсов не больше {MAX_LAB_SETS}: удалите ненужные.")
            self._check_quota(c, doctor_id, len(enc))
            if default:
                c.execute("UPDATE lab_refs SET is_default = 0 WHERE doctor_id = ?", (doctor_id,))
            c.execute("INSERT INTO lab_refs (id, doctor_id, name, enc_ranges, is_default, created) "
                      "VALUES (?, ?, ?, ?, ?, ?)", (lid, doctor_id, name, enc, 1 if default else 0, self.now()))
        return lid

    def update_lab_ref(self, doctor_id: str, lid: str, *, name: Optional[str] = None, ranges: Optional[dict] = None,
                       default: Optional[bool] = None) -> bool:
        """Правка набора без пересоздания: имя, границы (шифруются заново, объём — в предел пользователя),
        отметка «по умолчанию» (True снимает её с остальных наборов врача — по умолчанию один). Предел числа
        наборов не затрагивается. False — набора нет."""
        enc = self.seal(ranges, self._bind("lab_refs", lid, doctor_id)) if ranges is not None else None
        with self.tx() as c:
            row = c.execute("SELECT LENGTH(enc_ranges) FROM lab_refs WHERE id = ? AND doctor_id = ?",
                            (lid, doctor_id)).fetchone()
            if row is None:
                return False
            if enc is not None:
                self._check_quota(c, doctor_id, max(0, len(enc) - int(row[0])))
                c.execute("UPDATE lab_refs SET enc_ranges = ? WHERE id = ?", (enc, lid))
            if name is not None:
                c.execute("UPDATE lab_refs SET name = ? WHERE id = ?", (name, lid))
            if default is True:
                c.execute("UPDATE lab_refs SET is_default = (id = ?) WHERE doctor_id = ?", (lid, doctor_id))
            elif default is False:
                c.execute("UPDATE lab_refs SET is_default = 0 WHERE id = ?", (lid,))
        if enc is not None:
            self.checkpoint()                              # прежние границы не остаются в WAL
        return True

    def delete_lab_ref(self, doctor_id: str, lid: str) -> bool:
        with self.tx() as c:
            done = c.execute("DELETE FROM lab_refs WHERE id = ? AND doctor_id = ?", (lid, doctor_id)).rowcount > 0
        if done:
            self.checkpoint()
        return done

    def default_lab_ref(self, doctor_id: str) -> Optional[dict]:
        row = self._one("SELECT * FROM lab_refs WHERE doctor_id = ? AND is_default = 1 LIMIT 1", (doctor_id,))
        return None if row is None else self._lab_set(row)
