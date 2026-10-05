"""Слой сервиса DeficitLens: чистые функции «dict -> dict» без веб-фреймворка.

Веб-слой (app.py на Starlette) только разбирает запрос, проверяет ключ и лимиты и вызывает эти функции.
На площадке тот же модуль можно подключить к FastAPI без переписывания.

Ошибки — исключение ServiceError(status, code, message, field): веб-слой превращает его в ответ
{"error": {"code", "message", "field"}} с нужным кодом. Слой расчёта ничего не хранит (хранит только кабинет —
store.py, по согласию) и никуда наружу не ходит;
значения анализов в журнал не пишутся.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from functools import lru_cache
from pathlib import Path
from typing import Any, Optional

from pydantic import ValidationError

from deficitlens_core import constants as C
from deficitlens_core.config import REPO_ROOT, Config, demo_dir, load_config
from deficitlens_core.schemas import AnalysisInput, InputError

PREVIEW_ROWS = 20
ALLOWED_UPLOAD_EXT = (".csv", ".txt", ".xlsx", ".xlsm")
BENCHMARK_MODES = ("auto", "clinical", "benchmark")
MAX_TEXT_BYTES = 64 * 1024
MAX_BENCHMARK_ROWS = 10_000                       # строк в файле для расшифровки (как DL_MAX_BATCH по умолчанию)
MAX_XLSX_UNPACKED_BENCHMARK = 50 * 1024 * 1024    # XLSX в распакованном виде: файл для расшифровки
MAX_XLSX_UNPACKED_PARSE = 5 * 1024 * 1024         # XLSX в распакованном виде: файл одного анализа (сверка)

# Группы полей формы (порядок и названия — часть интерфейса, сами показатели берутся из config/analytes.yaml).
FORM_GROUPS: list[tuple[str, str, tuple[str, ...]]] = [
    ("cbc", "Общий анализ крови", ("cbc",)),
    ("iron", "Обмен железа", ("iron",)),
    ("b12_folate", "B12 и фолаты", ("b12", "folate")),
    ("b6_copper", "B6 и медь", ("b6", "copper")),
    ("inflammation", "Воспаление", ("inflammation",)),
    ("kidney_thyroid", "Почки и щитовидная железа", ("kidney", "tsh")),
    ("other", "Гемолиз и прочее", ("hemolysis", "albumin")),
]
STATUS_RU = {
    "verified": "проверено по первоисточнику",
    "secondary": "по тексту клинических рекомендаций (вторичный источник)",
    "needs_medical_expert": "рабочее значение, окончательно решает врач",
}
THRESHOLD_TITLES = {"urgent": "Когда срочно", "indices": "Индексы общего анализа крови"}


class ServiceError(Exception):
    """Ошибка, которую нужно вернуть клиенту: код HTTP, код ошибки, сообщение по-русски, поле ввода (если есть)."""

    def __init__(self, status: int, code: str, message: str, field: Optional[str] = None):
        super().__init__(message)
        self.status, self.code, self.message, self.field = status, code, message, field

    def body(self) -> dict:
        return {"error": {"code": self.code, "message": self.message, "field": self.field}}


# --------------------------------------------------------------------------------------------
# Модели и конфигурация: загружаются один раз на процесс
# --------------------------------------------------------------------------------------------
def _cfg() -> Config:
    return load_config()


def _models():
    from deficitlens_core.engine import load_models
    return load_models()


def warmup() -> dict:
    """Загружает модели и конфигурацию и делает один пробный расчёт на придуманных значениях (прогрев кэшей)."""
    cfg, models = _cfg(), _models()
    try:
        from deficitlens_core.engine import analyze
        analyze(AnalysisInput(sex="F", age_years=40, values={"hemoglobin": 130.0}), models=models, cfg=cfg)
    except Exception:  # noqa: BLE001 — прогрев не должен мешать запуску
        pass
    return health()


def _versions() -> dict:
    cfg, models = _cfg(), _models()
    return {
        "engine": C.ENGINE_VERSION,
        "case_model": getattr(models.case, "version", "нет") if models.case else "нет",
        "screen_model": getattr(models.screen, "version", "нет") if models.screen else "нет",
        "norms": str(cfg.norms.get("version", "")),
    }


def health() -> dict:
    """Состояние сервиса и версии: движок, модели, набор порогов."""
    return {"status": "ok", **_versions()}


# --------------------------------------------------------------------------------------------
# Один анализ и пакет
# --------------------------------------------------------------------------------------------
_FIELD_MESSAGES = {
    "sex": "Укажите пол: F (женский) или M (мужской).",
    "age_years": "Возраст — целое число от 18 до 120 лет: сервис рассчитан на взрослых.",
    "pregnancy": "Беременность: status — no, yes или unknown; trimester — 1, 2 или 3.",
    "norms": "Набор порогов: who или ru.",
    "mode": "Режим: clinical, benchmark или auto.",
    "refer_share": "Точка направления на ферритин (refer_share): 0.2, 0.3 или 0.4.",
    "reference_ranges": "Референсы (reference_ranges): словарь «код показателя -> {\"low\": число, \"high\": число, \"unit\": \"единица\"}».",
    "values": "Поле values — словарь «код показателя -> число» (или {\"value\": число, \"unit\": \"единица\"}).",
}


def _validation_error(e: ValidationError, cfg: Config) -> ServiceError:
    """Первая ошибка проверки схемы -> сообщение по-русски и имя поля. Значения в сообщение не попадают."""
    err = e.errors(include_input=False, include_url=False, include_context=False)[0]
    loc = [str(x) for x in err.get("loc", ())]
    top = loc[0] if loc else ""
    if err.get("type") == "extra_forbidden":
        return ServiceError(422, "unknown_field", f"Неизвестное поле «{'.'.join(loc)}».", ".".join(loc) or None)
    if err.get("type") == "missing":
        names = {"sex": "пол (sex)", "age_years": "возраст (age_years)", "values": "значения анализов (values)"}
        return ServiceError(422, "missing_field", f"Не указано обязательное поле: {names.get(top, top)}.", top or None)
    if top == "values" and len(loc) >= 2:
        code = loc[1]
        return ServiceError(422, "invalid_value", f"{cfg.name_ru(code)}: значение должно быть числом.", code)
    if top == "pregnancy":
        return ServiceError(422, "invalid_value", _FIELD_MESSAGES["pregnancy"], "pregnancy")
    return ServiceError(422, "invalid_value", _FIELD_MESSAGES.get(top, f"Некорректное значение поля «{top}»."),
                        top or None)


def _analyze(payload: Any):
    cfg = _cfg()
    if not isinstance(payload, dict):
        raise ServiceError(422, "invalid_body", "Тело запроса должно быть объектом JSON с полями sex, age_years, values.")
    try:
        inp = AnalysisInput.model_validate(payload)
    except ValidationError as e:
        raise _validation_error(e, cfg) from None
    from deficitlens_core.engine import analyze
    try:
        return analyze(inp, models=_models(), cfg=cfg)
    except InputError as e:
        raise ServiceError(422, e.code, e.message, e.field) from None


def analyze_one(payload: Any) -> dict:
    """Один анализ: проверка схемы AnalysisInput -> движок -> AnalysisResult как dict."""
    return _analyze(payload).model_dump(mode="json")


def analyze_many(records: Any, with_reports: bool = False, max_records: Optional[int] = None) -> dict:
    """Пакет записей: каждая получает статус done или rejected (тихих поправок нет) и общую сводку.

    with_reports=False — отчёты врачу и пациенту в ответ не включаются (ответ короче в несколько раз).
    """
    if not isinstance(records, list):
        raise ServiceError(422, "invalid_body", "Поле records должно быть списком записей.", "records")
    if max_records is not None and len(records) > max_records:
        raise ServiceError(413, "batch_too_large",
                           f"В пакете {len(records)} записей; допустимо не больше {max_records}.", "records")
    t0 = time.perf_counter()
    items: list[dict] = []
    done = 0
    for i, rec in enumerate(records):
        ref = rec.get("patient_ref") if isinstance(rec, dict) else None
        ref = ref if isinstance(ref, str) else None
        try:
            result = _analyze(rec).model_dump(mode="json")
        except ServiceError as e:
            items.append({"index": i, "patient_ref": ref, "status": "rejected",
                          "errors": [{"code": e.code, "message": e.message, "field": e.field}], "result": None})
            continue
        if not with_reports:
            result.pop("reports", None)
        items.append({"index": i, "patient_ref": ref, "status": "done", "errors": [], "result": result})
        done += 1
    summary = {"total": len(items), "done": done, "rejected": len(items) - done,
               "processing_ms": round(1000 * (time.perf_counter() - t0), 2)}
    return {"summary": summary, "items": items}


# --------------------------------------------------------------------------------------------
# Файл в схеме кейса -> предсказания и метрики
# --------------------------------------------------------------------------------------------
def metrics_dir() -> Path:
    return Path(os.environ.get("DL_METRICS_DIR", REPO_ROOT / "docs" / "metrics"))


@lru_cache(maxsize=1)
def _cv_metrics_cached(path: str, mtime: float) -> Optional[dict]:
    """Числа кросс-валидации из docs/metrics/case_metrics.json (в коде они не хранятся)."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        level = "extended"                       # все столбцы файла кейса — как в файле «как выдан»
        modes = {}
        for mode in ("benchmark", "clinical"):
            m = data["by_mode_level"][mode][level]

            def pair(acc: str, f1: str) -> dict:
                return {"accuracy": round(m[acc]["mean"], 4), "accuracy_sd": round(m[acc]["sd"], 4),
                        "macro_f1": round(m[f1]["mean"], 4), "macro_f1_sd": round(m[f1]["sd"], 4)}

            modes[mode] = {"classes12": pair("acc12", "f1_12"), "groups5": pair("acc5", "f1_5"),
                           "cause": pair("acc_cause", "f1_cause")}
        summary = data.get("summary", {})
        return {"source": "docs/metrics/case_metrics.json", "scheme": summary.get("cv"),
                "n_rows": summary.get("n_rows"), "level": level, "modes": modes}
    except Exception:  # noqa: BLE001 — файла метрик может не быть: тогда блок не показывается
        return None


def cv_metrics() -> Optional[dict]:
    p = metrics_dir() / "case_metrics.json"
    if not p.exists():
        return None
    return _cv_metrics_cached(str(p), p.stat().st_mtime)


def _read_upload_table(content: bytes, ext: str, *, max_rows: int, max_unpacked: int):
    """Файл из памяти -> DataFrame с пределами (io/table.py). Превышение предела и нечитаемый файл — 422
    с понятным текстом; содержимое файла в сообщение не попадает."""
    from deficitlens_core.io.table import MAX_COLUMNS, TableLimitError, read_table_bytes
    try:
        return read_table_bytes(content, ext if ext != ".txt" else ".csv", max_rows=max_rows,
                                max_columns=MAX_COLUMNS, max_unpacked=max_unpacked)
    except TableLimitError as e:
        raise ServiceError(422, e.code, f"Не удалось прочитать файл: {e.message}.", "file") from None
    except ValueError as e:
        raise ServiceError(422, "invalid_file", f"Не удалось прочитать файл: {e}.", "file") from None
    except Exception:  # noqa: BLE001 — повреждённый XLSX, неизвестная кодировка и т. п.
        raise ServiceError(422, "invalid_file",
                           "Не удалось прочитать файл: проверьте, что это CSV или XLSX в схеме кейса.",
                           "file") from None


# --------------------------------------------------------------------------------------------
# Своя структура файла: сопоставление столбцов и проверка, хватает ли данных для анализа
# --------------------------------------------------------------------------------------------
LABEL_TARGETS = {"anemia": "метка: анемия (0/1)", "anemia_class": "метка: класс анемии", "cause": "метка: причина",
                 "patient_id": "идентификатор строки"}
REQUIRED = (("sex", "пол"), ("age_years", "возраст"), ("hemoglobin", "гемоглобин"))
CBC_CORE = ("MCV", "MCH", "RDW", "RBC")         # без них скрининг скрытого дефицита и класс анемии слабее
MAX_MAPPING = 200


def _targets(cfg: Config) -> list[dict]:
    """Что можно выбрать для столбца: показатели модели (с единицами), пол, возраст, метки, «не использовать»."""
    out = [{"code": "sex", "name_ru": "Пол", "group": "person", "units": []},
           {"code": "age_years", "name_ru": "Возраст, лет", "group": "person", "units": []}]
    for code in C.FEATS:
        if code in ("sex", "age_years") or code not in cfg.analytes:
            continue
        info = cfg.analytes[code]
        units, seen = [], set()
        for name, factor in (info.get("units") or {}).items():          # по одному названию на множитель
            if float(factor) not in seen:
                seen.add(float(factor))
                units.append({"unit": name, "factor": float(factor)})
        out.append({"code": code, "name_ru": cfg.name_ru(code), "short_ru": cfg.short_ru(code),
                    "unit_ru": cfg.unit_ru(code), "group": "cbc" if code in C.CBC_LABS else "labs", "units": units})
    out += [{"code": k, "name_ru": v, "group": "label", "units": []} for k, v in LABEL_TARGETS.items()]
    return out


def requirements(cols: dict, cfg: Config) -> dict:
    """Хватает ли столбцов для анализа: errors — без этого строки будут отклонены; warnings — вывод слабее;
    info — что ещё стоит знать. Тексты — для страницы и сводки расчёта."""
    feats = set(cols.get("features") or {})
    errors = [f"Нет столбца «{name}» ({code}): без него каждая строка будет отклонена."
              for code, name in REQUIRED if code not in feats]
    warnings = []
    miss_cbc = [c for c in CBC_CORE if c not in feats]
    if miss_cbc:
        warnings.append("Нет показателей общего анализа крови: " + ", ".join(cfg.short_ru(c) for c in miss_cbc)
                        + ". Скрининг скрытого дефицита и класс анемии будут менее надёжны.")
    if not any(a in feats for a in C.LABS):
        warnings.append("Нет анализов вне общего анализа крови (ферритин, железо, B12, фолаты и др.): "
                        "причину анемии по одному ОАК надёжно не определить.")
    unknown = cols.get("ignored_unknown") or []
    if unknown:
        warnings.append(f"Не распознаны столбцы ({len(unknown)}): " + ", ".join(map(str, unknown[:12]))
                        + ("…" if len(unknown) > 12 else "") + " — сопоставьте их вручную, иначе они будут пропущены.")
    if cols.get("duplicates"):
        warnings.append("Несколько столбцов означают одно и то же — использован первый: "
                        + ", ".join(map(str, cols["duplicates"][:12])) + ".")
    info = []
    if not cols.get("labels"):
        info.append("Нет столбцов с метками (anemia, anemia_class, deficiency_cause): метрики не считаются, "
                    "будут только предсказания.")
    info.append("Единицы — как в сервисе (например, гемоглобин в г/л); если в файле другие — выберите единицу у столбца.")
    return {"ok": not errors, "errors": errors, "warnings": warnings, "info": info}


def _parse_mapping(raw: Any, columns: list[str], cfg: Config) -> tuple[dict, list[str], dict]:
    """Поле mapping: {"столбец файла": "код" | {"code": "код", "unit": "единица"} | "ignore"} ->
    (поправки для io.table: {"columns": {...}}, столбцы «не использовать», пересчёт единиц {столбец: множитель}).
    Неизвестный столбец, код или единица — 422: сопоставление пользователя нельзя молча исправлять."""
    if raw in (None, "", {}):
        return {}, [], {}
    if isinstance(raw, (str, bytes)):
        try:
            raw = json.loads(raw)
        except ValueError:
            raise ServiceError(422, "invalid_mapping", "Сопоставление столбцов: ожидается JSON-объект.", "mapping") from None
    if not isinstance(raw, dict) or len(raw) > MAX_MAPPING:
        raise ServiceError(422, "invalid_mapping", f"Сопоставление столбцов: объект не больше {MAX_MAPPING} записей.",
                           "mapping")
    allowed = {t["code"]: t for t in _targets(cfg)}
    known = {str(c) for c in columns}
    cols, ignore, factors = {}, [], {}
    for col, spec in raw.items():
        col = str(col)
        if col not in known:
            raise ServiceError(422, "invalid_mapping", f"Сопоставление: столбца «{col[:60]}» нет в файле.", "mapping")
        code, unit = (spec.get("code"), spec.get("unit")) if isinstance(spec, dict) else (spec, None)
        if code in (None, "", "auto"):
            continue
        if code == "ignore":
            ignore.append(col)
            continue
        if code not in allowed:
            raise ServiceError(422, "invalid_mapping", f"Сопоставление: «{str(code)[:40]}» — неизвестный показатель.",
                               "mapping")
        cols[col] = code
        if unit:
            from deficitlens_core.normalize import unit_key
            table = {unit_key(k): float(v) for k, v in (cfg.analytes.get(code, {}).get("units") or {}).items()}
            factor = table.get(unit_key(unit))
            if factor is None:
                raise ServiceError(422, "invalid_mapping",
                                   f"Сопоставление: единица «{str(unit)[:20]}» не подходит для «{allowed[code]['name_ru']}».",
                                   "mapping")
            if factor != 1.0:
                factors[col] = factor
    return ({"columns": cols} if cols else {}), ignore, factors


def _apply_units(df, factors: dict):
    """Пересчёт единиц столбцов в канонические (тем же множителем, что и в форме). Нечисловые ячейки не трогаем —
    их отклонит разбор строк с причиной «значение не является числом»."""
    import numpy as np
    from deficitlens_core.io.table import _parse_numeric
    for col, factor in factors.items():
        values, _bad, _ = _parse_numeric(df[col])
        new = df[col].astype(object).copy()
        ok = ~np.isnan(values)
        new[ok] = values[ok] * factor
        df[col] = new
    return df


ID_WORDS = ("пациент", "patient", "номер", "№", "id", "код")


def _guess_column(col: str, cfg: Config) -> tuple[str, str]:
    """Догадка для нераспознанного заголовка: имя до запятой или скобки — по кодам и синонимам analytes.yaml;
    единица — если в заголовке названа единица этого показателя (unit_key). ("", "") — догадки нет."""
    import re
    from deficitlens_core.io.table import EXTRA_COLUMN_ALIASES, _norm
    from deficitlens_core.normalize import unit_key
    base = re.split(r"[,(\[;]", col, maxsplit=1)[0]
    key = _norm(base)
    if not key:
        return "", ""
    if any(key.startswith(w) for w in ID_WORDS):
        return "patient_id", ""
    lookup = {_norm(c): c for c in C.FEATS}
    lookup.update({_norm(a): c for a, c in EXTRA_COLUMN_ALIASES.items()})
    for code, info in cfg.analytes.items():
        if code in C.F_IDX:
            for name in [*info.get("aliases", []), info.get("name_ru", ""), info.get("short_ru", "")]:
                if name:
                    lookup.setdefault(_norm(name), code)
    code = lookup.get(key, "")
    unit = ""
    if code in cfg.analytes:
        rest = unit_key(col[len(base):]).strip(",()[];")
        units = cfg.analytes[code].get("units") or {}
        factor = next((float(f) for name, f in units.items() if rest and unit_key(name) == rest), None)
        if factor is not None:                    # название единицы — как в списке выбора (_targets: первое на множитель)
            unit = next(name for name, f in units.items() if float(f) == factor)
    return code, unit


def columns_preview(content: bytes, filename: str, max_rows: int = MAX_BENCHMARK_ROWS) -> dict:
    """Шаг «Столбцы» страницы «Проверить на своём файле»: заголовок файла, что сервис понял в каждом столбце,
    пример значения (только для этого браузера; в журнал не пишется) и проверка, хватает ли данных."""
    from deficitlens_core.io.table import map_columns, _mapping
    if not isinstance(content, (bytes, bytearray)) or len(content) == 0:
        raise ServiceError(422, "empty_file", "Файл пуст или не передан.", "file")
    ext = Path(str(filename or "")).suffix.lower()
    if ext not in ALLOWED_UPLOAD_EXT:
        raise ServiceError(422, "unsupported_file", "Поддерживаются файлы CSV и XLSX.", "file")
    cfg = _cfg()
    df = _read_upload_table(bytes(content), ext, max_rows=max_rows, max_unpacked=MAX_XLSX_UNPACKED_BENCHMARK)
    cols = map_columns(list(df.columns), cfg, _mapping(cfg, None))
    found = {}
    for code, col in cols["features"].items():
        found[col] = ("feature", code)
    for code, col in cols["labels"].items():
        found[col] = ("label", code)
    if cols["id"]:
        found[cols["id"]] = ("label", "patient_id")
    for col in cols["ignored_service"]:
        found[col] = ("service", "")
    for col in cols["duplicates"]:
        found[col] = ("duplicate", "")
    rows = []
    taken = set(cols["features"]) | set(cols["labels"]) | ({"patient_id"} if cols["id"] else set())
    for col in df.columns:
        kind, target = found.get(col, ("unknown", ""))
        unit = ""
        if kind == "unknown":                     # «HGB, г/дл», «Ферритин (нг/мл)» — догадка, пользователь проверяет
            target, unit = _guess_column(str(col), cfg)
            kind = "guess" if target and target not in taken else "unknown"
            target = target if kind == "guess" else ""
            if kind == "guess":
                taken.add(target)
        non_empty = df[col].dropna()
        example = str(non_empty.iloc[0])[:24] if len(non_empty) else ""
        rows.append({"column": str(col)[:120], "kind": kind, "target": target, "unit": unit, "example": example})
    return {"rows": len(df), "columns": rows, "targets": _targets(cfg), "requirements": requirements(cols, cfg)}


def benchmark(content: bytes, filename: str, mode: str = "auto", max_rows: int = MAX_BENCHMARK_ROWS,
              mapping: Any = None) -> dict:
    """CSV / XLSX в схеме кейса -> {"summary", "preview" (первые 20 строк), "csv" (весь результат текстом)}.

    Если файл совпал с набором, на котором обучена модель, в сводке ставится признак same_as_training_data, а в ответ
    добавляется cv_metrics — честная оценка по кросс-валидации. Совпадение — по байтам (data.sha256 в metadata.json
    модели) или по отпечатку нормализованной таблицы (data.table_sha256: patient_id, метки и значения после разбора),
    так что пересохранённый CSV (CRLF, без BOM, «;») и XLSX тоже распознаются (ревизия волн 4–5, MEDIUM-3).
    Файл разбирается в памяти (на диск не пишется); пределы — строк max_rows, столбцов 200, XLSX в распакованном
    виде 50 МБ. CSV в ответе: все ячейки в кавычках, формулы экранированы.
    """
    from deficitlens_core.io.table import (escape_formulas, predict_table, same_as_training, table_fingerprint,
                                           to_safe_csv)

    if mode not in BENCHMARK_MODES:
        raise ServiceError(422, "invalid_mode", "Режим должен быть одним из: auto, clinical, benchmark.", "mode")
    if not isinstance(content, (bytes, bytearray)) or len(content) == 0:
        raise ServiceError(422, "empty_file", "Файл пуст или не передан.", "file")
    ext = Path(str(filename or "")).suffix.lower()
    if ext not in ALLOWED_UPLOAD_EXT:
        raise ServiceError(422, "unsupported_file", "Поддерживаются файлы CSV и XLSX.", "file")

    t0 = time.perf_counter()
    models = _models()
    df = _read_upload_table(bytes(content), ext, max_rows=max_rows, max_unpacked=MAX_XLSX_UNPACKED_BENCHMARK)
    # своя структура файла: сопоставление столбцов пользователя, «не использовать», пересчёт единиц
    user_map, ignore, factors = _parse_mapping(mapping, [str(c) for c in df.columns], _cfg())
    raw_for_fingerprint = df
    if ignore or factors:
        df = df.drop(columns=ignore).copy()
        df = _apply_units(df, factors)

    if models.case is None:
        raise ServiceError(503, "model_unavailable", "Модель уровня 2 не загружена: расчёт по файлу недоступен.")
    result, summary = predict_table(df, mode=mode, model=models.case, cfg=_cfg(), mapping=user_map or None)
    from deficitlens_core.io.table import map_columns, _mapping
    summary["requirements"] = requirements(map_columns(list(df.columns), _cfg(), _mapping(_cfg(), user_map or None)), _cfg())
    summary["input"] = {"file": Path(str(filename)).name[:120], **df.attrs.get("source", {})}

    out: dict[str, Any] = {}
    # Честность метрик: если это тот самый файл, на котором обучена модель, цифры на экране завышены.
    meta = getattr(models.case, "metadata", {}) or {}
    if summary.get("metrics") and same_as_training(hashlib.sha256(bytes(content)).hexdigest(),
                                                   table_fingerprint(raw_for_fingerprint, _cfg()), meta):
        summary["metrics"]["same_as_training_data"] = True
        summary["notes"].append(
            "Файл совпадает с набором, на котором обучена модель: метрики оптимистичны (обучение и проверка "
            "на одних строках). Честная оценка — кросс-валидация, см. docs/metrics/case_metrics.json.")
        cv = cv_metrics()
        if cv is not None:
            out["cv_metrics"] = cv

    safe = escape_formulas(result)
    preview = json.loads(safe.head(PREVIEW_ROWS).to_json(orient="records", force_ascii=False))
    summary["seconds"] = round(time.perf_counter() - t0, 3)
    cm = _cfg().class_mapping
    out = {"summary": json.loads(json.dumps(summary, ensure_ascii=False, default=str)),
           "preview": preview,
           "columns": list(result.columns),
           "csv": to_safe_csv(result),
           "labels_ru": {"groups5": cm.get("groups5", {}), "classes12": cm.get("classes12", {}),
                         "causes11": cm.get("causes11", {})},
           **out}
    return out


# --------------------------------------------------------------------------------------------
# Текст бланка, справочник, примеры
# --------------------------------------------------------------------------------------------
def parse_text(text: Any, max_bytes: Optional[int] = MAX_TEXT_BYTES) -> dict:
    """Вставленный текст бланка -> значения показателей для сверки. Расшифровку не возвращает.
    В ответе нет строк бланка как есть: нераспознанные строки — только номер строки и распознанные части.
    Кроме values / unrecognized / notes — reference_ranges (нормы с бланка в канонической единице), ignored
    (распознано, но сервисом не используется) и date (дата анализа, ISO) — docs/CONTRACTS.md, 04.10.2026, вечер."""
    from deficitlens_core.io.text_parser import parse_lab_text
    if not isinstance(text, str):
        raise ServiceError(422, "invalid_body", "Поле text должно быть строкой.", "text")
    if max_bytes is not None and len(text.encode("utf-8")) > max_bytes:
        raise ServiceError(413, "text_too_large", "Текст слишком длинный: вставьте только строки с результатами.", "text")
    try:
        return parse_lab_text(text, _cfg(), extended=True)
    except ValueError as e:
        raise ServiceError(422, "invalid_text", str(e)[:200], "text") from None


PARSE_FILE_EXT = (".pdf", ".txt", ".csv", ".xlsx", ".xlsm")
PHOTO_EXT = (".jpg", ".jpeg", ".jfif", ".png", ".webp", ".heic", ".heif")
PHOTO_NOTE = "Значения распознаны с фото: сверьте каждую строку с бланком, особенно запятые в числах и единицы."
SCAN_NOTE = ("Значения распознаны со скана PDF (в файле нет текстового слоя): сверьте каждую строку с бланком, "
             "особенно запятые в числах и единицы.")
SCAN_UNAVAILABLE = ("Это скан PDF без текстового слоя: его распознавание доступно в Docker-версии (там установлен "
                    "Tesseract). Здесь: загрузите PDF с текстом (из ЕМИАС, Госуслуг или личного кабинета "
                    "лаборатории), вставьте текст бланка или введите значения вручную.")
SCAN_TOTAL_SECONDS = 60.0     # все страницы скана вместе; одна страница — не дольше photo_ocr.MAX_SECONDS


def _parse_table(content: bytes, ext: str) -> dict:
    """Таблица одного анализа (CSV / XLSX) -> значения для сверки. Две раскладки:
      * «по строке»: заголовок — коды или синонимы показателей (как в схеме кейса), одна строка значений;
        единицы — канонические (как в пакетном режиме);
      * «по столбцам»: в каждой строке название показателя, число и единица (выгрузка бланка) — каждая строка
        таблицы разбирается как строка текста бланка.
    Пол, возраст и идентификаторы не извлекаются; текст ячеек в ответ не возвращается."""
    import numpy as np

    from deficitlens_core.io.table import _mapping, _parse_numeric, map_columns
    from deficitlens_core.io.text_parser import MAX_LINES, MAX_TEXT_CHARS

    cfg = _cfg()
    df = _read_upload_table(content, ext, max_rows=MAX_LINES, max_unpacked=MAX_XLSX_UNPACKED_PARSE)
    cols = map_columns(list(df.columns), cfg, _mapping(cfg, None))
    analyte_cols = {code: col for code, col in cols["features"].items() if code in cfg.analytes}
    if len(df) == 1 and len(analyte_cols) >= 2:
        values: dict[str, dict] = {}
        unrecognized: list[str] = []
        notes: list[str] = []
        for code, col in analyte_cols.items():
            num, bad, _ = _parse_numeric(df[col])
            name = cfg.analytes[code].get("name_ru", code)
            if bad[0]:
                unrecognized.append(f"{name}: значение не число")
                continue
            if np.isnan(num[0]):
                continue                                      # пустая ячейка = анализ не сдан
            lo, hi = cfg.analytes[code].get("hard", (-np.inf, np.inf))
            if not lo <= num[0] <= hi:
                unrecognized.append(f"{name}: значение вне диапазона")
                continue
            values[code] = {"value": float(num[0]), "unit": None}
        notes.append("Таблица «по строке»: значения приняты в канонических единицах показателей. Проверьте.")
        if not values:
            notes.append("Показатели не найдены: проверьте, что в строке есть числа.")
        return {"values": values, "unrecognized": unrecognized, "notes": notes, "reference_ranges": {},
                "ignored": [], "date": None, "layout": "row"}
    lines = [" ".join(str(c) for c in df.columns if not str(c).startswith("Unnamed"))]
    for row in df.itertuples(index=False):
        lines.append(" ".join(str(v).strip() for v in row if v is not None and str(v).strip() and str(v) != "nan"))
    result = parse_text("\n".join(lines)[:MAX_TEXT_CHARS], max_bytes=None)
    result["layout"] = "lines"
    return result


def parse_file(content: bytes, filename: str) -> dict:
    """Файл бланка -> значения показателей для сверки (как parse_text).

    .jpg / .jpeg / .png / .webp (или файл без расширения с сигнатурой изображения) — фото бланка: локальный
    Tesseract в дочернем процессе с жёстким таймаутом (io/photo_ocr.py), см. _parse_photo;
    .pdf — PDF любой лаборатории (pdfplumber в дочернем процессе с жёстким таймаутом, io/pdf_text.py): таблицы
    разбираются по столбцам (io/lab_tables.py), остальной текст — строками (io/text_parser.py); скан без
    текстового слоя — рендер страниц и Tesseract, как фото (_parse_pdf_scan);
    .txt — текст в utf-8 или cp1251; .csv / .xlsx — таблица одного анализа (пределы: XLSX в распакованном виде
    5 МБ, 200 столбцов, 400 строк). Файл разбирается в памяти и нигде не сохраняется. Ответ дополнительно
    содержит source: формат и число страниц (без содержимого файла). Вызывается под слотом тяжёлых расчётов
    (app.py, heavy_gate)."""
    from deficitlens_core.io.text_parser import MAX_TEXT_CHARS

    from deficitlens_core.io.photo_ocr import image_format

    if not isinstance(content, (bytes, bytearray)) or len(content) == 0:
        raise ServiceError(422, "empty_file", "Файл пуст или не передан.", "file")
    ext = Path(str(filename or "")).suffix.lower()
    # Фото: по расширению или (камера телефона иногда не даёт расширения) по сигнатуре файла.
    if ext in PHOTO_EXT or (ext not in PARSE_FILE_EXT and image_format(content) is not None):
        return _parse_photo(bytes(content))
    if ext not in PARSE_FILE_EXT:
        raise ServiceError(422, "unsupported_file", "Для разбора бланка поддерживаются фото (JPEG, PNG, WEBP), "
                           "PDF (с текстом или скан), TXT, CSV и XLSX.", "file")
    if ext in (".csv", ".xlsx", ".xlsm"):
        result = _parse_table(bytes(content), ext)
        result["source"] = {"format": "xlsx" if ext != ".csv" else "csv", "layout": result.pop("layout")}
        return result
    if ext == ".pdf":
        return _parse_pdf(bytes(content))
    if len(content) > MAX_TEXT_BYTES:
        raise ServiceError(413, "text_too_large",
                           "Текст слишком длинный: оставьте в файле только строки с результатами.", "file")
    try:
        text = bytes(content).decode("utf-8-sig")
    except UnicodeDecodeError:
        text = bytes(content).decode("cp1251", errors="replace")
    notes: list[str] = []
    if len(text) > MAX_TEXT_CHARS:
        text = text[:MAX_TEXT_CHARS]
        notes.append(f"Разобраны первые {MAX_TEXT_CHARS} символов текста.")
    result = parse_text(text, max_bytes=None)
    result["notes"] = notes + result["notes"]
    result["source"] = {"format": "txt"}
    return result


def _parse_pdf(content: bytes) -> dict:
    """PDF любой лаборатории -> значения для сверки. Таблицы — по столбцам из заголовка (в любом порядке),
    остальной текст (шапка, строки вне таблиц) — строками; дата анализа и нормы с бланка — в ответ.
    Скан без текстового слоя (или PDF, где в тексте не нашлось ни одного показателя, но страницы — картинки)
    распознаётся Tesseract: source.format = "pdf_scan"."""
    from deficitlens_core.io import photo_ocr
    from deficitlens_core.io.pdf_text import extract_text
    from deficitlens_core.io.text_parser import MAX_TEXT_CHARS, parse_lab_text

    try:
        pdf = extract_text(content)
    except InputError as e:
        # PDF больше pdf_text.MAX_PDF_BYTES (5 МБ) — 413, как любой слишком большой файл; остальное — 422
        raise ServiceError(413 if e.code == "pdf_too_large" else 422, e.code, e.message, e.field) from None
    if pdf.scanned:
        return _parse_pdf_scan(content)
    notes = list(pdf.notes)
    text = pdf.text_outside if pdf.tables else pdf.text
    if len(text) > MAX_TEXT_CHARS:
        text = text[:MAX_TEXT_CHARS]
        notes.append(f"Разобраны первые {MAX_TEXT_CHARS} символов текста.")
    result = parse_lab_text(text, _cfg(), extended=True, tables=pdf.tables, table_above=pdf.table_above)
    if not result["values"] and not result["ignored"] and pdf.images and photo_ocr.tesseract_available():
        # Текстовый слой есть, но показателей в нём нет, а на страницах картинки (скан с подписью сканера).
        try:
            scan = _parse_pdf_scan(content)
        except ServiceError:
            scan = None
        if scan is not None and scan["values"]:
            return scan
    result["notes"] = notes + result["notes"]
    result["source"] = {"format": "pdf", "pages_total": pdf.pages_total, "pages_read": pdf.pages_read}
    return result


def _parse_pdf_scan(content: bytes) -> dict:
    """Скан-PDF без текстового слоя -> тот же ответ, что у фото (warnings, check), source.format = "pdf_scan".

    До pdf_text.SCAN_MAX_PAGES страниц рендерятся pypdfium2 при SCAN_DPI в дочернем процессе (пределы памяти и
    жёсткий таймаут — как у разбора PDF); каждая страница распознаётся photo_ocr.extract_text (Tesseract в своём
    дочернем процессе; таймаут страницы — photo_ocr.MAX_SECONDS, всех страниц — SCAN_TOTAL_SECONDS). Распознанный
    текст в ответ и в журнал не попадает. Без tesseract — 422 ocr_unavailable."""
    import time

    from deficitlens_core.io import photo_ocr
    from deficitlens_core.io.pdf_text import render_pages
    from deficitlens_core.io.text_parser import MAX_TEXT_CHARS, parse_lab_text

    if not photo_ocr.tesseract_available():
        raise ServiceError(422, "ocr_unavailable", SCAN_UNAVAILABLE, "file")
    try:
        pages, total = render_pages(content)
    except InputError as e:
        raise ServiceError(422, e.code, e.message, e.field) from None
    deadline = time.monotonic() + SCAN_TOTAL_SECONDS
    lines: list[str] = []
    low: set[int] = set()
    notes: list[str] = []
    read = 0
    for k, png in enumerate(pages):
        remaining = deadline - time.monotonic()
        if k and remaining < 5:
            notes.append(f"Скан распознавался слишком долго — прочитаны первые {k} стр.; проверьте, все ли "
                         "показатели найдены.")
            break
        try:
            ocr = photo_ocr.extract_text(png, max_seconds=min(photo_ocr.MAX_SECONDS, max(remaining, 1.0)))
        except InputError as e:
            if e.code == "no_text":                  # пустая страница скана
                read += 1
                continue
            if k and e.code == "ocr_too_slow":
                notes.append(f"Скан распознавался слишком долго — прочитаны первые {k} стр.; проверьте, все ли "
                             "показатели найдены.")
                break
            message = SCAN_UNAVAILABLE if e.code == "ocr_unavailable" else e.message
            raise ServiceError(422, e.code, message, e.field) from None
        offset = len(lines)
        lines.extend(ocr.text.splitlines())
        low.update(offset + n for n in ocr.low_confidence_lines)
        notes.extend(ocr.notes)
        read += 1
    if total > len(pages):
        notes.append(f"В PDF {total} стр. — распознаны первые {len(pages)}; проверьте, все ли показатели найдены.")
    text = "\n".join(lines)
    if not text.strip():
        raise ServiceError(422, "no_text", "В скане PDF не найден текст: отсканируйте бланк чётче или загрузите "
                           "PDF с текстом.", "file")
    if len(text) > MAX_TEXT_CHARS:
        text = text[:MAX_TEXT_CHARS]
        notes.append(f"Разобраны первые {MAX_TEXT_CHARS} символов распознанного текста.")
    result = parse_lab_text(text, _cfg(), line_numbers=True, report_no_number=True, extended=True)
    result = _with_check_marks(result, low)
    result["notes"] = [SCAN_NOTE, *dict.fromkeys(notes), *result["notes"]]
    result["source"] = {"format": "pdf_scan", "pages_total": total, "pages_read": read}
    return result


def _with_check_marks(result: dict, low_confidence_lines: set[int]) -> dict:
    """Значение из строки, где Tesseract не уверен в цифрах: warnings — «строка N: <показатель> — проверьте
    значение», check — коды показателей (страница подсвечивает эти поля). Поле lines наружу не идёт."""
    cfg = _cfg()
    found_at = result.pop("lines")
    check = [code for code in result["values"] if found_at.get(code) in low_confidence_lines]
    result["warnings"] = [f"строка {found_at[code]}: {cfg.analytes[code].get('name_ru', code)} — проверьте значение"
                          for code in check]
    result["check"] = check
    return result


def _parse_photo(content: bytes) -> dict:
    """Фото бланка -> тот же ответ, что у текста и PDF, плюс пометки «проверьте значение».

    Распознавание — локальный Tesseract в дочернем процессе с жёстким таймаутом (io/photo_ocr.py). Распознанный
    текст разбирается text_parser и в ответ НЕ попадает (в нём ФИО, даты, телефон): только показатели, номера
    нераспознанных строк и замечания из справочника. Значение из строки, где Tesseract не уверен в цифрах,
    возвращается с пометкой: warnings — «строка N: <показатель> — проверьте значение», check — коды показателей
    (страница подсвечивает эти поля). Расчёт по фото сам не запускается: пользователь сверяет форму
    и нажимает «Рассчитать»."""
    from deficitlens_core.io import photo_ocr
    from deficitlens_core.io.text_parser import parse_lab_text

    try:
        ocr = photo_ocr.extract_text(content)
    except InputError as e:
        raise ServiceError(422, e.code, e.message, e.field) from None
    result = parse_lab_text(ocr.text, _cfg(), line_numbers=True, report_no_number=True, extended=True)
    result = _with_check_marks(result, ocr.low_confidence_lines)
    result["notes"] = [PHOTO_NOTE, *ocr.notes, *result["notes"]]
    result["source"] = {"format": "photo"}
    return result


def _thresholds(cfg: Config) -> list[dict]:
    """Пороги показа и правил из config/norms_ru.yaml: значения, источник, статус проверки, действует ли порог."""
    norms = cfg.norms
    sources = norms.get("sources", {})
    from deficitlens_core.rules import foreign_blocked
    by_lower = {code.lower(): code for code in cfg.analytes}
    service_keys = {"source", "sources", "status", "note", "origin", "formula_source", "mentzer_source"}
    out = []
    for key, spec in norms.items():
        if not isinstance(spec, dict) or key in ("policy", "sources"):
            continue
        if "source" not in spec and "sources" not in spec:
            continue
        refs = []
        src = spec.get("sources") if isinstance(spec.get("sources"), dict) else {"": spec.get("source")}
        statuses = spec.get("status")
        for role, skey in src.items():
            status = statuses.get(role) if isinstance(statuses, dict) else statuses
            info = sources.get(skey, {})
            refs.append({"role": role or None, "key": skey, "title": info.get("title", skey),
                         "url": info.get("url") or None, "status": status, "status_ru": STATUS_RU.get(status, status)})
        code = by_lower.get(key)
        notes = [str(v) for k, v in spec.items() if k.endswith("note") or k.endswith("_label")]
        origin = spec.get("origin", "ru_or_who")
        out.append({
            "key": key, "analyte": code,
            "name_ru": cfg.name_ru(code) if code else THRESHOLD_TITLES.get(key, key),
            "unit_ru": cfg.unit_ru(code) if code else None,
            "values": {k: v for k, v in spec.items()
                       if k not in service_keys and not k.endswith("note") and not k.endswith("_label")},
            "sources": refs, "notes": notes, "origin": origin,
            "active": not foreign_blocked(cfg, key),
        })
    return out


def _unit_choices(spec: dict) -> list[dict]:
    """Единицы для выбора в форме и подписи единиц на странице файла: по одной на множитель пересчёта в каноническую
    единицу (analytes.yaml → units), каноническая (множитель 1) — первой. Название — русское, если оно есть среди
    синонимов (г/дл, мкг/дл, пмоль/л), иначе первое из списка; любое из них сервер принимает в поле unit
    (normalize.unit_key) и сам пересчитывает значение тем же множителем."""
    import re
    groups: dict[float, list[str]] = {}
    for name, factor in (spec.get("units") or {}).items():
        groups.setdefault(float(factor), []).append(str(name))
    out = [{"unit": next((n for n in names if re.search("[а-яё]", n, re.IGNORECASE)), names[0]), "factor": factor}
           for factor, names in groups.items()]
    return sorted(out, key=lambda u: u["factor"] != 1.0)        # сортировка устойчивая: каноническая первой


def reference() -> dict:
    """Справочник для интерфейса и интеграции: показатели и единицы, группы формы, пороги с источниками и
    статусами, цены, версии, правило выбора источников. unit_choices — единицы для выбора (_unit_choices)."""
    cfg = _cfg()
    analytes = []
    for code, spec in cfg.analytes.items():
        price, available = cfg.price(code)
        analytes.append({
            "code": code, "name_ru": spec.get("name_ru", code), "short_ru": spec.get("short_ru", code),
            "unit": spec.get("unit"), "unit_ru": spec.get("unit_ru", ""), "group": spec.get("group"),
            "units": sorted((spec.get("units") or {}).keys()),
            "unit_choices": _unit_choices(spec),
            "price_rub": price, "available": available,
            "price_note": cfg.prices.get(code, {}).get("note"),
        })
    known_groups = {g for _, _, gs in FORM_GROUPS for g in gs}
    form_groups = []
    for gid, title, groups in FORM_GROUPS:
        codes = [a["code"] for a in analytes
                 if a["group"] in groups or (gid == "other" and a["group"] not in known_groups)]
        form_groups.append({"id": gid, "title": title, "always_open": gid == "cbc", "analytes": codes})
    prices_meta = _prices_meta()
    norms = cfg.norms
    return {
        "versions": {**_versions(), "prices": prices_meta.get("version")},
        "analytes": analytes,
        "form_groups": form_groups,
        "thresholds": _thresholds(cfg),
        "no_reference_yet": list(norms.get("no_reference_yet", [])),
        "sources": norms.get("sources", {}),
        "policy": norms.get("policy", {}),
        # Переключатель точки направления на ферритин (скрининг по ОАК): варианты, значение по умолчанию и что даёт
        # каждый вариант на 1000 человек (docs/metrics/screen_metrics.json; в коде страницы чисел нет).
        "screening": {"refer_share": float(cfg.screen.get("refer_share", 0.30)),
                      "refer_share_options": list(REFER_SHARE_OPTIONS), **screen_referral()},
        # Референсы проекта по умолчанию (norms_ru.yaml → reference_ranges) по полу, в канонических единицах —
        # для формы «Референсы лаборатории» на странице: пустое поле = значение по умолчанию.
        "reference_ranges": _reference_ranges_for_ui(cfg),
        "prices": {"currency": prices_meta.get("currency", "RUB"), "version": prices_meta.get("version"),
                   "blood_draw": prices_meta.get("blood_draw"), "regions": prices_meta.get("regions") or [],
                   "items": cfg.prices},
        "class_names": {"groups5": cfg.class_mapping.get("groups5", {}),
                        "classes12": cfg.class_mapping.get("classes12", {}),
                        "causes11": cfg.class_mapping.get("causes11", {}),
                        "nutrients": cfg.class_mapping.get("nutrients", {})},
        "disclaimer": "Исследовательский прототип, не медицинское изделие. Решение принимает врач.",
    }


REFER_SHARE_OPTIONS = (0.2, 0.3, 0.4)
REFERRAL_KEY = "referral_F18-49_t15"          # группа проверки: женщины 18–49 лет без анемии, цель — ферритин < 15
REFERRAL_FIELDS = ("refer_share", "referred_per1000", "found_per1000", "cases_per1000", "sensitivity", "ppv")


@lru_cache(maxsize=1)
def _screen_referral_cached(path: str, mtime: float) -> dict:
    """Что даёт точка направления на ферритин: из docs/metrics/screen_metrics.json → summary.referral_F18-49_t15.
    Считается на тесте NHANES 2017–2023 (США), см. summary.validation: на 1000 женщин 18–49 лет без анемии по ВОЗ —
    сколько направим (referred_per1000), сколько у них ферритин < 15 мкг/л всего (cases_per1000), скольких из них
    найдём (found_per1000), чувствительность (доля) и PPV (доля направленных, у кого дефицит подтвердится)."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        summary = data["summary"]
        target = (data.get("settings") or {}).get("targets_ferritin_ug_l", {}).get("t15")   # порог цели, мкг/л
        rows = []
        for row in summary[REFERRAL_KEY]:
            share = round(float(row["refer_share"]), 2)
            if share in REFER_SHARE_OPTIONS:
                rows.append({k: row[k] for k in REFERRAL_FIELDS} | {"refer_share": share})
        # Схема проверки — без служебной части про набор признаков («признаки full_cbc …»): странице она не нужна.
        validation = "; ".join(p for p in str(summary.get("validation") or "").split("; ") if not p.startswith("признаки"))
        return {"referral": sorted(rows, key=lambda r: r["refer_share"]),
                "referral_meta": {"source": "docs/metrics/screen_metrics.json", "group_ru": "женщин 18–49 лет без анемии",
                                  "ferritin_below": target, "validation": validation or None}}
    except Exception:  # noqa: BLE001 — файла метрик может не быть: тогда карточки показываются без чисел
        return {"referral": [], "referral_meta": None}


def screen_referral() -> dict:
    p = metrics_dir() / "screen_metrics.json"
    if not p.exists():
        return {"referral": [], "referral_meta": None}
    return _screen_referral_cached(str(p), p.stat().st_mtime)


def _reference_ranges_for_ui(cfg) -> dict:
    from deficitlens_core.references import project_ranges

    section = cfg.norms.get("reference_ranges") or {}
    by_sex = {s: {a: [r.low, r.high] for a, r in project_ranges(cfg, s).items()} for s in ("F", "M")}
    return {"label": section.get("label", "референс проекта по умолчанию"), "table": section.get("table"),
            "apply_in_pregnancy": bool(section.get("apply_in_pregnancy", False)), "by_sex": by_sex}


@lru_cache(maxsize=1)
def _prices_meta() -> dict:
    """Версия и валюта прайса (в Config попадает только словарь цен)."""
    import yaml
    from deficitlens_core.config import config_dir
    try:
        raw = yaml.safe_load((config_dir() / "prices.yaml").read_text(encoding="utf-8")) or {}
        return {k: raw.get(k) for k in ("version", "currency", "blood_draw", "regions")}
    except Exception:  # noqa: BLE001
        return {}


def examples() -> dict:
    """Демонстрационные случаи из data/demo/examples.json. Поле expect (инварианты для тестов) наружу не отдаётся."""
    path = demo_dir() / "examples.json"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"examples": []}
    public = []
    for ex in raw.get("examples", []):
        item = {k: ex[k] for k in ("id", "title", "pain", "input") if k in ex}
        if isinstance(ex.get("followup"), dict):
            item["followup"] = {k: ex["followup"][k] for k in ("add_values", "title") if k in ex["followup"]}
        public.append(item)
    return {"examples": public}
