"""
planner.py — (1) 調度端：120/180 分鐘前的人車安排（可行建議，非真實最優解）
             (2) 民眾端：最快 / 較穩 / 順路集點 三方案
所有假設集中在 ASSUMPTIONS，前端會原文顯示；未取得車隊、班表、集合點資料前皆為明示情境參數。
"""
import math, time, threading
from concurrent.futures import ThreadPoolExecutor
_legpool = ThreadPoolExecutor(max_workers=9)
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
    "lead_time_min": 60,             # 使用者確認：決策到人車抵達通常至少一小時
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
    return d

def plan_dispatch(pred, now_ts, horizon, scenario_delta=None, districts=None, existing_locked=None):
    """每行政區用貪婪最近鄰組趟：先取運出/供給站，再送缺車站。回傳任務列表。"""
    A = ASSUMPTIONS; g = gaps(pred, horizon, scenario_delta)
    target_ts = now_ts + pd.Timedelta(minutes=horizon)
    tasks = []
    locked_sids = existing_locked or set()
    for dist_name, grp in g.groupby("district"):
        if districts and dist_name not in districts: continue
        deficits = grp[(grp.need_in > 0) & ~grp.sid.isin(locked_sids)].copy()
        if deficits.empty: continue
        district_deficit = float(deficits.need_in.sum()); covered = 0.0; event_sids = set(grp[grp.event_delta != 0].sid.tolist())
        sources = grp[(grp.need_out > 0) | (grp.donor > 0)].copy()
        sources["avail"] = np.where(sources.need_out > 0, np.maximum(sources.need_out, np.minimum(sources.pb_adj - sources.min_stock, 8)), sources.donor)
        sources = sources[sources.avail > 0]
        depot = (float(grp.lat.mean()), float(grp.lon.mean()))
        trip_no = 0
        while not deficits.empty and trip_no < 3:
            trip_no += 1
            cap_left = A["truck_capacity"]; stops = []; pos = depot; load = 0
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
            if A["depot_supply"] and load < min(total_def, A["truck_capacity"]) and cap_left > 0:
                q = int(min(cap_left, math.ceil(total_def - load)))
                if q > 0:
                    stops.insert(0, {"sid": -1, "name": "調度中心／跨區補給（情境）", "lat": depot[0], "lon": depot[1], "action": "pickup", "qty": q, "now_bikes": None, "pred_bikes": None, "cap": None,
                                     "reason": "區內可供給站不足，由調度中心或跨區整車補給；倉儲庫存未提供，為情境假設"})
                    cap_left -= q; load += q
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
                stops.append({"sid": int(r.sid), "name": r["name"], "lat": float(r.lat), "lon": float(r.lon), "action": "dropoff", "qty": q,
                              "now_bikes": None if np.isnan(r.bikes) else int(r.bikes), "pred_bikes": round(float(r.pb_adj), 1), "cap": int(r.cap),
                              "p_empty": round(float(r.pe_h), 2), "reason": f"預測 {horizon} 分鐘後零車機率 {r.pe_h:.0%}，低於目標庫存 {int(r.min_stock)} 輛"})
                load -= q; covered += q; pos = (float(r.lat), float(r.lon)); deficits = deficits.drop(deficits.index[i])
            # 時間估算
            t = 0.0; prev = depot; legs = []
            for s_ in stops:
                dm = float(hav(prev[0], prev[1], s_["lat"], s_["lon"])) * A["road_detour"]
                tm = dm / 1000 / A["truck_speed_kmh"] * 60 + A["handling_fixed_min"] + A["handling_per_bike_min"] * s_["qty"]
                t += tm; s_["eta_min_from_depart"] = round(t); legs.append(round(dm)); prev = (s_["lat"], s_["lon"])
            first_drop = next((s_["eta_min_from_depart"] for s_ in stops if s_["action"] == "dropoff"), t)
            depart_by = target_ts - pd.Timedelta(minutes=first_drop)
            earliest_arrival = now_ts + pd.Timedelta(minutes=A["lead_time_min"]) + pd.Timedelta(minutes=first_drop)
            feasible = earliest_arrival <= target_ts
            tasks.append({"district": dist_name, "horizon": horizon, "trip": trip_no, "stops": stops, "route_minutes": round(t), "route_km": round(sum(legs) / 1000, 1),
                          "load": int(sum(s_["qty"] for s_ in stops if s_["action"] == "pickup")),
                          "target_ts": str(target_ts), "depart_by": str(min(depart_by, target_ts)), "earliest_arrival": str(earliest_arrival),
                          "status": "planned" if feasible else "too_late",
                          "reason": ("提前 %d 分鐘安排，可在目標時間前抵達" % horizon) if feasible else "決策到抵達至少 60 分鐘，來不及在目標時間前完成；改以在勤資源與民眾分流因應",
                          "depot": depot, "event_related": any(s_["sid"] in event_sids for s_ in stops)})
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
    cands = []
    for _, a in borrow.iterrows():
        w1 = leg(origin, (a.lat, a.lon), "walk", use_osrm=False); t_b = lead + w1["minutes"]
        pe = risk(a, t_b, "empty"); eb = expected_stock(a, t_b, "bikes")
        for _, b in ret.iterrows():
            if a.sid == b.sid: continue
            if a.d_d <= b.d_d + 150 or hav(a.lat, a.lon, b.lat, b.lon) < 400: continue   # 必須往目的地推進且騎乘段 ≥ 400m
            rd = leg((a.lat, a.lon), (b.lat, b.lon), "ride", use_osrm=False); rd["minutes"] = round(rd["minutes"] * t_mult, 1); t_r = t_b + rd["minutes"]
            pf = risk(b, t_r, "full"); es = expected_stock(b, t_r, "spaces")
            w2 = leg((b.lat, b.lon), dest, "walk", use_osrm=False)
            total = w1["minutes"] + rd["minutes"] + w2["minutes"]
            penalty = (pe + pf) * A["reroute_penalty_min"] * r_mult
            # 供需效益：從預測將滿的站借、還到預測將缺的站
            benefit = 0.0
            benefit += max(0.0, float(a["pf_120"]) - 0.2) * 1.0 + max(0.0, (float(a["bikes"]) if not np.isnan(a["bikes"]) else 0) / max(a["cap"], 1) - 0.7) * 0.5
            benefit += max(0.0, float(b["pe_120"]) - 0.2) * 1.5 + max(0.0, 0.3 - (expected_stock(b, 120, "bikes") / max(b["cap"], 1))) * 0.5
            # 分流：附近有站將滿/將空時，選擇風險低的替代站本身就是效益
            if hot_full >= 0.35 and pf <= 0.2: benefit += (hot_full - pf) * 0.8
            if hot_empty >= 0.35 and pe <= 0.2: benefit += (hot_empty - pe) * 0.8
            cands.append({"borrow": a, "return": b, "w1": w1, "ride": rd, "w2": w2, "total": total, "pe": pe, "pf": pf, "eb": eb, "es": es,
                          "penalty": penalty, "benefit": benefit, "t_borrow_min": t_b, "t_return_min": t_r})
    if not cands: return {"options": [], "error": "沒有可行的借還組合"}
    fastest = min(cands, key=lambda c: c["total"])
    reliable = min(cands, key=lambda c: c["total"] + 2.5 * c["penalty"] + (15 if (c["pe"] > 0.3 or c["pf"] > 0.3) else 0))
    reward = None
    if want_reward:
        pool = [c for c in cands if c["pe"] <= 0.4 and c["pf"] <= 0.4 and c["benefit"] >= 0.1 and c["total"] <= fastest["total"] + 10]
        if pool: reward = max(pool, key=lambda c: c["benefit"] - 0.02 * (c["total"] - fastest["total"]))
    def pack(c, kind, now_ts, depart_ts):
        # 只對最終三方案抓 OSRM 幾何（三段並行）
        fa = _legpool.submit(leg, origin, (c["borrow"].lat, c["borrow"].lon), "walk")
        fb = _legpool.submit(leg, (c["borrow"].lat, c["borrow"].lon), (c["return"].lat, c["return"].lon), "ride")
        fc = _legpool.submit(leg, (c["return"].lat, c["return"].lon), dest, "walk")
        w1, rd, w2 = fa.result(), fb.result(), fc.result()
        rd["minutes"] = round(rd["minutes"] * t_mult, 1)
        total = w1["minutes"] + rd["minutes"] + w2["minutes"]
        pts = 0
        if kind == "reward": pts = int(min(30, round(c["benefit"] * 20)))
        return {"kind": kind, "label": {"fast": "最快抵達", "reliable": "借還較穩", "reward": "順路集點"}[kind],
                "borrow": {"sid": int(c["borrow"].sid), "name": c["borrow"]["name"], "lat": float(c["borrow"].lat), "lon": float(c["borrow"].lon),
                           "bikes_now": None if np.isnan(c["borrow"].bikes) else int(c["borrow"].bikes), "cap": int(c["borrow"].cap),
                           "expected_bikes": round(c["eb"], 1), "p_empty": round(c["pe"], 2), "arrive_in_min": round(c["t_borrow_min"])},
                "return": {"sid": int(c["return"].sid), "name": c["return"]["name"], "lat": float(c["return"].lat), "lon": float(c["return"].lon),
                           "spaces_now": None if np.isnan(c["return"].spaces) else int(c["return"].spaces), "cap": int(c["return"].cap),
                           "expected_spaces": round(c["es"], 1), "p_full": round(c["pf"], 2), "arrive_in_min": round(c["t_return_min"])},
                "legs": [w1, rd, w2], "total_min": round(total, 1), "risk_penalty_min": round(c["penalty"], 1),
                "points": pts, "benefit": round(c["benefit"], 2),
                "eta": str(pd.Timestamp(depart_ts) + pd.Timedelta(minutes=total))}
    f1 = _legpool.submit(pack, fastest, "fast", now_ts, depart_ts); f2 = _legpool.submit(pack, reliable, "reliable", now_ts, depart_ts)
    f3 = _legpool.submit(pack, reward, "reward", now_ts, depart_ts) if reward else None
    opts = [f1.result(), f2.result()]
    if reward:
        rw = f3.result()
        if reward is fastest: rw["note"] = "與最快方案相同路線，加計集點"
        elif reward is reliable: rw["note"] = "與較穩方案相同路線，加計集點"
        opts.append(rw)
    if extended:
        for o in opts:
            if o["kind"] != "fast" and o["legs"][0]["minutes"] > max_walk_min: o["note"] = (o.get("note", "") + " 附近站散場後預測都缺車，多走幾分鐘到備援站較穩").strip()
    # 去除完全重複的方案（借還站相同者只留主推薦優先級較高的）
    seen = {}; uniq = []
    for o in opts:
        k = (o["borrow"]["sid"], o["return"]["sid"])
        if k in seen:
            seen[k]["also"] = seen[k].get("also", []) + [o["label"]]
            seen[k]["points"] = max(seen[k]["points"], o["points"]); continue
        seen[k] = o; uniq.append(o)
    opts = uniq
    order = {"time": ["fast", "reliable", "reward"], "reliable": ["reliable", "fast", "reward"], "reward": ["reward", "reliable", "fast"]}.get(preference, ["fast", "reliable", "reward"])
    opts.sort(key=lambda o: order.index(o["kind"]) if o["kind"] in order else 9)
    if opts:
        opts[0]["primary"] = True
        opts[0]["primary_reason"] = {"time": "你設定最在意準時抵達", "reliable": "你設定最在意一定借得到", "reward": "你設定最在意多集點"}.get(preference, "")
        base = opts[0]["total_min"]
        for o in opts[1:]:
            o["primary"] = False; o["delta_min"] = round(o["total_min"] - base, 1); o["delta_points"] = o["points"] - opts[0]["points"]
    return {"options": opts, "preference": preference, "weather_applied": (wf or {}).get("label"), "extended_search": extended, "candidates_considered": len(cands), "assumptions": {k: ASSUMPTIONS[k] for k in ["walk_kmh", "ride_kmh", "reroute_penalty_min", "intent_conversion"]}}


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
