#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
run_experiment.py — сравнение движков классификации для кейса
«ИИ-скрининг латентных дефицитных состояний» (синтетика, 840 строк).

Запуск:  python3 run_experiment.py            # полный прогон: 5 фолдов x 3 повтора
         python3 run_experiment.py --quick    # один фолд (проверка и замер времени)

Что внутри
  V0  мажоритарный класс (частоты профилей обучающей части)
  V1  «потолок утечки»: бустинг только на индикаторах «анализ сдан / не сдан»
  V2  наивный бустинг на всех признаках (NaN нативно)
  V3  бустинг только на ОАК
  V4  правила-пороги (грубая база)
  V5  слияние без утечки (fusion-lite): приор по ОАК x таблицы правдоподобия + замок ВОЗ
  V6  бустинг с маскированием (выравнивание частоты «сдано» + обрезка до уровней) + замок ВОЗ
  V6w доп. вариант вне ТЗ: вместо выбрасывания измерений — веса обратной вероятности (IPW)
  V7  V2 + замок ВОЗ
  V5L доп. вариант вне ТЗ («режим бенчмарка»): V5 + множитель P(сдан/не сдан | профиль),
      т.е. тот же движок слияния, но с намеренно включённой утечкой (один переключатель)
  V1eq диагностика: остаточная утечка после выравнивания (индикаторы, обучение и тест выровнены)

Всё обучаемое (модели, границы бинов, таблицы, веса групп, температура приора,
вероятности маскирования) оценивается ТОЛЬКО по обучающей части внешнего фолда.
Гиперпараметры бустинга зафиксированы заранее и по тестовым фолдам не подбирались.

Переносимые в продукт функции: to_matrix, fit_v5 / predict_v5, fit_v6 / predict_v6.
"""
import os

os.environ.setdefault("OMP_NUM_THREADS", "1")  # 1 поток на процесс: параллелим по фолдам

import json
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd
from joblib import Parallel, delayed
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.metrics import accuracy_score, confusion_matrix, f1_score, roc_auc_score
from sklearn.model_selection import RepeatedStratifiedKFold, StratifiedKFold

DATA = Path(os.environ.get("DL_CASE_DATA", "data/case/deficiency_anemia.csv"))  # путь к файлу кейса (840 строк)
OUT = Path(__file__).resolve().parent
SEED = 20261003

# ----------------------------------------------------------------------------
# 1. Словари признаков, групп анализов, уровней полноты и классов
# ----------------------------------------------------------------------------
CBC = ["age_years", "sex", "hemoglobin", "RBC", "hematocrit", "MCV", "MCH", "MCHC", "RDW", "platelets", "WBC"]
GROUPS = {
    "iron": ["ferritin", "serum_iron", "transferrin", "TIBC", "UIBC", "TSAT", "sTfR", "Ret_He"],
    "b12": ["vitamin_B12", "active_B12", "MMA", "homocysteine"],
    "folate": ["folate"],
    "copper": ["copper", "ceruloplasmin"],
    "b6": ["vitamin_B6"],
    "inflammation": ["CRP", "ESR"],
    "hemolysis": ["LDH", "indirect_bilirubin", "haptoglobin", "reticulocytes"],
    "kidney": ["creatinine", "eGFR"],
    "tsh": ["TSH"],
    "albumin": ["albumin"],
}
GROUP_NAMES = list(GROUPS)
LABS = [a for g in GROUPS.values() for a in g]  # 26 анализов вне ОАК
FEATS = CBC + LABS  # 37 признаков, порядок столбцов матрицы X
F_IDX = {f: i for i, f in enumerate(FEATS)}
CBC_IDX = [F_IDX[f] for f in CBC]
LAB_IDX = [F_IDX[f] for f in LABS]
GROUP_IDX = [[F_IDX[a] for a in GROUPS[g]] for g in GROUP_NAMES]
LAB_GROUP = [GROUP_NAMES.index(g) for g in GROUP_NAMES for _ in GROUPS[g]]  # группа каждого анализа
NG = len(GROUP_NAMES)

_L_IRON = ["ferritin", "serum_iron", "transferrin", "TIBC", "UIBC", "TSAT", "CRP"]
LEVELS = {  # какие анализы (кроме ОАК) оставляет уровень полноты
    "L_cbc": [],
    "L_iron": _L_IRON,
    "L_std": _L_IRON + ["vitamin_B12", "folate"],
    "L_asis": LABS,
}
LEVEL_NAMES = list(LEVELS)
LEVEL_DROP_IDX = {lv: [F_IDX[a] for a in LABS if a not in keep] for lv, keep in LEVELS.items()}

# 14 профилей = 12 классов, где mixed разбит на 3 подтипа
PROFILES = [
    "no_anemia_no_deficiency", "latent_deficiency", "B12_deficiency_no_anemia", "folate_deficiency_no_anemia",
    "iron_deficiency_anemia", "B12_deficiency_anemia", "folate_deficiency_anemia",
    "mixed:iron_B12", "mixed:iron_folate", "mixed:B12_folate",
    "inflammation_anemia", "anemia_other", "B6_deficiency", "copper_deficiency",
]
NP_ = len(PROFILES)
P_IDX = {p: i for i, p in enumerate(PROFILES)}
NONANEMIC = [0, 1, 2, 3]            # допустимы только без анемии
ANEMIC = [4, 5, 6, 7, 8, 9, 10, 11]  # допустимы только при анемии
BOTH = [12, 13]                      # B6 и медь — в обоих случаях
ALLOW = np.zeros((2, NP_), dtype=bool)
ALLOW[0, NONANEMIC + BOTH] = True
ALLOW[1, ANEMIC + BOTH] = True

CLASSES12 = [
    "no_anemia_no_deficiency", "latent_deficiency", "B12_deficiency_no_anemia", "folate_deficiency_no_anemia",
    "iron_deficiency_anemia", "B12_deficiency_anemia", "folate_deficiency_anemia", "mixed_deficiency",
    "inflammation_anemia", "anemia_other", "B6_deficiency", "copper_deficiency",
]
CAUSES11 = ["none", "iron_deficiency", "B12_deficiency", "folate_deficiency", "iron_B12", "iron_folate",
            "B12_folate", "inflammation", "undetermined", "B6_deficiency", "copper_deficiency"]
PROFILE_TO_CLASS = {p: ("mixed_deficiency" if p.startswith("mixed:") else p) for p in PROFILES}
PROFILE_TO_CAUSE = {
    "no_anemia_no_deficiency": "none", "latent_deficiency": "iron_deficiency",
    "B12_deficiency_no_anemia": "B12_deficiency", "folate_deficiency_no_anemia": "folate_deficiency",
    "iron_deficiency_anemia": "iron_deficiency", "B12_deficiency_anemia": "B12_deficiency",
    "folate_deficiency_anemia": "folate_deficiency", "mixed:iron_B12": "iron_B12",
    "mixed:iron_folate": "iron_folate", "mixed:B12_folate": "B12_folate",
    "inflammation_anemia": "inflammation", "anemia_other": "undetermined",
    "B6_deficiency": "B6_deficiency", "copper_deficiency": "copper_deficiency",
}
# матрицы свёртки вероятностей: 14 профилей -> 12 классов / 11 причин
A12 = np.zeros((NP_, len(CLASSES12)))
A11 = np.zeros((NP_, len(CAUSES11)))
for _p, _i in P_IDX.items():
    A12[_i, CLASSES12.index(PROFILE_TO_CLASS[_p])] = 1
    A11[_i, CAUSES11.index(PROFILE_TO_CAUSE[_p])] = 1

GROUPS5 = ["healthy", "iron_deficiency_anemia", "vitamin_B12_deficiency_anemia", "unexplained_anemia",
           "other_deficiency_anemia"]
# группа кейса для класса при наличии анемии (без анемии всегда healthy)
G5_IF_ANEMIA = np.array([3, 1, 2, 4, 1, 2, 4, 4, 3, 3, 4, 4])


def group5(cls12, anemia):
    return np.where(np.asarray(anemia) == 1, G5_IF_ANEMIA[np.asarray(cls12)], 0)


# ----------------------------------------------------------------------------
# 2. Базовые преобразования
# ----------------------------------------------------------------------------
def to_matrix(df):
    """DataFrame (или список словарей) -> матрица (n, 37) в порядке FEATS.
    Отсутствующий столбец = анализ не сдан (NaN). sex: 'F'/'M' -> 0/1."""
    df = pd.DataFrame(df).copy()
    if "sex" in df and df["sex"].dtype == object or str(df["sex"].dtype).startswith("str"):
        df["sex"] = df["sex"].map({"F": 0.0, "M": 1.0})
    for f in FEATS:
        if f not in df:
            df[f] = np.nan
    return df[FEATS].to_numpy(dtype=float)


def who_anemia(X):
    """Анемия по ВОЗ: Hb < 120 г/л у женщин, < 130 г/л у мужчин."""
    hb = X[:, F_IDX["hemoglobin"]]
    male = X[:, F_IDX["sex"]] == 1
    return (hb < np.where(male, 130.0, 120.0)).astype(int)


def who_lock(P, anemia):
    """«Замок ВОЗ»: обнуляет профили, несовместимые с анемией по Hb, и перенормирует."""
    A = ALLOW[np.asarray(anemia)]
    Q = P * A
    s = Q.sum(axis=1, keepdims=True)
    fallback = A / A.sum(axis=1, keepdims=True)
    return np.where(s > 0, Q / np.where(s > 0, s, 1.0), fallback)


def apply_level(X, level):
    """Обрезка строк до уровня полноты: всё вне списка уровня -> NaN."""
    X = X.copy()
    drop = LEVEL_DROP_IDX[level]
    if drop:
        X[:, drop] = np.nan
    return X


def indicators(X):
    """Индикаторы «анализ сдан» по 26 анализам (без значений и без ОАК)."""
    return (~np.isnan(X[:, LAB_IDX])).astype(float)


def group_measured(X):
    """(n, NG): сдан ли хотя бы один анализ группы."""
    return np.stack([(~np.isnan(X[:, idx])).any(axis=1) for idx in GROUP_IDX], axis=1)


def make_hgb(seed, min_samples_leaf=10):
    """Единая заранее зафиксированная конфигурация бустинга для всех вариантов."""
    return HistGradientBoostingClassifier(
        learning_rate=0.08, max_iter=120, max_leaf_nodes=8, min_samples_leaf=min_samples_leaf,
        l2_regularization=1.0, early_stopping=False, random_state=seed)


def proba14(model, Xf):
    """predict_proba, разложенный по всем 14 профилям (если какого-то не было в обучении — 0)."""
    P = np.zeros((len(Xf), NP_))
    P[:, model.classes_] = model.predict_proba(Xf)
    return P


# ----------------------------------------------------------------------------
# 3. V4 — правила-пороги (грубая база, не полировалась)
# ----------------------------------------------------------------------------
def predict_v4(X):
    g = lambda name: X[:, F_IDX[name]]
    an = who_anemia(X) == 1
    with np.errstate(invalid="ignore"):
        fer, b12v = g("ferritin"), g("vitamin_B12")
        iron = np.where(~np.isnan(fer), fer < 30, g("TSAT") < 16)
        b12 = np.where(~np.isnan(b12v), b12v < 200, (g("active_B12") < 35) | (g("MMA") > 0.4))
        fol, b6, cu, infl = g("folate") < 4, g("vitamin_B6") < 20, g("copper") < 11, g("CRP") > 5
    prof = np.select(
        [iron & b12, iron & fol, b12 & fol, iron & an, iron, b12 & an, b12, fol & an, fol, b6, cu, an & infl, an],
        [P_IDX["mixed:iron_B12"], P_IDX["mixed:iron_folate"], P_IDX["mixed:B12_folate"],
         P_IDX["iron_deficiency_anemia"], P_IDX["latent_deficiency"],
         P_IDX["B12_deficiency_anemia"], P_IDX["B12_deficiency_no_anemia"],
         P_IDX["folate_deficiency_anemia"], P_IDX["folate_deficiency_no_anemia"],
         P_IDX["B6_deficiency"], P_IDX["copper_deficiency"],
         P_IDX["inflammation_anemia"], P_IDX["anemia_other"]],
        default=P_IDX["no_anemia_no_deficiency"])
    return np.eye(NP_)[prof]


# ----------------------------------------------------------------------------
# 4. V5 — слияние без утечки (fusion-lite)
# ----------------------------------------------------------------------------
W_GRID = [1.0, 0.7, 0.5, 0.35, 0.25, 0.0]  # 0.0 добавлен сверх ТЗ: группа может быть отключена
T_GRID = [1.0, 0.7, 0.5, 0.35, 0.25]       # показатель степени («температура») приора по ОАК


def fit_tables(X, y, n_bins=5, alpha=1.0):
    """Для каждого анализа: квантильные границы по ИЗМЕРЕННЫМ значениям и log P(бин | профиль)
    со сглаживанием Лапласа. Несданный анализ в таблицы не попадает вовсе."""
    qs = np.linspace(0, 1, n_bins + 1)[1:-1]
    tabs = []
    for j in LAB_IDX:
        x = X[:, j]
        m = ~np.isnan(x)
        edges = np.unique(np.quantile(x[m], qs)) if m.sum() >= n_bins else np.array([])
        nb = len(edges) + 1
        cnt = np.zeros((NP_, nb))
        np.add.at(cnt, (y[m], np.searchsorted(edges, x[m], side="right")), 1)
        tabs.append((edges, np.log((cnt + alpha) / (cnt.sum(axis=1, keepdims=True) + alpha * nb))))
    return tabs


def group_loglik(tabs, X):
    """(n, NG, 14): сумма log P(бин | профиль) по сданным анализам каждой группы. Пропуск = 0."""
    LL = np.zeros((len(X), NG, NP_))
    for k, j in enumerate(LAB_IDX):
        edges, logp = tabs[k]
        x = X[:, j]
        m = ~np.isnan(x)
        if m.any():
            LL[m, LAB_GROUP[k], :] += logp[:, np.searchsorted(edges, x[m], side="right")].T
    return LL


def v5_posterior(prior, LL, t, w, anemia, extra=None):
    """апостериор ∝ приор^t × ∏_g (правдоподобие группы)^w_g, затем замок ВОЗ.
    extra — необязательная добавка к логарифму (используется только в V5L: член «сдан/не сдан»)."""
    logp = t * np.log(np.clip(prior, 1e-12, 1.0)) + np.tensordot(LL, np.asarray(w), axes=([1], [0]))
    if extra is not None:
        logp = logp + extra
    logp = np.where(ALLOW[np.asarray(anemia)], logp, -np.inf)
    logp -= logp.max(axis=1, keepdims=True)
    P = np.exp(logp)
    return P / P.sum(axis=1, keepdims=True)


V_GRID = [0.0, 0.25, 0.35, 0.5, 0.7, 1.0]  # вес члена «сдан/не сдан» в V5L


def fit_presence(X, y):
    """ТОЛЬКО для режима бенчмарка (V5L): log P(анализ сдан | профиль) и log P(не сдан | профиль)."""
    M = ~np.isnan(X[:, LAB_IDX])
    p = np.stack([(M[y == c].sum(axis=0) + 1.0) / ((y == c).sum() + 2.0) for c in range(NP_)])
    return np.log(p), np.log1p(-p)


def presence_loglik(ptabs, X):
    M = (~np.isnan(X[:, LAB_IDX])).astype(float)
    return M @ ptabs[0].T + (1.0 - M) @ ptabs[1].T


def fit_v5(X, y, seed=0, n_bins=5, alpha=1.0, inner_folds=5, passes=2):
    """X: (n, 37) в порядке FEATS, y: индекс профиля 0..13. Возвращает словарь-модель."""
    n = len(X)
    anemia = who_anemia(X)
    # 4.1 внутренние OOF: приор по ОАК и правдоподобия (таблицы тоже вне фолда)
    prior_oof = np.zeros((n, NP_))
    LL_oof = np.zeros((n, NG, NP_))
    PL_oof = np.zeros((n, NP_))
    for tr, va in StratifiedKFold(inner_folds, shuffle=True, random_state=seed).split(X, y):
        m = make_hgb(seed).fit(X[tr][:, CBC_IDX], y[tr])
        prior_oof[va] = proba14(m, X[va][:, CBC_IDX])
        LL_oof[va] = group_loglik(fit_tables(X[tr], y[tr], n_bins, alpha), X[va])
        PL_oof[va] = presence_loglik(fit_presence(X[tr], y[tr]), X[va])

    # 4.2 покоординатный подбор температуры приора и весов групп по log-loss на OOF
    def loss(params):
        P = v5_posterior(prior_oof, LL_oof, params[0], params[1:], anemia)
        return float(-np.mean(np.log(np.clip(P[np.arange(n), y], 1e-12, 1.0))))

    def nearest(v):
        return min(W_GRID, key=lambda g: abs(g - v))

    params = np.array([1.0] + [nearest(1 / np.sqrt(len(GROUPS[g]))) for g in GROUP_NAMES])
    best = loss(params)
    for _ in range(passes):
        for i in range(len(params)):
            for v in (T_GRID if i == 0 else W_GRID):
                trial = params.copy()
                trial[i] = v
                l = loss(trial)
                if l < best - 1e-9:
                    best, params = l, trial

    # 4.2b (только V5L) вес члена утечки при зафиксированных t и w
    def loss_v(v):
        P = v5_posterior(prior_oof, LL_oof, params[0], params[1:], anemia, extra=v * PL_oof)
        return float(-np.mean(np.log(np.clip(P[np.arange(n), y], 1e-12, 1.0))))

    v_losses = {v: loss_v(v) for v in V_GRID}
    v_best = min(v_losses, key=v_losses.get)

    # 4.3 финальные модель приора и таблицы на всей обучающей части
    return {
        "prior_model": make_hgb(seed).fit(X[:, CBC_IDX], y),
        "tables": fit_tables(X, y, n_bins, alpha),
        "t": float(params[0]), "w": params[1:].tolist(), "oof_logloss": best,
        "presence_tables": fit_presence(X, y), "v": float(v_best), "oof_logloss_with_presence": v_losses[v_best],
        "oof_logloss_prior_only": float(-np.mean(np.log(np.clip(prior_oof[np.arange(n), y], 1e-12, 1)))),
    }


def predict_v5(model, X, use_presence=False):
    """use_presence=False — клинический режим (пропуск анализа не несёт информации).
    use_presence=True  — режим бенчмарка V5L: добавляется член P(сдан/не сдан | профиль)."""
    prior = proba14(model["prior_model"], X[:, CBC_IDX])
    extra = model["v"] * presence_loglik(model["presence_tables"], X) if use_presence else None
    return v5_posterior(prior, group_loglik(model["tables"], X), model["t"], model["w"], who_anemia(X), extra)


# ----------------------------------------------------------------------------
# 5. V6 — бустинг с маскированием
# ----------------------------------------------------------------------------
def measured_rates(X, y):
    """p[c, g] = доля строк профиля c, где сдан хотя бы один анализ группы g (сглаживание Лапласа)."""
    M = group_measured(X)
    p = np.zeros((NP_, NG))
    for c in range(NP_):
        rows = y == c
        p[c] = (M[rows].sum(axis=0) + 1.0) / (rows.sum() + 2.0)
    return p


def keep_probs(p):
    """r[c, g] = min_c p[c, g] / p[c, g]: после прореживания частота «сдано» не зависит от профиля."""
    return p.min(axis=0, keepdims=True) / p


def mask_groups(X, y, r, rng):
    """Каждую сданную группу оставляем с вероятностью r[профиль, группа], иначе вся группа -> NaN."""
    X = X.copy()
    M = group_measured(X)
    keep = rng.random(M.shape) < r[y]
    for g, idx in enumerate(GROUP_IDX):
        drop = np.where(M[:, g] & ~keep[:, g])[0]
        if len(drop):
            X[np.ix_(drop, idx)] = np.nan
    return X


def truncate_random_levels(X, rng):
    """С вероятностью по 1/4 обрезаем копию до L_cbc / L_iron / L_std / оставляем как есть."""
    lv = rng.integers(0, len(LEVEL_NAMES), size=len(X))
    for k, name in enumerate(LEVEL_NAMES):
        rows = np.where(lv == k)[0]
        if len(rows) and LEVEL_DROP_IDX[name]:
            X[np.ix_(rows, LEVEL_DROP_IDX[name])] = np.nan
    return X


def fit_v6(X, y, seed=0, K=13, mode="mask"):
    """mode='mask' — вариант ТЗ: выравнивающее выбрасывание групп + обрезка до уровней.
    mode='ipw'  — доп. вариант: измерения не выбрасываются, строка получает вес обратной
                  вероятности (взвешенная частота «сдано» не зависит от профиля) + обрезка до уровней."""
    rng = np.random.default_rng(seed)
    p = measured_rates(X, y)
    r = keep_probs(p)
    Xa, ya = np.repeat(X, K, axis=0), np.repeat(y, K)
    sw = None
    if mode == "mask":
        Xa = mask_groups(Xa, ya, r, rng)
    else:
        M = group_measured(X)
        pbar = np.clip(M.mean(axis=0), 1e-3, 1 - 1e-3)
        w = np.where(M, pbar / p[y], (1 - pbar) / (1 - p[y])).prod(axis=1)
        w = np.clip(w, 0.1, 10.0)
        sw = np.repeat(w / w.mean(), K)
    Xa = truncate_random_levels(Xa, rng)
    # лист должен содержать копии минимум ~3 разных пациентов (копии одной строки делят ОАК)
    model = make_hgb(seed, min_samples_leaf=3 * K).fit(Xa, ya, sample_weight=sw)
    return {"model": model, "keep_prob": r, "measured_rate": p, "mode": mode, "K": K}


def predict_v6(model, X):
    return who_lock(proba14(model["model"], X), who_anemia(X))


# ----------------------------------------------------------------------------
# 6. Метрики
# ----------------------------------------------------------------------------
def ece_equal_mass(conf, correct, n_bins=10):
    order = np.argsort(conf, kind="stable")
    return float(sum(len(b) / len(conf) * abs(correct[b].mean() - conf[b].mean())
                     for b in np.array_split(order, n_bins) if len(b)))


def evaluate(P14, y, anemia_true, anemia_who):
    P12, P11 = P14 @ A12, P14 @ A11
    y12 = (np.eye(NP_)[y] @ A12).argmax(axis=1)
    y11 = (np.eye(NP_)[y] @ A11).argmax(axis=1)
    pred12, pred11 = P12.argmax(axis=1), P11.argmax(axis=1)
    g_true, g_pred = group5(y12, anemia_true), group5(pred12, anemia_who)
    conf, correct = P12.max(axis=1), (pred12 == y12).astype(float)
    out = {
        "acc12": accuracy_score(y12, pred12),
        "f1_12": f1_score(y12, pred12, labels=range(12), average="macro", zero_division=0),
        "f1_5": f1_score(g_true, g_pred, labels=range(5), average="macro", zero_division=0),
        "f1_cause": f1_score(y11, pred11, labels=range(11), average="macro", zero_division=0),
        "logloss12": float(-np.mean(np.log(np.clip(P12[np.arange(len(y)), y12], 1e-15, 1.0)))),
        "ece": ece_equal_mass(conf, correct),
    }
    # скрытый дефицит: среди строк без анемии, оценка = 1 - P(no_anemia_no_deficiency) после замка
    na = anemia_true == 0
    score = 1.0 - who_lock(P14, anemia_who)[na, 0]
    target = (y[na] != 0).astype(int)
    out["auroc_hidden"] = roc_auc_score(target, score) if 0 < target.sum() < len(target) else np.nan
    extra = {
        "cm5": confusion_matrix(g_true, g_pred, labels=range(5)),
        "cm12": confusion_matrix(y12, pred12, labels=range(12)),
        "conf": conf, "correct": correct,
    }
    return out, extra


def time_predict(fn, Xte, rng):
    fn(Xte[:1])
    t = []
    for i in range(100):
        x = Xte[i % len(Xte)][None, :]
        t0 = time.perf_counter()
        fn(x)
        t.append(time.perf_counter() - t0)
    Xbig = Xte[rng.integers(0, len(Xte), 10000)]
    t0 = time.perf_counter()
    fn(Xbig)
    return {"ms_per_single_row": 1000 * float(np.median(t)), "s_per_10000_rows": time.perf_counter() - t0}


# ----------------------------------------------------------------------------
# 7. Один внешний фолд
# ----------------------------------------------------------------------------
def run_fold(fold, tr, te, X, y, anemia_true):
    seed = 1000 + fold
    Xtr, ytr, Xte, yte = X[tr], y[tr], X[te], y[te]
    t0 = time.time()
    freq = np.bincount(ytr, minlength=NP_) / len(ytr)                 # V0
    v1 = make_hgb(seed).fit(indicators(Xtr), ytr)                     # V1
    v2 = make_hgb(seed).fit(Xtr, ytr)                                 # V2 / V7
    v5 = fit_v5(Xtr, ytr, seed)                                       # V5 (+ V3 = его приор)
    v6 = fit_v6(Xtr, ytr, seed, mode="mask")                          # V6
    v6w = fit_v6(Xtr, ytr, seed, mode="ipw")                          # V6w (вне ТЗ)
    r = v6["keep_prob"]
    # V1eq: индикаторы после выравнивания (остаточная утечка)
    rng_eq = np.random.default_rng(seed + 1)
    K_eq = 6
    v1eq = make_hgb(seed, min_samples_leaf=3 * K_eq).fit(
        indicators(mask_groups(np.repeat(Xtr, K_eq, axis=0), np.repeat(ytr, K_eq), r, rng_eq)),
        np.repeat(ytr, K_eq))
    fit_time = time.time() - t0

    def predict_all(XL):
        an = who_anemia(XL)
        p2 = proba14(v2, XL)
        return {
            "V0_majority": np.tile(freq, (len(XL), 1)),
            "V1_leak_only": proba14(v1, indicators(XL)),
            "V2_naive_gbm": p2,
            "V3_cbc_gbm": proba14(v5["prior_model"], XL[:, CBC_IDX]),
            "V4_rules": predict_v4(XL),
            "V5_fusion": predict_v5(v5, XL),
            "V5L_fusion_leak": predict_v5(v5, XL, use_presence=True),
            "V6_masked_gbm": predict_v6(v6, XL),
            "V6w_ipw_gbm": predict_v6(v6w, XL),
            "V7_naive_gbm_lock": who_lock(p2, an),
        }

    rows, extras = [], {}

    def record(level, XL, yL, an_true):
        an_who = who_anemia(XL)
        preds = predict_all(XL)
        if level == "L_asis_eq":
            preds["V1eq_residual_leak"] = proba14(v1eq, indicators(XL))
        for name, P in preds.items():
            m, ex = evaluate(P, yL, an_true, an_who)
            rows.append({"fold": fold, "rep": fold // 5, "variant": name, "level": level, **m})
            extras[(name, level)] = ex

    for level in LEVEL_NAMES:
        record(level, apply_level(Xte, level), yte, anemia_true[te])
    # «данные без утечки»: тестовые строки как есть + выравнивающее маскирование по r (3 реплики)
    rng_te = np.random.default_rng(seed + 7)
    R = 3
    Xeq = np.concatenate([mask_groups(Xte, yte, r, rng_te) for _ in range(R)])
    record("L_asis_eq", Xeq, np.tile(yte, R), np.tile(anemia_true[te], R))

    out = {"rows": rows, "extras": extras, "fit_time_s": fit_time,
           "v5_params": {"t": v5["t"], "w": v5["w"], "v": v5["v"], "oof_logloss": v5["oof_logloss"],
                         "oof_logloss_with_presence": v5["oof_logloss_with_presence"],
                         "oof_logloss_prior_only": v5["oof_logloss_prior_only"]}}
    if fold == 0:  # скорость предсказания (1 поток)
        rng = np.random.default_rng(0)
        out["speed"] = {"V5_fusion": time_predict(lambda Z: predict_v5(v5, Z), Xte, rng),
                        "V6_masked_gbm": time_predict(lambda Z: predict_v6(v6, Z), Xte, rng)}
    return out


# ----------------------------------------------------------------------------
# 8. Запуск и отчёт
# ----------------------------------------------------------------------------
METRICS = ["acc12", "f1_12", "f1_5", "f1_cause", "logloss12", "ece", "auroc_hidden"]
ORDER = ["V0_majority", "V1_leak_only", "V2_naive_gbm", "V7_naive_gbm_lock", "V3_cbc_gbm", "V4_rules",
         "V5_fusion", "V6_masked_gbm", "V6w_ipw_gbm", "V5L_fusion_leak", "V1eq_residual_leak"]
PAIRS = [("V7_naive_gbm_lock", "V5_fusion"), ("V5L_fusion_leak", "V7_naive_gbm_lock"),
         ("V5L_fusion_leak", "V5_fusion"), ("V5_fusion", "V2_naive_gbm"), ("V5_fusion", "V6_masked_gbm"),
         ("V5_fusion", "V6w_ipw_gbm")]


def load():
    df = pd.read_csv(DATA)
    prof = np.where(df["anemia_class"] == "mixed_deficiency", "mixed:" + df["deficiency_cause"], df["anemia_class"])
    y = np.array([P_IDX[p] for p in prof])
    X = to_matrix(df)
    an = df["anemia"].to_numpy()
    # проверки целостности разметки
    assert X.shape == (840, 37), X.shape
    assert (who_anemia(X) == an).all(), "anemia != правило ВОЗ"
    assert all(PROFILE_TO_CAUSE[p] == c for p, c in zip(prof, df["deficiency_cause"])), "cause != f(profile)"
    assert not np.isnan(X[:, [i for i in CBC_IDX if FEATS[i] != "RDW"]]).any()
    return X, y, an


NOTES = """## Оговорки и упрощения

- Гиперпараметры бустинга зафиксированы заранее (lr=0.08, 120 итераций, 8 листьев, min_samples_leaf=10, L2=1) и по тестовым фолдам не подбирались; для V6/V6w min_samples_leaf=3K, K=13 копий.
- V5: 5 квантильных бинов, сглаживание Лапласа alpha=1; веса групп — покоординатный спуск (2 прохода) по log-loss на внутренних OOF (таблицы для OOF тоже строятся вне фолда). Сверх ТЗ: в сетку весов добавлен 0 (группа отключается) и подбирается показатель степени приора t.
- V6: p_c(g) со сглаживанием Лапласа (иначе r=0 для групп, которых нет в малом профиле). Температура для V2/V6/V7 не подбиралась — log-loss и ECE даны как есть.
- L_asis_eq строится по истинному профилю тестовой строки (это диагностика, не режим работы), 3 случайные реплики; вместе с утечкой удаляются и реальные измерения. Остаточная утечка после выравнивания по группам — строка V1eq.
- Классы-12 и причины-11 прогноза — argmax по суммам вероятностей профилей; группа-5 прогноза = f(класс-12, анемия по ВОЗ): при анемии latent -> IDA, B12 без анемии -> B12DA, folate без анемии -> other_def, «нет дефицита» -> unexplained.
- V4: log-loss и ECE не определены (жёсткие метки). ECE по фолду (168 строк, 10 бинов) шумная и смещена вверх — см. ECE по объединённым предсказаниям.
- Фолды повторной кросс-валидации зависимы, поэтому для парных разностей даны среднее, ст.откл. и число выигранных фолдов, без p-значений.
- V5L, V6w, V1eq, парные разности и полнота по классам добавлены сверх ТЗ. sensitivity_v6.py — проверка чувствительности V6/V6w к ёмкости бустинга на 2 фолдах; в выбор конфигурации отчёта не входила.
- Данные синтетические (840 строк): все числа описывают генератор, а не клиническую точность.
"""


def md_table(header, rows):
    return "\n".join(["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
                     + ["| " + " | ".join(r) + " |" for r in rows])


def main():
    quick = "--quick" in sys.argv
    X, y, an = load()
    splits = list(RepeatedStratifiedKFold(n_splits=5, n_repeats=3, random_state=SEED).split(X, y))
    if quick:
        splits = splits[:1]
    t0 = time.time()
    res = Parallel(n_jobs=1 if quick else 2)(
        delayed(run_fold)(i, tr, te, X, y, an) for i, (tr, te) in enumerate(splits))
    wall = time.time() - t0
    df = pd.DataFrame([r for f in res for r in f["rows"]])
    agg = df.groupby(["variant", "level"])[METRICS].agg(["mean", "std"])
    suffix = "_quick" if quick else ""
    df.to_csv(OUT / f"fold_metrics{suffix}.csv", index=False)

    def cell(v, lv, m, nd=3):
        if (v, lv) not in agg.index:
            return "—"
        mu, sd = agg.loc[(v, lv), (m, "mean")], agg.loc[(v, lv), (m, "std")]
        if m in ("logloss12", "ece") and v == "V4_rules":
            return "н/п"
        return f"{mu:.{nd}f}±{0 if np.isnan(sd) else sd:.{nd}f}"

    levels_all = LEVEL_NAMES + ["L_asis_eq"]
    variants = [v for v in ORDER if v in df["variant"].unique()]
    md = [f"# RESULTS — движки классификации (5 фолдов x {len(splits) // 5 or 1} повт., n=840)", "",
          f"Время полного прогона: {wall:.0f} с; фолдов: {len(splits)}. Числа: среднее±ст.откл. по тестовым фолдам.",
          "L_asis_eq = строки как есть + выравнивающее маскирование тестовых строк (данные без утечки).", ""]
    titles = {"acc12": "Accuracy, 12 классов", "f1_12": "Macro-F1, 12 классов", "f1_5": "Macro-F1, 5 групп кейса",
              "f1_cause": "Macro-F1, 11 причин", "logloss12": "Log-loss, 12 классов",
              "ece": "ECE верхнего класса (10 равнонаполненных бинов, по фолду)",
              "auroc_hidden": "AUROC скрытого дефицита (строки без анемии, оценка после замка ВОЗ)"}
    for m in METRICS:
        md += [f"## {titles[m]}", "",
               md_table(["вариант"] + levels_all, [[v] + [cell(v, lv, m) for lv in levels_all] for v in variants]), ""]

    # сводная ECE по объединённым предсказаниям повтора (840 строк, меньше смещение малых бинов)
    def pooled_ece(v, lv):
        vals = []
        for rep in sorted({r["rep"] for f in res for r in f["rows"]}):
            fs = [f for i, f in enumerate(res) if i // 5 == rep]
            conf = np.concatenate([f["extras"][(v, lv)]["conf"] for f in fs])
            cor = np.concatenate([f["extras"][(v, lv)]["correct"] for f in fs])
            vals.append(ece_equal_mass(conf, cor))
        return float(np.mean(vals))

    # парные разности по фолдам (A − B): среднее±ст.откл. и число фолдов, где A лучше
    piv = df.pivot_table(index="fold", columns=["variant", "level"], values=["acc12", "f1_12", "f1_5", "f1_cause"])
    paired, prow = {}, []
    for a, b in PAIRS:
        for lv in ["L_iron", "L_std", "L_asis", "L_asis_eq"]:
            cells = []
            for m in ["acc12", "f1_12", "f1_5", "f1_cause"]:
                d = (piv[(m, a, lv)] - piv[(m, b, lv)]).to_numpy()
                sd = float(d.std(ddof=1)) if len(d) > 1 else 0.0
                paired[f"{a}-{b}|{lv}|{m}"] = {"mean": float(d.mean()), "sd": sd, "wins": int((d > 0).sum()), "n": len(d)}
                cells.append(f"{d.mean():+.3f}±{sd:.3f} ({int((d > 0).sum())}/{len(d)})")
            prow.append([f"{a} − {b}", lv] + cells)
    md += ["## Парные разности по фолдам (A − B; в скобках — в скольких фолдах A лучше)", "",
           md_table(["пара", "уровень", "acc12", "F1-12", "F1-5", "F1-причины"], prow), ""]

    cal_vars = ["V2_naive_gbm", "V7_naive_gbm_lock", "V3_cbc_gbm", "V5_fusion", "V6_masked_gbm", "V6w_ipw_gbm",
                "V5L_fusion_leak"]
    pooled = {v: {lv: pooled_ece(v, lv) for lv in levels_all} for v in cal_vars}
    md += ["## ECE по объединённым предсказаниям повтора (среднее по повторам)", "",
           md_table(["вариант"] + levels_all, [[v] + [f"{pooled[v][lv]:.3f}" for lv in levels_all] for v in cal_vars]), ""]

    # матрицы ошибок 5x5 (первый повтор) и полнота по 12 классам (все фолды)
    def cm_sum(v, lv, key, first_rep_only):
        fs = [f for i, f in enumerate(res) if (i // 5 == 0 or not first_rep_only)]
        return sum(f["extras"][(v, lv)][key] for f in fs)

    cms = {}
    short5 = ["healthy", "IDA", "B12DA", "unexpl", "other_def"]
    md += ["## Матрицы ошибок 5x5 (строки — истина, столбцы — предсказание; сумма по фолдам первого повтора)", ""]
    for v in ["V5_fusion", "V6_masked_gbm", "V6w_ipw_gbm", "V2_naive_gbm", "V7_naive_gbm_lock", "V5L_fusion_leak",
              "V4_rules"]:
        for lv in ["L_asis", "L_iron", "L_asis_eq"]:
            cm = cm_sum(v, lv, "cm5", True)
            cms[f"{v}|{lv}"] = cm.tolist()
            md += [f"**{v}, {lv}**", "",
                   md_table(["истина \\ прогноз"] + short5, [[short5[i]] + [str(int(x)) for x in cm[i]] for i in range(5)]), ""]
    recall = {}
    md += ["## Полнота (recall) по 12 классам, сумма по всем фолдам", ""]
    rec_cols = [(v, lv) for v in ["V2_naive_gbm", "V4_rules", "V5_fusion", "V6_masked_gbm", "V6w_ipw_gbm",
                                  "V5L_fusion_leak"]
                for lv in ["L_asis", "L_asis_eq"]] + [("V5_fusion", "L_iron"), ("V6_masked_gbm", "L_iron")]
    for v, lv in rec_cols:
        cm = cm_sum(v, lv, "cm12", False)
        recall[f"{v}|{lv}"] = (np.diag(cm) / cm.sum(axis=1)).tolist()
    md += [md_table(["класс"] + [f"{v.split('_')[0]} {lv[2:]}" for v, lv in rec_cols],
                    [[c] + [f"{recall[f'{v}|{lv}'][i]:.2f}" for v, lv in rec_cols] for i, c in enumerate(CLASSES12)]), ""]

    # параметры V5 и скорость
    v5p = [f["v5_params"] for f in res]
    w_med = np.median([p["w"] for p in v5p], axis=0)
    v5_summary = {"t_median": float(np.median([p["t"] for p in v5p])),
                  "w_median": dict(zip(GROUP_NAMES, w_med.tolist())),
                  "v_presence_median": float(np.median([p["v"] for p in v5p])),
                  "oof_logloss14_with_presence_mean": float(np.mean([p["oof_logloss_with_presence"] for p in v5p])),
                  "oof_logloss14_mean": float(np.mean([p["oof_logloss"] for p in v5p])),
                  "oof_logloss14_prior_only_mean": float(np.mean([p["oof_logloss_prior_only"] for p in v5p]))}
    speed = res[0].get("speed", {})
    md += ["## V5: подобранные параметры (медиана по фолдам)", "",
           f"температура приора t = {v5_summary['t_median']}; веса групп: "
           + ", ".join(f"{g}={w:g}" for g, w in v5_summary["w_median"].items())
           + f"; вес члена «сдан/не сдан» в V5L v = {v5_summary['v_presence_median']:g}", "",
           f"внутренний OOF log-loss (14 профилей): только приор ОАК {v5_summary['oof_logloss14_prior_only_mean']:.3f} -> "
           f"V5 {v5_summary['oof_logloss14_mean']:.3f} -> V5L {v5_summary['oof_logloss14_with_presence_mean']:.3f}", "",
           "## Скорость предсказания (1 поток CPU)", "",
           md_table(["вариант", "1 строка, мс", "10 000 строк, с"],
                    [[v, f"{s['ms_per_single_row']:.2f}", f"{s['s_per_10000_rows']:.2f}"] for v, s in speed.items()]), "",
           f"Среднее время обучения всех вариантов на фолд: {np.mean([f['fit_time_s'] for f in res]):.1f} с", ""]

    summary = {v: {lv: {m: {"mean": float(agg.loc[(v, lv), (m, "mean")]), "sd": float(agg.loc[(v, lv), (m, "std")])}
                        for m in METRICS} for lv in levels_all if (v, lv) in agg.index} for v in variants}
    md += [NOTES]
    (OUT / f"RESULTS{suffix}.md").write_text("\n".join(md), encoding="utf-8")
    (OUT / f"results{suffix}.json").write_text(json.dumps({
        "n_folds": len(splits), "wall_time_s": wall, "seed": SEED, "metrics": summary, "ece_pooled": pooled,
        "confusion5_first_repeat": cms, "groups5": GROUPS5, "recall12_all_folds": recall, "classes12": CLASSES12, "paired_differences": paired,
        "v5_params": v5_summary, "v5_params_per_fold": v5p, "speed": speed,
        "hgb_config": make_hgb(0).get_params(),
    }, ensure_ascii=False, indent=1, default=float), encoding="utf-8")
    print("\n".join(md))


if __name__ == "__main__":
    main()
