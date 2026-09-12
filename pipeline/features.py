"""
features.py — 訓練與線上推論共用的特徵建構。
資料以「半小時分箱 × 站點」矩陣表示：B=可借、S=可還、C=總車柱；NaN=無有效觀測。
所有特徵只用 t 時點(含)以前的觀測與日曆資訊；t+h 的日曆(時段/假日)可預先知道，允許使用。
"""
import numpy as np, pandas as pd

STEP_MIN = 30
HORIZONS = {30: 1, 60: 2, 120: 4, 180: 6}          # 分鐘 → 步數
# 2026 年台灣國定假日近似清單（含彈性放假），未逐一核對官方行事曆；週六日另以 weekday 判定
TW_HOLIDAYS_2026 = pd.to_datetime([
    "2026-01-01", "2026-02-16", "2026-02-17", "2026-02-18", "2026-02-19", "2026-02-20",
    "2026-02-27", "2026-04-03", "2026-04-06", "2026-05-01", "2026-06-19",
]).normalize()

FEATURES = [
    "bikes","spaces","cap","fill",
    "lag1_b","lag2_b","lag4_b","lag1_s","lag2_s",
    "d1_b","d2_b","d4_b","d1_s",
    "slot","wd","daytype","slot_h","daytype_h",
    "prof_b_now","prof_b_h","prof_s_now","prof_s_h","prof_pe_h","prof_pf_h","prof_drift_b","prof_drift_s",
    "naive_b","naive_s",
    "st_empty_rate","st_full_rate","st_mean_b",
    "nb_b","nb_s","nb_n","nb_fill",
    "flat_run","district",
]
CATEGORICAL = ["district"]

def calendar(bins):
    bins = pd.DatetimeIndex(bins)
    slot = (bins.hour * 2 + bins.minute // 30).values.astype(np.int16)
    wd = bins.weekday.values.astype(np.int8)
    hol = np.isin(bins.normalize().values, TW_HOLIDAYS_2026.values)
    daytype = ((wd >= 5) | hol).astype(np.int8)     # 0=工作日 1=週末/假日
    return slot, wd, daytype

def build_matrices(obs, n_stations, start=None, end=None):
    start = pd.Timestamp(start) if start is not None else obs["bin"].min().floor("D")
    end = pd.Timestamp(end) if end is not None else obs["bin"].max()
    bins = pd.date_range(start, end, freq="30min")
    nb = len(bins)
    v = obs["valid"].values
    o = obs[v]
    bidx = ((o["bin"].values - bins[0].to_datetime64()) / np.timedelta64(STEP_MIN, "m")).astype(int)
    ok = (bidx >= 0) & (bidx < nb)
    B = np.full((nb, n_stations), np.nan, np.float32)
    S = np.full((nb, n_stations), np.nan, np.float32)
    C = np.full((nb, n_stations), np.nan, np.float32)
    sid = o["sid"].values
    B[bidx[ok], sid[ok]] = o["bikes"].values[ok]
    S[bidx[ok], sid[ok]] = o["spaces"].values[ok]
    C[bidx[ok], sid[ok]] = o["cap"].values[ok]
    return bins, B, S, C

def build_profile(B, S, bins, train_rows):
    """訓練期的 (日型, 時段, 站) 平均可借/可還與零車/零位比例。"""
    slot, wd, daytype = calendar(bins)
    ns = B.shape[1]
    prof = {k: np.full((2, 48, ns), np.nan, np.float32) for k in ["b","s","pe","pf"]}
    rows = np.zeros(len(bins), bool); rows[train_rows] = True
    for dt in (0, 1):
        for sl in range(48):
            r = rows & (daytype == dt) & (slot == sl)
            if r.sum() == 0: continue
            b = B[r]; s = S[r]
            with np.errstate(all="ignore"):
                prof["b"][dt, sl] = np.nanmean(b, axis=0)
                prof["s"][dt, sl] = np.nanmean(s, axis=0)
                prof["pe"][dt, sl] = np.nanmean((b == 0).astype(np.float32) + np.where(np.isnan(b), np.nan, 0), axis=0)
                prof["pf"][dt, sl] = np.nanmean((s == 0).astype(np.float32) + np.where(np.isnan(s), np.nan, 0), axis=0)
    with np.errstate(all="ignore"):
        st = {
            "empty_rate": np.nanmean((B[rows] == 0).astype(np.float32) + np.where(np.isnan(B[rows]), np.nan, 0), axis=0),
            "full_rate": np.nanmean((S[rows] == 0).astype(np.float32) + np.where(np.isnan(S[rows]), np.nan, 0), axis=0),
            "mean_b": np.nanmean(B[rows], axis=0),
        }
    return prof, st

def neighbor_adjacency(neighbors_df, n_stations, radius_m=500):
    A = np.zeros((n_stations, n_stations), np.float32)
    nb = neighbors_df[neighbors_df["dist_m"] <= radius_m]
    A[nb["sid"].values, nb["nsid"].values] = 1.0
    return A

def neighbor_sums(B, S, C, A):
    Bf = np.nan_to_num(B); Sf = np.nan_to_num(S); Cf = np.nan_to_num(C)
    N = (~np.isnan(B)).astype(np.float32)
    return Bf @ A, Sf @ A, N @ A, Cf @ A

def flat_runs(B, S):
    """連續與前一步相同(且有觀測)的步數，上限 96(48小時)。"""
    F = np.zeros_like(B, dtype=np.float32)
    for t in range(1, B.shape[0]):
        same = (B[t] == B[t-1]) & (S[t] == S[t-1])
        F[t] = np.where(same, np.minimum(F[t-1] + 1, 96), 0)
    return F

class FeatureContext:
    """把所有矩陣與統計打包，供訓練與推論共用。"""
    def __init__(self, bins, B, S, C, prof, st, A, district_code):
        self.bins = pd.DatetimeIndex(bins); self.B, self.S, self.C = B, S, C
        self.prof, self.st, self.A = prof, st, A
        self.district_code = district_code.astype(np.int16)
        self.slot, self.wd, self.daytype = calendar(self.bins)
        self.NB_B, self.NB_S, self.NB_N, self.NB_C = neighbor_sums(B, S, C, A)
        self.FLAT = flat_runs(B, S)

    def make_X(self, t, s, h):
        t = np.asarray(t); s = np.asarray(s)
        B, S, C = self.B, self.S, self.C
        nb = B.shape[0]
        def at(M, tt):
            ok = (tt >= 0) & (tt < nb)
            out = np.full(len(tt), np.nan, np.float32)
            out[ok] = M[tt[ok], s[ok]]
            return out
        f = {}
        f["bikes"] = at(B, t); f["spaces"] = at(S, t); f["cap"] = at(C, t)
        with np.errstate(all="ignore"):
            f["fill"] = f["bikes"] / f["cap"]
        for k in (1, 2, 4):
            f[f"lag{k}_b"] = at(B, t - k)
        f["lag1_s"] = at(S, t - 1); f["lag2_s"] = at(S, t - 2)
        f["d1_b"] = f["bikes"] - f["lag1_b"]; f["d2_b"] = f["bikes"] - f["lag2_b"]; f["d4_b"] = f["bikes"] - f["lag4_b"]
        f["d1_s"] = f["spaces"] - f["lag1_s"]
        th = np.clip(t + h, 0, nb - 1)
        f["slot"] = self.slot[t]; f["wd"] = self.wd[t]; f["daytype"] = self.daytype[t]
        f["slot_h"] = self.slot[th]; f["daytype_h"] = self.daytype[th]
        P = self.prof
        f["prof_b_now"] = P["b"][f["daytype"], f["slot"], s]; f["prof_b_h"] = P["b"][f["daytype_h"], f["slot_h"], s]
        f["prof_s_now"] = P["s"][f["daytype"], f["slot"], s]; f["prof_s_h"] = P["s"][f["daytype_h"], f["slot_h"], s]
        f["prof_pe_h"] = P["pe"][f["daytype_h"], f["slot_h"], s]; f["prof_pf_h"] = P["pf"][f["daytype_h"], f["slot_h"], s]
        f["prof_drift_b"] = f["prof_b_h"] - f["prof_b_now"]; f["prof_drift_s"] = f["prof_s_h"] - f["prof_s_now"]
        with np.errstate(all="ignore"):
            f["naive_b"] = np.clip(f["bikes"] + np.nan_to_num(f["prof_drift_b"]), 0, f["cap"])
            f["naive_s"] = np.clip(f["spaces"] + np.nan_to_num(f["prof_drift_s"]), 0, f["cap"])
        f["st_empty_rate"] = self.st["empty_rate"][s]; f["st_full_rate"] = self.st["full_rate"][s]; f["st_mean_b"] = self.st["mean_b"][s]
        f["nb_b"] = at(self.NB_B, t); f["nb_s"] = at(self.NB_S, t); f["nb_n"] = at(self.NB_N, t)
        with np.errstate(all="ignore"):
            f["nb_fill"] = f["nb_b"] / at(self.NB_C, t)
        f["flat_run"] = at(self.FLAT, t)
        f["district"] = self.district_code[s]
        X = pd.DataFrame(f)[FEATURES]
        return X
