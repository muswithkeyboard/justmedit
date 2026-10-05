"""Проверка координатора: воспроизводим V5/V7 и пробуем ансамбли для «режима бенчмарка» (L_asis).
Те же фолды и seed, что в run_experiment.py. Ничего не подбирается по тестовым фолдам."""
import os; os.environ.setdefault("OMP_NUM_THREADS","1")
import numpy as np, pandas as pd, time, json
from joblib import Parallel, delayed
from sklearn.ensemble import HistGradientBoostingClassifier, RandomForestClassifier
from sklearn.metrics import accuracy_score, f1_score
from sklearn.model_selection import RepeatedStratifiedKFold
import run_experiment as R
X,y,an=R.load()
def feats_plus(X):
    g=lambda n: X[:,R.F_IDX[n]]
    with np.errstate(all='ignore'):
        extra=np.column_stack([g('MCV')/g('RBC'), g('RDW')/g('MCV')*100, g('sTfR')/np.log10(np.clip(g('ferritin'),1e-3,None)),
                               g('serum_iron')/g('TIBC')*100, g('vitamin_B12')/np.clip(g('folate'),1e-3,None), R.who_anemia(X)])
    return np.column_stack([X,extra])
def geo(*Ps,w=None):
    w=w or [1/len(Ps)]*len(Ps); L=sum(wi*np.log(np.clip(P,1e-9,1)) for wi,P in zip(w,Ps)); L-=L.max(1,keepdims=True); P=np.exp(L); return P/P.sum(1,keepdims=True)
def metrics(P14,yy,an_true,an_who):
    m,_=R.evaluate(P14,yy,an_true,an_who)
    P12=P14@R.A12; y12=(np.eye(R.NP_)[yy]@R.A12).argmax(1); p12=P12.argmax(1)
    m['acc5']=accuracy_score(R.group5(y12,an_true),R.group5(p12,an_who)); return m
def fold(k,tr,te):
    seed=1000+k; Xtr,ytr,Xte,yte=X[tr],y[tr],X[te],y[te]; out=[]
    v2=R.make_hgb(seed).fit(Xtr,ytr)
    v2b=HistGradientBoostingClassifier(learning_rate=0.05,max_iter=300,max_leaf_nodes=8,min_samples_leaf=8,l2_regularization=1.0,early_stopping=False,random_state=seed).fit(feats_plus(Xtr),ytr)
    rf=RandomForestClassifier(n_estimators=400,min_samples_leaf=2,max_features=0.4,random_state=seed,n_jobs=1).fit(feats_plus(Xtr),ytr)
    v5=R.fit_v5(Xtr,ytr,seed)
    for level in R.LEVEL_NAMES:
        XL=R.apply_level(Xte,level); aw=R.who_anemia(XL)
        P={ 'V7_gbm_lock':R.who_lock(R.proba14(v2,XL),aw),
            'V7b_gbm_tuned_feats_lock':R.who_lock(R.proba14(v2b,feats_plus(XL)),aw),
            'RF_feats_lock':R.who_lock(R.proba14(rf,feats_plus(XL)),aw),
            'V5_fusion':R.predict_v5(v5,XL),
            'V5L_fusion_presence':R.predict_v5(v5,XL,use_presence=True)}
        P['ENS_V7b+V5L']=R.who_lock(geo(P['V7b_gbm_tuned_feats_lock'],P['V5L_fusion_presence']),aw)
        P['ENS_V7b+RF+V5L']=R.who_lock(geo(P['V7b_gbm_tuned_feats_lock'],P['RF_feats_lock'],P['V5L_fusion_presence']),aw)
        P['ENS_V7b+V5(noleak)']=R.who_lock(geo(P['V7b_gbm_tuned_feats_lock'],P['V5_fusion']),aw)
        for name,p in P.items():
            out.append(dict(fold=k,variant=name,level=level,**{kk:vv for kk,vv in metrics(p,yte,an[te],aw).items()}))
    return out
t0=time.time()
splits=list(RepeatedStratifiedKFold(n_splits=5,n_repeats=3,random_state=R.SEED).split(X,y))
res=Parallel(n_jobs=2)(delayed(fold)(k,tr,te) for k,(tr,te) in enumerate(splits))
D=pd.DataFrame([r for f in res for r in f]); D.to_csv(os.path.join(os.path.dirname(os.path.abspath(__file__)),'ensemble_folds.csv'),index=False)
pd.set_option('display.width',220)
for lv in R.LEVEL_NAMES:
    g=D[D.level==lv].groupby('variant')[['acc12','f1_12','acc5','f1_5','f1_cause','logloss12','ece','auroc_hidden']].agg(['mean','std']).round(3)
    g.columns=[f'{a}_{b}' for a,b in g.columns]; print('\n==',lv); print(g[[c for c in g.columns if c.endswith('mean')]+['acc12_std']].sort_values('acc12_mean',ascending=False).to_string())
print('seconds',round(time.time()-t0))
