"""Слияние без утечки — клинический режим уровня 2 (перенос fit_v5 / predict_v5 из research/prototypes).

Идея. Вероятности 14 профилей считаются как
    апостериор ∝ приор(ОАК, возраст, пол)^t × ∏_группы ( ∏_сданные анализы группы P(бин | профиль) )^w_группы,
после чего применяется «замок ВОЗ» (профили, несовместимые с анемией по гемоглобину, обнуляются).

  * приор — бустинг (HistGradientBoosting) ТОЛЬКО по ОАК, возрасту и полу: они заполнены у всех;
  * каждый СДАННЫЙ анализ вне ОАК — 5 квантильных бинов по обучающей части и таблица P(бин | профиль)
    со сглаживанием Лапласа; границы бинов выучены по данным набора, это не пороги ВОЗ;
  * НЕСДАННЫЙ анализ не участвует вообще: множителя «сдан / не сдан» в модели нет. Поэтому ответ зависит
    только от сданных значений, а не от того, какие анализы врач решил назначить (в файле кейса сам факт
    назначения выдаёт класс — это утечка, см. docs/model_card.md);
  * веса групп w и степень приора t подбираются по log-loss на внутренних фолдах обучающей части
    (анализы внутри группы коррелируют, поэтому вес задаётся на группу и обычно меньше 1).

Отличие от прототипа одно: вариант V5L (член «сдан / не сдан») сюда не перенесён. На подбор t и w он не влиял.
"""
from __future__ import annotations

import contextlib

import numpy as np
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.model_selection import StratifiedKFold

from ..constants import ALLOW, CBC_IDX, GROUP_NAMES, GROUPS, LAB_GROUP, LAB_IDX, LABS, NP_
from .features import LEVEL_NAMES, apply_level, who_anemia

NG = len(GROUP_NAMES)
W_GRID = [1.0, 0.7, 0.5, 0.35, 0.25, 0.0]  # сетка весов групп; 0 — группа отключена
T_GRID = [1.0, 0.7, 0.5, 0.35, 0.25]       # сетка показателя степени приора по ОАК
N_BINS = 5
ALPHA = 1.0                                 # сглаживание Лапласа в таблицах P(бин | профиль)
PRIOR_GBM_PARAMS = dict(learning_rate=0.08, max_iter=120, max_leaf_nodes=8, min_samples_leaf=10,
                        l2_regularization=1.0, early_stopping=False)


def make_hgb(seed: int, min_samples_leaf: int = 10) -> HistGradientBoostingClassifier:
    """Заранее зафиксированная конфигурация бустинга прототипа (по тестовым фолдам не подбиралась)."""
    params = dict(PRIOR_GBM_PARAMS, min_samples_leaf=min_samples_leaf)
    return HistGradientBoostingClassifier(random_state=seed, **params)


_OMP_CONTROLLER = None


def single_thread():
    """Контекст «предсказание бустинга в один поток OpenMP».

    Бустинг scikit-learn открывает параллельную секцию на каждое дерево (у нас их тысячи). Когда ядра заняты
    другими процессами (несколько реплик сервиса, соседние задачи), потоки OpenMP простаивают в ожидании друг
    друга, и расшифровка 10 000 строк замедляется с 2 до 30–40 секунд. В один поток время стабильное,
    а результат тот же: суммирование по деревьям от числа потоков не зависит.
    """
    global _OMP_CONTROLLER
    try:
        if _OMP_CONTROLLER is None:
            from threadpoolctl import ThreadpoolController
            _OMP_CONTROLLER = ThreadpoolController()
        return _OMP_CONTROLLER.limit(limits=1, user_api="openmp")
    except Exception:  # noqa: BLE001 — без threadpoolctl просто считаем как есть
        return contextlib.nullcontext()


def proba14(model, Xf) -> np.ndarray:
    """predict_proba, разложенный по всем 14 профилям (если какого-то профиля не было в обучении — 0)."""
    P = np.zeros((len(Xf), NP_))
    with single_thread():
        P[:, model.classes_] = model.predict_proba(Xf)
    return P


def fit_tables(X, y, n_bins: int = N_BINS, alpha: float = ALPHA) -> list[tuple[np.ndarray, np.ndarray]]:
    """Таблицы правдоподобия для 26 анализов вне ОАК.

    Для каждого анализа: квантильные границы бинов по ИЗМЕРЕННЫМ значениям обучающей части и
    log P(бин | профиль) со сглаживанием Лапласа. Несданный анализ в таблицы не попадает вовсе.
    Возвращает список (границы, матрица (14, число бинов)) в порядке constants.LABS.
    """
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


def group_loglik(tabs, X) -> np.ndarray:
    """(n, число групп, 14): сумма log P(бин | профиль) по СДАННЫМ анализам каждой группы. Пропуск даёт 0."""
    LL = np.zeros((len(X), NG, NP_))
    for k, j in enumerate(LAB_IDX):
        edges, logp = tabs[k]
        x = X[:, j]
        m = ~np.isnan(x)
        if m.any():
            LL[m, LAB_GROUP[k], :] += logp[:, np.searchsorted(edges, x[m], side="right")].T
    return LL


def posterior(prior, LL, t, w, anemia) -> np.ndarray:
    """апостериор ∝ приор^t × ∏_g (правдоподобие группы g)^w_g, затем «замок ВОЗ». Считается в логарифмах."""
    logp = t * np.log(np.clip(prior, 1e-12, 1.0)) + np.tensordot(LL, np.asarray(w, dtype=float), axes=([1], [0]))
    logp = np.where(ALLOW[np.asarray(anemia, dtype=int)], logp, -np.inf)
    logp -= logp.max(axis=1, keepdims=True)
    P = np.exp(logp)
    return P / P.sum(axis=1, keepdims=True)


def fit_fusion(X, y, seed: int = 0, n_bins: int = N_BINS, alpha: float = ALPHA, inner_folds: int = 5,
               passes: int = 2, keep_oof: bool = False) -> dict:
    """Обучение слияния. X: (n, 37) в порядке FEATS, y: номер профиля 0..13. Возвращает словарь-модель.

    Шаги (как fit_v5 прототипа):
      1. внутренние фолды обучающей части: приор по ОАК и таблицы правдоподобия строятся без проверочной
         части фолда — получаются «честные» (OOF) приор и правдоподобия для каждой строки;
      2. покоординатный подбор степени приора t и весов групп w по log-loss на этих OOF-предсказаниях
         (2 прохода по сеткам T_GRID / W_GRID);
      3. итоговые приор и таблицы — на всей обучающей части.
    keep_oof=True дополнительно сохраняет OOF-вероятности на четырёх уровнях полноты (строка обрезается до уровня)
    и сами внутренние фолды: по ним считаются пороги скрытого дефицита (train_case.py).
    """
    X = np.asarray(X, dtype=float)
    y = np.asarray(y, dtype=int)
    n = len(X)
    anemia = who_anemia(X)

    # 1. Внутренние OOF: приор по ОАК и правдоподобия (таблицы тоже строятся вне проверочной части).
    prior_oof = np.zeros((n, NP_))
    LL_oof = np.zeros((n, NG, NP_))
    LL_levels = {lv: np.zeros((n, NG, NP_)) for lv in LEVEL_NAMES if lv != "extended"} if keep_oof else {}
    splits = list(StratifiedKFold(inner_folds, shuffle=True, random_state=seed).split(X, y))
    for tr, va in splits:
        m = make_hgb(seed).fit(X[tr][:, CBC_IDX], y[tr])
        prior_oof[va] = proba14(m, X[va][:, CBC_IDX])
        tabs = fit_tables(X[tr], y[tr], n_bins, alpha)
        LL_oof[va] = group_loglik(tabs, X[va])
        for lv in LL_levels:
            LL_levels[lv][va] = group_loglik(tabs, apply_level(X[va], lv))

    # 2. Покоординатный подбор степени приора и весов групп по log-loss на OOF.
    def loss(params) -> float:
        P = posterior(prior_oof, LL_oof, params[0], params[1:], anemia)
        return float(-np.mean(np.log(np.clip(P[np.arange(n), y], 1e-12, 1.0))))

    def nearest(v: float) -> float:
        return min(W_GRID, key=lambda g: abs(g - v))

    # Старт: вес группы ≈ 1 / √(число анализов в группе) — поправка на корреляцию анализов внутри группы.
    params = np.array([1.0] + [nearest(1 / np.sqrt(len(GROUPS[g]))) for g in GROUP_NAMES])
    best = loss(params)
    for _ in range(passes):
        for i in range(len(params)):
            for v in (T_GRID if i == 0 else W_GRID):
                trial = params.copy()
                trial[i] = v
                cur = loss(trial)
                if cur < best - 1e-9:
                    best, params = cur, trial

    # 3. Итоговые модель приора и таблицы — на всей обучающей части.
    model = {
        "prior_model": make_hgb(seed).fit(X[:, CBC_IDX], y),
        "tables": fit_tables(X, y, n_bins, alpha),
        "t": float(params[0]), "w": [float(v) for v in params[1:]],
        "n_bins": int(n_bins), "alpha": float(alpha), "seed": int(seed),
        "oof_logloss": float(best),
        "oof_logloss_prior_only": float(-np.mean(np.log(np.clip(prior_oof[np.arange(n), y], 1e-12, 1)))),
    }
    if keep_oof:
        LL_levels["extended"] = LL_oof
        model["oof"] = {
            "splits": splits,
            "posterior": {lv: posterior(prior_oof, LL_levels[lv], model["t"], model["w"], anemia)
                          for lv in LEVEL_NAMES},
        }
    return model


def prior_proba(model: dict, X) -> np.ndarray:
    """Приор по ОАК, возрасту и полу: (n, 14), без степени t и без замка ВОЗ."""
    X = np.asarray(X, dtype=float)
    return proba14(model["prior_model"], X[:, CBC_IDX])


def predict_fusion(model: dict, X) -> np.ndarray:
    """Вероятности 14 профилей в клиническом режиме, (n, 14), после «замка ВОЗ».

    Пропуск анализа не несёт информации: строка без анализов вне ОАК получает приор^t с замком ВОЗ.
    """
    X = np.asarray(X, dtype=float)
    return posterior(prior_proba(model, X), group_loglik(model["tables"], X), model["t"], model["w"], who_anemia(X))


def contributions(model: dict, x_row, top_profile: int, runner_profile: int) -> list[dict]:
    """Вклад каждого СДАННОГО анализа вне ОАК в выбор профиля top против runner.

    delta = вес группы × (log P(бин | top) − log P(бин | runner)), натуральный логарифм.
    delta > 0 — значение анализа говорит «за» top_profile, < 0 — в пользу runner_profile.
    Список отсортирован по убыванию |delta|. Анализы группы с нулевым весом получают delta = 0.
    """
    x = np.asarray(x_row, dtype=float).ravel()
    out = []
    for k, j in enumerate(LAB_IDX):
        if np.isnan(x[j]):
            continue
        edges, logp = model["tables"][k]
        b = int(np.searchsorted(edges, x[j], side="right"))
        delta = model["w"][LAB_GROUP[k]] * (logp[top_profile, b] - logp[runner_profile, b])
        out.append({"analyte": LABS[k], "delta": float(delta)})
    out.sort(key=lambda d: -abs(d["delta"]))
    return out


def entropy_bits(p, axis: int = -1) -> np.ndarray:
    """Энтропия Шеннона в битах; 0 · log 0 = 0."""
    p = np.asarray(p, dtype=float)
    with np.errstate(divide="ignore", invalid="ignore"):
        return -np.sum(np.where(p > 0, p * np.log2(p), 0.0), axis=axis)


def information_gain(model: dict, x_row) -> dict[str, float]:
    """Ожидаемое уменьшение энтропии по 14 профилям (в битах) для каждого НЕсданного анализа вне ОАК.

    Считается в закрытом виде по таблицам слияния, бустинг не пересчитывается:
      p        — текущий апостериор по профилям (после замка ВОЗ);
      P(b)     = Σ_c p_c · P(b | c)                 — с какой вероятностью анализ попадёт в бин b;
      p(· | b) ∝ p_c · P(b | c)^w                   — апостериор, если анализ попал в бин b (w — вес группы);
      IG       = H(p) − Σ_b P(b) · H(p(· | b)).
    При w = 1 это взаимная информация (она неотрицательна). При w < 1 обновление слабее байесовского, поэтому
    величина может оказаться чуть ниже нуля — такие значения обрезаются до 0 («анализ ничего не добавит»).
    Анализ из группы с нулевым весом всегда даёт 0. Сданные анализы в словарь не попадают.
    """
    x = np.asarray(x_row, dtype=float).reshape(1, -1)
    p = predict_fusion(model, x)[0]
    h0 = float(entropy_bits(p))
    out: dict[str, float] = {}
    for k, j in enumerate(LAB_IDX):
        if not np.isnan(x[0, j]):
            continue
        _edges, logp = model["tables"][k]                  # (14, число бинов)
        w = model["w"][LAB_GROUP[k]]
        if w == 0:                                         # группа отключена: анализ ответа не меняет
            out[LABS[k]] = 0.0
            continue
        p_bin = p @ np.exp(logp)                           # P(b)
        post = p[:, None] * np.exp(w * logp)               # ненормированный апостериор для каждого бина
        z = post.sum(axis=0, keepdims=True)
        post = post / np.where(z > 0, z, 1.0)
        gain = h0 - float(p_bin @ entropy_bits(post, axis=0))
        out[LABS[k]] = gain if gain > 1e-12 else 0.0       # отрицательные значения и шум округления -> 0
    return out
