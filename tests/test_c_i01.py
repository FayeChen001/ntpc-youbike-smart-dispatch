"""
I01：三端同一事件整合驗收（C 建報 → B 接手 → 修復 → 驗收）。

驗收矩陣 I01 要求「同一 ticket_id 追蹤，三端狀態一致；修復不等於自動驗收」。
三端的程式在同一個 server 上，所以這支測試不需要 A、B 的人在場：
用各端自己對外的 HTTP 介面走完一遍，再交叉比對同一張工單在三端讀到的值。

  C 端讀 GET /api/c/report/{rid}          → ticket（C 只讀不改）
  B 端讀 GET /api/ops/tickets/{tid}       → 工單本體
  A 端讀 GET /api/ledger                  → overview.equipment.rows（設備待查）

預期值不取實作輸出當答案：狀態機順序與中文標籤在本檔開頭以獨立常數寫死，
和 app/tickets.py 各自維護。B 改了詞彙這支會失敗，那正是要它失敗。

用法：YB_BASE=http://127.0.0.1:8791 python3 tests/test_c_i01.py
"""
import json, os, sys, urllib.request, urllib.error

BASE = os.environ.get("YB_BASE", "http://127.0.0.1:8791")
if BASE.rstrip("/").endswith(":8787"):
    print("拒絕執行：8787 是三端共用的展示伺服器，本測試會呼叫 /api/reset 清掉三端回放狀態。")
    print("請另開一個 port（例如 8791）再跑：YB_BASE=http://127.0.0.1:8791 python3 tests/test_c_i01.py")
    sys.exit(2)

# ---- 獨立 fixture：不從 /api/ops/contract 取，刻意跟實作分開維護 ----
EXPECT_FLOW = ["reported", "accepted", "on_site", "recovered", "verified", "closed"]
EXPECT_STATUS_LABEL = {"reported": "已受理", "accepted": "維修班組接單", "on_site": "現場檢查中",
                       "recovered": "已處理／回收", "verified": "驗收復役", "closed": "結案"}
# 修復（recovered）時設備是「已處理」，只有驗收（verified）才是「已驗收」。
EXPECT_ASSET_AFTER = {"recovered": "repaired", "verified": "verified_ok"}
VERIFIED_WORDS = ("已驗收", "驗收復役", "verified")

fails = []
PASSED = [0]


def call(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers={"content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode() or "{}")


GET = lambda p: call("GET", p)
POST = lambda p, b=None: call("POST", p, b if b is not None else {})


def check(name, cond, evidence=""):
    ok = bool(cond)
    print(("  PASS " if ok else "  FAIL ") + name + (f"  [{evidence}]" if evidence else ""))
    if ok:
        PASSED[0] += 1
    else:
        fails.append(name)


def a_row(tid):
    """A 政府端看到的同一張工單。"""
    _, led = GET("/api/ledger")
    eq = ((led.get("overview") or {}).get("equipment") or {})
    for r in eq.get("rows", []):
        if r.get("ticket_id") == tid:
            return r
    return None


def b_ticket(tid):
    s, tk = GET(f"/api/ops/tickets/{tid}")
    return tk if s == 200 else None


def c_report(rid):
    s, r = GET(f"/api/c/report/{rid}")
    return r if s == 200 else None


# ============================================================ 前置
POST("/api/reset")
_, sts = GET("/api/stations?adjusted=1")
SID = sts["stations"][0]["sid"]
STATION = sts["stations"][0]["name"]

print(f"\n[I01] 三端同一事件：站點 {STATION}（sid={SID}）")

REQ = "i01-closed-loop-1"
BODY = {"sid": SID, "stage": "before_borrow", "problem": "chain", "bike_no": "YB2-I01",
        "dock_no": "09", "free_text": "鏈條掉在地上", "request_id": REQ}
s, rep = POST("/api/c/report", BODY)
RID, TID = rep.get("id"), rep.get("ticket_id")
check("I01-0 C 建報成功並產生工單", s == 200 and RID and TID, f"report={RID} ticket={TID}")
if not TID:
    print("\n前置失敗，後續無法比對。")
    sys.exit(1)

# ---- 1. 同一個 ticket_id 在三端都查得到，且都關聯回同一個 report_id ----
bt, ar, cr = b_ticket(TID), a_row(TID), c_report(RID)
check("I01a B 端查得到同一張工單", bool(bt and bt.get("id") == TID), f"B: {bt and bt.get('id')}")
check("I01b A 端設備待查列出同一張工單", bool(ar), f"A: {ar and ar.get('ticket_id')}")
check("I01c C 端回報指向同一張工單", bool(cr and (cr.get("ticket") or {}).get("id") == TID),
      f"C: {(cr or {}).get('ticket', {}).get('id')}")
check("I01d B 端工單帶得回 C 的 report_id", RID in (bt or {}).get("report_ids", []),
      f"report_ids={(bt or {}).get('report_ids')}")
check("I01e A 端也看得到同一個 report_id", RID in (ar or {}).get("report_ids", []),
      f"report_ids={(ar or {}).get('report_ids')}")
check("I01f 柱號 dock_no 正規化後三端都是 dock_id", (bt or {}).get("dock_id") == "09" and (ar or {}).get("dock_id") == "09",
      f"B={(bt or {}).get('dock_id')} A={(ar or {}).get('dock_id')}")

# ---- 2. 重送同一個請求，三端工單數都不變 ----
_, tks_before = GET("/api/tickets")
s2, again = POST("/api/c/report", BODY)
_, tks_after = GET("/api/tickets")
check("I01g 同 request_id 重送回同一張工單", again.get("ticket_id") == TID and again.get("idempotent") is True,
      f"ticket={again.get('ticket_id')} idempotent={again.get('idempotent')}")
check("I01h 重送後工單總數不變", len(tks_after["tickets"]) == len(tks_before["tickets"]),
      f"{len(tks_before['tickets'])} → {len(tks_after['tickets'])}")
check("I01i 重送後 A 端也沒有多一筆", sum(1 for r in ((GET('/api/ledger')[1].get('overview') or {}).get('equipment') or {}).get('rows', []) if r.get('ticket_id') == TID) == 1)

# ---- 3. 剛建單時：三端都不能顯示已驗收 ----
for endname, obj, key in (("C", (cr or {}).get("ticket") or {}, "status"),
                          ("B", bt or {}, "status"),
                          ("A", ar or {}, "status")):
    check(f"I01j-{endname} 建單當下狀態是 reported", obj.get(key) == "reported", f"{obj.get(key)}")
check("I01k 建單當下設備狀態是疑似，不是已驗收", (bt or {}).get("asset_state") == "suspect",
      f"asset_state={(bt or {}).get('asset_state')}")
check("I01l A 端不把 AI 推測寫進已確認原因（本單無照片）",
      ((ar or {}).get("ai_observation") or {}).get("provided") is False,
      f"ai_observation.provided={((ar or {}).get('ai_observation') or {}).get('provided')}")
check("I01m A 端現場確認欄位在沒人到場前為未提供",
      ((ar or {}).get("field_check") or {}).get("provided") is False)

# ---- 4. B 接手：三端狀態一起走到 accepted ----
ver = (b_ticket(TID) or {}).get("version")
s, res = POST(f"/api/ops/tickets/{TID}/transition",
              {"to": "accepted", "version": ver, "crew": "維修二班", "eta": "08:40", "actor": "中控"})
check("I01n B 接手成功", s == 200 and res.get("status") == "accepted", f"status={res.get('status')}")
bt, ar, cr = b_ticket(TID), a_row(TID), c_report(RID)
ct = (cr or {}).get("ticket") or {}
check("I01o 接手後三端 status 一致",
      bt.get("status") == ar.get("status") == ct.get("status") == "accepted",
      f"B={bt.get('status')} A={ar.get('status')} C={ct.get('status')}")
check("I01p 接手後三端顯示的中文標籤一致且等於獨立對照表",
      ct.get("status_label") == ar.get("status_label") == EXPECT_STATUS_LABEL["accepted"],
      f"C={ct.get('status_label')} A={ar.get('status_label')} 對照={EXPECT_STATUS_LABEL['accepted']}")
check("I01q 負責人與 ETA 三端一致", ct.get("crew") == "維修二班" and (ar.get("operator") or {}).get("crew") == "維修二班"
      and ct.get("eta") == "08:40", f"C crew={ct.get('crew')} eta={ct.get('eta')} A crew={(ar.get('operator') or {}).get('crew')}")

# ---- 5. C 的坐墊標記：三端都看得到，但不推進工單 ----
before_status, before_ver = bt.get("status"), bt.get("version")
s, sad = POST(f"/api/c/report/{RID}/saddle", {"status": "done"})
bt2, ar2 = b_ticket(TID), a_row(TID)
check("I01r 坐墊標記寫進工單且 B 端讀得到 done",
      (bt2.get("saddle_marker") or {}).get("status") == "done", f"{(bt2.get('saddle_marker') or {}).get('status')}")
check("I01s A 端也讀得到同一個坐墊標記",
      ((ar2 or {}).get("saddle_marker") or {}).get("status") == "done",
      f"{((ar2 or {}).get('saddle_marker') or {}).get('label')}")
check("I01t 坐墊標記不推進工單狀態", bt2.get("status") == before_status,
      f"{before_status} → {bt2.get('status')}")
check("I01u 坐墊標記不改設備驗收狀態", bt2.get("asset_state") == "suspect",
      f"asset_state={bt2.get('asset_state')}")
check("I01v 坐墊標記仍會讓工單版本前進（有留痕）", bt2.get("version") == before_ver + 1,
      f"version {before_ver} → {bt2.get('version')}")

# ---- 6. 現場 → 修復。關鍵：修復不等於驗收 ----
POST(f"/api/ops/tickets/{TID}/transition", {"to": "on_site", "version": b_ticket(TID).get("version"), "actor": "維修二班"})
ver = b_ticket(TID).get("version")
s, res = POST(f"/api/ops/tickets/{TID}/transition", {"to": "recovered", "version": ver, "actor": "維修二班", "note": "換鏈條"})
bt, ar, cr = b_ticket(TID), a_row(TID), c_report(RID)
ct = (cr or {}).get("ticket") or {}
check("I01w 修復後三端 status 一致為 recovered",
      bt.get("status") == ar.get("status") == ct.get("status") == "recovered",
      f"B={bt.get('status')} A={ar.get('status')} C={ct.get('status')}")
check("I01x 修復後設備狀態是已處理，不是已驗收",
      bt.get("asset_state") == EXPECT_ASSET_AFTER["recovered"],
      f"asset_state={bt.get('asset_state')}（對照表要求 repaired）")
check("I01y 修復不等於驗收：三端都不得出現驗收字樣",
      not any(w in json.dumps({"C": ct, "A": ar}, ensure_ascii=False) for w in VERIFIED_WORDS),
      "C／A 顯示內容不含「已驗收／驗收復役／verified」")
check("I01z A 端主動標示「已處理但服務未恢復」的差異",
      any(d.get("code") == "handled_not_restored" for d in (ar.get("divergence") or [])),
      f"divergence={[d.get('code') for d in (ar.get('divergence') or [])]}")
check("I01aa 修復後站點服務狀態仍是未知，不自動宣稱恢復",
      bt.get("service_state") == "unknown" and ct.get("service_state") == "unknown",
      f"B={bt.get('service_state')} C={ct.get('service_state')}")

# ---- 7. 舊 version 重送：409，且不重複推進 ----
stale = ver  # 已經被 recovered 那一次用掉
s_conf, conf = POST(f"/api/ops/tickets/{TID}/transition", {"to": "verified", "version": stale, "actor": "中控"})
check("I01ab 帶舊 version 推進回 409", s_conf == 409, f"HTTP {s_conf} error={conf.get('error')}")
check("I01ac 409 之後工單沒有被推進", b_ticket(TID).get("status") == "recovered",
      f"status={b_ticket(TID).get('status')}")

# ---- 8. 驗收：三端一起走到 verified ----
ver = b_ticket(TID).get("version")
s, res = POST(f"/api/ops/tickets/{TID}/transition", {"to": "verified", "version": ver, "actor": "中控"})
bt, ar, cr = b_ticket(TID), a_row(TID), c_report(RID)
ct = (cr or {}).get("ticket") or {}
check("I01ad 驗收後三端 status 一致為 verified",
      bt.get("status") == ar.get("status") == ct.get("status") == "verified",
      f"B={bt.get('status')} A={ar.get('status')} C={ct.get('status')}")
check("I01ae 驗收後設備狀態才是 verified_ok",
      bt.get("asset_state") == EXPECT_ASSET_AFTER["verified"], f"asset_state={bt.get('asset_state')}")
check("I01af 驗收的中文標籤等於獨立對照表",
      ct.get("status_label") == EXPECT_STATUS_LABEL["verified"],
      f"{ct.get('status_label')} vs {EXPECT_STATUS_LABEL['verified']}")

# ---- 9. 使用者離開不結案 ----
s, left = POST(f"/api/c/report/{RID}/leave")
bt_after = b_ticket(TID)
check("I01ag 使用者離開後工單仍存在且未結案",
      bt_after is not None and bt_after.get("status") != "closed", f"status={bt_after and bt_after.get('status')}")
check("I01ah 使用者離開後 A 端仍看得到這張工單", a_row(TID) is not None)
check("I01ai 使用者離開只留痕，不改回報狀態", left.get("status") == "routed_repair" and left.get("user_left") is True,
      f"status={left.get('status')} user_left={left.get('user_left')}")

# ---- 10. 狀態機順序本身：不得倒退 ----
s_back, back = POST(f"/api/ops/tickets/{TID}/transition", {"to": "accepted", "version": b_ticket(TID).get("version")})
check("I01aj 工單不可倒退", s_back == 400 and back.get("error") == "backwards", f"HTTP {s_back} {back.get('error')}")
_, contract = GET("/api/ops/contract")
check("I01ak B 公布的狀態機順序與獨立對照表一致",
      (contract.get("states") or {}).get("status") == EXPECT_FLOW,
      f"{(contract.get('states') or {}).get('status')}")

# ---- 11. 三端 reset 一致：清掉後三端都不留殘影 ----
POST("/api/reset")
s_c, _ = GET(f"/api/c/report/{RID}")
s_b, _ = GET(f"/api/ops/tickets/{TID}")
check("I01al reset 後 C 端回報已清除", s_c == 404, f"HTTP {s_c}")
check("I01am reset 後 B 端工單已清除", s_b == 404, f"HTTP {s_b}")
check("I01an reset 後 A 端設備待查不再有這張工單", a_row(TID) is None)
_, sm = GET("/api/c/saddle_markers")
check("I01ao reset 後坐墊標記不留指向已刪除工單的殘影",
      not [m for m in sm.get("markers", []) if m.get("ticket_id") == TID], f"markers={len(sm.get('markers', []))}")
s_again, again2 = POST("/api/c/report", BODY)
check("I01ap reset 後同一個 request_id 會重新建單，不是回舊殘影",
      again2.get("idempotent") is False and again2.get("ticket_action") == "created",
      f"idempotent={again2.get('idempotent')} action={again2.get('ticket_action')}")
POST("/api/reset")

print(f"\n=== I01 三端整合：通過 {PASSED[0]}　失敗 {len(fails)} ===")
for f in fails:
    print("  未通過：" + f)
sys.exit(1 if fails else 0)
