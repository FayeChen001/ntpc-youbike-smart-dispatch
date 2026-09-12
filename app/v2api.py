"""v2api.py — 一站式網站的後端（v2 新增，不改動任何既有端點）。

三個資料模式，畫面上一律標示：
  即時   新北市政府資料開放平臺官方 API（app/live.py），每 5 分鐘更新
  歷史   2026-01~06 共 1,332 萬筆快照的預算分析（analytics/build_analytics.py）
  回放   既有的情境回放與訓練好的 30/60/120/180 分模型（既有端點，本模組不碰）

即時模式下的「風險」＝ 該站在歷史同一時段（平日/假日 × 半小時分箱）的無車／無位快照比例，
**不是模型預測**，畫面上標為「歷史同時段」。模型預測只在回放模式提供。
"""
import json
import math
import os
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

TZ = timezone(timedelta(hours=8))
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
ANA = os.path.join(ROOT, "data", "analytics")

router = APIRouter(prefix="/api/v2", tags=["v2"])

_CTX = {}


# ---------------------------------------------------------------- 初始化
class Risk:
    """歷史同時段風險查表：sid → 平日/假日 × 48 分箱。"""

    def __init__(self, path):
        self.ok = False
        self.wd, self.we = {}, {}
        if not os.path.exists(path):
            return
        t = pd.read_parquet(path)
        for wk, store in ((True, self.wd), (False, self.we)):
            sub = t[t.weekday == wk]
            for sid, g in sub.groupby("sid"):
                a = np.full((48, 4), np.nan, dtype="float32")
                a[g.slot.to_numpy(), 0] = g.p_no_bike.to_numpy()
                a[g.slot.to_numpy(), 1] = g.p_no_dock.to_numpy()
                a[g.slot.to_numpy(), 2] = g.mean_bikes.to_numpy()
                a[g.slot.to_numpy(), 3] = g.mean_docks.to_numpy()
                store[int(sid)] = a
        self.ok = True

    def at(self, sid, when=None, ahead_min=0):
        when = (when or datetime.now(TZ)) + timedelta(minutes=ahead_min)
        store = self.wd if when.weekday() < 5 else self.we
        a = store.get(int(sid))
        if a is None:
            return None
        slot = when.hour * 2 + (1 if when.minute >= 30 else 0)
        row = a[slot]
        if np.isnan(row[0]):
            return None
        return {"slot": int(slot),
                "at": when.strftime("%H:%M"),
                "p_no_bike": round(float(row[0]), 4),
                "p_no_dock": round(float(row[1]), 4),
                "mean_bikes": round(float(row[2]), 2),
                "mean_docks": round(float(row[3]), 2)}


def init(live_store, stations_df, state, planner=None, forecaster=None):
    _CTX["live"] = live_store
    _CTX["fc"] = forecaster
    _CTX["st"] = stations_df
    _CTX["state"] = state
    _CTX["planner"] = planner
    _CTX["risk"] = Risk(os.path.join(ANA, "slot_risk.parquet"))
    _CTX["summary"] = _load(os.path.join(ANA, "summary.json"), {})
    rows = _load(os.path.join(ANA, "stations.json"), [])
    _CTX["stations"] = {int(r["sid"]): r for r in rows}
    # 鄰站表只在啟動時讀一次；原本每個 /station 與 /trip 請求都重讀 parquet（/trip 讀兩次）
    nbp = os.path.join(ROOT, "data", "processed", "neighbors_800m.parquet")
    nbr = {}
    if os.path.exists(nbp):
        nb = pd.read_parquet(nbp).sort_values("dist_m")
        for sid, g in nb.groupby("sid"):
            nbr[int(sid)] = [(int(r.nsid), float(r.dist_m)) for r in g.itertuples()]
    _CTX["neighbors"] = nbr
    return _CTX


def _near(sid, max_m):
    """回傳 [(鄰站 sid, 距離公尺)]，已依距離遞增排序。"""
    return [(n, d) for n, d in _CTX.get("neighbors", {}).get(int(sid), []) if d <= max_m]


def _sanitize(o):
    """NaN/Inf 進到回應會讓 FastAPI 直接 500，載入時就換成 None。"""
    if isinstance(o, float):
        return None if (math.isnan(o) or math.isinf(o)) else o
    if isinstance(o, dict):
        return {k: _sanitize(v) for k, v in o.items()}
    if isinstance(o, list):
        return [_sanitize(v) for v in o]
    return o


def _load(p, default):
    try:
        with open(p, encoding="utf-8") as f:
            return _sanitize(json.load(f))
    except (OSError, json.JSONDecodeError):
        return default


def _live():
    lv = _CTX.get("live")
    if lv is None:
        raise HTTPException(503, "即時資料層尚未初始化")
    return lv


# ---------------------------------------------------------------- 即時
@router.get("/live")
def live_overview():
    lv = _live()
    stats, status = lv.stats(), lv.status()
    s = _CTX.get("summary", {})
    hist = None
    if s:
        mrt = s.get("mrt_compare", {})
        hist = {"am": mrt.get("am"), "pm": mrt.get("pm"),
                "avail_june": s.get("rates", {}).get("avail_june", {}).get("mean"),
                "park_june": s.get("rates", {}).get("park_june", {}).get("mean")}
    return {"status": status, "stats": stats, "history_context": hist,
            "semantics": {
                "capacity_gap": ("總停車格數 −（可借＋可還）的差額。名目上存在、"
                                 "但官方 API 既不計入可借也不計入可還的車柱。不判定根因。"),
                "no_bike": "官方回報可借車輛為 0，不等於現場真的一台都借不到，也不等於失敗旅次。",
                "inactive": "官方標示暫停營運（act≠1），與無車可借是兩件事。",
                "risk": "即時模式的風險為歷史同時段快照比例，不是模型預測。"}}


@router.get("/live/stations")
def live_stations(district: str = None, only: str = None, limit: int = 2000):
    """only: no_bike / no_dock / gap / inactive。回傳精簡欄位供地圖用。"""
    snap = _live().snapshot()
    out = []
    for x in snap.values():
        if district and x["district"] != district:
            continue
        if only == "no_bike" and not (x["no_bike"] and x["active"]):
            continue
        if only == "no_dock" and not (x["no_dock"] and x["active"]):
            continue
        if only == "gap" and x["capacity_gap"] <= 0:
            continue
        if only == "inactive" and x["active"]:
            continue
        meta = _CTX.get("stations", {}).get(x["sid"], {})
        out.append({"sid": x["sid"], "name": x["name"], "district": x["district"],
                    "lat": x["lat"], "lon": x["lon"], "cap": x["capacity"],
                    "b": x["bikes"], "d": x["docks"], "e": x["bikes_electric"],
                    "gap": x["capacity_gap"], "act": x["active"],
                    "age": x["age_min"], "prof": meta.get("profile")})
        if len(out) >= limit:
            break
    return {"count": len(out), "stations": out}


@router.get("/station/{sid}")
def station_detail(sid: int):
    lv = _live()
    cur = lv.station(sid)
    meta = _CTX.get("stations", {}).get(int(sid), {})
    risk = _CTX.get("risk")
    horizon = []
    if risk and risk.ok:
        for m in (0, 30, 60, 120, 180):
            r = risk.at(sid, ahead_min=m)
            if r:
                horizon.append({"ahead_min": m, **r})
    alts = []
    for nsid, dist in _near(sid, 500)[:6]:
        a = lv.station(nsid)
        if a:
            alts.append({"sid": nsid, "name": a["name"],
                         "dist_m": int(dist), "walk_min": round(dist / 80.0, 1),
                         "bikes": a["bikes"], "docks": a["docks"], "act": a["active"]})
    is_new = int(sid) < 0
    fc = _CTX.get("fc")
    model = fc.station(sid) if (fc is not None and not is_new) else None
    return {"live": cur, "history": meta, "horizon": horizon, "alternatives": alts,
            "model": model,
            "is_new_station": is_new,
            "horizon_semantics": (
                "本站為 2026 年 6 月之後新增，歷史快照資料沒有涵蓋，因此沒有同時段風險可比。"
                if is_new else
                "歷史同時段（平日/假日 × 半小時分箱）的快照比例，不是模型預測")}


# ---------------------------------------------------------------- 即時模型預測
@router.get("/forecast/status")
def forecast_status():
    fc = _CTX.get("fc")
    if fc is None:
        return {"available": False, "reason": "即時預測層未初始化"}
    return fc.status()


@router.get("/forecast/risk")
def forecast_risk(kind: str = "full", horizon: int = 120, limit: int = 20,
                  min_p: float = 0.3, include_offline: bool = False):
    """未來最可能無位可還／無車可借的站——這是模型預測，不是歷史同時段分布。

    預設排除此刻借還同時為 0 的整站無服務站；它們的機率必然接近 1，會把真正
    需要處理的站擠出排名，而且派車也解決不了。要看的話加 include_offline=true。
    """
    fc = _CTX.get("fc")
    if fc is None:
        raise HTTPException(503, "即時預測層未初始化")
    st = fc.status()
    rows = fc.risk_ranking(kind=kind, horizon=horizon, limit=limit, min_p=min_p,
                           exclude_offline=not include_offline)
    # 標出歷史上六月整月零車的站。不過濾掉——它們現在有車就是有車，
    # 但值班看到要知道這一站長期沒有庫存，預警的意義和一般站不同。
    meta = _CTX.get("stations", {})
    for r in rows:
        m = meta.get(r["sid"], {})
        r["retired_in_history"] = bool(m.get("retired"))
        r["profile"] = m.get("profile")
    return {"kind": kind, "horizon_min": horizon, "min_p": min_p,
            "count": len(rows), "stations": rows,
            "status": st,
            "excluded_offline": not include_offline,
            "semantics": ("已排除此刻借還同時為 0 的整站無服務站（機率必然接近 1，派車解決不了）。"
                          "HistGradientBoosting 模型跑在官方即時站況上；"
                          "落後特徵來自本服務自行累積的即時歷史，累積不足時準確度下降。"
                          "模型卡的離線指標是在六月測試集上量的，不等於即時推論的準確度。")}


@router.get("/forecast/station/{sid}")
def forecast_station(sid: int):
    fc = _CTX.get("fc")
    if fc is None:
        raise HTTPException(503, "即時預測層未初始化")
    r = fc.station(sid)
    if r is None:
        return {"available": False,
                "reason": "此站沒有即時觀測或沒有訓練期輪廓（例如 2026-06 之後新增的站）"}
    return {"available": True, **r}


# ---------------------------------------------------------------- 歷史分析
@router.get("/analytics")
def analytics():
    s = _CTX.get("summary")
    if not s:
        raise HTTPException(503, "分析結果尚未產生，請先跑 analytics/build_analytics.py")
    return s


# ---------------------------------------------------------------- 建議
@router.get("/insights")
def insights():
    """把即時狀況與歷史分析交叉，產生可執行建議。每條都附證據與口徑。"""
    lv = _live()
    stats, snap = lv.stats(), lv.snapshot()
    s = _CTX.get("summary", {})
    risk = _CTX.get("risk")
    now = datetime.now(TZ)
    items = []

    gap_units = stats.get("capacity_gap_units", 0)
    gap_st = stats.get("capacity_gap_stations", 0)
    if gap_st:
        worst = lv.worst("capacity_gap", 5)
        items.append({
            "level": "high" if gap_units > 300 else "mid",
            "audience": "gov", "title": "名目庫存與服務可用性的落差",
            "body": (f"此刻 {gap_st} 站（{stats.get('capacity_gap_pct')}%）的總停車格數不等於可借加可還，"
                     f"合計 {gap_units} 個車柱名目上存在但既借不到也還不了。"
                     "見車率與見位率都不會反映這個缺口，因為兩者各自只看單邊數量。"),
            "evidence": [f"{x['name']}（{x['district']}）總 {x['capacity']}／可借 {x['bikes']}／可還 {x['docks']}，差 {x['capacity_gap']}"
                         for x in worst],
            "action": "把容量落差納入巡檢排程，並逐站確認是設備故障、保留柱位還是資料延遲。",
            "caveat": "本工具不判定根因，也不將落差等同於故障台數。"})

    if stats.get("stale_stations"):
        items.append({
            "level": "mid", "audience": "gov", "title": "資料新鮮度",
            "body": (f"{stats['stale_stations']} 站的最後上傳時間超過 30 分鐘"
                     f"（全市中位數 {stats.get('data_age_min_median')} 分鐘）。"
                     "資料停止更新不等於服務恢復，也不等於空站。"),
            "evidence": [], "action": "資料中斷需單獨立案，不可與空站事件混算。",
            "caveat": "來源為官方 API 的場站上傳時間欄位。"})

    for kind, label, who in (("no_dock", "還不了", "ops"), ("no_bike", "借不到", "ops")):
        worst = lv.worst(kind, 8)
        if not worst:
            continue
        rows = []
        for x in worst:
            r = risk.at(x["sid"], ahead_min=60) if (risk and risk.ok) else None
            p = r[("p_no_dock" if kind == "no_dock" else "p_no_bike")] if r else None
            rows.append({"sid": x["sid"], "name": x["name"], "district": x["district"],
                         "bikes": x["bikes"], "docks": x["docks"], "cap": x["capacity"],
                         "hist_risk_60": None if p is None else round(p * 100, 1)})
        high = [r for r in rows if (r["hist_risk_60"] or 0) >= 30]
        items.append({
            "level": "high" if high else "mid", "audience": who,
            "title": f"此刻最可能{label}的站",
            "body": (f"{len(worst)} 站已在臨界（可{'還車位' if kind=='no_dock' else '借車輛'} ≤2）。"
                     + (f"其中 {len(high)} 站在歷史同時段的一小時後風險超過 30%，屬結構性而非偶發。"
                        if high else "歷史同時段風險不高，可能是偶發。")),
            "evidence": [f"{r['name']}（{r['district']}）可借 {r['bikes']}／可還 {r['docks']}／容量 {r['cap']}"
                         + (f"，歷史同時段 1 小時後{label}機率 {r['hist_risk_60']}%" if r["hist_risk_60"] is not None else "")
                         for r in rows[:5]],
            "action": ("優先處理結構性站；人車前置至少 60 分鐘，來不及的缺口改用民眾分流誘因補位。"
                       if high else "納入一般排程即可。"),
            "caveat": "歷史同時段風險是快照比例，不是模型預測，也不保證今天會發生。"})

    dil = s.get("dilution", [])
    if dil:
        hi = [d for d in dil if d["band"] == "高"]
        items.append({
            "level": "high", "audience": "gov", "title": "月平均指標把尖峰稀釋掉了",
            "body": (f"{len(hi)} 個站的六月見位率落在官方分級的「高」（≥90%），"
                     "但平日早峰有三成以上的時間還不了車。18 小時月平均會把通勤者真正遇到的那兩小時洗掉。"),
            "evidence": [f"{d['name']} 見位率 {d['park_rate']}%（{d['band']}），早峰無位可還 {d['am_no_dock']}%"
                         for d in dil[:5]],
            "action": "驗收指標加上尖峰口徑，不要只看日間平均。",
            "caveat": "見位率為 2026 年 6 月、06:00–23:59 的快照比例。"})

    mrt = s.get("mrt_compare", {})
    if mrt.get("am") and mrt.get("pm"):
        items.append({
            "level": "mid", "audience": "citizen", "title": "同一趟通勤，早晚各壞一次",
            "body": (f"平日早峰非捷運站無車可借 {mrt['am']['non_mrt']['no_bike']}%、"
                     f"捷運站無位可還 {mrt['am']['mrt']['no_dock']}%；"
                     f"晚峰翻轉成捷運站無車可借 {mrt['pm']['mrt']['no_bike']}%。"
                     "起點與終點是兩種不同的失敗。"),
            "evidence": [], "action": "出發前就給替代站，不要等使用者到現場才發現。",
            "caveat": "快照比例，非旅次成功率。"})

    # 行政區層級：哪一區的早峰缺車最嚴重，以及此刻的實況對照
    dists = s.get("districts", [])
    if dists:
        top3 = dists[:3]
        by_d = stats.get("by_district", {})
        ev = []
        for d in top3:
            cur = by_d.get(d["district"], {})
            ev.append(f"{d['district']}（{d['stations']} 站）歷史早峰無車 {d['no_bike']}%、"
                      f"無位 {d['no_dock']}%；此刻無車 {cur.get('no_bike', 0)} 站、"
                      f"落差車柱 {cur.get('gap_units', 0)} 個")
        items.append({
            "level": "mid", "audience": "gov", "title": "早峰缺車最集中的行政區",
            "body": (f"平日早峰無車可借比例最高的是 {'、'.join(d['district'] for d in top3)}。"
                     "這三區同時也是通勤起點最密集的區域——住家端被借空，正是早峰的第一種失敗。"),
            "evidence": ev,
            "action": "前一天的預排以這幾區的住家端站為主，不要等早上才動。",
            "caveat": "歷史為半小時快照比例；此刻數字為官方即時資料，兩者口徑不同不可相減。"})

    # 站點分型：結構性的早晚反向需求
    prof = s.get("profiles", {})
    if prof:
        dest = next((k for k in prof if k.startswith("目的地端")), None)
        home = next((k for k in prof if k.startswith("住家端")), None)
        if dest and home:
            items.append({
                "level": "mid", "audience": "ops", "title": "兩種站的需求方向剛好相反",
                "body": (f"用平日每小時淨流量分群，{prof[home]} 個站屬住家端（早上被借空、傍晚回補）、"
                         f"{prof[dest]} 個站屬目的地端（早上被還爆、傍晚被借空）。"
                         "**同一台車早上該從住家端往目的地端走，傍晚要反過來。**"
                         "這代表早晚各需要一次方向相反的再平衡，不是單向補車。"),
                "evidence": [f"{k}：{v} 站" for k, v in prof.items()],
                "action": "把兩類站配成對，早晚各跑一次反向，比各自獨立補車省里程。",
                "caveat": ("分型用的是快照的淨變化，其中同時包含使用者借還與人工調度，"
                           "兩者無法分離，不是純需求。")})

    # 調度負擔：哪些站不管怎麼補都會再度失衡
    burden = s.get("burden", {})
    if burden.get("top"):
        items.append({
            "level": "mid", "audience": "gov", "title": "先天就需要高頻服務的站",
            "body": (f"以「日內庫存振幅 ÷ 容量」當調度負擔的代理指標，全市中位數 {burden['median']}，"
                     "代表典型站每天的庫存起伏約是容量的六成。下列站明顯更高——"
                     "它們不是補一次就好，而是先天需要高頻服務，或該重新檢討容量與配置。"),
            "evidence": [f"{x['name']}（{x['district']}）振幅比 {x['amp_ratio']}"
                         for x in burden["top"][:5]],
            "action": "這類站適合固定班次而不是事件驅動；也值得評估加柱或調整初始配車。",
            "caveat": ("振幅同時包含使用者借還與人工調度，無法分離，"
                       "不可當成純需求，也不等於這些站服務比較差。")})

    fc = _CTX.get("fc")
    if fc is not None:
        fst = fc.status()
        if fst.get("available"):
            hz = 120 if 120 in (fst.get("horizons") or []) else (fst.get("horizons") or [None])[-1]
            full = fc.risk_ranking("full", hz, 8, 0.3)
            empty = fc.risk_ranking("empty", hz, 8, 0.3)
            cov = min(fc.forecast()["coverage"].values()) if fc.forecast() else 0
            partial = cov < 100
            if full or empty:
                items.append({
                    "level": "high" if (full or empty) else "mid", "audience": "ops",
                    "title": f"模型預警：{hz} 分鐘後可能出事的站",
                    "body": (f"模型在官方即時站況上推論，{hz} 分鐘後無位可還機率 ≥30% 的有 {len(full)} 站、"
                             f"無車可借 ≥30% 的有 {len(empty)} 站。"
                             + ("**落後特徵尚未累積完整，準確度會低於模型卡的離線指標。**"
                                if partial else "落後特徵已累積完整。")),
                    "evidence": ([f"{r['name']}（{r['district']}）目前可還 {r['now']:.0f}，"
                                  f"{hz} 分後無位機率 {r['p']}%" for r in full[:3]]
                                 + [f"{r['name']}（{r['district']}）目前可借 {r['now']:.0f}，"
                                    f"{hz} 分後無車機率 {r['p']}%" for r in empty[:3]]),
                    "action": f"{hz} 分鐘足夠新動員一台車（前置 15 分＋行車），現在排還來得及。",
                    "caveat": ("模型預測，不是歷史同時段分布；模型卡的離線指標在六月測試集上量，"
                               "不等於即時推論的準確度。")})

    return {"generated_at": now.isoformat(), "count": len(items), "insights": items}


# ---------------------------------------------------------------- 微笑單車看板
@router.get("/ops/board")
def ops_board():
    """調度候選清單。

    三類分開，不可混為一談：
      送車／清運   真正靠調度能解決的缺口，依歷史同時段風險排序
      整站無服務   可借與可還同時為 0 —— 那不是缺車，是整站設備離線或未投車，
                   派車過去也沒有柱位可用，必須先查修。**不列入調度優先序。**
      設備查修     容量落差過大但仍有服務
    """
    lv = _live()
    risk = _CTX.get("risk")
    now = datetime.now(TZ)
    dispatch, offline, repair = [], [], []
    for x in lv.snapshot().values():
        if not x["active"] or x["capacity"] <= 0:
            continue
        meta = _CTX.get("stations", {}).get(x["sid"], {})
        base = {"sid": x["sid"], "name": x["name"], "district": x["district"],
                "bikes": x["bikes"], "docks": x["docks"], "cap": x["capacity"],
                "gap": x["capacity_gap"], "profile": meta.get("profile"),
                "burden": meta.get("burden"),
                "retired_in_history": bool(meta.get("retired"))}

        if x["bikes"] == 0 and x["docks"] == 0:
            offline.append({**base, "need": "整站無服務",
                            "note": ("歷史上六月整月零車，可能已退場"
                                     if meta.get("retired") else "可借與可還同時為 0")})
            continue

        target = max(3, round(x["capacity"] * 0.2))
        need_bike = max(0, target - x["bikes"])
        need_dock = max(0, target - x["docks"])
        if need_bike or need_dock:
            r60 = risk.at(x["sid"], ahead_min=60) if (risk and risk.ok) else None
            r120 = risk.at(x["sid"], ahead_min=120) if (risk and risk.ok) else None
            key = "p_no_dock" if need_dock else "p_no_bike"
            p60 = r60[key] if r60 else None
            p120 = r120[key] if r120 else None
            dispatch.append({**base,
                             "need": "清運" if need_dock else "送車",
                             "qty": need_dock or need_bike,
                             "hist_risk_60": None if p60 is None else round(p60 * 100, 1),
                             "hist_risk_120": None if p120 is None else round(p120 * 100, 1),
                             "priority": round((p60 or 0) * 100 + (need_dock or need_bike) * 6, 1)})
        elif x["capacity_gap"] > 3:
            repair.append({**base, "need": "設備查修", "qty": x["capacity_gap"]})

    dispatch.sort(key=lambda c: -c["priority"])
    offline.sort(key=lambda c: -c["cap"])
    repair.sort(key=lambda c: -c["gap"])
    structural = sum(1 for c in dispatch if (c["hist_risk_60"] or 0) >= 30)
    return {"generated_at": now.isoformat(),
            "candidates": dispatch[:40],
            "offline": offline[:20], "repair": repair[:20],
            "totals": {"送車": sum(1 for c in dispatch if c["need"] == "送車"),
                       "清運": sum(1 for c in dispatch if c["need"] == "清運"),
                       "整站無服務": len(offline), "設備查修": len(repair),
                       "結構性": structural},
            "semantics": ("候選清單依即時站況與歷史同時段風險排序，**不是派車任務**；"
                          "沒有真實車隊位置、班表與載量，本看板不產生 ETA。"
                          "可借與可還同時為 0 的站另列為整站無服務，派車無法解決。")}


# ---------------------------------------------------------------- 即時事件台帳
# 我們主張「台北做到看得見現在，但紅點是狀態不是案件」。這一段就是把話做出來：
# 每一個被看到的問題都可以立案，並留下誰負責、什麼時候、為什麼。
#
# 守住既有的講話界線：
#   * ack（看到了）不等於指派（有人負責）。未指派比例一律以 owner 計，不用 ack 代替。
#   * 本系統不生成 ETA——沒有車隊位置、班表與載量。恢復時間只記錄「實際恢復的觀測時間」。
#   * 事件關閉需要現場條件成立（借還恢復），不是按一個按鈕就算好了。
EVENT_KINDS = {
    "no_dock": "無位可還", "no_bike": "無車可借",
    "offline": "整站無服務", "capacity_gap": "容量落差", "stale": "資料中斷",
}


def _events():
    return _CTX["state"].setdefault("v2_events", [])


def _event_view(e):
    now = datetime.now(TZ)
    created = datetime.fromisoformat(e["created_at"])
    lv = _CTX.get("live")
    cur = lv.station(e["sid"]) if lv else None
    recovered = None
    if cur:
        if e["kind"] == "no_dock":
            recovered = cur["docks"] > 0
        elif e["kind"] == "no_bike":
            recovered = cur["bikes"] > 0
        elif e["kind"] == "offline":
            recovered = not (cur["bikes"] == 0 and cur["docks"] == 0)
        elif e["kind"] == "capacity_gap":
            recovered = cur["capacity_gap"] <= 0
    return {
        **e,
        "kind_label": EVENT_KINDS.get(e["kind"], e["kind"]),
        "open_min": round((now - created).total_seconds() / 60.0, 1),
        "assigned": e.get("owner") is not None,
        "current": None if not cur else {"bikes": cur["bikes"], "docks": cur["docks"],
                                         "gap": cur["capacity_gap"], "active": cur["active"]},
        "field_recovered": recovered,
        "semantics": ("field_recovered 是依官方即時資料判斷現場借還是否已恢復；"
                      "它與工單是否結案、設備是否修好是三件不同的事。"),
    }


class EventIn(BaseModel):
    sid: int
    kind: str
    note: str = ""
    request_id: str = None


@router.post("/events")
def event_create(body: EventIn):
    if body.kind not in EVENT_KINDS:
        raise HTTPException(400, f"kind 必須是 {list(EVENT_KINDS)} 其中之一")
    lv = _live()
    cur = lv.station(body.sid)
    if not cur:
        raise HTTPException(404, "查無此站的即時資料")
    evs = _events()
    if body.request_id:
        for e in evs:
            if e.get("request_id") == body.request_id:
                return {"ok": True, "idempotent": True, "event": _event_view(e)}
    # 同一站同一類型還開著就不重複立案
    for e in evs:
        if e["sid"] == body.sid and e["kind"] == body.kind and e["status"] != "closed":
            return {"ok": True, "duplicate_of": e["id"], "event": _event_view(e)}
    now = datetime.now(TZ)
    e = {
        "id": f"E{len(evs) + 1:04d}",
        "sid": body.sid, "name": cur["name"], "district": cur["district"],
        "kind": body.kind, "note": body.note,
        "status": "open", "owner": None, "version": 1,
        "created_at": now.isoformat(),
        "observed": {"bikes": cur["bikes"], "docks": cur["docks"],
                     "capacity": cur["capacity"], "gap": cur["capacity_gap"],
                     "info_time": cur["info_time"]},
        "log": [{"ts": now.isoformat(), "action": "created",
                 "by": "監看", "note": body.note or "自即時看板立案"}],
        "request_id": body.request_id,
    }
    evs.append(e)
    return {"ok": True, "event": _event_view(e)}


class EventAction(BaseModel):
    action: str                 # ack | assign | close | reopen
    owner: str = None
    reason: str = ""
    version: int


@router.post("/events/{eid}/action")
def event_action(eid: str, body: EventAction):
    e = next((x for x in _events() if x["id"] == eid), None)
    if e is None:
        raise HTTPException(404, "查無此事件")
    if body.version != e["version"]:
        raise HTTPException(409, {"error": "版本已變更，請重新載入後再操作",
                                  "current_version": e["version"]})
    now = datetime.now(TZ)
    if body.action == "ack":
        # 只記錄「已讀」。**不設定 owner**——ack 不等於指派。
        e["acked_at"] = now.isoformat()
    elif body.action == "assign":
        if not body.owner:
            raise HTTPException(400, "指派必須指定負責人")
        e["owner"] = body.owner
        e["status"] = "assigned"
    elif body.action == "close":
        v = _event_view(e)
        if v["field_recovered"] is False:
            raise HTTPException(409, {
                "error": "現場尚未恢復，不能結案",
                "detail": "官方即時資料顯示這一站的借還狀況仍未恢復。"
                          "結案要在現場條件成立之後，不是按按鈕就算好了。",
                "current": v["current"]})
        e["status"] = "closed"
        e["closed_at"] = now.isoformat()
        e["recovery_observed_min"] = v["open_min"]
    elif body.action == "reopen":
        e["status"] = "open"
        e.pop("closed_at", None)
    else:
        raise HTTPException(400, "action 必須是 ack / assign / close / reopen")
    e["version"] += 1
    e["log"].append({"ts": now.isoformat(), "action": body.action,
                     "by": body.owner or "監看", "note": body.reason})
    return {"ok": True, "event": _event_view(e)}


@router.get("/events")
def event_list(status: str = None):
    evs = [_event_view(e) for e in _events()]
    if status:
        evs = [e for e in evs if e["status"] == status]
    evs.sort(key=lambda e: (e["status"] == "closed", -e["open_min"]))
    open_ev = [e for e in evs if e["status"] != "closed"]
    return {
        "count": len(evs), "events": evs,
        "summary": {
            "open": len(open_ev),
            "unassigned": sum(1 for e in open_ev if not e["assigned"]),
            "over_30min": sum(1 for e in open_ev if e["open_min"] > 30),
            "over_60min": sum(1 for e in open_ev if e["open_min"] > 60),
            "field_recovered_but_open": sum(1 for e in open_ev if e["field_recovered"] is True),
        },
        "semantics": {
            "unassigned": "以 owner 計，不用 ack 代替——看到了不等於有人負責。",
            "no_eta": "本系統不生成 ETA；沒有車隊位置、班表與載量，算出來的都是虛構的。",
            "field_recovered_but_open": "現場已恢復但案件還開著，代表要回頭確認原因與結案。",
        },
    }


@router.post("/events/reset")
def event_reset():
    """清空 v2 事件台帳。獨立於既有的 /api/reset，不動三端共用的重設流程。"""
    n = len(_events())
    _CTX["state"]["v2_events"] = []
    return {"ok": True, "cleared": n}


# ---------------------------------------------------------------- 出發前行程規劃
def _hav(lat1, lon1, lat2, lon2):
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def _arrival_risk(sid, ahead_min, kind):
    """到達當下的風險。優先用模型即時推論，沒有才退回歷史同時段分布，並回報用了哪一個。"""
    fc = _CTX.get("fc")
    if fc is not None:
        st = fc.status()
        if st.get("available"):
            hz = st.get("horizons") or []
            near = min(hz, key=lambda h: abs(h - ahead_min)) if hz else None
            if near is not None:
                one = fc.station(sid)
                if one:
                    for h in one["horizon"]:
                        if h["ahead_min"] == near:
                            return (h["p_full"] if kind == "dock" else h["p_empty"],
                                    f"模型預測（{near} 分鐘尺度）")
    risk = _CTX.get("risk")
    if risk and risk.ok:
        r = risk.at(sid, ahead_min=ahead_min)
        if r:
            return (r["p_no_dock"] if kind == "dock" else r["p_no_bike"]), "歷史同時段分布"
    return None, "無資料"


@router.get("/trip")
def trip(from_sid: int, to_sid: int):
    """出發前的三方案：最快／較穩／順路集點。

    真實計算：兩站直線距離、既有假設下的騎乘與步行時間、兩端的即時可借可還、
              到達時的風險（模型或歷史同時段）、替代站、加碼任務。
    明示假設：騎乘 12 km/h、步行 4.5 km/h、直線→道路係數 1.4、到場無車或無位改站罰時 10 分，
              全部取自 app/planner.py 的 ASSUMPTIONS，與調度端同一份。
    不宣稱：這不是路線導航，沒有真實路網與號誌；到站成功率不等於旅次成功率。
    """
    import planner as PL
    A = PL.ASSUMPTIONS
    lv = _live()
    o, d = lv.station(from_sid), lv.station(to_sid)
    if not o or not d:
        raise HTTPException(404, "起點或終點查無即時資料")

    straight = _hav(o["lat"], o["lon"], d["lat"], d["lon"])
    road_m = straight * A["road_detour"]
    ride_min = road_m / 1000.0 / A["ride_kmh"] * 60.0

    p_start, src_start = _arrival_risk(from_sid, 0, "bike")
    p_end, src_end = _arrival_risk(to_sid, int(round(ride_min)), "dock")

    plans = []
    plans.append({
        "kind": "最快", "station_sid": to_sid, "station": d["name"],
        "ride_min": round(ride_min, 1), "walk_min": 0.0,
        "total_min": round(ride_min, 1),
        "risk": None if p_end is None else round(p_end * 100, 1),
        "risk_source": src_end,
        "docks_now": d["docks"],
        "note": (f"若到場無位，改騎去鄰站估計再加 {A['reroute_penalty_min']} 分鐘"
                 if (p_end or 0) > 0.2 else "到達時有位可還的機會高"),
    })

    # 較穩：終點 500 公尺內、到達時風險最低的站，走回原目的地
    best = None
    if to_sid >= 0:
        for nsid, dist in _near(to_sid, 500):
            a = lv.station(nsid)
            if not a or not a["active"]:
                continue
            walk_m = dist * A["walk_detour"]
            walk_min = walk_m / 1000.0 / A["walk_kmh"] * 60.0
            leg = _hav(o["lat"], o["lon"], a["lat"], a["lon"]) * A["road_detour"]
            rmin = leg / 1000.0 / A["ride_kmh"] * 60.0
            p, src = _arrival_risk(nsid, int(round(rmin)), "dock")
            cand = {"kind": "較穩", "station_sid": nsid, "station": a["name"],
                    "ride_min": round(rmin, 1), "walk_min": round(walk_min, 1),
                    "total_min": round(rmin + walk_min, 1),
                    "risk": None if p is None else round(p * 100, 1), "risk_source": src,
                    "docks_now": a["docks"],
                    "note": f"停在 {a['name']}，再走 {walk_min:.0f} 分鐘到原目的地"}
            key = ((p if p is not None else 1.0), cand["total_min"])
            if best is None or key < best[0]:
                best = (key, cand)
    if best and (p_end is None or best[1]["risk"] is None or best[1]["risk"] < (p_end * 100) - 5):
        plans.append(best[1])

    # 順路集點：終點 800 公尺內有加碼任務（需要有人還車過去）的站
    quest = None
    if to_sid >= 0:
        for nsid, dist in _near(to_sid, 800):
            a = lv.station(nsid)
            if not a or not a["active"] or a["capacity"] <= 0:
                continue
            target = max(3, round(a["capacity"] * 0.2))
            deficit = target - a["bikes"]
            if deficit <= 0 or a["docks"] <= 0:
                continue
            walk_m = dist * A["walk_detour"]
            walk_min = walk_m / 1000.0 / A["walk_kmh"] * 60.0
            leg = _hav(o["lat"], o["lon"], a["lat"], a["lon"]) * A["road_detour"]
            rmin = leg / 1000.0 / A["ride_kmh"] * 60.0
            extra = (rmin + walk_min) - ride_min
            # 通勤族不會為了點數多繞太久。超過 12 分鐘就不算「順路」，直接不推。
            if extra > 12:
                continue
            mult = _multiplier(deficit / target, 60, 8 if walk_min >= 8 else 0)
            cand = {"kind": "順路集點", "station_sid": nsid, "station": a["name"],
                    "ride_min": round(rmin, 1), "walk_min": round(walk_min, 1),
                    "total_min": round(rmin + walk_min, 1),
                    "risk": None, "risk_source": "—",
                    "docks_now": a["docks"],
                    "multiplier": mult, "points": _points(mult), "deficit": deficit,
                    "extra_min": round(max(0.0, extra), 1),
                    "points_per_extra_min": (round(_points(mult) / extra, 1) if extra > 0.5
                                             else _points(mult)),
                    "note": (f"這站還差 {deficit} 輛，把車還過去可得 {_points(mult)} 元"
                             f"（×{mult} 加碼）；"
                             + (f"只比最快方案多花 {extra:.0f} 分鐘" if extra > 0.5
                                else "而且不比最快方案慢"))}
            # 以「每多花一分鐘換到幾點」排序，而不是只看倍率高低
            if quest is None or cand["points_per_extra_min"] > quest["points_per_extra_min"]:
                quest = cand
    if quest:
        plans.append(quest)

    co2_saved = round(road_m / 1000.0 * A["co2_scooter_g_per_km"])
    return {
        "from": {"sid": from_sid, "name": o["name"], "district": o["district"],
                 "bikes": o["bikes"], "docks": o["docks"],
                 "risk_no_bike": None if p_start is None else round(p_start * 100, 1),
                 "risk_source": src_start},
        "to": {"sid": to_sid, "name": d["name"], "district": d["district"],
               "bikes": d["bikes"], "docks": d["docks"]},
        "distance_m": round(straight), "road_m": round(road_m),
        "plans": plans,
        "compare": {"co2_saved_g_vs_scooter": co2_saved,
                    "free_minutes": A["youbike_free_min"]},
        "assumptions": {k: A[k] for k in
                        ("ride_kmh", "walk_kmh", "road_detour", "walk_detour",
                         "reroute_penalty_min", "youbike_free_min", "co2_scooter_g_per_km")},
        "caveat": ("直線距離乘以繞路係數估算，不是路線導航，沒有真實路網與號誌；"
                   "風險為到站當下無位可還的機率，不等於旅次失敗率；"
                   "點數為示範機制，兌付尚未取得。"),
    }


# ---------------------------------------------------------------- 獎勵
def _points(multiplier):
    """單次點數 = 5 × 倍率，固定四捨五入。

    點數與新臺幣 1:1（docs/REWARDS.md 的設計：基礎 5 元、單次上限 25 元），
    畫面上直接寫「元」比寫「點」有感——但必須同時標明是示範機制、兌付尚未取得。

    不用內建 round()：它是銀行家捨入，round(22.5)=22 但 round(23.5)=24，
    會讓 ×4.5 的任務比 ×4.4 給得還少，對使用者是莫名其妙的。
    """
    return int(math.floor(5.0 * multiplier + 0.5))


def _multiplier(deficit_ratio, minutes_to_target, extra_walk_min):
    """docs/REWARDS.md 第 1 層：倍率 = 1 + min(4, 缺口 + 急迫 + 距離)。"""
    w_gap = min(2.0, deficit_ratio * 2.0)
    w_urg = 1.5 if minutes_to_target < 60 else (1.0 if minutes_to_target < 120 else 0.5)
    w_dist = 0.5 if extra_walk_min >= 8 else 0.0
    return round(1.0 + min(4.0, w_gap + w_urg + w_dist), 2)


# 兌換目錄：讓點數有具體的價值感。**全部是示範**，合作商家尚未洽談。
CATALOG = [
    {"tier": 30, "title": "超商中杯咖啡折 10 元", "note": "示範品項"},
    {"tier": 50, "title": "YouBike 騎乘金 50 元", "note": "示範品項"},
    {"tier": 120, "title": "捷運單程票一張", "note": "示範品項"},
    {"tier": 250, "title": "合作店家 100 元抵用券", "note": "示範品項，商家未洽談"},
    {"tier": 500, "title": "月租型輕騎方案折抵", "note": "示範品項"},
]
SEED_STAMPS = 2          # 已賦予進度效應：新用戶一開始就有 2 枚，不是從 0 開始


def _wallet(rw):
    n = len(rw.get("stamps", [])) + rw.get("seed_stamps", SEED_STAMPS)
    pts = rw.get("points", 0)
    nxt = next((c for c in CATALOG if c["tier"] > pts), None)
    streak = rw.get("streak_days", 0)
    milestones = [3, 7, 14, 30]
    next_ms = next((m for m in milestones if m > streak), None)
    return {
        "points": pts,
        "stamps": n,
        "stamps_in_card": (n % 10) or (10 if n else 0),
        "cards_completed": n // 10,
        "streak_days": streak,
        "streak_next_milestone": next_ms,
        "streak_days_to_milestone": (next_ms - streak) if next_ms else None,
        "level": ("白金調度師" if n >= 30 else "黃金調度師" if n >= 20
                  else "白銀調度師" if n >= 10 else "青銅調度師"),
        "next_level_at": 10 if n < 10 else 20 if n < 20 else 30 if n < 30 else n,
        "seeded": rw.get("seed_stamps", SEED_STAMPS),
        "next_reward": nxt,
        "points_to_next": (nxt["tier"] - pts) if nxt else None,
        "catalog": CATALOG,
    }


@router.get("/rewards/board")
def rewards_board(sid: int = None):
    """動態加碼任務板：只在真的有缺口的站加碼，並標出還差幾輛就解除。"""
    lv = _live()
    risk = _CTX.get("risk")
    rw = _CTX["state"].get("rewards", {})
    quests = []
    for x in lv.snapshot().values():
        if not x["active"] or x["capacity"] <= 0:
            continue
        target_min = max(3, round(x["capacity"] * 0.2))
        r60 = risk.at(x["sid"], ahead_min=60) if (risk and risk.ok) else None
        # 缺車型任務：鼓勵「還車到這一站」
        if x["bikes"] < target_min:
            deficit = target_min - x["bikes"]
            mult = _multiplier(deficit / target_min, 60 if (r60 and r60["p_no_bike"] > .3) else 120, 0)
            quests.append({"sid": x["sid"], "name": x["name"], "district": x["district"],
                           "lat": x["lat"], "lon": x["lon"], "kind": "還車到這站",
                           "deficit": deficit, "multiplier": mult, "points": _points(mult),
                           "remaining_to_clear": deficit,
                           "hist_risk_60": round(r60["p_no_bike"] * 100, 1) if r60 else None})
        # 滿站型任務：鼓勵「從這一站借車騎走」
        if x["docks"] < target_min:
            deficit = target_min - x["docks"]
            mult = _multiplier(deficit / target_min, 60 if (r60 and r60["p_no_dock"] > .3) else 120, 0)
            quests.append({"sid": x["sid"], "name": x["name"], "district": x["district"],
                           "lat": x["lat"], "lon": x["lon"], "kind": "從這站借走",
                           "deficit": deficit, "multiplier": mult, "points": _points(mult),
                           "remaining_to_clear": deficit,
                           "hist_risk_60": round(r60["p_no_dock"] * 100, 1) if r60 else None})
    quests.sort(key=lambda q: (-q["multiplier"], -q["deficit"]))
    top = quests[:40]
    budget = sum(q["deficit"] * 25 for q in top)
    issued = sum(h.get("points", 0) for h in rw.get("history", [])
                 if str(h.get("ts", "")).startswith(datetime.now(TZ).strftime("%Y-%m-%d")))
    return {
        "wallet": _wallet(rw),
        "quests": top, "quest_total": len(quests),
        "budget_cap_twd": budget,
        "issued_today_points": issued,
        "point_value_twd": 1,
        "rules": {
            "point_value": "1 點 = 1 元（設計上 1:1；示範機制，兌付尚未取得）",
            "formula": "倍率 = 1 + min(4, 缺口權重 + 急迫權重 + 距離權重)；單次上限 25 元",
            "guards": ["借還同站不計", "騎乘距離需 ≥400 公尺", "沿用官方每帳號 10 分鐘最多 2 次"],
            "baseline": ("官方友愛接力 2026-01-01~06-30 北北桃試辦：日均發券 17,323 張、"
                         "見車率 +4.62%、見位率 +1.52%（官方公布數字）"),
        },
        "caveat": "點數與優惠為示範機制，兌付與合作商家尚未取得，不代表可實際兌換。"}


class ClaimIn(BaseModel):
    sid: int
    kind: str
    request_id: str = None


@router.post("/rewards/claim")
def rewards_claim(body: ClaimIn):
    """領取任務：示範用，寫進既有 STATE.rewards，並套用防呆與冪等。"""
    st = _CTX["state"]
    rw = st.setdefault("rewards", {})
    rw.setdefault("history", []); rw.setdefault("stamps", []); rw.setdefault("points", 0)
    claimed = rw.setdefault("claimed_ids", [])
    if body.request_id and body.request_id in claimed:
        return {"ok": True, "idempotent": True, "points": rw["points"]}
    lv = _live()
    x = lv.station(body.sid)
    if not x:
        raise HTTPException(404, "查無此站的即時資料")
    cap = max(1, x["capacity"])
    target_min = max(3, round(cap * 0.2))
    deficit = (target_min - x["bikes"]) if body.kind == "還車到這站" else (target_min - x["docks"])
    if deficit <= 0:
        raise HTTPException(409, "這一站目前已經沒有缺口，加碼已解除")
    mult = _multiplier(deficit / target_min, 60, 0)
    pts = _points(mult)
    rw["points"] = rw.get("points", 0) + pts
    rw["streak_days"] = rw.get("streak_days", 0) + (0 if rw.get("streak_today") else 1)
    rw["streak_today"] = True
    rw["stamps"].append({"name": "接力章", "type": "relay",
                         "ts": datetime.now(TZ).strftime("%Y-%m-%d %H:%M")})
    rw["history"].insert(0, {"ts": datetime.now(TZ).strftime("%Y-%m-%d %H:%M"),
                             "points": pts,
                             "reason": f"{body.kind}：{x['name']}（×{mult} 加碼，示範紀錄）"})
    if body.request_id:
        claimed.append(body.request_id)
    w = _wallet(rw)
    return {"ok": True, "points": rw["points"], "gained": pts, "multiplier": mult,
            "stamps": w["stamps"], "level": w["level"], "streak_days": w["streak_days"],
            "wallet": w, "caveat": "示範機制，不代表可實際兌換"}


@router.get("/rewards/leaderboard")
def leaderboard():
    """第 3 層：團體賽。示範資料，明確標示。"""
    rw = _CTX["state"].get("rewards", {})
    me = _wallet(rw)["points"]
    teams = [{"team": "板橋隊", "points": 12840, "members": 312},
             {"team": "新莊隊", "points": 11226, "members": 288},
             {"team": "中和隊", "points": 9871, "members": 265},
             {"team": "三重隊", "points": 9540, "members": 251},
             {"team": "土城隊", "points": 7233, "members": 190}]
    return {"teams": teams, "me": {"points": me, "team": "板橋隊"},
            "week_task": {"title": "本週完成 3 次尖峰分流", "progress": min(3, len(rw.get("stamps", []))),
                          "target": 3, "bonus": "50 元券"},
            "caveat": "排行榜為示範資料，非真實用戶。"}
