"""
server.py — 新北 YouBike 雙端協同服務 demo 後端（FastAPI）。
三端共用：政府端(/gov) 調度端(/ops) 民眾端(/citizen) 與展示舞台(/)。
真實計算：歷史矩陣回放、預測、站群、缺口、排程建議、三方案、意向修正。
明示模擬：派車執行進度、店家接單/驗收、獎勵兌付、通知送達(頁內)、活動出席人數(情境參數)。
"""
import os, sys, json, time, asyncio, uuid, threading, math
from datetime import datetime
import numpy as np, pandas as pd
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, StreamingResponse, JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
sys.path.insert(0, os.path.dirname(__file__))
from predict import Predictor
import planner as PL
import llm as LLM
import weather as WX
import awsloc as AWSLOC

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
STATIC = os.path.join(ROOT, "app", "static")
app = FastAPI(title="新北 YouBike 雙端協同服務")
app.mount("/static", StaticFiles(directory=STATIC), name="static")

print("loading predictor ...", flush=True)
PRED = Predictor()
print("predictor ready; model horizons:", PRED.model_horizons, flush=True)
ST = PRED.st
def sid_by_name(name):
    m = ST.index[ST["name"] == name]
    return int(ST.loc[m[0], "sid"]) if len(m) else None
def coord(sid): r = ST.loc[ST.sid == sid].iloc[0]; return (float(r.lat), float(r.lon))

# ------------------------------------------------------------------ 狀態
FOCUS_DISTRICTS = ["板橋區", "新莊區", "土城區"]
STATE = {
    "clock": {"t_idx": PRED.t_index("2026-06-16 07:00"), "playing": False, "speed": 4.0},
    "user": {"name": "小映", "home": None, "work": None, "commute_out": "07:40", "commute_back": "18:10", "days": [0, 1, 2, 3, 4],
             "max_walk_min": 12, "student": False, "notify": {"before_min": 15, "cooldown_min": 240}},
    "intents": [], "tickets": [], "alerts": {}, "alert_log": [], "tasks": [], "task_seq": 0,
    "notifications": {"citizen": [], "gov": [], "ops": []},
    "rewards": {"points": 92, "seed_points": 92, "demo_seed": True,
                "stamps": [{"name": "板橋區生活圈章", "type": "district", "ts": "2026-06-03 08:12"},
                           {"name": "新莊區生活圈章", "type": "district", "ts": "2026-06-09 18:41"},
                           {"name": "維修回報章", "type": "service", "ts": "2026-06-11 19:02"}],
                "coupons": [{"tier": 50, "title": "模擬商家優惠：咖啡折 10 元", "note": "合作商家待洽談，不代表可實際兌付", "ts": "2026-06-10 09:00"}],
                "history": [{"ts": "2026-06-15 18:22", "points": 8, "reason": "順路集點：還到預測將缺車的站（示範紀錄）"},
                            {"ts": "2026-06-12 08:05", "points": 5, "reason": "完成一趟 2.8 公里（示範紀錄）"},
                            {"ts": "2026-06-11 19:02", "points": 12, "reason": "有效報修經店家驗收（示範紀錄）"}]},
    "scenario": {"name": None, "event": None},
    "explain": {}, "last_reminder": {}, "last_plan_hour": None, "cloud": {"dynamodb": "unknown", "writes": 0, "errors": 0},
    "kpi_history": [],
    "profile": {"onboarded": False, "nickname": "我", "role": "worker", "home_sid": None, "work_sid": None,
                "out_time": "07:40", "back_time": "18:10", "join_rewards": True, "preference": "time", "max_walk_min": 12},
    "trip": None, "trip_log": [], "choice_log": [], "pref_prompt": None,
}
SEED_TRIPS = [   # 示範帳戶歷史：僅用於存摺呈現，明確標示為模擬紀錄
    ("2026-06-01", 2.8), ("2026-06-02", 3.1), ("2026-06-02", 2.9), ("2026-06-03", 3.1), ("2026-06-04", 2.7),
    ("2026-06-05", 3.4), ("2026-06-05", 2.8), ("2026-06-08", 3.0), ("2026-06-09", 3.2), ("2026-06-09", 2.9),
    ("2026-06-10", 2.6), ("2026-06-11", 3.3), ("2026-06-12", 2.8), ("2026-06-15", 3.1), ("2026-06-15", 3.0),
]
SUBS = []  # SSE queues
LOOP = None

def now_ts(): return PRED.bins[STATE["clock"]["t_idx"]]
def iso(ts): return pd.Timestamp(ts).strftime("%Y-%m-%d %H:%M")

def broadcast(ev_type, payload):
    msg = json.dumps({"type": ev_type, "ts": iso(now_ts()), "payload": payload}, ensure_ascii=False, default=str)
    for q in list(SUBS):
        try: q.put_nowait(msg)
        except Exception: pass

def notify(channel, title, body, kind="info", extra=None):
    n = {"id": uuid.uuid4().hex[:8], "ts": iso(now_ts()), "title": title, "body": body, "kind": kind, "channel": channel, "extra": extra or {}}
    STATE["notifications"][channel].insert(0, n); STATE["notifications"][channel] = STATE["notifications"][channel][:60]
    broadcast("notify", n); return n

# ------------------------------------------------------------------ DynamoDB（盡力寫入，不影響 demo）
_ddb = None
def ddb_put(table, item):
    def _w():
        global _ddb
        try:
            if _ddb is None:
                import boto3; _ddb = boto3.Session(profile_name=os.environ.get("AWS_PROFILE", "hackathon")).resource("dynamodb", region_name="us-west-2")
            _ddb.Table(table).put_item(Item={"pk": str(item.get("id")), "data": json.dumps(item, ensure_ascii=False, default=str), "updated": iso(now_ts())})
            STATE["cloud"]["dynamodb"] = "ok"; STATE["cloud"]["writes"] += 1
        except Exception as e:
            STATE["cloud"]["dynamodb"] = f"error: {type(e).__name__}"; STATE["cloud"]["errors"] += 1
    threading.Thread(target=_w, daemon=True).start()

# ------------------------------------------------------------------ 預測 + 意向修正
def current_pred(adjusted=True):
    p = PRED.predict_all(STATE["clock"]["t_idx"])
    return PL.apply_intents(p, STATE["intents"], now_ts()) if adjusted and STATE["intents"] else p

def scenario_delta_for(horizon):
    """活動情境：散場後外流借車、入場前流入還車；只在目標時間落在窗內時套用。"""
    ev = STATE["scenario"].get("event")
    if not ev: return None
    tgt = now_ts() + pd.Timedelta(minutes=horizon)
    start, end = pd.Timestamp(ev["start"]), pd.Timestamp(ev["end"])
    delta = {}
    if end <= tgt <= end + pd.Timedelta(minutes=75):
        for sid, share in ev["station_share"].items(): delta[int(sid)] = -round(ev["riders_out"] * share, 1)
    elif start - pd.Timedelta(minutes=75) <= tgt <= start:
        for sid, share in ev["station_share"].items(): delta[int(sid)] = round(ev["riders_in"] * share, 1)
    return delta or None

# ------------------------------------------------------------------ 告警規則
SEV = {"high": 3, "warn": 2, "info": 1}
def evaluate_alerts(p):
    t = STATE["clock"]["t_idx"]; B = PRED.ctx.B; S = PRED.ctx.S
    cands = []
    for r in p.itertuples():
        sid = r.sid; st = r.status
        if st == "stale_flat": cands.append((sid, "stale_flat", "info", f"{r.name}：24 小時以上庫存無變化，疑似停站或資料未更新，列入待查、不派車", 0.0))
        elif st == "both_zero": cands.append((sid, "both_zero", "info", f"{r.name}：可借與可還同時為 0，狀態待查、不直接派車", 0.0))
        elif st == "cap_conflict": cands.append((sid, "cap_conflict", "info", f"{r.name}：可借＋可還超過總車柱，容量版本待確認", 0.0))
        elif st == "empty":
            prev = B[max(0, t-2):t, sid]
            if len(prev) == 2 and np.all(prev == 0): cands.append((sid, "persistent_empty", "high", f"{r.name}：已連續 90 分鐘以上無車可借", 1.0))
        elif st == "full":
            prev = S[max(0, t-2):t, sid]
            if len(prev) == 2 and np.all(prev == 0): cands.append((sid, "persistent_full", "high", f"{r.name}：已連續 90 分鐘以上無位可還", 1.0))
        if st == "normal":
            if r.pe_60 >= 0.6 and r.bikes > 0: cands.append((sid, "forecast_empty_60", "high", f"{r.name}：現有 {int(r.bikes)} 輛，60 分鐘後零車機率 {r.pe_60:.0%}", float(r.pe_60)))
            elif r.pe_120 >= 0.5 and r.bikes > 2: cands.append((sid, "forecast_empty_120", "warn", f"{r.name}：現有 {int(r.bikes)} 輛，120 分鐘後零車機率 {r.pe_120:.0%}，可提前安排", float(r.pe_120)))
            elif r.pe_180 >= 0.5 and r.bikes > 3: cands.append((sid, "forecast_empty_180", "warn", f"{r.name}：180 分鐘後零車機率 {r.pe_180:.0%}，建議納入下一趟排程", float(r.pe_180)))
            if r.pf_60 >= 0.6 and r.spaces > 0: cands.append((sid, "forecast_full_60", "high", f"{r.name}：剩 {int(r.spaces)} 格，60 分鐘後零位機率 {r.pf_60:.0%}", float(r.pf_60)))
            elif r.pf_120 >= 0.5 and r.spaces > 2: cands.append((sid, "forecast_full_120", "warn", f"{r.name}：剩 {int(r.spaces)} 格，120 分鐘後零位機率 {r.pf_120:.0%}，可提前安排運出", float(r.pf_120)))
    # 去重：同站同類型開啟中不重發；節流：每步最多 8 則新告警，其餘併入摘要
    new, suppressed = [], 0
    active_keys = set()
    for sid, typ, sev, msg, score in sorted(cands, key=lambda x: (-SEV[x[2]], -x[4])):
        key = f"{sid}:{typ}"; active_keys.add(key)
        if key in STATE["alerts"] and STATE["alerts"][key]["status"] != "resolved": continue
        if typ in ("stale_flat", "both_zero", "cap_conflict") and any(a["key"] == key and a["opened"][:10] == iso(now_ts())[:10] for a in STATE["alert_log"][-300:]): continue
        if len(new) >= 8: suppressed += 1; continue
        a = {"id": uuid.uuid4().hex[:8], "key": key, "sid": int(sid), "station": p.loc[p.sid == sid, "name"].iloc[0], "district": p.loc[p.sid == sid, "district"].iloc[0],
             "type": typ, "severity": sev, "message": msg, "score": round(score, 2), "status": "open", "opened": iso(now_ts()), "acked": None, "resolved": None,
             "route": "待查清單" if typ in ("stale_flat", "both_zero", "cap_conflict") else ("調度端" if "persistent" in typ or "_60" in typ else "提前排程")}
        STATE["alerts"][key] = a; STATE["alert_log"].append(a); new.append(a); ddb_put("yb_alerts", a)
    # 自動解除：條件消失
    for key, a in STATE["alerts"].items():
        if a["status"] != "resolved" and key not in active_keys and a["type"] not in ("stale_flat", "both_zero", "cap_conflict"):
            a["status"] = "resolved"; a["resolved"] = iso(now_ts()); a["resolve_reason"] = "條件已解除（自動）"
    ev = STATE["scenario"].get("event")
    if ev:
        for lead in (180, 120):
            key = f"event:{ev['title']}:{lead}"
            if abs((pd.Timestamp(ev["end"]) - pd.Timedelta(minutes=lead) - now_ts()).total_seconds()) < 60 and key not in STATE["alerts"]:
                a = {"id": uuid.uuid4().hex[:8], "key": key, "sid": -1, "station": ev["venue"], "district": "新莊區", "type": "event_surge", "severity": "warn",
                     "message": f"{ev['title']}：{pd.Timestamp(ev['end']).strftime('%H:%M')} 散場，情境估 {ev['riders_out']} 人借車（600 m 內 {len(ev['stations'])} 站）。提前 {lead} 分鐘安排補車與分流。",
                     "score": 0.9, "status": "open", "opened": iso(now_ts()), "acked": None, "resolved": None, "route": "提前排程"}
                STATE["alerts"][key] = a; STATE["alert_log"].append(a); new.append(a)
    for a in new: broadcast("alert", a)
    if suppressed: broadcast("alert_digest", {"suppressed": suppressed, "message": f"另有 {suppressed} 站達到告警門檻，已併入清單不逐則通知"})
    return new

# ------------------------------------------------------------------ 調度任務
def plan_tasks():
    p = current_pred()
    hour_key = iso(now_ts())[:13]
    if STATE["last_plan_hour"] == hour_key: return []
    STATE["last_plan_hour"] = hour_key
    created = []
    for horizon in (120, 180):
        locked = {s["sid"] for tk in STATE["tasks"] if tk["status"] in ("dispatched", "en_route") for s in tk["stops"] if s["action"] == "dropoff"}
        tasks = PL.plan_dispatch(p, now_ts(), horizon, scenario_delta_for(horizon), districts=FOCUS_DISTRICTS, existing_locked=locked)
        for tk in tasks:
            if tk["status"] == "gap_summary":
                STATE["tasks"] = [x for x in STATE["tasks"] if not (x["status"] == "gap_summary" and x["district"] == tk["district"] and x["horizon"] == horizon)]
                STATE["task_seq"] += 1; tk.update({"id": f"G{STATE['task_seq']:03d}", "created": iso(now_ts()), "history": []}); STATE["tasks"].append(tk); created.append(tk); continue
            drops = tuple(sorted(s["sid"] for s in tk["stops"] if s["action"] == "dropoff"))
            dup = None
            for ex in STATE["tasks"]:
                if ex["status"] in ("planned", "dispatched", "en_route", "proposed") and ex["district"] == tk["district"] and ex["horizon"] == horizon:
                    exd = tuple(sorted(s["sid"] for s in ex["stops"] if s["action"] == "dropoff"))
                    if exd == drops: dup = ex; break
                    if ex["status"] == "planned" and set(exd) & set(drops):
                        ex["status"] = "superseded"; ex["reason"] = "需求變化，由新建議取代（尚未出車，未造成重排）"
            if dup: continue
            STATE["task_seq"] += 1
            tk.update({"id": f"T{STATE['task_seq']:03d}", "created": iso(now_ts()), "history": [{"ts": iso(now_ts()), "status": tk["status"], "note": tk["reason"]}]})
            tk.setdefault("event_related", False)
            STATE["tasks"].append(tk); created.append(tk); ddb_put("yb_tasks", tk)
    STATE["tasks"] = STATE["tasks"][-60:]
    for tk in created:
        broadcast("task", tk)
        if tk["status"] == "planned":
            notify("ops", f"新排程 {tk['id']}｜{tk['district']}｜{tk['horizon']} 分鐘前安排", f"{len(tk['stops'])} 站、載 {tk['load']} 輛，最遲 {tk['depart_by'][11:16]} 出發", "task", {"task_id": tk["id"]})
        elif tk["status"] == "too_late":
            notify("ops", f"來不及派車｜{tk['district']}", tk["reason"], "warn", {"task_id": tk["id"]})
    return created

def progress_tasks():
    now = now_ts()
    for tk in STATE["tasks"]:
        if tk["status"] == "planned" and pd.Timestamp(tk["depart_by"]) <= now:
            tk["status"] = "dispatched"; tk["history"].append({"ts": iso(now), "status": "dispatched", "note": "模擬：調度車出發（未接營運商派車系統）"}); broadcast("task", tk)
        elif tk["status"] in ("dispatched", "en_route"):
            dep = pd.Timestamp(tk["depart_by"]); elapsed = (now - dep).total_seconds() / 60
            done = [s for s in tk["stops"] if s["eta_min_from_depart"] <= elapsed]
            for s in done: s["done"] = True
            if len(done) == len(tk["stops"]):
                tk["status"] = "done"; tk["history"].append({"ts": iso(now), "status": "done", "note": "模擬：全部站點完成"}); broadcast("task", tk)
            elif tk["status"] == "dispatched":
                tk["status"] = "en_route"; tk["history"].append({"ts": iso(now), "status": "en_route", "note": "模擬：行進中"}); broadcast("task", tk)

# ------------------------------------------------------------------ 民眾提醒（觸發範圍、冷卻、不在移動中要求操作）
def check_reminders():
    u = STATE["user"]; now = now_ts()
    if u["home"] is None or u["work"] is None: return
    if now.weekday() not in u["days"]: return
    for key, hhmm, origin, dest in [("out", u["commute_out"], u["home"], u["work"]), ("back", u["commute_back"], u["work"], u["home"])]:
        target = pd.Timestamp(f"{now.date()} {hhmm}")
        lead = (target - now).total_seconds() / 60
        if 0 <= lead <= u["notify"]["before_min"] + 15:
            last = STATE["last_reminder"].get(key)
            if last and (now - pd.Timestamp(last)).total_seconds() / 60 < u["notify"]["cooldown_min"]: continue
            STATE["last_reminder"][key] = str(now)
            w = wx_now()
            res = PL.plan_trip(apply_dispatch_to_pred(apply_event_to_pred(current_pred())), origin, dest, target, now,
                               STATE["profile"]["max_walk_min"], weather=w, preference=STATE["profile"]["preference"])
            res["weather"] = w; res["depart_ts"] = str(target)
            if res.get("options"):
                o = res["options"][0]
                wtxt = ("外面在下雨（" + str(w.get("rain_mm")) + " 毫米），記得雨具，騎乘時間已加計。" if w.get("is_raining")
                        else ("等等可能下雨，" if w.get("will_rain_3h") else "目前沒有下雨，"))
                notify("citizen", f"{hhmm} 出發提醒｜{'上班' if key=='out' else '回家'}", f"{wtxt}建議 {o['borrow']['name']} 借 → {o['return']['name']} 還，約 {o['total_min']} 分" + (f"，可得 {o['points']} 點" if o['points'] else ""), "reminder", {"plan": res, "trip": key})
    ev = STATE["scenario"].get("event")
    if ev and abs((pd.Timestamp(ev["end"]) - pd.Timedelta(minutes=30) - now).total_seconds()) < 60 and not STATE["last_reminder"].get("event"):
        STATE["last_reminder"]["event"] = str(now)
        notify("citizen", f"散場提醒｜{ev['title']}", "散場後場館旁的站會很快沒車。建議多走 5 分鐘到備援站，順路集點另有加碼。", "event", {"event": ev})
    if u["student"] and now.weekday() < 5 and iso(now)[11:16] == "15:30" and not STATE["last_reminder"].get("student:" + str(now.date())):
        STATE["last_reminder"]["student:" + str(now.date())] = str(now)
        notify("citizen", "放學順路任務", "今天 18:00 捷運站出口預測會缺車。放學騎去捷運站還車，順路可得接力章 +15 點。", "task")

# ------------------------------------------------------------------ 維修工單（模擬流程）
TICKET_FLOW = ["reported", "accepted", "on_site", "recovered", "verified", "closed"]
TICKET_LABEL = {"reported": "已回報", "accepted": "授權店家接單（模擬）", "on_site": "現場處理中（模擬）", "recovered": "已回收/維修（模擬）", "verified": "驗收復役（模擬）", "closed": "結案，核發獎勵"}
def progress_tickets():
    for tk in STATE["tickets"]:
        if tk["status"] == "closed": continue
        steps = int((now_ts() - pd.Timestamp(tk["ts"])).total_seconds() / 1800)
        want = TICKET_FLOW[min(len(TICKET_FLOW) - 1, steps)]
        if TICKET_FLOW.index(want) > TICKET_FLOW.index(tk["status"]):
            tk["status"] = want; tk["history"].append({"ts": iso(now_ts()), "status": want, "label": TICKET_LABEL[want]})
            broadcast("ticket", tk); ddb_put("yb_tickets", tk)
            if want == "closed":
                grant_reward(12, f"有效報修 {tk['station']}", stamp={"name": "維修回報章", "type": "service"})
                notify("citizen", "報修已結案", f"{tk['station']} 的回報經店家驗收復役，獲得維修回報章 +12 點（獎勵兌付為模擬）", "reward")

def grant_reward(points, reason, stamp=None, coupon=None):
    R = STATE["rewards"]; R["points"] += points
    R["history"].insert(0, {"ts": iso(now_ts()), "points": points, "reason": reason})
    if stamp and stamp["name"] not in [s["name"] for s in R["stamps"]]: R["stamps"].append({**stamp, "ts": iso(now_ts())})
    if coupon: R["coupons"].append({**coupon, "ts": iso(now_ts())})
    elif R["points"] >= 50 and not any(c["tier"] == 50 for c in R["coupons"]):
        R["coupons"].append({"tier": 50, "title": "模擬商家優惠：咖啡折 10 元", "note": "合作商家待洽談，不代表可實際兌付", "ts": iso(now_ts())})
    broadcast("reward", R)

# ------------------------------------------------------------------ 時鐘
def kpis(p):
    ok = p[p.status.isin(PL.DISPATCHABLE)]
    open_alerts = [a for a in STATE["alerts"].values() if a["status"] != "resolved"]
    acks = [(pd.Timestamp(a["acked"]) - pd.Timestamp(a["opened"])).total_seconds() / 60 for a in STATE["alert_log"] if a.get("acked")]
    active_int = [i for i in STATE["intents"] if i["status"] == "active"]
    return {"ts": iso(now_ts()), "stations": int(len(p)), "empty_now": int((p.status == "empty").sum()), "full_now": int((p.status == "full").sum()),
            "both_zero": int((p.status == "both_zero").sum()), "stale": int((p.status == "stale_flat").sum()), "no_data": int((p.status == "no_data").sum()),
            "risk_empty_60": int((ok.pe_60 >= 0.5).sum()), "risk_empty_120": int((ok.pe_120 >= 0.5).sum()), "risk_empty_180": int((ok.pe_180 >= 0.5).sum()),
            "risk_full_60": int((ok.pf_60 >= 0.5).sum()), "risk_full_120": int((ok.pf_120 >= 0.5).sum()), "risk_full_180": int((ok.pf_180 >= 0.5).sum()),
            "open_alerts": len(open_alerts), "high_alerts": sum(1 for a in open_alerts if a["severity"] == "high"),
            "mean_ack_min": round(float(np.mean(acks)), 1) if acks else None,
            "tasks_planned": sum(1 for t in STATE["tasks"] if t["status"] in ("planned", "dispatched", "en_route")),
            "intents_active": len(active_int), "expected_diverted": round(len(active_int) * PL.ASSUMPTIONS["intent_conversion"], 1),
            "model_source": p.source.iloc[0], "model_horizons": PRED.model_horizons}

def on_tick():
    p = current_pred()
    evaluate_alerts(p); plan_tasks(); progress_tasks(); progress_tickets(); check_reminders()
    k = kpis(p); STATE["kpi_history"].append(k); STATE["kpi_history"] = STATE["kpi_history"][-96:]
    broadcast("tick", {"clock": {**STATE["clock"], "ts": k["ts"]}, "kpi": k})

async def clock_loop():
    while True:
        try:
            if STATE["clock"]["playing"]:
                if STATE["clock"]["t_idx"] < len(PRED.bins) - 7:
                    STATE["clock"]["t_idx"] += 1; await asyncio.get_event_loop().run_in_executor(None, on_tick)
                else: STATE["clock"]["playing"] = False
            await asyncio.sleep(STATE["clock"]["speed"])
        except Exception as e:
            print("tick error", e, flush=True); await asyncio.sleep(1)

@app.on_event("startup")
async def _start():
    global LOOP; LOOP = asyncio.get_event_loop(); asyncio.create_task(clock_loop())
    threading.Thread(target=lambda: (time.sleep(1), set_scenario_sync("commute_am")), daemon=True).start()   # 預設情境
    threading.Thread(target=lambda: (time.sleep(3), api_live(), api_culture()), daemon=True).start()   # 預熱外部資料快取

# ------------------------------------------------------------------ 頁面
@app.get("/", response_class=HTMLResponse)
def entry(): return FileResponse(os.path.join(STATIC, "index.html"))
@app.get("/stage", response_class=HTMLResponse)
def stage(): return FileResponse(os.path.join(STATIC, "stage.html"))
@app.get("/gov", response_class=HTMLResponse)
def gov(): return FileResponse(os.path.join(STATIC, "gov.html"))
@app.get("/ops", response_class=HTMLResponse)
def ops(): return FileResponse(os.path.join(STATIC, "ops.html"))
@app.get("/citizen", response_class=HTMLResponse)
def citizen(): return FileResponse(os.path.join(STATIC, "citizen.html"))

# ------------------------------------------------------------------ API
@app.get("/api/events")
async def events(request: Request):
    q = asyncio.Queue(maxsize=500); SUBS.append(q)
    async def gen():
        try:
            yield "data: " + json.dumps({"type": "hello", "payload": {"clock": STATE["clock"], "ts": iso(now_ts())}}, ensure_ascii=False) + "\n\n"
            while True:
                if await request.is_disconnected(): break
                try:
                    msg = await asyncio.wait_for(q.get(), timeout=15); yield f"data: {msg}\n\n"
                except asyncio.TimeoutError: yield ": ping\n\n"
        finally:
            if q in SUBS: SUBS.remove(q)
    return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

from fastapi.responses import Response
@app.get("/api/tiles/{tileset}/{z}/{x}/{y}")
def api_tiles(tileset: str, z: int, x: int, y: int):
    if tileset not in ("raster.satellite", "vector.basemap"): return JSONResponse({"error": "bad tileset"}, 400)
    b = AWSLOC.tile(tileset, z, x, y)
    if b is None: return Response(status_code=204)
    return Response(b, media_type="image/jpeg" if tileset.startswith("raster") else "application/vnd.mapbox-vector-tile",
                    headers={"Cache-Control": "public, max-age=86400"})

# ---------- Amazon Athena：直接查 S3 資料湖上的 1,332 萬筆原始觀測 ----------
ATHENA = {"db": "ntpc_youbike", "bucket": "ntpc-youbike-hackathon-329899784315", "cache": {}}
ATHENA_QUERIES = {
    "district_empty": {"label": "各行政區零車觀測比例（全期）", "sql": """
SELECT st.district AS "行政區", count(*) AS "觀測數",
       round(100.0 * sum(CASE WHEN o.bikes = 0 THEN 1 ELSE 0 END) / count(*), 2) AS "零車比例",
       round(100.0 * sum(CASE WHEN o.spaces = 0 THEN 1 ELSE 0 END) / count(*), 2) AS "零位比例"
FROM observations o JOIN stations st ON o.sid = st.sid
WHERE o.valid GROUP BY st.district ORDER BY 3 DESC LIMIT 12
"""},
    "hourly": {"label": "工作日各小時零車與零位比例", "sql": """
SELECT hour(o.bin) AS "小時", count(*) AS "觀測數",
       round(100.0 * sum(CASE WHEN o.bikes = 0 THEN 1 ELSE 0 END) / count(*), 2) AS "零車比例",
       round(100.0 * sum(CASE WHEN o.spaces = 0 THEN 1 ELSE 0 END) / count(*), 2) AS "零位比例"
FROM observations o WHERE o.valid AND day_of_week(o.bin) <= 5
GROUP BY hour(o.bin) ORDER BY 1
"""},
    "worst_stations": {"label": "空或滿比例最高的站點（前 12）", "sql": """
SELECT st.district AS "行政區", st.name AS "站名", count(*) AS "觀測數",
       round(100.0 * sum(CASE WHEN o.bikes = 0 OR o.spaces = 0 THEN 1 ELSE 0 END) / count(*), 2) AS "空或滿比例"
FROM observations o JOIN stations st ON o.sid = st.sid
WHERE o.valid GROUP BY st.district, st.name HAVING count(*) > 5000 ORDER BY 4 DESC LIMIT 12
"""},
    "monthly": {"label": "逐月觀測與空滿比例", "sql": """
SELECT month(o.bin) AS "月份", count(*) AS "觀測數", count(DISTINCT o.sid) AS "站點數",
       round(100.0 * sum(CASE WHEN o.bikes = 0 THEN 1 ELSE 0 END) / count(*), 2) AS "零車比例"
FROM observations o WHERE o.valid GROUP BY month(o.bin) ORDER BY 1
"""},
}
@app.get("/api/athena")
def api_athena(q: str = "district_empty"):
    if q not in ATHENA_QUERIES: return JSONResponse({"error": "unknown query", "available": list(ATHENA_QUERIES)}, 400)
    if q in ATHENA["cache"]: return ATHENA["cache"][q]
    try:
        import boto3
        at = boto3.Session(profile_name=os.environ.get("AWS_PROFILE", "hackathon")).client("athena", region_name="us-west-2")
        t0 = time.time()
        qid = at.start_query_execution(QueryString=ATHENA_QUERIES[q]["sql"],
                                       QueryExecutionContext={"Database": ATHENA["db"]},
                                       ResultConfiguration={"OutputLocation": f"s3://{ATHENA['bucket']}/athena-results/"})["QueryExecutionId"]
        for _ in range(90):
            ex = at.get_query_execution(QueryExecutionId=qid)["QueryExecution"]
            if ex["Status"]["State"] in ("SUCCEEDED", "FAILED", "CANCELLED"): break
            time.sleep(0.7)
        if ex["Status"]["State"] != "SUCCEEDED":
            return JSONResponse({"error": ex["Status"].get("StateChangeReason", "query failed")}, 502)
        rs = at.get_query_results(QueryExecutionId=qid, MaxResults=60)["ResultSet"]["Rows"]
        rows = [[c.get("VarCharValue") for c in r["Data"]] for r in rs]
        out = {"label": ATHENA_QUERIES[q]["label"], "query_id": qid, "columns": rows[0] if rows else [], "rows": rows[1:],
               "scanned_mb": round(ex["Statistics"]["DataScannedInBytes"] / 1e6, 1),
               "engine_ms": ex["Statistics"].get("EngineExecutionTimeInMillis"), "wall_s": round(time.time() - t0, 1),
               "sql": ATHENA_QUERIES[q]["sql"].strip(),
               "source": f"Amazon Athena 查詢 S3 資料湖 s3://{ATHENA['bucket']}/lake/（Glue Data Catalog: {ATHENA['db']}），未經本機預先彙總"}
        ATHENA["cache"][q] = out; return out
    except Exception as e:
        return JSONResponse({"error": f"{type(e).__name__}: {str(e)[:200]}"}, 502)

@app.get("/api/athena/list")
def api_athena_list(): return {"queries": [{"key": k, "label": v["label"]} for k, v in ATHENA_QUERIES.items()]}

@app.get("/api/aws_status")
def api_aws_status():
    return {"location": AWSLOC.STATUS, "bedrock": {**LLM.STATUS, "model_fast": LLM.MODEL_FAST, "model_smart": LLM.MODEL_SMART},
            "dynamodb": STATE["cloud"], "region": AWSLOC.REGION,
            "s3_bucket": "ntpc-youbike-hackathon-329899784315"}

@app.get("/api/weather")
def api_weather():
    w = WX.at(now_ts()); w["_factor"] = WX.ride_factor(w)
    return {"replay": w, "live": WX.live()}

def wx_now():
    w = WX.at(now_ts()); w["_factor"] = WX.ride_factor(w); return w

@app.get("/api/state")
def api_state():
    p = current_pred()
    return {"clock": {**STATE["clock"], "ts": iso(now_ts())}, "kpi": kpis(p), "assumptions": PL.ASSUMPTIONS, "user": STATE["user"], "scenario": STATE["scenario"],
            "llm": {**LLM.STATUS, "model_fast": LLM.MODEL_FAST, "model_smart": LLM.MODEL_SMART}, "cloud": STATE["cloud"], "focus_districts": FOCUS_DISTRICTS,
            "model_horizons": PRED.model_horizons, "model_origin": PRED.model_origin, "kpi_history": STATE["kpi_history"][-48:]}

@app.post("/api/clock")
async def api_clock(body: dict):
    c = STATE["clock"]; act = body.get("action")
    if act == "play": c["playing"] = True
    elif act == "pause": c["playing"] = False
    elif act == "step": c["t_idx"] = min(len(PRED.bins) - 7, c["t_idx"] + 1); await asyncio.get_event_loop().run_in_executor(None, on_tick)
    elif act == "set":
        c["t_idx"] = PRED.t_index(body["ts"]); STATE["last_plan_hour"] = None
        await asyncio.get_event_loop().run_in_executor(None, on_tick)
    if "speed" in body: c["speed"] = float(body["speed"])
    return {"clock": {**c, "ts": iso(now_ts())}}

@app.get("/api/stations")
def api_stations(district: str = None, adjusted: int = 1):
    p = current_pred(bool(adjusted))
    if district: p = p[p.district == district]
    cols = ["sid", "name", "district", "lat", "lon", "bikes", "spaces", "cap", "status", "pb_30", "ps_30", "pe_30", "pf_30", "pb_60", "ps_60", "pe_60", "pf_60", "pb_120", "ps_120", "pe_120", "pf_120", "pb_180", "ps_180", "pe_180", "pf_180", "flat_run"]
    out = p[cols].replace({np.nan: None}).to_dict(orient="records")
    return {"ts": iso(now_ts()), "source": p.source.iloc[0], "stations": out}

@app.get("/api/nav_route")
def api_nav_route(from_lat: float, from_lon: float, to_lat: float, to_lon: float, mode: str = "ride"):
    kmh = PL.ASSUMPTIONS["ride_kmh"] if mode == "ride" else PL.ASSUMPTIONS["walk_kmh"]
    f = wx_now().get("_factor", {}).get("time", 1.0) if mode == "ride" else 1.0
    straight = float(PL.hav(from_lat, from_lon, np.array([to_lat]), np.array([to_lon]))[0])
    a = AWSLOC.route((from_lat, from_lon), (to_lat, to_lon), mode)
    r = a if a else PL.osrm_steps((from_lat, from_lon), (to_lat, to_lon))
    if a: r["aws_minutes"] = round(a["aws_duration_s"] / 60, 1)
    limit = straight * 3.0 + 400
    d = r["dist_m"]
    if d is None or d <= 0 or d > limit:
        r["dist_m"] = round(straight * PL.ASSUMPTIONS["walk_detour"])
        r["geometry"] = [[from_lat, from_lon], [to_lat, to_lon]]
        r["steps"] = [{"text": "朝目的地前進", "dist_m": r["dist_m"], "road": "", "at": [from_lat, from_lon]},
                      {"text": "抵達目的地", "dist_m": 0, "road": "", "at": [to_lat, to_lon]}]
        r["source"] = "直線×1.3估計（路網服務不可用）" if d is None else "直線×1.3估計（路網結果不適用於此段，已捨棄）"
    elif not a: r["source"] = "OSRM 公開路網（備援，汽車路網）"
    r["minutes"] = round(r["dist_m"] / 1000 / kmh * 60 * f, 1); r["mode"] = mode
    return r

@app.get("/api/nearby")
def api_nearby(lat: float, lon: float, r: int = 800):
    p = apply_dispatch_to_pred(apply_event_to_pred(current_pred()))
    res = PL.nearby(p, (lat, lon), r)
    ev = STATE["scenario"].get("event")
    res["events"] = []
    if ev:
        dm = float(PL.hav(lat, lon, np.array([ev["lat"]]), np.array([ev["lon"]]))[0])
        if dm <= 2500:
            start, end = pd.Timestamp(ev["start"]), pd.Timestamp(ev["end"]); n = now_ts()
            phase = ("散場前後" if end - pd.Timedelta(minutes=60) <= n <= end + pd.Timedelta(minutes=90)
                     else "入場前" if start - pd.Timedelta(minutes=90) <= n <= start else "活動期間" if start <= n <= end else "稍後")
            res["events"].append({"title": ev["title"], "venue": ev["venue"], "lat": ev["lat"], "lon": ev["lon"], "dist_m": int(dm),
                                  "start": ev["start"], "end": ev["end"], "phase": phase, "riders_out": ev["riders_out"],
                                  "affected_stations": len(ev["stations"]), "source": ev["source"],
                                  "hint": f"{pd.Timestamp(ev['end']).strftime('%H:%M')} 散場，場館周邊 {len(ev['stations'])} 站會很難借車" if phase in ("散場前後", "活動期間") else f"{pd.Timestamp(ev['start']).strftime('%H:%M')} 入場，場館周邊車位會變少"})
    res["ts"] = iso(now_ts()); res["weather"] = wx_now()
    return res

@app.get("/api/station/{sid}")
def api_station(sid: int):
    p = current_pred(); raw = current_pred(False)
    r = p[p.sid == sid].iloc[0]; r0 = raw[raw.sid == sid].iloc[0]
    nb = PRED.neighbors(sid); nbd = p[p.sid.isin(nb)].copy()
    nbd["dist_m"] = PL.hav(r.lat, r.lon, nbd.lat.values, nbd.lon.values).round()
    nbd["walk_min"] = (nbd["dist_m"] * PL.ASSUMPTIONS["walk_detour"] / 1000 / PL.ASSUMPTIONS["walk_kmh"] * 60).round(1)
    t = STATE["clock"]["t_idx"]; hist_idx = list(range(max(0, t - 12), t + 1))
    hist = [{"ts": iso(PRED.bins[i]), "bikes": None if np.isnan(PRED.ctx.B[i, sid]) else int(PRED.ctx.B[i, sid]), "spaces": None if np.isnan(PRED.ctx.S[i, sid]) else int(PRED.ctx.S[i, sid])} for i in hist_idx]
    ints = [i for i in STATE["intents"] if i["status"] == "active" and (i["borrow_sid"] == sid or i["return_sid"] == sid)]
    return {"station": r.replace({np.nan: None}).to_dict(), "raw_forecast": {k: (None if pd.isna(r0[k]) else float(r0[k])) for k in ["pb_60", "pb_120", "pb_180", "ps_60", "ps_120", "ps_180"]},
            "adjusted_forecast": {k: (None if pd.isna(r[k]) else float(r[k])) for k in ["pb_60", "pb_120", "pb_180", "ps_60", "ps_120", "ps_180"]},
            "intents_here": len(ints), "expected_diverted": round(len(ints) * PL.ASSUMPTIONS["intent_conversion"], 1),
            "neighbors": nbd[["sid", "name", "dist_m", "walk_min", "bikes", "spaces", "cap", "status", "pe_60", "pf_60", "pb_60", "ps_60"]].replace({np.nan: None}).sort_values("dist_m").to_dict(orient="records"),
            "history": hist, "alerts": [a for a in STATE["alerts"].values() if a["sid"] == sid and a["status"] != "resolved"]}

@app.get("/api/alerts")
def api_alerts(all: int = 0):
    items = sorted(STATE["alerts"].values(), key=lambda a: (a["status"] == "resolved", -SEV[a["severity"]], a["opened"]), reverse=False)
    if not all: items = [a for a in items if a["status"] != "resolved"]
    return {"alerts": items[:200], "log_count": len(STATE["alert_log"])}

@app.post("/api/alerts/{aid}/{action}")
def api_alert_action(aid: str, action: str, body: dict = None):
    for a in STATE["alerts"].values():
        if a["id"] == aid:
            if action == "ack": a["status"] = "acked"; a["acked"] = iso(now_ts())
            elif action == "resolve": a["status"] = "resolved"; a["resolved"] = iso(now_ts()); a["resolve_reason"] = (body or {}).get("reason", "人工結案")
            elif action == "dispatch":
                a["status"] = "acked"; a["acked"] = a["acked"] or iso(now_ts()); a["route"] = "調度端"
                notify("ops", f"政府端轉派｜{a['station']}", a["message"], "warn", {"alert_id": aid, "sid": a["sid"]})
            ddb_put("yb_alerts", a); broadcast("alert", a); return a
    return JSONResponse({"error": "not found"}, 404)

@app.get("/api/tasks")
def api_tasks():
    return {"tasks": sorted(STATE["tasks"], key=lambda t: t.get("created", ""), reverse=True), "assumptions": PL.ASSUMPTIONS}

@app.post("/api/tasks/{tid}/{action}")
def api_task_action(tid: str, action: str, body: dict = None):
    for tk in STATE["tasks"]:
        if tk["id"] == tid:
            if action == "confirm": tk["confirmed_by"] = "調度員（demo）"; tk["history"].append({"ts": iso(now_ts()), "status": tk["status"], "note": "調度員確認"})
            elif action == "dispatch_now": tk["status"] = "dispatched"; tk["depart_by"] = str(now_ts()); tk["history"].append({"ts": iso(now_ts()), "status": "dispatched", "note": "調度員手動立即出車（模擬）"})
            elif action == "cancel": tk["status"] = "cancelled"; tk["history"].append({"ts": iso(now_ts()), "status": "cancelled", "note": (body or {}).get("reason", "調度員取消")})
            ddb_put("yb_tasks", tk); broadcast("task", tk); return tk
    return JSONResponse({"error": "not found"}, 404)

@app.get("/api/tasks/{tid}/route")
def api_task_route(tid: str):
    for tk in STATE["tasks"]:
        if tk["id"] == tid:
            pts = [tuple(tk["depot"])] + [(s["lat"], s["lon"]) for s in tk["stops"] if s["sid"] != -1]
            a = AWSLOC.route_multi(pts, "drive") if len(pts) >= 2 else None
            if a: return {"geometry": a["geometry"], "dist_m": a["dist_m"], "aws_minutes": round(a["aws_duration_s"] / 60), "source": a["source"]}
            rt = PL.osrm(pts) if len(pts) >= 2 else None
            if rt: return {"geometry": rt["geometry"], "dist_m": rt["dist_m"], "source": "OSRM 公開路網（備援，汽車）"}
            return {"geometry": [list(p) for p in pts], "dist_m": None, "source": "直線連接（路網服務不可用）"}
    return JSONResponse({"error": "not found"}, 404)

@app.get("/api/briefing")
def api_briefing(scope: str = "ops", task_id: str = None):
    p = current_pred(); k = kpis(p)
    if scope == "ops" and task_id:
        tk = next((t for t in STATE["tasks"] if t["id"] == task_id), None)
        if not tk: return JSONResponse({"error": "not found"}, 404)
        stops = "；".join(f"{s['name']}{'取' if s['action']=='pickup' else '送'}{s['qty']}輛(現{s['now_bikes']}輛,預測{s['pred_bikes']})" for s in tk["stops"])
        fb = f"{tk['district']}任務{tk['id']}：{len(tk['stops'])} 站、載 {tk['load']} 輛、路程約 {tk['route_minutes']} 分，最遲 {tk['depart_by'][11:16]} 出發，目標 {tk['target_ts'][11:16]}。{tk['reason']}"
        prompt = f"你是調度員的簡報助理。用 2–3 句話說明這趟任務的情況與注意事項，只用以下事實：目前時間 {k['ts']}；{fb}；站點：{stops}；載量上限 {PL.ASSUMPTIONS['truck_capacity']} 輛為情境假設。"
        return LLM.generate(prompt, fb, cache_key=f"ops:{task_id}:{tk['status']}")
    if scope == "gov":
        opens = [a for a in STATE["alerts"].values() if a["status"] != "resolved"]
        top = "；".join(f"{a['station']}({a['severity']},{a['type']})" for a in sorted(opens, key=lambda a: -SEV[a["severity"]])[:8])
        fb = f"{k['ts']}：目前空站 {k['empty_now']}、滿站 {k['full_now']}、待查 {k['both_zero']+k['stale']}；120 分鐘內高風險缺車 {k['risk_empty_120']} 站、缺位 {k['risk_full_120']} 站；開啟告警 {k['open_alerts']} 則（高 {k['high_alerts']}）；已排程 {k['tasks_planned']} 趟；民眾意向 {k['intents_active']} 筆，預期分流 {k['expected_diverted']} 車次。"
        prompt = f"你是交通局主管的簡報助理。用 3 句話摘要現況與建議關注點，只用這些事實，不新增數字：{fb} 主要告警：{top or '無'}。"
        return LLM.generate(prompt, fb, cache_key=f"gov:{k['ts']}:{k['open_alerts']}")
    return {"text": "", "source": "none"}

def apply_event_to_pred(p):
    """活動情境：只調整場館 600 m 內站點的預測欄位，供民眾端與告警使用（情境參數，非實到人數）。"""
    ev = STATE["scenario"].get("event")
    if not ev: return p
    p = p.copy(); start, end = pd.Timestamp(ev["start"]), pd.Timestamp(ev["end"])
    for h in (30, 60, 120, 180):
        tgt = now_ts() + pd.Timedelta(minutes=h)
        if end <= tgt <= end + pd.Timedelta(minutes=75):
            for sid, share in ev["station_share"].items():
                i = p.index[p.sid == int(sid)]
                p.loc[i, f"pb_{h}"] = np.maximum(0, p.loc[i, f"pb_{h}"] - ev["riders_out"] * share); p.loc[i, f"pe_{h}"] = np.maximum(p.loc[i, f"pe_{h}"], 0.75)
        elif start - pd.Timedelta(minutes=75) <= tgt <= start:
            for sid, share in ev["station_share"].items():
                i = p.index[p.sid == int(sid)]
                p.loc[i, f"ps_{h}"] = np.maximum(0, p.loc[i, f"ps_{h}"] - ev["riders_in"] * share); p.loc[i, f"pf_{h}"] = np.maximum(p.loc[i, f"pf_{h}"], 0.6)
    return p

def apply_dispatch_to_pred(p):
    """已排定／出車中的送車量加到目標時間前的預測（標示為含排程補車；派車執行本身為模擬）。"""
    p = p.copy()
    for tk in STATE["tasks"]:
        if tk["status"] not in ("planned", "dispatched", "en_route"): continue
        for s_ in tk["stops"]:
            if s_["action"] != "dropoff" or s_.get("done"): continue
            for h in (30, 60, 120, 180):
                if pd.Timestamp(tk["target_ts"]) <= now_ts() + pd.Timedelta(minutes=h):
                    i = p.index[p.sid == s_["sid"]]
                    p.loc[i, f"pb_{h}"] = np.minimum(p.loc[i, "cap"], p.loc[i, f"pb_{h}"] + s_["qty"]); p.loc[i, f"pe_{h}"] = p.loc[i, f"pe_{h}"] * (0.4 if s_["qty"] >= 5 else 0.7)
    return p

@app.post("/api/plan")
def api_plan(body: dict):
    u = STATE["user"]
    origin = tuple(body.get("origin") or u["home"]); dest = tuple(body.get("dest") or u["work"])
    depart = pd.Timestamp(body.get("depart_ts") or now_ts())
    w = wx_now(); pref = body.get("preference") or STATE["profile"]["preference"]
    res = PL.plan_trip(apply_dispatch_to_pred(apply_event_to_pred(current_pred())), origin, dest, depart, now_ts(),
                       int(body.get("max_walk_min") or STATE["profile"]["max_walk_min"]), weather=w, preference=pref)
    res["weather"] = w
    res["includes"] = ["模型預測", "已登記意向×0.7"] + (["雨天時間與風險加成"] if w.get("is_raining") else []) + (["活動情境需求"] if STATE["scenario"].get("event") else []) + (["已排程補車（執行為模擬）"] if any(tk["status"] in ("planned", "dispatched", "en_route") for tk in STATE["tasks"]) else [])
    res["ts"] = iso(now_ts()); res["depart_ts"] = iso(depart); res["origin"] = origin; res["dest"] = dest
    if res.get("options"):
        eid = uuid.uuid4().hex[:8]; res["explain_id"] = eid
        facts = "；".join(f"{o['label']}：{o['borrow']['name']}借(現{o['borrow']['bikes_now']}輛,到時預估{o['borrow']['expected_bikes']}輛,零車機率{o['borrow']['p_empty']:.0%})→{o['return']['name']}還(到時預估{o['return']['expected_spaces']}格,零位機率{o['return']['p_full']:.0%})，全程{o['total_min']}分，集點{o['points']}" for o in res["options"])
        prompt = f"為每個方案各寫一句 25 字內的推薦理由（口語、直接），回傳 JSON 陣列，順序與輸入相同，只用這些事實：{facts}"
        fallback = json.dumps([f"{o['label']}：全程 {o['total_min']} 分，借車零車機率 {o['borrow']['p_empty']:.0%}、還車零位機率 {o['return']['p_full']:.0%}" for o in res["options"]], ensure_ascii=False)
        STATE["explain"][eid] = LLM.generate_async(prompt, fallback, max_tokens=300)
        for o in res["options"]: o["reason"] = f"全程 {o['total_min']} 分；借車零車機率 {o['borrow']['p_empty']:.0%}、還車零位機率 {o['return']['p_full']:.0%}" + (f"；順路集點 +{o['points']}" if o["points"] else "")
    return res

@app.get("/api/explain/{eid}")
def api_explain(eid: str):
    fut = STATE["explain"].get(eid)
    if not fut: return {"ready": False}
    if not fut.done(): return {"ready": False}
    r = fut.result(); txt = r["text"]
    try:
        js = txt[txt.index("["): txt.rindex("]") + 1]; reasons = json.loads(js)
    except Exception: reasons = None
    return {"ready": True, "reasons": reasons, "source": r["source"], "model": r.get("model"), "ms": r.get("ms")}

@app.post("/api/intents")
def api_intent(body: dict):
    o = body["option"]; now = now_ts()
    it = {"id": uuid.uuid4().hex[:8], "kind": o["kind"], "borrow_sid": o["borrow"]["sid"], "return_sid": o["return"]["sid"],
          "borrow_name": o["borrow"]["name"], "return_name": o["return"]["name"],
          "borrow_ts": str(pd.Timestamp(body.get("depart_ts") or now) + pd.Timedelta(minutes=o["borrow"]["arrive_in_min"] if body.get("depart_ts") is None else o["legs"][0]["minutes"])),
          "return_ts": str(pd.Timestamp(body.get("depart_ts") or now) + pd.Timedelta(minutes=o["return"]["arrive_in_min"] if body.get("depart_ts") is None else o["legs"][0]["minutes"] + o["legs"][1]["minutes"])),
          "points": o.get("points", 0), "status": "active", "created": iso(now)}
    STATE["intents"].append(it); ddb_put("yb_intents", it)
    PRED._cache.clear()
    notify("citizen", "已登記借還意向", f"{it['borrow_name']} → {it['return_name']}。這是意向配額，不是車位預約；取消或改路線請釋放名額。", "intent", {"intent_id": it["id"]})
    broadcast("intent", it); return it

@app.post("/api/intents/{iid}/{action}")
def api_intent_action(iid: str, action: str):
    for it in STATE["intents"]:
        if it["id"] == iid:
            if action == "cancel": it["status"] = "cancelled"; notify("citizen", "已釋放意向名額", f"{it['borrow_name']} → {it['return_name']} 已取消", "intent")
            elif action == "complete":
                it["status"] = "completed"; it["completed"] = iso(now_ts())
                pts = it.get("points", 0)
                p = current_pred(False); r = p[p.sid == it["return_sid"]].iloc[0]
                stamp = {"name": f"{r.district}生活圈章", "type": "district"}
                if "藝文" in it["return_name"] or "體育館" in it["return_name"] or "棒球場" in it["return_name"]: stamp = {"name": f"{it['return_name']}限定章", "type": "landmark"}
                if STATE["scenario"].get("event"): stamp = {"name": f"活動限定章：{STATE['scenario']['event']['title']}", "type": "event"}
                grant_reward(pts + 5, f"完成「{ {'fast':'最快抵達','reliable':'借還較穩','reward':'順路集點'}.get(it['kind'], it['kind']) }」借還（模擬完成事件，未經 GPS/BLE 驗證）", stamp=stamp)
                notify("citizen", "集章成功", f"獲得「{stamp['name']}」+{pts+5} 點。完成事件為模擬，正式需由借還交易驗證。", "reward")
            ddb_put("yb_intents", it); broadcast("intent", it); return it
    return JSONResponse({"error": "not found"}, 404)

@app.get("/api/intents")
def api_intents(): return {"intents": STATE["intents"]}

@app.get("/api/rewards")
def api_rewards(): return STATE["rewards"]

@app.get("/api/notifications")
def api_notifications(channel: str = "citizen"): return {"items": STATE["notifications"].get(channel, [])}

@app.get("/api/tickets")
def api_tickets(): return {"tickets": STATE["tickets"], "flow": [{"key": k, "label": TICKET_LABEL[k]} for k in TICKET_FLOW]}

@app.post("/api/tickets")
def api_ticket_create(body: dict):
    sid = int(body["sid"]); issue = body.get("issue", "其他"); name = ST.loc[ST.sid == sid, "name"].iloc[0]
    for tk in STATE["tickets"]:
        if tk["sid"] == sid and tk["issue"] == issue and tk["status"] not in ("closed",) and (now_ts() - pd.Timestamp(tk["ts"])).total_seconds() < 7200:
            tk["reports"] += 1; tk["history"].append({"ts": iso(now_ts()), "status": tk["status"], "label": f"重複回報合併（第 {tk['reports']} 次）"}); broadcast("ticket", tk)
            notify("citizen", "回報已合併", f"{name} 的同類回報已在處理中，感謝補充。", "ticket"); return tk
    tk = {"id": f"R{len(STATE['tickets'])+1:03d}", "sid": sid, "station": name, "bike_no": body.get("bike_no", ""), "issue": issue, "note": body.get("note", ""), "reports": 1,
          "ts": str(now_ts()), "status": "reported", "history": [{"ts": iso(now_ts()), "status": "reported", "label": TICKET_LABEL["reported"]}], "assignee": "授權店家 A（模擬）"}
    STATE["tickets"].insert(0, tk); ddb_put("yb_tickets", tk); broadcast("ticket", tk)
    notify("citizen", "報修已送出", f"{name}｜{issue}。請勿騎乘故障車送修；處理進度會在此更新。", "ticket")
    notify("gov", f"民眾報修｜{name}", f"{issue}（車號 {body.get('bike_no','未填')}），已轉授權店家（模擬）", "ticket")
    notify("ops", f"民眾報修｜{name}", f"{issue}（車號 {body.get('bike_no','未填')}）。工單 {tk['id']} 已建立，待授權店家接單（模擬）", "ticket", {"ticket_id": tk["id"], "sid": sid})
    return tk


# ------------------------------------------------------------------ 民眾端：地點選單、檔案、存摺、行程
PLACE_GROUPS = {
    "home": ["板橋四維公園", "松柏街50巷90弄口", "江翠國小", "四維公園地下停車場", "永寧街33巷口", "中平中榮街口", "頭前運動公園"],
    "work": ["捷運江子翠站(4號出口)", "捷運府中站(1號出口)", "新北市政府", "捷運新埔站(1號出口)", "捷運板橋站(3號出口)", "捷運幸福站", "捷運永寧站(4號出口)"],
    "school": ["江翠國中", "土城國中", "福和國中", "大觀國中", "明志國中", "臺北大學公共事務大樓(法商大道)"],
}
@app.get("/api/places")
def api_places():
    out = {}
    for k, names in PLACE_GROUPS.items():
        items = []
        for n in names:
            m = ST.index[ST["name"] == n]
            if len(m):
                r = ST.loc[m[0]]
                items.append({"sid": int(r.sid), "name": n, "district": r.district, "lat": float(r.lat), "lon": float(r.lon), "cap": int(r.cap_mode)})
        out[k] = items
    return out

def place_of(sid, offset=(0.0, 0.0)):
    r = ST.loc[ST.sid == sid].iloc[0]
    return (float(r.lat) + offset[0], float(r.lon) + offset[1])

@app.get("/api/profile")
def api_profile_get():
    pr = dict(STATE["profile"])
    for k, sk in (("home", "home_sid"), ("work", "work_sid")):
        if pr.get(sk) is not None:
            r = ST.loc[ST.sid == pr[sk]].iloc[0]
            pr[k] = {"sid": int(r.sid), "name": r["name"], "district": r.district, "lat": float(r.lat), "lon": float(r.lon)}
    pr["pref_prompt"] = STATE["pref_prompt"]
    pr["home_pos"] = list(STATE["user"]["home"]) if STATE["user"]["home"] else None     # 實際住處／目的地座標（含偏移）
    pr["work_pos"] = list(STATE["user"]["work"]) if STATE["user"]["work"] else None
    return pr

@app.post("/api/profile")
def api_profile_set(body: dict):
    pr = STATE["profile"]
    for k in ("nickname", "role", "home_sid", "work_sid", "out_time", "back_time", "join_rewards", "preference", "max_walk_min", "onboarded"):
        if k in body: pr[k] = body[k]
    if pr["home_sid"] is not None and pr["work_sid"] is not None:
        STATE["user"]["home"] = place_of(pr["home_sid"], (0.0022, 0.0016))   # 家＝站點附近的住處，非站點本身
        STATE["user"]["work"] = place_of(pr["work_sid"], (0.0011, 0.0009))   # 目的地在站點附近，不等於站點本身
        STATE["user"]["commute_out"] = pr["out_time"]; STATE["user"]["commute_back"] = pr["back_time"]
        STATE["user"]["student"] = pr["role"] == "student"; STATE["user"]["max_walk_min"] = pr["max_walk_min"]
    STATE["pref_prompt"] = None
    broadcast("profile", api_profile_get())
    return api_profile_get()

def wallet_data():
    A = PL.ASSUMPTIONS; n = now_ts()
    month = n.strftime("%Y-%m")
    seed = [{"date": d, "km": k, "sim": True} for d, k in SEED_TRIPS if d.startswith(month) and d <= str(n.date())]
    real = [{"date": t["date"], "km": t["km"], "sim": False, "points": t.get("points", 0)} for t in STATE["trip_log"]]
    trips = seed + real
    km = sum(t["km"] for t in trips)
    co2_kg = km * A["co2_scooter_g_per_km"] / 1000.0
    def taxi(k):
        return A["taxi_base_fare"] + max(0.0, k - A["taxi_base_km"]) * A["taxi_per_km"]
    saved = sum(taxi(t["km"]) for t in trips)      # YouBike 一般車前 30 分免費，估為 0 元
    days = sorted({t["date"] for t in trips})
    streak = 0; cur = pd.Timestamp(n.date())
    if cur.strftime("%Y-%m-%d") not in days: cur -= pd.Timedelta(days=1)   # 今天尚未騎，從昨天起算
    while True:
        d = cur.strftime("%Y-%m-%d")
        if d in days: streak += 1; cur -= pd.Timedelta(days=1)
        elif cur.weekday() >= 5: cur -= pd.Timedelta(days=1)   # 週末不中斷
        else: break
        if streak > 60: break
    R = STATE["rewards"]
    miles = [{"key": "km50", "label": "累積 50 公里", "now": km, "goal": 50, "icon": "🚲"},
             {"key": "co2_5", "label": "減碳 5 公斤", "now": co2_kg, "goal": 5, "icon": "🌱"},
             {"key": "trip20", "label": "本月 20 趟", "now": len(trips), "goal": 20, "icon": "📅"},
             {"key": "pt100", "label": "累積 100 點", "now": R["points"], "goal": 100, "icon": "🪙"}]
    for m in miles: m["pct"] = min(100, round(m["now"] / m["goal"] * 100))
    return {"month": month, "trips": len(trips), "sim_trips": len(seed), "real_trips": len(real),
            "km": round(km, 1), "co2_kg": round(co2_kg, 2), "saved_ntd": int(round(saved)), "streak_days": streak,
            "points": R["points"], "seed_points": R.get("seed_points", 0), "demo_seed": R.get("demo_seed", False),
            "stamps": R["stamps"], "coupons": R["coupons"], "history": R["history"][:10],
            "milestones": miles, "by_day": [{"date": d, "km": round(sum(t["km"] for t in trips if t["date"] == d), 1)} for d in days],
            "assumptions": {"co2_對照": f"騎機車 {A['co2_scooter_g_per_km']} 公克 CO2e／公里（公開常見估計值，正式數字須引用環境部公告）",
                             "省錢_對照": f"計程車起跳 {A['taxi_base_fare']} 元含 {A['taxi_base_km']} 公里，續程每公里 {A['taxi_per_km']} 元（假設費率）；YouBike 一般車前 {A['youbike_free_min']} 分鐘以 0 元計",
                             "資料來源": f"本月 {len(seed)} 趟為示範帳戶模擬紀錄，{len(real)} 趟為本次操作實際完成"}}

@app.get("/api/wallet")
def api_wallet(): return wallet_data()

# ---- 行程狀態機（導航）----
@app.get("/api/trip")
def api_trip_get(): return STATE["trip"] or {"active": False}

@app.post("/api/trip/start")
def api_trip_start(body: dict):
    o = body["option"]; n = now_ts()
    opts_all = body.get("all_labels") or []
    if not o.get("primary") and opts_all:
        STATE["choice_log"].append(o["kind"])
        last = STATE["choice_log"][-2:]
        if len(last) == 2 and last[0] == last[1] and last[0] != STATE["profile"]["preference"]:
            STATE["pref_prompt"] = {"kind": last[0], "label": {"fast": "準時抵達", "reliable": "一定借得到", "reward": "多集點"}.get(last[0], last[0])}
    trip = {"active": True, "id": uuid.uuid4().hex[:8], "kind": o["kind"], "started": iso(n), "phase": "walk_to_borrow",
            "option": o, "bike_no": f"YB2-{np.random.randint(10000, 99999)}", "report": None,
            "km": round(o["legs"][1]["dist_m"] / 1000, 2), "points": o.get("points", 0)}
    STATE["trip"] = trip; broadcast("trip", trip)
    it = {"id": uuid.uuid4().hex[:8], "kind": o["kind"], "borrow_sid": o["borrow"]["sid"], "return_sid": o["return"]["sid"],
          "borrow_name": o["borrow"]["name"], "return_name": o["return"]["name"],
          "borrow_ts": str(n + pd.Timedelta(minutes=o["legs"][0]["minutes"])),
          "return_ts": str(n + pd.Timedelta(minutes=o["legs"][0]["minutes"] + o["legs"][1]["minutes"])),
          "points": o.get("points", 0), "status": "active", "created": iso(n), "trip_id": trip["id"]}
    STATE["intents"].append(it); ddb_put("yb_intents", it); PRED._cache.clear(); broadcast("intent", it)
    trip["intent_id"] = it["id"]
    return trip

@app.post("/api/trip/phase")
def api_trip_phase(body: dict):
    t = STATE["trip"]
    if not t: return JSONResponse({"error": "no trip"}, 404)
    t["phase"] = body["phase"]
    if body["phase"] == "riding": t["borrowed_at"] = iso(now_ts())
    broadcast("trip", t); return t

@app.post("/api/trip/finish")
def api_trip_finish(body: dict = None):
    t = STATE["trip"]
    if not t: return JSONResponse({"error": "no trip"}, 404)
    body = body or {}; n = now_ts()
    t["phase"] = "done"; t["finished"] = iso(n); t["active"] = False
    for it in STATE["intents"]:
        if it.get("trip_id") == t["id"]: it["status"] = "completed"; it["completed"] = iso(n); ddb_put("yb_intents", it)
    STATE["trip_log"].append({"date": str(n.date()), "km": t["km"], "points": t["points"], "kind": t["kind"]})
    r = current_pred(False); row = r[r.sid == t["option"]["return"]["sid"]].iloc[0]
    stamp = {"name": f"{row.district}生活圈章", "type": "district"}
    if STATE["scenario"].get("event"): stamp = {"name": f"活動限定章：{STATE['scenario']['event']['title']}", "type": "event"}
    elif STATE["profile"]["role"] == "student" and t["kind"] == "reward": stamp = {"name": "放學接力章", "type": "relay"}
    base = 5 + t["points"]
    grant_reward(base, f"完成一趟 {t['km']} 公里（模擬完成事件，未經借還交易驗證）", stamp=stamp)
    notify("citizen", "還車完成", f"本趟 {t['km']} 公里，獲得「{stamp['name']}」與 {base} 點。", "reward")
    broadcast("trip", t); broadcast("wallet", wallet_data())
    STATE["trip"] = None
    return {"ok": True, "points": base, "stamp": stamp, "wallet": wallet_data()}

ISSUES = [{"key": "brake", "icon": "🛑", "label": "煞車異常"}, {"key": "tire", "icon": "🛞", "label": "輪胎沒氣"},
          {"key": "chain", "icon": "⛓️", "label": "鏈條掉了"}, {"key": "seat", "icon": "🪑", "label": "坐墊或把手"},
          {"key": "lock", "icon": "🔒", "label": "無法借還／上鎖"}, {"key": "battery", "icon": "🔋", "label": "電輔車無電"},
          {"key": "light", "icon": "💡", "label": "車燈故障"}, {"key": "other", "icon": "❓", "label": "其他"}]
@app.get("/api/issues")
def api_issues(): return {"issues": ISSUES}

@app.post("/api/user")
def api_user(body: dict):
    STATE["user"].update({k: v for k, v in body.items() if k in STATE["user"]}); return STATE["user"]

# ------------------------------------------------------------------ 情境
def event_scenario(title, venue_name, start, end, riders_out=150, riders_in=90, radius_m=600):
    vs = sid_by_name(venue_name); vlat, vlon = coord(vs)
    d = PL.hav(vlat, vlon, ST.lat.values, ST.lon.values)
    near = ST[(d <= radius_m)].copy(); near["w"] = near["cap_mode"].astype(float); near["w"] /= near["w"].sum()
    return {"title": title, "venue": venue_name, "lat": vlat, "lon": vlon, "start": start, "end": end, "riders_out": riders_out, "riders_in": riders_in,
            "station_share": {int(r.sid): round(float(r.w), 3) for r in near.itertuples()}, "stations": near["name"].tolist(),
            "source": "情境參數（無出席人數資料，不代表實到人數）", "data_date": None}

SCENARIOS = {
    "commute_am": {"label": "通勤早峰｜江子翠", "ts": "2026-06-16 07:20", "home": "板橋四維公園", "work": "捷運江子翠站(4號出口)", "home_offset": (0.0006, 0.0004), "student": False, "event": None},
    "commute_pm": {"label": "通勤晚峰｜江子翠", "ts": "2026-06-16 17:50", "home": "板橋四維公園", "work": "捷運江子翠站(4號出口)", "home_offset": (0.0006, 0.0004), "student": False, "event": None},
    "concert": {"label": "演唱會散場｜新莊體育館", "ts": "2026-06-16 18:00", "home": "捷運幸福站", "work": "新莊體育館", "home_offset": (0.0, 0.0), "student": False,
                "event": ("情境：新莊體育館演唱會", "新莊體育館", "2026-06-16 19:30", "2026-06-16 21:30")},
    "student": {"label": "放學接力｜土城國中", "ts": "2026-06-16 15:00", "home": "捷運永寧站(4號出口)", "work": "土城國中", "home_offset": (0.0004, 0.0003), "student": True, "event": None},
    "repair": {"label": "故障報修｜江子翠", "ts": "2026-06-16 12:00", "home": "板橋四維公園", "work": "捷運江子翠站(4號出口)", "home_offset": (0.0006, 0.0004), "student": False, "event": None},
}

@app.get("/api/scenarios")
def api_scenarios(): return {k: {"label": v["label"], "ts": v["ts"]} for k, v in SCENARIOS.items()}

def set_scenario_sync(name, body=None):
    sc = SCENARIOS[name]; body = body or {}
    hs = sid_by_name(sc["home"]); ws = sid_by_name(sc["work"])
    hl, hn = coord(hs); wl, wn = coord(ws)
    STATE["user"].update({"home": (hl + sc["home_offset"][0], hn + sc["home_offset"][1]), "work": (wl, wn), "student": sc["student"],
                          "commute_out": "07:40" if name != "student" else "07:30", "commute_back": "18:10" if name != "student" else "15:45"})
    STATE["scenario"] = {"name": name, "label": sc["label"], "event": event_scenario(*sc["event"], riders_out=int(body.get("riders_out", 150)), riders_in=int(body.get("riders_in", 90))) if sc["event"] else None}
    STATE["clock"]["t_idx"] = PRED.t_index(sc["ts"]); STATE["clock"]["playing"] = False; STATE["last_plan_hour"] = None; STATE["last_reminder"] = {}
    # 時間跳躍：清除舊排程與告警（保留意向、工單、獎勵）
    STATE["tasks"].clear(); STATE["alerts"].clear(); STATE["notifications"]["ops"].clear(); STATE["notifications"]["gov"].clear(); PRED._cache.clear()
    on_tick()
    broadcast("scenario", STATE["scenario"])
    return {"scenario": STATE["scenario"], "user": STATE["user"], "clock": {**STATE["clock"], "ts": iso(now_ts())}}

@app.post("/api/scenario/{name}")
async def api_scenario(name: str, body: dict = None):
    return await asyncio.get_event_loop().run_in_executor(None, set_scenario_sync, name, body or {})

@app.post("/api/reset")
def api_reset():
    STATE["intents"].clear(); STATE["tickets"].clear(); STATE["alerts"].clear(); STATE["alert_log"].clear(); STATE["tasks"].clear(); STATE["task_seq"] = 0
    for c in STATE["notifications"]: STATE["notifications"][c].clear()
    STATE["rewards"] = {"points": 0, "seed_points": 0, "demo_seed": False, "stamps": [], "coupons": [], "history": []}; STATE["last_plan_hour"] = None; STATE["last_reminder"] = {}; PRED._cache.clear()
    return {"ok": True}

# ------------------------------------------------------------------ 外部資料：即時 API、藝文活動、模型報告
_live = {"ts": 0, "data": None}
@app.get("/api/live")
def api_live():
    if time.time() - _live["ts"] < 60 and _live["data"]: return _live["data"]
    try:
        rows = []; page = 0
        while page < 6:
            js = PL.http_get_json(f"https://data.ntpc.gov.tw/api/datasets/010e5b15-3823-4b20-b401-b1cf000550c5/json?page={page}&size=1000", timeout=20) or []
            rows += js
            if len(js) < 1000: break
            page += 1
        key = (ST["district"] + "|" + ST["name"]).tolist(); kidx = {k: i for i, k in enumerate(key)}
        out = []
        for x in rows:
            nm = x["sna"].replace("YouBike2.0_", ""); k = f"{x['sarea']}|{nm}"
            out.append({"sno": x["sno"], "name": nm, "district": x["sarea"], "lat": float(x["lat"]), "lon": float(x["lng"]), "tot": int(x["tot_quantity"]), "sbi": int(x["sbi_quantity"]), "bemp": int(x["bemp"]),
                        "act": x["act"], "yb2": int(x.get("yb2_quantity") or 0), "eyb": int(x.get("eyb_quantity") or 0), "mday": x["mday"], "matched_sid": kidx.get(k)})
        data = {"fetched_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "count": len(out), "matched": sum(1 for o in out if o["matched_sid"] is not None),
                "empty": sum(1 for o in out if o["sbi"] == 0 and o["act"] == "1"), "full": sum(1 for o in out if o["bemp"] == 0 and o["act"] == "1"), "inactive": sum(1 for o in out if o["act"] != "1"), "stations": out,
                "source": "新北市政府資料開放平台 YouBike2.0 即時資料（data.gov.tw/dataset/146969）"}
        _live.update({"ts": time.time(), "data": data}); return data
    except Exception as e:
        return JSONResponse({"error": f"{type(e).__name__}: {str(e)[:120]}", "cached": _live["data"] is not None}, 502)

_cul = {"ts": 0, "data": None}
@app.get("/api/culture_events")
def api_culture():
    if time.time() - _cul["ts"] < 1800 and _cul["data"]: return _cul["data"]
    NT = ["新北", "新莊", "板橋", "三重", "中和", "永和", "土城", "新店", "汐止", "淡水", "林口", "蘆洲", "樹林", "三峽", "鶯歌", "泰山", "五股", "八里", "深坑"]
    items = []; errors = []
    for cat, cname in [("17", "演唱會"), ("1", "音樂"), ("2", "戲劇"), ("3", "舞蹈")]:
        for attempt in range(2):
            try:
                js = PL.http_get_json(f"https://cloud.culture.tw/frontsite/trans/SearchShowAction.do?method=doFindTypeJ&category={cat}", timeout=12)
                if js is None: raise RuntimeError("fetch failed")
                for ev in js:
                    for s in ev.get("showInfo", []):
                        loc = (s.get("location") or "") + (s.get("locationName") or "")
                        if any(k in loc for k in NT):
                            lat, lng = s.get("latitude"), s.get("longitude")
                            near = None
                            if lat and lng:
                                d = PL.hav(float(lat), float(lng), ST.lat.values, ST.lon.values); idx = np.argsort(d)[:3]
                                near = [{"sid": int(ST.sid.iloc[i]), "name": ST.name.iloc[i], "dist_m": int(d[i])} for i in idx if d[i] <= 600]
                            items.append({"category": cname, "title": ev["title"].strip(), "time": s["time"], "end": s.get("endTime"), "venue": s.get("locationName"), "address": s.get("location"),
                                          "lat": lat, "lng": lng, "on_sales": s.get("onSales"), "uid": ev.get("UID"), "near_stations": near, "source_version": ev.get("version")})
                break
            except Exception as e:
                if attempt == 1: errors.append(f"{cname}: {type(e).__name__}")
    items = sorted(items, key=lambda x: x["time"])
    today = datetime.now().strftime("%Y/%m/%d")
    up = [x for x in items if x["time"][:10] >= today][:60]
    data = {"fetched_at": datetime.now().strftime("%Y-%m-%d %H:%M"), "total_ntpc": len(items), "upcoming": up, "errors": errors,
            "source": "文化部展演資訊 API（data.gov.tw/dataset/6478）；無出席人數欄位，僅供需求情境", "note": "回放日(2026-06)無同日活動資料，演唱會以情境參數呈現"}
    _cul.update({"ts": time.time(), "data": data}); return data

@app.get("/api/model")
def api_model():
    path = os.path.join(ROOT, "reports", "model_eval.json")
    rep = json.load(open(path)) if os.path.exists(path) else {"status": "training"}
    audit = json.load(open("/Users/chenhongfei/Desktop/Claude_YouBike專案交接包/資料稽核/data_audit.json"))
    ingest = json.load(open(os.path.join(ROOT, "data/processed/ingest_log.json")))
    return {"eval": rep, "model_origin": PRED.model_origin, "sagemaker_metrics": getattr(PRED, "sm_metrics", None), "audit": {k: audit[k] for k in ["raw_rows", "valid_rows", "invalid_counts", "both_zero", "canonical_stations", "observation_empty_pct", "observation_full_pct", "both_zero_excluded_empty_pct", "both_zero_excluded_full_pct", "june_persistence_baseline"]},
            "ingest": ingest, "model_horizons": PRED.model_horizons}

@app.post("/api/model/reload")
def api_model_reload(): PRED._cache.clear(); return {"model_horizons": PRED.reload_models()}
