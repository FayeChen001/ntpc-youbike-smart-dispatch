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
import metrics as MX
import events as EV
import tickets as TK

if not (os.environ.get("AWS_PROFILE") or "").strip():
    os.environ.pop("AWS_PROFILE", None); os.environ.pop("AWS_DEFAULT_PROFILE", None)
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
                "out_time": "07:40", "back_time": "18:10", "join_rewards": True, "preference": "time", "max_walk_min": 12,
                "recent_places": []},
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
                import boto3; _ddb = AWSLOC.session().resource("dynamodb", region_name="us-west-2")
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
def _zero_run(M, t, sid, max_back=12):
    """回傳（連續為零的觀測筆數, 首尾相距分鐘）。缺測即中斷，不視為延續。
    半小時快照只能說明取樣當下的狀態，不能證明取樣之間沒有恢復。"""
    n = 0
    for k in range(t, max(-1, t - max_back), -1):
        v = M[k, sid]
        if np.isnan(v) or v != 0: break
        n += 1
    return n, (n - 1) * 30

def evaluate_alerts(p):
    t = STATE["clock"]["t_idx"]; B = PRED.ctx.B; S = PRED.ctx.S
    cands = []
    for r in p.itertuples():
        sid = r.sid; st = r.status
        if st == "stale_flat": cands.append((sid, "stale_flat", "info", f"{r.name}：24 小時以上庫存無變化，疑似停站或資料未更新，列入待查、不派車", 0.0))
        elif st == "both_zero": cands.append((sid, "both_zero", "info", f"{r.name}：可借與可還同時為 0，狀態待查、不直接派車", 0.0))
        elif st == "cap_conflict": cands.append((sid, "cap_conflict", "info", f"{r.name}：可借＋可還超過總車柱，容量版本待確認", 0.0))
        elif st == "empty":
            n_obs, span = _zero_run(B, t, sid)
            if n_obs >= 3: cands.append((sid, "persistent_empty", "high",
                f"{r.name}：最近 {n_obs} 次觀測（跨 {span} 分鐘）都是零車。快照之間是否曾短暫有車無法由此資料判定。", 1.0))
        elif st == "full":
            n_obs, span = _zero_run(S, t, sid)
            if n_obs >= 3: cands.append((sid, "persistent_full", "high",
                f"{r.name}：最近 {n_obs} 次觀測（跨 {span} 分鐘）都是零空位。快照之間是否曾短暫有位無法由此資料判定。", 1.0))
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
        quiet = len(new) >= 8 and sev != "high"          # 高風險一律通知，不被節流吞掉
        a = {"id": uuid.uuid4().hex[:8], "key": key, "sid": int(sid), "station": p.loc[p.sid == sid, "name"].iloc[0], "district": p.loc[p.sid == sid, "district"].iloc[0],
             "type": typ, "severity": sev, "message": msg, "score": round(score, 2), "status": "open", "opened": iso(now_ts()), "acked": None, "resolved": None,
             "notified": not quiet,
             "route": "待查清單" if typ in ("stale_flat", "both_zero", "cap_conflict") else ("調度端" if "persistent" in typ or "_60" in typ else "提前排程")}
        STATE["alerts"][key] = a; STATE["alert_log"].append(a); ddb_put("yb_alerts", a)   # 先建檔，永遠不丟事件
        if quiet: suppressed += 1
        else: new.append(a)
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
    if suppressed: broadcast("alert_digest", {"suppressed": suppressed, "message": f"另有 {suppressed} 站已建檔於告警清單，本步不逐則跳通知（高風險不受節流）"})
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
        if tk.get("manual"): continue          # 已由派車端實際派工的單，狀態只能由人推進
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
    threading.Thread(target=_warm_routes, daemon=True).start()

def _warm_routes():
    """預熱預設通勤路線的路段快取，讓 demo 第一次規劃就很快。"""
    time.sleep(6)
    try:
        pr = STATE["profile"]
        if pr.get("home_sid") is None or pr.get("work_sid") is None: return
        for o, d, t in ((STATE["user"]["home"], STATE["user"]["work"], pr["out_time"]),
                        (STATE["user"]["work"], STATE["user"]["home"], pr["back_time"])):
            w = wx_now()
            PL.plan_trip(apply_dispatch_to_pred(apply_event_to_pred(current_pred())), tuple(o), tuple(d),
                         pd.Timestamp(f"{now_ts().date()} {t}"), now_ts(), pr["max_walk_min"], weather=w, preference=pr["preference"])
        print("[warm] 預設通勤路線快取完成", flush=True)
    except Exception as e:
        print("[warm] skipped:", type(e).__name__, flush=True)

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
@app.get("/manifest.webmanifest")
def manifest(): return FileResponse(os.path.join(STATIC, "manifest.webmanifest"), media_type="application/manifest+json")
@app.get("/sw.js")
def sw(): return FileResponse(os.path.join(STATIC, "sw.js"), media_type="application/javascript", headers={"Cache-Control": "no-cache"})

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
        at = AWSLOC.session().client("athena", region_name="us-west-2")
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
                a["dispatched"] = a.get("dispatched") or iso(now_ts())
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
                       int(body.get("max_walk_min") or STATE["profile"]["max_walk_min"]), weather=w, preference=pref,
                       arrive_by=body.get("arrive_by"))
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
        reasons = [r if isinstance(r, str) else (r.get("reason") or r.get("text") or next(iter(r.values()), "")) if isinstance(r, dict) else str(r) for r in reasons]
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

def _ticket_age_s(tk):
    return (now_ts() - pd.Timestamp(tk["ts"])).total_seconds()

def submit_ticket(body: dict):
    """唯一的建單入口。HTTP 路由與 server 內部呼叫都走這裡，才不會出現兩套去重規則。
    實作在 app/tickets.py（B 線擁有），這裡只負責站名、廣播與通知。"""
    sid = int(body["sid"])
    row = ST.loc[ST.sid == sid]
    if row.empty: return None
    name = row["name"].iloc[0]
    tk, action = TK.SERVICE.submit(STATE["tickets"], body, station_name=name,
                                   now_iso=iso(now_ts()), now_str=str(now_ts()), age_s=_ticket_age_s)
    ddb_put("yb_tickets", tk); broadcast("ticket", tk)
    ident = tk.get("bike_no") or (f"{tk['dock_id']} 號柱" if tk.get("dock_id") else "無資產識別")
    if action == "idempotent":
        return tk
    if action == "merged":
        notify("citizen", "回報已合併", f"{name} 的同一個{TK.ASSET_LABEL.get(tk.get('asset_type'), '設備')}（{ident}）回報已在處理中，感謝補充。", "ticket",
               {"ticket_id": tk["id"], "sid": sid})
        return tk
    notify("citizen", "報修已受理", f"{name}｜{tk['issue']}。工單 {tk['id']}；處理進度會在此更新。", "ticket", {"ticket_id": tk["id"], "sid": sid})
    notify("gov", f"民眾報修｜{name}", f"{tk['issue']}（{ident}）。工單 {tk['id']}，由營運端主責處理。", "ticket", {"ticket_id": tk["id"], "sid": sid})
    notify("ops", f"維修工單 {tk['id']}｜{name}", f"{TK.ASSET_LABEL.get(tk.get('asset_type'), '設備')}｜{tk['issue']}（{ident}）"
           + ("。明確機械問題，可直接派修。" if tk.get("diagnosis") == "direct_repair" else "。證據不足，列待診斷。"),
           "ticket", {"ticket_id": tk["id"], "sid": sid, "asset_type": tk.get("asset_type")})
    return tk

@app.post("/api/tickets")
def api_ticket_create(body: dict):
    tk = submit_ticket(body)
    if tk is None: return JSONResponse({"error": "unknown sid"}, 404)
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

@app.get("/api/search_place")
def api_search_place(q: str):
    q = (q or "").strip()
    if len(q) < 1: return {"results": []}
    res = []
    # 先比對站點名稱（本地資料，最快）
    hit = ST[ST["name"].str.contains(q, regex=False, na=False)].head(4)
    for _, r in hit.iterrows():
        res.append({"title": r["name"], "sub": f"{r.district}・YouBike 站點", "lat": float(r.lat), "lon": float(r.lon), "src": "station"})
    # 再用 Amazon Location Places 搜地標與地址
    try:
        import boto3
        gp = AWSLOC.session().client("geo-places", region_name="us-west-2")
        rr = gp.suggest(QueryText=q, BiasPosition=[121.4723, 25.0262], MaxResults=6, Language="zh-Hant",
                        Filter={"IncludeCountries": ["TWN"]}, AdditionalFeatures=["Core"])
        AWSLOC.STATUS["places"] += 1
        for x in rr.get("ResultItems", []):
            pos = (x.get("Place") or {}).get("Position")
            if not pos: continue
            addr = ((x.get("Place") or {}).get("Address") or {}).get("Label", "")
            if any(abs(pos[1] - r["lat"]) < 1e-4 and abs(pos[0] - r["lon"]) < 1e-4 for r in res): continue
            res.append({"title": x.get("Title"), "sub": addr[:40] or "Amazon Location", "lat": pos[1], "lon": pos[0], "src": "aws"})
    except Exception as e:
        AWSLOC.STATUS["errors"] += 1; AWSLOC.STATUS["last_error"] = f"suggest: {type(e).__name__}"
    return {"results": res[:8], "source": "站點名稱比對＋Amazon Location Service Places"}

@app.post("/api/recent_place")
def api_recent_place(body: dict):
    rp = STATE["profile"]["recent_places"]
    item = {"title": body["title"], "lat": body["lat"], "lon": body["lon"]}
    rp = [x for x in rp if x["title"] != item["title"]]
    STATE["profile"]["recent_places"] = ([item] + rp)[:6]
    return {"recent_places": STATE["profile"]["recent_places"]}

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
    for k in ("nickname", "role", "home_sid", "work_sid", "out_time", "back_time", "join_rewards", "preference", "max_walk_min", "onboarded", "recent_places"):
        if k in body: pr[k] = body[k]
    if pr["home_sid"] is not None and pr["work_sid"] is not None:
        STATE["user"]["home"] = place_of(pr["home_sid"], (0.0022, 0.0016))   # 家＝站點附近的住處，非站點本身
        STATE["user"]["work"] = place_of(pr["work_sid"], (0.0011, 0.0009))   # 目的地在站點附近，不等於站點本身
        STATE["user"]["commute_out"] = pr["out_time"]; STATE["user"]["commute_back"] = pr["back_time"]
        STATE["user"]["student"] = pr["role"] == "student"; STATE["user"]["max_walk_min"] = pr["max_walk_min"]
    STATE["pref_prompt"] = None
    broadcast("profile", api_profile_get())
    threading.Thread(target=_warm_routes, daemon=True).start()
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
            STATE["pref_prompt"] = {"kind": last[0], "label": {"fast": "準時抵達", "reliable": "不用怕沒車沒位", "reward": "多集點"}.get(last[0], last[0])}
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

@app.post("/api/trip/report_bike")
def api_trip_report_bike(body: dict):
    """借車前或騎乘中回報目前這台車有問題，系統建立工單並指派下一台。"""
    t = STATE["trip"]
    if not t: return JSONResponse({"error": "no trip"}, 404)
    stage = body.get("stage", "before_borrow")
    sid = t["option"]["borrow"]["sid"] if stage == "before_borrow" else t["option"]["return"]["sid"]
    tk = api_ticket_create({"sid": sid, "bike_no": t["bike_no"], "issue": body["issue"],
                            "note": {"before_borrow": "借車前發現", "riding": "騎乘中發現", "after_return": "還車時回報"}.get(stage, "")})
    old = t["bike_no"]
    if stage == "before_borrow":
        t["bike_no"] = f"YB2-{np.random.randint(10000, 99999)}"
        t.setdefault("skipped_bikes", []).append({"bike_no": old, "issue": body["issue"]})
    t.setdefault("reports", []).append({"bike_no": old, "issue": body["issue"], "stage": stage, "ticket": tk["id"] if isinstance(tk, dict) else None})
    broadcast("trip", t)
    return {"trip": t, "ticket": tk if isinstance(tk, dict) else None, "old_bike": old, "new_bike": t["bike_no"], "stage": stage}

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
    base = 5 + t["points"] + 12 * len(t.get("reports", []))
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
    TK.SERVICE.reset(); PL.ledger_reset()          # B 線自有狀態：建單冪等表、規劃週期資源帳
    # C 線自有狀態：民眾回報、request_id 冪等表、坐墊標記。不清會留下指向已刪除工單的殘影。
    try: api_c_reset()
    except Exception as e: print("[reset] C 狀態清除失敗：%s: %s" % (type(e).__name__, e))
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
    audit = json.load(open(os.path.join(ROOT, "reports", "data_audit.json")))
    ingest = json.load(open(os.path.join(ROOT, "data/processed/ingest_log.json")))
    return {"eval": rep, "model_origin": PRED.model_origin, "sagemaker_metrics": getattr(PRED, "sm_metrics", None), "audit": {k: audit[k] for k in ["raw_rows", "valid_rows", "invalid_counts", "both_zero", "canonical_stations", "observation_empty_pct", "observation_full_pct", "both_zero_excluded_empty_pct", "both_zero_excluded_full_pct", "june_persistence_baseline"]},
            "ingest": ingest, "model_horizons": PRED.model_horizons}

@app.post("/api/model/reload")
def api_model_reload(): PRED._cache.clear(); return {"model_horizons": PRED.reload_models()}

# ------------------------------------------------------------------ 驗收指標
MET = MX.ServiceMetrics(PRED)

@app.get("/api/metrics/service")
def api_metrics_service(window: str = "day", kind: str = "empty"):
    """服務中斷事件：由回放觀測快照真實計算，附觀測下界與可能上界。"""
    d = MET.compute(window, STATE["clock"]["t_idx"], kind)
    return {**d, "windows": MX.WINDOWS, "now": iso(now_ts())}

@app.get("/api/metrics/threshold")
def api_metrics_threshold(horizon: int = 120, kind: str = "empty", duty: float = None):
    path = os.path.join(ROOT, "reports", "model_eval.json")
    if not os.path.exists(path): return {"error": "模型評估尚未產出"}
    rep = json.load(open(path))
    r = MX.threshold_tradeoff(rep, horizon, kind, duty)
    return r or {"error": f"沒有 {horizon} 分鐘尺度的評估結果"}

@app.get("/api/metrics/flow")
def api_metrics_flow():
    return {**MX.flow_metrics(STATE["alert_log"], STATE["tasks"], iso(now_ts())),
            "not_measurable": MX.NOT_MEASURABLE, "assumptions": PL.ASSUMPTIONS, "now": iso(now_ts())}


# ============================================================================
# C 主線（用戶端）：按鈕直接通報 → 不確定才用圖片輔助 → 受理成功 → 坐墊提醒 → 追蹤進度
#
# 設計界線（介面必須照著說）：
# - 建單不等於已確認根因、不等於遠端停租、不等於官方庫存已扣除、不等於安全復役。
# - 坐墊反轉是「提醒下一位」的現場標記，不是修好車，不會結束工單。
# - 還車未確認時不宣稱停止計費、不發完成獎勵。
# - 建單一律走 B 主線的 api_ops_ticket（與 POST /api/ops/tickets 同一實作），不另建入口。
# 本區塊只新增端點與自有狀態，不修改其他主線既有的函式或 STATE 結構。
# ============================================================================
import report_image as RIMG
from fastapi import UploadFile, File, Form

# 使用者按鈕。mech = 車輛機械問題，才會進入坐墊提醒。
C_PROBLEMS = [
    {"key": "tire",  "icon": "🛞", "label": "輪胎沒氣／破損", "kind": "mech", "asset": "bike", "unsafe": True},
    {"key": "brake", "icon": "🛑", "label": "煞車異常",       "kind": "mech", "asset": "bike", "unsafe": True},
    {"key": "chain", "icon": "⛓️", "label": "鏈條異常",       "kind": "mech", "asset": "bike", "unsafe": True},
    {"key": "body",  "icon": "🪑", "label": "車身／坐墊異常", "kind": "mech", "asset": "bike", "unsafe": False, "asks_saddle_broken": True},
    {"key": "cannot_use", "icon": "🚫", "label": "借不到／還不了", "kind": "service", "asset": "unknown"},
    {"key": "unsure",     "icon": "❓", "label": "不確定",         "kind": "unsure",  "asset": "unknown", "offers_photo": True},
]
C_PROB = {p["key"]: p for p in C_PROBLEMS}
# 交易狀態由使用者自述，系統無法驗證。文案不可寫成已停止計費或已退款。
C_TXN = {
    "not_started": {"label": "還沒借成功，車沒出來", "safe_swap": True},
    "unsure":      {"label": "不確定有沒有借到或扣款", "safe_swap": False},
    "riding":      {"label": "已經借出來，騎乘中",   "safe_swap": False},
    "return_unconfirmed": {"label": "推回去了但沒看到還車成功", "safe_swap": False},
}
C_STAGE = {"before_borrow": "借車前", "riding": "騎乘中", "after_return": "還車時", "passing": "路過看到"}
# 狀態分開：使用者離開、工單處理、服務恢復、設備驗收不共用同一個欄位。
C_STATUS_LABEL = {
    "received": "已受理", "pending_triage": "已受理，待診斷（尚未指派維修）",
    "routed_repair": "已建立維修工單", "routed_ops": "已轉調度端（供需問題）",
    "user_left": "你已離開，案件仍在處理",
}
SADDLE_STEPS = [
    "把車停穩，確認腳架立好或已停回車柱。",
    "鬆開坐墊下方的快拆桿，把坐墊降到最低。",
    "把坐墊轉 180 度，讓坐墊前端朝向車尾。",
    "鎖回快拆桿，確認坐墊不會自己轉動。",
]
CREPORTS = {"items": [], "seq": 0, "by_request": {}, "saddle": []}


def _c_now():
    return {"ts": iso(now_ts()), "clock_source": "replay", "version": 1}


def _c_find(rid):
    for r in CREPORTS["items"]:
        if r["id"] == rid: return r
    return None


ASSET_STATE_LABEL = {"suspect": "待現場確認", "confirmed_faulty": "已確認故障", "repaired": "已處理",
                     "verified_ok": "已驗收", "not_applicable": "不適用"}
SERVICE_STATE_LABEL = {"unknown": "未知", "degraded": "服務受影響", "restored": "服務已恢復"}

def _c_ticket_view(tid):
    """工單由 B 主線維護，C 只讀不改。四種狀態分開顯示，不混成一個。"""
    if not tid: return None
    for tk in STATE["tickets"]:
        if tk["id"] != tid: continue
        sm = tk.get("saddle_marker") or {}
        return {"id": tk["id"], "version": tk.get("version", 1),
                "status": tk["status"], "status_label": TK.FLOW_LABEL.get(tk["status"], tk["status"]),
                "asset_state": tk.get("asset_state"), "asset_state_label": ASSET_STATE_LABEL.get(tk.get("asset_state")),
                "service_state": tk.get("service_state"), "service_state_label": SERVICE_STATE_LABEL.get(tk.get("service_state")),
                "saddle_marker": {"status": sm.get("status", "unknown"), "source": sm.get("source"), "ts": sm.get("ts")},
                "asset_type": tk.get("asset_type", "unknown"), "assignee": tk.get("assignee"),
                "crew": tk.get("crew"), "eta": tk.get("eta"),
                "reports": tk.get("reports", 1), "report_ids": tk.get("report_ids", []),
                "dedup_key": (TK.ticket_key(tk)[0] if hasattr(TK, "ticket_key") else tk.get("dedup_key")),
                "history": tk.get("history", [])[-5:],
                "note": "工單處理、設備驗收、服務恢復、坐墊標記是四件事，不會互相代表。"}
    return {"id": tid, "status": "unknown", "status_label": "工單狀態未知（可能已被重置）"}


def _c_saddle(problem, stage, txn_state, saddle_broken):
    """坐墊提醒的適用條件。只針對已停穩、可安全操作、坐墊本身完好的車輛機械問題。"""
    base = {"title": "提醒下一位：把故障車坐墊調到最低，再轉 180 度朝後",
            "steps": SADDLE_STEPS,
            "disclaimer": "這是標記故障車給下一位看，不是把車修好。工單不會因此結束，也不會延長你的計費。",
            "official": "YouBike 官方對故障車的處理說明（2026-04-08 公告）也是坐墊調低並反轉 180 度。本服務把提醒放在受理之後，不表示官方要求先通報才能反轉。"}
    p = C_PROB.get(problem, {})
    # 順序有意義：還車未確認最優先，其次是坐墊本身壞掉，最後才是「不是機械問題」。
    if txn_state == "return_unconfirmed":
        return {**base, "applicable": False, "status": "not_applicable",
                "reason": "還車還沒確認，請先處理還車與計費，不要先去弄坐墊。"}
    if problem == "body" and saddle_broken:
        return {**base, "applicable": False, "status": "not_applicable",
                "reason": "坐墊本身卡住或損壞時不要強行轉動，以免傷手或弄壞更多。"}
    if p.get("kind") != "mech":
        return {**base, "applicable": False, "status": "not_applicable",
                "reason": "你回報的不是車輛機械問題，不要去反轉一台正常車的坐墊。"}
    if stage == "riding":
        return {**base, "applicable": True, "status": "deferred",
                "reason": "你還在騎乘中。請先停到安全的地方、確認車停穩，再做這個動作。"}
    return {**base, "applicable": True, "status": "pending", "reason": ""}


def _c_triage(rep):
    """依證據給疑似方向。按鈕選的機械問題是使用者自述，來源標明待現場檢查，不當成已確認根因。"""
    p = C_PROB.get(rep["problem"], {})
    img = rep.get("image") or {}
    if p.get("kind") == "mech":
        return {"suspect": "bike", "text": f"使用者自述{p['label']}，屬車輛可觀察機械問題",
                "route": "維修工單，待現場檢查確認", "evidence": "使用者按鈕選擇（自述，未經現場確認）",
                "confident": False, "source": "user_report"}
    if p.get("kind") == "service":
        return {"suspect": "service", "text": "借不到或還不了可能是車柱、車機、鎖具或交易，不能直接判定車輛壞掉",
                "route": "租借／車機／鎖具／交易待查", "evidence": "使用者按鈕選擇", "confident": False, "source": "user_report"}
    obs = img.get("observations") or []
    if img.get("ok") and obs and img.get("suspected_asset_type") in ("bike", "dock", "station") and rep.get("image_confirmed"):
        a = img["suspected_asset_type"]
        return {"suspect": a, "text": f"使用者確認照片顯示的{ {'bike':'車輛','dock':'車柱','station':'站端'}[a] }可見現象",
                "route": "待現場檢查確認", "evidence": "照片可見現象＋使用者確認（影像推測，非確認根因）",
                "confident": False, "source": "user_confirmed_image"}
    return {"suspect": "unknown", "text": "還無法判斷是車、柱、站端還是交易問題",
            "route": "待診斷，先不指派維修", "evidence": "資訊不足或照片未能確認", "confident": False, "source": "user_report"}


def _c_guidance(rep):
    """交易與安全狀態的處置建議。不宣稱已停止計費、已退款或已鎖定設備。"""
    g = {"safety_stop": False, "may_swap": False, "escalate": False, "no_reward": False, "lines": []}
    p = C_PROB.get(rep["problem"], {})
    if p.get("unsafe") and rep["stage"] in ("riding", "before_borrow"):
        g["safety_stop"] = True
        g["lines"].append("這類狀況有安全疑慮。請先停止騎乘，把車停在安全的地方，不要騎故障車去送修。")
    txn = rep.get("txn_state")
    if rep["stage"] == "before_borrow" and (txn == "not_started" or p.get("kind") == "mech"):
        g["may_swap"] = True
        g["lines"].append("你還沒騎走這台，可以直接換一台。這台我們已經標記待查。")
    if txn == "unsure":
        g["lines"].append("交易狀態不明時請不要反覆刷卡或重試，可能造成重複扣款。先到官方 APP 或客服查證這筆有沒有成立。")
    if rep["stage"] == "after_return" and txn == "return_unconfirmed":
        g["escalate"] = True; g["no_reward"] = True
        g["lines"].append("還車還沒確認，我們無法代你停止計費，也無法確認這筆是否已結束。")
        g["lines"].append("請保留現場證據：柱號、車號、現在時間，可以的話拍下車柱畫面與停放狀態。")
        g["lines"].append("接著用官方 APP 或客服申報未完成還車，這個流程只有營運商能處理。")
    if rep["stage"] == "riding" and not g["safety_stop"]:
        g["lines"].append("騎乘中先不要操作手機。到站停妥後再補充細節。")
    if rep["stage"] == "passing":
        g["lines"].append("你不是這台車的租借人，回報不會影響你的任何費用。")
    if not g["lines"]:
        g["lines"].append("已收到。你可以隨時離開，不需要反覆試車幫我們診斷。")
    return g


@app.get("/api/c/problems")
def api_c_problems():
    return {"problems": C_PROBLEMS, "txn_states": [{"key": k, **v} for k, v in C_TXN.items()], "stages": C_STAGE,
            "image": {"max_bytes": RIMG.MAX_BYTES, "min_side": RIMG.MIN_SIDE, "formats": ["jpeg", "png"],
                      "purpose": "照片只用來辨識看得見的現象與編號，會送到 Amazon Bedrock 判讀，不會存進事件流或版本庫。",
                      "avoid": "請不要拍到人臉、票卡或付款畫面。"},
            "note": "按鈕就能送出，圖片與文字都非必填。建單代表已受理，不代表已確認根因或已停租。"}


@app.post("/api/c/image")
async def api_c_image(file: UploadFile = File(None), hint: str = Form("（未填）"), data_url: str = Form(None)):
    """照片輔助判讀。失敗一律降級為人工待判讀，不回傳假的辨識成功。圖片本身不落地、不進事件流。"""
    raw = b""
    try:
        if file is not None: raw = await file.read()
        elif data_url:
            import base64
            raw = base64.b64decode(data_url.split(",", 1)[-1])
    except Exception as e:
        return {**RIMG._blank(f"讀取上傳內容失敗：{type(e).__name__}"), "hint": hint}
    r = RIMG.analyze(raw, hint)
    r["hint"] = hint
    r["note"] = "辨識結果僅供你確認或更正。不採用也可以照常送出回報。"
    return r


@app.post("/api/c/report")
def api_c_report(body: dict):
    """單一回報入口。request_id 相同就回同一筆，不會重複建單。"""
    rid_req = (body.get("request_id") or "").strip()
    if rid_req and rid_req in CREPORTS["by_request"]:
        old = _c_find(CREPORTS["by_request"][rid_req])
        if old: return {**old, "idempotent": True, "ticket": _c_ticket_view(old.get("ticket_id"))}
    problem = body.get("problem")
    if problem not in C_PROB: return JSONResponse({"error": "unknown problem", "allowed": list(C_PROB)}, 400)
    try: sid = int(body["sid"])
    except Exception: return JSONResponse({"error": "sid is required"}, 400)
    row = ST.loc[ST.sid == sid]
    if row.empty: return JSONResponse({"error": "unknown sid"}, 404)

    p = C_PROB[problem]
    # 柱號對外統一 dock_id，相容 C 既有的 dock_no 輸入，正規化一次。
    dock_id = (body.get("dock_id") or body.get("dock_no") or "").strip() or None
    img = body.get("image_analysis") or None
    if isinstance(img, dict):
        img = {k: img.get(k) for k in ("ok", "model_source", "observations", "suspected_asset_type",
                                       "extracted_bike_no", "extracted_dock_id", "error_text",
                                       "uncertainties", "requires_manual_review", "degraded_reason", "ms")}
    CREPORTS["seq"] += 1
    rep = {"id": f"C{CREPORTS['seq']:03d}", "request_id": rid_req or None, "sid": sid,
           "station": row["name"].iloc[0], "stage": body.get("stage", "passing"),
           "problem": problem, "problem_label": p["label"], "problem_kind": p["kind"],
           "free_text": (body.get("free_text") or "").strip()[:200] or None,
           "bike_no": (body.get("bike_no") or "").strip() or None, "dock_id": dock_id,
           "error_code": (body.get("error_code") or "").strip() or None,
           "txn_state": body.get("txn_state"), "saddle_broken": bool(body.get("saddle_broken")),
           "image": img, "image_confirmed": bool(body.get("image_confirmed")),
           "accepted": False, "ticket_id": None, "status": "received",
           "history": [], **_c_now()}
    rep["history"].append({"ts": rep["ts"], "status": "received", "label": C_STATUS_LABEL["received"]})
    CREPORTS["items"].insert(0, rep)
    if rid_req: CREPORTS["by_request"][rid_req] = rep["id"]

    rep["triage"] = _c_triage(rep)
    rep["guidance"] = _c_guidance(rep)

    # 建單：機械問題或使用者確認的照片線索才開維修工單，走 B 的單一實作。
    if rep["triage"]["suspect"] in ("bike", "dock", "station"):
        issue = p["label"] if p.get("kind") == "mech" else "、".join((img or {}).get("observations", [])[:1]) or "照片可見異常"
        note_bits = [C_STAGE.get(rep["stage"], rep["stage"]), f"來源：{rep['triage']['source']}（自述／待現場檢查）"]
        if rep["free_text"]: note_bits.append(f"補充：{rep['free_text']}")
        if img and img.get("observations"): note_bits.append("照片觀察：" + "；".join(img["observations"][:3]))
        if img and img.get("uncertainties"): note_bits.append("照片無法確認：" + "；".join(img["uncertainties"][:2]))
        evidence = [{"kind": "user_choice", "text": p["label"], "certainty": "stated"}]
        if rep["free_text"]: evidence.append({"kind": "user_text", "text": rep["free_text"], "certainty": "stated"})
        for o in (img or {}).get("observations", [])[:3]:
            evidence.append({"kind": "image_observation", "text": o, "certainty": "observed",
                             "model": (img or {}).get("model_source")})
        for u in (img or {}).get("uncertainties", [])[:2]:
            evidence.append({"kind": "image_uncertainty", "text": u, "certainty": "unknown"})
        tk = api_ops_ticket({"sid": sid, "issue": issue, "bike_no": rep["bike_no"] or "", "dock_id": dock_id or "",
                             "error_code": rep["error_code"] or "", "asset_type": rep["triage"]["suspect"],
                             "source": "user_report", "note": "｜".join(note_bits),
                             "request_id": rep["request_id"], "report_id": rep["id"],
                             "evidence": evidence, "certainty": "stated"})
        if isinstance(tk, dict) and tk.get("id"):
            rep["ticket_id"] = tk["id"]; rep["ticket_action"] = tk.get("action")
            rep["accepted"] = True; rep["status"] = "routed_repair"
            act = {"created": "", "merged": "（合併既有工單）", "idempotent": "（同一次送出，未重建）"}.get(tk.get("action"), "")
            rep["history"].append({"ts": iso(now_ts()), "status": "routed_repair",
                                   "label": f"{C_STATUS_LABEL['routed_repair']} {tk['id']}{act}"})
        else:
            rep["status"] = "pending_triage"
            rep["history"].append({"ts": iso(now_ts()), "status": "pending_triage", "label": "建單未成功，保留為待診斷"})
    elif rep["triage"]["suspect"] == "service":
        rep["accepted"] = True; rep["status"] = "routed_ops"
        rep["history"].append({"ts": iso(now_ts()), "status": "routed_ops", "label": C_STATUS_LABEL["routed_ops"]})
        notify("ops", f"民眾回報借不到／還不了｜{rep['station']}",
               f"{p['label']}。車柱／車機／鎖具／交易待查，未判定車輛故障。", "warn",
               {"sid": sid, "report_id": rep["id"], "role": "ops"})
    else:
        rep["accepted"] = True; rep["status"] = "pending_triage"
        rep["history"].append({"ts": iso(now_ts()), "status": "pending_triage", "label": C_STATUS_LABEL["pending_triage"]})
        notify("ops", f"待診斷回報｜{rep['station']}", f"{p['label']}。證據不足，尚未指派維修。", "info",
               {"sid": sid, "report_id": rep["id"], "role": "ops"})

    rep["saddle"] = _c_saddle(problem, rep["stage"], rep["txn_state"], rep["saddle_broken"])
    rep["saddle"]["marker_status"] = "not_applicable" if not rep["saddle"]["applicable"] else "unknown"
    # 事件流只送摘要，不含照片、base64 或自由文字。
    broadcast("c_report", {"id": rep["id"], "sid": sid, "station": rep["station"], "status": rep["status"],
                           "problem": problem, "ticket_id": rep["ticket_id"], "accepted": rep["accepted"]})
    return {**rep, "ticket": _c_ticket_view(rep["ticket_id"]), "idempotent": False}


@app.get("/api/c/report/{rid}")
def api_c_report_get(rid: str):
    r = _c_find(rid)
    if not r: return JSONResponse({"error": "not found"}, 404)
    return {**r, "ticket": _c_ticket_view(r.get("ticket_id")), "server_ts": iso(now_ts())}


@app.post("/api/c/report/{rid}/saddle")
def api_c_saddle(rid: str, body: dict):
    """記錄坐墊標記。這是現場提醒，不是維修確認，不會結束工單。"""
    r = _c_find(rid)
    if not r: return JSONResponse({"error": "not found"}, 404)
    st = (body or {}).get("status")
    if st not in ("done", "skipped", "not_applicable", "unknown"):
        return JSONResponse({"error": "status must be done/skipped/not_applicable/unknown"}, 400)
    if not r["saddle"]["applicable"] and st == "done":
        return JSONResponse({"error": "此回報不適用坐墊標記", "reason": r["saddle"]["reason"]}, 409)
    r["saddle"]["marker_status"] = st
    r["history"].append({"ts": iso(now_ts()), "status": r["status"],
                         "label": {"done": "使用者回報已完成坐墊反轉（現場提醒，非維修確認）",
                                   "skipped": "使用者略過坐墊動作（工單不受影響）",
                                   "not_applicable": "不適用坐墊標記",
                                   "unknown": "坐墊標記狀態未知"}[st]})
    CREPORTS["saddle"] = [x for x in CREPORTS["saddle"] if x["report_id"] != rid]
    CREPORTS["saddle"].append({"report_id": rid, "ticket_id": r.get("ticket_id"), "sid": r["sid"],
                               "bike_no": r.get("bike_no"), "saddle_marker_status": st,
                               "source": "user_report", "ts": iso(now_ts())})
    # 依 B 的契約 b-1 寫進工單的附加欄位。工單狀態不會因此改變。
    wrote = None
    if r.get("ticket_id"):
        before = _c_ticket_view(r["ticket_id"]) or {}
        res = api_ops_ticket_saddle(r["ticket_id"], {"status": st, "source": "user_report"})
        after = _c_ticket_view(r["ticket_id"]) or {}
        wrote = {"ok": isinstance(res, dict) and not res.get("error"),
                 "status_before": before.get("status"), "status_after": after.get("status"),
                 "status_unchanged": before.get("status") == after.get("status"),
                 "asset_state_unchanged": before.get("asset_state") == after.get("asset_state"),
                 "ticket_version": after.get("version")}
    return {"report_id": rid, "saddle_marker_status": st, "ticket_id": r.get("ticket_id"),
            "ticket_unchanged": bool(wrote and wrote["status_unchanged"]) if wrote else True,
            "written_to_ticket": wrote,
            "note": "坐墊標記只是現場提醒，工單狀態不受影響，也不代表車輛已修好或已驗收。"}


@app.get("/api/c/saddle_markers")
def api_c_saddle_markers():
    """給 B／A 讀的坐墊標記清單。B 在工單加上欄位後可改為直接寫入工單。"""
    return {"markers": CREPORTS["saddle"],
            "semantics": "done 代表使用者自述已把故障車坐墊反轉，僅為現場提醒；不是維修確認、不停止工單、不是安全或故障真值。"}


@app.post("/api/c/report/{rid}/leave")
def api_c_report_leave(rid: str):
    r = _c_find(rid)
    if not r: return JSONResponse({"error": "not found"}, 404)
    r["history"].append({"ts": iso(now_ts()), "status": r["status"], "label": C_STATUS_LABEL["user_left"]})
    r["user_left"] = True
    return {**r, "ticket": _c_ticket_view(r.get("ticket_id")),
            "note": "使用者離開不結案。工單與回報狀態維持不變。"}


@app.get("/api/c/reports")
def api_c_reports():
    return {"items": [{**r, "ticket": _c_ticket_view(r.get("ticket_id"))} for r in CREPORTS["items"][:30]],
            "server_ts": iso(now_ts())}


@app.get("/api/c/alternatives")
def api_c_alternatives(sid: int, kind: str = "borrow"):
    """附近替代站。明示是快照與預測，不保證你走到時還有車或還有位。"""
    p = current_pred()
    row = p[p.sid == sid]
    if row.empty: return JSONResponse({"error": "unknown station"}, 404)
    r = row.iloc[0]
    res = PL.nearby(p, (float(r.lat), float(r.lon)), 700, 12)
    out = []
    for s in res["stations"]:
        if s["sid"] == sid: continue
        ok = (s["bikes"] or 0) >= 2 if kind == "borrow" else (s["spaces"] or 0) >= 2
        if not ok: continue
        out.append({**s, "state": s["ease"] if kind == "borrow" else s["dock"]})
    return {"station": r["name"], "kind": kind, "alternatives": out[:5], "ts": iso(now_ts()),
            "disclaimer": "這是最近一次快照與預測，不是保證。走過去時可能已經被借走或停滿，建議到場前再看一次。"}


@app.get("/api/c/push/status")
def api_c_push_status():
    """誠實揭露推播能力。沒有推播伺服器就不能宣稱背景送達。"""
    configured = bool(os.environ.get("VAPID_PUBLIC_KEY"))
    return {
        "web_push_configured": configured, "vapid_public_key": os.environ.get("VAPID_PUBLIC_KEY") or None,
        "levels": [
            {"key": "in_page", "label": "頁面內通知", "available": True,
             "desc": "這個頁面開著的時候才會出現。經 Server-Sent Events 由伺服器推到瀏覽器。"},
            {"key": "os_while_alive", "label": "系統通知（頁面仍在執行時）", "available": True,
             "desc": "需要你授權通知權限。切到別的 App 時仍可能出現，但把這個分頁關掉就不會有。"},
            {"key": "background_push", "label": "背景推播（關掉頁面也收得到）", "available": configured,
             "desc": "需要推播伺服器與 VAPID 金鑰。本次未設定，關掉頁面後不會收到任何通知。iOS 鎖定畫面通知本輪為明示模擬。"},
        ],
        "honesty": "介面不會顯示「已送達」。伺服器只知道事件已發出，不知道你的裝置有沒有收到或看到。",
        "ios_note": "iPhone 需先把網頁加到主畫面才能使用 Web Push；本次未設定推播伺服器，所以加了也只有頁面內通知。",
        "simulated": True,
    }


@app.post("/api/c/swap_bike")
def api_c_swap_bike(body: dict = None):
    """只換掉行程中的車號，不重複開工單（工單已由 /api/c/report 依證據決定是否開）。"""
    t = STATE["trip"]
    if not t: return JSONResponse({"error": "no trip"}, 404)
    old = t["bike_no"]; t["bike_no"] = f"YB2-{np.random.randint(10000, 99999)}"
    t.setdefault("skipped_bikes", []).append({"bike_no": old, "reason": (body or {}).get("reason", "使用者回報")})
    broadcast("trip", t)
    return {"old_bike": old, "new_bike": t["bike_no"]}


@app.post("/api/c/reset")
def api_c_reset():
    """C 主線自有狀態的重置。api_reset 由共用邏輯負責，這裡不動它。"""
    n = len(CREPORTS["items"])
    CREPORTS["items"].clear(); CREPORTS["by_request"].clear(); CREPORTS["saddle"].clear(); CREPORTS["seq"] = 0
    RIMG.STATUS.update({"calls": 0, "degraded": 0, "last_error": None, "last_model": None})
    return {"cleared_reports": n}


@app.get("/api/c/vision/status")
def api_c_vision_status():
    return {**RIMG.STATUS, "model": RIMG.VISION_MODEL, "timeout_s": RIMG.VISION_TIMEOUT_S,
            "provider": "Amazon Bedrock（競賽允許的基礎模型）",
            "degrade_policy": "模型不可用、逾時或看不清楚，一律保留照片附件與人工待判讀，不回傳假的辨識成功。"}



# ============================================================================
# A 主線（政府端）：事件台帳 — 分級、觀測時長、負責人、ETA、原因與證據、狀態留痕
# 本區塊只新增端點。台帳的重算在讀取時進行，刻意不改 on_tick 等共用邏輯。
# ============================================================================

@app.get("/api/ledger")
def api_ledger(level: str = None, district: str = None):
    EV.refresh(STATE, PRED, current_pred(), now_ts(), iso)
    d = EV.ledger(STATE, iso(now_ts()))
    items = d["events"]
    if level: items = [a for a in items if a.get("level") == level]
    if district: items = [a for a in items if a.get("district") == district]
    # 服務可用性直接呼叫 B 主線的同一支實作，不在政府端另算一套
    av = api_ops_availability(only_flagged=1)
    if isinstance(d.get("overview"), dict):
        d["overview"]["availability"] = {"stations": av["stations"][:20], "count": av["count"],
                                         "semantics": av["semantics"],
                                         "source": "GET /api/ops/availability（B 主線，唯讀）"}
    return {**d, "events": items, "owners": EV.OWNERS, "causes": EV.CAUSES}


@app.get("/api/ledger/{aid}")
def api_ledger_get(aid: str):
    EV.refresh(STATE, PRED, current_pred(), now_ts(), iso)
    ev = _find_event(aid)
    if ev is None: return JSONResponse({"error": "not found"}, 404)
    out = dict(ev)
    if ev.get("sid") is not None and ev["sid"] >= 0:
        av = api_ops_availability(sid=int(ev["sid"]))
        out["availability"] = (av["stations"][0] if av["stations"] else None)
        out["availability_semantics"] = av["semantics"]
    return out


def _find_event(aid):
    for a in STATE["alerts"].values():
        if a["id"] == aid: return a
    for a in STATE["alert_log"]:
        if a["id"] == aid: return a
    return None


def _ops_echo(ev, action, body):
    """跨區／逾時與政府端要求，一律同步警示營運端：同一個 event_id 與 version，不另建任務。"""
    lvl = EV.LEVELS.get(ev.get("level"), {}).get("label", "")
    obs = ev.get("observed") or {}
    base = {"event_id": ev["id"], "event_version": ev["version"], "sid": ev.get("sid"),
            "level": ev.get("level"), "owner": ev.get("owner"), "alert_id": ev["id"]}
    if action == "assign":
        notify("ops", f"政府端指派｜{ev['station']}",
               f"{lvl} 事件指派給 {ev.get('owner')}：{ev['message']}", "warn", base)
    elif action == "request_ops":
        notify("ops", f"政府端要求處理｜{ev['station']}",
               f"{lvl}｜零值快照跨度 {obs.get('span_min', '未知')} 分"
               + (f"・站群 {ev.get('cluster_n')} 站同時無法服務" if ev.get("cluster") else "")
               + f"。{(body or {}).get('note') or '請營運端評估處理方案與 ETA'}"
               + "（政府端只提出要求，派工方案與人車由營運端決定）", "warn", base)
    elif action == "coordinate":
        ds = "、".join((body or {}).get("districts") or []) or "未指定行政區"
        notify("ops", f"跨區協調｜{ev['station']}",
               f"{lvl} 需要跨區支援（{ds}）。{(body or {}).get('note') or ''}"
               "　這是協調紀錄，不是派車任務。", "warn", base)


@app.post("/api/ledger/{aid}/action")
def api_ledger_action(aid: str, body: dict = None):
    """
    政府端動作：ack／assign／request_ops／track／coordinate／note。
    body 可帶 version（樂觀鎖，過期回 409）與 request_id（冪等，重送不重做）。
    政府端不建立也不修改派車任務。
    """
    ev = _find_event(aid)
    if ev is None: return JSONResponse({"error": "not found"}, 404)
    action = (body or {}).get("action")
    res, code = EV.apply_action(ev, action, body or {}, now_ts(), iso)
    if code == 200 and not res.get("idempotent"):
        ddb_put("yb_alerts", ev); broadcast("alert", ev)
        _ops_echo(ev, action, body)
    return JSONResponse(res, code)


@app.post("/api/ledger/{aid}/assign")
def api_ledger_assign(aid: str, body: dict = None):
    """相容舊呼叫；內部走同一個 apply_action，不另外實作一套。"""
    ev = _find_event(aid)
    if ev is None: return JSONResponse({"error": "not found"}, 404)
    res, code = EV.apply_action(ev, "assign", {**(body or {})}, now_ts(), iso)
    if code != 200: return JSONResponse(res, code)
    if not res.get("idempotent"):
        ddb_put("yb_alerts", ev); broadcast("alert", ev); _ops_echo(ev, "assign", body)
    return ev


@app.post("/api/ledger/{aid}/note")
def api_ledger_note(aid: str, body: dict = None):
    ev = _find_event(aid)
    if ev is None: return JSONResponse({"error": "not found"}, 404)
    res, code = EV.apply_action(ev, "note", body or {}, now_ts(), iso)
    if code != 200: return JSONResponse(res, code)
    ddb_put("yb_alerts", ev)
    return ev


# ================================================================== B 線（派車端）附加端點
# 只在檔尾新增，不改動上面既有函式的行為。建單邏輯一律委派 app/tickets.py，不在這裡複製規則。

def _saddle_from_c(tid):
    """唯讀橋接：C 主線的坐墊標記目前存在 CREPORTS["saddle"]，尚未改呼叫 B 的 /saddle 端點。
    這裡只讀不寫，標成 external 來源，避免冒充工單上的權威欄位。"""
    try:
        rows = [x for x in CREPORTS.get("saddle", []) if x.get("ticket_id") == tid]
    except Exception:
        return None
    if not rows: return None
    r = sorted(rows, key=lambda x: x.get("ts") or "")[-1]
    return {"status": r.get("saddle_marker_status"), "source": r.get("source") or "user_report",
            "ts": r.get("ts"), "origin": "c_report", "authoritative": False,
            "note": "來自用戶端回報清單，尚未寫入工單欄位；不是維修確認"}

def _with_saddle(tk):
    if (tk.get("saddle_marker") or {}).get("status") in (None, "unknown"):
        ext = _saddle_from_c(tk["id"])
        if ext: return {**tk, "saddle_marker_external": ext}
    return tk

def _find_ticket(tid):
    for tk in STATE["tickets"]:
        if tk["id"] == tid: return tk
    return None

@app.post("/api/ops/tickets")
def api_ops_ticket(body: dict):
    """派車端／民眾端共用的建單入口（與 POST /api/tickets 走同一個 submit_ticket）。
    body: sid(必填), issue, note, bike_no, dock_id|dock_no, error_code, asset_type,
          request_id(冪等), report_id, source, symptom_keys[], evidence[], certainty"""
    if "sid" not in (body or {}): return JSONResponse({"error": "sid is required"}, 400)
    before = {t["id"] for t in STATE["tickets"]}
    tk = submit_ticket(body)
    if tk is None: return JSONResponse({"error": "unknown sid"}, 404)
    action = "created" if tk["id"] not in before else ("merged" if tk.get("reports", 1) > 1 else "idempotent")
    return {**tk, "action": action, "merged": action == "merged"}

@app.get("/api/ops/tickets")
def api_ops_tickets(state: str = "", pending: int = 0):
    """唯讀。state=open 只看未結案；pending=1 只看待診斷（證據不足、尚未確認是哪個設備）。"""
    out = list(STATE["tickets"])
    if state == "open": out = [t for t in out if t["status"] != "closed"]
    if pending: out = [t for t in out if t.get("diagnosis") == "pending_triage"]
    return {"tickets": [_with_saddle(t) for t in out], "flow": [{"key": k, "label": TK.FLOW_LABEL[k]} for k in TK.FLOW],
            "contract_version": "b-1", "clock_source": "replay", "ts": iso(now_ts())}

@app.get("/api/ops/tickets/dedup")
def api_ops_ticket_dedup():
    """給驗收看的：每張單是靠什麼識別去重的。唯讀。"""
    out = []
    for tk in STATE["tickets"]:
        k, b = TK.ticket_key(tk)
        out.append({"id": tk["id"], "station": tk["station"], "issue": tk.get("issue"),
                    "asset_type": tk.get("asset_type", "unknown"), "dedup_key": k, "keyed_by": b,
                    "reports": tk.get("reports", 1), "bike_no": tk.get("bike_no"), "dock_id": tk.get("dock_id"),
                    "error_codes": tk.get("error_codes", []), "related_ids": tk.get("related_ids", []),
                    "status": tk["status"], "version": tk.get("version", 1)})
    return {"tickets": out, "rule": "車號 > 柱號 > 站點＋問題類別（只有最後一種限 2 小時窗，且雙方都必須沒有資產識別）"}

@app.get("/api/ops/tickets/{tid}")
def api_ops_ticket_one(tid: str):
    tk = _find_ticket(tid)
    if tk is None: return JSONResponse({"error": "not found"}, 404)
    return {**_with_saddle(tk), "clock_source": "replay", "ts": iso(now_ts())}

@app.post("/api/ops/tickets/{tid}/saddle")
def api_ops_ticket_saddle(tid: str, body: dict = None):
    """用戶端回報是否照官方方式把故障車坐墊調低反轉。
    只更新附加欄位：不結案、不改工單狀態、不等於已維修、不影響工單處理。"""
    tk = _find_ticket(tid)
    if tk is None: return JSONResponse({"error": "not found"}, 404)
    st = ((body or {}).get("status") or "unknown").strip()
    res, code = TK.SERVICE.set_saddle(tk, st, source=((body or {}).get("source") or "user_report"), now_iso=iso(now_ts()))
    if code != "ok": return JSONResponse({"error": code, "allowed": list(TK.SADDLE)}, 400)
    ddb_put("yb_tickets", tk); broadcast("ticket", tk)
    return {**tk, "note": "坐墊標記僅為現場提醒，非維修確認，工單仍在處理中"}

@app.post("/api/ops/tickets/{tid}/transition")
def api_ops_ticket_transition(tid: str, body: dict = None):
    """派車端推進工單：接單→現場→處理→驗收→結案。
    帶 version 做樂觀鎖，舊版本回 409，同一個決策重送不會重複推進。"""
    tk = _find_ticket(tid)
    if tk is None: return JSONResponse({"error": "not found"}, 404)
    b = body or {}
    res, code = TK.SERVICE.transition(tk, (b.get("to") or "").strip(), actor=b.get("actor") or "派車端",
                                      now_iso=iso(now_ts()), version=b.get("version"),
                                      note=b.get("note"), crew=b.get("crew"), eta=b.get("eta"))
    if code == "conflict":
        return JSONResponse({"error": "version_conflict", "current_version": tk.get("version", 1),
                             "current_status": tk["status"], "hint": "先 GET 取回最新版本再重送"}, 409)
    if code in ("bad_status", "backwards"):
        return JSONResponse({"error": code, "current_status": tk["status"], "flow": TK.FLOW}, 400)
    tk["manual"] = True          # 已由人實際派工，模擬流程不再自動推進它
    ddb_put("yb_tickets", tk); broadcast("ticket", tk)
    if code == "ok":
        notify("gov", f"工單進度 {tk['id']}｜{tk['station']}", f"{TK.FLOW_LABEL[tk['status']]}"
               + (f"｜{tk.get('crew')}" if tk.get("crew") else ""), "ticket",
               {"ticket_id": tk["id"], "sid": tk["sid"], "status": tk["status"], "version": tk["version"]})
    return {**tk, "action": code}

@app.get("/api/ops/contract")
def api_ops_contract():
    """給 A／C 對照的契約摘要，避免三端各自猜欄位。唯讀。"""
    return {
        "contract_version": "b-1",
        "single_entry": "POST /api/tickets 與 POST /api/ops/tickets 都委派 server.submit_ticket → app/tickets.py",
        "identifiers": {"dock": "對外統一 dock_id，相容輸入 dock_no，內部正規化一次",
                        "null_policy": "沒有就是 null，不用空字串冒充識別"},
        "idempotency": "帶 request_id 重送回同一張單（action=idempotent）",
        "dedup_rule": "車號 > 柱號 > 站點＋問題類別；站點層級限 2 小時且雙方都必須沒有資產識別",
        "states": {"status": TK.FLOW, "service_state": list(TK.SERVICE_STATE),
                   "asset_state": list(TK.ASSET_STATE), "saddle_marker": list(TK.SADDLE)},
        "state_meaning": {"status": "工單處理流程", "service_state": "站點服務是否恢復",
                          "asset_state": "設備驗收", "saddle_marker": "用戶現場標記，非維修確認"},
        "version": "每次變更 +1；transition 帶舊 version 回 409",
        "clock_source": "replay", "ts": iso(now_ts()),
    }

# ---- B 線：規劃週期與任務資源狀態 ----
def _find_task(tid):
    for tk in STATE["tasks"]:
        if tk["id"] == tid: return tk
    return None

@app.get("/api/ops/cycle")
def api_ops_cycle():
    """唯讀：目前規劃週期的資源帳。候選／已確認／在途／已釋放分開列，GET 不改任何狀態。"""
    cid = PL.cycle_open(now_ts())
    snap = PL.cycle_snapshot(cid)
    names = {}
    for row in snap["by_station"]:
        if row["sid"] >= 0:
            r = ST.loc[ST.sid == row["sid"]]
            names[row["sid"]] = r["name"].iloc[0] if not r.empty else str(row["sid"])
        else:
            names[row["sid"]] = "調度中心／跨區補給（情境）"
    for row in snap["by_station"]: row["station"] = names.get(row["sid"])
    return {**snap, "clock_source": "replay", "ts": iso(now_ts()),
            "states": {"candidate": "規劃產生、尚未確認", "confirmed": "調度員已確認派工",
                       "in_transit": "已出車", "released": "取消或重算釋放，不再占用資源"}}

@app.post("/api/ops/tasks/{tid}/{action}")
def api_ops_task_action(tid: str, action: str, body: dict = None):
    """派車端的任務動作。除了推進任務狀態，同時把資源帳上的預約改成對應狀態。
    confirm→confirmed、dispatch→in_transit、cancel→released（釋放資源）。"""
    tk = _find_task(tid)
    if tk is None: return JSONResponse({"error": "not found"}, 404)
    b = body or {}
    if b.get("version") is not None and int(b["version"]) != int(tk.get("res_version", 1)):
        return JSONResponse({"error": "version_conflict", "current_version": tk.get("res_version", 1),
                             "current_state": tk.get("reservation_state"), "hint": "先 GET /api/tasks 取回最新版本"}, 409)
    mapping = {"confirm": "confirmed", "dispatch": "in_transit", "cancel": "released"}
    if action not in mapping and action != "escalate":
        return JSONResponse({"error": "unknown action", "allowed": list(mapping) + ["escalate"]}, 400)
    now = iso(now_ts())
    if action == "escalate":
        plan = (b.get("plan") or "").strip()
        if plan not in ("cross_district", "accept_delay", "divert_only"):
            return JSONResponse({"error": "plan must be cross_district / accept_delay / divert_only"}, 400)
        if plan == "cross_district" and not b.get("eta"):
            return JSONResponse({"error": "cross_district 必須帶實際可行的 eta，沒有可派資源就不要給 ETA"}, 400)
        tk["escalation"] = {"plan": plan, "eta": b.get("eta"), "reason": b.get("reason") or "",
                            "owner": b.get("owner") or "微笑單車調度中心", "ts": now}
        tk.setdefault("history", []).append({"ts": now, "status": tk["status"],
                                             "note": f"營運端主責處理：{ {'cross_district': '跨區支援', 'accept_delay': '接受延誤並通知', 'divert_only': '無可派資源，改民眾分流'}[plan] }"
                                                     + (f"，ETA {b['eta']}" if b.get("eta") else "，不提供 ETA（無可派資源）")})
        tk["res_version"] = int(tk.get("res_version", 1)) + 1
        ddb_put("yb_tasks", tk); broadcast("task", tk)
        notify("gov", f"營運端處理方案｜{tk['district']} {tk['id']}",
               tk["escalation"]["reason"] or {"cross_district": "已安排跨區支援", "accept_delay": "接受延誤，持續處理",
                                              "divert_only": "無可派人車，改以民眾分流"}[plan], "warn",
               {"task_id": tid, "plan": plan, "eta": b.get("eta"), "owner": tk["escalation"]["owner"]})
        return tk
    state = mapping[action]
    moved = PL.cycle_set_res_state(tk.get("cycle"), tk.get("reservations"), state)
    tk["reservation_state"] = state
    tk["res_version"] = int(tk.get("res_version", 1)) + 1
    if action == "confirm":
        tk["confirmed_by"] = b.get("actor") or "調度員"
        tk.setdefault("history", []).append({"ts": now, "status": tk["status"], "note": "調度員確認派工，資源改為已確認"})
    elif action == "dispatch":
        tk["status"] = "dispatched"; tk["depart_by"] = str(now_ts())
        tk.setdefault("history", []).append({"ts": now, "status": "dispatched", "note": "調度員手動立即出車（執行進度為模擬）"})
    elif action == "cancel":
        tk["status"] = "cancelled"
        tk.setdefault("history", []).append({"ts": now, "status": "cancelled", "note": b.get("reason") or "調度員取消，資源已釋放"})
    ddb_put("yb_tasks", tk); broadcast("task", tk)
    return {**tk, "reservations_changed": moved}

@app.get("/api/ops/availability")
def api_ops_availability(district: str = "", sid: int = -1, only_flagged: int = 0):
    """服務可用性彙總（給 A 的事件台帳與 B 的待查清單用）。唯讀。

    誠實界線，這支端點不做以下任何一件事：
    - **不把本工具的工單從官方可借數扣掉**。我們沒有營運商的庫存介接，不知道官方是否已經扣除；
      兩個數字並列，不可相加也不可相減。
    - **不把整站判成不可用**。只有在「已知不可用車數 ≥ 官方可借數，且每一台都有車號識別」時
      才給一個 may_need_service_event 建議旗標，仍需人工確認。
    - **不聲稱已鎖車**。沒有遠端停租介接，工單只代表本工具排除推薦。
    """
    p = current_pred()
    rows = []
    open_tk = [t for t in STATE["tickets"] if t.get("status") not in ("closed", "verified")]
    by_sid = {}
    for t in open_tk:
        by_sid.setdefault(t["sid"], []).append(t)
    for r in p.itertuples():
        if district and r.district != district: continue
        if sid >= 0 and int(r.sid) != sid: continue
        tks = by_sid.get(int(r.sid), [])
        if only_flagged and not tks: continue
        bikes = None if (r.bikes is None or (isinstance(r.bikes, float) and np.isnan(r.bikes))) else int(r.bikes)
        unusable_bikes = sorted({t["bike_no"] for t in tks if t.get("asset_type") == "bike" and t.get("bike_no")})
        unusable_docks = sorted({t["dock_id"] for t in tks if t.get("asset_type") == "dock" and t.get("dock_id")})
        suspected = [t["id"] for t in tks if t.get("diagnosis") == "pending_triage" or not (t.get("bike_no") or t.get("dock_id"))]
        flag = bool(bikes is not None and unusable_bikes and len(unusable_bikes) >= bikes)
        row = {
            "sid": int(r.sid), "station": r.name, "district": r.district,
            "official_bikes": bikes, "official_spaces": None if r.spaces is None else int(r.spaces),
            "known_unusable_bikes": len(unusable_bikes), "known_unusable_bike_nos": unusable_bikes,
            "known_unusable_docks": len(unusable_docks), "known_unusable_dock_ids": unusable_docks,
            "suspected_tickets": suspected, "open_tickets": [t["id"] for t in tks],
            "excluded_from_recommendation": bool(unusable_bikes or unusable_docks),
            "may_need_service_event": flag,
            "confirmed_at": iso(now_ts()) if tks else None,
        }
        if only_flagged and not (flag or suspected or unusable_bikes or unusable_docks): continue
        rows.append(row)
    return {
        "stations": rows[:400], "count": len(rows), "clock_source": "replay", "ts": iso(now_ts()),
        "semantics": {
            "official_bikes": "站點快照的可借車數，未扣除本工具的工單",
            "known_unusable_bikes": "本工具有未結案工單且帶車號的不同車輛數；不代表官方已扣除，兩者不可相加",
            "suspected_tickets": "證據不足或沒有資產識別的工單，不計入不可用",
            "may_need_service_event": "僅為建議旗標，需人工確認才可開服務中斷事件",
            "excluded_from_recommendation": "本工具不再推薦這些設備；沒有遠端停租介接，不代表已鎖車",
        },
    }
