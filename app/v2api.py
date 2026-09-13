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
import re
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
from fastapi import APIRouter, HTTPException
from fastapi.routing import APIRoute
from fastapi.responses import JSONResponse
from pydantic import BaseModel

TZ = timezone(timedelta(hours=8))
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
ANA = os.path.join(ROOT, "data", "analytics")

class _StripMarkdownRoute(APIRoute):
    """把回應裡殘留的 markdown 粗體標記剝掉。

    前端用 esc() 把字串直接塞進 HTML，字串裡的 ** 只會原樣顯示成星號。
    只有在 body 真的含有 ** 時才重新編碼，所以一般請求不用付代價。
    """

    def get_route_handler(self):
        original = super().get_route_handler()

        async def handler(request):
            response = await original(request)
            body = getattr(response, "body", None)
            if body and b"**" in body:
                try:
                    return JSONResponse(content=_sanitize(json.loads(body)),
                                        status_code=response.status_code)
                except (ValueError, TypeError):
                    return response
            return response

        return handler


router = APIRouter(prefix="/api/v2", tags=["v2"], route_class=_StripMarkdownRoute)

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


def init(live_store, stations_df, state, planner=None, forecaster=None, submit_ticket=None):
    # submit_ticket 一定要用傳的，不可以在請求裡 import server——
    # uvicorn 載入的是 app.server，用裸名 import server 會產生第二份模組實例，
    # 重跑整個模組、建立新的 Predictor 與 LiveStore，而且它的 V2.init() 會把這份 _CTX 蓋掉。
    # 症狀是任務突然消失、即時層變成 ok=0 fetched_at=None。已踩過一次。
    _CTX["live"] = live_store
    _CTX["fc"] = forecaster
    _CTX["submit_ticket"] = submit_ticket
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


_MD_BOLD = re.compile(r"\*\*(.+?)\*\*", re.S)


def _sanitize(o):
    """NaN/Inf 進到回應會讓 FastAPI 直接 500；順便剝掉 markdown 粗體標記。

    前端是用 esc() 把字串直接塞進 HTML 的，字串裡寫 ** 只會原樣顯示成星號。
    這個錯已經逐條修過三次還是再犯（insights、ops semantics、order caveat），
    所以改成在邊界統一處理，而不是靠每次記得不要寫。
    """
    if isinstance(o, str):
        return _MD_BOLD.sub(r"\1", o)
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


# ---------------------------------------------------------------- 資料與方法
@router.get("/method")
def method():
    """模型卡與資料稽核。給要檢查我們有沒有唬爛的人看的。"""
    ev = _load(os.path.join(ROOT, "reports", "model_eval.json"), {})
    s = _CTX.get("summary", {})
    lv = _CTX.get("live")
    fc = _CTX.get("fc")
    rows = []
    for hm, h in sorted((ev.get("horizons") or {}).items(), key=lambda x: int(x[0])):
        b, g = h.get("baselines", {}), h.get("hgb", {})
        allg = g.get("all", {})
        pers = (b.get("persistence") or {}).get("all", {})
        prof = (b.get("profile") or {}).get("all", {})
        e, f = allg.get("empty", {}), allg.get("full", {})
        rows.append({
            "horizon_min": int(hm), "n_test": h.get("n_test"),
            "mae_bikes": {"persistence": pers.get("mae_bikes"),
                          "profile": prof.get("mae_bikes"), "model": allg.get("mae_bikes")},
            "mae_spaces": {"persistence": pers.get("mae_spaces"),
                           "profile": prof.get("mae_spaces"), "model": allg.get("mae_spaces")},
            "empty_new_event": {"pr_auc": e.get("new_event_pr_auc"),
                                "recall": e.get("new_event_recall"),
                                "precision": e.get("new_event_precision"),
                                "positives": e.get("positives"), "new_events": e.get("new_events")},
            "full_new_event": {"pr_auc": f.get("new_event_pr_auc"),
                               "recall": f.get("new_event_recall"),
                               "precision": f.get("new_event_precision"),
                               "positives": f.get("positives"), "new_events": f.get("new_events")},
            "segments": {k: {"mae_bikes": (g.get(k) or {}).get("mae_bikes")}
                         for k in ("mrt", "school", "weekday_peak") if g.get(k)},
        })
    cal = (((ev.get("horizons") or {}).get("120") or {}).get("hgb", {})
           .get("all", {}).get("empty", {}).get("calibration"))
    meta = s.get("meta", {})
    return {
        "data": {
            "period": meta.get("period"), "rows": meta.get("rows"),
            "stations_total": meta.get("stations_total"),
            "stations_analyzed": meta.get("stations_analyzed"),
            "step": "半小時分箱", "day_window": meta.get("day_window"),
        },
        "audit": {
            "double_zero_cells": meta.get("double_zero_cells"),
            "double_zero_stations": meta.get("double_zero_stations"),
            "dead_stations": meta.get("dead_stations"),
            "live_capacity_gap_stations": (lv.stats() if lv else {}).get("capacity_gap_stations"),
            "live_capacity_gap_units": (lv.stats() if lv else {}).get("capacity_gap_units"),
            "live_new_stations": (lv.stats() if lv else {}).get("new_stations"),
            "live_inactive": (lv.stats() if lv else {}).get("inactive"),
            "notes": [
                "雙零快照語意未定：可能是整站服務中斷，也可能是資料中斷。單獨統計，不混入空站或滿站。",
                "六月整月零車的站是退場或未投車，不是調度失敗，已從所有排名剔除。",
                "容量落差＝官方總格數−(可借+可還)。不判定根因，也不等同故障台數。",
                "2026-06 之後新增的站沒有訓練期輪廓，不做模型預測，只給即時現況。",
            ],
        },
        "split": ev.get("split"), "n_train_sample": ev.get("n_train_sample"),
        "model": {"algorithm": "scikit-learn HistGradientBoosting",
                  "targets": "回歸目標為變化量（t+h 減 t）；分類目標為 t+h 零車／零位",
                  "origin": fc.status().get("model_origin") if fc else None,
                  "horizons": rows},
        "calibration_120_empty": cal,
        "live_inference": fc.status() if fc else None,
        "comparison": {"rates": s.get("rates", {})},
        "caveats": [
            "六月測試集是一次性的最終測試，不是即時推論的準確度。",
            "零車新事件的召回偏低（180 分 0.131），不可只用 MAE 進步宣稱預警完整。",
            "所有比例都是半小時快照比例，不是連續中斷時長，也不是旅次成功率。",
            "臺北的見車率／見位率演算法可能帶容忍參數，與我們的純快照比例不是同一套，只能當量級參考。",
        ],
    }


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
                         "同一台車早上該從住家端往目的地端走，傍晚要反過來。"
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
                             + ("落後特徵尚未累積完整，準確度會低於模型卡的離線指標。"
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
            "semantics": ("候選清單依即時站況與歷史同時段風險排序，不是派車任務；"
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
    today = datetime.now(TZ).strftime("%Y-%m-%d")
    hist_today = [h for h in rw.get("history", []) if str(h.get("ts", "")).startswith(today)]
    issued = sum(h.get("points", 0) for h in hist_today)
    # 分流成效換算。每次分流當作補上 1 輛；派車一趟載 20 輛、約 600 元
    # （2 人 × 1.27 小時 × 200 元/時 ＋ 油耗折舊，人事費率為情境假設，見 docs/REWARDS.md）。
    moved = len(hist_today)
    truck_cap, truck_cost = 20, 600
    impact = {
        "claims_today": moved,
        "bikes_redistributed": moved,
        "incentive_cost_twd": issued,
        "equivalent_truck_trips": round(moved / truck_cap, 2),
        "equivalent_truck_cost_twd": round(moved / truck_cap * truck_cost),
        "saved_twd": round(moved / truck_cap * truck_cost) - issued,
        "assumptions": {"truck_capacity": truck_cap, "truck_cost_twd": truck_cost,
                        "note": ("每次分流以補上 1 輛計；派車成本為情境假設，"
                                 "人事費率未經實際派工紀錄校準。")},
        "caveat": ("這是把示範的領取紀錄換算成等值派車成本，不是實測成效。"
                   "真實的分流轉換率、是否真的騎到目標站、以及是否排擠原本就會發生的旅次，"
                   "都需要實際試辦才知道。"),
    }
    return {
        "wallet": _wallet(rw),
        "quests": top, "quest_total": len(quests),
        "budget_cap_twd": budget,
        "issued_today_points": issued,
        "impact": impact,
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


# ==================================================================== 政府端專用
# 目標是讓值班的人打開就知道「我現在要做哪些決策」，而不是自己從地圖上找問題。
OPT = None


def _opt():
    global OPT
    if OPT is None:
        OPT = _load(os.path.join(ANA, "optimization.json"), {})
    return OPT


# 每一種問題對應的建議處置。default_message 是預填的命令稿，畫面上可以改。
ACTIONS = {
    "no_dock": [
        {"key": "ops_clear", "label": "請微笑單車清運", "to": "微笑單車",
         "why": "可還柱位見底，且一到兩小時後仍高風險",
         "msg": "【清運】{name}（{district}）目前可還 {docks} 位、容量 {cap}。"
                "請安排清運，目標把可還柱位回到 {target} 位以上。"},
        {"key": "incentive", "label": "啟動民眾加碼分流", "to": "民眾端",
         "why": "派車來不及或不划算時，用誘因請民眾把車騎走",
         "msg": "【分流】對 {name} 啟動「從這站借走」加碼，缺口 {gap_docks} 位，"
                "補滿後自動解除。"},
        {"key": "watch", "label": "持續觀察，先不動用資源", "to": "值班",
         "why": "歷史同時段風險不高，可能是偶發",
         "msg": "【觀察】{name} 暫不派車。理由："},
    ],
    "no_bike": [
        {"key": "ops_supply", "label": "請微笑單車送車", "to": "微笑單車",
         "why": "可借車輛見底，且一到兩小時後仍高風險",
         "msg": "【送車】{name}（{district}）目前可借 {bikes} 台、容量 {cap}。"
                "請安排送車，目標把可借回到 {target} 台以上。"},
        {"key": "incentive", "label": "啟動民眾加碼分流", "to": "民眾端",
         "why": "派車前置至少 60 分鐘，來不及的缺口用誘因補",
         "msg": "【分流】對 {name} 啟動「還車到這站」加碼，缺口 {gap_bikes} 台，"
                "補滿後自動解除。"},
        {"key": "watch", "label": "持續觀察，先不動用資源", "to": "值班",
         "why": "歷史同時段風險不高，可能是偶發",
         "msg": "【觀察】{name} 暫不派車。理由："},
    ],
    "offline": [
        {"key": "equipment", "label": "開設備查修單", "to": "微笑單車",
         "why": "可借與可還同時為 0，派車也沒有柱位可用，要先查修",
         "msg": "【查修】{name}（{district}）可借與可還同時為 0，容量 {cap}。"
                "請確認是設備離線、站端斷線還是整站未投車。此站不列入調度優先序。"},
        {"key": "watch", "label": "先確認是否為官方暫停營運", "to": "值班",
         "why": "官方可能已標示暫停，與故障是兩件事",
         "msg": "【查證】{name} 借還皆 0，先確認官方營運狀態再決定是否派工。"},
    ],
    "capacity_gap": [
        {"key": "equipment", "label": "排入巡檢確認落差原因", "to": "微笑單車",
         "why": "總格數不等於可借加可還，差額既借不到也還不了",
         "msg": "【巡檢】{name}（{district}）容量落差 {gap} 個車柱"
                "（總 {cap}／可借 {bikes}／可還 {docks}）。"
                "請確認是設備故障、保留柱位還是資料延遲。本工具不判定根因。"},
        {"key": "watch", "label": "記錄後持續觀察", "to": "值班",
         "why": "落差小或為已知的保留柱位",
         "msg": "【觀察】{name} 容量落差 {gap}，暫不派工。理由："},
    ],
}


def _severity(item):
    s = 0
    s += {"offline": 40, "no_dock": 30, "no_bike": 28, "capacity_gap": 12}.get(item["kind"], 0)
    s += min(30, (item.get("risk_60") or 0) * 0.4)
    s += min(20, (item.get("gap") or 0) * 1.2)
    if item.get("mrt"):
        s += 8
    return round(s, 1)


@router.get("/gov/decisions")
def gov_decisions(limit: int = 25):
    """我現在要做哪些決策——把此刻有問題的站整理成待決事項，每一項都附建議處置。"""
    lv = _live()
    risk = _CTX.get("risk")
    fc = _CTX.get("fc")
    meta = _CTX.get("stations", {})
    fc_ok = fc is not None and fc.status().get("available")
    items = []
    for x in lv.snapshot().values():
        if not x["active"] or x["capacity"] <= 0:
            continue
        target = max(3, round(x["capacity"] * 0.2))
        if x["bikes"] == 0 and x["docks"] == 0:
            kind = "offline"
        elif x["docks"] == 0 or x["docks"] < target * 0.5:
            kind = "no_dock"
        elif x["bikes"] == 0 or x["bikes"] < target * 0.5:
            kind = "no_bike"
        elif x["capacity_gap"] > 5:
            kind = "capacity_gap"
        else:
            continue

        r60 = risk.at(x["sid"], ahead_min=60) if (risk and risk.ok) else None
        key = "p_no_dock" if kind == "no_dock" else "p_no_bike"
        hist60 = round(r60[key] * 100, 1) if r60 else None
        model = None
        if fc_ok and kind in ("no_dock", "no_bike"):
            rr = fc.risk_ranking("full" if kind == "no_dock" else "empty", 120, 400, 0.0)
            hit = next((z for z in rr if z["sid"] == x["sid"]), None)
            model = hit["p"] if hit else None

        m = meta.get(x["sid"], {})
        it = {
            "id": f"D{x['sid']}-{kind}",
            "sid": x["sid"], "name": x["name"], "district": x["district"],
            "kind": kind, "kind_label": EVENT_KINDS.get(kind, kind),
            "bikes": x["bikes"], "docks": x["docks"], "cap": x["capacity"],
            "gap": x["capacity_gap"], "target": target,
            "gap_bikes": max(0, target - x["bikes"]), "gap_docks": max(0, target - x["docks"]),
            "risk_60": hist60, "model_120": model,
            "profile": m.get("profile"), "retired_in_history": bool(m.get("retired")),
            "mrt": "捷運" in x["name"],
            "age_min": x["age_min"],
        }
        it["severity"] = _severity({**it, "risk_60": hist60})
        ev = [f"官方即時：可借 {x['bikes']}／可還 {x['docks']}／容量 {x['capacity']}"
              f"（{x['age_min']} 分鐘前）"]
        if x["capacity_gap"]:
            ev.append(f"容量落差 {x['capacity_gap']} 個車柱，既借不到也還不了")
        if hist60 is not None:
            ev.append(f"歷史同時段一小時後{'無位' if kind == 'no_dock' else '無車'}機率 {hist60}%")
        if model is not None:
            ev.append(f"模型即時推論 120 分鐘後機率 {model}%")
        if m.get("profile"):
            ev.append(f"站點分型：{m['profile']}")
        if m.get("retired"):
            ev.append("歷史上六月整月零車，可能已退場——處置前先確認營運狀態")
        it["evidence"] = ev
        acts = []
        for a in ACTIONS.get(kind, []):
            acts.append({**{k: a[k] for k in ("key", "label", "to", "why")},
                         "message": a["msg"].format(
                             name=x["name"], district=x["district"], bikes=x["bikes"],
                             docks=x["docks"], cap=x["capacity"], gap=x["capacity_gap"],
                             target=target, gap_bikes=it["gap_bikes"],
                             gap_docks=it["gap_docks"])})
        # 推薦哪一個。第一版只看未來風險，結果「此刻可還位已經是 0」的站被建議「持續觀察」——
        # 問題已經在發生，預測講的是未來，兩件事不能混為一談。
        already = (kind == "no_dock" and x["docks"] == 0) or (kind == "no_bike" and x["bikes"] == 0)
        # 說明是哪一個數字支撐判斷，不要只說「高風險」讓人無從查核
        src = []
        if (hist60 or 0) >= 20:
            src.append(f"歷史同時段一小時後 {hist60}%")
        if (model or 0) >= 20:
            src.append(f"模型 120 分鐘後 {model}%")
        persists = bool(src)
        basis = "、".join(src)
        if kind in ("offline", "capacity_gap"):
            rec, why = acts[0]["key"], "設備類問題調度補不到，要先查修"
        elif already and persists:
            rec, why = acts[0]["key"], f"此刻已經見底，且{basis}——屬結構性，值得派資源"
        elif already:
            rec, why = "incentive", (f"此刻已經見底，但一到兩小時後的風險不高"
                                     f"（歷史同時段 {hist60 if hist60 is not None else '—'}%"
                                     f"、模型 {model if model is not None else '—'}%），"
                                     "通常會自己緩解；先用誘因分流，比派車便宜")
        elif persists:
            rec, why = acts[0]["key"], f"現在還沒見底，但{basis}——趁前置時間還夠先排"
        else:
            rec, why = "watch", (f"尚未見底，一到兩小時後的風險也不高"
                                 f"（歷史同時段 {hist60 if hist60 is not None else '—'}%"
                                 f"、模型 {model if model is not None else '—'}%），先觀察不動用資源")
        it["recommended"] = rec
        it["recommend_reason"] = why
        it["actions"] = acts
        items.append(it)

    items.sort(key=lambda z: -z["severity"])
    done = {e["sid"] for e in _events() if e["status"] != "closed"}
    for z in items:
        z["already_open"] = z["sid"] in done
    return {
        "generated_at": datetime.now(TZ).isoformat(),
        "count": len(items), "decisions": items[:limit],
        "summary": {
            "offline": sum(1 for z in items if z["kind"] == "offline"),
            "no_dock": sum(1 for z in items if z["kind"] == "no_dock"),
            "no_bike": sum(1 for z in items if z["kind"] == "no_bike"),
            "capacity_gap": sum(1 for z in items if z["kind"] == "capacity_gap"),
            "recommend_dispatch": sum(1 for z in items if z["recommended"] != "watch"),
        },
        "semantics": ("嚴重度是本工具的排序分數，不是官方分級。建議處置只是預填，"
                      "實際要不要動用資源由值班決定。本系統不產生 ETA。"),
    }


class OrderIn(BaseModel):
    sid: int
    kind: str
    action: str
    message: str
    owner: str = None
    request_id: str = None


@router.post("/gov/order")
def gov_order(body: OrderIn):
    """在畫面上直接發佈命令：建立事件、寫下命令原文與收件對象、留痕。"""
    lv = _live()
    x = lv.station(body.sid)
    if not x:
        raise HTTPException(404, "查無此站的即時資料")
    if not (body.message or "").strip():
        raise HTTPException(400, "命令內容不可為空")
    kind = body.kind if body.kind in EVENT_KINDS else "capacity_gap"
    res = event_create(EventIn(sid=body.sid, kind=kind,
                               note=body.message.strip(),
                               request_id=body.request_id))
    eid = res["event"]["id"]
    e = next(z for z in _events() if z["id"] == eid)
    to = next((a["to"] for a in ACTIONS.get(kind, []) if a["key"] == body.action), "值班")
    now = datetime.now(TZ)
    e.setdefault("orders", []).append({
        "ts": now.isoformat(), "action": body.action, "to": to,
        "message": body.message.strip(), "by": body.owner or "監看",
    })
    if body.action != "watch":
        e["owner"] = body.owner or to
        e["status"] = "assigned"
    e["version"] += 1
    e["log"].append({"ts": now.isoformat(), "action": f"order:{body.action}",
                     "by": body.owner or "監看", "note": body.message.strip()[:120]})
    return {"ok": True, "event": _event_view(e), "sent_to": to,
            "caveat": ("命令已記錄在事件台帳並標示收件對象。"
                       "**這是示範：沒有與微笑單車的實際派工系統介接**，不代表對方已收到。")}


@router.get("/gov/search")
def gov_search(q: str, limit: int = 20):
    """搜尋站點：站名、行政區、地址都找。"""
    q = (q or "").strip()
    if len(q) < 1:
        return {"count": 0, "stations": []}
    lv = _live()
    meta = _CTX.get("stations", {})
    out = []
    for x in lv.snapshot().values():
        if q in x["name"] or q in x["district"] or q in (x["address"] or ""):
            m = meta.get(x["sid"], {})
            out.append({"sid": x["sid"], "name": x["name"], "district": x["district"],
                        "address": x["address"], "bikes": x["bikes"], "docks": x["docks"],
                        "cap": x["capacity"], "gap": x["capacity_gap"], "act": x["active"],
                        "profile": m.get("profile"),
                        "am_no_dock": m.get("am_no_dock"), "park_june": m.get("park_june")})
        if len(out) >= limit * 3:
            break
    # 有狀況的排前面
    out.sort(key=lambda z: (z["bikes"] > 0 and z["docks"] > 0, z["name"]))
    return {"count": len(out), "stations": out[:limit]}


@router.get("/gov/district/{name}")
def gov_district(name: str):
    """點一個行政區進來：這一區此刻怎麼樣、歷史上怎麼樣、要優先處理誰。"""
    lv = _live()
    s = _CTX.get("summary", {})
    meta = _CTX.get("stations", {})
    rows = [x for x in lv.snapshot().values() if x["district"] == name]
    if not rows:
        raise HTTPException(404, "查無此行政區的即時資料")
    act = [x for x in rows if x["active"]]
    n = len(rows) or 1
    hist = next((d for d in s.get("districts", []) if d["district"] == name), None)
    profiles = {}
    burden = []
    for x in rows:
        m = meta.get(x["sid"], {})
        if m.get("profile"):
            profiles[m["profile"]] = profiles.get(m["profile"], 0) + 1
        if m.get("burden") is not None:
            burden.append(m["burden"])
    worst = sorted(act, key=lambda x: (x["docks"], x["bikes"]))[:8]
    gaps = sorted([x for x in rows if x["capacity_gap"] > 0],
                  key=lambda x: -x["capacity_gap"])[:8]
    opt = _opt().get("periods", {}).get("m1", {})
    idle_b = [z for z in opt.get("idle", {}).get("top_bikes", []) if z["district"] == name]
    idle_d = [z for z in opt.get("idle", {}).get("top_docks", []) if z["district"] == name]
    return {
        "district": name,
        "live": {
            "stations": len(rows), "inactive": sum(1 for x in rows if not x["active"]),
            "no_bike": sum(1 for x in act if x["no_bike"]),
            "no_dock": sum(1 for x in act if x["no_dock"]),
            "both_zero": sum(1 for x in act if x["both_zero"]),
            "bikes": sum(x["bikes"] for x in rows),
            "bikes_electric": sum(x["bikes_electric"] for x in rows),
            "capacity": sum(x["capacity"] for x in rows),
            "gap_stations": sum(1 for x in rows if x["capacity_gap"] > 0),
            "gap_units": sum(max(0, x["capacity_gap"]) for x in rows),
            "fill_pct": round(sum(x["bikes"] for x in rows) / max(1, sum(x["capacity"] for x in rows)) * 100, 1),
        },
        "history": hist,
        "profiles": profiles,
        "burden_median": round(float(np.median(burden)), 3) if burden else None,
        "worst_now": [{"sid": x["sid"], "name": x["name"], "bikes": x["bikes"],
                       "docks": x["docks"], "cap": x["capacity"],
                       "profile": meta.get(x["sid"], {}).get("profile")} for x in worst],
        # 官方暫停營運的站，整站容量都會算進落差——那不是設備落差，要標出來不然會誤導
        "capacity_gaps": [{"sid": x["sid"], "name": x["name"], "gap": x["capacity_gap"],
                           "cap": x["capacity"], "act": x["active"],
                           "both_zero": x["both_zero"]} for x in gaps],
        "idle_bikes": idle_b[:6], "idle_docks": idle_d[:6],
        "semantics": ("此刻數字為官方即時資料；歷史為 2026-01~06 平日早峰的半小時快照比例。"
                      "兩者口徑不同，不可相減。"),
    }


@router.get("/gov/optimization")
def gov_optimization(period: str = "m1"):
    """可優化空間。**不是已經發生的成效**——我們沒有優化前後的對照組。"""
    o = _opt()
    if not o:
        raise HTTPException(503, "尚未產生，請先跑 analytics/build_optimization.py")
    p = o.get("periods", {}).get(period)
    if not p:
        raise HTTPException(400, f"period 必須是 {list(o.get('periods', {}))} 其中之一")
    return {"meta": o["meta"], "period_key": period, "period": p,
            "available_periods": [{"key": k, "label": v["label"], "days": v["days"]}
                                  for k, v in o["periods"].items()]}


# ==================================================================== 微笑單車端
# 兩個角色看的東西不一樣：
#   監控台   全市視角，決定「要不要做、誰去做」，可以批次派工
#   調度人員 只看自己今天負責的區，決定「先做哪一站、怎麼走、車上夠不夠」
#
# 誠實邊界（整段都要守）：
#   * 沒有 GPS 與車隊位置。路線的起點由調度人員自己選，不是系統知道他在哪。
#   * 路程是直線距離乘繞路係數估的，不是導航；沒有真實路網、號誌與單行道。
#   * 進度由人工回報，系統只能用官方即時站況**驗證現場是否真的恢復**，
#     兩者不一致時要標出來——「回報完成但站況未恢復」是最值得看的訊號。
TASK_KINDS = {"supply": "送車", "clear": "清運", "repair": "設備查修"}
TASK_FLOW = ["open", "assigned", "enroute", "arrived", "done"]


def _tasks():
    return _CTX["state"].setdefault("v2_tasks", [])


def _task_view(t):
    lv = _CTX.get("live")
    cur = lv.station(t["sid"]) if lv else None
    now = datetime.now(TZ)
    created = datetime.fromisoformat(t["created_at"])
    recovered = None
    if cur and t["kind"] in ("supply", "clear"):
        target = max(3, round(cur["capacity"] * 0.2))
        recovered = (cur["bikes"] >= target) if t["kind"] == "supply" else (cur["docks"] >= target)
    return {
        **t,
        "kind_label": TASK_KINDS.get(t["kind"], t["kind"]),
        "open_min": round((now - created).total_seconds() / 60.0, 1),
        "current": None if not cur else {"bikes": cur["bikes"], "docks": cur["docks"],
                                         "cap": cur["capacity"], "gap": cur["capacity_gap"]},
        "field_recovered": recovered,
        "mismatch": bool(t["status"] == "done" and recovered is False),
    }


@router.get("/ops/monitor")
def ops_monitor():
    """監控台：這一批要決定什麼、候選有誰、模型在預警什麼、設備有什麼要查。"""
    lv = _live()
    board = ops_board()
    fc = _CTX.get("fc")
    risk = _CTX.get("risk")
    fst = fc.status() if fc else {"available": False}
    alerts = []
    if fst.get("available"):
        hz = 120 if 120 in (fst.get("horizons") or []) else (fst.get("horizons") or [None])[-1]
        for kind, lab in (("empty", "無車可借"), ("full", "無位可還")):
            for r in fc.risk_ranking(kind, hz, 8, 0.3):
                alerts.append({**r, "kind": kind, "kind_label": lab, "horizon_min": hz})
        alerts.sort(key=lambda z: -z["p"])

    # 一批決策：候選裡真正需要現在決定的（結構性、或已經見底）
    batch = []
    for c in board["candidates"]:
        r60 = c.get("hist_risk_60")
        already = (c["need"] == "送車" and c["bikes"] == 0) or (c["need"] == "清運" and c["docks"] == 0)
        structural = (r60 or 0) >= 25
        if not (already or structural):
            continue
        kind = "supply" if c["need"] == "送車" else "clear"
        batch.append({
            "sid": c["sid"], "name": c["name"], "district": c["district"],
            "kind": kind, "kind_label": TASK_KINDS[kind], "qty": c["qty"],
            "bikes": c["bikes"], "docks": c["docks"], "cap": c["cap"],
            "hist_risk_60": r60, "hist_risk_120": c.get("hist_risk_120"),
            "profile": c.get("profile"), "priority": c["priority"],
            "already": already, "structural": structural,
            "why": ("此刻已見底" if already else "") + ("，" if already and structural else "")
                   + (f"一小時後仍有 {r60}% 風險" if structural else ""),
        })
    batch.sort(key=lambda z: -z["priority"])

    existing = {t["sid"] for t in _tasks() if t["status"] != "done"}
    for b in batch:
        b["already_assigned"] = b["sid"] in existing

    districts = {}
    for b in batch:
        d = districts.setdefault(b["district"], {"district": b["district"], "supply": 0,
                                                 "clear": 0, "qty": 0})
        d[b["kind"]] += 1
        d["qty"] += b["qty"]

    return {
        "generated_at": datetime.now(TZ).isoformat(),
        "batch": batch[:40], "batch_total": len(batch),
        "by_district": sorted(districts.values(), key=lambda d: -(d["supply"] + d["clear"])),
        "candidates": board["candidates"][:25],
        "offline": board["offline"], "repair": board["repair"],
        "totals": board["totals"],
        "alerts": alerts[:12], "forecast_status": fst,
        "open_tasks": sum(1 for t in _tasks() if t["status"] != "done"),
        "semantics": ("這一批是「已經見底」或「一小時後仍有 25% 以上風險」的站。"
                      "其餘候選在下方清單，不強迫現在決定。本看板不產生 ETA。"),
    }


class BatchIn(BaseModel):
    sids: list
    district: str = None
    assignee: str = None
    request_id: str = None


@router.post("/ops/tasks/batch")
def ops_batch(body: BatchIn):
    """一次把選好的站建成任務並指派。重複的站不會重建。"""
    lv = _live()
    mon = ops_monitor()
    by_sid = {b["sid"]: b for b in mon["batch"]}
    tasks = _tasks()
    have = {t["sid"] for t in tasks if t["status"] != "done"}
    now = datetime.now(TZ)
    made, skipped = [], []
    for sid in body.sids:
        sid = int(sid)
        if sid in have:
            skipped.append(sid)
            continue
        b = by_sid.get(sid)
        cur = lv.station(sid)
        if not cur:
            skipped.append(sid)
            continue
        if not b:
            target = max(3, round(cur["capacity"] * 0.2))
            kind = "clear" if cur["docks"] < target else "supply"
            qty = max(1, target - (cur["docks"] if kind == "clear" else cur["bikes"]))
            b = {"kind": kind, "qty": qty, "priority": 0, "why": "由監控台手動加入"}
        t = {
            "id": f"T{len(tasks) + len(made) + 1:04d}",
            "sid": sid, "name": cur["name"], "district": cur["district"],
            "lat": cur["lat"], "lon": cur["lon"],
            "kind": b["kind"], "qty": b["qty"], "priority": b.get("priority", 0),
            "why": b.get("why", ""),
            "status": "assigned" if (body.district or body.assignee) else "open",
            "assigned_district": body.district, "assignee": body.assignee,
            "created_at": now.isoformat(), "version": 1,
            "observed": {"bikes": cur["bikes"], "docks": cur["docks"], "cap": cur["capacity"]},
            "log": [{"ts": now.isoformat(), "action": "created", "by": "監控台",
                     "note": b.get("why", "")}],
            "progress": [],
        }
        tasks.append(t)
        made.append(t["id"])
    return {"ok": True, "created": made, "skipped_already_open": skipped,
            "total_open": sum(1 for t in tasks if t["status"] != "done"),
            "caveat": "建立任務不等於已派工；沒有與微笑單車的實際派工系統介接。"}


@router.get("/ops/districts")
def ops_districts():
    """調度人員上工時選今天負責哪幾區——附上每一區現在有多少事情。"""
    lv = _live()
    mon = ops_monitor()
    tasks = [t for t in _tasks() if t["status"] != "done"]
    rows = {}
    for x in lv.snapshot().values():
        d = rows.setdefault(x["district"], {"district": x["district"], "stations": 0,
                                            "no_bike": 0, "no_dock": 0, "tasks": 0, "batch": 0})
        d["stations"] += 1
        if x["active"]:
            d["no_bike"] += int(x["no_bike"])
            d["no_dock"] += int(x["no_dock"])
    for t in tasks:
        if t["district"] in rows:
            rows[t["district"]]["tasks"] += 1
    for b in mon["batch"]:
        if b["district"] in rows:
            rows[b["district"]]["batch"] += 1
    out = sorted(rows.values(), key=lambda d: -(d["tasks"] * 10 + d["batch"]))
    return {"districts": out}


@router.get("/ops/worker")
def ops_worker(districts: str = "", assignee: str = None):
    """調度人員看板：只顯示自己今天負責的區。"""
    ds = [d for d in (districts or "").split(",") if d.strip()]
    lv = _live()
    risk = _CTX.get("risk")
    tasks = [_task_view(t) for t in _tasks()
             if (not ds or t["district"] in ds) and t["status"] != "done"]
    tasks.sort(key=lambda t: (-t.get("priority", 0), t["open_min"] * -1))
    done_today = [_task_view(t) for t in _tasks()
                  if t["status"] == "done" and (not ds or t["district"] in ds)]

    # 還沒建成任務、但這幾區現在就有狀況的站
    mon_batch = [b for b in ops_monitor()["batch"] if not ds or b["district"] in ds]
    have = {t["sid"] for t in tasks}
    suggest = [b for b in mon_batch if b["sid"] not in have][:15]

    return {
        "districts": ds, "assignee": assignee,
        "tasks": tasks, "suggested": suggest,
        "done_today": done_today,
        "summary": {
            "open": len(tasks),
            "supply": sum(1 for t in tasks if t["kind"] == "supply"),
            "clear": sum(1 for t in tasks if t["kind"] == "clear"),
            "repair": sum(1 for t in tasks if t["kind"] == "repair"),
            "done": len(done_today),
            "mismatch": sum(1 for t in done_today if t["mismatch"]),
        },
        "semantics": ("優先序＝監控台算的優先度，主要吃一小時後的風險與缺口大小。"
                      "系統不知道你在哪裡，路線要自己選起點。"),
    }


class RouteIn(BaseModel):
    task_ids: list
    start_sid: int = None
    truck_capacity: int = 20
    onboard: int = 0


@router.post("/ops/route")
def ops_route(body: RouteIn):
    """把選好的任務排成一條路線，並做取送守恆檢查。

    路線：從你選的起點開始的最近鄰貪婪排序，**不是最佳解**，也不是導航。
    守恆：模擬車上載量，送車會減少、清運會增加；任何一站超過車容量或車上不夠，
          會直接標出來並試著把取車站往前挪。
    """
    import planner as PL
    A = PL.ASSUMPTIONS
    lv = _live()
    tmap = {t["id"]: t for t in _tasks()}
    picked = [tmap[i] for i in body.task_ids if i in tmap]
    if not picked:
        raise HTTPException(400, "沒有選到任何任務")
    cap = max(1, int(body.truck_capacity))
    start = lv.station(body.start_sid) if body.start_sid else None
    if start is None:
        la = sum(t["lat"] for t in picked) / len(picked)
        lo = sum(t["lon"] for t in picked) / len(picked)
        start_pt, start_name = (la, lo), "所選任務的重心（未指定起點）"
    else:
        start_pt, start_name = (start["lat"], start["lon"]), start["name"]

    # 最近鄰貪婪
    rest = list(picked)
    order, cur = [], start_pt
    while rest:
        nxt = min(rest, key=lambda t: _hav(cur[0], cur[1], t["lat"], t["lon"]))
        order.append(nxt)
        rest.remove(nxt)
        cur = (nxt["lat"], nxt["lon"])

    def simulate(seq):
        load, stops, ok = int(body.onboard), [], True
        prev = start_pt
        total_m = 0.0
        for t in seq:
            d = _hav(prev[0], prev[1], t["lat"], t["lon"]) * A["road_detour"]
            total_m += d
            drive = d / 1000.0 / A["truck_speed_kmh"] * 60.0
            qty = int(t["qty"])
            if t["kind"] == "supply":
                take = min(qty, load)
                short = qty - take
                load -= take
            elif t["kind"] == "clear":
                room = cap - load
                take = min(qty, room)
                short = qty - take
                load += take
            else:
                take, short = 0, 0
            if short > 0:
                ok = False
            handle = A["handling_fixed_min"] + take * A["handling_per_bike_min"]
            stops.append({"task_id": t["id"], "sid": t["sid"], "name": t["name"],
                          "district": t["district"], "kind": t["kind"],
                          "kind_label": TASK_KINDS[t["kind"]],
                          "qty_planned": qty, "qty_possible": take, "short": short,
                          "load_after": load,
                          "drive_min": round(drive, 1), "handle_min": round(handle, 1),
                          "leg_m": round(d)})
            prev = (t["lat"], t["lon"])
        return stops, ok, total_m, load

    stops, feasible, total_m, end_load = simulate(order)
    repaired = False
    if not feasible:
        # 把清運（會裝車）往前挪，讓後面的送車有車可放
        clears = [t for t in order if t["kind"] == "clear"]
        others = [t for t in order if t["kind"] != "clear"]
        alt = clears + others
        s2, ok2, m2, l2 = simulate(alt)
        if ok2 or sum(x["short"] for x in s2) < sum(x["short"] for x in stops):
            order, stops, feasible, total_m, end_load = alt, s2, ok2, m2, l2
            repaired = True

    # 只說「不可行」沒有用，現場人員需要知道該去哪裡取車。
    # 找路線附近庫存明顯過剩的站當供給站，並算出取多少才夠。
    pickups = []
    if not feasible:
        need = sum(s["short"] for s in stops if s["kind"] == "supply")
        if need > 0:
            picked_sids = {t["sid"] for t in picked}
            cands = []
            for x in lv.snapshot().values():
                if not x["active"] or x["sid"] in picked_sids or x["capacity"] <= 0:
                    continue
                keep = max(3, round(x["capacity"] * 0.2))
                surplus = x["bikes"] - keep          # 取走之後仍要留下最低水位
                if surplus < 3:
                    continue
                d = min(_hav(x["lat"], x["lon"], t["lat"], t["lon"]) for t in picked)
                cands.append({"sid": x["sid"], "name": x["name"], "district": x["district"],
                              "surplus": int(surplus), "bikes": x["bikes"], "cap": x["capacity"],
                              "detour_m": round(d)})
            cands.sort(key=lambda z: (z["detour_m"] / max(1, z["surplus"])))
            got, total = [], 0
            for c in cands:
                if total >= min(need, cap - int(body.onboard)):
                    break
                take = min(c["surplus"], cap - int(body.onboard) - total)
                if take <= 0:
                    break
                got.append({**c, "suggest_take": int(take)})
                total += take
            pickups = got[:3]

    drive = sum(s["drive_min"] for s in stops)
    handle = sum(s["handle_min"] for s in stops)
    return {
        "start": {"sid": body.start_sid, "name": start_name},
        "pickup_suggestions": pickups,
        "pickup_note": (None if feasible else
                        f"車上 {int(body.onboard)} 台，但這條路線要送出 "
                        f"{sum(s['qty_planned'] for s in stops if s['kind'] == 'supply')} 台。"
                        "下面是附近庫存過剩、可以先去取車的站——取走之後仍會留下該站的最低水位。"),
        "truck_capacity": cap, "onboard_start": int(body.onboard), "onboard_end": end_load,
        "stops": stops, "feasible": feasible, "reordered_for_load": repaired,
        "totals": {"stops": len(stops), "distance_km": round(total_m / 1000.0, 2),
                   "drive_min": round(drive, 1), "handle_min": round(handle, 1),
                   "total_min": round(drive + handle, 1),
                   "short_total": sum(s["short"] for s in stops)},
        "assumptions": {k: A[k] for k in ("truck_capacity", "truck_speed_kmh", "road_detour",
                                          "handling_fixed_min", "handling_per_bike_min")},
        "caveat": ("最近鄰貪婪排序，不是最佳解；距離是直線乘繞路係數，不是導航，"
                   "沒有真實路網、號誌與單行道。時間不含等紅燈與找車位。"
                   "系統不知道你的實際位置，起點是你自己選的。"),
    }


class ProgressIn(BaseModel):
    task_id: str
    action: str                 # enroute | arrived | done | failed | undo
    qty_done: int = None
    note: str = ""
    by: str = None


@router.post("/ops/progress")
def ops_progress(body: ProgressIn):
    """進度回報。系統不會自己認定完成，但會用官方站況驗證現場是否真的恢復。"""
    t = next((z for z in _tasks() if z["id"] == body.task_id), None)
    if t is None:
        raise HTTPException(404, "查無此任務")
    if body.action not in ("enroute", "arrived", "done", "failed", "undo"):
        raise HTTPException(400, "action 必須是 enroute / arrived / done / failed / undo")
    now = datetime.now(TZ)
    if body.action == "undo":
        t["status"] = "assigned"
        t.pop("done_at", None)
    elif body.action == "failed":
        t["status"] = "failed"
        t["failed_at"] = now.isoformat()
    else:
        t["status"] = {"enroute": "enroute", "arrived": "arrived", "done": "done"}[body.action]
        if body.action == "done":
            t["done_at"] = now.isoformat()
            t["qty_done"] = body.qty_done if body.qty_done is not None else t["qty"]
    t["version"] += 1
    t["progress"].append({"ts": now.isoformat(), "action": body.action,
                          "by": body.by or t.get("assignee") or "調度人員",
                          "qty_done": body.qty_done, "note": body.note})
    t["log"].append({"ts": now.isoformat(), "action": body.action,
                     "by": body.by or "調度人員", "note": body.note})
    v = _task_view(t)
    msg = None
    if v["mismatch"]:
        msg = ("回報完成，但官方即時站況顯示這一站還沒回到目標水位。"
               "可能是資料還沒更新（官方每 5 分鐘），也可能是實際沒補足——請再確認。")
    return {"ok": True, "task": v, "warning": msg,
            "caveat": "完成與否以現場為準；本系統只能用官方站況交叉驗證，不能代替你確認。"}


class FieldReportIn(BaseModel):
    sid: int
    symptom: str
    bike_no: str = None
    dock_no: str = None
    note: str = ""
    by: str = None
    request_id: str = None


@router.post("/ops/field-report")
def ops_field_report(body: FieldReportIn):
    """現場回報：帶車號或柱號就能精準去重，走既有的建單入口。"""
    submit = _CTX.get("submit_ticket")
    if submit is None:
        raise HTTPException(503, "建單服務未就緒")
    lv = _live()
    cur = lv.station(body.sid)
    if not cur:
        raise HTTPException(404, "查無此站的即時資料")
    payload = {
        "sid": body.sid, "station": cur["name"], "issue": body.symptom,
        "bike_no": body.bike_no, "dock_id": body.dock_no,
        "note": (body.note or "") + "（調度人員現場回報）",
        "source": "ops_field", "request_id": body.request_id,
        "reporter": body.by or "調度人員",
    }
    try:
        res = submit(payload)
    except Exception as e:                                    # noqa: BLE001
        raise HTTPException(500, f"建單失敗：{type(e).__name__}: {e}")
    return {"ok": True, "ticket": res,
            "dedup": "去重優先序：車號 > 柱號 > 站點＋問題類別（最後一種限 2 小時）",
            "caveat": ("建單不等於已確認根因，也不等於官方庫存已扣除或已遠端停租。")}


@router.get("/ops/handover")
def ops_handover(districts: str = ""):
    """班次交接：這一班留下什麼，接班的人一眼看完。"""
    ds = [d for d in (districts or "").split(",") if d.strip()]
    tasks = [_task_view(t) for t in _tasks() if not ds or t["district"] in ds]
    opent = [t for t in tasks if t["status"] not in ("done",)]
    done = [t for t in tasks if t["status"] == "done"]
    failed = [t for t in tasks if t["status"] == "failed"]
    mism = [t for t in done if t["mismatch"]]
    visited = sorted({t["name"] for t in tasks if t["progress"]})
    return {
        "districts": ds,
        "open": opent, "done_count": len(done), "failed": failed, "mismatch": mism,
        "visited_stations": visited,
        "summary": {
            "open": len(opent), "enroute": sum(1 for t in opent if t["status"] == "enroute"),
            "arrived": sum(1 for t in opent if t["status"] == "arrived"),
            "never_started": sum(1 for t in opent if not t["progress"]),
            "done": len(done), "failed": len(failed), "mismatch": len(mism),
            "longest_open_min": max([t["open_min"] for t in opent], default=0),
        },
        "handover_notes": [
            "未開始的任務要先確認還需不需要做——站況可能已經自己恢復了。",
            "「回報完成但站況未恢復」的要現場再確認，不要直接結案。",
            "來不及的缺口可以切成民眾分流，不必硬排車。",
        ],
    }


@router.get("/ops/tasks")
def ops_tasks(districts: str = ""):
    ds = [d for d in (districts or "").split(",") if d.strip()]
    ts = [_task_view(t) for t in _tasks() if not ds or t["district"] in ds]
    ts.sort(key=lambda t: (t["status"] == "done", -t.get("priority", 0)))
    return {"count": len(ts), "tasks": ts}


@router.post("/ops/tasks/reset")
def ops_tasks_reset():
    n = len(_tasks())
    _CTX["state"]["v2_tasks"] = []
    return {"ok": True, "cleared": n}
