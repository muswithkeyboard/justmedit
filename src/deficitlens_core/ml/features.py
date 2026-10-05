"""Признаки модели по файлу кейса: матрица X, правило ВОЗ, «замок ВОЗ», производные признаки, уровень полноты.

Матрица X — (n, 37) в порядке constants.FEATS: возраст, пол, 9 показателей ОАК и 26 анализов вне ОАК.
Единицы — канонические (config/analytes.yaml): Hb в г/л, MCV в фл, ферритин в мкг/л и т. д.
Несданный анализ = NaN. Никаких индикаторов «сдан / не сдан» здесь не создаётся: в клиническом режиме
пропуск не несёт информации (см. ml/fusion.py).
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd

from ..constants import ALLOW, F_IDX, FEATS, LAB_IDX, LABS, LEVELS

N_FEATS = len(FEATS)
LEVEL_NAMES = list(LEVELS)  # "cbc" < "cbc_iron_crp" < "standard" < "extended"
# Что уровень полноты ВЫБРАСЫВАЕТ: номера столбцов анализов вне ОАК, не входящих в список уровня.
LEVEL_DROP_IDX = {lv: [F_IDX[a] for a in LABS if a not in keep] for lv, keep in LEVELS.items()}

# Порог анемии по гемоглобину, г/л: ВОЗ 2024 — Hb < 120 у женщин, < 130 у мужчин (взрослые, не беременные).
# Это часть модели («замок ВОЗ» при обучении и применении), поэтому значение зашито рядом с алгоритмом;
# пороги ПОКАЗА и тяжесть анемии живут в config/norms_ru.yaml и считаются в level1.py.
WHO_HB_FEMALE = 120.0
WHO_HB_MALE = 130.0

_SEX_CODES = {"f": 0.0, "m": 1.0, "0": 0.0, "1": 1.0}


def _sex_to_num(v) -> float:
    """Пол: "F" -> 0, "M" -> 1 (как в прототипе); числа 0/1 проходят как есть; всё остальное -> NaN."""
    if v is None:
        return math.nan
    if isinstance(v, str):
        return _SEX_CODES.get(v.strip().lower(), math.nan)
    try:
        f = float(v)
    except (TypeError, ValueError):
        return math.nan
    return f if f in (0.0, 1.0) else math.nan


def _to_num(v) -> float:
    """Значение анализа -> float. None, пустая строка, нечисловой текст -> NaN (анализ не сдан)."""
    if v is None:
        return math.nan
    if isinstance(v, bool):
        return math.nan
    if isinstance(v, (int, float, np.integer, np.floating)):
        return float(v)
    if isinstance(v, str):
        s = v.strip()
        if not s:
            return math.nan
        try:
            return float(s)
        except ValueError:
            return math.nan
    if isinstance(v, dict) and "value" in v:  # {"value": 12.3, "unit": ...} — на случай сырого ввода
        return _to_num(v["value"])
    value = getattr(v, "value", None)       # schemas.LabValue
    return _to_num(value) if value is not None else math.nan


def to_matrix(records) -> np.ndarray:
    """list[dict] | DataFrame -> матрица (n, 37) в порядке FEATS.

    Отсутствующий ключ (столбец), None, NaN и пустая строка означают одно и то же: анализ не сдан (NaN).
    Пол: "F" / "M" -> 0 / 1. Лишние ключи и столбцы игнорируются — в модель попадает только белый список FEATS.
    """
    if isinstance(records, pd.DataFrame):
        X = np.full((len(records), N_FEATS), np.nan)
        for j, f in enumerate(FEATS):
            if f not in records.columns:
                continue
            col = records[f]
            if f == "sex":
                X[:, j] = [_sex_to_num(v) for v in col.tolist()]
            elif pd.api.types.is_bool_dtype(col):
                continue
            elif pd.api.types.is_numeric_dtype(col):
                X[:, j] = col.to_numpy(dtype=float, na_value=np.nan)
            else:
                X[:, j] = [_to_num(v) for v in col.tolist()]
        return X
    if isinstance(records, dict):
        records = [records]
    records = list(records)
    X = np.full((len(records), N_FEATS), np.nan)
    for i, rec in enumerate(records):
        for key, v in rec.items():
            j = F_IDX.get(key)
            if j is not None:
                X[i, j] = _sex_to_num(v) if key == "sex" else _to_num(v)
    return X


def who_anemia(X) -> np.ndarray:
    """Анемия по ВОЗ 2024: Hb < 120 г/л у женщин, < 130 г/л у мужчин. Возвращает 0/1 для каждой строки.

    Если гемоглобин не задан (NaN), сравнение ложно и строка считается «без анемии»: проверять наличие
    гемоглобина должен вызывающий код (normalize.py, io/table.py).
    """
    X = np.asarray(X, dtype=float)
    hb = X[:, F_IDX["hemoglobin"]]
    male = X[:, F_IDX["sex"]] == 1
    with np.errstate(invalid="ignore"):
        return (hb < np.where(male, WHO_HB_MALE, WHO_HB_FEMALE)).astype(int)


def who_lock(P, anemia) -> np.ndarray:
    """«Замок ВОЗ»: обнуляет профили, несовместимые с анемией по Hb, и перенормирует вероятности.

    При анемии запрещены профили «без анемии» (здоров, латентный дефицит, B12 и фолаты без анемии), без анемии —
    все анемические профили; дефицит B6 и меди разрешён в обоих случаях (constants.ALLOW). Если после обнуления
    не осталось массы, вероятность делится поровну между разрешёнными профилями.
    """
    P = np.asarray(P, dtype=float)
    A = ALLOW[np.asarray(anemia, dtype=int)]
    Q = P * A
    s = Q.sum(axis=1, keepdims=True)
    fallback = A / A.sum(axis=1, keepdims=True)
    return np.where(s > 0, Q / np.where(s > 0, s, 1.0), fallback)


def feats_plus(X) -> np.ndarray:
    """X + 6 производных признаков для бустинга режима бенчмарка (как в прототипе ensemble_check.py) -> (n, 43).

      1. MCV / RBC                 — индекс Ментцера (микроцитоз: дефицит железа против талассемии);
      2. RDW / MCV × 100           — разнородность эритроцитов относительно их объёма;
      3. sTfR / log10(ферритин)    — индекс sTfR-ферритин (дефицит железа на фоне воспаления);
      4. железо / ОЖСС × 100       — расчётное насыщение трансферрина, %;
      5. B12 / фолаты              — какой из двух дефицитов преобладает;
      6. анемия по ВОЗ (0/1)       — правило по Hb и полу.
    Если хотя бы один из двух анализов не сдан, производный признак равен NaN (бустинг работает с NaN нативно).
    Отличие от прототипа: деление на ноль (ферритин ровно 1 мкг/л, RBC = 0 и т. п.) даёт NaN, а не бесконечность, —
    иначе scikit-learn отказывается считать. В файле кейса таких строк нет, поэтому числа прототипа не меняются.
    """
    X = np.asarray(X, dtype=float)
    g = lambda name: X[:, F_IDX[name]]  # noqa: E731
    with np.errstate(all="ignore"):
        extra = np.column_stack([
            g("MCV") / g("RBC"),
            g("RDW") / g("MCV") * 100,
            g("sTfR") / np.log10(np.clip(g("ferritin"), 1e-3, None)),
            g("serum_iron") / g("TIBC") * 100,
            g("vitamin_B12") / np.clip(g("folate"), 1e-3, None),
            who_anemia(X),
        ])
    extra[~np.isfinite(extra)] = np.nan
    return np.column_stack([X, extra])


def apply_level(X, level: str) -> np.ndarray:
    """Обрезка строк до уровня полноты: все анализы вне списка constants.LEVELS[level] становятся NaN (копия X)."""
    X = np.array(X, dtype=float, copy=True)
    drop = LEVEL_DROP_IDX[level]
    if drop:
        X[:, drop] = np.nan
    return X


def completeness_many(X) -> np.ndarray:
    """Уровень полноты для каждой строки матрицы (см. completeness)."""
    X = np.asarray(X, dtype=float)
    M = ~np.isnan(X[:, LAB_IDX])                       # (n, 26): сдан ли анализ вне ОАК
    out = np.full(len(X), "extended", dtype=object)
    # От широкого уровня к узкому: последним присваивается самый узкий подходящий уровень.
    for lv in ("standard", "cbc_iron_crp", "cbc"):
        outside = np.array([a not in LEVELS[lv] for a in LABS])
        out[~(M & outside).any(axis=1)] = lv
    return out


def completeness(x_row) -> str:
    """Уровень полноты панели одной строки:

      "cbc"          — не сдан ни один анализ вне ОАК;
      "cbc_iron_crp" — все сданные анализы входят в набор «обмен железа + СРБ»;
      "standard"     — все сданные входят в набор «обмен железа + СРБ + B12 + фолаты»;
      "extended"     — сдано что-то ещё.
    """
    return str(completeness_many(np.asarray(x_row, dtype=float).reshape(1, -1))[0])
