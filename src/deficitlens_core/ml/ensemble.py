"""Режим бенчмарка: ансамбль «бустинг × слияние» + замок ВОЗ (вариант ENS_V7b+V5(noleak) прототипа).

  1. бустинг на ВСЕХ 37 признаках (пропуски — нативно, как NaN) и 6 производных (features.feats_plus),
     затем замок ВОЗ;
  2. слияние без утечки (ml/fusion.py);
  3. геометрическое среднее двух наборов вероятностей и снова замок ВОЗ.

Важно для model card: бустинг видит, КАКИЕ анализы сданы, а в файле кейса это выдаёт класс. Поэтому режим
бенчмарка предназначен только для файлов в схеме кейса (ввод «как выдан набор»); для разбора одного анализа
и для неполных панелей используется клинический режим.
Гиперпараметры бустинга взяты из прототипа (V7b) и по тестовым фолдам не подбирались.
"""
from __future__ import annotations

import numpy as np
from sklearn.ensemble import HistGradientBoostingClassifier

from .features import feats_plus, who_anemia, who_lock
from .fusion import predict_fusion, proba14

BENCH_GBM_PARAMS = dict(learning_rate=0.05, max_iter=300, max_leaf_nodes=8, min_samples_leaf=8,
                        l2_regularization=1.0, early_stopping=False)


def make_bench_gbm(seed: int) -> HistGradientBoostingClassifier:
    """Бустинг варианта V7b: lr 0,05; 300 итераций; 8 листьев; минимум 8 строк в листе; L2 = 1."""
    return HistGradientBoostingClassifier(random_state=seed, **BENCH_GBM_PARAMS)


def geo(*Ps, w=None) -> np.ndarray:
    """Взвешенное геометрическое среднее вероятностей (по умолчанию веса равные), перенормированное по строке."""
    w = w or [1 / len(Ps)] * len(Ps)
    L = sum(wi * np.log(np.clip(P, 1e-9, 1)) for wi, P in zip(w, Ps))
    L = L - L.max(axis=1, keepdims=True)
    P = np.exp(L)
    return P / P.sum(axis=1, keepdims=True)


def fit_bench_gbm(X, y, seed: int = 0) -> HistGradientBoostingClassifier:
    """Бустинг на всех признаках + производных. X: (n, 37), y: номер профиля 0..13."""
    return make_bench_gbm(seed).fit(feats_plus(np.asarray(X, dtype=float)), np.asarray(y, dtype=int))


def gbm_proba_locked(gbm, X) -> np.ndarray:
    """Вероятности бустинга по 14 профилям после замка ВОЗ (вариант V7b_gbm_tuned_feats_lock)."""
    X = np.asarray(X, dtype=float)
    return who_lock(proba14(gbm, feats_plus(X)), who_anemia(X))


def combine(P_gbm_locked, P_fusion, anemia) -> np.ndarray:
    """Геометрическое среднее вероятностей бустинга (после замка) и слияния, затем замок ВОЗ."""
    return who_lock(geo(P_gbm_locked, P_fusion), anemia)


def predict_ensemble(model: dict, X, P_fusion=None) -> np.ndarray:
    """Вероятности 14 профилей в режиме бенчмарка, (n, 14), после замка ВОЗ.

    P_fusion — уже посчитанные вероятности слияния для тех же строк (чтобы не считать их дважды).
    """
    X = np.asarray(X, dtype=float)
    if P_fusion is None:
        P_fusion = predict_fusion(model["fusion"], X)
    return combine(gbm_proba_locked(model["gbm"], X), P_fusion, who_anemia(X))
