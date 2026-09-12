"""
05_verify_public.py — 把我們算出來的數字，逐項對照公開資料（docs/DATA_SOURCES.md 的 B 類）。
任何一項超出容許誤差就以非零碼結束，讓閉環驗收抓得到。
重跑：python3 pipeline/05_verify_public.py
"""
import json, os, sys
import numpy as np, pandas as pd

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
P = os.path.join(ROOT, "data", "processed")

# ---- 公開資料（B 類），改這裡就等於改對照基準 ----
PUBLIC = {
    "stations_jun": 1576,                       # B1
    "bikes_total": 23479,                       # B2 = 21379 + 2100
    "district_stations": {"板橋區": 224, "新莊區": 140, "土城區": 102, "三重區": 140, "新店區": 127, "中和區": 118},  # B4（2026-09-11，晚於資料期間）
    "avail_rate_peak": 93.0,                    # B6 見車率
    "dock_rate_peak": 99.0,                     # B6 見位率
    "avail_rate_target": 90.0,                  # B7 合約目標
    "dispatch_trucks": 40,                      # B5/B7
    "dispatch_staff": 350,                      # B7
    "relay_start": "2026-01-01", "relay_end": "2026-06-30",   # B10 友愛接力試辦期
}
TOL = {"stations": 0, "district_stations": 3, "rate": 1.5}

res, fails = [], []
def check(key, got, want, tol, unit="", note=""):
    ok = abs(got - want) <= tol
    res.append({"項目": key, "我們算出": round(got, 2), "公開數字": want, "容許誤差": tol, "單位": unit, "通過": ok, "備註": note})
    if not ok: fails.append(key)
    return ok

print("載入資料 ...", flush=True)
st = pd.read_parquet(os.path.join(P, "stations.parquet"))
ob = pd.read_parquet(os.path.join(P, "obs.parquet"))
ob = ob[ob.valid].copy()
ob["ts"] = pd.to_datetime(ob.ts)

# A1 六月站點數
jun = ob[(ob.ts >= "2026-06-01") & (ob.ts < "2026-07-01")]
check("A1 六月站點數", jun.sid.nunique(), PUBLIC["stations_jun"], TOL["stations"], "站")

# A2 各區站點數
for d, want in PUBLIC["district_stations"].items():
    got = int((st.district == d).sum())
    check(f"A2 {d}站數", got, want, TOL["district_stations"], "站", "公開數字為 2026-09-11，晚於資料期間")

# A3/A4 工作日早尖峰見車率／見位率
j = jun.merge(st[["sid", "district"]], on="sid", how="left")
j["wd"] = j.ts.dt.weekday; j["h"] = j.ts.dt.hour + j.ts.dt.minute / 60
am = j[(j.wd < 5) & (j.h >= 7) & (j.h < 9.5)]
g = am.groupby("ts").agg(n=("sid", "size"), avail=("bikes", lambda s: (s > 0).sum()), dock=("spaces", lambda s: (s > 0).sum()))
avail_peak = float(100 * (g.avail / g.n).mean()); dock_peak = float(100 * (g.dock / g.n).mean())
check("A3 早尖峰見車率", avail_peak, PUBLIC["avail_rate_peak"], TOL["rate"], "%")
check("A4 早尖峰見位率", dock_peak, PUBLIC["dock_rate_peak"], TOL["rate"], "%")

# A5 全日
ga = jun.groupby("ts").agg(n=("sid", "size"), avail=("bikes", lambda s: (s > 0).sum()), dock=("spaces", lambda s: (s > 0).sum()), bikes=("bikes", "sum"))
avail_all = float(100 * (ga.avail / ga.n).mean()); dock_all = float(100 * (ga.dock / ga.n).mean()); in_dock = float(ga.bikes.mean())

# A6 分區早尖峰見車率（判定是否達 90% 合約目標）
by_d = {}
for d in PUBLIC["district_stations"]:
    sub = am[am.district == d]
    gg = sub.groupby("ts").agg(n=("sid", "size"), avail=("bikes", lambda s: (s > 0).sum()), dock=("spaces", lambda s: (s > 0).sum()))
    by_d[d] = {"見車率": round(float(100 * (gg.avail / gg.n).mean()), 2), "見位率": round(float(100 * (gg.dock / gg.n).mean()), 2),
               "站數": int(sub.sid.nunique()), "達標": bool(float(100 * (gg.avail / gg.n).mean()) >= PUBLIC["avail_rate_target"])}

# A8 在柱率
in_dock_pct = 100 * in_dock / PUBLIC["bikes_total"]

# A12/A13 缺車時 500m 內鄰站是否有車（工作日早尖峰）
nb = pd.read_parquet(os.path.join(P, "neighbors_800m.parquet"))
nb5 = nb[nb.dist_m <= 500]
amb = am[["sid", "ts", "bikes"]]
empty = amb[amb.bikes <= 0][["sid", "ts"]]
look = amb.set_index(["ts", "sid"]).bikes
pairs = empty.merge(nb5[["sid", "nsid"]], on="sid", how="left")
pairs["nb_bikes"] = look.reindex(pd.MultiIndex.from_arrays([pairs.ts.values, pairs.nsid.values])).values
agg = pairs.groupby(["ts", "sid"]).nb_bikes.agg(n_obs="count", n_ok=lambda s: (s > 0).sum())
tot = len(agg)
cover_pct = float(100 * (agg.n_ok > 0).sum() / max(tot, 1))
stranded_pct = float(100 * ((agg.n_ok == 0).sum()) / max(tot, 1))

# 友愛接力試辦期是否完整落在資料期間（B10）
data_start, data_end = str(ob.ts.min())[:10], str(ob.ts.max())[:10]
relay_covered = data_start <= PUBLIC["relay_start"] and data_end >= PUBLIC["relay_end"]
res.append({"項目": "B10 友愛接力試辦期完整落在資料期間", "我們算出": f"{data_start}~{data_end}", "公開數字": f"{PUBLIC['relay_start']}~{PUBLIC['relay_end']}",
            "容許誤差": "-", "單位": "", "通過": bool(relay_covered), "備註": "見車率已含分流效果"})
if not relay_covered: fails.append("B10")

# ---- 全市盤面：29 區的見車率、缺口、與 40 車 350 人的配置換算 ----
def rate_by_district(df):
    g = df.groupby(["district", "ts"]).agg(n=("sid", "size"), a=("bikes", lambda s: (s > 0).sum()), k=("spaces", lambda s: (s > 0).sum()))
    r = g.assign(av=100 * g.a / g.n, dk=100 * g.k / g.n).groupby("district")[["av", "dk"]].mean()
    return r
pm = j[(j.wd < 5) & (j.h >= 17) & (j.h < 19.5)]
r_am, r_pm = rate_by_district(am), rate_by_district(pm)

dp = json.load(open(os.path.join(ROOT, "reports", "dispatch_plan.json")))["prepositioning"]
K_AM, K_PM = "早尖峰 07:00-09:30", "晚尖峰 17:00-19:30"
def_am = {x["district"]: x["deficit"] for x in dp[K_AM]["by_district"]}
sur_am = {x["district"]: x["surplus"] for x in dp[K_AM]["by_district"]}
def_pm = {x["district"]: x["deficit"] for x in dp[K_PM]["by_district"]}
sur_pm = {x["district"]: x["surplus"] for x in dp[K_PM]["by_district"]}

def largest_remainder(weights, total):
    """依權重分配整數資源，用最大餘數法，總和剛好等於 total。"""
    tw = sum(weights.values())
    if tw <= 0: return {k: 0 for k in weights}
    raw = {k: v / tw * total for k, v in weights.items()}
    base = {k: int(np.floor(v)) for k, v in raw.items()}
    left = total - sum(base.values())
    for k, _ in sorted(raw.items(), key=lambda kv: kv[1] - np.floor(kv[1]), reverse=True)[:left]:
        base[k] += 1
    return base

TRUCKS, STAFF = PUBLIC["dispatch_trucks"], PUBLIC["dispatch_staff"]
alloc_am = largest_remainder(def_am, TRUCKS)
alloc_pm = largest_remainder(def_pm, TRUCKS)

# C7 情境假設：350 人的職務拆分（公開資料只有總數）
CREW_PER_TRUCK, REPAIR_STAFF = 2, 70
crew_staff = TRUCKS * CREW_PER_TRUCK                 # 80
mover_staff = STAFF - crew_staff - REPAIR_STAFF      # 200 人力調度員
assert mover_staff > 0

districts = sorted(set(st.district))
board = []
for d in districts:
    n = int((st.district == d).sum())
    board.append({
        "district": d, "stations": n,
        "avail_am": round(float(r_am.av.get(d, np.nan)), 2) if d in r_am.index else None,
        "dock_am": round(float(r_am.dk.get(d, np.nan)), 2) if d in r_am.index else None,
        "avail_pm": round(float(r_pm.av.get(d, np.nan)), 2) if d in r_pm.index else None,
        "dock_pm": round(float(r_pm.dk.get(d, np.nan)), 2) if d in r_pm.index else None,
        "deficit_am": def_am.get(d, 0.0), "surplus_am": sur_am.get(d, 0.0),
        "deficit_pm": def_pm.get(d, 0.0), "surplus_pm": sur_pm.get(d, 0.0),
        "trucks_am": alloc_am.get(d, 0), "trucks_pm": alloc_pm.get(d, 0),
    })
for b in board:
    b["crew"] = b["trucks_am"] * CREW_PER_TRUCK
    b["movers"] = int(round(mover_staff * (b["deficit_am"] / max(sum(def_am.values()), 1))))
    b["repair"] = int(round(REPAIR_STAFF * (b["stations"] / max(all_stations, 1)))) if (all_stations := int(st.shape[0])) else 0
    b["below_target_am"] = (b["avail_am"] is not None and b["avail_am"] < PUBLIC["avail_rate_target"])
    b["below_target_pm"] = (b["avail_pm"] is not None and b["avail_pm"] < PUBLIC["avail_rate_target"])

focus = ["板橋區", "新莊區", "土城區"]
focus_stations = int(st[st.district.isin(focus)].shape[0]); all_stations = int(st.shape[0])
focus_def_am = sum(def_am.get(d, 0) for d in focus); tot_def_am = sum(def_am.values())
focus_def_pm = sum(def_pm.get(d, 0) for d in focus); tot_def_pm = sum(def_pm.values())

baseline = {
    "generated_by": "pipeline/05_verify_public.py", "data_window": f"{data_start}~{data_end}",
    "public": PUBLIC,
    "city": {
        "stations": all_stations, "docks": int(st.cap_mode.sum()),
        "avail_am": round(avail_peak, 2), "dock_am": round(dock_peak, 2),
        "avail_all": round(avail_all, 2), "dock_all": round(dock_all, 2),
        "bikes_in_dock": round(in_dock), "bikes_in_dock_pct": round(in_dock_pct, 1),
        "deficit_am": tot_def_am, "deficit_pm": tot_def_pm,
        "trips_am": dp[K_AM]["trips_needed_cap20"], "trips_pm": dp[K_PM]["trips_needed_cap20"],
        "stations_deficit_am": dp[K_AM]["stations_with_deficit"], "stations_deficit_pm": dp[K_PM]["stations_with_deficit"],
        "surplus_am": dp[K_AM]["total_surplus_bikes"], "surplus_pm": dp[K_PM]["total_surplus_bikes"],
        "neighbor_cover_am_pct": round(cover_pct, 1), "stranded_am_pct": round(stranded_pct, 1),
        "empty_obs_am": int(tot),
    },
    "staffing_assumption": {"crew_per_truck": CREW_PER_TRUCK, "crew_total": crew_staff, "movers_total": mover_staff,
                            "repair_total": REPAIR_STAFF, "note": "公開資料只有總數 350 人與 40 車，職務拆分為情境假設（DATA_SOURCES.md C7）"},
    "districts": board,
    "focus": {"districts": focus, "stations": focus_stations, "station_share_pct": round(100 * focus_stations / all_stations, 1),
              "deficit_am": focus_def_am, "deficit_share_am_pct": round(100 * focus_def_am / tot_def_am, 1),
              "deficit_pm": focus_def_pm, "deficit_share_pm_pct": round(100 * focus_def_pm / tot_def_pm, 1),
              "trucks_by_station_share": round(TRUCKS * focus_stations / all_stations, 1),
              "trucks_by_deficit_share_am": sum(alloc_am.get(d, 0) for d in focus),
              "trucks_by_deficit_share_pm": sum(alloc_pm.get(d, 0) for d in focus)},
}
json.dump(baseline, open(os.path.join(ROOT, "app", "static", "ops_baseline.json"), "w"), ensure_ascii=False, indent=1)

out = {
    "checks": res, "failed": fails,
    "derived": {
        "全日見車率": round(avail_all, 2), "全日見位率": round(dock_all, 2),
        "早尖峰見車率": round(avail_peak, 2), "早尖峰見位率": round(dock_peak, 2),
        "平均在柱車輛": round(in_dock), "在柱率%": round(in_dock_pct, 1), "車柱總數": int(st.cap_mode.sum()),
        "分區早尖峰": by_d,
        "早尖峰缺車時500m內有鄰站可借%": round(cover_pct, 1), "早尖峰走五分鐘也借不到%": round(stranded_pct, 1),
        "零車觀測數(早尖峰)": int(tot),
        "三區站數": focus_stations, "全市站數": all_stations, "三區站數占比%": round(100 * focus_stations / all_stations, 1),
        "三區早尖峰缺口占比%": round(100 * focus_def_am / tot_def_am, 1),
        "三區按站數比例車輛": round(TRUCKS * focus_stations / all_stations, 1),
        "三區按缺口比例車輛(早)": sum(alloc_am.get(d, 0) for d in focus),
        "三區按缺口比例車輛(晚)": sum(alloc_pm.get(d, 0) for d in focus),
        "早尖峰未達90%的區": [b["district"] for b in board if b["below_target_am"]],
        "晚尖峰未達90%的區": [b["district"] for b in board if b["below_target_pm"]],
    },
}
os.makedirs(os.path.join(ROOT, "reports"), exist_ok=True)
json.dump(out, open(os.path.join(ROOT, "reports", "verify_public.json"), "w"), ensure_ascii=False, indent=1)

df = pd.DataFrame(res)
print(df.to_string(index=False)); print()
print(json.dumps(out["derived"], ensure_ascii=False, indent=1)); print()
print("已產生 app/static/ops_baseline.json")
if fails:
    print("未通過：", fails); sys.exit(1)
print("全部通過")
