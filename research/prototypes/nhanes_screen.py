#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
nhanes_screen.py — прототип модуля «скрытый дефицит железа по одному ОАК» на реальных людях (NHANES).

Запуск:
  DL_NHANES_DATA=/abs/nhanes_deficiency_real.csv DL_CASE_DATA=/abs/deficiency_anemia.csv python3 nhanes_screen.py

Что делает
  * когорта: взрослые 18+, небеременные, с измеренным ферритином (ferritin_harmonized) и полным ОАК;
  * цель: ферритин < 15 мкг/л (ВОЗ 2020) и < 30 мкг/л (порог практики) при НОРМАЛЬНОМ гемоглобине (ВОЗ);
  * временная внешняя проверка: обучение на циклах 1999–2016, тест на 2017–2018 и 2021–2023;
  * два уровня признаков: L0 = Hb + возраст + пол; L1 = полный ОАК + возраст + пол;
  * два представления ОАК: «сырые» значения и стандартизация внутри лаборатории/цикла по полу
    (z = (x − медиана) / межквартильный размах; метки не нужны). Стандартизация обязательна:
    в NHANES при смене анализатора (2013+) медиана RDW выросла с 12.5 до 13.6, и модель на сырых
    значениях завышает риск примерно в 1.5 раза; после стандартизации калибровка восстанавливается;
  * таблица «сколько направить на ферритин и сколько случаев найдём» на 1000 человек (с весами NHANES);
  * перенос на файл кейса; описательная таблица «ферритин под маской воспаления» (sTfR).
Результаты: RESULTS_nhanes.md, nhanes_metrics.csv, nhanes_referral.json, lab_reference_default.json — рядом со скриптом.
Это прототип для хакатона, не медицинское изделие. Данные — США.
"""
import json, os, time, warnings
from pathlib import Path
import numpy as np, pandas as pd
from sklearn.calibration import CalibratedClassifierCV
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.inspection import permutation_importance
from sklearn.metrics import average_precision_score, brier_score_loss, roc_auc_score
warnings.filterwarnings("ignore")
OUT = Path(__file__).resolve().parent
NHANES = os.environ.get("DL_NHANES_DATA", "data/nhanes/nhanes_deficiency_real.csv")
CASE = os.environ.get("DL_CASE_DATA", "data/case/deficiency_anemia.csv")
CBC = ["hemoglobin", "RBC", "hematocrit", "MCV", "MCH", "MCHC", "RDW", "platelets", "WBC"]
TEST_CYCLES = ["2017-2018", "2021-2023"]
PRICE_FERRITIN = 845  # руб., медиана по 4 лабораториям Москвы, 03.10.2026, без взятия крови
t0 = time.time(); L = []
def say(*a):
    s = " ".join(str(x) for x in a); print(s); L.append(s)
def wmean(x, w): return float((np.asarray(x, float) * w).sum() / w.sum())

n = pd.read_csv(NHANES, low_memory=False)
adults = n[(n.age_years >= 18) & (n.pregnant == 0)].dropna(subset=CBC).copy()
adults["female"] = (adults.sex == "F").astype(int)
adults["anemia_who"] = np.where(adults.female == 1, adults.hemoglobin < 120, adults.hemoglobin < 130).astype(int)
# стандартизация ОАК внутри цикла (= «лаборатории») по полу — только по распределению ОАК, без меток
g = adults.groupby(["nhanes_cycle", "sex"])
for c in CBC:
    med = g[c].transform("median"); iqr = g[c].transform(lambda s: s.quantile(.75) - s.quantile(.25))
    adults[c + "_z"] = (adults[c] - med) / iqr
a = adults[adults.ferritin_harmonized.notna()].copy()
a["fer"] = a.ferritin_harmonized
a["t15"] = (a.fer < 15).astype(int); a["t30"] = (a.fer < 30).astype(int)
a["is_test"] = a.nhanes_cycle.isin(TEST_CYCLES); a["w"] = a.mec_weight
a["grp"] = np.select([(a.female == 1) & (a.age_years < 50), (a.female == 1) & (a.age_years >= 50)], ["F18-49", "F50+"], "M")
say("# Прототип: скрытый дефицит железа по ОАК (NHANES)\n")
say(f"- всего строк в файле: {len(n)}; взрослых 18+: {(n.age_years>=18).sum()}; детей: {(n.age_years<18).sum()}")
say(f"- взрослых небеременных с ферритином и полным ОАК: {len(a)} (обучение 1999–2016: {(~a.is_test).sum()}, тест 2017–2023: {a.is_test.sum()})")
say(f"- без анемии по ВОЗ: {(a.anemia_who==0).sum()} (обучение {((a.anemia_who==0)&~a.is_test).sum()}, тест {((a.anemia_who==0)&a.is_test).sum()})")
say("\n## Медиана RDW по циклам (взрослые) — смена анализатора видна на 2013+\n")
say(adults.groupby("nhanes_cycle").RDW.median().round(2).to_string())
say("\n## Распространённость при НОРМАЛЬНОМ гемоглобине, % (взвешенно, тест 2017–2023)\n")
for grp in ["F18-49", "F50+", "M"]:
    d = a[(a.anemia_who == 0) & a.is_test & (a.grp == grp)]
    say(f"- {grp}: n={len(d)}; ферритин <15: {100*wmean(d.t15,d.w):.1f}%; <30: {100*wmean(d.t30,d.w):.1f}%")
say("\n## Доля людей с дефицитом железа, у которых НЕТ анемии (по одному Hb их не видно), % (взвешенно, тест)\n")
for grp in ["F18-49", "F50+", "M"]:
    d = a[a.is_test & (a.grp == grp)]
    say(f"- {grp}: при ферритине <15 — {100*wmean(1-d[d.t15==1].anemia_who, d[d.t15==1].w):.1f}% (n={int(d.t15.sum())}); при <30 — {100*wmean(1-d[d.t30==1].anemia_who, d[d.t30==1].w):.1f}% (n={int(d.t30.sum())})")

FEATS = {"L0_raw": ["hemoglobin", "age_years", "female"], "L1_raw": CBC + ["age_years", "female"],
         "L0_z": ["hemoglobin_z", "age_years", "female"], "L1_z": [c + "_z" for c in CBC] + ["age_years", "female"]}
def fit(X, y):
    base = HistGradientBoostingClassifier(max_iter=250, learning_rate=0.05, max_leaf_nodes=15, l2_regularization=1.0, min_samples_leaf=40, random_state=0)
    return CalibratedClassifierCV(base, method="isotonic", cv=5).fit(X, y)
def boot(y, p, B=300, seed=0):
    rng = np.random.default_rng(seed); y = np.asarray(y); p = np.asarray(p); out = []
    for _ in range(B):
        i = rng.integers(0, len(y), len(y))
        if y[i].min() != y[i].max(): out.append(roc_auc_score(y[i], p[i]))
    return [round(float(v), 3) for v in np.percentile(out, [2.5, 97.5])]
na = a[a.anemia_who == 0]; tr, te = na[~na.is_test], na[na.is_test]
rows, models = [], {}
for tgt in ["t15", "t30"]:
    for name, F in FEATS.items():
        m = fit(tr[F], tr[tgt]); models[(tgt, name)] = m; p = m.predict_proba(te[F])[:, 1]
        for grp in ["ALL", "F18-49", "F50+", "M"]:
            k = np.ones(len(te), bool) if grp == "ALL" else (te.grp == grp).values
            y = te[tgt].values[k]
            if y.sum() < 15: continue
            rows.append(dict(target=tgt, features=name, group=grp, n=int(k.sum()), cases=int(y.sum()), auc=round(roc_auc_score(y, p[k]), 3), ci95=boot(y, p[k]),
                             pr_auc=round(average_precision_score(y, p[k]), 3), brier=round(brier_score_loss(y, p[k]), 4),
                             mean_predicted=round(float(p[k].mean()), 3), observed=round(float(y.mean()), 3)))
R = pd.DataFrame(rows); R.to_csv(OUT / "nhanes_metrics.csv", index=False)
say("\n## Временная проверка (обучение 1999–2016 → тест 2017–2023), люди с нормальным Hb\n")
say("mean_predicted против observed — калибровка «в целом»: у сырых признаков завышение, у стандартизованных (z) — совпадение.\n")
say(R.to_string(index=False))

def referral(p, y, w, fracs=(0.1, 0.2, 0.3, 0.4, 0.5)):
    o = np.argsort(-p); cw = np.cumsum(w[o]) / w.sum(); out = []
    for f in fracs:
        k = int(np.searchsorted(cw, f)) + 1; s = o[:k]; tp = (w[s] * y[s]).sum(); ref = w[s].sum()
        out.append(dict(refer_share=f, referred_per1000=round(1000 * ref / w.sum()), cases_per1000=round(1000 * (w * y).sum() / w.sum()),
                        found_per1000=round(1000 * tp / w.sum()), sensitivity=round(float(tp / (w * y).sum()), 3), ppv=round(float(tp / ref), 3),
                        referred_per_case_found=round(float(ref / tp), 2), rub_per_case_found=round(PRICE_FERRITIN * float(ref / tp))))
    return pd.DataFrame(out)
ref_json = {}
say("\n## Кого направлять на ферритин: на 1000 человек с нормальным Hb (взвешенно, тест 2017–2023, модель L1_z)\n")
for grp in ["F18-49", "F50+", "M"]:
    d = te[te.grp == grp]
    for tgt in ["t15", "t30"]:
        if d[tgt].sum() < 15: continue
        y, w = d[tgt].values, d.w.values
        for name in ["L1_z", "L0_z"]:
            T = referral(models[(tgt, name)].predict_proba(d[FEATS[name]])[:, 1], y, w); ref_json[f"{grp}|{tgt}|{name}"] = T.to_dict("records")
            say(f"\n### {grp}, цель {tgt}, признаки {name}\n"); say(T.to_string(index=False))
        prev = wmean(y, w)
        say(f"\nБазы: «ферритин всем» — {round(1000*prev)} случаев на 1000 анализов, {round(PRICE_FERRITIN/prev)} руб. на найденный случай; "
            f"«никому» (текущая практика при нормальном Hb) — 0 найдено.")
        flag = ((d.MCV < 80) | (d.MCH < 27) | (d.RDW > 14.5)).values.astype(float)
        say(f"Простое правило MCV<80 или MCH<27 или RDW>14.5: направлено {round(1000*wmean(flag,w))} на 1000, чувствительность {(w*flag*y).sum()/(w*y).sum():.3f}, PPV {(w*flag*y).sum()/(w*flag).sum():.3f}.")
        # модель при том же числе направлений, что и правило (парный бутстреп разницы чувствительности)
        pm = models[(tgt, "L1_z")].predict_proba(d[FEATS["L1_z"]])[:, 1]; share = wmean(flag, w)
        def at_share(idx):
            yy, ww, pp, ff = y[idx], w[idx], pm[idx], flag[idx]; o = np.argsort(-pp); cw = np.cumsum(ww[o]) / ww.sum()
            k = int(np.searchsorted(cw, (ww * ff).sum() / ww.sum())) + 1; sel = np.zeros(len(yy)); sel[o[:k]] = 1
            return (ww*sel*yy).sum()/(ww*yy).sum(), (ww*sel*yy).sum()/(ww*sel).sum(), (ww*ff*yy).sum()/(ww*yy).sum()
        sm, pv, sr = at_share(np.arange(len(y))); rng = np.random.default_rng(1); diffs = []
        for _ in range(500):
            i = rng.integers(0, len(y), len(y))
            if y[i].sum() > 0: a_, _, b_ = at_share(i); diffs.append(a_ - b_)
        lo, hi = np.percentile(diffs, [2.5, 97.5])
        say(f"Модель при том же числе направлений ({round(1000*share)} на 1000): чувствительность {sm:.3f}, PPV {pv:.3f}, {round(PRICE_FERRITIN/pv)} руб. на найденный случай; "
            f"правило: {round(PRICE_FERRITIN/((w*flag*y).sum()/(w*flag).sum()))} руб.; разница чувствительности {100*(sm-sr):+.1f} п.п. (95% ДИ {100*lo:+.1f}…{100*hi:+.1f}).")
d = te[te.grp == "F18-49"]
pi = permutation_importance(models[("t15", "L1_z")], d[FEATS["L1_z"]], d["t15"], scoring="roc_auc", n_repeats=5, random_state=0)
imp = sorted(zip(FEATS["L1_z"], pi.importances_mean.round(4)), key=lambda x: -x[1])
say("\n## Важность признаков (падение AUC при перемешивании), женщины 18–49, цель t15\n"); say(", ".join(f"{k}: {v}" for k, v in imp))

if os.path.exists(CASE):
    c = pd.read_csv(CASE); c["female"] = (c.sex == "F").astype(int); cn = c[c.anemia == 0]
    s = cn[cn.anemia_class.isin(["latent_deficiency", "no_anemia_no_deficiency"])]
    y = (s.anemia_class == "latent_deficiency").astype(int)
    say("\n## Перенос на файл кейса (синтетика): модель, обученная на реальных людях, только ОАК\n")
    say(f"- латентный дефицит железа против здоровых, n={len(s)}: AUC {roc_auc_score(y, models[('t30','L1_raw')].predict_proba(s[FEATS['L1_raw']])[:,1]):.3f} (сырые признаки, цель <30)")
    say("- для сравнения: модель, обученная на самой синтетике, по одному ОАК даёт AUROC скрытого дефицита 0.89–0.90 (см. RESULTS по кейсу) — синтетика «прозрачнее» реальных людей")

b = adults[adults.ferritin_harmonized.notna() & adults.sTfR.notna() & adults.CRP.notna()].copy()
refp = b[(b.CRP <= 5) & (b.ferritin_harmonized >= 50) & (b.anemia_who == 0)]
cut = {s: float(refp[refp.sex == s].sTfR.quantile(0.95)) for s in ["F", "M"]}
b["stfr_high"] = (b.sTfR > b.sex.map(cut)).astype(float)
b["ferritin_bin"] = pd.cut(b.ferritin_harmonized, [0, 15, 30, 70, 100, 1e9], right=False, labels=["<15", "15-30", "30-70", "70-100", "100+"])
b["inflammation"] = np.where(b.CRP > 5, "CRP>5", "CRP<=5")
t = b.groupby(["inflammation", "ferritin_bin"], observed=True).apply(lambda q: pd.Series(dict(n=len(q), stfr_high_pct=round(100 * wmean(q.stfr_high, q.mec_weight), 1))))
say("\n## Ферритин под маской воспаления: доля с повышенным sTfR по диапазонам ферритина (взвешенно)\n")
say(f"Повышенный sTfR = выше 95-го перцентиля у людей без дефицита и воспаления (Ж {cut['F']:.2f}, М {cut['M']:.2f} мг/л; n={len(refp)}).\n"); say(t.to_string())

ref = adults[adults.nhanes_cycle.isin(TEST_CYCLES)].groupby("sex")[CBC].quantile([.25, .5, .75]).round(3)
json.dump({"source": "NHANES 2017-2023, взрослые небеременные", "by_sex": {s: {c: {"q25": float(ref.loc[(s, .25), c]), "median": float(ref.loc[(s, .5), c]), "q75": float(ref.loc[(s, .75), c])} for c in CBC} for s in ["F", "M"]}},
          open(OUT / "lab_reference_default.json", "w"), ensure_ascii=False, indent=1)
json.dump({"referral": ref_json, "importance": [(k, float(v)) for k, v in imp]}, open(OUT / "nhanes_referral.json", "w"), ensure_ascii=False, indent=1)
say(f"\nВремя: {round(time.time()-t0)} с")
(OUT / "RESULTS_nhanes.md").write_text("\n".join(L), encoding="utf-8")
