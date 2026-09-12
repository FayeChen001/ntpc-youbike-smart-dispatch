"""
03_utilization.py — 使用率與調度優化分析。
核心問題：怎麼調度才能讓車輛使用率最大化。
界線：快照庫存差不是真實借還需求。零車時想借的人不會出現在資料裡（右設限）。
      同一個 30 分鐘區間內的一借一還會互相抵銷，因此觀測到的流量是「下界」。
"""
import os, sys, json, time
import numpy as np, pandas as pd
sys.path.insert(0, os.path.join(os.path.dirname(__file__)))
from features import build_matrices, calendar

ROOT = "/Users/chenhongfei/CC/ntpc-youbike"
P = f"{ROOT}/data/processed"; R = f"{ROOT}/reports"
t0 = time.time()
obs = pd.read_parquet(f"{P}/obs.parquet")
st = pd.read_parquet(f"{P}/stations.parquet").sort_values("sid").reset_index(drop=True)
nb = pd.read_parquet(f"{P}/neighbors_800m.parquet")
ns = len(st)
out = {}

# ---------------- 1. 逐檔剖析 ----------------
ing = json.load(open(f"{P}/ingest_log.json"))
per_file = []
for i, f in enumerate(ing["files"]):
    m = obs["src"].values == i
    o = obs[m]
    per_file.append({"檔名": f["file"], "編碼": f["encoding"], "原始筆數": f["rows"], "採用筆數": int(m.sum()),
                     "起": str(o["ts"].min()), "迄": str(o["ts"].max()), "站點數": int(o["sid"].nunique()),
                     "容量矛盾隔離": int((~o["valid"]).sum()), "缺行政區(已連結)": f["null_district"]})
out["per_file"] = per_file
print("per-file done", round(time.time()-t0), flush=True)

bins, B, S, C = build_matrices(obs, ns, "2026-01-01", "2026-06-30 23:30")
slot, wd, daytype = calendar(bins)
del obs
print("matrices", B.shape, round(time.time()-t0), flush=True)

# ---------------- 2. 觀測流量（下界）與使用率代理 ----------------
D = np.diff(B, axis=0)                      # t→t+1 的庫存變化
valid_pair = ~np.isnan(B[:-1]) & ~np.isnan(B[1:])
D = np.where(valid_pair, D, np.nan)
outflow = np.nansum(np.where(D < 0, -D, 0), axis=0)      # 觀測淨借出（下界）
inflow = np.nansum(np.where(D > 0, D, 0), axis=0)        # 觀測淨還入（下界）
nobs = np.sum(~np.isnan(B), axis=0)
days = np.maximum(nobs / 48.0, 1)
cap = np.nan_to_num(np.nanmedian(C, axis=0), nan=0)
cap = np.where(cap <= 0, st["cap_mode"].values, cap)
mean_bikes = np.nanmean(B, axis=0)
zero_bike = np.nansum(B == 0, axis=0); zero_dock = np.nansum(S == 0, axis=0)
both_zero = np.nansum((B == 0) & (S == 0), axis=0)

flow_per_day = (outflow + inflow) / days
turn_per_dock = flow_per_day / np.maximum(cap, 1)                 # 每柱每日周轉（下界）
turn_per_bike = flow_per_day / np.maximum(mean_bikes, 0.5)        # 每輛車每日周轉（下界）
idle_bike_hours = mean_bikes * (nobs / 2.0)                       # 車 × 小時

ut = pd.DataFrame({"sid": st.sid, "district": st.district, "name": st.name, "lat": st.lat, "lon": st.lon,
                   "cap": cap.round(0), "n_obs": nobs, "days": days.round(1), "mean_bikes": mean_bikes.round(2),
                   "outflow_day": (outflow / days).round(2), "inflow_day": (inflow / days).round(2),
                   "flow_day": flow_per_day.round(2), "turn_per_dock": turn_per_dock.round(3),
                   "turn_per_bike": turn_per_bike.round(3),
                   "pct_zero_bike": (100 * zero_bike / np.maximum(nobs, 1)).round(2),
                   "pct_zero_dock": (100 * zero_dock / np.maximum(nobs, 1)).round(2),
                   "pct_both_zero": (100 * both_zero / np.maximum(nobs, 1)).round(2)})
ut["net_drift_day"] = ((inflow - outflow) / days).round(2)
ut.to_csv(f"{R}/station_utilization.csv", index=False)

active = ut[(ut.pct_both_zero < 50) & (ut.cap > 0)]
out["city"] = {
    "stations": int(ns), "active_stations": int(len(active)),
    "total_docks": int(cap.sum()), "mean_bikes_citywide": round(float(np.nansum(mean_bikes)), 0),
    "observed_flow_per_day_lower_bound": round(float(np.nansum(flow_per_day)), 0),
    "median_turn_per_dock": round(float(active.turn_per_dock.median()), 3),
    "median_turn_per_bike": round(float(active.turn_per_bike.median()), 3),
    "note": "flow 為觀測庫存變化的絕對值加總，同區間一借一還會抵銷，故為真實借還量的下界。"
}
print("utilization done", round(time.time()-t0), flush=True)

# ---------------- 3. 閒置車輛：低周轉高庫存 ----------------
a = active.copy()
a["idle_score"] = a["mean_bikes"] / np.maximum(a["flow_day"], 0.1)     # 每日流量需要幾輛庫存支撐
harvest = a[(a.turn_per_bike < a.turn_per_bike.quantile(0.25)) & (a.mean_bikes >= 5)].nlargest(40, "mean_bikes")
harvest["surplus_est"] = (harvest.mean_bikes - np.maximum(2, harvest.outflow_day * 1.5)).round(1)
harvest = harvest[harvest.surplus_est > 0]
out["harvest_candidates"] = harvest[["sid","district","name","cap","mean_bikes","flow_day","turn_per_bike","surplus_est","pct_zero_bike"]].head(25).to_dict("records")
out["harvest_total_surplus"] = round(float(harvest.surplus_est.sum()), 0)

starved = a[(a.pct_zero_bike >= 10) & (a.outflow_day >= 3)].nlargest(40, "outflow_day")
out["starved_stations"] = starved[["sid","district","name","cap","mean_bikes","outflow_day","pct_zero_bike","turn_per_bike"]].head(25).to_dict("records")
print("idle/starved done", round(time.time()-t0), flush=True)

# ---------------- 4. 缺車時數與可回收服務量 ----------------
# 零車時段中，屬於高需求時段者才是「真的損失機會」
hour = (slot // 2)
demand_by_slot = np.zeros(48)
for s_ in range(48):
    m = slot == s_
    demand_by_slot[s_] = np.nansum(np.where(D[m[:-1]] < 0, -D[m[:-1]], 0))
peak_slots = set(np.argsort(-demand_by_slot)[:16].tolist())
is_peak = np.array([s_ in peak_slots for s_ in slot])
zero_peak = np.nansum((B == 0) & is_peak[:, None], axis=0)
ut["zero_bike_peak_halfhours_per_day"] = (zero_peak / days).round(2)
out["lost_service"] = {
    "zero_bike_halfhours_total": int(np.nansum(B == 0)),
    "zero_bike_halfhours_in_peak": int(zero_peak.sum()),
    "peak_slots_local_time": sorted([f"{s_//2:02d}:{'30' if s_%2 else '00'}" for s_ in peak_slots]),
    "dock_hours_wasted_by_both_zero": round(float((np.nansum((B == 0) & (S == 0), axis=0) * cap).sum() / 2), 0),
    "note": "零車時段不等於失敗旅次；想借而未借到的人不會出現在資料中。"
}

# ---------------- 5. 分流可行性：缺車時鄰站是否有車 ----------------
A500 = np.zeros((ns, ns), np.float32)
nb5 = nb[nb.dist_m <= 500]
A500[nb5.sid.values, nb5.nsid.values] = 1
Bf = np.nan_to_num(B)
NBB = Bf @ A500
empty_mask = (B == 0)
cover = (NBB >= 3) & empty_mask
per_station_cover = cover.sum(axis=0) / np.maximum(empty_mask.sum(axis=0), 1)
ut["neighbor_cover_pct"] = (100 * per_station_cover).round(1)
ut["n_neighbors_500m"] = A500.sum(axis=1).astype(int)
tot_empty = int(empty_mask.sum()); tot_cover = int(cover.sum())
out["diversion"] = {
    "empty_observations": tot_empty, "with_neighbor_ge3_bikes": tot_cover,
    "coverage_pct": round(100 * tot_cover / max(tot_empty, 1), 1),
    "stations_with_no_neighbor_500m": int((A500.sum(axis=1) == 0).sum()),
    "note": "鄰站當下有車只代表候選資源存在，不等於使用者走得到、走到還有車。"
}

# ---------------- 6. 結構性淨流：每天需要搬回去的量 ----------------
ut_act = ut[ut.sid.isin(active.sid)]
drift = ut_act.groupby("district").agg(net_drift_day=("net_drift_day","sum"), stations=("sid","size")).round(1).sort_values("net_drift_day")
out["district_net_drift"] = drift.reset_index().to_dict("records")
struct = ut_act.reindex(ut_act.net_drift_day.abs().sort_values(ascending=False).index)
out["structural_imbalance_top"] = struct[["sid","district","name","cap","net_drift_day","outflow_day","inflow_day","pct_zero_bike","pct_zero_dock"]].head(25).to_dict("records")
out["citywide_daily_rebalance_lower_bound"] = round(float(ut_act[ut_act.net_drift_day < 0].net_drift_day.abs().sum()), 0)

# ---------------- 7. 車柱配置診斷 ----------------
ut_act = ut_act.copy()
ut_act["dock_productivity"] = ut_act.turn_per_dock
over = ut_act[(ut_act.cap >= 25) & (ut_act.dock_productivity < ut_act.dock_productivity.quantile(0.15))].nlargest(20, "cap")
under = ut_act[(ut_act.pct_zero_dock >= 8) & (ut_act.dock_productivity > ut_act.dock_productivity.quantile(0.75))].nlargest(20, "pct_zero_dock")
out["oversized_docks"] = over[["sid","district","name","cap","mean_bikes","flow_day","dock_productivity","pct_zero_bike"]].to_dict("records")
out["undersized_docks"] = under[["sid","district","name","cap","flow_day","dock_productivity","pct_zero_dock"]].to_dict("records")

# ---------------- 8. 時段淨流（全市） ----------------
rows = []
for s_ in range(48):
    m = slot[:-1] == s_
    wk = m & (daytype[:-1] == 0)
    rows.append({"slot": f"{s_//2:02d}:{'30' if s_%2 else '00'}",
                 "weekday_outflow": round(float(np.nansum(np.where(D[wk] < 0, -D[wk], 0))), 0),
                 "weekday_inflow": round(float(np.nansum(np.where(D[wk] > 0, D[wk], 0))), 0)})
out["hourly_flow"] = rows
ut.to_csv(f"{R}/station_utilization.csv", index=False)
json.dump(out, open(f"{R}/utilization_analysis.json", "w"), ensure_ascii=False, indent=1, default=str)
print("ALL DONE", round(time.time()-t0), "s")
