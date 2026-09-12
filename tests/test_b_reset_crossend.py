"""三端 reset 一致性：reset 之後，任何一端都不該留下指向已刪除工單的殘影。"""
import json, os, sys, urllib.request, urllib.error
B = os.environ.get("YB_BASE", "http://127.0.0.1:8789")
def req(m, p, b=None):
    d = json.dumps(b).encode() if b is not None else None
    r = urllib.request.Request(B + p, data=d, method=m, headers={"content-type": "application/json"})
    try:
        with urllib.request.urlopen(r, timeout=30) as x: return x.status, json.loads(x.read().decode())
    except urllib.error.HTTPError as e: return e.code, json.loads(e.read().decode() or "{}")
GET = lambda p: req("GET", p); POST = lambda p, b=None: req("POST", p, b or {})
fails = []
def check(name, got, want, note=""):
    okk = got == want
    print(("[PASS] " if okk else "[**FAIL**] ") + name + f"  → {got!r}" + ("" if okk else f"   want={want!r}" + (f"\n           {note}" if note else "")))
    if not okk: fails.append(name)

POST("/api/reset")
sid = GET("/api/stations?adjusted=1")[1]["stations"][0]["sid"]
body = {"sid": sid, "stage": "before_borrow", "problem": "chain", "bike_no": "YB-RST", "request_id": "reset-probe"}
s, rep = POST("/api/c/report", body)
tid = rep.get("ticket_id"); rid = rep.get("id")
check("前置：C 回報建出工單", bool(tid and rid), True)
first_ts = GET(f"/api/ops/tickets/{tid}")[1].get("ts")
POST(f"/api/c/report/{rid}/saddle", {"status": "skipped"})

POST("/api/reset")

check("reset 後 B 的工單清空", len(GET("/api/tickets")[1]["tickets"]), 0)
# reset 會清空資源帳；下一個 tick 才會重新規劃，所以這裡預期是 0（我上一版把判準寫反了）
check("reset 後 B 的資源帳歸零", GET("/api/ops/cycle")[1]["reservations"], 0)

s2, r2 = GET(f"/api/c/report/{rid}")
check("reset 後 C 的回報也該清掉（否則會指向已刪除的工單）", s2, 404,
      f"目前回應 {s2}，內容 ticket_id={r2.get('ticket_id')}；api_reset 沒有清 CREPORTS")

s3, sm = GET("/api/c/saddle_markers")
stale = [m for m in sm.get("markers", []) if m.get("ticket_id") == tid]
check("reset 後坐墊標記不該還指向已刪除的工單", stale, [],
      "CREPORTS['saddle'] 未被 api_reset 清除")

# 重送同一個 request_id：要建出「真正的新單」。
# 不能拿 ticket_id 字串比對——reset 之後編號從 R001 重新開始，新單本來就會撞到舊的 id。
# 真正的判準是：舊 id 已經 404、新 id 解析得到、而且那張單是全新的（version=1、reports=1、時間不同）。
POST("/api/clock", {"action": "step"})          # 推一步讓時間戳分得出新舊
s4, again = POST("/api/c/report", body)
new_tid = again.get("ticket_id")
s5, new_tk = GET(f"/api/ops/tickets/{new_tid}")
check("reset 後重送，回傳的 ticket_id 解析得到（不是指向已刪除的單）", s5, 200)
check("reset 後重送建出來的是全新的單，不是殘影",
      (new_tk.get("version"), new_tk.get("reports")), (1, 1),
      f"若是殘影，version/reports 會沿用舊值")
check("reset 後重送，工單總數為 1", len(GET("/api/tickets")[1]["tickets"]), 1)
check("reset 後重送的單，時間戳是重送當下而不是 reset 前",
      new_tk.get("ts") != first_ts, True, f"reset 前 ts={first_ts}，重送後 ts={new_tk.get('ts')}")

POST("/api/reset")
print()
print("全部通過" if not fails else f"未通過 {len(fails)} 項：{fails}")
sys.exit(1 if fails else 0)
