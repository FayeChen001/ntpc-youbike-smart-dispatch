"""I01 三端閉環：C 送回報 → B 收到同一張工單 → 派查 → 處理 → 驗收，三端狀態一致。"""
import json, sys, urllib.request, urllib.error
B = "http://127.0.0.1:8789"
def req(m, p, b=None):
    d = json.dumps(b).encode() if b is not None else None
    r = urllib.request.Request(B + p, data=d, method=m, headers={"content-type": "application/json"})
    try:
        with urllib.request.urlopen(r, timeout=30) as x: return x.status, json.loads(x.read().decode())
    except urllib.error.HTTPError as e: return e.code, json.loads(e.read().decode() or "{}")
GET = lambda p: req("GET", p); POST = lambda p, b=None: req("POST", p, b or {})
fails = []
def check(name, got, want):
    okk = got == want
    print(("[PASS] " if okk else "[**FAIL**] ") + name + f"  → {got!r}" + ("" if okk else f"   want={want!r}"))
    if not okk: fails.append(name)

POST("/api/reset")
sid = GET("/api/stations?adjusted=1")[1]["stations"][0]["sid"]

# C 端：明確機械問題，帶柱號舊欄位名，送兩次同一個 request
body = {"sid": sid, "stage": "before_borrow", "problem": "chain", "bike_no": "YB-C01",
        "dock_no": "09", "request_id": "closed-loop-1"}
s1, r1 = POST("/api/c/report", body)
s2, r2 = POST("/api/c/report", body)
check("C 端回報建立成功", s1, 200)
tid = r1.get("ticket_id")
check("C 端拿到工單編號", bool(tid), True)
assert tid, f"C 端沒有建出工單，後續檢查無意義；回應={r1}"
check("C01 同 request 重送不重建", r2.get("ticket_id"), tid)
tks = GET("/api/tickets")[1]["tickets"]
check("C01 只有一張工單", len(tks), 1)

# B 端：同一個 ticket_id 看得到，柱號有結構化保留
s, tk = GET(f"/api/ops/tickets/{tid}")
check("I01 B 端用同一個 ticket_id 讀得到", s, 200)
check("I02 柱號結構化保留（dock_no → dock_id）", tk["dock_id"], "09")
check("I02 車號保留", tk["bike_no"], "YB-C01")
check("明確機械問題 → 可直接派修", tk["diagnosis"], "direct_repair")

# C 端：略過坐墊
s, sd = POST(f"/api/c/report/{r1['id']}/saddle", {"status": "skipped"})
check("C03 坐墊端點回報成功", s, 200)
tk2 = GET(f"/api/ops/tickets/{tid}")[1]
check("C03 略過坐墊後工單狀態不變", tk2["status"], "reported")
ext = tk2.get("saddle_marker_external") or {}
check("C03 B 端看得到坐墊標記（唯讀橋接）", ext.get("status"), "skipped")
check("C03 橋接明示不是工單權威欄位", ext.get("authoritative"), False)

# B 端：派查 → 現場 → 處理 → 驗收
v = tk2["version"]
s, a1 = POST(f"/api/ops/tickets/{tid}/transition", {"to": "accepted", "version": v, "crew": "板橋維修1組", "eta": "08:20"})
check("I01 派查成功", (s, a1["crew"]), (200, "板橋維修1組"))
s, a2 = POST(f"/api/ops/tickets/{tid}/transition", {"to": "on_site", "version": a1["version"]})
s, a3 = POST(f"/api/ops/tickets/{tid}/transition", {"to": "recovered", "version": a2["version"]})
check("I01 處理完成後設備狀態為已維修", a3["asset_state"], "repaired")
check("I01 修復不等於自動驗收", a3["status"] != "verified" and a3["status"] != "closed", True)
s, a4 = POST(f"/api/ops/tickets/{tid}/transition", {"to": "verified", "version": a3["version"]})
check("I01 驗收後設備狀態才是驗收通過", a4["asset_state"], "verified_ok")

# C 端追蹤：使用者端讀得到同一張單的進度
s, cr = GET(f"/api/c/report/{r1['id']}")
check("I01 C 端追蹤到同一個 ticket_id", cr.get("ticket_id"), tid)

# I03 重連：GET 補回權威狀態且不重複建單
n_before = len(GET("/api/tickets")[1]["tickets"])
for _ in range(3): GET(f"/api/ops/tickets/{tid}"); GET("/api/ops/tickets"); GET(f"/api/c/report/{r1['id']}")
check("I03 重複 GET 不會重複建單", len(GET("/api/tickets")[1]["tickets"]), n_before)
check("I03 GET 取回的狀態就是最新權威狀態", GET(f"/api/ops/tickets/{tid}")[1]["status"], "verified")

POST("/api/reset")
print()
print("全部通過" if not fails else f"未通過 {len(fails)} 項：{fails}")
sys.exit(1 if fails else 0)
