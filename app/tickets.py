"""
tickets.py — 單一建單服務（B 線擁有）。

為什麼要獨立成一個模組：
2026-09-12 盤點發現建單有兩套實作——server.py 的 api_ticket_create（站點＋問題類別＋2 小時去重、
丟掉柱號）與 /api/ops/tickets（資產識別去重）。民眾端的 HTTP 與 server.py 內部呼叫都走舊那套，
所以「同站兩台不同車」會被合併成一張單、柱號結構化欄位整個遺失、重送會重建。
這個模組是唯一的建單入口，兩個路由與所有內部呼叫都委派過來。

設計原則
- 純函式：不 import server、不碰 FastAPI，帶 now 與 tickets list 進來，才能用 fixture 單測。
- 識別不捏造：沒有車號柱號就是 None，不填空字串假裝有值。
- 狀態分四組，不共用一個欄位：
    status        工單處理流程（reported→accepted→on_site→recovered→verified→closed）
    service_state 站點服務是否恢復（unknown/degraded/restored）
    asset_state   設備驗收（suspect/confirmed_faulty/repaired/verified_ok/not_applicable）
    saddle_marker 用戶是否照官方方式標記故障車（unknown/done/skipped/not_applicable）
  用戶把坐墊反轉只更新 saddle_marker，不改 status、不結案、不代表修好。
- 合併規則（見 dedup_key 與 _match）：有資產識別才敢合併，沒有識別只關聯不盲目合併。
"""
from __future__ import annotations

FLOW = ["reported", "accepted", "on_site", "recovered", "verified", "closed"]
FLOW_LABEL = {"reported": "已受理", "accepted": "維修班組接單", "on_site": "現場檢查中",
              "recovered": "已處理／回收", "verified": "驗收復役", "closed": "結案"}
SERVICE_STATE = ("unknown", "degraded", "restored")
ASSET_STATE = ("suspect", "confirmed_faulty", "repaired", "verified_ok", "not_applicable")
SADDLE = ("unknown", "done", "skipped", "not_applicable")
ASSET_LABEL = {"bike": "車輛", "dock": "車柱", "station": "站端系統", "unknown": "待判定"}
STATION_MERGE_WINDOW_S = 7200          # 沒有資產識別時，才用「同站同問題 2 小時」當作同一件

# 明確機械問題可直接建報修單；其餘保留待診斷（任務書：借不到／刷卡沒反應不等於車壞）
MECHANICAL = {"tire", "brake", "chain", "seat", "frame", "pedal", "light", "kickstand"}
UNCERTAIN = {"unsure", "cannot_borrow", "cannot_return", "payment", "account", "card", "screen"}


def _s(v):
    """空字串與空白一律收斂成 None，不讓空值冒充識別。"""
    if v is None: return None
    t = str(v).strip()
    return t or None


def normalize(body: dict) -> dict:
    """對外統一 dock_id，相容民眾端既有的 dock_no；只正規化一次。"""
    dock = _s(body.get("dock_id")) or _s(body.get("dock_no"))
    return {
        "sid": int(body["sid"]),
        "issue": _s(body.get("issue")) or "其他",
        "bike_no": _s(body.get("bike_no")),
        "dock_id": dock,
        "error_code": _s(body.get("error_code")),
        "asset_type": _s(body.get("asset_type")),
        "note": _s(body.get("note")) or "",
        "source": _s(body.get("source")) or "unknown",
        "request_id": _s(body.get("request_id")),
        "report_id": _s(body.get("report_id")),
        "evidence": body.get("evidence") or [],
        "certainty": _s(body.get("certainty")) or "stated",   # stated / observed / confirmed
    }


def dedup_key(sid: int, issue: str, bike_no=None, dock_id=None):
    """優先序：車號 > 柱號 > 站點＋問題類別。回傳 (key, basis)。"""
    if bike_no: return f"bike:{bike_no}", "bike"
    if dock_id: return f"dock:{int(sid)}:{dock_id}", "dock"
    return f"station:{int(sid)}:{issue}", "station"


def infer_asset_type(n: dict) -> str:
    if n.get("asset_type") in ASSET_LABEL: return n["asset_type"]
    if n.get("bike_no"): return "bike"
    if n.get("dock_id"): return "dock"
    return "unknown"


def ticket_key(tk: dict):
    """舊工單沒有 dedup_key，用同一套規則補算，才能跟新回報對得起來。"""
    if tk.get("dedup_key"): return tk["dedup_key"], tk.get("dedup_basis", "station")
    return dedup_key(tk["sid"], tk.get("issue", ""), _s(tk.get("bike_no")), _s(tk.get("dock_id")) or _s(tk.get("dock_no")))


def _match(tickets, n, key, basis, now_ts, window_s=STATION_MERGE_WINDOW_S, age_s=None):
    """找可以合併的既有工單。
    有資產識別：只跟同一個資產的未結案工單合併，不看時間窗（同一台壞車隔天回報還是同一台）。
    沒有資產識別：只跟「同樣也沒有資產識別」且同站同問題、2 小時內的工單合併；
                  對方有識別就不敢認定是同一件，改成關聯。"""
    for tk in tickets:
        if tk.get("status") == "closed": continue
        k, b = ticket_key(tk)
        if k != key: continue
        if basis == "station":
            if b != "station": continue
            if age_s is not None and age_s(tk) >= window_s: continue
        return tk
    return None


def _related(tickets, n):
    """沒有資產識別時，把同站未結案的工單列為關聯，但不合併。"""
    out = []
    for tk in tickets:
        if tk.get("status") == "closed": continue
        if tk.get("sid") != n["sid"]: continue
        out.append(tk["id"])
    return out[:5]


class TicketService:
    """狀態只有冪等表；工單本體存在呼叫端傳進來的 list（server 的 STATE["tickets"]）。"""

    def __init__(self):
        self.idem = {}          # request_id -> ticket_id
        self.seq = 0

    def reset(self):
        self.idem.clear(); self.seq = 0

    def _new_id(self, tickets):
        self.seq = max(self.seq, len(tickets)) + 1
        used = {t["id"] for t in tickets}
        while f"R{self.seq:03d}" in used: self.seq += 1
        return f"R{self.seq:03d}"

    def submit(self, tickets, body, *, station_name, now_iso, now_str, age_s, mechanical=None):
        """建立或合併工單。回傳 (ticket, action)，action ∈ created / merged / idempotent。
        age_s(tk) 回傳該工單距今幾秒，由呼叫端用回放時鐘算，模組本身不碰時間來源。"""
        n = normalize(body)
        if n["request_id"] and n["request_id"] in self.idem:
            tid = self.idem[n["request_id"]]
            for tk in tickets:
                if tk["id"] == tid: return tk, "idempotent"
        asset = infer_asset_type(n)
        key, basis = dedup_key(n["sid"], n["issue"], n["bike_no"], n["dock_id"])
        hit = _match(tickets, n, key, basis, now_str, age_s=age_s)
        if hit is not None:
            hit["reports"] = hit.get("reports", 1) + 1
            hit["version"] = hit.get("version", 1) + 1
            for f, v in (("bike_no", n["bike_no"]), ("dock_id", n["dock_id"]), ("error_code", n["error_code"])):
                if v and not hit.get(f): hit[f] = v          # 後來補到的識別要補上，但不覆蓋已有值
            if n["error_code"]:
                hit.setdefault("error_codes", [])
                if n["error_code"] not in hit["error_codes"]: hit["error_codes"].append(n["error_code"])
            if n["report_id"]:
                hit.setdefault("report_ids", [])
                if n["report_id"] not in hit["report_ids"]: hit["report_ids"].append(n["report_id"])
            hit.setdefault("sources", []).append({"ts": now_iso, "source": n["source"], "certainty": n["certainty"]})
            hit["history"].append({"ts": now_iso, "status": hit["status"],
                                   "label": f"重複回報合併（第 {hit['reports']} 次，依{ASSET_LABEL.get(basis, basis)}識別 {key.split(':', 1)[1]}）"})
            if n["request_id"]: self.idem[n["request_id"]] = hit["id"]
            return hit, "merged"

        mech = MECHANICAL if mechanical is None else mechanical
        direct = bool(n["bike_no"] or n["dock_id"]) or (body.get("symptom_keys") and set(body["symptom_keys"]) & mech)
        tk = {
            "id": self._new_id(tickets), "version": 1,
            "sid": n["sid"], "station": station_name,
            "issue": n["issue"], "note": n["note"],
            "bike_no": n["bike_no"], "dock_id": n["dock_id"], "error_code": n["error_code"],
            "error_codes": [n["error_code"]] if n["error_code"] else [],
            "asset_type": asset, "dedup_key": key, "dedup_basis": basis,
            "reports": 1, "ts": now_str,
            "status": "reported",                       # 工單處理流程
            "service_state": "unknown",                 # 站點服務是否恢復
            "asset_state": "suspect" if asset != "unknown" else "not_applicable",   # 設備驗收
            "saddle_marker": {"status": "unknown", "source": None, "ts": None},
            "diagnosis": "direct_repair" if direct else "pending_triage",
            "sources": [{"ts": now_iso, "source": n["source"], "certainty": n["certainty"]}],
            "report_ids": [n["report_id"]] if n["report_id"] else [],
            "evidence": list(n["evidence"]),
            "related_ids": _related(tickets, n) if basis == "station" else [],
            "assignee": None, "eta": None, "crew": None,
            "history": [{"ts": now_iso, "status": "reported",
                          "label": "已受理（" + ASSET_LABEL.get(asset, asset)
                                   + (f"：{n['bike_no']}" if n["bike_no"] else (f"：{n['dock_id']} 號柱" if n["dock_id"] else "，無資產識別"))
                                   + (f"，錯誤碼 {n['error_code']}" if n["error_code"] else "") + "）"}],
        }
        tickets.insert(0, tk)
        if n["request_id"]: self.idem[n["request_id"]] = tk["id"]
        return tk, "created"

    def set_saddle(self, tk, status, *, source, now_iso):
        """用戶端回報坐墊標記。只更新附加欄位，不動 status、不結案、不代表修好。"""
        if status not in SADDLE: return None, "bad_status"
        tk["saddle_marker"] = {"status": status, "source": source, "ts": now_iso}
        tk["version"] = tk.get("version", 1) + 1
        tk["history"].append({"ts": now_iso, "status": tk["status"],
                              "label": f"用戶坐墊標記：{ {'done': '已反轉坐墊', 'skipped': '未操作／已離開', 'not_applicable': '不適用', 'unknown': '未知'}[status] }"
                                       "（僅為現場提醒，非維修確認）"})
        return tk, "ok"

    def transition(self, tk, to, *, actor, now_iso, version=None, note=None, crew=None, eta=None):
        """工單處理流程推進。帶 version 做樂觀鎖：版本過期回 conflict，重送不重複推進。"""
        if version is not None and int(version) != int(tk.get("version", 1)):
            return None, "conflict"
        if to not in FLOW: return None, "bad_status"
        cur, nxt = FLOW.index(tk["status"]), FLOW.index(to)
        if nxt == cur: return tk, "noop"
        if nxt < cur: return None, "backwards"
        tk["status"] = to
        if crew: tk["crew"] = crew; tk["assignee"] = crew
        if eta is not None: tk["eta"] = eta
        if to == "recovered": tk["asset_state"] = "repaired" if tk["asset_type"] != "unknown" else tk["asset_state"]
        if to == "verified": tk["asset_state"] = "verified_ok" if tk["asset_type"] != "unknown" else tk["asset_state"]
        tk["version"] = tk.get("version", 1) + 1
        tk["history"].append({"ts": now_iso, "status": to,
                              "label": FLOW_LABEL[to] + (f"｜{actor}" if actor else "") + (f"｜{note}" if note else "")})
        return tk, "ok"


SERVICE = TicketService()
