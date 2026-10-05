# Проверка чувствительности V6/V6w к ёмкости бустинга (НЕ используется для выбора конфигурации отчёта).
# Смотрим только фолды 0 и 7; цель — убедиться, что слабость V6 не артефакт заранее выбранных гиперпараметров.
import sys, time, numpy as np
import run_experiment as R
from sklearn.ensemble import HistGradientBoostingClassifier as H
from sklearn.model_selection import RepeatedStratifiedKFold
X, y, an = R.load()
splits = list(RepeatedStratifiedKFold(n_splits=5, n_repeats=3, random_state=R.SEED).split(X, y))
cfgs = {"base(leaf=3K,it=120,lr=.08)": None,
        "leaf=10": dict(learning_rate=0.08, max_iter=120, max_leaf_nodes=8, min_samples_leaf=10),
        "leaf=10,it=400,lr=.05": dict(learning_rate=0.05, max_iter=400, max_leaf_nodes=8, min_samples_leaf=10),
        "leaf=20,it=300,leaves=16": dict(learning_rate=0.08, max_iter=300, max_leaf_nodes=16, min_samples_leaf=20)}
orig = R.make_hgb
for fold in (0, 7):
    tr, te = splits[fold]
    for name, kw in cfgs.items():
        if kw is None: R.make_hgb = orig
        else: R.make_hgb = lambda seed, min_samples_leaf=10, kw=kw: H(l2_regularization=1.0, early_stopping=False, random_state=seed, **kw)
        for mode in ("mask", "ipw"):
            t0 = time.time(); m = R.fit_v6(X[tr], y[tr], 1000 + fold, mode=mode)
            out = []
            for lv in ("L_iron", "L_std", "L_asis"):
                XL = R.apply_level(X[te], lv)
                met, _ = R.evaluate(R.predict_v6(m, XL), y[te], an[te], R.who_anemia(XL))
                out.append(f"{lv}: acc={met['acc12']:.3f} f1={met['f1_12']:.3f} ll={met['logloss12']:.2f}")
            print(f"fold{fold} {mode:4s} {name:28s} | " + " | ".join(out) + f" | {time.time()-t0:.0f}s", flush=True)
