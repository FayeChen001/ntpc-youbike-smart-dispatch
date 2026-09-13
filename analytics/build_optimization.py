#!/usr/bin/env python3
"""build_optimization.py — 政府端「成效與優化」分頁的預算（只讀凍結資料）。

輸出 data/analytics/optimization.json，含三個期間（全期／近三月／近一月）各自的：

  閒置車      整個期間日間平日的庫存最低點仍大於 0 → 那些車從頭到尾沒被借走過
  閒置柱      整個期間車數的最高點都碰不到容量 → 上面那幾柱一次都沒被停過
  大額庫存跳變 單一分箱內 |Δ| ≥ max(10, 容量一半) → 量級參考，**無法區分人工調度與大量借還**
  站群容量不足 500 公尺站群在平日尖峰「整群同時滿載」或「整群同時空掉」的時間比例

**這裡沒有「優化前 vs 優化後」的真實對照組**——我們手上只有 2026-01~06 的歷史，
沒有實際執行過任何優化。所以輸出的是「若照建議做可以回收多少」，
不是已經發生的成效。臺北公布的每日少調度 1,192 輛同樣是預估值。

用法：python3 analytics/build_optimization.py
"""
import json
import math
import os
import sys

import numpy as np
import pandas as pd

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
PROC = os.path.join(ROOT, "data", "processed")
OUT = os.path.join(ROOT, "data", "analytics")
os.makedirs(OUT, exist_ok=True)

DAY_LO, DAY_HI = 6, 23
AM, PM = (7, 9), (17, 19)
PERIODS = {
    "all":  ("2026-01-01", "2026-06-30", "全期（1–6 月）"),
    "m3":   ("2026-04-01", "2026-06-30", "近三個月（4–6 月）"),
    "m1":   ("2026-06-01", "2026-06-30", "近一個月（6 月）"),
}
# 最低保留量與 app/planner.py 的 ASSUMPTIONS 一致：容量的 15%，至少 2
RESERVE_RATIO, RESERVE_MIN = 0.15, 2
# 跳變門檻：至少 10 輛、且至少是容量的一半。
# 第一版用「超過該站 |Δ| 的 p95 且 ≥5 輛」，結果全市每日算出兩萬多輛——
# 全新北才約 1.7 萬台車，那顯然是把尖峰的正常借還潮當成貨車了。
JUMP_ABS, JUMP_CAP_RATIO = 10, 0.5


def clean(o):
    if isinstance(o, float):
        return None if (math.isnan(o) or math.isinf(o)) else o
    if isinstance(o, dict):
        return {k: clean(v) for k, v in o.items()}
    if isinstance(o, list):
        return [clean(v) for v in o]
    return o


def load():
    st = pd.read_parquet(os.path.join(PROC, "stations.parquet"))
    o = pd.read_parquet(os.path.join(PROC, "obs.parquet"),
                        columns=["sid", "bin", "bikes", "spaces", "cap", "valid"])
    o = o[o.valid].copy()
    o = o[~((o.bikes == 0) & (o.spaces == 0))]          # 雙零語意未定，排除
    jun = o[o.bin.dt.month == 6]
    alive = jun.groupby("sid").bikes.max()
    alive = set(alive[alive > 0].index)
    o = o[o.sid.isin(alive)].copy()
    o["hour"] = o.bin.dt.hour
    o["weekday"] = o.bin.dt.dayofweek < 5
    o["date"] = o.bin.dt.date
    return st, o, alive


def idle(o, st, lo, hi):
    """閒置車與閒置柱：日間平日的 5 百分位仍高於保留量的部分。"""
    d = o[(o.bin >= lo) & (o.bin <= hi) & o.weekday
          & (o.hour >= DAY_LO) & (o.hour <= DAY_HI)]
    g = d.groupby("sid")
    n = g.size()
    keep = n[n >= 200].index                            # 觀測太少不下結論
    minb = g.bikes.min().reindex(keep)
    maxb = g.bikes.max().reindex(keep)
    p5b = g.bikes.quantile(0.05).reindex(keep)
    cap = st.set_index("sid").cap_mode.reindex(keep).astype(float)
    reserve = np.maximum(RESERVE_MIN, (cap * RESERVE_RATIO).round())
    # 閒置車（嚴格）：整個期間庫存的最低點都還有這麼多，代表這些車從頭到尾沒被借走過。
    idle_b = minb.clip(lower=0).round().astype(int)
    # 閒置柱（嚴格）：整個期間車數的最高點都碰不到容量，上面那幾柱一次都沒被停過。
    #   兩個都用最小／最大值而不是百分位——p5/p95 等於「只有 5% 時間用得到就算閒置」，
    #   太寬鬆：第一版那樣算出 6,385 柱，臺北實際只找到約 390 柱。
    idle_s = (cap - maxb).clip(lower=0).round().astype(int)
    # 較寬鬆的參考值：扣掉最低保留量之後還剩多少（給要更積極回收的人看）
    loose_b = (p5b - reserve).clip(lower=0).round().astype(int)
    # 期間內車數完全沒變過的站＝資料停滯（站端沒回報），不是「閒置」。
    # 這種站會同時衝上閒置車與閒置柱兩張榜，例如中和德光莒光路口容量 30、
    # 六月最高與最低都是 4。必須標出來並排除，否則整張表都被它們汙染。
    flat = (maxb == minb)
    nuniq = g.bikes.nunique().reindex(keep)
    flat = flat | (nuniq <= 1)
    return pd.DataFrame({"cap": cap, "min_bikes": minb, "max_bikes": maxb, "p5_bikes": p5b,
                         "reserve": reserve, "idle_bikes": idle_b, "idle_docks": idle_s,
                         "loose_idle_bikes": loose_b, "flat": flat})


def dispatch_volume(o, st, lo, hi):
    """大額庫存跳變：單一分箱內 |Δ| ≥ max(10, 容量一半)。

    **這不是調度量。** 半小時快照無法區分人工搬運與短時間內的大量借還，
    臺北能算調度量是因為他們有微笑單車的分時調度資料，我們沒有。
    這個數字只能當「上限量級」看。
    """
    d = o[(o.bin >= lo) & (o.bin <= hi)].sort_values(["sid", "bin"]).copy()
    d["delta"] = d.groupby("sid").bikes.diff()
    d = d.dropna(subset=["delta"])
    d["absd"] = d.delta.abs()
    capm = st.set_index("sid").cap_mode.astype(float)
    d["thr"] = np.maximum(JUMP_ABS, d.sid.map(capm) * JUMP_CAP_RATIO)
    jump = d[d.absd >= d.thr]
    days = max(1, d.date.nunique())
    per_station = jump.groupby("sid").absd.sum() / days
    return {
        "days": int(days),
        "daily_bikes_moved": round(float(jump.absd.sum() / days), 1),
        "daily_events": round(float(len(jump) / days), 1),
        "per_station": per_station,
    }


def cluster_saturation(o, st, lo, hi):
    """站群容量不足：500 公尺站群在平日尖峰整群同時滿載／同時空掉的時間比例。

    整群同時滿載，代表那一帶的柱位總量在尖峰不夠用——這是「該加柱或增站」的直接證據，
    而不是調度補得到的問題。
    """
    nb = pd.read_parquet(os.path.join(PROC, "neighbors_800m.parquet"))
    nb = nb[nb.dist_m <= 500]
    d = o[(o.bin >= lo) & (o.bin <= hi) & o.weekday]
    peak = d[((d.hour >= AM[0]) & (d.hour <= AM[1])) | ((d.hour >= PM[0]) & (d.hour <= PM[1]))]
    pb = peak.pivot_table(index="sid", columns="bin", values="bikes", aggfunc="first")
    ps = peak.pivot_table(index="sid", columns="bin", values="spaces", aggfunc="first")
    sids = pb.index.to_numpy()
    pos = {s: i for i, s in enumerate(sids)}
    B, S = pb.to_numpy(), ps.to_numpy()
    M = ~np.isnan(B)
    rows = []
    grp = {s: [pos[x] for x in g.nsid if x in pos] for s, g in nb.groupby("sid")}
    name = st.set_index("sid")["name"]
    dist = st.set_index("sid")["district"]
    capm = st.set_index("sid")["cap_mode"]
    for s, i in pos.items():
        idx = np.array([i] + grp.get(s, []))
        if len(idx) < 2:
            continue                                     # 孤站不算站群
        m = M[idx]
        valid = m.all(0)
        if valid.sum() < 100:
            continue
        full = ((S[idx] == 0) & m).all(0) & valid        # 整群同時沒有空位
        empty = ((B[idx] == 0) & m).all(0) & valid       # 整群同時沒有車
        rows.append({
            "sid": int(s), "name": name.get(s, ""), "district": dist.get(s, ""),
            "cluster_n": int(len(idx)),
            "cluster_capacity": int(capm.reindex(sids[idx]).fillna(0).sum()),
            "peak_bins": int(valid.sum()),
            "all_full_pct": round(float(full.sum() / valid.sum()) * 100, 2),
            "all_empty_pct": round(float(empty.sum() / valid.sum()) * 100, 2),
        })
    return rows


def main():
    print("讀取 …", flush=True)
    st, o, alive = load()
    name = st.set_index("sid")["name"]
    dist = st.set_index("sid")["district"]
    out = {"meta": {
        "generated_from": "2026-01-01 ~ 2026-06-30 半小時快照",
        "stations_analyzed": len(alive),
        "reserve_rule": f"最低保留量＝容量 × {RESERVE_RATIO}，至少 {RESERVE_MIN}（與調度端同一組假設）",
        "not_an_outcome": ("這是「若照建議做可以回收多少」的可優化空間，"
                           "不是已經執行過的成效——我們沒有優化前後的對照組。"),
        "caveats": [
            "閒置判定取整個期間的最低／最高點，是最嚴格的認定：只要出現過一次就不算閒置。另附較寬鬆的百分位版本供比較。",
            "大額庫存跳變不是調度量：快照無法區分人工搬運與短時間大量借還。臺北能算調度量是因為有微笑單車的分時調度資料，我們沒有。",
            "站群同時滿載代表該區柱位總量在尖峰不足，屬於加柱或增站的問題，調度補不到。",
        ],
    }, "periods": {}}

    for key, (lo, hi, label) in PERIODS.items():
        print(f"期間 {label} …", flush=True)
        lo_ts, hi_ts = pd.Timestamp(lo), pd.Timestamp(hi) + pd.Timedelta(days=1)
        idf = idle(o, st, lo_ts, hi_ts)
        dv = dispatch_volume(o, st, lo_ts, hi_ts)
        sat = cluster_saturation(o, st, lo_ts, hi_ts)

        live_idf = idf[~idf.flat]                 # 資料停滯的站不算閒置
        flat_n = int(idf.flat.sum())
        top_bikes = live_idf[live_idf.idle_bikes > 0].sort_values("idle_bikes", ascending=False).head(20)
        top_docks = live_idf[live_idf.idle_docks > 0].sort_values("idle_docks", ascending=False).head(20)
        busiest = dv["per_station"].sort_values(ascending=False).head(15)
        sat_full = sorted(sat, key=lambda r: -r["all_full_pct"])[:15]
        sat_empty = sorted(sat, key=lambda r: -r["all_empty_pct"])[:15]

        out["periods"][key] = {
            "label": label, "from": lo, "to": hi, "days": dv["days"],
            "idle": {
                "definition": ("閒置車＝期間內庫存最低點（那些車從沒被借走）；"
                               "閒置柱＝容量減去車數最高點（那些柱從沒被停過）。兩者都取最嚴格的認定。"),
                "stations_with_idle_bikes": int((live_idf.idle_bikes > 0).sum()),
                "total_idle_bikes": int(live_idf.idle_bikes.sum()),
                "loose_total_idle_bikes": int(live_idf.loose_idle_bikes.sum()),
                "flat_stations_excluded": flat_n,
                "flat_note": ("期間內車數完全沒變過的站已排除——那是資料停滯（站端沒回報），"
                              "不是閒置庫存。不排除的話它們會同時衝上閒置車與閒置柱兩張榜。"),
                "loose_note": "較寬鬆版：庫存 5 百分位扣掉最低保留量，給要更積極回收的情境參考。",
                "stations_with_idle_docks": int((live_idf.idle_docks > 0).sum()),
                "total_idle_docks": int(live_idf.idle_docks.sum()),
                "top_bikes": [{"sid": int(s), "name": name.get(s, ""), "district": dist.get(s, ""),
                               "idle": int(r.idle_bikes), "cap": int(r.cap),
                               "min_bikes": int(r.min_bikes), "reserve": int(r.reserve)}
                              for s, r in top_bikes.iterrows()],
                "top_docks": [{"sid": int(s), "name": name.get(s, ""), "district": dist.get(s, ""),
                               "idle": int(r.idle_docks), "cap": int(r.cap),
                               "max_bikes": int(r.max_bikes), "reserve": int(r.reserve)}
                              for s, r in top_docks.iterrows()],
            },
            "dispatch": {
                "is_proxy": True,
                "what_it_is": ("單一分箱內庫存跳變 ≥ max(10, 容量一半) 的總量。"
                               "無法區分人工調度與短時間大量借還，只能當上限量級參考。"),
                "daily_bikes_moved": dv["daily_bikes_moved"],
                "daily_events": dv["daily_events"],
                "top_stations": [{"sid": int(s), "name": name.get(s, ""),
                                  "district": dist.get(s, ""), "daily": round(float(v), 1)}
                                 for s, v in busiest.items()],
            },
            "capacity_shortage": {
                "clusters_ever_all_full": sum(1 for r in sat if r["all_full_pct"] > 0),
                "clusters_ever_all_empty": sum(1 for r in sat if r["all_empty_pct"] > 0),
                "clusters_evaluated": len(sat),
                "top_full": sat_full, "top_empty": sat_empty,
            },
        }

    with open(os.path.join(OUT, "optimization.json"), "w", encoding="utf-8") as f:
        json.dump(clean(out), f, ensure_ascii=False, indent=1, allow_nan=False)
    kb = os.path.getsize(os.path.join(OUT, "optimization.json")) / 1024
    print(f"完成：optimization.json {kb:.1f} KB")
    for k, v in out["periods"].items():
        i = v["idle"]
        print(f"  {v['label']}：閒置車 {i['total_idle_bikes']} 輛／{i['stations_with_idle_bikes']} 站・"
              f"閒置柱 {i['total_idle_docks']} 柱・"
              f"每日疑似搬運 {v['dispatch']['daily_bikes_moved']} 輛")


if __name__ == "__main__":
    sys.exit(main())
