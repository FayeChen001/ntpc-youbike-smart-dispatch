"""
test_a_i01_loop.py — I01：同一個 report 送出 → 接手 → 修復 → 驗收，
三端用同一個 ticket_id 追蹤，A 端狀態一致，且「修復」不等於「自動驗收」。

從 A 主線的視角打真實 HTTP，不繞過任何一端的實作。
期望值手寫。

執行（伺服器要先跑起來）：
    YB_BASE=http://127.0.0.1:8788 python3 tests/test_a_i01_loop.py
"""
import json
import os
import sys
import urllib.request

BASE = os.environ.get("YB_BASE", "http://127.0.0.1:8788")
FAILS = []


def req(method, path, body=None, timeout=60):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(BASE + path, data=data, method=method,
                               headers={"content-type": "application/json"})
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode() or "{}")


def check(name, got, want):
    ok = got == want
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}: got={got!r} want={want!r}")
    if not ok:
        FAILS.append(name)


def check_true(name, cond, detail=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}{(' — ' + detail) if detail else ''}")
    if not cond:
        FAILS.append(name)


def a_row(tid):
    """A 端全域儀表板裡這張工單的那一列。"""
    st, d = req("GET", "/api/ledger")
    assert st == 200, d
    for r in d["overview"]["equipment"]["rows"]:
        if r["ticket_id"] == tid:
            return r
    return None


def test_ai_column():
    """附圖回報：AI 觀察要獨立成欄，觀察與『照片無法確認』分開，且不得寫進已確認原因。"""
    SID = 300
    print("附加：AI 圖片觀察欄位")
    st, rep = req("POST", "/api/c/report",
                  {"request_id": "i01img-" + os.urandom(4).hex(), "sid": SID, "problem": "unsure",
                   "stage": "before_borrow", "bike_no": "YB-IMG-" + os.urandom(3).hex().upper(), "image_confirmed": True,
                   "image_analysis": {"ok": True, "model_source": "測試樁（非真實模型）",
                                      "observations": ["前輪明顯扁平", "輪胎側面有裂痕"],
                                      "uncertainties": ["無法從照片確認煞車是否有效"],
                                      "suspected_asset_type": "bike",
                                      "requires_manual_review": True}})
    check("附圖回報建立", st, 200)
    tid = rep.get("ticket_id")
    if not tid:
        print(f"  [SKIP] 這次分流沒有開單（status={rep.get('status')}），AI 欄位無法從工單驗")
        return
    r = a_row(tid)
    check_true("A 端找得到這張單", r is not None)
    check("② AI 觀察有提供", r["ai_observation"]["provided"], True)
    texts = [x["text"] for x in r["ai_observation"]["observations"]]
    check("觀察內容照抄", texts, ["前輪明顯扁平", "輪胎側面有裂痕"])
    check("不確定項獨立一欄", r["ai_observation"]["uncertainties"], ["無法從照片確認煞車是否有效"])
    check("標示模型來源", r["ai_observation"]["model_source"], "測試樁（非真實模型）")
    check_true("附註說明照片不能確認煞車與內部電子故障",
               "不能憑照片確認煞車功能" in r["ai_observation"]["caveat"])
    check("① 用戶描述仍獨立", r["user_report"]["provided"], True)
    check("③ 現場確認仍未提供", r["field_check"]["provided"], False)
    check("設備狀態仍是疑似（AI 推測不得升格為已確認）", r["asset_state"], "疑似故障")


def main():
    SID = 172
    # 每次跑都用唯一車號，測試才不會相依於伺服器既有狀態（可重複執行）
    TAG = os.urandom(3).hex().upper()
    BIKE = f"YB-I01-{TAG}"
    BIKE2 = f"YB-I01B-{TAG}"
    print(f"對象伺服器 {BASE}")
    st, _ = req("GET", "/api/state")
    if st != 200:
        print("伺服器沒有回應，先啟動 uvicorn 再跑這個測試")
        return 2

    print("步驟 1　C 端送出明確機械症狀的回報（無圖片、單一 request_id）")
    RQ = "i01-" + os.urandom(4).hex()
    st, rep = req("POST", "/api/c/report",
                  {"request_id": RQ, "sid": SID, "problem": "chain", "stage": "before_borrow",
                   "bike_no": BIKE, "dock_id": "09", "free_text": "鏈條掉下來卡住"})
    check("回報建立", st, 200)
    rid = rep.get("id")
    tid = rep.get("ticket_id")
    check_true("回報有 report_id", bool(rid), f"report_id={rid}")
    check_true("機械症狀直接開出工單", bool(tid), f"ticket_id={tid}")

    print("步驟 1b　同 request_id 重送不得重複建單（C01 的 A 端可見部分）")
    st, rep2 = req("POST", "/api/c/report",
                   {"request_id": RQ, "sid": SID, "problem": "chain", "stage": "before_borrow",
                    "bike_no": BIKE, "dock_id": "09"})
    check("重送回傳碼", st, 200)
    check("重送標記冪等", rep2.get("idempotent"), True)
    check("重送仍是同一個 ticket_id", rep2.get("ticket_id"), tid)

    print("步驟 2　A 端看得到同一個 ticket_id，三欄分開且未把用戶自述當成已確認")
    r = a_row(tid)
    check_true("A 端找得到這張工單", r is not None, f"ticket_id={tid}")
    check("A 端 ticket_id 一致", r["ticket_id"], tid)
    check("車號帶到 A 端", r["bike_no"], BIKE)
    check("柱號統一為 dock_id", r["dock_id"], "09")
    check("① 用戶描述有提供", r["user_report"]["provided"], True)
    check("② AI 觀察未提供（本次沒有附圖）", r["ai_observation"]["provided"], False)
    check("③ 現場確認未提供（還沒到現場）", r["field_check"]["provided"], False)
    check("設備狀態為疑似", r["asset_state"], "疑似故障")
    check("服務狀態未知", r["service_state"], "服務狀態未知")
    check_true("回報 id 有回連", rid in (r.get("report_ids") or []),
               f"report_ids={r.get('report_ids')}")

    print("步驟 3　B 端接手：accepted → on_site")
    for to, want_label in (("accepted", "維修班組接單"), ("on_site", "現場檢查中")):
        st, _ = req("POST", f"/api/ops/tickets/{tid}/transition",
                    {"to": to, "actor": "三重維運組", "crew": "三重維運組"})
        check(f"推進到 {to}", st, 200)
        r = a_row(tid)
        check(f"A 端流程狀態＝{want_label}", r["status_label"], want_label)
    check("A 端看到營運負責人", r["operator"]["assignee"], "三重維運組")
    check("③ 現場確認已提供", r["field_check"]["provided"], True)

    print("步驟 4　修復（recovered）：設備是已維修，但不得自動變成驗收，服務也不得自動恢復")
    st, _ = req("POST", f"/api/ops/tickets/{tid}/transition",
                {"to": "recovered", "actor": "三重維運組"})
    check("推進到 recovered", st, 200)
    r = a_row(tid)
    check("流程狀態", r["status_label"], "已處理／回收")
    check("設備狀態＝已維修", r["asset_state"], "已維修")
    check_true("設備狀態不是驗收正常", r["asset_state"] != "驗收正常")
    check("服務狀態仍未知（修復不等於站點借得到車）", r["service_state"], "服務狀態未知")
    codes = [x["code"] for x in r["divergence"]]
    check_true("A 端標出『已處理但服務未恢復』", "handled_not_restored" in codes, f"divergence={codes}")

    print("步驟 5　驗收（verified）才是驗收")
    st, _ = req("POST", f"/api/ops/tickets/{tid}/transition",
                {"to": "verified", "actor": "三重維運組"})
    check("推進到 verified", st, 200)
    st, tk = req("GET", f"/api/ops/tickets/{tid}")
    check("工單設備狀態", tk.get("asset_state"), "verified_ok")
    check("工單流程狀態", tk.get("status"), "verified")

    print("步驟 6　全程同一個 ticket_id，且 A 端不自行建立工單")
    st, dedup = req("GET", "/api/ops/tickets/dedup")
    ids = [x["id"] for x in dedup["tickets"]]
    check("這個 ticket_id 只出現一次", ids.count(tid), 1)
    mine = [x for x in dedup["tickets"] if x["id"] == tid][0]
    check("去重依據是車號", mine["keyed_by"], "bike")

    print("步驟 7　同站不同車不得合併（I02 的 A 端可見部分）")
    st, rep3 = req("POST", "/api/c/report",
                   {"request_id": "i01b-" + os.urandom(4).hex(), "sid": SID, "problem": "tire",
                    "stage": "before_borrow", "bike_no": BIKE2})
    tid2 = rep3.get("ticket_id")
    check_true("另一台車另開一張單", bool(tid2) and tid2 != tid, f"{tid} vs {tid2}")
    r2 = a_row(tid2)
    check_true("A 端兩張單並存", r2 is not None and r2["ticket_id"] == tid2)

    print("步驟 8　重連補狀態：GET 不得有副作用（I03 的 A 端可見部分）")
    st, d1 = req("GET", "/api/ledger")
    before = (len(d1["overview"]["equipment"]["rows"]), d1["open_total"])
    for _ in range(3):
        req("GET", "/api/ledger")
    st, d2 = req("GET", "/api/ledger")
    after = (len(d2["overview"]["equipment"]["rows"]), d2["open_total"])
    check("重複 GET 後工單列數與事件數不變", after, before)

    test_ai_column()

    print()
    if FAILS:
        print(f"FAILED {len(FAILS)}: {FAILS}")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
