"""
train.py — 在 Amazon SageMaker AI 訓練作業中執行。
輸入：context.npz（半小時×站點的可借/可還/車柱矩陣、日型時段輪廓、鄰接矩陣）
流程：以 pipeline 相同的 features.py 建特徵 → 1–4 月訓練、5 月早停/驗證、6 月保留 → 四個尺度各四個模型。
輸出：/opt/ml/model/ 下的 joblib 模型與 metrics.json
"""
import os, sys, json, time
import numpy as np, pandas as pd, joblib
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from features import FEATURES, CATEGORICAL, HORIZONS, FeatureContext
from sklearn.ensemble import HistGradientBoostingRegressor, HistGradientBoostingClassifier
from sklearn.metrics import average_precision_score

IN = os.environ.get("SM_CHANNEL_TRAINING", "/opt/ml/input/data/training")
OUT = os.environ.get("SM_MODEL_DIR", "/opt/ml/model")
N_TRAIN = int(os.environ.get("N_TRAIN", "1500000"))
rng = np.random.default_rng(42); t0 = time.time()

z = np.load(os.path.join(IN, "context.npz"), allow_pickle=True)
bins = pd.DatetimeIndex(z["bins"]); B, S, C = z["B"], z["S"], z["C"]
prof = {"b": z["prof_b"], "s": z["prof_s"], "pe": z["prof_pe"], "pf": z["prof_pf"]}
stst = {"empty_rate": z["st_empty_rate"], "full_rate": z["st_full_rate"], "mean_b": z["st_mean_b"]}
ctx = FeatureContext(bins, B, S, C, prof, stst, z["A"], z["dcode"])
print("context ready", round(time.time()-t0), "s", B.shape, flush=True)

TRAIN = bins < "2026-05-01"; VAL = (bins >= "2026-05-01") & (bins < "2026-06-01")
CAT_IDX = [FEATURES.index(c) for c in CATEGORICAL]
HGB = dict(learning_rate=0.1, max_iter=600, max_leaf_nodes=63, min_samples_leaf=200, l2_regularization=1.0,
           early_stopping=True, validation_fraction=0.1, n_iter_no_change=25, categorical_features=CAT_IDX, random_state=42)

def cells(mask, h, n=None):
    T = np.where(mask)[0]; T = T[T + h < len(bins)]; T = T[mask[np.minimum(T + h, len(bins)-1)]]
    ok = ~np.isnan(B[T]) & ~np.isnan(B[T + h]); tt, ss = np.where(ok); t = T[tt]
    if n and len(t) > n:
        i = rng.choice(len(t), n, replace=False); t, ss = t[i], ss[i]
    return t, ss

metrics = {"trained_on": "Amazon SageMaker AI Training Job", "split": {"train": "2026-01..04", "val": "2026-05", "holdout": "2026-06"}, "horizons": {}}
for hm, h in HORIZONS.items():
    th = time.time()
    t_tr, s_tr = cells(TRAIN, h, N_TRAIN); t_va, s_va = cells(VAL, h, 500000)
    Xtr = ctx.make_X(t_tr, s_tr, h).values.astype(np.float32); Xva = ctx.make_X(t_va, s_va, h).values.astype(np.float32)
    cb_tr = Xtr[:, FEATURES.index("bikes")]; cs_tr = Xtr[:, FEATURES.index("spaces")]
    cb_va = Xva[:, FEATURES.index("bikes")]; cs_va = Xva[:, FEATURES.index("spaces")]
    yB_tr, yB_va = B[t_tr+h, s_tr], B[t_va+h, s_va]; yS_tr, yS_va = S[t_tr+h, s_tr], S[t_va+h, s_va]
    m = {}
    for tag, ytr, yva, cur_tr, cur_va, is_cls in [
        ("reg_bikes", yB_tr - cb_tr, yB_va, cb_tr, cb_va, False), ("reg_spaces", yS_tr - cs_tr, yS_va, cs_tr, cs_va, False),
        ("cls_empty", (yB_tr == 0).astype(int), (yB_va == 0).astype(int), cb_tr, cb_va, True),
        ("cls_full", (yS_tr == 0).astype(int), (yS_va == 0).astype(int), cs_tr, cs_va, True)]:
        mdl = (HistGradientBoostingClassifier(**HGB) if is_cls else HistGradientBoostingRegressor(**HGB))
        mdl.fit(Xtr, ytr); joblib.dump(mdl, os.path.join(OUT, f"hgb_h{hm}_{tag}.joblib"))
        if is_cls:
            p = mdl.predict_proba(Xva)[:, 1]; sc = float(average_precision_score(yva, p))
        else:
            p = np.clip(cur_va + mdl.predict(Xva), 0, None); sc = float(np.mean(np.abs(yva - p)))
        m[tag] = {"n_iter": int(mdl.n_iter_), "val_score": round(sc, 4)}
        print(f"h={hm} {tag} iters={mdl.n_iter_} val={sc:.4f} ({time.time()-th:.0f}s)", flush=True)
    m["n_train"] = int(len(Xtr)); m["n_val"] = int(len(Xva)); m["seconds"] = round(time.time()-th)
    metrics["horizons"][str(hm)] = m
metrics["total_seconds"] = round(time.time()-t0)
json.dump(metrics, open(os.path.join(OUT, "metrics.json"), "w"), ensure_ascii=False, indent=1)
print("DONE", metrics["total_seconds"], "s", flush=True)
