"""
events.py — 政府端事件台帳：把「告警」升級成有生命週期、有分級、有原因與證據的事件。

界線：
- 觀測是每 30 分鐘一筆的庫存快照。「空站多久」只能給觀測下界（第一筆零快照到現在）
  與可能上界（最後一筆正常觀測到現在），不能給精確值。缺測不算恢復。
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
    "P0": {"label": "P0", "desc": "超過 60 分鐘、500 公尺內沒有替代站、且沒有可執行的恢復計畫；或 P1 逾時無人接手；或同區站群同時失效", "notify": "主管與值班主管"},
    "P1": {"label": "P1", "desc": "觀測到的空站／滿站已達 30 分鐘以上；若 500 公尺內還有替代站，最高只到這一級", "notify": "值班調度"},
    "P2": {"label": "P2", "desc": "預測 60–180 分鐘後將失衡，進入值班排程", "notify": "不逐則打擾主管"},
    "C":  {"label": "長期", "desc": "連續 12 小時以上沒車，或超過 24 小時沒看到過正常庫存：這不是今天的突發事件，派車解決不了，要查核營運狀態", "notify": "營運查核，不打擾主管"},
    "W":  {"label": "待查", "desc": "雙零、長期不變、容量矛盾：狀態待查核，不直接派車", "notify": "不通知"},
}
LEVEL_ORDER = {"P0": 0, "P1": 1, "P2": 2, "C": 3, "W": 4}

CHRONIC_MIN = 720        # 觀測下界達 12 小時 → 長期性，不是今天的事件
CHRONIC_UPPER_MIN = 1440 # 超過 24 小時沒看過正常庫存 → 同上

P1_MIN = 30          # 觀測下界達此值 → P1
P0_MIN = 60          # 觀測下界達此值且無可執行恢復計畫 → P0
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
    lower_min：第一筆零快照到現在（確定已經這樣多久）
    upper_min：最後一筆「確認正常」的觀測到現在（最多可能多久）
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
        "lower_min": int((t_idx - start) * BIN_MIN),
        "upper_min": None if j < 0 else int((t_idx - j) * BIN_MIN),
        "snapshots": int(t_idx - start + 1),
        "started_obs": str(bins[start]),
        "last_data_ts": None if k < 0 else str(bins[k]),
        "gap_inside": gap,
    }


# ---------------------------------------------------------------- 覆蓋這一站的任務
def covering_task(tasks, sid):
    """找出目前有哪一張任務要送車到這一站。回傳 (task, stop) 或 (None, None)。"""
    best = (None, None)
    for tk in tasks:
        if tk.get("status") not in ("planned", "dispatched", "en_route", "proposed", "too_late"):
            continue
        for s in tk.get("stops", []):
            if s.get("action") == "dropoff" and s.get("sid") == sid:
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
    tk, stop = covering_task(tasks, sid)
    if tk is None:
        cross = [t for t in district_task_state(tasks, ev.get("district")) if t.get("status") == "needs_cross_district"]
        if cross:
            return {"code": "no_supply", "label": CAUSES["no_supply"], "confident": True,
                    "evidence": [{"kind": "task", "ref": cross[0]["id"], "text": cross[0].get("reason", "區內無可供給站")}]}
        return {"code": "no_task", "label": CAUSES["no_task"], "confident": True,
                "evidence": [{"kind": "task", "ref": "—", "text": "目前排程中沒有任何一張任務要送車到這一站"}]}
    if tk["status"] == "too_late":
        return {"code": "too_late", "label": CAUSES["too_late"], "confident": True,
                "evidence": [{"kind": "task", "ref": tk["id"], "text": tk.get("reason", "決策到抵達的時間趕不上目標時間")}]}
    if tk["status"] in ("dispatched", "en_route"):
        return {"code": "en_route", "label": CAUSES["en_route"], "confident": True,
                "evidence": [{"kind": "task", "ref": tk["id"], "text": f"{tk['id']} 已出車（模擬狀態），預計 {str(stop.get('arrives_by', ''))[11:16]} 抵達本站"}]}
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

    # 第一段：基準分級（不看站群）
    for a in live:
        a.setdefault("timeline", [{"ts": a["opened"], "action": "opened", "actor": "系統", "note": a["message"]}])
        a.setdefault("owner", None)
        a["escalated"] = None
        if a["type"] in WATCH_TYPES:
            base[a["id"]] = "W"
            continue
        if a.get("sid") is None or a["sid"] < 0:          # 活動情境事件
            base[a["id"]] = "P2"
            continue

        kind = "empty" if "empty" in a["type"] else "full"
        a["observed"] = observed_run(ctx, bins, t_idx, a["sid"], kind) if a["type"] in ONGOING_TYPES else None

        tk, stop = covering_task(state["tasks"], a["sid"])
        if tk and stop and stop.get("arrives_by"):
            a["eta"] = str(stop["arrives_by"])
            a["eta_source"] = f"{tk['id']}（{tk['status']}，模擬派工狀態）"
            a["covered_by"] = tk["id"]
        else:
            a["eta"] = a["eta_source"] = a["covered_by"] = None

        a["cause"] = infer_cause(a, state, now_ts)
        a["alt"] = alternatives(pred_df, pred.neighbors, a["sid"], kind) if a["type"] in ONGOING_TYPES else None

        lvl = "P2"
        if a["type"] in ONGOING_TYPES and a.get("observed"):
            o = a["observed"]
            lower, upper = o["lower_min"], o["upper_min"]
            chronic = lower >= CHRONIC_MIN or upper is None or upper >= CHRONIC_UPPER_MIN
            executable = bool(tk and tk["status"] in ("planned", "dispatched", "en_route"))
            has_alt = bool(a["alt"] and a["alt"]["has"])
            if chronic:
                lvl = "C"
            elif lower >= P0_MIN and not executable and not has_alt:
                lvl = "P0"
                a["escalated"] = f"已達 {lower} 分鐘、500 公尺內沒有替代站、也沒有可執行的恢復計畫"
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
                                             (f"：觀測到的中斷已達 {a['observed']['lower_min']} 分鐘" if a.get("observed") else ""))})
        a["timeline"] = a["timeline"][-40:]
    return live


def ledger(state, now_ts):
    """台帳檢視：未結案事件依分級排序，加上彙總。"""
    items = [a for a in state["alerts"].values() if a.get("status") != "resolved"]
    items.sort(key=lambda a: (LEVEL_ORDER.get(a.get("level", "P2"), 9), a["opened"]))
    closed = [a for a in state["alert_log"] if a.get("status") == "resolved"]
    counts = {k: sum(1 for a in items if a.get("level") == k) for k in LEVELS}
    unowned = sum(1 for a in items if a.get("level") in ("P0", "P1") and not a.get("owner"))
    no_plan = sum(1 for a in items if a.get("level") in ("P0", "P1") and not a.get("covered_by"))
    return {
        "events": items[:200],
        "counts": counts,
        "open_total": len(items),
        "closed_total": len(closed),
        "unowned_p01": unowned,
        "no_plan_p01": no_plan,
        "planned_districts": sorted({t["district"] for t in state["tasks"] if t.get("district")}),
        "coverage_note": ("排程目前只涵蓋重點行政區，其他行政區的事件不會自動產生任務，"
                          "所以「沒排到任務」多半不是漏派，而是這一版還沒把全市納入排程。"),
        "auto_resolved": sum(1 for a in closed if "自動" in (a.get("resolve_reason") or "")),
        "manual_resolved": sum(1 for a in closed if a.get("resolve_reason") and "自動" not in a["resolve_reason"]),
        "levels": LEVELS,
        "thresholds": {"P1_min": P1_MIN, "P0_min": P0_MIN, "P0_unacked_min": P0_UNACKED_MIN, "cluster_n": CLUSTER_N,
                       "note": ("試行門檻，待主管確認，非官方標準。訪談提到的「觀測空站 20 分鐘」在 30 分鐘解析度下量不到，"
                                "這裡改用 30 分鐘並標示；60 分鐘的恢復目標同樣待確認是否全站適用。")},
        "ts": now_ts,
    }


OWNERS = ["值班調度 A", "值班調度 B", "板橋維運組", "新莊維運組", "土城維運組", "值班主管"]
