"""Честная оценка модели по файлу кейса: кросс-валидация 5 фолдов × 3 повтора (как в прототипе).

Фолды и seed те же, что в research/prototypes: RepeatedStratifiedKFold(5, 3, random_state=20261003),
стратификация по 14 профилям, seed фолда = 1000 + номер фолда. Всё обучаемое (бустинг, границы бинов,
таблицы, веса групп, степень приора) оценивается ТОЛЬКО по обучающей части фолда; по тестовой части
ничего не подбирается — она нужна лишь для чисел в отчёт.

Режимы:
  "clinical"  — слияние без утечки (то, что работает по умолчанию);
  "benchmark" — ансамбль «бустинг × слияние» + замок ВОЗ;
  "naive"     — бустинг на всех столбцах без замка (вариант V2 прототипа: «то, что сделает типичная команда»).
Уровни полноты: тестовые строки обрезаются до списка анализов уровня (constants.LEVELS); "extended" — как есть.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import numpy as np
from joblib import Parallel, delayed
from sklearn.metrics import accuracy_score, confusion_matrix, f1_score, roc_auc_score
from sklearn.model_selection import RepeatedStratifiedKFold

from .. import constants as C
from ..config import load_config, models_dir
from . import ensemble, fusion
from .case_model import METADATA_FILE, VERSION, decode_profiles, library_versions, write_json
from .features import LEVEL_NAMES, apply_level, who_anemia, who_lock
from .train_case import SEED, hidden_thresholds, load_case

N_SPLITS, N_REPEATS = 5, 3
MODES = ("clinical", "benchmark", "naive")
FOLD_METRICS = ["acc12", "f1_12", "acc5", "f1_5", "f1_cause", "f1_cause_proto", "acc_cause", "logloss12", "ece",
                "auroc_hidden", "acc5_anemic", "hidden_specificity", "hidden_sensitivity"]
# Точность по 12 классам в прототипе (research/prototypes/RESULTS_*.md) — для сверки переноса.
PROTOTYPE_ACC12 = {
    "clinical": {"cbc": 0.573, "cbc_iron_crp": 0.665, "standard": 0.766, "extended": 0.849},
    "benchmark": {"cbc": 0.504, "cbc_iron_crp": 0.638, "standard": 0.797, "extended": 0.919},
    "naive": {"cbc": 0.411, "cbc_iron_crp": 0.571, "standard": 0.752, "extended": 0.881},
}
# Пороги приёмки (допуск в них уже заложен).
GATES = {
    "level1_match": 840,
    "benchmark_extended_acc12": 0.90, "benchmark_extended_acc5": 0.93,
    "clinical_extended_acc12": 0.83, "clinical_cbc_iron_crp_acc12": 0.64, "clinical_cbc_acc12": 0.55,
    "clinical_cbc_auroc_hidden": 0.87, "clinical_ece_pooled_max": 0.06,
}


def group5_map(class_mapping: dict | None = None) -> np.ndarray:
    """Группа кейса (номер в GROUPS5) для каждого из 12 классов ПРИ АНЕМИИ — по config/class_mapping.yaml.

    Для классов, которых в конфиге нет («безанемичные»: после замка ВОЗ при анемии они невозможны),
    берётся значение прототипа constants.G5_IF_ANEMIA — оно нужно только режиму "naive" без замка.
    """
    cm = class_mapping if class_mapping is not None else load_config().class_mapping
    to_group = cm.get("class_to_group5_if_anemia", {})
    return np.array([C.GROUPS5.index(to_group[c]) if c in to_group else int(C.G5_IF_ANEMIA[i])
                     for i, c in enumerate(C.CLASSES12)])


def ece_equal_mass(conf, correct, n_bins: int = 10) -> float:
    """ECE верхнего класса: 10 равнонаполненных бинов по уверенности, |доля верных − средняя уверенность|."""
    order = np.argsort(conf, kind="stable")
    return float(sum(len(b) / len(conf) * abs(correct[b].mean() - conf[b].mean())
                     for b in np.array_split(order, n_bins) if len(b)))


def fold_metrics(P14, y, anemia_true, anemia_who, g5_if_anemia) -> tuple[dict, dict]:
    """Метрики одного тестового фолда. P14: (n, 14); y: истинный профиль; anemia_true: метка anemia файла;
    anemia_who: анемия по правилу ВОЗ для (возможно, обрезанной) строки."""
    P12, P11 = P14 @ C.A12, P14 @ C.A11
    y12 = (np.eye(C.NP_)[y] @ C.A12).argmax(axis=1)
    y11 = (np.eye(C.NP_)[y] @ C.A11).argmax(axis=1)
    dec = decode_profiles(P14)
    pred12 = dec["class12"]
    cause_product = dec["cause11"]            # как в продукте: причина лучшего профиля внутри лучшего класса
    cause_proto = P11.argmax(axis=1)          # как в прототипе: argmax по суммам вероятностей причин
    g_true = np.where(anemia_true == 1, g5_if_anemia[y12], 0)
    g_pred = np.where(anemia_who == 1, g5_if_anemia[pred12], 0)
    conf, correct = P12.max(axis=1), (pred12 == y12).astype(float)
    an = anemia_true == 1
    out = {
        "acc12": accuracy_score(y12, pred12),
        "f1_12": f1_score(y12, pred12, labels=range(12), average="macro", zero_division=0),
        "acc5": accuracy_score(g_true, g_pred),
        "f1_5": f1_score(g_true, g_pred, labels=range(5), average="macro", zero_division=0),
        "f1_cause": f1_score(y11, cause_product, labels=range(11), average="macro", zero_division=0),
        "f1_cause_proto": f1_score(y11, cause_proto, labels=range(11), average="macro", zero_division=0),
        "acc_cause": accuracy_score(y11, cause_product),
        "logloss12": float(-np.mean(np.log(np.clip(P12[np.arange(len(y)), y12], 1e-15, 1.0)))),
        "ece": ece_equal_mass(conf, correct),
        # 309 строк без анемии получают группу healthy прямо из правила по гемоглобину, поэтому отдельно
        # считается точность по 5 группам только на строках с анемией.
        "acc5_anemic": accuracy_score(g_true[an], g_pred[an]) if an.any() else float("nan"),
    }
    # Скрытый дефицит: строки без анемии, оценка = 1 − P(«анемии и дефицитов нет») после замка ВОЗ.
    na = anemia_true == 0
    score = 1.0 - who_lock(P14, anemia_who)[na, 0]
    target = (y[na] != 0).astype(int)
    out["auroc_hidden"] = roc_auc_score(target, score) if 0 < target.sum() < len(target) else float("nan")
    extra = {"conf": conf, "correct": correct,
             "cm5": confusion_matrix(g_true, g_pred, labels=range(5)),
             "cm12": confusion_matrix(y12, pred12, labels=range(12))}
    return {k: float(v) for k, v in out.items()}, extra


def run_fold(fold: int, tr, te, X, y, anemia_true, g5_if_anemia) -> dict:
    """Один внешний фолд: обучение трёх вариантов на обучающей части, метрики на тестовой по 4 уровням полноты."""
    seed = 1000 + fold
    Xtr, ytr, Xte, yte = X[tr], y[tr], X[te], y[te]
    t0 = time.time()
    fus = fusion.fit_fusion(Xtr, ytr, seed, keep_oof=True)  # клинический режим
    # Порог флага скрытого дефицита — как при обучении итоговой модели: по внутренним OOF обучающей части.
    tau, _ = hidden_thresholds(fus.pop("oof")["posterior"], ytr, anemia_true[tr])
    bench = ensemble.fit_bench_gbm(Xtr, ytr, seed)          # бустинг режима бенчмарка
    naive = fusion.make_hgb(seed).fit(Xtr, ytr)             # наивный бустинг на всех столбцах (V2 прототипа)
    fit_s = time.time() - t0
    metrics, extras = {}, {}
    for lv in LEVEL_NAMES:
        XL = apply_level(Xte, lv)
        aw = who_anemia(XL)
        P_clin = fusion.predict_fusion(fus, XL)
        preds = {
            "clinical": P_clin,
            "benchmark": ensemble.combine(ensemble.gbm_proba_locked(bench, XL), P_clin, aw),
            "naive": fusion.proba14(naive, XL),
        }
        for mode, P in preds.items():
            metrics[(mode, lv)], extras[(mode, lv)] = fold_metrics(P, yte, anemia_true[te], aw, g5_if_anemia)
            metrics[(mode, lv)].update({"hidden_specificity": float("nan"), "hidden_sensitivity": float("nan")})
        # Флаг скрытого дефицита на тестовых строках без анемии (только клинический режим): честная проверка того,
        # что порог, выбранный на обучающей части под специфичность 0,90, держит её на новых строках.
        na = anemia_true[te] == 0
        flag = (1.0 - P_clin[na, 0]) >= tau[lv]
        healthy = yte[na] == 0
        if healthy.any() and (~healthy).any():
            metrics[("clinical", lv)]["hidden_specificity"] = float((~flag[healthy]).mean())
            metrics[("clinical", lv)]["hidden_sensitivity"] = float(flag[~healthy].mean())
    return {"fold": fold, "rep": fold // N_SPLITS, "metrics": metrics, "extras": extras, "fit_seconds": fit_s,
            "fusion": {"t": fus["t"], "w": fus["w"], "oof_logloss": fus["oof_logloss"]}}


def _n_jobs() -> int:
    """Число процессов: по умолчанию 2 (как в прототипе), не больше числа ядер; переопределяется DL_JOBS."""
    try:
        want = int(os.environ.get("DL_JOBS", "2"))
    except ValueError:
        want = 2
    return max(1, min(want, os.cpu_count() or 1))


def _ensure_children_can_import() -> None:
    """Рабочие процессы joblib импортируют этот модуль заново: каталог src должен быть в их PYTHONPATH."""
    src = str(Path(__file__).resolve().parents[2])
    paths = [p for p in os.environ.get("PYTHONPATH", "").split(os.pathsep) if p]
    if src not in paths:
        os.environ["PYTHONPATH"] = os.pathsep.join([src] + paths)


def _mean_sd(values) -> dict:
    v = np.asarray(values, dtype=float)
    v = v[~np.isnan(v)]
    if not len(v):
        return {"mean": None, "sd": None}
    return {"mean": float(v.mean()), "sd": float(v.std(ddof=1)) if len(v) > 1 else 0.0}


def evaluate(case_path: str, out_json: str | None = None) -> dict:
    """Кросс-валидация 5×3 на файле кейса. Возвращает {"summary", "by_mode_level", "confusion", "config"};
    если задан out_json — пишет результат туда и краткую сводку в models/case-v1/metadata.json (ключ cv_metrics)."""
    t0 = time.time()
    data = load_case(case_path)
    X, y, anemia = data["X"], data["y"], data["anemia"]
    g5 = group5_map()
    splits = list(RepeatedStratifiedKFold(n_splits=N_SPLITS, n_repeats=N_REPEATS, random_state=SEED).split(X, y))
    jobs = _n_jobs()
    if jobs > 1:
        _ensure_children_can_import()
    res = Parallel(n_jobs=jobs)(delayed(run_fold)(k, tr, te, X, y, anemia, g5) for k, (tr, te) in enumerate(splits))
    res = sorted(res, key=lambda r: r["fold"])

    # Среднее и стандартное отклонение по 15 тестовым фолдам.
    by: dict[str, dict[str, dict]] = {m: {} for m in MODES}
    for mode in MODES:
        for lv in LEVEL_NAMES:
            cell = {m: _mean_sd([r["metrics"][(mode, lv)][m] for r in res]) for m in FOLD_METRICS}
            # ECE по объединённым предсказаниям повтора (840 строк): меньше смещение малых бинов, чем по фолду.
            pooled = []
            for rep in range(N_REPEATS):
                fs = [r for r in res if r["rep"] == rep]
                pooled.append(ece_equal_mass(np.concatenate([r["extras"][(mode, lv)]["conf"] for r in fs]),
                                             np.concatenate([r["extras"][(mode, lv)]["correct"] for r in fs])))
            cell["ece_pooled"] = _mean_sd(pooled)
            by[mode][lv] = cell

    # Матрицы ошибок: сумма по фолдам первого повтора (каждая из 840 строк встречается ровно один раз).
    confusion = {}
    for mode in ("clinical", "benchmark"):
        first = [r for r in res if r["rep"] == 0]
        confusion[mode] = {
            "level": "extended", "repeat": 0, "rows_true_columns_predicted": True,
            "groups5": {"labels": C.GROUPS5,
                        "matrix": sum(r["extras"][(mode, "extended")]["cm5"] for r in first).tolist()},
            "classes12": {"labels": C.CLASSES12,
                          "matrix": sum(r["extras"][(mode, "extended")]["cm12"] for r in first).tolist()},
        }

    mean = lambda mode, lv, m: by[mode][lv][m]["mean"]  # noqa: E731
    acc12 = {mode: {lv: round(mean(mode, lv, "acc12"), 4) for lv in LEVEL_NAMES} for mode in MODES}
    diffs = {f"{mode}/{lv}": round(mean(mode, lv, "acc12") - ref, 4)
             for mode, levels in PROTOTYPE_ACC12.items() for lv, ref in levels.items()}
    ece_pooled_clin = {lv: round(mean("clinical", lv, "ece_pooled"), 4) for lv in LEVEL_NAMES}
    gates = {
        "level1_match": data["level1_match"] == GATES["level1_match"],
        "benchmark_extended_acc12": mean("benchmark", "extended", "acc12") >= GATES["benchmark_extended_acc12"],
        "benchmark_extended_acc5": mean("benchmark", "extended", "acc5") >= GATES["benchmark_extended_acc5"],
        "clinical_extended_acc12": mean("clinical", "extended", "acc12") >= GATES["clinical_extended_acc12"],
        "clinical_cbc_iron_crp_acc12": mean("clinical", "cbc_iron_crp", "acc12") >= GATES["clinical_cbc_iron_crp_acc12"],
        "clinical_cbc_acc12": mean("clinical", "cbc", "acc12") >= GATES["clinical_cbc_acc12"],
        "clinical_cbc_auroc_hidden": mean("clinical", "cbc", "auroc_hidden") >= GATES["clinical_cbc_auroc_hidden"],
        "clinical_ece_pooled_max": max(ece_pooled_clin.values()) <= GATES["clinical_ece_pooled_max"],
    }
    short = ["acc12", "f1_12", "acc5", "f1_5", "f1_cause", "f1_cause_proto", "logloss12", "ece", "ece_pooled",
             "auroc_hidden", "acc5_anemic"]
    seconds = round(time.time() - t0, 1)
    summary = {
        "n_rows": data["n"], "level1_match": data["level1_match"],
        "cv": f"{N_SPLITS} фолдов × {N_REPEATS} повтора, стратификация по 14 профилям, seed {SEED}",
        "acc12": acc12,
        "benchmark_extended": {m: round(mean("benchmark", "extended", m), 4) for m in short},
        "clinical_extended": {m: round(mean("clinical", "extended", m), 4) for m in short},
        "clinical_cbc": {m: round(mean("clinical", "cbc", m), 4) for m in short},
        "acc5_anemic": {"benchmark_extended": round(mean("benchmark", "extended", "acc5_anemic"), 4),
                        "clinical_extended": round(mean("clinical", "extended", "acc5_anemic"), 4),
                        "clinical_cbc": round(mean("clinical", "cbc", "acc5_anemic"), 4)},
        "auroc_hidden_clinical": {lv: round(mean("clinical", lv, "auroc_hidden"), 4) for lv in LEVEL_NAMES},
        "ece_pooled_clinical": ece_pooled_clin,
        "hidden_flag_clinical": {lv: {"specificity": round(mean("clinical", lv, "hidden_specificity"), 4),
                                      "sensitivity": round(mean("clinical", lv, "hidden_sensitivity"), 4)}
                                 for lv in LEVEL_NAMES},
        "prototype_check": {"reference_acc12": PROTOTYPE_ACC12, "diff_acc12": diffs,
                            "max_abs_diff": round(max(abs(v) for v in diffs.values()), 4)},
        "gates": {"thresholds": GATES, "passed": gates, "all_passed": bool(all(gates.values()))},
        "seconds": seconds,
    }
    result = {
        "summary": summary,
        "by_mode_level": by,
        "confusion": confusion,
        "config": {
            "model": VERSION, "data_sha256": data["sha256"], "rows": data["n"],
            "n_splits": N_SPLITS, "n_repeats": N_REPEATS, "random_state": SEED, "fold_seed": "1000 + номер фолда",
            "stratify": "14 профилей", "levels": {lv: C.LEVELS[lv] for lv in LEVEL_NAMES},
            "modes": {"clinical": "слияние без утечки", "benchmark": "ансамбль бустинг × слияние + замок ВОЗ",
                      "naive": "бустинг на всех столбцах без замка (V2 прототипа)"},
            "prior_and_naive_gbm": fusion.PRIOR_GBM_PARAMS, "benchmark_gbm": ensemble.BENCH_GBM_PARAMS,
            "fusion": {"n_bins": fusion.N_BINS, "alpha": fusion.ALPHA, "w_grid": fusion.W_GRID,
                       "t_grid": fusion.T_GRID, "inner_folds": 5, "passes": 2},
            "fusion_params_median_over_folds": {
                "prior_power_t": float(np.median([r["fusion"]["t"] for r in res])),
                "group_weights": dict(zip(C.GROUP_NAMES,
                                          np.median([r["fusion"]["w"] for r in res], axis=0).tolist()))},
            "group5_if_anemia": {c: C.GROUPS5[g5[i]] for i, c in enumerate(C.CLASSES12)},
            "metric_notes": {
                "sd": "стандартное отклонение по 15 тестовым фолдам (ddof=1); фолды повторов зависимы",
                "f1_cause": "причина = причина лучшего профиля внутри лучшего класса (как в продукте)",
                "f1_cause_proto": "причина = argmax по суммам вероятностей причин (как в прототипе)",
                "ece": "по фолду, 10 равнонаполненных бинов; ece_pooled — по объединённым предсказаниям повтора",
                "auroc_hidden": "строки без анемии: 1 − P(«анемии и дефицитов нет») против наличия дефицита",
                "acc5_anemic": "точность по 5 группам только на строках с анемией",
                "hidden_specificity / hidden_sensitivity": "флаг скрытого дефицита на тестовых строках без анемии; "
                                                           "порог выбран на обучающей части фолда (цель 0,90)",
            },
            "versions": library_versions(), "n_jobs": jobs,
            "mean_fit_seconds_per_fold": round(float(np.mean([r["fit_seconds"] for r in res])), 1),
        },
    }
    if out_json:
        Path(out_json).parent.mkdir(parents=True, exist_ok=True)
        write_json(out_json, result)
        _update_metadata(summary, data["sha256"], out_json)
    return result


def _update_metadata(summary: dict, data_sha256: str, out_json: str) -> None:
    """Кладёт сводку последнего полного прогона в metadata.json модели, если модель обучена
    на том же файле данных. Файлы модели и их контрольные суммы не меняются."""
    path = models_dir() / VERSION / METADATA_FILE
    if not path.exists():
        return
    try:
        meta = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    if meta.get("data", {}).get("sha256") != data_sha256:
        return
    keep = ["n_rows", "level1_match", "cv", "acc12", "benchmark_extended", "clinical_extended", "clinical_cbc",
            "acc5_anemic", "auroc_hidden_clinical", "ece_pooled_clinical", "hidden_flag_clinical", "gates"]
    meta["cv_metrics"] = {**{k: summary[k] for k in keep}, "source": Path(out_json).name,
                          "note": "кросс-валидация 5×3; итоговая модель обучена на всех строках"}
    write_json(path, meta)
