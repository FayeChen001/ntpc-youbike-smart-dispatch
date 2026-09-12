"""HTTP 整合測試 2：規劃週期資源帳、任務動作、跨區逾時主責。跑在 8789。"""
import json, os, sys, urllib.request, urllib.error
B = os.environ.get("YB_BASE", "http://127.0.0.1:8789")   # 預設打測試機，不要對共用機亂跑
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

def ensure_tasks():
    """任務只在時鐘 tick 時重排。前一支測試若跑過 /api/reset，這裡要自己推一步。"""
    for _ in range(4):
        ts = [t for t in GET("/api/tasks")[1]["tasks"] if t["stops"] and t["status"] == "planned"]
        if ts: return ts
        POST("/api/clock", {"action": "step"})
        import time; time.sleep(2)
    return []
tasks = ensure_tasks()
trip = tasks[0] if tasks else None
check("有可操作的派車任務", trip is not None, True)
assert trip, "無法取得可操作任務，後續檢查無意義"
tid = trip["id"]
check("任務帶週期與預約 id", bool(trip.get("cycle")) and len(trip.get("reservations", [])) > 0, True)

def snap_of(sid, kind):
    s = GET("/api/ops/cycle")[1]
    for r in s["by_station"]:
        if r["sid"] == sid and r["kind"] == kind: return r
    return None

drop = next(s for s in trip["stops"] if s["action"] == "dropoff")
b0 = snap_of(drop["sid"], "demand")
check("N04 起始時該送車站的承諾是候選狀態", b0["candidate"] > 0 and b0["confirmed"] == 0, True)

# GET 無副作用
s1 = GET("/api/ops/cycle")[1]; [GET("/api/ops/cycle") for _ in range(3)]; s2 = GET("/api/ops/cycle")[1]
check("N04 重複 GET /api/ops/cycle 不改帳本", s1["by_station"] == s2["by_station"], True)

# 確認 → confirmed
_, r1 = POST(f"/api/ops/tasks/{tid}/confirm", {"actor": "調度員甲"})
b1 = snap_of(drop["sid"], "demand")
check("N04 確認後預約轉為 confirmed", b1["confirmed"] > 0, True)
check("N04 確認後候選歸零", b1["candidate"], 0.0)
check("N04 承諾總量不變（只換狀態，不重扣）", b1["confirmed"], b0["candidate"])

# 版本衝突
v = r1["res_version"]
s_c, r_c = POST(f"/api/ops/tasks/{tid}/confirm", {"version": v - 1})
check("N07 帶舊版本重送回 409", s_c, 409)

# 出車 → in_transit
_, r2 = POST(f"/api/ops/tasks/{tid}/dispatch", {"version": v})
b2 = snap_of(drop["sid"], "demand")
check("N04 出車後轉為 in_transit", b2["in_transit"] > 0, True)
check("N04 出車後仍不重複占用", b2["confirmed"], 0.0)

# 取消 → released
_, r3 = POST(f"/api/ops/tasks/{tid}/cancel", {"reason": "測試釋放", "version": r2["res_version"]})
b3 = snap_of(drop["sid"], "demand")
check("N04 取消後在途歸零", b3["in_transit"], 0.0)
check("N04 取消正確釋放資源", b3["released"] > 0, True)
check("N04 取消後任務狀態為 cancelled", r3["status"], "cancelled")

# 跨區／逾時主責：沒有可派資源就不准給 ETA
t2 = next((t for t in GET("/api/tasks")[1]["tasks"] if t["stops"] and t["status"] == "planned"), None)
if t2:
    s_bad, r_bad = POST(f"/api/ops/tasks/{t2['id']}/escalate", {"plan": "cross_district"})
    check("無可行 ETA 時不得建立跨區支援（擋下）", s_bad, 400)
    s_ok, r_ok = POST(f"/api/ops/tasks/{t2['id']}/escalate", {"plan": "divert_only", "reason": "區內無可派人車，改以民眾分流"})
    check("無人可派時可明示改分流，且不產生假 ETA", (s_ok, r_ok["escalation"]["eta"]), (200, None))
    check("處理方案有記錄負責人", r_ok["escalation"]["owner"], "微笑單車調度中心")
    gov = GET("/api/notifications?channel=gov")[1]["items"]
    check("同步通知政府端", any(n.get("extra", {}).get("task_id") == t2["id"] for n in gov), True)

# 逐站期限在 API 上看得到
drops = [s for t in GET("/api/tasks")[1]["tasks"] for s in t["stops"] if s["action"] == "dropoff"]
check("N05 送車站都有自己的 due_ts/on_time", all("due_ts" in s and "on_time" in s for s in drops), True)
dues = sorted({s["due_min"] for s in drops})
check("N05 服務時限不是同一個值", len(dues) > 1, True)
mixed = [t for t in GET("/api/tasks")[1]["tasks"] if t.get("late_stops") and t.get("on_time_stops", 0) > 0]
if mixed:
    t = mixed[0]
    check("N05 有站趕不上時，理由不宣稱全部準時", "都能在各自的服務時限前抵達" in t["reason"], False)

print()
print("全部通過" if not fails else f"未通過 {len(fails)} 項：{fails}")
sys.exit(1 if fails else 0)
