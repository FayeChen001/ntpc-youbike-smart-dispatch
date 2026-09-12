"""
events.py — 政府端事件台帳：把「告警」升級成有生命週期、有分級、有原因與證據的事件。

界線：
- 觀測是每 30 分鐘一筆的庫存快照。「空站多久」只能給觀測跨度（第一筆零快照到現在）
  與可能上界（最後一筆確認正常的觀測到現在），不能給精確值，也不能宣稱期間連續中斷。
  前面沒有任何一筆確認正常的觀測時，上界為未知，不以跨度加一格之類的數字充數。缺測不算恢復。
- 分級門檻是依訪談轉述整理的「試行」門檻，不是官方標準，也還沒確認是否全站適用。
  訪談提到的「觀測空站 20 分鐘」在 30 分鐘解析度下量不到，這裡改用 30 分鐘並標示。
- 原因一律要有證據才寫上去。推不出證據就是「待查」，不猜。
- 「條件自動解除」代表站點恢復有車，不代表有人去處理過，兩者在台帳上分開記。
"""
import numpy as np
import pandas as pd

BIN_MIN = 30

# 試行分級（待主管確認，非官方標準）
LEVELS = {
    "P0": {"label": "P0", "desc": "零值快照跨度已達 60 分鐘、500 公尺內沒有替代站、且沒有可執行的恢復計畫；或 P1 逾時無人接手；或站群同時失效", "notify": "主管與值班主管"},
    "P1": {"label": "P1", "desc": "零值快照跨度已達 30 分鐘；若 500 公尺內還有替代站，最高只到這一級", "notify": "值班調度"},
    "P2": {"label": "P2", "desc": "預測 60–180 分鐘後將失衡，進入值班排程", "notify": "不逐則打擾主管"},
    "C":  {"label": "長期", "desc": "連續 12 小時以上沒車，或超過 24 小時沒看到過正常庫存：這不是今天的突發事件，派車解決不了，要查核營運狀態", "notify": "營運查核，不打擾主管"},
    "W":  {"label": "待查", "desc": "雙零、長期不變、容量矛盾：狀態待查核，不直接派車", "notify": "不通知"},
}
LEVEL_ORDER = {"P0": 0, "P1": 1, "P2": 2, "C": 3, "W": 4}

CHRONIC_MIN = 720        # 觀測跨度達 12 小時 → 長期性，不是今天的事件
CHRONIC_UPPER_MIN = 1440 # 超過 24 小時沒看過正常庫存 → 同上

P1_MIN = 30          # 觀測跨度達此值 → P1（跨度不等於確定連續中斷，分級門檻用跨度是保守取法）
P0_MIN = 60          # 觀測跨度達此值且無替代站、無可執行恢復計畫 → P0
P0_UNACKED_MIN = 30  # P1 開啟超過此時間仍無人確認 → 升 P0
CLUSTER_N = 3        # 同一行政區同時有這麼多件 P0/P1 → 視為站群同時失效

WATCH_TYPES = ("stale_flat", "both_zero", "cap_conflict")
ONGOING_TYPES = ("persistent_empty", "persistent_full")

CAUSES = {
    "data_stale": "資料過期",
    "no_task": "沒排到任務",
    "no_supply": "區內無供給",
    "too_late": "已排任務但趕不上",
    "en_route": "已派車，行進中",
    "demand_surge": "需求暴增（活動）",
    "equipment": "疑似設備故障",
    "unknown": "待查",
}


# ---------------------------------------------------------------- 觀測到的中斷時長
def observed_run(ctx, bins, t_idx, sid, kind):
    """
    回傳這一站在 t_idx 當下、仍在進行中的零車／零位區間。
    span_min：第一筆零快照到現在的時間差。這是觀測跨度，不是確定的連續中斷時間——
             快照之間是否曾短暫恢復，30 分鐘一筆的資料證明不了。
    upper_min：事件前最後一筆「確認正常」的觀測到現在（最多可能多久）。
              前面找不到確認正常的觀測時為 None（未知），不以其他數字代替。
    兩個都是「到目前為止」，事件還沒結束。
    """
    M = ctx.B if kind == "empty" else ctx.S
    O = ctx.S if kind == "empty" else ctx.B
    v = M[t_idx, sid]
    if np.isnan(v) or v != 0:
        return None

    i = t_idx
    while i - 1 >= 0:
        prev, other = M[i - 1, sid], O[i - 1, sid]
        if np.isnan(prev) or prev != 0 or (prev == 0 and other == 0):
            break
        i -= 1
    start = i

    j, gap = start - 1, False
    while j >= 0:
        if np.isnan(M[j, sid]):
            gap = True
            j -= 1
            continue
        if M[j, sid] == 0:
            j -= 1
            continue
        break

    k = t_idx
    while k >= 0 and np.isnan(M[k, sid]):
        k -= 1

    return {
        "span_min": int((t_idx - start) * BIN_MIN),
        "upper_min": None if j < 0 else int((t_idx - j) * BIN_MIN),
        "upper_known": j >= 0,
        "zero_snapshot_min": int((t_idx - start + 1) * BIN_MIN),
        "snapshots": int(t_idx - start + 1),
        "started_obs": str(bins[start]),
        "last_data_ts": None if k < 0 else str(bins[k]),
        "gap_inside": gap,
        "wording": "觀測跨度，不是確定的連續中斷時間",
    }


# ---------------------------------------------------------------- 覆蓋這一站的任務
# 缺車要靠「送車到這一站」（dropoff），缺位要靠「從這一站運出」（pickup）。
# 用同一個動作判斷兩種事件，會讓所有缺位事件都被誤判成「沒排到任務」而升級。
COVER_ACTION = {"empty": "dropoff", "full": "pickup"}
ACTION_LABEL = {"dropoff": "送車補給", "pickup": "運出騰位"}


def covering_task(tasks, sid, kind="empty"):
    """
    找出目前有哪一張任務會處理這一站的這一種缺口。回傳 (task, stop) 或 (None, None)。
    kind="empty" 找 dropoff；kind="full" 找 pickup。
    """
    want = COVER_ACTION.get(kind, "dropoff")
    best = (None, None)
    for tk in tasks:
        if tk.get("status") not in ("planned", "dispatched", "en_route", "proposed", "too_late"):
            continue
        for s in tk.get("stops", []):
            if s.get("action") == want and s.get("sid") == sid:
                if best[0] is None or LEVEL_ORDER.get(tk["status"], 9) < 9:
                    best = (tk, s)
    return best


def alternatives(pred_df, neighbors, sid, kind, need=2):
    """500 公尺內有沒有現在就借得到／還得到的替代站。
    只證明候選資源存在，不等於使用者走得到，也不等於走到時還有車。"""
    nb = neighbors(sid)
    if not nb:
        return {"n": 0, "names": [], "has": False, "no_neighbor": True}
    sub = pred_df[pred_df.sid.isin(nb)]
    col = "bikes" if kind == "empty" else "spaces"
    ok = sub[(sub.status.isin(("normal", "empty", "full"))) & (sub[col].fillna(0) >= need)]
    return {"n": int(len(ok)), "names": ok["name"].head(3).tolist(), "has": bool(len(ok)), "no_neighbor": False}


def district_task_state(tasks, district, horizon=None):
    out = [t for t in tasks if t.get("district") == district and t.get("status") not in ("done", "cancelled", "superseded")]
    return out


# ---------------------------------------------------------------- 原因與證據
def infer_cause(ev, state, now_ts):
    """
    只寫得出證據的原因。推不出來就是「待查」，不猜。
    回傳 {code, label, evidence:[{kind, ref, text}], confident:bool}
    """
    sid = ev.get("sid")
    tasks = state["tasks"]
    obs = ev.get("observed") or {}
    kind = "full" if "full" in (ev.get("type") or "") else "empty"
    act = ACTION_LABEL[COVER_ACTION[kind]]
    evidence = []

    # 1. 資料過期：最後一筆觀測距離現在超過一個分箱
    last = obs.get("last_data_ts")
    if last:
        stale_min = (pd.Timestamp(now_ts) - pd.Timestamp(last)).total_seconds() / 60
        if stale_min > BIN_MIN:
            return {"code": "data_stale", "label": CAUSES["data_stale"], "confident": True,
                    "evidence": [{"kind": "observation", "ref": last, "text": f"最後一筆有效觀測 {last[11:16]}，距今 {int(stale_min)} 分鐘，期間狀態不明"}]}

    # 2. 設備：這一站有未結案的維修工單
    tks = [t for t in state["tickets"] if t.get("sid") == sid and t.get("status") != "closed"]
    if tks:
        t0 = tks[0]
        evidence.append({"kind": "ticket", "ref": t0["id"], "text": f"工單 {t0['id']}（{t0.get('issue', '民眾回報')}）狀態 {t0.get('status')}"})
        return {"code": "equipment", "label": CAUSES["equipment"], "confident": True, "evidence": evidence}

    # 3. 活動情境造成的需求暴增
    sc = state["scenario"].get("event")
    if sc and sid in [int(x) for x in sc.get("station_share", {}).keys()]:
        end = pd.Timestamp(sc["end"])
        if end - pd.Timedelta(minutes=30) <= pd.Timestamp(now_ts) <= end + pd.Timedelta(minutes=90):
            return {"code": "demand_surge", "label": CAUSES["demand_surge"], "confident": True,
                    "evidence": [{"kind": "scenario", "ref": sc["title"], "text": f"{sc['title']} {end.strftime('%H:%M')} 散場，情境估 {sc['riders_out']} 人借車（出席人數為情境參數）"}]}

    # 4. 任務面
    tk, stop = covering_task(tasks, sid, kind)
    if tk is None:
        cross = [t for t in district_task_state(tasks, ev.get("district")) if t.get("status") == "needs_cross_district"]
        if cross:
            return {"code": "no_supply", "label": CAUSES["no_supply"], "confident": True,
                    "evidence": [{"kind": "task", "ref": cross[0]["id"], "text": cross[0].get("reason", "區內無可供給站")}]}
        return {"code": "no_task", "label": CAUSES["no_task"], "confident": True,
                "evidence": [{"kind": "task", "ref": "—",
                              "text": f"目前排程中沒有任何一張任務要為這一站{act}。"
                                      "本系統沒有巡查或人員定位紀錄，這只代表排程沒有涵蓋，"
                                      "不能據此推論沒有人到過現場。"}]}
    if tk["status"] == "too_late":
        return {"code": "too_late", "label": CAUSES["too_late"], "confident": True,
                "evidence": [{"kind": "task", "ref": tk["id"], "text": tk.get("reason", "決策到抵達的時間趕不上目標時間")}]}
    if tk["status"] in ("dispatched", "en_route"):
        return {"code": "en_route", "label": CAUSES["en_route"], "confident": True,
                "evidence": [{"kind": "task", "ref": tk["id"], "text": f"{tk['id']} 已出車（模擬狀態），預計 {str(stop.get('arrives_by', ''))[11:16]} 抵達本站{act}"}]}
    return {"code": "unknown", "label": CAUSES["unknown"], "confident": False,
            "evidence": [{"kind": "none", "ref": "—", "text": "只有庫存快照，無法證明是需求、人車、供給還是設備造成；需要任務、GPS、設備與借還紀錄才能判定"}]}


# ---------------------------------------------------------------- 生命週期
def new_event_fields(a, now_ts):
    return {"level": None,
            "owner": None, "eta": None, "eta_source": None,
            "observed": None, "cause": None,
            "timeline": [{"ts": a["opened"], "action": "opened", "actor": "系統", "note": a["message"]}],
            "escalated": None, "cluster": False, "notified": False}


def refresh(state, pred, pred_df, now_ts, iso):
    """
    重算觀測時長、替代站、ETA、原因與分級，升級留痕。只處理未結案的事件。
    在讀取台帳時呼叫，不掛在 on_tick 上（server.py 是三條主線共用的檔案）。

    分三段做，順序不能顛倒：先算每一件的基準級，再用基準級認定站群，最後才定案並寫時間線。
    若把站群促級寫在留痕之後，下一次重算會看到「已是 P0、基準是 P1」而反覆記錄一來一回。
    """
    ctx, bins, t_idx = pred.ctx, pred.bins, state["clock"]["t_idx"]
    live = [a for a in state["alerts"].values() if a.get("status") != "resolved"]
    base = {}
    state["_a_overview"] = overview(state, pred, pred_df, now_ts)

    # 第一段：基準分級（不看站群）
    for a in live:
        a.setdefault("timeline", [{"ts": a["opened"], "action": "opened", "actor": "系統", "note": a["message"]}])
        a.setdefault("owner", None)
        ensure_version(a)
        a["escalated"] = None
        if a["type"] in WATCH_TYPES:
            base[a["id"]] = "W"
            continue
        if a.get("sid") is None or a["sid"] < 0:          # 活動情境事件
            base[a["id"]] = "P2"
            continue

        kind = "empty" if "empty" in a["type"] else "full"
        a["observed"] = observed_run(ctx, bins, t_idx, a["sid"], kind) if a["type"] in ONGOING_TYPES else None

        tk, stop = covering_task(state["tasks"], a["sid"], kind)
        if tk and stop and stop.get("arrives_by"):
            a["eta"] = str(stop["arrives_by"])
            a["eta_source"] = f"{tk['id']}（{tk['status']}，{ACTION_LABEL[COVER_ACTION[kind]]}，模擬派工狀態）"
            a["covered_by"] = tk["id"]
            a["cover_action"] = COVER_ACTION[kind]
        else:
            a["eta"] = a["eta_source"] = a["covered_by"] = None
            a["cover_action"] = COVER_ACTION[kind]

        a["cause"] = infer_cause(a, state, now_ts)
        a["alt"] = alternatives(pred_df, pred.neighbors, a["sid"], kind) if a["type"] in ONGOING_TYPES else None

        lvl = "P2"
        if a["type"] in ONGOING_TYPES and a.get("observed"):
            o = a["observed"]
            span, upper = o["span_min"], o["upper_min"]
            chronic = span >= CHRONIC_MIN or upper is None or upper >= CHRONIC_UPPER_MIN
            executable = bool(tk and tk["status"] in ("planned", "dispatched", "en_route"))
            has_alt = bool(a["alt"] and a["alt"]["has"])
            if chronic:
                lvl = "C"
            elif span >= P0_MIN and not executable and not has_alt:
                lvl = "P0"
                a["escalated"] = f"零值快照跨度已達 {span} 分鐘、500 公尺內沒有替代站、也沒有可執行的恢復計畫"
            else:
                lvl = "P1"
        if lvl == "P1" and a.get("status") == "open":
            waited = (pd.Timestamp(now_ts) - pd.Timestamp(a["opened"])).total_seconds() / 60
            if waited >= P0_UNACKED_MIN and not (a["alt"] and a["alt"]["has"]):
                lvl = "P0"
                a["escalated"] = f"P1 開啟 {int(waited)} 分鐘仍無人確認，且 500 公尺內沒有替代站"
        base[a["id"]] = lvl

    # 第二段：站群同時失效。以 500 公尺鄰站關係認定，不是以行政區——
    # 一個行政區有一兩百站，三件同級不代表整片借不到；鄰站同時失效才代表。
    affected = {a["sid"] for a in live if base.get(a["id"]) in ("P0", "P1") and a.get("sid", -1) >= 0}
    cluster_of = {}
    for a in live:
        if a.get("sid", -1) < 0 or base.get(a["id"]) not in ("P0", "P1"):
            cluster_of[a["id"]] = 0
            continue
        n = len(set(pred.neighbors(a["sid"])) & affected) + 1
        cluster_of[a["id"]] = n if n >= CLUSTER_N else 0

    # 第三段：定案與留痕
    for a in live:
        lvl = base.get(a["id"], "P2")
        n = cluster_of.get(a["id"], 0)
        if n and lvl == "P1":
            lvl = "P0"
            a["escalated"] = f"站群同時失效：500 公尺內另有 {n - 1} 站同時無法服務"
        prev, was_cluster = a.get("level"), bool(a.get("cluster"))
        a["level"], a["cluster"], a["cluster_n"] = lvl, bool(n), n
        if bool(n) != was_cluster:
            a["timeline"].append({"ts": iso(now_ts), "action": "cluster", "actor": "系統",
                                  "note": (f"站群同時失效：{a['station']} 500 公尺內另有 {n - 1} 站同時是 P0／P1，整片借不到"
                                           if n else "站群已不再同時失效")})
        if prev is None:
            a["timeline"].append({"ts": iso(now_ts), "action": "level", "actor": "系統",
                                  "note": f"分級 {LEVELS[lvl]['label']}" + (f"：{a['escalated']}" if a.get("escalated") else "")})
        elif lvl != prev:
            a["timeline"].append({"ts": iso(now_ts), "action": "level", "actor": "系統",
                                  "note": f"分級 {LEVELS[prev]['label']} → {LEVELS[lvl]['label']}"
                                          + (f"：{a['escalated']}" if a.get("escalated") else
                                             (f"：零值快照跨度已達 {a['observed']['span_min']} 分鐘" if a.get("observed") else ""))})
        a["timeline"] = a["timeline"][-40:]
    return live


def ledger(state, now_ts):
    """台帳檢視：未結案事件依分級排序，加上彙總。"""
    items = [a for a in state["alerts"].values() if a.get("status") != "resolved"]
    items.sort(key=lambda a: (LEVEL_ORDER.get(a.get("level", "P2"), 9), a["opened"]))
    closed = [a for a in state["alert_log"] if a.get("status") == "resolved"]
    counts = {k: sum(1 for a in items if a.get("level") == k) for k in LEVELS}
    unowned = sum(1 for a in items if a.get("level") in ("P0", "P1") and not a.get("owner"))
    acked_not_assigned = sum(1 for a in items if a.get("acked") and not a.get("owner"))
    no_plan = sum(1 for a in items if a.get("level") in ("P0", "P1") and not a.get("covered_by"))
    return {
        "events": items[:200],
        "counts": counts,
        "open_total": len(items),
        "closed_total": len(closed),
        "unowned_p01": unowned,
        "acked_not_assigned": acked_not_assigned,
        "ack_note": "ack 只代表有人看到，不代表已指派負責人，也不代表有人到現場。",
        "no_plan_p01": no_plan,
        "planned_districts": sorted({t["district"] for t in state["tasks"] if t.get("district")}),
        "coverage_note": ("排程目前只涵蓋重點行政區，其他行政區的事件不會自動產生任務，"
                          "所以「沒排到任務」多半不是漏派，而是這一版還沒把全市納入排程。"),
        "auto_resolved": sum(1 for a in closed if "自動" in (a.get("resolve_reason") or "")),
        "manual_resolved": sum(1 for a in closed if a.get("resolve_reason") and "自動" not in a["resolve_reason"]),
        "levels": LEVELS,
        "overview": state.get("_a_overview"),
        "actions": {k: v["label"] for k, v in ACTIONS.items()},
        "thresholds": {"P1_min": P1_MIN, "P0_min": P0_MIN, "P0_unacked_min": P0_UNACKED_MIN, "cluster_n": CLUSTER_N,
                       "note": ("試行門檻，待主管確認，非官方標準。訪談提到的「觀測空站 20 分鐘」在 30 分鐘解析度下量不到，"
                                "這裡改用 30 分鐘並標示；60 分鐘的恢復目標同樣待確認是否全站適用。")},
        "ts": now_ts,
    }


OWNERS = ["值班調度 A", "值班調度 B", "板橋維運組", "新莊維運組", "土城維運組", "值班主管"]


# ---------------------------------------------------------------- 版本、冪等與政府端動作
# 政府端只做「追蹤／要求處理／協調／指派」。不建立任務、不改任務、不碰資源帳——
# 派工是 B 主線的職責，政府端插手會變成第二套派車邏輯。
ACTIONS = {
    "ack": {"label": "確認看到", "sets_owner": False},
    "assign": {"label": "指派負責人", "sets_owner": True},
    "request_ops": {"label": "要求營運處理", "sets_owner": False},
    "track": {"label": "列入追蹤", "sets_owner": False},
    "coordinate": {"label": "跨區協調", "sets_owner": False},
    "note": {"label": "處理紀錄", "sets_owner": False},
}


def ensure_version(ev):
    ev.setdefault("version", 1)
    ev.setdefault("applied_requests", {})
    return ev


def _summary(ev):
    return {"event_id": ev["id"], "version": ev["version"], "level": ev.get("level"),
            "owner": ev.get("owner"), "status": ev.get("status"),
            "acked": ev.get("acked"), "assigned_at": ev.get("assigned_at"),
            "ops_requested_at": ev.get("ops_requested_at"), "tracked": bool(ev.get("tracked"))}


def apply_action(ev, action, payload, now_ts, iso, actor="政府端"):
    """
    回傳 (body, http_status)。
    冪等：同一個 request_id 重送直接回上次結果，不重複執行，也不再撞版本。
    版本：帶了 version 且與現況不符回 409，重送不會造成第二次動作。
    """
    ensure_version(ev)
    payload = payload or {}
    req = payload.get("request_id")

    # 冪等優先於版本檢查：重送本來就會帶舊版本，那是重送不是衝突。
    if req and req in ev["applied_requests"]:
        prev = ev["applied_requests"][req]
        return {"ok": True, "idempotent": True, "replayed_from": prev["ts"],
                "action": prev["action"], "event": _summary(ev),
                "note": "同一個 request_id 已處理過，沒有重複執行"}, 200

    if action not in ACTIONS:
        return {"error": "unknown_action", "allowed": sorted(ACTIONS)}, 400

    want = payload.get("version")
    if want is not None and int(want) != ev["version"]:
        return {"error": "version_conflict",
                "message": "這個事件在你操作前已經被更新過，請重新讀取後再送一次",
                "expected_version": ev["version"], "your_version": int(want),
                "event": _summary(ev)}, 409

    ts = iso(now_ts)
    note = (payload.get("note") or "").strip()
    tl = ev.setdefault("timeline", [])

    if action == "ack":
        if not ev.get("acked"):
            ev["acked"] = ts
        if ev.get("status") == "open":
            ev["status"] = "acked"
        tl.append({"ts": ts, "action": "ack", "actor": actor,
                   "note": "確認看到。這不代表已經指派給誰，也不代表有人到現場。"})
    elif action == "assign":
        owner = (payload.get("owner") or "").strip()
        if not owner:
            return {"error": "owner_required"}, 400
        prev_owner = ev.get("owner")
        ev["owner"] = owner
        ev["assigned_at"] = ts
        if not ev.get("acked"):
            ev["acked"] = ts
        if ev.get("status") == "open":
            ev["status"] = "acked"
        tl.append({"ts": ts, "action": "assign", "actor": actor,
                   "note": (f"負責人 {prev_owner} → {owner}" if prev_owner else f"指派負責人：{owner}")})
    elif action == "request_ops":
        ev["ops_requested_at"] = ts
        ev.setdefault("ops_requests", []).append({"ts": ts, "actor": actor, "note": note})
        tl.append({"ts": ts, "action": "request_ops", "actor": actor,
                   "note": ("要求營運端處理" + (f"：{note}" if note else "")
                            + "。政府端只提出要求與追蹤，派工方案與人車由營運端決定。")})
    elif action == "track":
        ev["tracked"] = True
        tl.append({"ts": ts, "action": "track", "actor": actor,
                   "note": "列入追蹤" + (f"：{note}" if note else "")})
    elif action == "coordinate":
        targets = payload.get("districts") or []
        ev.setdefault("coordination", []).append({"ts": ts, "actor": actor, "districts": targets, "note": note})
        tl.append({"ts": ts, "action": "coordinate", "actor": actor,
                   "note": (f"跨區協調（{'、'.join(targets) or '未指定行政區'}）" + (f"：{note}" if note else "")
                            + "。這是協調紀錄，不是派車任務。")})
    elif action == "note":
        if not note:
            return {"error": "note_required"}, 400
        tl.append({"ts": ts, "action": "note", "actor": actor, "note": note})

    ev["version"] += 1
    ev["timeline"] = tl[-40:]
    if req:
        ev["applied_requests"][req] = {"ts": ts, "action": action, "version": ev["version"]}
        if len(ev["applied_requests"]) > 50:
            ev["applied_requests"].pop(next(iter(ev["applied_requests"])))
    return {"ok": True, "idempotent": False, "action": action, "event": _summary(ev)}, 200


# ---------------------------------------------------------------- 全域儀表板
STALE_WARN_MIN = 60          # 最後一筆有效觀測超過這個時間，視為資料過期
FRESH_WINDOW_BINS = 48       # 往回看 24 小時找最後一筆有效觀測


def data_freshness(pred, t_idx):
    """每站最後一筆有效觀測距今多久。缺測不是恢復，也不是中斷，要單獨看得見。"""
    ctx = pred.ctx
    t0 = max(0, t_idx - FRESH_WINDOW_BINS + 1)
    W = ctx.B[t0:t_idx + 1]
    valid = ~np.isnan(W)
    T = W.shape[0]
    idx = np.arange(T, dtype=np.int32)[:, None]
    last = np.where(valid, idx, np.int32(-1))
    np.maximum.accumulate(last, axis=0, out=last)
    last_row = last[-1]                                   # 每站最後一筆有效觀測的列號，-1 代表窗內全無
    age = np.where(last_row < 0, -1, (T - 1 - last_row) * BIN_MIN)
    return {
        "fresh": int(((age >= 0) & (age == 0)).sum()),
        "lag_30": int(((age > 0) & (age < STALE_WARN_MIN)).sum()),
        "stale_60": int((age >= STALE_WARN_MIN).sum()),
        "no_data_24h": int((age < 0).sum()),
        "window_hours": round(FRESH_WINDOW_BINS * BIN_MIN / 60, 1),
        "note": ("以回放時鐘往回看 24 小時。缺測期間站點狀態不明，"
                 "不能當成恢復，也不能當成持續中斷。"),
    }


def supply_demand(pred_df):
    """供需現況。全部來自觀測快照與模型預測，沒有需求資料。"""
    p = pred_df
    ok = p[p.status.isin(("normal", "empty", "full"))]
    by_d = []
    for d, g in p.groupby("district"):
        by_d.append({"district": d, "stations": int(len(g)),
                     "empty": int((g.status == "empty").sum()),
                     "full": int((g.status == "full").sum()),
                     "bikes": int(g.bikes.fillna(0).sum()),
                     "spaces": int(g.spaces.fillna(0).sum()),
                     "risk_empty_120": int((g.pe_120.fillna(0) >= 0.5).sum()),
                     "risk_full_120": int((g.pf_120.fillna(0) >= 0.5).sum())})
    by_d.sort(key=lambda r: -(r["empty"] + r["risk_empty_120"]))
    return {
        "stations": int(len(p)),
        "empty_now": int((p.status == "empty").sum()),
        "full_now": int((p.status == "full").sum()),
        "bikes_total": int(ok.bikes.fillna(0).sum()),
        "spaces_total": int(ok.spaces.fillna(0).sum()),
        "risk_empty_120": int((ok.pe_120.fillna(0) >= 0.5).sum()),
        "risk_full_120": int((ok.pf_120.fillna(0) >= 0.5).sum()),
        "watch": {"both_zero": int((p.status == "both_zero").sum()),
                  "stale_flat": int((p.status == "stale_flat").sum()),
                  "cap_conflict": int((p.status == "cap_conflict").sum()),
                  "no_data": int((p.status == "no_data").sum())},
        "by_district": by_d[:14],
        "not_claimed": "這裡是庫存與風險，不是需求。沒有借還交易資料，算不出有多少人想借而借不到。",
    }


# B 主線 app/tickets.py 公布的詞彙。A 只讀不改；B 改了這裡要跟著改。
TICKET_FLOW_LABEL = {"reported": "已受理", "accepted": "維修班組接單", "on_site": "現場檢查中",
                     "recovered": "已處理／回收", "verified": "驗收復役", "closed": "結案"}
SERVICE_STATE_LABEL = {"unknown": "服務狀態未知", "degraded": "服務未恢復", "restored": "服務已恢復"}
ASSET_STATE_LABEL = {"suspect": "疑似故障", "confirmed_faulty": "已確認故障", "repaired": "已維修",
                     "verified_ok": "驗收正常", "not_applicable": "不適用"}
SADDLE_LABEL = {"unknown": "未回報", "done": "已完成", "skipped": "略過", "not_applicable": "不適用"}
FIELD_CONFIRMED = ("on_site", "recovered", "verified", "closed")


def _divergence(tk):
    """
    A 任務 5：把容易被混為一談的狀態差異挑出來。
    工單流程、服務是否恢復、設備是否修好是三件事，任何一件都不能代表另外兩件。
    """
    st, svc, ast = tk.get("status"), tk.get("service_state", "unknown"), tk.get("asset_state", "suspect")
    out = []
    if st in ("recovered", "verified") and svc != "restored":
        out.append({"code": "handled_not_restored", "label": "已處理但服務未恢復",
                    "why": f"工單已到「{TICKET_FLOW_LABEL.get(st, st)}」，但服務狀態是「{SERVICE_STATE_LABEL.get(svc, svc)}」。"
                           "維修完成不等於這一站借得到車。"})
    if svc == "restored" and st not in ("verified", "closed"):
        out.append({"code": "restored_not_closed", "label": "服務已恢復但維修未結案",
                    "why": f"站點服務已恢復，但工單還在「{TICKET_FLOW_LABEL.get(st, st)}」。"
                           "站點有車不代表那台壞車已經修好或驗收。"})
    if ast == "suspect" and st in ("recovered", "verified"):
        out.append({"code": "repaired_but_unconfirmed", "label": "流程已推進但設備仍是疑似",
                    "why": "設備狀態還停在疑似故障，沒有現場確認的根因。"})
    if not tk.get("bike_no") and not tk.get("dock_id"):
        out.append({"code": "no_asset_id", "label": "沒有資產識別",
                    "why": "沒有車號也沒有柱號，只能關聯到站點，不能斷定是哪一台設備，也不能跟別的回報合併。"})
    return out


def equipment_board(state):
    """
    設備待查：用戶選項、AI 圖片觀察、現場確認分三欄呈現，不把 AI 推測寫進已確認原因。
    欄位對齊 B 主線 app/tickets.py 的工單契約；B 沒提供的一律顯示未提供，不自行填補。
    """
    rows, by_asset, unidentified, diverge = [], {}, 0, 0
    for tk in state.get("tickets", []):
        if tk.get("status") == "closed":
            continue
        asset = tk.get("asset_type") or "unknown"
        by_asset[asset] = by_asset.get(asset, 0) + 1
        dock = tk.get("dock_id") or tk.get("dock_no")
        if not tk.get("bike_no") and not dock:
            unidentified += 1
        field = [h for h in tk.get("history", []) if h.get("status") in FIELD_CONFIRMED]
        ev = tk.get("evidence") or []
        img = [e for e in ev if (e.get("kind") or e.get("type")) in ("image", "image_observation", "vision")]
        srcs = tk.get("sources") or []
        d = _divergence(tk)
        if d:
            diverge += 1
        sm = tk.get("saddle_marker") or {}
        rows.append({
            "ticket_id": tk.get("id"), "ticket_version": tk.get("version"), "sid": tk.get("sid"),
            "station": tk.get("station"), "asset_type": asset,
            "bike_no": tk.get("bike_no") or None, "dock_id": dock or None,
            "error_codes": tk.get("error_codes") or ([tk["error_code"]] if tk.get("error_code") else []),
            "status": tk.get("status"), "status_label": TICKET_FLOW_LABEL.get(tk.get("status"), tk.get("status")),
            "service_state": SERVICE_STATE_LABEL.get(tk.get("service_state", "unknown"), tk.get("service_state")),
            "asset_state": ASSET_STATE_LABEL.get(tk.get("asset_state", "suspect"), tk.get("asset_state")),
            "reports": tk.get("reports", 1), "report_ids": tk.get("report_ids") or [],
            "saddle_marker": {"status": sm.get("status", "unknown"),
                              "label": SADDLE_LABEL.get(sm.get("status", "unknown"), sm.get("status")),
                              "source": sm.get("source"), "ts": sm.get("ts")},
            "operator": {"assignee": tk.get("assignee"), "crew": tk.get("crew"), "eta": tk.get("eta")},
            "divergence": d,
            # 三欄分開，不合併
            "user_report": {"provided": bool(tk.get("issue")), "text": tk.get("issue") or None,
                            "note": tk.get("note") or None,
                            "sources": [{"source": x.get("source"), "certainty": x.get("certainty"), "ts": x.get("ts")}
                                        for x in srcs]},
            "ai_observation": ({"provided": True, "observations": img}
                               if img else {"provided": False,
                                            "why": "這張單的 evidence 沒有圖片辨識結果（C 主線的 report_image 尚未附上）"}),
            "field_check": ({"provided": True, "steps": [{"ts": h["ts"], "label": h.get("label")} for h in field]}
                            if field else {"provided": False, "why": "現場尚未回報確認"}),
        })
    return {
        "open_tickets": len(rows),
        "by_asset": by_asset,
        "without_asset_id": unidentified,
        "with_divergence": diverge,
        "rows": rows[:40],
        "rule": ("用戶描述、AI 圖片觀察、現場確認分開呈現。AI 觀察是推測，不能當成已確認原因；"
                 "沒有現場確認之前，設備故障一律是疑似。坐墊標記只是給下一位使用者的現場提醒，"
                 "不代表維修完成，也不會結束工單。"),
        "owner": "維修與派工由營運端（B 主線）主責，政府端只看進度與責任歸屬。",
        "state_note": ("工單流程、服務是否恢復、設備是否修好是三件事。"
                       "已處理不等於服務恢復；站點恢復有車也不等於那台壞車已經修好或驗收。"),
    }


def task_board(state):
    """
    人車任務摘要與未覆蓋缺口。只讀 B 主線的任務，不建立也不修改。
    欄位對齊 B 主線實際輸出（cycle／reservation_state／late_stops／on_time_stops／tightest_due_min）。
    """
    tasks = state.get("tasks", [])
    by_status, by_reservation = {}, {}
    late_total = ontime_total = drop_total = 0
    for t in tasks:
        by_status[t.get("status", "unknown")] = by_status.get(t.get("status", "unknown"), 0) + 1
        rs = t.get("reservation_state")
        if rs:
            by_reservation[rs] = by_reservation.get(rs, 0) + 1
        late = t.get("late_stops")
        ont = t.get("on_time_stops")
        if isinstance(late, list):
            late_total += len(late)
        elif isinstance(late, int):
            late_total += late
        if isinstance(ont, list):
            ontime_total += len(ont)
        elif isinstance(ont, int):
            ontime_total += ont
        drop_total += sum(1 for x in t.get("stops", []) if x.get("action") == "dropoff")

    gaps = [{"district": t.get("district"), "horizon": t.get("horizon"),
             "deficit": t.get("deficit_total"), "reason": t.get("reason"), "status": t.get("status"),
             "cycle": t.get("cycle")}
            for t in tasks if t.get("status") in ("gap_summary", "needs_cross_district", "minor_gap", "too_late")]
    active = [t for t in tasks if t.get("status") in ("planned", "dispatched", "en_route")]
    missing = [k for k in ("version", "event_id", "operator_owner")
               if not any(k in t for t in tasks)] if tasks else []
    return {
        "by_status": by_status,
        "by_reservation": by_reservation,
        "active": len(active),
        "cycles": sorted({str(t["cycle"]) for t in tasks if t.get("cycle") is not None}),
        "stop_deadlines": {"dropoff_stops": drop_total, "on_time": ontime_total, "late": late_total,
                           "note": ("逐站期限由 B 主線逐站判定。只要有一站來不及，整趟就不能標成全部準時。"
                                    "這裡顯示的是 B 給的逐站結果，政府端不重算。")},
        "uncovered": gaps[:20],
        "uncovered_count": len(gaps),
        "planned_districts": sorted({t["district"] for t in tasks if t.get("district")}),
        "contract_missing": missing,
        "contract_note": ("以下欄位尚未由 B 主線提供，政府端顯示為未提供，不自行推算："
                          + "、".join(missing)) if missing else "B 主線已提供政府端需要的任務欄位。",
        "owner": "任務由營運端建立與調整。政府端可要求處理與跨區協調，但不建立第二套派車任務。",
        "coverage_rule": "缺車看送車（dropoff）任務，缺位看運出（pickup）任務，兩者不互相認定。",
    }


def overview(state, pred, pred_df, now_ts):
    return {
        "data_ts": str(now_ts),
        "clock_source": "replay",
        "clock_source_label": "回放（2026 年 1–6 月歷史快照），不是真實時鐘",
        "supply_demand": supply_demand(pred_df),
        "freshness": data_freshness(pred, state["clock"]["t_idx"]),
        "equipment": equipment_board(state),
        "tasks": task_board(state),
    }
