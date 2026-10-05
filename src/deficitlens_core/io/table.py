"""Пакетная расшифровка файлов в схеме кейса: CSV / XLSX -> таблица предсказаний и сводка.

Поддерживаются обе схемы, описанные в кейсе:
  * выданный файл: 840 строк, 12 классов, столбец deficiency_cause;
  * файл из текста кейса: 5 групп, столбец anemia_cause, служебные record_origin, birth_date, age_group,
    anchor_sample_date, нейтрофилы.
Правила импорта:
  * имена столбцов сопоставляются с кодами показателей без учёта регистра и пробелов; метки, идентификатор
    и служебные столбцы — по config/class_mapping.yaml; неизвестные столбцы игнорируются и перечисляются в сводке;
  * в модель попадает только белый список признаков constants.FEATS — столбцы-метки в признаки попасть не могут;
  * значения НЕ исправляются: строка с нераспознанным полом, возрастом < 18, без гемоглобина, с нечисловым
    значением или значением за жёсткой границей (hard в config/analytes.yaml) получает статус rejected и причину;
  * единицы — канонические (config/analytes.yaml): пересчёт единиц в пакетном режиме не делается;
  * вероятности считаются векторно, одним вызовом модели на все принятые строки; отчёты не строятся.
Сервис ничего не хранит: сводка содержит только счётчики, названия столбцов и метрики, без значений анализов.

Защита при чтении чужих файлов (ревизии волн 2–3, этап 4а):
  * XLSX — это zip: до разбора суммируются объявленные распакованные размеры частей (zipfile не распакует часть
    больше объявленного размера), больше предела — отказ (zip-бомба); число частей тоже ограничено;
  * столбцов не больше MAX_COLUMNS — по строке заголовка, до разбора всего файла;
  * строк читается не больше предела + 1: лишняя строка означает «файл больше предела» — отказ, а не обрезка;
  * отказ — TableLimitError (подкласс ValueError) с кодом и понятным текстом; сервис отвечает 422.
CSV на выходе: все ячейки в кавычках (QUOTE_ALL), текст, похожий на формулу, — с апострофом (escape_cell).
"""
from __future__ import annotations

import csv
import hashlib
import io
import itertools
import json
import re
import time
import zipfile
from functools import lru_cache
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import yaml
from sklearn.metrics import accuracy_score, confusion_matrix, f1_score

from .. import constants as C
from ..config import Config, load_config
from ..ml.case_model import CaseModel, decode_profiles, p_any_deficiency, sha256_file
from ..ml.features import LEVEL_NAMES, completeness_many, who_anemia

OUTPUT_COLUMNS = ["patient_id", "anemia", "case_group", "anemia_class", "deficiency_cause", "confidence",
                  "hidden_deficiency", "mode", "status", "reject_reason"]
MODES = ("auto", "clinical", "benchmark")
# Режим auto. Файл кейса «как выдан» узнаётся по доле сданных анализов на строку
# (из 26 вне ОАК): в файле кейса она 0,476, у случайных 30 строк из него — 0,41–0,54.
AUTO_MIN_ROWS = 30
AUTO_SHARE_RANGE = (0.38, 0.58)
MIN_AGE, MAX_AGE = 18, 120          # только взрослые: модель и пороги не рассчитаны на детей (как в AnalysisInput)
NA_STRINGS = {"", "na", "n/a", "nan", "null", "none", "#n/a", "<na>", "-nan", "#na"}
# Начало ячейки, с которого Excel / LibreOffice / Google Таблицы могут прочитать формулу (CSV injection, OWASP):
# = + - @, табуляция и возврат каретки; то же после пробелов и полноширинные ＝ ＋ － ＠ (Excel их нормализует).
FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")
FORMULA_CHARS = frozenset("=+-@\uff1d\uff0b\uff0d\uff20")
# Пределы чтения файла (по умолчанию — для пакетной расшифровки; сервис передаёт свои).
MAX_COLUMNS = 200                    # в схеме кейса 48 столбцов; шире — отказ до разбора
MAX_XLSX_PARTS = 10_000              # частей в zip-архиве XLSX
EXTRA_COLUMN_ALIASES = {"age": "age_years", "возраст": "age_years", "пол": "sex", "gender": "sex"}
_DECIMAL_COMMA = r"[+-]?\d+,\d+"
MAX_LISTED = 30                     # сколько нераспознанных значений меток перечислять в сводке


# --------------------------------------------------------------------------------------------
# Чтение и запись файлов
# --------------------------------------------------------------------------------------------
def _norm(s: Any) -> str:
    """Имя столбца или значение метки без учёта регистра и пробелов."""
    return re.sub(r"\s+", "", str(s)).lower()


class TableLimitError(ValueError):
    """Файл превышает предел (размер в распакованном виде, столбцы, строки) или это не тот формат.
    code — машинный код для ответа 422; текст — по-русски, без содержимого файла."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code, self.message = code, message


def _check_xlsx(data: bytes, max_unpacked: int | None) -> None:
    """XLSX — zip-архив. До разбора: архив читается, частей не больше MAX_XLSX_PARTS, сумма объявленных
    распакованных размеров не больше max_unpacked байт (защита от zip-бомбы: zipfile не выдаёт больше
    объявленного размера части, поэтому сумма объявленных размеров ограничивает и фактическую распаковку)."""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            infos = z.infolist()
    except (zipfile.BadZipFile, zipfile.LargeZipFile, OSError, ValueError) as e:
        raise TableLimitError("invalid_file", "файл XLSX повреждён или это не XLSX") from e
    if len(infos) > MAX_XLSX_PARTS:
        raise TableLimitError("file_too_large", f"в архиве XLSX больше {MAX_XLSX_PARTS} частей")
    if max_unpacked is not None:
        total = sum(i.file_size for i in infos)
        if total > max_unpacked:
            raise TableLimitError("file_too_large",
                                  f"XLSX в распакованном виде больше {max_unpacked // (1024 * 1024)} МБ: "
                                  "сохраните нужный лист как CSV или разбейте файл на части")


def _check_columns(n: int, max_columns: int | None) -> None:
    if max_columns is not None and n > max_columns:
        raise TableLimitError("too_many_columns",
                              f"в файле {n} столбцов — больше {max_columns}; нужна схема кейса (около 50 столбцов)")


def _check_rows(n: int, max_rows: int | None) -> None:
    if max_rows is not None and n > max_rows:
        raise TableLimitError("too_many_rows", f"в файле больше {max_rows} строк: разбейте файл на части")


def read_table_bytes(data: bytes, ext: str, *, max_rows: int | None = None, max_columns: int | None = MAX_COLUMNS,
                     max_unpacked: int | None = None) -> pd.DataFrame:
    """Содержимое файла (CSV или XLSX) из памяти -> DataFrame, с пределами: строк (max_rows), столбцов по заголовку
    (max_columns), распакованного объёма XLSX в байтах (max_unpacked). None — без предела. Превышение —
    TableLimitError. На диск ничего не пишется. ext — расширение с точкой (.csv, .txt, .xlsx, .xlsm)."""
    ext = ext.lower()
    nrows = None if max_rows is None else max_rows + 1          # лишняя строка = «больше предела», не обрезка
    if ext in (".xlsx", ".xlsm"):
        _check_xlsx(data, max_unpacked)
        header = pd.read_excel(io.BytesIO(data), dtype=object, engine="openpyxl", nrows=0)
        _check_columns(len(header.columns), max_columns)
        df = pd.read_excel(io.BytesIO(data), dtype=object, engine="openpyxl", nrows=nrows)
        source = {"format": "xlsx"}
    elif ext == ".xls":
        raise ValueError("формат .xls не поддерживается: сохраните файл как .xlsx или .csv")
    else:
        if max_unpacked is not None and len(data) > max_unpacked:
            raise TableLimitError("file_too_large", f"файл больше {max_unpacked // (1024 * 1024)} МБ")
        try:
            text, encoding = data.decode("utf-8-sig"), "utf-8"
        except UnicodeDecodeError:
            text, encoding = data.decode("cp1251"), "cp1251"
        header = next((ln for ln in text.splitlines() if ln.strip()), "")
        if not header:
            raise ValueError("файл пуст: нет строки заголовков")
        counts = {sep: header.count(sep) for sep in (";", ",", "\t")}
        sep = ";" if counts[";"] and counts[";"] >= counts[","] else ("\t" if counts["\t"] > counts[","] else ",")
        _check_columns(counts[sep] + 1, max_columns)            # до разбора: по строке заголовка
        if max_columns is not None:
            # ...и по каждой строке данных: строка шире заголовка превращается в pandas в MultiIndex,
            # который проверка df.columns не видит, а разбор такой строки — секунды и сотни МБ (ревизия волн 4–5)
            limit = None if nrows is None else nrows + 1
            widest = max((ln.count(sep) for ln in itertools.islice(text.splitlines(), limit)), default=0)
            _check_columns(widest + 1, max_columns)
        df = pd.read_csv(io.StringIO(text), sep=sep, dtype=str, keep_default_na=False, na_filter=False, nrows=nrows,
                         index_col=False)
        source = {"format": "csv", "encoding": encoding, "delimiter": {";": ";", ",": ",", "\t": "tab"}[sep]}
    _check_columns(len(df.columns), max_columns)                 # строки шире заголовка
    _check_rows(len(df), max_rows)
    df.columns = [str(c).strip() for c in df.columns]
    df.attrs["source"] = source
    return df


def read_table(path: str | Path, *, max_rows: int | None = None, max_columns: int | None = MAX_COLUMNS,
               max_unpacked: int | None = None) -> pd.DataFrame:
    """CSV (разделители «,» и «;», кодировки utf-8 и cp1251) или XLSX -> DataFrame.

    Все ячейки CSV читаются как текст: ничего не преобразуется молча (идентификаторы не теряют ведущие нули,
    «NA» не превращается в пропуск до явной проверки). Сведения о формате — в df.attrs["source"].
    Пределы — как в read_table_bytes.
    """
    p = Path(path)
    ext = p.suffix.lower()
    if ext == ".xls":
        raise ValueError("формат .xls не поддерживается: сохраните файл как .xlsx или .csv")
    return read_table_bytes(p.read_bytes(), ext, max_rows=max_rows, max_columns=max_columns,
                            max_unpacked=max_unpacked)


def escape_cell(v: Any) -> Any:
    """Защита от формул в таблицах: текстовая ячейка, которая начинается (в том числе после пробелов) с = + - @
    или полноширинных ＝ ＋ － ＠, а также с табуляции или возврата каретки, получает апостроф в начале.
    Числа не трогаются."""
    if not isinstance(v, str) or not v:
        return v
    if v.startswith(FORMULA_PREFIXES):
        return "'" + v
    stripped = v.lstrip()                       # все пробельные символы Юникода, в том числе \u3000 и \xa0
    if stripped and stripped[0] in FORMULA_CHARS:
        return "'" + v
    return v


def escape_formulas(df: pd.DataFrame) -> pd.DataFrame:
    """Копия таблицы, в которой экранированы все текстовые ячейки и заголовки, похожие на формулы."""
    out = df.copy()
    for col in out.columns:
        if not pd.api.types.is_numeric_dtype(out[col]):
            out[col] = out[col].map(escape_cell)
    out.columns = [escape_cell(str(c)) for c in out.columns]
    return out


def to_safe_csv(df: pd.DataFrame) -> str:
    """Таблица -> текст CSV для отдачи наружу: формулы экранированы, все ячейки в кавычках."""
    return escape_formulas(df).to_csv(index=False, lineterminator="\n", quoting=csv.QUOTE_ALL)


def write_table(df: pd.DataFrame, path: str | Path) -> None:
    """Запись результата: .xlsx — если так названо, иначе CSV (utf-8, разделитель «,», все ячейки в кавычках)."""
    p = Path(path)
    if p.parent and not p.parent.exists():
        p.parent.mkdir(parents=True, exist_ok=True)
    if p.suffix.lower() == ".xlsx":
        escape_formulas(df).to_excel(p, index=False, engine="openpyxl")
    else:
        p.write_text(to_safe_csv(df), encoding="utf-8", newline="")


# --------------------------------------------------------------------------------------------
# Сопоставление столбцов
# --------------------------------------------------------------------------------------------
def _mapping(cfg: Config, mapping: dict | None) -> dict:
    """class_mapping.yaml с поправками из пользовательского файла сопоставления (те же ключи + columns)."""
    cm = dict(cfg.class_mapping)
    for key, value in (mapping or {}).items():
        cm[key] = {**cm[key], **value} if isinstance(value, dict) and isinstance(cm.get(key), dict) else value
    return cm


def map_columns(columns, cfg: Config, cm: dict) -> dict:
    """Какой столбец файла что означает. Возвращает словарь:
    features {код признака: столбец}, labels {anemia / anemia_class / cause: столбец}, id, birth_date,
    anchor_sample_date, ignored_service [...], ignored_unknown [...], duplicates [...].
    """
    lookup: dict[str, tuple[str, str]] = {}

    def offer(name: Any, kind: str, target: str) -> None:
        lookup.setdefault(_norm(name), (kind, target))

    for file_col, code in (cm.get("columns") or {}).items():       # явное сопоставление пользователя
        if code in C.F_IDX:
            offer(file_col, "feature", code)
        elif code in ("anemia", "anemia_class", "cause"):
            offer(file_col, "label", code)
        elif code == "patient_id":
            offer(file_col, "id", "patient_id")
    for code in C.FEATS:                                            # канонические коды = имена столбцов кейса
        offer(code, "feature", code)
    for label, names in cm.get("label_columns", {}).items():
        for name in names:
            offer(name, "label", label)
    for name in cm.get("id_columns", []):
        offer(name, "id", "patient_id")
    offer("birth_date", "date", "birth_date")
    offer("anchor_sample_date", "date", "anchor_sample_date")
    for name in cm.get("ignored_columns", []):
        offer(name, "service", str(name))
    for alias, code in EXTRA_COLUMN_ALIASES.items():
        offer(alias, "feature", code)
    for code, info in cfg.analytes.items():                         # синонимы показателей из analytes.yaml
        if code in C.F_IDX:
            for alias in info.get("aliases", []):
                offer(alias, "feature", code)

    out: dict[str, Any] = {"features": {}, "labels": {}, "id": None, "birth_date": None, "anchor_sample_date": None,
                           "ignored_service": [], "ignored_unknown": [], "duplicates": []}
    for col in columns:
        kind, target = lookup.get(_norm(col), ("unknown", ""))
        if kind == "feature":
            if target in out["features"]:
                out["duplicates"].append(col)
            else:
                out["features"][target] = col
        elif kind == "label":
            if target in out["labels"]:
                out["duplicates"].append(col)
            else:
                out["labels"][target] = col
        elif kind == "id":
            if out["id"] is None:
                out["id"] = col
            else:
                out["duplicates"].append(col)
        elif kind == "date":
            if out[target] is None:
                out[target] = col
            else:
                out["duplicates"].append(col)
        elif kind == "service":
            out["ignored_service"].append(col)
        else:
            out["ignored_unknown"].append(col)
    return out


# --------------------------------------------------------------------------------------------
# Разбор ячеек
# --------------------------------------------------------------------------------------------
def _text(col: pd.Series) -> pd.Series:
    """Столбец как текст без пробелов по краям; пропуск -> пустая строка."""
    s = col.astype("string").str.strip()
    return s.fillna("").astype(object)


def _parse_numeric(col: pd.Series) -> tuple[np.ndarray, np.ndarray, int]:
    """Столбец -> (значения float, маска «не число», сколько ячеек с десятичной запятой).

    Пустая ячейка и обозначения пропуска (NA, null, …) = анализ не сдан (NaN). Десятичная запятая («12,5»)
    читается как число — это формат записи, а не поправка значения. Любой другой текст («<5», «н/д»)
    помечается как «не число», и строка будет отклонена.
    """
    if pd.api.types.is_bool_dtype(col):
        return np.full(len(col), np.nan), col.notna().to_numpy(), 0
    if pd.api.types.is_numeric_dtype(col):
        return col.to_numpy(dtype=float, na_value=np.nan), np.zeros(len(col), dtype=bool), 0
    s = _text(col)
    missing = s.str.lower().isin(NA_STRINGS).to_numpy()
    comma = s.str.fullmatch(_DECIMAL_COMMA).to_numpy(dtype=bool)
    if comma.any():
        s = s.where(~comma, s.str.replace(",", ".", regex=False))
    num = np.array(pd.to_numeric(s, errors="coerce"), dtype=float)   # копия: массив pandas только для чтения
    num[missing] = np.nan
    bad = ~missing & np.isnan(num)
    return num, bad, int(comma.sum())


def _parse_sex(col: pd.Series, sex_values: dict) -> np.ndarray:
    """Пол -> 0 (Ж) / 1 (М) по словарю sex_values из class_mapping.yaml; не распознан -> NaN."""
    table = {}
    for code, names in sex_values.items():
        for name in names:
            table[_norm(name)] = 0.0 if code == "F" else 1.0
    if pd.api.types.is_numeric_dtype(col) and not pd.api.types.is_bool_dtype(col):
        s = col.map(lambda v: "" if pd.isna(v) else (str(int(v)) if float(v).is_integer() else str(v)))
    else:
        s = _text(col).map(lambda v: v[:-2] if re.fullmatch(r"[01]\.0", v) else v)   # 0.0 / 1.0 из Excel
    return s.map(lambda v: table.get(_norm(v), np.nan)).to_numpy(dtype=float)


def _parse_dates(col: pd.Series) -> pd.Series:
    """Даты в формате ISO (2024-03-05) или дд.мм.гггг / дд/мм/гггг; не распознано -> NaT."""
    if pd.api.types.is_datetime64_any_dtype(col):
        return col
    s = _text(col)
    out = pd.to_datetime(s, format="ISO8601", errors="coerce")
    for fmt in ("%d.%m.%Y", "%d/%m/%Y"):
        rest = out.isna()
        if not rest.any():
            break
        out = out.where(~rest, pd.to_datetime(s, format=fmt, errors="coerce"))
    return out


def _age_from_dates(birth: pd.Series, anchor: pd.Series) -> np.ndarray:
    """Полных лет на дату взятия анализа (anchor_sample_date) по дате рождения."""
    b, a = _parse_dates(birth), _parse_dates(anchor)
    years = (a.dt.year - b.dt.year).to_numpy(dtype=float, na_value=np.nan)
    before_birthday = ((a.dt.month < b.dt.month) | ((a.dt.month == b.dt.month) & (a.dt.day < b.dt.day))).to_numpy()
    return np.where(np.isnan(years), np.nan, years - np.where(before_birthday, 1.0, 0.0))


# --------------------------------------------------------------------------------------------
# Метрики по меткам файла
# --------------------------------------------------------------------------------------------
# --------------------------------------------------------------------------------------------
# Отпечаток таблицы: «тот же набор данных», даже если файл пересохранён
# --------------------------------------------------------------------------------------------
def _label_key(value: Any) -> str:
    """Метка для отпечатка: число -> каноническая запись (1, 1.0 и «1» совпадают), текст -> без регистра и пробелов."""
    text = str(value).strip()
    if text == "" or text.lower() in NA_STRINGS:
        return ""
    try:
        return repr(float(text.replace(",", ".")))
    except ValueError:
        return _norm(text)


def table_fingerprint(df: pd.DataFrame, cfg: Config | None = None, mapping: dict | None = None) -> str | None:
    """sha256 нормализованной таблицы: patient_id, метки (anemia, anemia_class, cause) и значения показателей ПОСЛЕ
    разбора (числа — float с округлением до 6 знаков; пол — 0/1; пусто = не сдан). Порядок строк и столбцов, формат
    (CSV / XLSX), кодировка, BOM, перевод строки, разделитель и десятичная запятая на отпечаток не влияют.
    None — в таблице нет ни меток, ни показателей (сравнивать нечего).
    Ревизия волн 4–5, MEDIUM-3: плашка «файл обучения» ставится по отпечатку, а не по байтам файла."""
    cfg = cfg or load_config()
    cm = _mapping(cfg, mapping)
    cols = map_columns(list(df.columns), cfg, cm)
    if not cols["labels"] or not cols["features"]:
        return None
    parts: dict[str, list[str]] = {}
    if cols["id"] is not None:
        parts["patient_id"] = list(_text(df[cols["id"]]))
    for label, col in sorted(cols["labels"].items()):
        parts[f"label:{label}"] = [_label_key(v) for v in _text(df[col])]
    for code, col in sorted(cols["features"].items()):
        if code == "sex":
            values = _parse_sex(df[col], cm.get("sex_values", {}))
        else:
            values, _, _ = _parse_numeric(df[col])
        parts[code] = ["" if np.isnan(v) else repr(round(float(v), 6)) for v in values]
    keys = list(parts)
    rows = sorted(zip(*(parts[k] for k in keys))) if keys else []
    payload = json.dumps({"columns": keys, "rows": rows}, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def same_as_training(content_sha256: str | None, fingerprint: str | None, metadata: dict | None) -> bool:
    """Файл совпадает с набором обучения модели: по байтам (data.sha256) или по отпечатку таблицы (data.table_sha256)."""
    data = (metadata or {}).get("data", {}) or {}
    by_bytes = bool(content_sha256) and content_sha256 == data.get("sha256")
    by_table = bool(fingerprint) and fingerprint == data.get("table_sha256")
    return by_bytes or by_table


def _score(y_true, y_pred) -> dict:
    """Accuracy и macro-F1 (по значениям, встретившимся в истине или прогнозе)."""
    y_true, y_pred = list(y_true), list(y_pred)
    if not y_true:
        return {"n": 0, "accuracy": None, "macro_f1": None}
    return {"n": len(y_true), "accuracy": round(float(accuracy_score(y_true, y_pred)), 4),
            "macro_f1": round(float(f1_score(y_true, y_pred, average="macro", zero_division=0)), 4)}


def _group_if_anemia(cm: dict) -> dict[str, str]:
    """Класс из 12 -> группа кейса при анемии (class_mapping.yaml; для отсутствующих там — значение прототипа)."""
    to_group = cm.get("class_to_group5_if_anemia", {})
    return {c: to_group.get(c, C.GROUPS5[int(C.G5_IF_ANEMIA[i])]) for i, c in enumerate(C.CLASSES12)}


def _listed(values) -> list[str]:
    vals = sorted({str(v) for v in values})
    return vals[:MAX_LISTED] + ([f"… ещё {len(vals) - MAX_LISTED}"] if len(vals) > MAX_LISTED else [])


def _label_metrics(labels: dict[str, pd.Series], anemia_who: np.ndarray, decoded: dict[str, dict], chosen: str,
                   cm: dict, notes: list[str]) -> dict | None:
    """Метрики по меткам файла для принятых строк. decoded: {режим: {"class": [...], "cause": [...], "group": [...]}}."""
    if not labels:
        return None
    n = len(anemia_who)
    out: dict[str, Any] = {"selected_mode": chosen, "unrecognized_labels": {}}

    # Уровень 1: метка anemia против правила ВОЗ (от режима не зависит).
    a_true = np.full(n, np.nan)
    if "anemia" in labels:
        raw, bad, _ = _parse_numeric(labels["anemia"])
        ok = np.isin(raw, (0.0, 1.0))
        a_true[ok] = raw[ok]
        wrong = _text(labels["anemia"])[(~ok) & (bad | ~np.isnan(raw))]
        if len(wrong):
            out["unrecognized_labels"]["anemia"] = _listed(wrong)
        yt, yp = a_true[ok].astype(int), anemia_who[ok]
        out["level1"] = {**_score(yt, yp), "match": int((yt == yp).sum())}

    # Класс: 12 классов файла кейса или 5 групп (значения из class_aliases).
    to_group = _group_if_anemia(cm)
    names12 = {c.lower(): c for c in C.CLASSES12}
    alias5 = {_norm(a): g for g, names in cm.get("class_aliases", {}).items() for a in list(names) + [g]}
    scheme, g_true, c_true = None, np.full(n, None, dtype=object), np.full(n, None, dtype=object)
    if "anemia_class" in labels:
        v = _text(labels["anemia_class"]).map(_norm).to_numpy(dtype=object)
        present = {x for x in v if x}
        only12 = {x for x in present if x in names12 and x not in alias5}
        only5 = {x for x in present if x in alias5 and x not in names12}
        unknown = {x for x in present if x not in names12 and x not in alias5}
        if unknown:
            out["unrecognized_labels"]["anemia_class"] = _listed(unknown)
        if only5:
            scheme = "groups5" if not only12 else "mixed"
        elif present - unknown:
            scheme = "classes12"
        if scheme:
            # Истинная анемия для свёртки класса в группу: метка anemia; если её нет — по самому классу.
            # Дефицит B6 и меди бывает и с анемией, и без неё: без метки anemia группа таких строк определяется
            # правилом ВОЗ, а в метрики уровня 1 они не входят (истина неизвестна).
            a_class = np.full(n, np.nan)
            for i, x in enumerate(v):
                if not x or x in unknown:
                    continue
                if scheme != "classes12" and x in alias5:
                    g_true[i] = alias5[x]
                    a_class[i] = float(g_true[i] != "healthy")
                    continue
                cls = names12[x]
                if scheme == "classes12":
                    c_true[i] = cls
                k = C.P_IDX.get(cls)
                if k in C.NONANEMIC:
                    a_class[i] = 0.0
                elif k not in C.BOTH:
                    a_class[i] = 1.0
                a = a_true[i] if not np.isnan(a_true[i]) else (a_class[i] if not np.isnan(a_class[i]) else anemia_who[i])
                g_true[i] = to_group[cls] if a == 1 else "healthy"
            if scheme == "mixed":
                notes.append("В столбце класса смешаны названия 12 классов и 5 групп: метрики посчитаны по 5 группам.")
            if "anemia" not in labels:
                ok = ~np.isnan(a_class)
                yt, yp = a_class[ok].astype(int), anemia_who[ok]
                out["level1"] = {**_score(yt, yp), "match": int((yt == yp).sum()),
                                 "note": "метки anemia нет: анемия выведена из класса"}
    out["label_scheme"] = scheme

    # Причина (deficiency_cause / anemia_cause) через cause_aliases.
    cause_true = np.full(n, None, dtype=object)
    if "cause" in labels:
        alias_c = {_norm(a): code for code, names in cm.get("cause_aliases", {}).items() for a in list(names) + [code]}
        v = _text(labels["cause"]).map(_norm).to_numpy(dtype=object)
        unknown = {x for x in v if x not in alias_c}
        if unknown:
            out["unrecognized_labels"]["cause"] = _listed(unknown)
        for i, x in enumerate(v):
            if x in alias_c:
                cause_true[i] = alias_c[x]

    by_mode = {}
    for mode, dec in decoded.items():
        m: dict[str, Any] = {}
        ok = np.array([g is not None for g in g_true])
        if ok.any():
            yt, yp = g_true[ok], np.asarray(dec["group"], dtype=object)[ok]
            anemic = np.array([g != "healthy" for g in yt])
            m["groups5"] = {**_score(yt, yp),
                            "confusion": {"labels": C.GROUPS5, "rows_true_columns_predicted": True,
                                          "matrix": confusion_matrix(list(yt), list(yp), labels=C.GROUPS5).tolist()},
                            "anemic_rows": _score(yt[anemic], yp[anemic])}
        ok = np.array([c is not None for c in c_true])
        if ok.any():
            m["classes12"] = _score(c_true[ok], np.asarray(dec["class"], dtype=object)[ok])
        ok = np.array([c is not None for c in cause_true])
        if ok.any():
            m["cause"] = _score(cause_true[ok], np.asarray(dec["cause"], dtype=object)[ok])
        by_mode[mode] = m
    out["by_mode"] = by_mode
    if not out["unrecognized_labels"]:
        out.pop("unrecognized_labels")
    return out


# --------------------------------------------------------------------------------------------
# Предсказание
# --------------------------------------------------------------------------------------------
@lru_cache(maxsize=1)
def _default_model() -> CaseModel:
    return CaseModel.load()


def _decode(P14: np.ndarray, anemia: np.ndarray, to_group: dict[str, str]) -> dict:
    dec = decode_profiles(P14)
    cls = np.array(C.CLASSES12, dtype=object)[dec["class12"]]
    group = np.where(anemia == 1, np.array([to_group[c] for c in C.CLASSES12], dtype=object)[dec["class12"]],
                     "healthy").astype(object)
    return {"class": cls, "cause": np.array(C.CAUSES11, dtype=object)[dec["cause11"]], "group": group,
            "confidence": dec["confidence"]}


def predict_table(df: pd.DataFrame, mode: str = "auto", *, model: CaseModel | None = None,
                  cfg: Config | None = None, mapping: dict | None = None) -> tuple[pd.DataFrame, dict]:
    """Расшифровка таблицы в схеме кейса. Возвращает (таблица результата, сводка).

    Столбцы результата: patient_id, anemia (0/1 по ВОЗ), case_group (5 групп), anemia_class (12 классов),
    deficiency_cause (11 причин), confidence (вероятность лучшего класса), hidden_deficiency (0/1: анемии нет и
    P(любой дефицит) ≥ порога для уровня полноты строки), mode, status (done / rejected), reject_reason.
    mode="auto": меньше 30 принятых строк — клинический режим; от 30 строк — режим бенчмарка, если средняя доля
    сданных анализов на строку лежит в пределах 0,38–0,58 (так выглядит файл кейса «как выдан»), иначе клинический.
    """
    if mode not in MODES:
        raise ValueError(f"mode должен быть одним из {MODES}, получено {mode!r}")
    t0 = time.perf_counter()
    cfg = cfg or load_config()
    cm = _mapping(cfg, mapping)
    n = len(df)
    notes: list[str] = []
    cols = map_columns(list(df.columns), cfg, cm)

    # ---- разбор и проверка строк --------------------------------------------------------------
    reasons: list[list[str]] = [[] for _ in range(n)]
    reason_counts: dict[str, int] = {}

    def reject(mask: np.ndarray, code: str, message) -> None:
        idx = np.flatnonzero(mask)
        if len(idx):
            reason_counts[code] = reason_counts.get(code, 0) + int(len(idx))
        for i in idx:
            reasons[i].append(message(i) if callable(message) else message)

    X = np.full((n, len(C.FEATS)), np.nan)
    decimal_commas = 0
    not_number = {"age_years": np.zeros(n, dtype=bool), "hemoglobin": np.zeros(n, dtype=bool)}
    for code, col in cols["features"].items():
        j = C.F_IDX[code]
        if code == "sex":
            X[:, j] = _parse_sex(df[col], cm.get("sex_values", {}))
            continue
        values, bad, commas = _parse_numeric(df[col])
        decimal_commas += commas
        X[:, j] = values
        if code in not_number:
            not_number[code] = bad
        reject(bad, "not_a_number", f"{code}: значение не является числом")
        if code in cfg.analytes:
            lo, hi = cfg.analytes[code]["hard"]
            unit = cfg.unit_ru(code)
            with np.errstate(invalid="ignore"):
                out_of_range = (values < lo) | (values > hi)
            reject(out_of_range, "out_of_hard_range",
                   lambda i, c=code, v=values, lo=lo, hi=hi, u=unit:
                   f"{c}: значение {v[i]:g} вне допустимого диапазона {lo:g}–{hi:g} {u}".rstrip())

    # Возраст: столбец age_years; если его нет (или ячейка пуста), а есть даты рождения и взятия анализа — считаем.
    j_age = C.F_IDX["age_years"]
    if cols["birth_date"] and cols["anchor_sample_date"]:
        computed = _age_from_dates(df[cols["birth_date"]], df[cols["anchor_sample_date"]])
        fill = np.isnan(X[:, j_age]) & ~np.isnan(computed)
        if fill.any():
            X[fill, j_age] = computed[fill]
            notes.append(f"Возраст вычислен из birth_date и anchor_sample_date для {int(fill.sum())} строк.")
    age = X[:, j_age]
    with np.errstate(invalid="ignore"):
        reject(np.isnan(age) & ~not_number["age_years"], "age_missing", "возраст не указан")
        reject(age < MIN_AGE, "age_below_18", "возраст меньше 18 лет: модель и пороги рассчитаны на взрослых")
        reject(age > MAX_AGE, "age_above_120", "возраст больше 120 лет: проверьте значение")
    reject(np.isnan(X[:, C.F_IDX["sex"]]), "sex_unrecognized",
           "пол не распознан" if "sex" in cols["features"] else "пол не указан: в файле нет столбца sex")
    reject(np.isnan(X[:, C.F_IDX["hemoglobin"]]) & ~not_number["hemoglobin"], "hemoglobin_missing",
           "гемоглобин не указан" if "hemoglobin" in cols["features"] else "гемоглобин не указан: нет столбца hemoglobin")
    for required in ("sex", "age_years", "hemoglobin"):
        if required not in cols["features"] and not (required == "age_years" and cols["birth_date"]
                                                     and cols["anchor_sample_date"]):
            notes.append(f"В файле нет обязательного столбца {required}: все строки отклонены.")
    if decimal_commas:
        m10, m100 = decimal_commas % 10, decimal_commas % 100
        word = "ячейке" if m10 == 1 and m100 != 11 else "ячейках"
        notes.append(f"Десятичная запятая распознана как разделитель дробной части в {decimal_commas} {word}.")
    if cols["duplicates"]:
        notes.append("Столбцы-дубликаты не использованы: " + ", ".join(map(str, cols["duplicates"])) + ".")

    ok = np.array([not r for r in reasons], dtype=bool)
    ok_idx = np.flatnonzero(ok)
    Xok = X[ok_idx]
    n_ok = len(ok_idx)

    # ---- режим ------------------------------------------------------------------------------
    share = float((~np.isnan(Xok[:, C.LAB_IDX])).mean()) if n_ok else None
    auto_cfg = cm.get("auto_mode", {})           # config/class_mapping.yaml; запасные значения — константы модуля
    min_rows = int(auto_cfg.get("min_rows", AUTO_MIN_ROWS))
    lo, hi = (float(v) for v in auto_cfg.get("measured_share", AUTO_SHARE_RANGE))
    if mode != "auto":
        chosen, why = mode, "режим задан явно"
    elif n_ok < min_rows:
        chosen, why = "clinical", f"принятых строк меньше {min_rows}"
    elif lo <= share <= hi:
        chosen, why = "benchmark", (f"средняя доля сданных анализов {share:.3f} в пределах "
                                    f"{lo}–{hi}: файл похож на файл кейса")
    else:
        chosen, why = "clinical", (f"средняя доля сданных анализов {share:.3f} вне пределов {lo}–{hi}")

    # ---- предсказание: один векторный вызов модели на все принятые строки ------------------------
    labels = {name: df[col].iloc[ok_idx] for name, col in cols["labels"].items()}
    to_group = _group_if_anemia(cm)
    res: dict[str, np.ndarray] = {
        "patient_id": (np.array(_text(df[cols["id"]]), dtype=object) if cols["id"] is not None
                       else np.array([""] * n, dtype=object)),
        "case_group": np.full(n, "", dtype=object), "anemia_class": np.full(n, "", dtype=object),
        "deficiency_cause": np.full(n, "", dtype=object), "confidence": np.full(n, np.nan),
        "mode": np.full(n, "", dtype=object),
    }
    empty_id = np.array([not v for v in res["patient_id"]], dtype=bool)
    if empty_id.any():
        res["patient_id"][empty_id] = [f"row_{i + 1}" for i in np.flatnonzero(empty_id)]
        notes.append("Идентификатора нет" + (" у части строк" if cols["id"] is not None else "")
                     + ": использован номер строки (row_N).")
    anemia_col = pd.array([pd.NA] * n, dtype="Int64")
    hidden_col = pd.array([pd.NA] * n, dtype="Int64")
    metrics, levels_count, model_info = None, {}, None
    if n_ok:
        model = model or _default_model()
        model_info = {"version": model.version, "trained_at": model.metadata.get("trained_at")}
        anemia = who_anemia(Xok)
        if labels:
            P = model.predict_both(Xok)                    # при наличии меток — метрики обоих режимов
        else:
            P = {chosen: model.predict_profiles(Xok, mode=chosen)}
        decoded = {m: _decode(p, anemia, to_group) for m, p in P.items()}
        dec = decoded[chosen]
        # Скрытый дефицит: анемии нет и P(любой дефицит) не ниже порога для уровня полноты этой строки.
        levels = completeness_many(Xok)
        tau = np.array([model.hidden_threshold(lv, mode=chosen) for lv in levels])
        hidden = ((anemia == 0) & (p_any_deficiency(P[chosen]) >= tau)).astype(int)
        res["case_group"][ok_idx] = dec["group"]
        res["anemia_class"][ok_idx] = dec["class"]
        res["deficiency_cause"][ok_idx] = dec["cause"]
        res["confidence"][ok_idx] = np.round(dec["confidence"], 3)
        res["mode"][ok_idx] = chosen
        anemia_col[ok_idx] = anemia
        hidden_col[ok_idx] = hidden
        levels_count = {lv: int((levels == lv).sum()) for lv in LEVEL_NAMES}
        metrics = _label_metrics(labels, anemia, decoded, chosen, cm, notes)

    result = pd.DataFrame({
        "patient_id": res["patient_id"], "anemia": anemia_col, "case_group": res["case_group"],
        "anemia_class": res["anemia_class"], "deficiency_cause": res["deficiency_cause"],
        "confidence": res["confidence"], "hidden_deficiency": hidden_col, "mode": res["mode"],
        "status": np.where(ok, "done", "rejected"),
        "reject_reason": np.array(["; ".join(r) for r in reasons], dtype=object),
    }, columns=OUTPUT_COLUMNS)

    summary = {
        "rows_total": int(n), "rows_done": int(n_ok), "rows_rejected": int(n - n_ok),
        "reject_reasons": reason_counts,
        "columns": {
            "recognized": {str(col).strip(): code for code, col in cols["features"].items()},
            "labels": {k: str(v).strip() for k, v in cols["labels"].items()},
            "id": None if cols["id"] is None else str(cols["id"]).strip(),
            "dates": [str(c).strip() for c in (cols["birth_date"], cols["anchor_sample_date"]) if c],
            "ignored_service": [str(c).strip() for c in cols["ignored_service"]],
            "ignored_unknown": [str(c).strip() for c in cols["ignored_unknown"]],
            "analytes_absent": [a for a in C.ALL_ANALYTES if a not in cols["features"]],
        },
        "mode": chosen, "mode_requested": mode,
        "auto": {"reason": why, "accepted_rows": int(n_ok), "min_rows_for_benchmark": AUTO_MIN_ROWS,
                 "measured_share": None if share is None else round(share, 4),
                 "share_range_for_benchmark": list(AUTO_SHARE_RANGE)},
        "completeness": levels_count,
        "predicted": ({"anemia": int((anemia_col == 1).sum()), "hidden_deficiency": int((hidden_col == 1).sum()),
                       "case_group": {g: int((res["case_group"] == g).sum()) for g in C.GROUPS5}} if n_ok else {}),
        "metrics": metrics,
        "model": model_info,
        "notes": notes,
        "processing_ms": round(1000 * (time.perf_counter() - t0), 1),
    }
    return result, summary


def predict_file(in_path: str, out_path: str, mode: str = "auto", summary_path: str | None = None,
                 mapping_path: str | None = None) -> dict:
    """Файл (CSV / XLSX в схеме кейса) -> файл с предсказаниями. Возвращает сводку: время обработки, число строк,
    принято, отклонено, распознанные и проигнорированные столбцы, режим, метрики (если в файле есть метки).
    mapping_path — YAML с поправками к config/class_mapping.yaml (те же ключи) и необязательным словарём
    columns {столбец файла: код показателя}. Если задан summary_path — сводка записывается туда в JSON."""
    t0 = time.perf_counter()
    df = read_table(in_path)
    mapping = None
    if mapping_path:
        with open(mapping_path, encoding="utf-8") as f:
            mapping = yaml.safe_load(f) or {}
        if not isinstance(mapping, dict):
            raise ValueError("файл сопоставления должен быть словарём YAML (как config/class_mapping.yaml)")
    result, summary = predict_table(df, mode=mode, mapping=mapping)
    write_table(result, out_path)
    summary["input"] = {"file": Path(in_path).name, **df.attrs.get("source", {})}
    summary["output"] = {"file": str(out_path), "columns": OUTPUT_COLUMNS}
    # Честность метрик: если файл — тот самый набор, на котором обучена модель, метрики оптимистичны.
    if summary.get("metrics") and summary.get("rows_done"):
        try:
            meta = _default_model().metadata
            if same_as_training(sha256_file(in_path), table_fingerprint(df, mapping=mapping), meta):
                summary["notes"].append(
                    "Файл совпадает с набором, на котором обучена модель: метрики оптимистичны (обучение и проверка "
                    "на одних строках). Честная оценка — кросс-валидация, см. docs/metrics/case_metrics.json.")
                summary["metrics"]["same_as_training_data"] = True
        except Exception:  # noqa: BLE001 — пометка необязательна и не должна ломать расшифровку
            pass
    summary["seconds"] = round(time.perf_counter() - t0, 3)
    if summary_path:
        p = Path(summary_path)
        if p.parent and not p.parent.exists():
            p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
    return summary
