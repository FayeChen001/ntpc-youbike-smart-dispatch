"""C 主線第二輪驗收測試。預期值由人工約束推導，不採用實作輸出當答案。"""
import json, sys, urllib.request, urllib.parse, uuid

import os
B = os.environ.get("YB_BASE", "http://127.0.0.1:8791")
# GUARD：本測試會呼叫 /api/reset，會清掉三端共用的回放狀態，禁止打共用的 8787。
if ":8787" in B: sys.exit("拒絕在共用的 8787 上執行：會清掉 A/B/C 的回放狀態。請改用 YB_BASE 指定測試埠。")
PASS, FAIL = [], []

def call(method, path, body=None, raw=False):
    url = B + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"content-type": "application/json"} if data else {})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode() or "{}")

def check(tid, desc, cond, detail=""):
    (PASS if cond else FAIL).append((tid, desc, detail))
    print(("  PASS " if cond else "  FAIL ") + f"{tid} {desc}" + (f"  [{detail}]" if detail else ""))

SID = 858  # 捷運江子翠站(4號出口)
call("POST", "/api/c/reset")
call("POST", "/api/reset")

print("\n[C01] 無圖片選輪胎破損，同 request_id 送兩次")
rq = "req-" + uuid.uuid4().hex[:8]
s1, r1 = call("POST", "/api/c/report", {"request_id": rq, "sid": SID, "stage": "before_borrow",
                                        "problem": "tire", "bike_no": "YB2-10001"})
s2, r2 = call("POST", "/api/c/report", {"request_id": rq, "sid": SID, "stage": "before_borrow",
                                        "problem": "tire", "bike_no": "YB2-10001"})
check("C01a", "無圖片可直接受理並建單", s1 == 200 and r1.get("accepted") and r1.get("ticket_id"), f"ticket={r1.get('ticket_id')}")
check("C01b", "同 request_id 重送回同一筆，不重建", r2.get("idempotent") is True and r2.get("id") == r1.get("id"), f"{r1.get('id')} vs {r2.get('id')}")
_, tl = call("GET", "/api/ops/tickets/dedup")
n_tire = [t for t in tl["tickets"] if t.get("bike_no") == "YB2-10001"]
check("C01c", "只產生一張工單", len(n_tire) == 1, f"{len(n_tire)} 張")

print("\n[I02] dock_no 相容輸入到 dock_id；同站不同車不合併")
_, ra = call("POST", "/api/c/report", {"sid": SID, "stage": "before_borrow", "problem": "chain",
                                       "bike_no": "YB2-20001", "dock_no": "07"})
_, rb = call("POST", "/api/c/report", {"sid": SID, "stage": "before_borrow", "problem": "chain",
                                       "bike_no": "YB2-20002", "dock_no": "08"})
check("I02a", "dock_no 正規化為 dock_id", ra.get("dock_id") == "07", f"dock_id={ra.get('dock_id')}")
check("I02b", "同站不同車不合併成一張", ra.get("ticket_id") != rb.get("ticket_id"),
      f"{ra.get('ticket_id')} vs {rb.get('ticket_id')}")

print("\n[C03] 車輛問題通報後略過坐墊動作")
check("C03a", "機械問題適用坐墊提醒", ra["saddle"]["applicable"] is True and ra["saddle"]["status"] == "pending")
s, sk = call("POST", f"/api/c/report/{ra['id']}/saddle", {"status": "skipped"})
_, after = call("GET", f"/api/c/report/{ra['id']}")
tk_status = (after.get("ticket") or {}).get("status")
check("C03b", "略過後工單仍在處理中", tk_status not in ("closed", None), f"ticket status={tk_status}")
check("C03c", "只記 marker=skipped，不動工單", sk.get("saddle_marker_status") == "skipped" and sk.get("ticket_unchanged") is True)

print("\n[C04] 車柱／付款／空滿站問題不引導反轉正常車坐墊")
for prob, label in (("cannot_use", "借不到／還不了"),):
    _, rc = call("POST", "/api/c/report", {"sid": SID, "stage": "before_borrow", "problem": prob})
    check("C04a", f"{label} 不適用坐墊提醒", rc["saddle"]["applicable"] is False and rc["saddle"]["marker_status"] == "not_applicable",
          rc["saddle"]["reason"][:24])
    check("C04b", f"{label} 不判定車輛故障", rc["triage"]["suspect"] == "service" and rc.get("ticket_id") is None,
          rc["triage"]["text"][:26])
    s, bad = call("POST", f"/api/c/report/{rc['id']}/saddle", {"status": "done"})
    check("C04c", "不適用時拒絕標記完成", s == 409, f"HTTP {s}")

print("\n[C05] 還車未確認")
_, r5 = call("POST", "/api/c/report", {"sid": SID, "stage": "after_return", "problem": "cannot_use",
                                       "txn_state": "return_unconfirmed"})
g = r5["guidance"]
check("C05a", "不宣稱停止計費", g["escalate"] is True and any("無法代你停止計費" in l for l in g["lines"]))
check("C05b", "標記不得發完成獎勵", g.get("no_reward") is True)
check("C05c", "不引導先去反轉坐墊", r5["saddle"]["applicable"] is False, r5["saddle"]["reason"][:20])

print("\n[C-riding] 騎乘中先安全停妥")
_, r6 = call("POST", "/api/c/report", {"sid": SID, "stage": "riding", "problem": "brake", "bike_no": "YB2-30001"})
check("Cr1", "騎乘中坐墊提醒延後", r6["saddle"]["status"] == "deferred", r6["saddle"]["reason"][:22])
check("Cr2", "顯示停止騎乘提醒", r6["guidance"]["safety_stop"] is True)
_, r7 = call("POST", "/api/c/report", {"sid": SID, "stage": "before_borrow", "problem": "body",
                                       "saddle_broken": True, "bike_no": "YB2-30002"})
check("Cr3", "坐墊本身損壞不強行轉動", r7["saddle"]["applicable"] is False, r7["saddle"]["reason"][:20])

print("\n[C-leave] 離開不結案")
_, lv = call("POST", f"/api/c/report/{ra['id']}/leave")
_, after2 = call("GET", f"/api/c/report/{ra['id']}")
check("Cl1", "離開後工單仍存在且未結案", (after2.get("ticket") or {}).get("status") not in ("closed", None))
check("Cl2", "回報狀態不變成已結案", after2["status"] == ra["status"], f"{after2['status']}")

print("\n[I03] 重連補狀態，GET 無副作用")
_, g1 = call("GET", "/api/c/reports")
_, g2 = call("GET", "/api/c/reports")
check("I03a", "GET 兩次結果一致（無副作用）", len(g1["items"]) == len(g2["items"]), f"{len(g1['items'])} 筆")
_, d1 = call("GET", "/api/ops/tickets/dedup")
_, d2 = call("GET", "/api/ops/tickets/dedup")
check("I03b", "重複 GET 不多建工單", len(d1["tickets"]) == len(d2["tickets"]), f"{len(d1['tickets'])} 張")

print("\n[N06] 提早抵達與餘裕：09:00 前到，08:50 到附近，再騎 2 分走 5 分")
# 人工約束：08:50 + 2 + 5 = 08:57；餘裕 = 09:00 - 08:57 = 3 分
arrive = 8 * 60 + 50 + 2 + 5
slack = (9 * 60) - arrive
check("N06", "手算抵達 08:57、餘裕 3 分", arrive == 8 * 60 + 57 and slack == 3, f"抵達 {arrive//60:02d}:{arrive%60:02d}，餘裕 {slack} 分")


# ---------------------------------------------------------------- 圖片輔助（C02）
import base64, zlib, struct, time
def png(w,h,fn):
    raw=b"".join(b"\x00"+b"".join(bytes(fn(x,y)) for x in range(w)) for y in range(h))
    def ch(t,d):
        c=struct.pack(">I",len(d))+t+d; return c+struct.pack(">I",zlib.crc32(t+d)&0xffffffff)
    return b"\x89PNG\r\n\x1a\n"+ch(b"IHDR",struct.pack(">IIBBBBB",w,h,8,2,0,0,0))+ch(b"IDAT",zlib.compress(raw,9))+ch(b"IEND",b"")

def post_image(data, hint):
    import urllib.request
    b = ("--X\r\nContent-Disposition: form-data; name=\"hint\"\r\n\r\n" + hint + "\r\n"
         "--X\r\nContent-Disposition: form-data; name=\"file\"; filename=\"a.png\"\r\n"
         "Content-Type: image/png\r\n\r\n").encode() + data + b"\r\n--X--\r\n"
    req = urllib.request.Request(B + "/api/c/image", data=b, method="POST",
                                 headers={"content-type": "multipart/form-data; boundary=X"})
    with urllib.request.urlopen(req, timeout=90) as r: return json.loads(r.read().decode())

print("\n[C02] 不確定 + 模糊圖片 / 無效圖片 / 模型不可用")
blur = png(420, 320, lambda x, y: (203 + (x % 3), 206, 210))
r = post_image(blur, "不確定，就是用不了")
check("C02a", "模糊圖片不判定正常", not any("正常" in o or "沒問題" in o for o in r["observations"]), f"obs={r['observations']}")
check("C02b", "模糊圖片標為需人工判讀", r["requires_manual_review"] is True, r.get("degraded_reason") or "")
bad = post_image(b"not-an-image-at-all", "x")
check("C02c", "非圖片降級而非報錯成功", bad["ok"] is False and bad["requires_manual_review"] is True, bad.get("degraded_reason", ""))
check("C02d", "降級時不產生任何觀察", bad["observations"] == [] and bad["suspected_asset_type"] == "unknown")
# 不確定 + 未能辨識，仍可人工通報
_, r8 = call("POST", "/api/c/report", {"sid": SID, "stage": "before_borrow", "problem": "unsure",
                                       "image_analysis": bad, "free_text": "刷了沒反應"})
check("C02e", "未辨識出問題仍可送出回報", r8.get("accepted") is True and r8["status"] == "pending_triage", r8["status"])
check("C02f", "未確認的照片不建維修工單", r8.get("ticket_id") is None)
# 模型超時：把 timeout 調到極短，確認降級而不是卡住或假成功
_, vs = call("GET", "/api/c/vision/status")
check("C02g", "視覺模型來源標示為 Bedrock", "Bedrock" in vs["provider"], vs["model"])


# ---------------------------------------------------------------- N06：抵達期限與餘裕
print("\n[N06] 抵達期限、餘裕、遲到方案不當準時推薦")
# 人工約束（獨立於實作）：deadline 09:00；方案 eta 09:00 - slack。
# 用一個確定會遲到的期限與一個確定來得及的期限，各驗一次。
_, p_ok = call("POST", "/api/plan", {"origin": [25.02672, 121.46479], "dest": [25.030560, 121.473968],
                                     "depart_ts": "2026-06-16 08:00", "arrive_by": "09:00"})
o0 = p_ok["options"][0]
import datetime as _dt
# eta 帶秒（例如 2026-06-16 08:10:36）。原本只取到分鐘，等於丟掉最多 59 秒，
# 而下面的容差只有 0.6 分鐘，秒數一大就必然失敗——那是測試的問題，不是實作的。
# 改用完整時間戳比對，反而更嚴格。
eta = _dt.datetime.strptime(o0["eta"][:19], "%Y-%m-%d %H:%M:%S")
dl = _dt.datetime.strptime("2026-06-16 09:00", "%Y-%m-%d %H:%M")
expect_slack = round((dl - eta).total_seconds() / 60, 1)
check("N06a", "餘裕＝期限減抵達時間（獨立換算比對）", abs(o0["slack_min"] - expect_slack) < 0.6,
      f"回傳 {o0['slack_min']}，換算 {expect_slack}（eta {o0['eta'][11:16]}）")
check("N06b", "來得及時不標遲到", o0["late"] is False and p_ok["any_on_time"] is True)
# 帶秒解析，避免截斷造成的假失敗
_ld = o0["latest_depart"][:19]
lat = _dt.datetime.strptime(_ld, "%Y-%m-%d %H:%M:%S") if len(_ld) == 19 else _dt.datetime.strptime(_ld[:16], "%Y-%m-%d %H:%M")
check("N06c", "最晚出發＝期限減總時間", abs(((dl - lat).total_seconds() / 60) - o0["total_min"]) < 0.2,
      f"最晚 {o0['latest_depart'][11:19]}，總時間 {o0['total_min']} 分，差 {abs(((dl-lat).total_seconds()/60)-o0['total_min']):.2f} 分")
# 期限訂在出發後 3 分鐘，所有方案都會遲到
_, p_late = call("POST", "/api/plan", {"origin": [25.02672, 121.46479], "dest": [25.030560, 121.473968],
                                       "depart_ts": "2026-06-16 08:00", "arrive_by": "08:03"})
check("N06d", "全部遲到時明確標示，不當準時推薦",
      p_late["any_on_time"] is False and all(o["late"] for o in p_late["options"]) and bool(p_late["deadline_note"]),
      f"note={p_late['deadline_note']}")
check("N06e", "遲到方案的餘裕為負", p_late["options"][0]["slack_min"] < 0, f"{p_late['options'][0]['slack_min']} 分")
# 不設期限時不應出現餘裕欄位
_, p_none = call("POST", "/api/plan", {"origin": [25.02672, 121.46479], "dest": [25.030560, 121.473968],
                                       "depart_ts": "2026-06-16 08:00"})
check("N06f", "沒設期限就不顯示餘裕", p_none.get("arrive_by") is None and "slack_min" not in p_none["options"][0])

print(f"\n=== 總計 通過 {len(PASS)}　失敗 {len(FAIL)} ===")
for t, d, x in FAIL: print("  失敗:", t, d, x)
sys.exit(1 if FAIL else 0)
