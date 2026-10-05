"""Временная проверка модели скрининга скрытого дефицита железа на NHANES.

Что считается (всё — перенос research/prototypes/nhanes_screen.py, числа должны совпасть с RESULTS_nhanes.md):
  * обучение на циклах 1999–2016, тест на 2017–2018 и 2021–2023; люди 18+, не беременные, полный ОАК,
    без анемии по ВОЗ (Hb ≥ 120 г/л у женщин, ≥ 130 г/л у мужчин), измерен ферритин (мкг/л);
  * цели: ферритин < 15 мкг/л (ВОЗ 2020) и < 30 мкг/л (порог практики);
  * по группам ALL / F18-49 / F50+ / M: AUC и 95 % ДИ (бутстреп), PR-AUC, Brier, средняя предсказанная
    и наблюдаемая доля — для стандартизованных (z) и сырых признаков, полного ОАК и одного гемоглобина;
  * таблицы направлений на ферритин на 1000 человек с весами обследования (mec_weight) и ценой ферритина в рублях;
  * сравнение с простым правилом по индексам (MCV < 80 фл, или MCH < 27 пг, или RDW > 14,5 %)
    при том же числе направлений; разница чувствительности и её 95 % ДИ (парный бутстреп);
  * сдвиг анализатора: медиана RDW по циклам и калибровка сырых признаков против стандартизованных;
  * описание когорты: размеры, распространённость, доля людей с дефицитом, у которых нет анемии.
Тестовые циклы используются только для отчёта: ничего по ним не подбирается.
В результат и в файл попадают только сводные числа — ни одной строки NHANES.
"""
from __future__ import annotations

import csv
import json
import platform
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from ..config import REPO_ROOT, config_dir
from .screen import (CALIBRATION, GROUPS, HGB_PARAMS, TARGETS, TEST_CYCLES, VERSION, WHO_HB_G_L, reference_by_sex,
                     feature_matrix, feature_names, fit_model, load_cohort, load_lab_references, referral_table,
                     sha256_file, standardize, wmean, DEFAULT_REFERENCE, FEATURE_LABS)

# (набор признаков, представление ОАК, имя в прототипе) — порядок как в прототипе.
FEATURE_VARIANTS = [("hb_only", "raw", "L0_raw"), ("full_cbc", "raw", "L1_raw"),
                    ("hb_only", "z", "L0_z"), ("full_cbc", "z", "L1_z")]
METRIC_GROUPS = ["ALL", *GROUPS]
MIN_CASES = 15              # при меньшем числе случаев метрики группы не считаются (как в прототипе)
REFER_FRACS = (0.1, 0.2, 0.3, 0.4, 0.5)
AUC_BOOTSTRAP = dict(B=300, seed=0)
RULE_BOOTSTRAP = dict(B=500, seed=1)
# Простое правило по эритроцитарным индексам (сырые значения): MCV < 80 фл, или MCH < 27 пг, или RDW > 14,5 %.
RULE = {"MCV_below_fl": 80.0, "MCH_below_pg": 27.0, "RDW_above_pct": 14.5}
PRICE_FERRITIN_FALLBACK = 845  # ₽; используется, только если config/prices.yaml недоступен
# Диапазоны предсказанной вероятности для таблицы калибровки «предсказано против наблюдаемого».
PROB_BINS = (0.0, 0.02, 0.05, 0.10, 0.20, 0.30, 0.50, 1.0)
# Ворота плана (§14): женщины 18–49, тест 2017–2023, полный ОАК со стандартизацией.
GATES = {"auc_t15_min": 0.80, "auc_t30_min": 0.70, "calibration_gap_max": 0.03}


def ferritin_price() -> int:
    """Цена ферритина, ₽: config/prices.yaml (Москва, 03.10.2026, медиана по лабораториям, без взятия крови)."""
    try:
        import yaml

        with open(config_dir() / "prices.yaml", encoding="utf-8") as f:
            return int(yaml.safe_load(f)["prices"]["ferritin"]["price"])
    except Exception:
        return PRICE_FERRITIN_FALLBACK


def auc_ci(y, p, B: int = 300, seed: int = 0) -> list[float]:
    """95 % ДИ для AUC: бутстреп по людям (B повторов), перцентили 2,5 и 97,5."""
    from sklearn.metrics import roc_auc_score

    rng = np.random.default_rng(seed)
    y, p = np.asarray(y), np.asarray(p)
    out = []
    for _ in range(B):
        i = rng.integers(0, len(y), len(y))
        if y[i].min() != y[i].max():
            out.append(roc_auc_score(y[i], p[i]))
    return [round(float(v), 3) for v in np.percentile(out, [2.5, 97.5])]


def rule_flag(d) -> np.ndarray:
    """Простое правило по индексам ОАК (1 — направить на ферритин)."""
    return ((d.MCV < RULE["MCV_below_fl"]) | (d.MCH < RULE["MCH_below_pg"])
            | (d.RDW > RULE["RDW_above_pct"])).values.astype(float)


def compare_with_rule(p, y, w, flag, price: int, B: int = 500, seed: int = 1) -> dict:
    """Модель против простого правила при том же числе направлений (с весами обследования).

    Правило направляет долю share людей. Модель направляет столько же — верхнюю долю share по вероятности.
    Сравниваются чувствительность и PPV; разница чувствительности — с 95 % ДИ по парному бутстрепу
    (в каждой повторной выборке пересчитываются и правило, и модель).
    """
    share = wmean(flag, w)

    def at_share(idx):
        yy, ww, pp, ff = y[idx], w[idx], p[idx], flag[idx]
        o = np.argsort(-pp)
        cw = np.cumsum(ww[o]) / ww.sum()
        k = int(np.searchsorted(cw, (ww * ff).sum() / ww.sum())) + 1
        sel = np.zeros(len(yy))
        sel[o[:k]] = 1
        return ((ww * sel * yy).sum() / (ww * yy).sum(), (ww * sel * yy).sum() / (ww * sel).sum(),
                (ww * ff * yy).sum() / (ww * yy).sum(), (ww * sel).sum() / ww.sum())

    sm, pv, sr, model_share = at_share(np.arange(len(y)))
    rule_ppv = (w * flag * y).sum() / (w * flag).sum()
    rng = np.random.default_rng(seed)
    diffs = []
    for _ in range(B):
        i = rng.integers(0, len(y), len(y))
        if y[i].sum() > 0:
            a_, _, b_, _ = at_share(i)
            diffs.append(a_ - b_)
    lo, hi = np.percentile(diffs, [2.5, 97.5])
    return {
        "rule": {"referred_per1000": int(round(1000 * share)), "sensitivity": round(float(sr), 3),
                 "ppv": round(float(rule_ppv), 3), "rub_per_case_found": int(round(price / rule_ppv))},
        "model_same_referrals": {"referred_per1000": int(round(1000 * model_share)), "sensitivity": round(float(sm), 3),
                                 "ppv": round(float(pv), 3), "rub_per_case_found": int(round(price / pv))},
        "sensitivity_diff_pp": round(100 * float(sm - sr), 1),
        "sensitivity_diff_ci95_pp": [round(100 * float(lo), 1), round(100 * float(hi), 1)],
        "model_better": bool(lo > 0),   # преимущество показано, только если весь 95 % ДИ выше нуля
    }


def _metric_rows(te, preds) -> list[dict]:
    """AUC, 95 % ДИ, PR-AUC, Brier, средняя предсказанная и наблюдаемая доля по группам (без весов, как в прототипе)."""
    from sklearn.metrics import average_precision_score, brier_score_loss, roc_auc_score

    rows = []
    for tgt in TARGETS:
        for features, scaling, proto in FEATURE_VARIANTS:
            p = preds[(tgt, features, scaling)]
            for grp in METRIC_GROUPS:
                k = np.ones(len(te), bool) if grp == "ALL" else (te.grp == grp).values
                y = te[tgt].values[k]
                if y.sum() < MIN_CASES:
                    continue
                mean_p, obs = float(p[k].mean()), float(y.mean())
                rows.append(dict(
                    target=tgt, features=features, scaling=scaling, prototype_name=proto, group=grp,
                    n=int(k.sum()), cases=int(y.sum()),
                    auc=round(float(roc_auc_score(y, p[k])), 3), ci95=auc_ci(y, p[k], **AUC_BOOTSTRAP),
                    pr_auc=round(float(average_precision_score(y, p[k])), 3),
                    brier=round(float(brier_score_loss(y, p[k])), 4),
                    mean_predicted=round(mean_p, 3), observed=round(obs, 3),
                    calibration_gap=round(mean_p - obs, 3),
                ))
    return rows


def calibration_bins(y, p) -> list[dict]:
    """Калибровка по диапазонам: сколько людей получили вероятность из диапазона, средняя предсказанная
    и наблюдаемая доля случаев (без весов)."""
    y, p = np.asarray(y), np.asarray(p, float)
    rows = []
    for a, b in zip(PROB_BINS, PROB_BINS[1:]):
        j = (p >= a) & ((p < b) if b < 1.0 else (p <= b))
        if j.sum():
            rows.append({"p_from": a, "p_to": b, "n": int(j.sum()), "cases": int(y[j].sum()),
                         "mean_predicted": round(float(p[j].mean()), 3), "observed": round(float(y[j].mean()), 3)})
    return rows


def _application_path(te, models, metrics) -> dict:
    """Путь применения в сервисе: те же модели временной проверки, но ОАК тестовых людей стандартизован
    не внутри цикла, а по справочнику лаборатории "default" из config/lab_reference.yaml (как в ScreenModel.predict).
    Показывает, что метрики проверки относятся и к тому, как модель реально вызывается."""
    from sklearn.metrics import roc_auc_score

    by_sex = reference_by_sex(load_lab_references().get("references", {}).get(DEFAULT_REFERENCE))
    rows = []
    for features in ("full_cbc", "hb_only"):
        labs = FEATURE_LABS[features]
        X = np.array([standardize(r, r["sex"], by_sex, labs) + [float(r["age_years"]), float(r["female"])]
                      for r in te[labs + ["sex", "age_years", "female"]].to_dict("records")], dtype=float)
        for tgt in TARGETS:
            p = models[(tgt, features, "z")].predict_proba(X)[:, 1]
            for grp in METRIC_GROUPS:
                ref = _row(metrics, tgt, features, "z", grp)
                if ref is None:
                    continue
                k = np.ones(len(te), bool) if grp == "ALL" else (te.grp == grp).values
                y = te[tgt].values[k]
                rows.append(dict(target=tgt, features=features, group=grp, n=int(k.sum()),
                                 auc_within_cycle=ref["auc"],
                                 auc_default_reference=round(float(roc_auc_score(y, p[k])), 3),
                                 mean_predicted_within_cycle=ref["mean_predicted"],
                                 mean_predicted_default_reference=round(float(p[k].mean()), 3),
                                 observed=ref["observed"]))
    return {"note": ("стандартизация по справочнику \"default\" (NHANES 2017–2023, оба цикла вместе, по полу) вместо "
                     "стандартизации внутри цикла; модели те же — обученные на 1999–2016"),
            "rows": rows}


def _row(rows, target, features, scaling, group) -> dict | None:
    for r in rows:
        if (r["target"], r["features"], r["scaling"], r["group"]) == (target, features, scaling, group):
            return r
    return None


def _prototype_check(metrics: list[dict], tables: dict) -> dict:
    """Сверка с сохранёнными результатами прототипа (research/prototypes): наибольшее расхождение по всем числам."""
    proto = REPO_ROOT / "research" / "prototypes"
    out: dict = {"available": False}
    try:
        with open(proto / "nhanes_metrics.csv", encoding="utf-8") as f:
            ref_rows = list(csv.DictReader(f))
        ref_tables = json.loads((proto / "nhanes_referral.json").read_text(encoding="utf-8"))["referral"]
    except (OSError, KeyError, ValueError):
        return out
    mine = {(r["target"], r["prototype_name"], r["group"]): r for r in metrics}
    max_diff, compared, missing = 0.0, 0, []
    for r in ref_rows:
        m = mine.get((r["target"], r["features"], r["group"]))
        if m is None:
            missing.append(f'{r["target"]}|{r["features"]}|{r["group"]}')
            continue
        compared += 1
        pairs = [(m[k], float(r[k])) for k in ("n", "cases", "auc", "pr_auc", "brier", "mean_predicted", "observed")]
        pairs += list(zip(m["ci95"], json.loads(r["ci95"])))
        max_diff = max(max_diff, max(abs(float(a) - float(b)) for a, b in pairs))
    names = {"full_cbc": "L1_z", "hb_only": "L0_z"}
    t_max, t_compared = 0.0, 0
    for grp, by_tgt in tables.items():
        for tgt, by_feat in by_tgt.items():
            for features, proto_name in names.items():
                ref = ref_tables.get(f"{grp}|{tgt}|{proto_name}")
                if ref is None or features not in by_feat:
                    missing.append(f"{grp}|{tgt}|{proto_name}")
                    continue
                t_compared += 1
                for a, b in zip(by_feat[features], ref):
                    t_max = max(t_max, max(abs(float(a[k]) - float(b[k])) for k in b))
    out.update(available=True, metrics_rows_compared=compared, metrics_rows_in_prototype=len(ref_rows),
               metrics_max_abs_diff=round(max_diff, 6), referral_tables_compared=t_compared,
               referral_tables_in_prototype=len(ref_tables), referral_max_abs_diff=round(t_max, 6),
               missing=missing, identical=bool(max_diff == 0 and t_max == 0 and not missing))
    return out


def _case_transfer(case_path: str, models: dict) -> dict:
    """Перенос на файл кейса (синтетика): латентный дефицит железа против здоровых, люди без анемии, только ОАК.

    raw — как в прототипе (сырые признаки, цель < 30); z — путь применения в сервисе: стандартизация
    по справочнику лаборатории "default" (NHANES 2017–2023), модель временной проверки.
    """
    import pandas as pd
    from sklearn.metrics import roc_auc_score

    c = pd.read_csv(case_path)
    c["female"] = (c.sex == "F").astype(int)
    cn = c[c.anemia == 0]
    s = cn[cn.anemia_class.isin(["latent_deficiency", "no_anemia_no_deficiency"])]
    y = (s.anemia_class == "latent_deficiency").astype(int).values
    out = {"n": int(len(s)), "cases": int(y.sum()),
           "task": "latent_deficiency против no_anemia_no_deficiency, строки кейса без анемии",
           "auc_raw_t30": round(float(roc_auc_score(
               y, models[("t30", "full_cbc", "raw")].predict_proba(feature_matrix(s, "full_cbc", "raw"))[:, 1])), 3)}
    by_sex = reference_by_sex(load_lab_references().get("references", {}).get(DEFAULT_REFERENCE))
    labs = FEATURE_LABS["full_cbc"]
    Xz = np.array([standardize(r, r["sex"], by_sex, labs) + [float(r["age_years"]), float(r["female"])]
                   for r in s[labs + ["sex", "age_years", "female"]].to_dict("records")], dtype=float)
    for tgt in TARGETS:
        out[f"auc_z_default_reference_{tgt}"] = round(float(roc_auc_score(
            y, models[(tgt, "full_cbc", "z")].predict_proba(Xz)[:, 1])), 3)
    return out


def evaluate(nhanes_path: str, out_json: str | None = None, case_path: str | None = None) -> dict:
    """Временная проверка скрининга. Возвращает словарь с разделами summary / metrics / referral /
    rule_comparison / calibration_shift / cohort; при out_json сохраняет его в файл (только сводные числа)."""
    from sklearn.inspection import permutation_importance
    import sklearn

    t0 = time.time()
    price = ferritin_price()
    adults, cohort, counts = load_cohort(nhanes_path)
    na = cohort[cohort.anemia_who == 0]
    tr, te = na[~na.is_test], na[na.is_test]

    # --- обучение на 1999–2016, предсказания на тесте ------------------------------------------
    models, preds = {}, {}
    for tgt in TARGETS:
        for features, scaling, _ in FEATURE_VARIANTS:
            m = fit_model(feature_matrix(tr, features, scaling), tr[tgt].to_numpy())
            models[(tgt, features, scaling)] = m
            preds[(tgt, features, scaling)] = m.predict_proba(feature_matrix(te, features, scaling))[:, 1]
    metrics = _metric_rows(te, preds)
    # Группы, где случаев меньше MIN_CASES: метрики и таблицы не считаются (как в прототипе), но это видно в отчёте.
    skipped = [{"target": tgt, "group": grp, "n": int((te.grp == grp).sum()),
                "cases": int(te[tgt].values[(te.grp == grp).values].sum()), "reason": f"случаев меньше {MIN_CASES}"}
               for tgt in TARGETS for grp in GROUPS if te[tgt].values[(te.grp == grp).values].sum() < MIN_CASES]

    # --- таблицы направлений и сравнение с правилом (веса mec_weight) ---------------------------
    tables: dict = {}
    baselines: dict = {}
    rule_cmp: dict = {}
    for grp in GROUPS:
        mask = (te.grp == grp).values
        d = te[mask]
        w = d.w.values
        flag = rule_flag(d)
        for tgt in TARGETS:
            y = d[tgt].values
            if y.sum() < MIN_CASES:
                continue
            tables.setdefault(grp, {})[tgt] = {
                features: referral_table(preds[(tgt, features, "z")][mask], y, w, REFER_FRACS, price)
                for features in ("full_cbc", "hb_only")}
            prev = wmean(y, w)
            baselines.setdefault(grp, {})[tgt] = {
                "n": int(len(d)), "cases": int(y.sum()),
                "test_everyone": {"cases_per1000": int(round(1000 * prev)),
                                  "rub_per_case_found": int(round(price / prev))},
                "test_nobody": {"found_per1000": 0},  # текущая практика при нормальном Hb
            }
            rule_cmp.setdefault(grp, {})[tgt] = compare_with_rule(
                preds[(tgt, "full_cbc", "z")][mask], y, w, flag, price, **RULE_BOOTSTRAP)

    # --- важность признаков (падение AUC при перемешивании), женщины 18–49, цель t15 -------------
    f_mask = (te.grp == "F18-49").values
    pi = permutation_importance(models[("t15", "full_cbc", "z")], feature_matrix(te[f_mask], "full_cbc", "z"),
                                te["t15"].values[f_mask], scoring="roc_auc", n_repeats=5, random_state=0)
    importance = sorted(zip(feature_names("full_cbc", "z"), [round(float(v), 4) for v in pi.importances_mean]),
                        key=lambda x: -x[1])

    # --- сдвиг анализатора: сырые признаки против стандартизованных -----------------------------
    shift_rows = []
    for tgt in TARGETS:
        for features in ("full_cbc", "hb_only"):
            for grp in METRIC_GROUPS:
                raw, z = _row(metrics, tgt, features, "raw", grp), _row(metrics, tgt, features, "z", grp)
                if raw is None or z is None:
                    continue
                obs = z["observed"]
                shift_rows.append(dict(
                    target=tgt, features=features, group=grp, observed=obs,
                    mean_predicted_raw=raw["mean_predicted"], mean_predicted_z=z["mean_predicted"],
                    ratio_raw=round(raw["mean_predicted"] / obs, 2), ratio_z=round(z["mean_predicted"] / obs, 2),
                    brier_raw=raw["brier"], brier_z=z["brier"], auc_raw=raw["auc"], auc_z=z["auc"]))
    calibration_shift = {
        "note": ("Смена анализатора в NHANES с 2013 года сдвинула RDW; модель на сырых значениях завышает риск, "
                 "стандартизация внутри цикла по полу (без меток) возвращает калибровку."),
        "rdw_median_by_cycle": {str(k): round(float(v), 2)
                                for k, v in adults.groupby("nhanes_cycle").RDW.median().items()},
        "rows": shift_rows,
    }

    # --- когорта ---------------------------------------------------------------------------------
    prevalence, hidden = {}, {}
    for grp in GROUPS:
        d = cohort[(cohort.anemia_who == 0) & cohort.is_test & (cohort.grp == grp)]
        prevalence[grp] = {"n": int(len(d)), "ferritin_lt15_pct": round(100 * wmean(d.t15, d.w), 1),
                           "ferritin_lt30_pct": round(100 * wmean(d.t30, d.w), 1)}
        d = cohort[cohort.is_test & (cohort.grp == grp)]
        d15, d30 = d[d.t15 == 1], d[d.t30 == 1]
        hidden[grp] = {"lt15_without_anemia_pct": round(100 * wmean(1 - d15.anemia_who, d15.w), 1),
                       "lt15_n": int(len(d15)),
                       "lt30_without_anemia_pct": round(100 * wmean(1 - d30.anemia_who, d30.w), 1),
                       "lt30_n": int(len(d30))}
    by_cycle = cohort.groupby(["nhanes_cycle", "sex"]).size().unstack(fill_value=0)
    cohort_info = {
        "scope": (f"взрослые 18+, не беременные, полный ОАК (9 показателей), измерен ферритин; модель и метрики — "
                  f"без анемии по ВОЗ (Hb ≥ {WHO_HB_G_L['F']:g} г/л у женщин, ≥ {WHO_HB_G_L['M']:g} г/л у мужчин)"),
        "counts": counts,
        "test_cycles": list(TEST_CYCLES),
        "test_no_anemia_by_group": {g: int((te.grp == g).sum()) for g in GROUPS},
        "train_no_anemia_by_group": {g: int((tr.grp == g).sum()) for g in GROUPS},
        "with_ferritin_by_cycle": {str(c): {s: int(by_cycle.loc[c, s]) for s in by_cycle.columns}
                                   for c in by_cycle.index},
        "prevalence_no_anemia_test_weighted": prevalence,
        "deficient_without_anemia_test_weighted": hidden,
    }

    # --- сводка, ворота, сверка с прототипом -------------------------------------------------------
    def key(tgt, grp, features="full_cbc", scaling="z"):
        r = _row(metrics, tgt, features, scaling, grp)
        return None if r is None else {k: r[k] for k in ("n", "cases", "auc", "ci95", "pr_auc", "brier",
                                                          "mean_predicted", "observed", "calibration_gap")}

    f15, f30 = key("t15", "F18-49"), key("t30", "F18-49")
    gate_checks = {
        "auc_t15_F18-49": bool(f15["auc"] >= GATES["auc_t15_min"]),
        "auc_t30_F18-49": bool(f30["auc"] >= GATES["auc_t30_min"]),
    }
    for tgt in TARGETS:
        for grp in ("ALL", "F18-49"):
            gap = abs(key(tgt, grp)["calibration_gap"])
            gate_checks[f"calibration_{tgt}_{grp}"] = bool(gap <= GATES["calibration_gap_max"])
    prototype = _prototype_check(metrics, tables)
    application = _application_path(te, models, metrics)
    summary = {
        "model": VERSION,
        "validation": ("обучение 1999–2016 → тест 2017–2018 и 2021–2023; взрослые без анемии по ВОЗ; "
                       "признаки full_cbc со стандартизацией"),
        "n_train": int(len(tr)), "n_test": int(len(te)),
        "by_group": {grp: {tgt: key(tgt, grp) for tgt in TARGETS} for grp in METRIC_GROUPS},
        "hb_only_F18-49": {tgt: key(tgt, "F18-49", "hb_only") for tgt in TARGETS},
        "raw_features_F18-49": {tgt: key(tgt, "F18-49", "full_cbc", "raw") for tgt in TARGETS},
        "referral_F18-49_t15": tables.get("F18-49", {}).get("t15", {}).get("full_cbc"),
        "baseline_F18-49_t15": baselines.get("F18-49", {}).get("t15"),
        "rule_vs_model_F18-49_t15": rule_cmp.get("F18-49", {}).get("t15"),
        "application_path_F18-49": [r for r in application["rows"]
                                    if r["group"] == "F18-49" and r["features"] == "full_cbc"],
        "not_evaluated": skipped,
        "gates": {"thresholds": GATES, "checks": gate_checks, "passed": bool(all(gate_checks.values()))},
        "prototype_check": prototype,
        "price_ferritin_rub": price,
    }

    result = {
        "summary": summary,
        "metrics": metrics,
        "metrics_skipped": skipped,
        "referral": {"price_ferritin_rub": price, "weights": "mec_weight",
                     "population": "тест 2017–2023, без анемии по ВОЗ",
                     "note": "на 1000 человек группы; модели со стандартизацией (z)",
                     "tables": tables, "baselines": baselines},
        "rule_comparison": {"rule": "MCV < 80 фл, или MCH < 27 пг, или RDW > 14,5 %", "model": "full_cbc, z",
                            "bootstrap": RULE_BOOTSTRAP, "by_group": rule_cmp},
        "calibration_shift": calibration_shift,
        "cohort": cohort_info,
        "calibration_bins": {
            "note": ("предсказанная вероятность против наблюдаемой доли по диапазонам; "
                     "full_cbc со стандартизацией, без весов"),
            "by_target": {tgt: {grp: calibration_bins(te[tgt].values[k], preds[(tgt, "full_cbc", "z")][k])
                                for grp, k in (("ALL", np.ones(len(te), bool)), ("F18-49", f_mask))}
                          for tgt in TARGETS}},
        "application_path": application,
        "importance": {"group": "F18-49", "target": "t15",
                       "method": "падение AUC при перемешивании признака, 5 повторов",
                       "features": [{"feature": k, "auc_drop": v} for k, v in importance]},
        "settings": {"targets_ferritin_ug_l": TARGETS, "hgb": HGB_PARAMS, "calibration": CALIBRATION,
                     "auc_bootstrap": AUC_BOOTSTRAP, "refer_shares": list(REFER_FRACS), "min_cases": MIN_CASES},
        "data": {"file": Path(nhanes_path).name, "sha256": sha256_file(nhanes_path)},
        "versions": {"python": platform.python_version(), "scikit-learn": sklearn.__version__, "numpy": np.__version__},
        "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    if case_path and Path(case_path).exists():
        result["case_transfer"] = _case_transfer(case_path, models)
        summary["case_transfer"] = result["case_transfer"]
    summary["seconds"] = round(time.time() - t0, 1)

    if out_json:
        out = Path(out_json)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    return result
