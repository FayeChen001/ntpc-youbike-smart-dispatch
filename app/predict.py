"""
predict.py — 載入六個月矩陣 context 與已訓練模型，提供任一回放時點的全站預測。
模型尚未產出時退回「持續值＋日型時段漂移」基準與訓練期零車/零位比例，並在 source 標示。
"""
import os, sys, json, glob
import numpy as np, pandas as pd
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "pipeline"))
from features import FeatureContext, FEATURES, HORIZONS

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
P = f"{ROOT}/data/processed"; M = f"{ROOT}/models"

class Predictor:
    def __init__(self):
        self.st = pd.read_parquet(f"{P}/stations.parquet").sort_values("sid").reset_index(drop=True)
        z = np.load(f"{M}/context.npz", allow_pickle=True)
        bins = pd.DatetimeIndex(z["bins"])
        prof = {"b": z["prof_b"], "s": z["prof_s"], "pe": z["prof_pe"], "pf": z["prof_pf"]}
        stst = {"empty_rate": z["st_empty_rate"], "full_rate": z["st_full_rate"], "mean_b": z["st_mean_b"]}
        self.ctx = FeatureContext(bins, z["B"], z["S"], z["C"], prof, stst, z["A"], z["dcode"])
        self.bins = bins; self.ns = len(self.st)
        # 容量矛盾(隔離)列：只保留 bin, sid 以標示待查
        obs = pd.read_parquet(f"{P}/obs.parquet", columns=["sid", "bin", "valid"])
        inv = obs[~obs["valid"]]
        self.invalid = set(zip(((inv["bin"].values - bins[0].to_datetime64()) / np.timedelta64(30, "m")).astype(int).tolist(), inv["sid"].tolist()))
        del obs
        self.models = {}; self.reload_models()
        self._cache = {}

    def reload_models(self):
        """優先載入 Amazon SageMaker AI 訓練作業產出的模型；沒有才用本機訓練結果。"""
        import joblib
        self.models = {}; self.model_origin = "none"
        sm_dir = f"{M}/sagemaker"
        for use_dir, origin in ((sm_dir, "Amazon SageMaker AI 訓練作業"), (M, "本機訓練")):
            files = glob.glob(f"{use_dir}/hgb_h*_*.joblib")
            if not files: continue
            loaded = {}
            try:
                for f in files:
                    hm, tag = os.path.basename(f)[4:-7].split("_", 1)
                    loaded[(int(hm[1:]), tag)] = joblib.load(f)
            except Exception as e:
                print(f"[predict] 無法載入 {origin} 的模型（{type(e).__name__}），改用下一個來源", flush=True); continue
            self.models = loaded; self.model_origin = origin; break
        try:
            self.sm_metrics = json.load(open(f"{sm_dir}/metrics.json")) if use_dir == sm_dir else None
        except Exception: self.sm_metrics = None
        self.model_horizons = sorted({h for h, _ in self.models if all((h, t) in self.models for t in ["reg_bikes", "reg_spaces", "cls_empty", "cls_full"])})
        return self.model_horizons

    def t_index(self, ts):
        ts = pd.Timestamp(ts).floor("30min")
        i = int((ts - self.bins[0]) / pd.Timedelta(minutes=30))
        return max(0, min(i, len(self.bins) - 1))

    def predict_all(self, t_idx):
        """回傳 DataFrame：每站現況 + 各尺度預測。"""
        if t_idx in self._cache: return self._cache[t_idx]
        c = self.ctx; s = np.arange(self.ns); t = np.full(self.ns, t_idx)
        base = pd.DataFrame({"sid": s, "name": self.st["name"], "district": self.st["district"], "lat": self.st["lat"], "lon": self.st["lon"],
                             "bikes": c.B[t_idx], "spaces": c.S[t_idx], "cap": c.C[t_idx], "flat_run": c.FLAT[t_idx]})
        base["cap"] = base["cap"].fillna(self.st["cap_mode"])
        for hm, h in HORIZONS.items():
            X = c.make_X(t, s, h)
            if hm in self.model_horizons:
                Xv = X.values.astype(np.float32)
                pb = np.clip(np.nan_to_num(X["bikes"].values) + self.models[(hm, "reg_bikes")].predict(Xv), 0, X["cap"].values)
                ps = np.clip(np.nan_to_num(X["spaces"].values) + self.models[(hm, "reg_spaces")].predict(Xv), 0, X["cap"].values)
                pe = self.models[(hm, "cls_empty")].predict_proba(Xv)[:, 1]
                pf = self.models[(hm, "cls_full")].predict_proba(Xv)[:, 1]
                src = "hgb"
            else:
                pb = X["naive_b"].values; ps = X["naive_s"].values
                pe = np.where(X["bikes"].values == 0, np.maximum(0.7, np.nan_to_num(X["prof_pe_h"].values)), np.nan_to_num(X["prof_pe_h"].values))
                pf = np.where(X["spaces"].values == 0, np.maximum(0.7, np.nan_to_num(X["prof_pf_h"].values)), np.nan_to_num(X["prof_pf_h"].values))
                src = "baseline"
            base[f"pb_{hm}"] = np.round(pb, 1); base[f"ps_{hm}"] = np.round(ps, 1)
            base[f"pe_{hm}"] = np.round(pe, 3); base[f"pf_{hm}"] = np.round(pf, 3)
            srcs = base.attrs.setdefault("sources", {}); srcs[hm] = src
        base["source"] = "hgb" if all(v == "hgb" for v in base.attrs["sources"].values()) else ("baseline" if all(v == "baseline" for v in base.attrs["sources"].values()) else "mixed:" + ",".join(f"{k}={v}" for k, v in base.attrs["sources"].items()))
        # 狀態分類（不把停站當缺車）
        st = np.full(self.ns, "normal", dtype=object)
        nodata = base["bikes"].isna().values
        both0 = (base["bikes"].values == 0) & (base["spaces"].values == 0)
        stale = base["flat_run"].values >= 48
        st[(base["bikes"].values == 0) & ~both0] = "empty"
        st[(base["spaces"].values == 0) & ~both0] = "full"
        st[both0] = "both_zero"; st[stale] = "stale_flat"; st[nodata] = "no_data"
        st[[ (t_idx, int(x)) in self.invalid for x in s ]] = "cap_conflict"
        base["status"] = st
        self._cache[t_idx] = base
        if len(self._cache) > 400: self._cache.pop(next(iter(self._cache)))
        return base

    def neighbors(self, sid, radius_m=500):
        A = self.ctx.A
        return np.where(A[sid] > 0)[0].tolist()
