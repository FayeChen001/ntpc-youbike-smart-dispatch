"""
02_train_eval.py — 30/60/120/180 分鐘預測：基準 vs LightGBM；時間切分 1–4 月訓練、5 月驗證/早停、6 月測試。
輸出：models/*.txt、models/context.npz、reports/model_eval.json
注意：六月曾做描述性探索（見交接），此處只做一次最終測試，不依六月調參。
"""
import os, sys, json, time, pickle
import numpy as np, pandas as pd, joblib
from sklearn.metrics import average_precision_score
from sklearn.ensemble import HistGradientBoostingRegressor, HistGradientBoostingClassifier
from sklearn.inspection import permutation_importance
sys.path.insert(0, os.path.dirname(__file__))
from features import *

ROOT = "/Users/chenhongfei/CC/ntpc-youbike"
P = f"{ROOT}/data/processed"; M = f"{ROOT}/models"; R = f"{ROOT}/reports"
os.makedirs(M, exist_ok=True); os.makedirs(R, exist_ok=True)
rng = np.random.default_rng(42)
N_TRAIN_SAMPLE = int(os.environ.get("N_TRAIN", 2_000_000)); N_VAL_SAMPLE = 800_000
t0 = time.time()

obs = pd.read_parquet(f"{P}/obs.parquet"); st = pd.read_parquet(f"{P}/stations.parquet").sort_values("sid")
nbdf = pd.read_parquet(f"{P}/neighbors_800m.parquet")
ns = len(st)
bins, B, S, C = build_matrices(obs, ns, "2026-01-01", "2026-06-30 23:30")
del obs
print("matrices", B.shape, f"{time.time()-t0:.0f}s", flush=True)

TRAIN = (bins < "2026-05-01"); VAL = (bins >= "2026-05-01") & (bins < "2026-06-01"); TEST = (bins >= "2026-06-01")
train_rows = np.where(TRAIN)[0]; val_rows = np.where(VAL)[0]; test_rows = np.where(VAL | TEST)[0]
prof, stst = build_profile(B, S, bins, train_rows)
A = neighbor_adjacency(nbdf, ns, 500)
dcode = pd.Categorical(st["district"]).codes
ctx = FeatureContext(bins, B, S, C, prof, stst, A, dcode)
print("context ready", f"{time.time()-t0:.0f}s", flush=True)

# ---- 先儲存推論用 context（矩陣 + profile + 站統計 + 鄰接），訓練期間即可供 app 使用 ----
np.savez_compressed(f"{M}/context.npz", bins=bins.values.astype("datetime64[m]"), B=B, S=S, C=C,
                    prof_b=prof["b"], prof_s=prof["s"], prof_pe=prof["pe"], prof_pf=prof["pf"],
                    st_empty_rate=stst["empty_rate"], st_full_rate=stst["full_rate"], st_mean_b=stst["mean_b"], A=A, dcode=dcode)
json.dump({"features": FEATURES, "categorical": CATEGORICAL, "horizons": HORIZONS, "regression_target": "delta",
           "district_categories": list(pd.Categorical(st["district"]).categories)}, open(f"{M}/meta.json", "w"), ensure_ascii=False)
print("context saved", f"{time.time()-t0:.0f}s", flush=True)

def cells(rows_mask, h, n_sample=None):
    """回傳 (t, s) 使得 t 與 t+h 都在 rows_mask 內且兩者皆有有效觀測。"""
    T = np.where(rows_mask)[0]; T = T[(T + h < len(bins))]; T = T[rows_mask[np.minimum(T + h, len(bins)-1)]]
    ok = ~np.isnan(B[T]) & ~np.isnan(B[T + h])
    tt, ss = np.where(ok); t = T[tt]; s = ss
    if n_sample and len(t) > n_sample:
        idx = rng.choice(len(t), n_sample, replace=False); t, s = t[idx], s[idx]
    return t, s

def metrics_cls(y, p, cur, k_budget=None):
    y = y.astype(bool); pred = p >= 0.5
    tp = (pred & y).sum(); fp = (pred & ~y).sum(); fn = (~pred & y).sum()
    prec = tp / max(tp + fp, 1); rec = tp / max(tp + fn, 1)
    new = cur > 0                                  # 現在非零、未來為零 = 新事件
    ny = y & new; npred = pred & new
    rec_new = (npred & ny).sum() / max(ny.sum(), 1); prec_new = (npred & ny).sum() / max(npred.sum(), 1)
    try: ap = float(average_precision_score(y, p)) if y.sum() > 0 else None
    except Exception: ap = None
    try: ap_new = float(average_precision_score(y[new], p[new])) if ny.sum() > 0 else None
    except Exception: ap_new = None
    k = int(k_budget or y.sum())
    topk = np.argsort(-p)[:k]; prec_at_k = float(y[topk].mean()) if k > 0 else None
    # 校準（10 分箱）
    q = np.clip((p * 10).astype(int), 0, 9); cal = []
    for b in range(10):
        m = q == b
        if m.sum() > 0: cal.append({"bin": b, "n": int(m.sum()), "mean_pred": float(p[m].mean()), "obs_rate": float(y[m].mean())})
    return {"n": int(len(y)), "positives": int(y.sum()), "new_events": int(ny.sum()),
            "precision": float(prec), "recall": float(rec), "f1": float(2*prec*rec/max(prec+rec,1e-9)),
            "pr_auc": ap, "new_event_recall": float(rec_new), "new_event_precision": float(prec_new), "new_event_pr_auc": ap_new,
            "precision_at_k_equal_positives": prec_at_k, "calibration": cal}

def mae(y, p): return float(np.mean(np.abs(y - p)))

names = st["name"].values
seg_mrt = np.array(["捷運" in n for n in names]); seg_school = np.array([any(k in n for k in ["國小","國中","高中","大學","學校","高工","高商"]) for n in names])
def segments(t, s, X):
    slot = ctx.slot[t]; dt = ctx.daytype[t]
    peak = (dt == 0) & (((slot >= 14) & (slot <= 19)) | ((slot >= 34) & (slot <= 39)))  # 07:00–09:30, 17:00–19:30
    return {"all": np.ones(len(t), bool), "mrt": seg_mrt[s], "school": seg_school[s], "weekday_peak": peak,
            "excl_flat24h": X["flat_run"].values < 48}

CAT_IDX = [FEATURES.index(c) for c in CATEGORICAL]
HGB = dict(learning_rate=0.1, max_iter=600, max_leaf_nodes=63, min_samples_leaf=200, l2_regularization=1.0,
           early_stopping=True, validation_fraction=0.1, n_iter_no_change=25, categorical_features=CAT_IDX, random_state=42)
# 早停用訓練期(1–4月)內部隨機 10%；5 月僅用於報告門檻/校準；6 月一次最終測試。

report = {"split": {"train": "2026-01-01..04-30", "val": "2026-05", "test": "2026-06"}, "n_train_sample": N_TRAIN_SAMPLE,
          "features": FEATURES, "horizons": {}}
for hm, h in HORIZONS.items():
    th = time.time()
    t_tr, s_tr = cells(TRAIN, h, N_TRAIN_SAMPLE); t_va, s_va = cells(VAL, h, N_VAL_SAMPLE); t_te, s_te = cells(TEST, h)
    Xtr = ctx.make_X(t_tr, s_tr, h); Xva = ctx.make_X(t_va, s_va, h); Xte = ctx.make_X(t_te, s_te, h)
    yB_tr, yB_va, yB_te = B[t_tr+h, s_tr], B[t_va+h, s_va], B[t_te+h, s_te]
    yS_tr, yS_va, yS_te = S[t_tr+h, s_tr], S[t_va+h, s_va], S[t_te+h, s_te]
    print(f"h={hm} train {len(Xtr):,} val {len(Xva):,} test {len(Xte):,}  ({time.time()-th:.0f}s)", flush=True)
    out = {"n_train": int(len(Xtr)), "n_val": int(len(Xva)), "n_test": int(len(Xte)), "models": {}, "baselines": {}}
    segs = segments(t_te, s_te, Xte)
    cur_b = Xte["bikes"].values; cur_s = Xte["spaces"].values

    # ---- 基準 ----
    base = {
        "persistence": {"b": cur_b, "s": cur_s, "pe": (cur_b == 0).astype(float), "pf": (cur_s == 0).astype(float)},
        "profile": {"b": np.nan_to_num(Xte["prof_b_h"].values, nan=0), "s": np.nan_to_num(Xte["prof_s_h"].values, nan=0),
                    "pe": np.nan_to_num(Xte["prof_pe_h"].values, nan=0), "pf": np.nan_to_num(Xte["prof_pf_h"].values, nan=0)},
        "naive_drift": {"b": Xte["naive_b"].values, "s": Xte["naive_s"].values,
                        "pe": (Xte["naive_b"].values <= 0.5).astype(float), "pf": (Xte["naive_s"].values <= 0.5).astype(float)},
    }
    for bn, bv in base.items():
        out["baselines"][bn] = {seg: {"mae_bikes": mae(yB_te[m], bv["b"][m]), "mae_spaces": mae(yS_te[m], bv["s"][m]),
                                      "empty": metrics_cls(yB_te[m] == 0, bv["pe"][m], cur_b[m]),
                                      "full": metrics_cls(yS_te[m] == 0, bv["pf"][m], cur_s[m])} for seg, m in segs.items()}
        # 校準表只保留 all
        for seg in segs:
            if seg != "all":
                out["baselines"][bn][seg]["empty"].pop("calibration"); out["baselines"][bn][seg]["full"].pop("calibration")

    # ---- 梯度提升樹(sklearn HGB)：回歸(車/位) + 分類(零車/零位) ----
    preds = {}
    for tag, ytr, yva, yte, is_cls in [
        ("reg_bikes", yB_tr - Xtr["bikes"].values, yB_va, yB_te, False), ("reg_spaces", yS_tr - Xtr["spaces"].values, yS_va, yS_te, False),
        ("cls_empty", (yB_tr == 0).astype(int), (yB_va == 0).astype(int), (yB_te == 0).astype(int), True),
        ("cls_full", (yS_tr == 0).astype(int), (yS_va == 0).astype(int), (yS_te == 0).astype(int), True)]:
        mdl = (HistGradientBoostingClassifier(**HGB) if is_cls else HistGradientBoostingRegressor(**HGB))
        mdl.fit(Xtr.values.astype(np.float32), ytr)
        joblib.dump(mdl, f"{M}/hgb_h{hm}_{tag}.joblib")
        cur_col = "bikes" if "bikes" in tag else "spaces"
        p = mdl.predict_proba(Xte.values.astype(np.float32))[:, 1] if is_cls else np.clip(Xte[cur_col].values + mdl.predict(Xte.values.astype(np.float32)), 0, Xte["cap"].values)
        preds[tag] = p
        pv = mdl.predict_proba(Xva.values.astype(np.float32))[:, 1] if is_cls else np.clip(Xva[cur_col].values + mdl.predict(Xva.values.astype(np.float32)), 0, Xva["cap"].values)
        val_score = float(average_precision_score(yva, pv)) if is_cls else mae(yva, pv)
        sub = rng.choice(len(Xva), 20000, replace=False)
        y_pi = yva[sub] if is_cls else (yva[sub] - Xva[cur_col].values[sub])
        pi = permutation_importance(mdl, Xva.values[sub].astype(np.float32), y_pi, n_repeats=1, random_state=0,
                                    scoring="average_precision" if is_cls else "neg_mean_absolute_error")
        imp = dict(zip(FEATURES, pi.importances_mean.round(4).tolist()))
        out["models"][tag] = {"n_iter": int(mdl.n_iter_), "val_score_may": val_score, "top_features": sorted(imp.items(), key=lambda x: -x[1])[:10]}
        print(f"  {tag} iters={mdl.n_iter_} valMay={val_score:.4f} ({time.time()-th:.0f}s)", flush=True)
    out["hgb"] = {}
    for seg, m in segs.items():
        out["hgb"][seg] = {"mae_bikes": mae(yB_te[m], preds["reg_bikes"][m]), "mae_spaces": mae(yS_te[m], preds["reg_spaces"][m]),
                                "empty": metrics_cls(yB_te[m] == 0, preds["cls_empty"][m], cur_b[m]),
                                "full": metrics_cls(yS_te[m] == 0, preds["cls_full"][m], cur_s[m])}
        if seg != "all":
            out["hgb"][seg]["empty"].pop("calibration"); out["hgb"][seg]["full"].pop("calibration")
    report["horizons"][str(hm)] = out
    json.dump(report, open(f"{R}/model_eval.json", "w"), ensure_ascii=False, indent=1)
    a = out["hgb"]["all"]; pb = out["baselines"]["persistence"]["all"]; nd = out["baselines"]["naive_drift"]["all"]
    print(f"== h={hm}min  MAE bikes: persist {pb['mae_bikes']:.3f} | naive_drift {nd['mae_bikes']:.3f} | HGB {a['mae_bikes']:.3f}"
          f"   empty new-event recall: persist {pb['empty']['new_event_recall']:.3f} | HGB {a['empty']['new_event_recall']:.3f}"
          f"  PR-AUC(new) HGB {a['empty']['new_event_pr_auc']}  ({time.time()-th:.0f}s)", flush=True)

print("ALL DONE", f"{time.time()-t0:.0f}s")
