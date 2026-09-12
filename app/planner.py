"""
planner.py — (1) 調度端：120/180 分鐘前的人車安排（可行建議，非真實最優解）
             (2) 民眾端：最快 / 較穩 / 順路集點 三方案
所有假設集中在 ASSUMPTIONS，前端會原文顯示；未取得車隊、班表、集合點資料前皆為明示情境參數。
"""
import math, time, threading
from concurrent.futures import ThreadPoolExecutor
_legpool = ThreadPoolExecutor(max_workers=32)
import numpy as np, pandas as pd, json, ssl, subprocess, urllib.request, os, sys
sys.path.insert(0, os.path.dirname(__file__))
import awsloc as AWSLOC

_SSL = ssl.create_default_context(); _SSL.check_hostname = False; _SSL.verify_mode = ssl.CERT_NONE
def http_get_json(url, timeout=6):
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 ntpc-youbike-demo"})
        with urllib.request.urlopen(req, timeout=timeout, context=_SSL) as r: return json.loads(r.read().decode("utf-8"))
    except Exception:
        try:
            out = subprocess.run(["curl", "-s", "-m", str(timeout), "-A", "Mozilla/5.0", url], capture_output=True, timeout=timeout + 2).stdout
            return json.loads(out.decode("utf-8")) if out else None
        except Exception:
            return None

ASSUMPTIONS = {
    "truck_capacity": 20,            # 每趟載量(輛)，未取得車隊資料的情境值
    "truck_speed_kmh": 20,           # 市區調度車平均速度
    "road_detour": 1.4,              # 直線→道路距離係數(派車估算用)
    "handling_fixed_min": 4,         # 每站固定作業時間
    "handling_per_bike_min": 0.4,    # 每輛搬運時間
    "lead_time_min": 60,             # 訪談值：決策到人車抵達的總量參考（保留給三端顯示，不再直接拿來加路程）
    # 把上面那 60 分鐘拆段：只有「準備」是路線估不到的固定前置，行車與裝卸已含在站點作業與路段時間裡，
    # 舊寫法 now + lead_time_min + 路程 會把行車重複算一次。
    "lead_prepare_min": 15,          # 新動員：接到指令→人員到位、車輛出場（情境值，非實測，需派工紀錄校準）
    "divert_prepare_min": 3,         # 在勤改道：車已在路上，只要切換目的地（情境值，非實測）
    "lead_load_note": "取車裝車已計入取車站的站點作業時間",
    "lead_travel_note": "行車時間由路段距離與車速估算",
    "lead_unload_note": "卸車已計入送車站的站點作業時間",
    "depot": "行政區內站點重心（未提供集合點）",
    "walk_kmh": 4.5, "ride_kmh": 12, "walk_detour": 1.3,
    "min_stock_ratio": 0.15, "min_stock_abs": 2,   # 目標最低庫存(車/位)
    "risk_threshold": 0.35,          # 預測零車/零位機率門檻
    "donor_keep_ratio": 0.5,         # 供給站至少保留的比例(保留稍後需求)
    "intent_conversion": 0.7,        # 已登記意向轉為實際借還的假設比例
    "depot_supply": True,            # 區內供給不足時，允許由調度中心/跨區補給整車（情境；未取得倉儲庫存資料）
    "reroute_penalty_min": 10,       # 到站無車/無位時改站的估計額外時間
    # 綠色足跡估算係數（公開常見估計值，正式數字須引用環境部公告）
    "co2_scooter_g_per_km": 95,      # 對照：騎機車
    "co2_car_g_per_km": 190,         # 對照：自用小客車
    "taxi_base_fare": 85, "taxi_base_km": 1.25, "taxi_per_km": 25,   # 對照：計程車（假設費率）
    "youbike_free_min": 30,          # 一般車前 30 分鐘（新北現行優惠，以官方公告為準）
}
R = 6371000.0

def hav(lat1, lon1, lat2, lon2):
    lat1, lon1, lat2, lon2 = map(np.radians, [lat1, lon1, lat2, lon2])
    a = np.sin((lat2 - lat1) / 2) ** 2 + np.cos(lat1) * np.cos(lat2) * np.sin((lon2 - lon1) / 2) ** 2
    return 2 * R * np.arcsin(np.sqrt(a))

# ---------- 路網（OSRM 公開伺服器；只取距離與幾何，時間自行換算；失敗退回直線×係數） ----------
_route_cache = {}; _rc_lock = threading.Lock()
MANEUVER = {"turn": "轉", "new name": "續行", "depart": "出發", "arrive": "抵達", "merge": "併入", "fork": "分岔",
            "end of road": "路口", "continue": "直行", "roundabout": "圓環", "rotary": "圓環", "notification": "注意"}
MODIFIER = {"left": "左", "right": "右", "slight left": "靠左", "slight right": "靠右", "sharp left": "大左",
            "sharp right": "大右", "straight": "直行", "uturn": "迴轉"}

def osrm_steps(a, b):
    """逐段指示：用 OSRM 真實路線的轉彎點與路名，翻成中文。失敗回傳空陣列。"""
    url = (f"https://router.project-osrm.org/route/v1/driving/{a[1]:.6f},{a[0]:.6f};{b[1]:.6f},{b[0]:.6f}"
           "?overview=full&geometries=geojson&steps=true")
    js = http_get_json(url, timeout=6)
    if not js or js.get("code") != "Ok": return {"steps": [], "geometry": [list(a), list(b)], "dist_m": None}
    rt = js["routes"][0]; out = []
    for st in rt["legs"][0]["steps"]:
        m = st.get("maneuver", {}); typ = m.get("type", ""); mod = m.get("modifier", "")
        road = st.get("name") or ""
        if typ == "depart": text = f"沿{road}出發" if road else "出發"
        elif typ == "arrive": text = "抵達目的地"
        elif typ in ("turn", "end of road", "fork", "merge"):
            text = f"{MODIFIER.get(mod, '')}轉進{road}" if mod in ("left", "right") and road else f"{MODIFIER.get(mod, mod)}{'' if mod=='straight' else '轉'}"
            if road and mod not in ("left", "right"): text += f"（{road}）"
        elif typ in ("new name", "continue"): text = f"沿{road}直行" if road else "直行"
        elif typ in ("roundabout", "rotary"): text = f"進入圓環後{MODIFIER.get(mod, '')}出"
        else: text = MANEUVER.get(typ, "直行") + (f"（{road}）" if road else "")
        out.append({"text": text, "dist_m": round(st.get("distance", 0)), "road": road,
                    "at": [m.get("location", [0, 0])[1], m.get("location", [0, 0])[0]]})
    return {"steps": out, "geometry": [[c[1], c[0]] for c in rt["geometry"]["coordinates"]], "dist_m": round(rt["distance"])}

def osrm(coords):
    key = tuple(round(c, 5) for p in coords for c in p)
    with _rc_lock:
        if key in _route_cache: return _route_cache[key]
    url = "https://router.project-osrm.org/route/v1/driving/" + ";".join(f"{lon:.6f},{lat:.6f}" for lat, lon in coords) + "?overview=full&geometries=geojson"
    out = None
    js = http_get_json(url, timeout=3.5)
    if js and js.get("code") == "Ok":
        rt = js["routes"][0]
        out = {"dist_m": float(rt["distance"]), "geometry": [[c[1], c[0]] for c in rt["geometry"]["coordinates"]]}
    with _rc_lock: _route_cache[key] = out
    return out

def leg(a, b, mode, use_osrm=True):
    """a,b=(lat,lon)。優先用 Amazon Location Service 真實步行／騎乘路網；失敗才退回估算。"""
    straight = float(hav(a[0], a[1], b[0], b[1]))
    dist = geom = src = None
    if use_osrm and straight > 40:
        r = AWSLOC.route(a, b, "walk" if mode == "walk" else "ride")
        if r and r["dist_m"] <= straight * 3.0 + 400:
            dist, geom, src = r["dist_m"], r["geometry"], r["source"]
    if dist is None:
        dist, geom, src = straight * ASSUMPTIONS["walk_detour"], [list(a), list(b)], "直線×1.3估計（AWS 路網未取得或結果不適用）"
    kmh = ASSUMPTIONS["walk_kmh"] if mode == "walk" else ASSUMPTIONS["ride_kmh"]
    return {"mode": mode, "dist_m": round(dist), "minutes": round(dist / 1000 / kmh * 60, 1), "geometry": geom, "source": src}

# ---------- 風險查詢：把「幾分鐘後」對應到最近的預測尺度 ----------
def horizon_for(minutes_ahead):
    if minutes_ahead <= 45: return 30
    if minutes_ahead <= 90: return 60
    if minutes_ahead <= 150: return 120
    return 180

def risk(row, minutes_ahead, kind):
    """kind='empty'|'full'。<=5 分鐘用現況。"""
    if minutes_ahead <= 5:
        v = row["bikes"] if kind == "empty" else row["spaces"]
        return 0.95 if (v is not None and not np.isnan(v) and v <= 0) else (0.25 if v <= 2 else 0.03)
    h = horizon_for(minutes_ahead)
    return float(row[f"pe_{h}" if kind == "empty" else f"pf_{h}"])

def expected_stock(row, minutes_ahead, kind):
    if minutes_ahead <= 5: return float(row["bikes"] if kind == "bikes" else row["spaces"])
    h = horizon_for(minutes_ahead); return float(row[f"pb_{h}" if kind == "bikes" else f"ps_{h}"])

# ---------- 意向修正（不重複計算：歷史模型已含一般需求，只加入本服務登記的淨意向×轉換率） ----------
def apply_intents(pred, intents, now_ts):
    pred = pred.copy(); conv = ASSUMPTIONS["intent_conversion"]
    for it in intents:
        if it["status"] not in ("active",): continue
        for h in (30, 60, 120, 180):
            tgt = now_ts + pd.Timedelta(minutes=h)
            if pd.Timestamp(it["borrow_ts"]) <= tgt:
                i = pred.index[pred.sid == it["borrow_sid"]]
                pred.loc[i, f"pb_{h}"] = np.maximum(0, pred.loc[i, f"pb_{h}"] - conv); pred.loc[i, f"ps_{h}"] = np.minimum(pred.loc[i, "cap"], pred.loc[i, f"ps_{h}"] + conv)
            if pd.Timestamp(it["return_ts"]) <= tgt:
                i = pred.index[pred.sid == it["return_sid"]]
                pred.loc[i, f"pb_{h}"] = np.minimum(pred.loc[i, "cap"], pred.loc[i, f"pb_{h}"] + conv); pred.loc[i, f"ps_{h}"] = np.maximum(0, pred.loc[i, f"ps_{h}"] - conv)
    return pred

# ---------- 調度端：顯式規劃週期（planning cycle）與資源帳 ----------
# 為什麼要有週期：伺服器同一個 tick 會先排 120 再排 180。沒有共用帳本時，兩次規劃會對同一站
# 重複承諾送車，也會把同一個供給站的車重複抽兩次。
# 為什麼要分狀態：重算時只能釋放「還沒確認」的候選，已確認或已在途的承諾不可以被重算吃掉。
#
# 一筆預約 = {id, cycle, kind(supply/demand), sid, qty, state, horizon, task}
#   candidate  規劃產生、調度員還沒確認
#   confirmed  調度員確認派工
#   in_transit 已出車
#   released   取消或重算釋放（不再占用資源）
# 可用量 = 原始可用量 − 該站所有 candidate/confirmed/in_transit 的 qty。released 不計。

_CYCLES = {}          # cycle_id -> cycle dict
_CYCLE_BY_TS = {}     # ts -> cycle_id（同一個回放時刻沿用同一個週期）
_RES_SEQ = [0]
ACTIVE_RES = ("candidate", "confirmed", "in_transit")


def ledger_reset():
    """整個資源帳歸零。只有 /api/reset 與情境切換該呼叫。"""
    _CYCLES.clear(); _CYCLE_BY_TS.clear(); _RES_SEQ[0] = 0


def cycle_open(now_ts):
    """取得（或開啟）這個回放時刻的規劃週期。"""
    ts = str(now_ts)
    cid = _CYCLE_BY_TS.get(ts)
    if cid and cid in _CYCLES: return cid
    _RES_SEQ[0] += 1
    cid = f"CY-{ts[:16].replace(' ', 'T')}-{_RES_SEQ[0]}"
    _CYCLES[cid] = {"id": cid, "ts": ts, "horizons": [], "res": {}}
    _CYCLE_BY_TS[ts] = cid
    return cid


def _cycle(cid): return _CYCLES.get(cid) or {}


def cycle_release_candidates(cid, horizon=None):
    """重算前釋放候選。已確認與在途的不動——那才是「重算不超配、取消才釋放」。"""
    c = _cycle(cid); freed = 0
    for r in c.get("res", {}).values():
        if r["state"] != "candidate": continue
        if horizon is not None and r["horizon"] != horizon: continue
        r["state"] = "released"; freed += 1
    return freed


def _reserve(cid, kind, sid, qty, horizon):
    _RES_SEQ[0] += 1
    rid = f"RES{_RES_SEQ[0]:05d}"
    _cycle(cid).setdefault("res", {})[rid] = {"id": rid, "cycle": cid, "kind": kind, "sid": int(sid),
                                              "qty": float(qty), "state": "candidate", "horizon": horizon, "task": None}
    return rid


def _committed(cid, kind, sid):
    return sum(r["qty"] for r in _cycle(cid).get("res", {}).values()
               if r["kind"] == kind and r["sid"] == int(sid) and r["state"] in ACTIVE_RES)


def cycle_bind_task(cid, res_ids, task_id):
    for rid in res_ids or []:
        r = _cycle(cid).get("res", {}).get(rid)
        if r: r["task"] = task_id


def cycle_set_state(cid, task_id, state):
    """調度員確認／出車／取消時改變該任務所有預約的狀態。回傳異動筆數。"""
    if state not in ACTIVE_RES + ("released",): return 0
    c = _cycle(cid); k = 0
    for r in c.get("res", {}).values():
        if r.get("task") != task_id: continue
        if r["state"] == "released" and state != "released": continue
        r["state"] = state; k += 1
    return k


def cycle_set_res_state(cid, res_ids, state):
    """直接用預約 id 改狀態。任務 id 是伺服器排完才給的，前端只拿得到 reservations。"""
    if state not in ACTIVE_RES + ("released",): return 0
    c = _cycle(cid); k = 0
    for rid in res_ids or []:
        r = c.get("res", {}).get(rid)
        if not r: continue
        if r["state"] == "released" and state != "released": continue
        r["state"] = state; k += 1
    return k


def cycle_snapshot(cid):
    """唯讀：週期內每個站的承諾量。GET 用這個，不會改任何狀態。"""
    c = _cycle(cid)
    by = {}
    for r in c.get("res", {}).values():
        d = by.setdefault((r["kind"], r["sid"]), {"kind": r["kind"], "sid": r["sid"], "candidate": 0.0,
                                                  "confirmed": 0.0, "in_transit": 0.0, "released": 0.0})
        d[r["state"]] = d.get(r["state"], 0.0) + r["qty"]
    return {"cycle": c.get("id"), "ts": c.get("ts"), "horizons": list(c.get("horizons", [])),
            "reservations": len(c.get("res", {})),
            "by_station": sorted(by.values(), key=lambda x: (x["kind"], x["sid"]))}


# ---------- 調度端：缺口計算 ----------
DISPATCHABLE = {"normal", "empty", "full"}
def gaps(pred, horizon, scenario_delta=None):
    """回傳每站 need_in(要運入) / need_out(要運出) / donor(可供給)；排除待查狀態。"""
    A = ASSUMPTIONS; d = pred.copy()
    d["min_stock"] = np.maximum(A["min_stock_abs"], np.ceil(d["cap"] * A["min_stock_ratio"]))
    pb = d[f"pb_{horizon}"].astype(float).values.copy(); ps = d[f"ps_{horizon}"].astype(float).values.copy()
    d["event_delta"] = 0.0
    if scenario_delta:
        for sid, delta in scenario_delta.items():
            i = d.index[d.sid == sid]
            if len(i): pb[i] = pb[i] + delta; ps[i] = ps[i] - delta; d.loc[i, "event_delta"] = delta   # 不截斷，保留需求量
    ok = d["status"].isin(DISPATCHABLE).values
    pe = d[f"pe_{horizon}"].values; pf = d[f"pf_{horizon}"].values
    if scenario_delta:
        for sid, delta in scenario_delta.items():
            i = d.index[d.sid == sid]
            if len(i) and delta < 0: pe[i] = max(pe[i][0], 0.6)
            if len(i) and delta > 0: pf[i] = max(pf[i][0], 0.6)
    d["need_in"] = np.where(ok & (pe >= A["risk_threshold"]), np.maximum(0, d["min_stock"] - pb), 0)
    d["need_out"] = np.where(ok & (pf >= A["risk_threshold"]), np.maximum(0, d["min_stock"] - ps), 0)
    keep = np.maximum(d["min_stock"], d["cap"] * A["donor_keep_ratio"])
    d["donor"] = np.where(ok & (d["need_in"] == 0) & (d["need_out"] == 0), np.maximum(0, pb - keep), 0)
    d["pb_adj"] = np.clip(pb, -999, d["cap"]); d["ps_adj"] = np.clip(ps, -999, d["cap"]); d["pe_h"] = pe; d["pf_h"] = pf
    # 每站自己的服務時限：最早達到零車機率門檻的尺度，不是整趟共用一個目標時間。
    # 一個 30 分鐘後就會空的站，跟一個 180 分鐘後才空的站，不該用同一個期限判定來不來得及。
    # 判準與缺口定義一致：同時滿足「零車機率過門檻」與「預測庫存低於目標下限」才算該尺度已到期，
    # 只用機率門檻會過度悲觀（35% 機率不等於一定空）。
    due = np.full(len(d), float(horizon))
    for h in (180, 120, 60, 30):
        pec, pbc = f"pe_{h}", f"pb_{h}"
        if pec in d.columns and pbc in d.columns:
            hit = (d[pec].astype(float).values >= A["risk_threshold"]) & (d[pbc].astype(float).values < d["min_stock"].values)
            due = np.where(hit, float(h), due)
    if scenario_delta:                       # 活動情境把該尺度的風險拉高，期限就是該尺度
        for sid in scenario_delta:
            i = d.index[d.sid == sid]
            if len(i): due[i] = np.minimum(due[i], float(horizon))
    d["due_min"] = due
    return d

def plan_dispatch(pred, now_ts, horizon, scenario_delta=None, districts=None, existing_locked=None, cycle=None):
    """每行政區用貪婪最近鄰組趟：先取運出/供給站，再送缺車站。回傳任務列表。"""
    A = ASSUMPTIONS; g = gaps(pred, horizon, scenario_delta)
    target_ts = now_ts + pd.Timedelta(minutes=horizon)
    tasks = []
    locked_sids = existing_locked or set()
    cid = cycle or cycle_open(now_ts)
    cyc = _cycle(cid)
    if horizon in cyc.get("horizons", []):   # 同一個尺度再算一次＝重算，先釋放自己的候選再重排
        cycle_release_candidates(cid, horizon)
    else:
        cyc.setdefault("horizons", []).append(horizon)
    promised = {sid: _committed(cid, "demand", sid) for sid in g["sid"].tolist()}
    g["need_in"] = np.maximum(0.0, g["need_in"] - g["sid"].map(promised).fillna(0.0))
    for dist_name, grp in g.groupby("district"):
        if districts and dist_name not in districts: continue
        deficits = grp[(grp.need_in > 0) & ~grp.sid.isin(locked_sids)].copy()
        if deficits.empty: continue
        district_deficit = float(deficits.need_in.sum()); covered = 0.0; event_sids = set(grp[grp.event_delta != 0].sid.tolist())
        sources = grp[(grp.need_out > 0) | (grp.donor > 0)].copy()
        sources["avail"] = np.where(sources.need_out > 0, np.maximum(sources.need_out, np.minimum(sources.pb_adj - sources.min_stock, 8)), sources.donor)
        sources["avail"] = np.maximum(0.0, sources["avail"] - sources["sid"].map(
            {sid: _committed(cid, "supply", sid) for sid in sources["sid"].tolist()}).fillna(0.0))
        sources = sources[sources.avail > 0]
        depot = (float(grp.lat.mean()), float(grp.lon.mean()))
        trip_no = 0
        while not deficits.empty and trip_no < 3:
            trip_no += 1
            cap_left = A["truck_capacity"]; stops = []; pos = depot; load = 0; res_ids = []
            total_def = float(deficits.need_in.sum())
            # 取車
            src = sources.copy()
            while cap_left > 0 and load < min(total_def, A["truck_capacity"]) and not src.empty:
                dd = hav(pos[0], pos[1], src.lat.values, src.lon.values); i = int(np.argmin(dd)); r = src.iloc[i]
                q = int(min(cap_left, r.avail, max(1, total_def - load)))
                if q <= 0: break
                stops.append({"sid": int(r.sid), "name": r["name"], "lat": float(r.lat), "lon": float(r.lon), "action": "pickup", "qty": q,
                              "now_bikes": None if np.isnan(r.bikes) else int(r.bikes), "pred_bikes": round(float(r.pb_adj), 1), "cap": int(r.cap),
                              "reason": "預測將滿站，需運出" if r.need_out > 0 else "庫存充裕可供給，保留下限 %d 輛" % int(max(r.min_stock, r.cap * A["donor_keep_ratio"]))})
                cap_left -= q; load += q; pos = (float(r.lat), float(r.lon))
                sources.loc[sources.sid == r.sid, "avail"] -= q; src = src.drop(src.index[i])
                res_ids.append(_reserve(cid, "supply", int(r.sid), q, horizon))
            if A["depot_supply"] and load < min(total_def, A["truck_capacity"]) and cap_left > 0:
                q = int(min(cap_left, math.ceil(total_def - load)))
                if q > 0:
                    stops.insert(0, {"sid": -1, "name": "調度中心／跨區補給（情境）", "lat": depot[0], "lon": depot[1], "action": "pickup", "qty": q, "now_bikes": None, "pred_bikes": None, "cap": None,
                                     "reason": "區內可供給站不足，由調度中心或跨區整車補給；倉儲庫存未提供，為情境假設"})
                    cap_left -= q; load += q; res_ids.append(_reserve(cid, "supply", -1, q, horizon))
            if 0 < load < 3:
                tasks.append({"district": dist_name, "horizon": horizon, "status": "minor_gap", "stops": [], "deficit_total": int(total_def),
                              "reason": f"缺口僅 {int(total_def)} 輛，不單獨成趟；先以鄰站引導與民眾分流，併入下一趟", "target_ts": str(target_ts), "trip": trip_no, "load": 0, "route_minutes": 0, "route_km": 0, "depot": depot, "depart_by": str(target_ts), "earliest_arrival": str(target_ts)})
                break
            if load == 0:
                # 區內無來源：標示需跨區補車
                tasks.append({"district": dist_name, "horizon": horizon, "status": "needs_cross_district", "stops": [], "trip": trip_no, "load": 0, "route_minutes": 0, "route_km": 0, "depot": depot,
                              "deficit_total": int(total_def), "reason": "區內無可供給站，需跨區調車或由鄰近站分流", "target_ts": str(target_ts), "depart_by": str(target_ts), "earliest_arrival": str(target_ts)})
                break
            # 送車
            while load > 0 and not deficits.empty:
                dd = hav(pos[0], pos[1], deficits.lat.values, deficits.lon.values); i = int(np.argmin(dd)); r = deficits.iloc[i]
                q = int(min(load, max(1, math.ceil(r.need_in))))
                due_min = int(r.due_min) if not np.isnan(r.due_min) else horizon
                stops.append({"sid": int(r.sid), "name": r["name"], "lat": float(r.lat), "lon": float(r.lon), "action": "dropoff", "qty": q,
                              "now_bikes": None if np.isnan(r.bikes) else int(r.bikes), "pred_bikes": round(float(r.pb_adj), 1), "cap": int(r.cap),
                              "p_empty": round(float(r.pe_h), 2), "due_min": due_min, "due_ts": str(now_ts + pd.Timedelta(minutes=due_min)),
                              "reason": f"預測 {horizon} 分鐘後零車機率 {r.pe_h:.0%}，低於目標庫存 {int(r.min_stock)} 輛；本站自己的服務時限是 {due_min} 分鐘後"})
                load -= q; covered += q; pos = (float(r.lat), float(r.lon))
                res_ids.append(_reserve(cid, "demand", int(r.sid), q, horizon))
                remain = float(r.need_in) - q
                if remain >= 1:      # 只補到一部分時保留殘量，不可把整站從缺口清單移除
                    deficits.iloc[i, deficits.columns.get_loc("need_in")] = remain
                else:
                    deficits = deficits.drop(deficits.index[i])
            # 時間估算
            t = 0.0; prev = depot; legs = []
            for s_ in stops:
                dm = float(hav(prev[0], prev[1], s_["lat"], s_["lon"])) * A["road_detour"]
                tm = dm / 1000 / A["truck_speed_kmh"] * 60 + A["handling_fixed_min"] + A["handling_per_bike_min"] * s_["qty"]
                t += tm; s_["eta_min_from_depart"] = round(t); legs.append(round(dm)); prev = (s_["lat"], s_["lon"])
            drops = [s_ for s_ in stops if s_["action"] == "dropoff"]
            # 決策到抵達只加「人員準備與車輛出場」；行車與裝卸已經算在站點作業與路段時間裡，
            # 舊寫法 now + 60 + 路程 會把行車重複算一次（見 ASSUMPTIONS 的 lead_* 拆段）。
            ready = now_ts + pd.Timedelta(minutes=A["lead_prepare_min"])
            def _due(s_): return pd.Timestamp(s_["due_ts"]) if s_.get("due_ts") else target_ts
            for s_ in drops:
                arr = ready + pd.Timedelta(minutes=s_["eta_min_from_depart"])
                s_["arrives_by"] = str(arr); s_["on_time"] = bool(arr <= _due(s_))
                s_["late_min"] = max(0, int(round((arr - _due(s_)).total_seconds() / 60)))
            late = [s_["name"] for s_ in drops if not s_["on_time"]]
            ok_drops = [s_ for s_ in drops if s_["on_time"]]
            first_drop = drops[0]["eta_min_from_depart"] if drops else t
            last_drop = drops[-1]["eta_min_from_depart"] if drops else t
            # 最遲出發：對每個「來得及」的送站各自往回推，取最緊的那一個
            depart_by = (min(_due(s_) - pd.Timedelta(minutes=s_["eta_min_from_depart"]) for s_ in ok_drops)
                         if ok_drops else target_ts)
            earliest_arrival = ready + pd.Timedelta(minutes=first_drop)
            feasible = len(ok_drops) > 0
            tight = min((s_["due_min"] for s_ in drops if s_.get("due_min") is not None), default=horizon)
            tasks.append({"district": dist_name, "horizon": horizon, "trip": trip_no, "stops": stops, "route_minutes": round(t), "route_km": round(sum(legs) / 1000, 1),
                          "load": int(sum(s_["qty"] for s_ in stops if s_["action"] == "pickup")),
                          "target_ts": str(target_ts), "depart_by": str(min(depart_by, target_ts)), "earliest_arrival": str(earliest_arrival),
                          "status": "planned" if feasible else "too_late",
                          "late_stops": late, "last_drop_min": last_drop, "on_time_stops": len(ok_drops), "tightest_due_min": int(tight),
                          "ready_ts": str(ready),
                          "reason": (("提前 %d 分鐘安排，%d 個送車站都能在各自的服務時限前抵達（最緊的是 %d 分鐘後）" % (horizon, len(drops), tight)) if not late
                                     else ("提前 %d 分鐘安排：%d 站可如期，%s 等 %d 站趕不上自己的服務時限（最緊 %d 分鐘），那幾站改以人力就近補或民眾分流"
                                           % (horizon, len(ok_drops), "、".join(late[:2]), len(late), tight))) if feasible
                                    else ("所有送車站都趕不上自己的服務時限（最緊 %d 分鐘）：人員準備 %d 分＋行車作業 %d 分才到得了第一站，派車來不及；改以在勤資源、人力就近補與民眾分流因應"
                                          % (tight, A["lead_prepare_min"], first_drop)),
                          "depot": depot, "event_related": any(s_["sid"] in event_sids for s_ in stops),
                          "cycle": cid, "reservations": res_ids, "reservation_state": "candidate"})
        remaining = max(0.0, district_deficit - covered)
        if remaining > 0 or covered > 0:
            # 分流建議：缺車站 500m 內預測仍有車的鄰站
            alt = grp[(grp.need_in == 0) & (grp.need_out == 0) & (grp.pb_adj >= 5)].nlargest(5, "pb_adj")
            tasks.append({"district": dist_name, "horizon": horizon, "status": "gap_summary", "stops": [], "trip": 0, "load": int(covered), "route_minutes": 0, "route_km": 0, "depot": depot,
                          "deficit_total": int(round(district_deficit)), "covered": int(covered), "remaining": int(round(remaining)),
                          "reason": (f"缺口 {int(round(district_deficit))} 輛：派車可補 {int(covered)} 輛，剩餘 {int(round(remaining))} 輛需靠民眾分流／在勤資源" if remaining > 0 else f"缺口 {int(round(district_deficit))} 輛已全數排入派車"),
                          "alternatives": [{"sid": int(r["sid"]), "name": r["name"], "pred_bikes": round(float(r["pb_adj"]), 1)} for _, r in alt.iterrows()],
                          "target_ts": str(target_ts), "depart_by": str(target_ts), "earliest_arrival": str(target_ts), "event_related": bool(event_sids)})
    return tasks

# ---------- 民眾端：三方案 ----------
def plan_trip(pred, origin, dest, depart_ts, now_ts, max_walk_min=12, want_reward=True, nb_sids=None, weather=None, preference="time"):
    A = ASSUMPTIONS
    wf = weather.get("_factor") if weather else None
    t_mult = (wf or {}).get("time", 1.0); r_mult = (wf or {}).get("risk", 1.0)
    d = pred[pred.status.isin(DISPATCHABLE)].copy()
    d["d_o"] = hav(origin[0], origin[1], d.lat.values, d.lon.values); d["d_d"] = hav(dest[0], dest[1], d.lat.values, d.lon.values)
    max_walk_m = max_walk_min * A["walk_kmh"] * 1000 / 60 / A["walk_detour"]
    borrow = d[d.d_o <= max_walk_m].nsmallest(6, "d_o"); ret = d[d.d_d <= max_walk_m].nsmallest(6, "d_d")
    if borrow.empty or ret.empty: return {"options": [], "error": "步行範圍內沒有站點，請放寬步行時間"}
    lead0 = max(0.0, (pd.Timestamp(depart_ts) - now_ts).total_seconds() / 60)
    extended = []
    if all(risk(a, lead0 + 5, "empty") > 0.4 for _, a in borrow.iterrows()):
        far = d[(d.d_o > max_walk_m) & (d.d_o <= max_walk_m * 1.6)].nsmallest(5, "d_o")
        far = far[[risk(a, lead0 + 8, "empty") <= 0.3 for _, a in far.iterrows()]]
        if len(far): borrow = pd.concat([borrow, far]); extended.append("borrow")
    if all(risk(b, lead0 + 15, "full") > 0.4 for _, b in ret.iterrows()):
        far = d[(d.d_d > max_walk_m) & (d.d_d <= max_walk_m * 1.6)].nsmallest(5, "d_d")
        far = far[[risk(b, lead0 + 15, "full") <= 0.3 for _, b in far.iterrows()]]
        if len(far): ret = pd.concat([ret, far]); extended.append("return")
    od = float(hav(origin[0], origin[1], dest[0], dest[1]))
    if od < 500: return {"options": [], "error": f"起點到目的地直線約 {int(od)} 公尺，建議直接步行"}
    lead = max(0.0, (pd.Timestamp(depart_ts) - now_ts).total_seconds() / 60)
    hot_full = float(np.nanmax([risk(b, lead + 8, "full") for _, b in ret.iterrows()] + [0.0]))
    hot_empty = float(np.nanmax([risk(a, lead + 3, "empty") for _, a in borrow.iterrows()] + [0.0]))
    # ---- 候選網格：借車站 × 還車站，全部取真實路網後再排序 ----
    borrow = borrow.nsmallest(5, "d_o"); ret = ret.nsmallest(5, "d_d")
    cands = []
    for _, a in borrow.iterrows():
        for _, b in ret.iterrows():
            if a.sid == b.sid: continue
            if a.d_d <= b.d_d + 150 or hav(a.lat, a.lon, b.lat, b.lon) < 400: continue
            benefit = 0.0
            benefit += max(0.0, float(a["pf_120"]) - 0.2) * 1.0 + max(0.0, (float(a["bikes"]) if not np.isnan(a["bikes"]) else 0) / max(a["cap"], 1) - 0.7) * 0.5
            benefit += max(0.0, float(b["pe_120"]) - 0.2) * 1.5 + max(0.0, 0.3 - (expected_stock(b, 120, "bikes") / max(b["cap"], 1))) * 0.5
            cands.append({"borrow": a, "return": b, "benefit": benefit})
    if not cands: return {"options": [], "error": "沒有可行的借還組合"}

    # ---- 路段取得：步行段全取；騎乘段先算直線下界，只查有機會勝出的組合 ----
    # 直線距離必定不大於實際路網距離，因此直線時間是真實時間的下界，可安全剪枝。
    walk_o = {int(r.sid): (float(r.lat), float(r.lon)) for _, r in borrow.iterrows()}
    walk_d = {int(r.sid): (float(r.lat), float(r.lon)) for _, r in ret.iterrows()}
    jobs = {}
    for sid, pt in walk_o.items(): jobs[("wo", sid)] = _legpool.submit(leg, origin, pt, "walk")
    for sid, pt in walk_d.items(): jobs[("wd", sid)] = _legpool.submit(leg, pt, dest, "walk")
    R = {k: f.result() for k, f in jobs.items()}

    def lower_bound(c):
        bs, rs = int(c["borrow"].sid), int(c["return"].sid)
        ride_lb = float(hav(c["borrow"].lat, c["borrow"].lon, c["return"].lat, c["return"].lon)) / 1000 / A["ride_kmh"] * 60 * t_mult
        return R[("wo", bs)]["minutes"] + ride_lb + R[("wd", rs)]["minutes"]
    for c in cands: c["lb"] = lower_bound(c)
    cands.sort(key=lambda c: c["lb"])

    def fetch_rides(subset):
        fs = {(int(c["borrow"].sid), int(c["return"].sid)):
              _legpool.submit(leg, (float(c["borrow"].lat), float(c["borrow"].lon)), (float(c["return"].lat), float(c["return"].lon)), "ride")
              for c in subset if ("rd", int(c["borrow"].sid), int(c["return"].sid)) not in R}
        for k, f in fs.items(): R[("rd",) + k] = f.result()

    def build(c):
        bs, rs = int(c["borrow"].sid), int(c["return"].sid)
        w1 = R[("wo", bs)]; w2 = R[("wd", rs)]; rd = dict(R[("rd", bs, rs)])
        rd["minutes"] = round(rd["minutes"] * t_mult, 1)
        total = round(w1["minutes"] + rd["minutes"] + w2["minutes"], 1)
        t_b = lead + w1["minutes"]; t_r = t_b + rd["minutes"]
        pe = risk(c["borrow"], t_b, "empty"); pf = risk(c["return"], t_r, "full")
        eb = expected_stock(c["borrow"], t_b, "bikes"); es = expected_stock(c["return"], t_r, "spaces")
        penalty = (pe + pf) * A["reroute_penalty_min"] * r_mult
        bn = c["benefit"]
        if hot_full >= 0.35 and pf <= 0.2: bn += (hot_full - pf) * 0.8
        if hot_empty >= 0.35 and pe <= 0.2: bn += (hot_empty - pe) * 0.8
        return {"c": c, "legs": [w1, rd, w2], "total_min": total, "pe": pe, "pf": pf, "eb": eb, "es": es,
                "penalty": penalty, "benefit": bn, "t_borrow_min": t_b, "t_return_min": t_r}

    first = cands[:8]
    fetch_rides(first)
    built = [build(c) for c in first]
    best_so_far = min(b["total_min"] for b in built) if built else 1e9
    # 集點方案可能比最快多 10 分鐘仍值得，所以剪枝門檻放寬到 best + 10
    rest = [c for c in cands[8:] if c["lb"] <= best_so_far + 10]
    if rest:
        fetch_rides(rest)
        built += [build(c) for c in rest]
    pruned = len(cands) - len(built)
    if not built: return {"options": [], "error": "沒有可行的借還組合"}

    # ---- 以真實路網結果排序，三個標準各取一個 ----
    best_time = min(b["total_min"] for b in built)
    fastest  = min(built, key=lambda b: b["total_min"])
    reliable = min(built, key=lambda b: b["total_min"] + 2.5 * b["penalty"] + (15 if (b["pe"] > 0.3 or b["pf"] > 0.3) else 0))
    reward = None
    if want_reward:
        pool = [b for b in built if b["pe"] <= 0.4 and b["pf"] <= 0.4 and b["benefit"] >= 0.1 and b["total_min"] <= best_time + 10]
        if pool: reward = max(pool, key=lambda b: b["benefit"] - 0.02 * (b["total_min"] - best_time))

    def pack(b, kind):
        c = b["c"]; pts = int(min(30, round(b["benefit"] * 20))) if kind == "reward" else 0
        return {"kind": kind, "label": {"fast": "最快抵達", "reliable": "不用怕沒車沒位", "reward": "順路集點"}.get(kind, "其他選擇"),
                "borrow": {"sid": int(c["borrow"].sid), "name": c["borrow"]["name"], "lat": float(c["borrow"].lat), "lon": float(c["borrow"].lon),
                           "bikes_now": None if np.isnan(c["borrow"].bikes) else int(c["borrow"].bikes), "cap": int(c["borrow"].cap),
                           "expected_bikes": round(b["eb"], 1), "p_empty": round(b["pe"], 2), "arrive_in_min": round(b["t_borrow_min"])},
                "return": {"sid": int(c["return"].sid), "name": c["return"]["name"], "lat": float(c["return"].lat), "lon": float(c["return"].lon),
                           "spaces_now": None if np.isnan(c["return"].spaces) else int(c["return"].spaces), "cap": int(c["return"].cap),
                           "expected_spaces": round(b["es"], 1), "p_full": round(b["pf"], 2), "arrive_in_min": round(b["t_return_min"])},
                "legs": b["legs"], "total_min": b["total_min"], "risk_penalty_min": round(b["penalty"], 1),
                "points": pts, "benefit": round(b["benefit"], 2),
                "eta": str(pd.Timestamp(depart_ts) + pd.Timedelta(minutes=b["total_min"]))}

    chosen = [(fastest, "fast"), (reliable, "reliable")] + ([(reward, "reward")] if reward else [])
    opts, by_id = [], {}
    for b, kind in chosen:
        key = id(b)
        if key in by_id:                      # 同一條路線同時滿足多個標準
            o = by_id[key]; o.setdefault("also", []).append({"fast": "最快抵達", "reliable": "不用怕沒車沒位", "reward": "順路集點"}[kind])
            if kind == "reward": o["points"] = int(min(30, round(b["benefit"] * 20)))
            continue
        o = pack(b, kind); by_id[key] = o; opts.append(o)

    # 同一條路線贏三個標準時，補上真正不同的替代組合，讓使用者仍有選擇
    if len(opts) < 3:
        used = {(o["borrow"]["sid"], o["return"]["sid"]) for o in opts}
        extras = sorted([x for x in built if (x["c"]["borrow"].sid, x["c"]["return"].sid) not in used],
                        key=lambda x: x["total_min"] + 1.2 * x["penalty"])
        for x in extras[: 3 - len(opts)]:
            o = pack(x, "alt")
            diff = []
            if x["c"]["borrow"].sid != opts[0]["borrow"]["sid"]: diff.append("換借車站")
            if x["c"]["return"].sid != opts[0]["return"]["sid"]: diff.append("換還車站")
            o["diff"] = "、".join(diff) or "不同組合"
            opts.append(o); used.add((o["borrow"]["sid"], o["return"]["sid"]))

    if extended:
        for o in opts:
            if o["kind"] != "fast" and o["legs"][0]["minutes"] > max_walk_min:
                o["note"] = (o.get("note", "") + " 附近站散場後預測都缺車，多走幾分鐘到備援站較穩").strip()
    order = {"time": ["fast", "reliable", "reward", "alt"], "reliable": ["reliable", "fast", "reward", "alt"], "reward": ["reward", "reliable", "fast", "alt"]}.get(preference, ["fast", "reliable", "reward", "alt"])
    opts.sort(key=lambda o: order.index(o["kind"]) if o["kind"] in order else 9)
    if opts:
        opts[0]["primary"] = True
        opts[0]["primary_reason"] = {"time": "你設定最在意準時抵達", "reliable": "你設定最在意不用怕沒車沒位", "reward": "你設定最在意多集點"}.get(preference, "")
        base = opts[0]["total_min"]
        for o in opts[1:]:
            o["primary"] = False; o["delta_min"] = round(o["total_min"] - base, 1); o["delta_points"] = o["points"] - opts[0]["points"]
    return {"options": opts, "preference": preference, "weather_applied": (wf or {}).get("label"), "extended_search": extended,
            "candidates_considered": len(cands), "routed_candidates": len(built), "pruned_by_bound": pruned,
            "assumptions": {k: ASSUMPTIONS[k] for k in ["walk_kmh", "ride_kmh", "reroute_penalty_min", "intent_conversion"]}}


# ---------- 民眾端：附近可借車輛與擁擠程度 ----------
def ease_label(row, minutes_ahead=8):
    """好借程度：綜合現有車數與到達時零車機率。定義寫在介面上。"""
    b = row["bikes"]
    if row["status"] not in DISPATCHABLE or b is None or (isinstance(b, float) and np.isnan(b)):
        return {"key": "unknown", "text": "狀態待查", "color": "#9aa4b2"}
    p = risk(row, minutes_ahead, "empty")
    if b <= 0: return {"key": "none", "text": "目前無車", "color": "#d64545"}
    if b <= 2 or p >= 0.5: return {"key": "tight", "text": "快沒車", "color": "#e08a00"}
    if b <= 5 or p >= 0.25: return {"key": "ok", "text": "還有車", "color": "#e6c229"}
    return {"key": "plenty", "text": "車很多", "color": "#2fa66a"}

def dock_label(row, minutes_ahead=20):
    s_ = row["spaces"]
    if row["status"] not in DISPATCHABLE or s_ is None or (isinstance(s_, float) and np.isnan(s_)):
        return {"key": "unknown", "text": "狀態待查", "color": "#9aa4b2"}
    p = risk(row, minutes_ahead, "full")
    if s_ <= 0: return {"key": "none", "text": "無位可還", "color": "#d64545"}
    if s_ <= 2 or p >= 0.5: return {"key": "tight", "text": "快滿", "color": "#e08a00"}
    if s_ <= 5 or p >= 0.25: return {"key": "ok", "text": "還有位", "color": "#e6c229"}
    return {"key": "plenty", "text": "空位多", "color": "#2fa66a"}

def nearby(pred, center, radius_m=800, limit=25):
    d = pred.copy()
    d["dist_m"] = hav(center[0], center[1], d.lat.values, d.lon.values)
    d = d[d.dist_m <= radius_m].nsmallest(limit, "dist_m")
    out = []
    for _, r in d.iterrows():
        e = ease_label(r); k = dock_label(r)
        out.append({"sid": int(r.sid), "name": r["name"], "lat": float(r.lat), "lon": float(r.lon),
                    "dist_m": int(r.dist_m), "walk_min": round(r.dist_m * ASSUMPTIONS["walk_detour"] / 1000 / ASSUMPTIONS["walk_kmh"] * 60, 1),
                    "bikes": None if pd.isna(r.bikes) else int(r.bikes), "spaces": None if pd.isna(r.spaces) else int(r.spaces), "cap": int(r.cap),
                    "status": r.status, "ease": e, "dock": k,
                    "p_empty_60": float(r.pe_60), "p_full_60": float(r.pf_60)})
    ok = [o for o in out if o["status"] in DISPATCHABLE]
    bikes = sum(o["bikes"] or 0 for o in ok); caps = sum(o["cap"] for o in ok) or 1
    tight = sum(1 for o in ok if o["ease"]["key"] in ("none", "tight"))
    ratio = bikes / caps
    if not ok: area = {"key": "nodata", "text": "附近無可用資料"}
    elif ratio < 0.12 or (tight / max(len(ok), 1)) >= 0.6: area = {"key": "busy", "text": "這區很搶手", "hint": "多數站快沒車，建議往外一點借"}
    elif ratio < 0.3: area = {"key": "normal", "text": "這區普通", "hint": "尖峰時段可能變快"}
    else: area = {"key": "easy", "text": "這區車很充足", "hint": ""}
    return {"stations": out, "area": {**area, "bikes_total": int(bikes), "capacity_total": int(caps),
            "fill_ratio": round(ratio, 2), "stations_counted": len(ok), "tight_stations": tight},
            "definition": "好借程度＝現有車數搭配到站時零車機率；區域擁擠度＝附近站點可借車數佔車柱總數比例與快沒車站點比例。均為快照與預測，不是實際借車成功率。"}
