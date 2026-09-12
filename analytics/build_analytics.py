#!/usr/bin/env python3
"""build_analytics.py — v2 歷史大數據分析預算（只讀 data/processed，不動凍結資產）。

輸出：
  data/analytics/slot_risk.parquet   sid × 平日假日 × 48 個半小時分箱 → 無車／無位機率
  data/analytics/summary.json        一站式網站「歷史分析」頁要用的全部彙總
  data/analytics/stations.json       站點清單（含分型、調度負擔、替代站數）

口徑（凡是輸出到畫面的都要跟著這份說明走）：
  * 半小時快照比例，不是連續中斷時長，也不是失敗旅次。
  * 日間定義 06:00–23:59，對齊臺北市「見車率／見位率」的 18 小時定義。
  * 排除「雙零」快照（bikes=0 且 spaces=0）：語意未定，可能是整站服務中斷也可能是資料中斷，
    另外單獨統計，不混入空站或滿站。
  * 排除六月整月零車的站（停用／退場／未投車），它們不是調度失敗。
  * 「調度負擔」是用庫存振幅推的代理指標，快照變化同時包含使用者借還與人工調度，
    無法分離，**不可當成純需求**。

用法：python3 analytics/build_analytics.py
"""
import json
import math
import os
import sys

import numpy as np
import pandas as pd

def clean(o):
    """NaN/Inf 不是合法 JSON，且 FastAPI 的嚴格編碼會直接回 500。一律換成 null。"""
    if isinstance(o, float):
        return None if (math.isnan(o) or math.isinf(o)) else o
    if isinstance(o, dict):
        return {k: clean(v) for k, v in o.items()}
    if isinstance(o, list):
        return [clean(v) for v in o]
    return o


ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
PROC = os.path.join(ROOT, "data", "processed")
OUT = os.path.join(ROOT, "data", "analytics")
os.makedirs(OUT, exist_ok=True)

DAY_LO, DAY_HI = 6, 23          # 06:00–23:59
AM = (7, 9)                     # 平日早峰 07:00–09:59
PM = (17, 19)                   # 平日晚峰 17:00–19:59
# 臺北城市儀表板 component 144 / 289（2026 年 6 月、06:00–23:59）— 外部數字，標明出處
TPE = {"avail": {"high": 1710, "mid": 42, "low": 0, "n": 1752},
       "park": {"high": 1747, "mid": 5, "low": 0, "n": 1752}}


def load():
    st = pd.read_parquet(os.path.join(PROC, "stations.parquet"))
    o = pd.read_parquet(os.path.join(PROC, "obs.parquet"),
                        columns=["sid", "bin", "bikes", "spaces", "cap", "valid"])
    o = o[o.valid].copy()
    dz = (o.bikes == 0) & (o.spaces == 0)
    dz_cells, dz_st = int(dz.sum()), int(o[dz].sid.nunique())
    o = o[~dz]
    jun = o[o.bin.dt.month == 6]
    alive = jun.groupby("sid").bikes.max()
    alive = set(alive[alive > 0].index)
    dead = sorted(set(st.sid.tolist()) - alive)
    o = o[o.sid.isin(alive)].copy()
    o["hour"] = o.bin.dt.hour
    o["dow"] = o.bin.dt.dayofweek
    o["weekday"] = o.dow < 5
    o["slot"] = o.bin.dt.hour * 2 + (o.bin.dt.minute >= 30).astype(int)
    return st, o, {"double_zero_cells": dz_cells, "double_zero_stations": dz_st,
                   "dead_stations": dead, "alive": len(alive)}


def slot_risk(o):
    """sid × 平日/假日 × 48 分箱 的無車／無位機率——即時頁的風險估計就是查這張表。"""
    g = o.groupby(["sid", "weekday", "slot"])
    t = g.agg(n=("bikes", "size"),
              p_no_bike=("bikes", lambda s: float((s == 0).mean())),
              p_no_dock=("spaces", lambda s: float((s == 0).mean())),
              mean_bikes=("bikes", "mean"),
              mean_docks=("spaces", "mean")).reset_index()
    t = t[t.n >= 4]
    for c in ("p_no_bike", "p_no_dock", "mean_bikes", "mean_docks"):
        t[c] = t[c].astype("float32")
    t.to_parquet(os.path.join(OUT, "slot_risk.parquet"), index=False)
    return t


def peak_rank(o, st, col, lo, hi, k=15):
    d = o[(o.hour >= lo) & (o.hour <= hi) & o.weekday]
    t = d.groupby("sid").agg(rate=(col, lambda s: float((s == 0).mean())), n=(col, "size"))
    t = t[t.n > 150].merge(st[["sid", "name", "district"]], on="sid")
    t = t.sort_values("rate", ascending=False).head(k)
    return [{"sid": int(r.sid), "name": r["name"], "district": r.district,
             "rate": round(r.rate * 100, 1)} for _, r in t.iterrows()]


def bands(series):
    hi = int((series >= 0.9).sum()); lo = int((series < 0.6).sum())
    return {"high": hi, "mid": int(len(series) - hi - lo), "low": lo, "n": int(len(series))}


def station_profiles(o, st):
    """站點分型：用平日每小時的淨流量形狀分群（學 BiciCoruña 的 K=4 功能原型）。"""
    from sklearn.cluster import KMeans
    d = o[o.weekday].sort_values(["sid", "bin"])
    d["delta"] = d.groupby("sid").bikes.diff()
    prof = d.groupby(["sid", "hour"]).delta.mean().unstack(fill_value=0.0)
    prof = prof.reindex(columns=range(24), fill_value=0.0)
    cap = st.set_index("sid").cap_mode.reindex(prof.index).replace(0, np.nan)
    norm = prof.div(cap, axis=0).fillna(0.0)                 # 以容量正規化，避免大站主導
    km = KMeans(n_clusters=4, n_init=10, random_state=0).fit(norm.values)
    lab = pd.Series(km.labels_, index=prof.index)
    # 依早峰淨流命名：早上淨流出＝住家端，早上淨流入＝目的地端
    am_net = norm[[7, 8, 9]].sum(axis=1)
    pm_net = norm[[17, 18, 19]].sum(axis=1)
    names = {}
    for c in range(4):
        m = lab == c
        a, p = float(am_net[m].mean()), float(pm_net[m].mean())
        if a < -0.05 and p > 0.02:
            names[c] = "住家端（早上被借空、傍晚回補）"
        elif a > 0.05 and p < -0.02:
            names[c] = "目的地端（早上被還爆、傍晚被借空）"
        elif abs(a) < 0.03 and abs(p) < 0.03:
            names[c] = "平穩型"
        else:
            names[c] = "混合型"
    # 同名去重
    seen = {}
    for c in range(4):
        n = names[c]
        if n in seen:
            names[c] = f"{n}·{c}"
        seen[n] = True
    return lab.map(names), lab


def dispatch_burden(o, st):
    """調度負擔代理：日內累積淨流的振幅 ÷ 容量。>1 代表當日必然需要人為介入。

    **警告**：快照變化同時包含使用者借還與人工調度，兩者無法分離，這不是純需求。
    """
    d = o.sort_values(["sid", "bin"]).copy()
    d["date"] = d.bin.dt.date
    d["delta"] = d.groupby(["sid", "date"]).bikes.diff().fillna(0.0)
    d["cum"] = d.groupby(["sid", "date"]).delta.cumsum()
    amp = d.groupby(["sid", "date"]).cum.agg(lambda s: float(s.max() - s.min()))
    med = amp.groupby("sid").median()
    cap = st.set_index("sid").cap_mode.reindex(med.index).replace(0, np.nan)
    return (med / cap).fillna(0.0)


def main():
    print("讀取 …", flush=True)
    st, o, meta = load()
    day = o[(o.hour >= DAY_LO) & (o.hour <= DAY_HI)]
    jun = day[day.bin.dt.month == 6]

    print("分箱風險表 …", flush=True)
    slot_risk(o)

    print("見車率／見位率 …", flush=True)
    avail = jun.groupby("sid").bikes.apply(lambda s: float((s >= 1).mean()))
    park = jun.groupby("sid").spaces.apply(lambda s: float((s >= 1).mean()))

    print("小時曲線 …", flush=True)
    def curve(df):
        g = df.groupby("hour")
        return {"no_bike": [round(float(x) * 100, 2) for x in g.bikes.apply(lambda s: (s == 0).mean())],
                "no_dock": [round(float(x) * 100, 2) for x in g.spaces.apply(lambda s: (s == 0).mean())],
                "hours": [int(h) for h in sorted(df.hour.unique())]}
    curves = {"weekday": curve(day[day.weekday]), "weekend": curve(day[~day.weekday])}

    print("捷運對比 …", flush=True)
    mrt = set(st[st.name.str.contains("捷運", na=False)].sid)
    def split(lo, hi):
        d = o[(o.hour >= lo) & (o.hour <= hi) & o.weekday]
        out = {}
        for lab, sel in (("mrt", d.sid.isin(mrt)), ("non_mrt", ~d.sid.isin(mrt))):
            x = d[sel]
            out[lab] = {"no_bike": round(float((x.bikes == 0).mean()) * 100, 2),
                        "no_dock": round(float((x.spaces == 0).mean()) * 100, 2)}
        return out
    mrt_cmp = {"am": split(*AM), "pm": split(*PM)}

    print("稀釋效應 …", flush=True)
    am = jun[(jun.hour >= AM[0]) & (jun.hour <= AM[1]) & jun.weekday]
    am_nodock = am.groupby("sid").spaces.apply(lambda s: float((s == 0).mean()))
    dil = pd.DataFrame({"park": park, "am_no_dock": am_nodock}).dropna()
    dil = dil.merge(st[["sid", "name", "district"]], on="sid")
    dil = dil.sort_values("am_no_dock", ascending=False).head(12)
    dilution = [{"sid": int(r.sid), "name": r["name"], "district": r.district,
                 "park_rate": round(r.park * 100, 1), "am_no_dock": round(r.am_no_dock * 100, 1),
                 "band": "高" if r.park >= .9 else ("中" if r.park >= .6 else "低")}
                for _, r in dil.iterrows()]

    print("站點分型與調度負擔 …", flush=True)
    ptype, _ = station_profiles(o, st)
    burden = dispatch_burden(o, st)

    print("替代站 …", flush=True)
    nb = pd.read_parquet(os.path.join(PROC, "neighbors_800m.parquet"))
    n500 = nb[nb.dist_m <= 500].groupby("sid").size()

    print("行政區 …", flush=True)
    dist = o[o.weekday & (o.hour >= AM[0]) & (o.hour <= AM[1])].merge(
        st[["sid", "district"]], on="sid")
    dg = dist.groupby("district").agg(
        no_bike=("bikes", lambda s: float((s == 0).mean())),
        no_dock=("spaces", lambda s: float((s == 0).mean())),
        n=("bikes", "size")).reset_index()
    districts = [{"district": r.district, "no_bike": round(r.no_bike * 100, 2),
                  "no_dock": round(r.no_dock * 100, 2),
                  "stations": int(st[st.district == r.district].shape[0])}
                 for _, r in dg.sort_values("no_bike", ascending=False).iterrows()]

    summary = {
        "meta": {
            "period": "2026-01-01 ~ 2026-06-30",
            "rows": 13324945,
            "stations_total": int(len(st)),
            "stations_analyzed": meta["alive"],
            "double_zero_cells": meta["double_zero_cells"],
            "double_zero_stations": meta["double_zero_stations"],
            "dead_stations": len(meta["dead_stations"]),
            "day_window": "06:00-23:59",
            "caveat": ("半小時快照比例，不是連續中斷時長，也不是失敗旅次；"
                       "已排除雙零快照與六月整月零車的停用站"),
        },
        "rates": {
            "avail_june": {"mean": round(float(avail.mean()) * 100, 1), **bands(avail)},
            "park_june": {"mean": round(float(park.mean()) * 100, 1), **bands(park)},
            "taipei_reference": TPE,
            "taipei_source": "臺北城市儀表板 component 144／289，2026 年 6 月，06:00–23:59",
            "compare_caveat": ("臺北的算法可能帶容忍台數與容忍時間參數（見其開源 abnormal.py），"
                               "我們是純快照比例，兩者不是同一個演算法，只能當量級參考"),
        },
        "curves": curves,
        "mrt_compare": mrt_cmp,
        "dilution": dilution,
        "peaks": {
            "am_no_bike": peak_rank(o, st, "bikes", *AM),
            "am_no_dock": peak_rank(o, st, "spaces", *AM),
            "pm_no_bike": peak_rank(o, st, "bikes", *PM),
            "pm_no_dock": peak_rank(o, st, "spaces", *PM),
        },
        "districts": districts,
        "dead_station_names": st[st.sid.isin(meta["dead_stations"])][
            ["sid", "name", "district"]].to_dict("records"),
        "profiles": {k: int(v) for k, v in ptype.value_counts().items()},
        "burden": {
            "median": round(float(burden.median()), 3),
            "over_1": int((burden > 1).sum()),
            "top": [{"sid": int(s), "name": st.set_index("sid").loc[s, "name"],
                     "district": st.set_index("sid").loc[s, "district"],
                     "amp_ratio": round(float(v), 2)}
                    for s, v in burden.sort_values(ascending=False).head(12).items()],
        },
    }
    with open(os.path.join(OUT, "summary.json"), "w", encoding="utf-8") as f:
        json.dump(clean(summary), f, ensure_ascii=False, indent=1, allow_nan=False)

    stations = st[["sid", "name", "district", "lat", "lon", "cap_mode"]].copy()
    stations["avail_june"] = stations.sid.map(avail).round(4)
    stations["park_june"] = stations.sid.map(park).round(4)
    stations["am_no_dock"] = stations.sid.map(am_nodock).round(4)
    stations["profile"] = stations.sid.map(ptype)
    stations["burden"] = stations.sid.map(burden).round(3)
    stations["alt_500m"] = stations.sid.map(n500).fillna(0).astype(int)
    stations["retired"] = stations.sid.isin(meta["dead_stations"])
    stations = stations.where(pd.notnull(stations), None)
    with open(os.path.join(OUT, "stations.json"), "w", encoding="utf-8") as f:
        json.dump(clean(stations.to_dict("records")), f, ensure_ascii=False, allow_nan=False)

    print("完成：", {k: round(os.path.getsize(os.path.join(OUT, k)) / 1024, 1)
                   for k in os.listdir(OUT)}, "KB")


if __name__ == "__main__":
    sys.exit(main())
