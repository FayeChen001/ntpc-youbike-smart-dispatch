"""service_state 接站點恢復判定：degraded / restored / nominal 與兩種落差旗標。
用回放資料裡真實發生過的「零車 → 有車」時點，不是造假的狀態。需伺服器。"""
import json, os, sys, urllib.request, urllib.error
B = os.environ.get("YB_BASE", "http://127.0.0.1:8789")
def req(m, p, b=None):
    d = json.dumps(b).encode() if b is not None else None
    r = urllib.request.Request(B + p, data=d, method=m, headers={"content-type": "application/json"})
    try:
        with urllib.request.urlopen(r, timeout=60) as x: return x.status, json.loads(x.read().decode())
    except urllib.error.HTTPError as e: return e.code, json.loads(e.read().decode() or "{}")
GET = lambda p: req("GET", p); POST = lambda p, b=None: req("POST", p, b or {})
fails = []
def check(name, got, want, note=""):
    okk = got == want
    print(("[PASS] " if okk else "[**FAIL**] ") + name + f"  → {got!r}" + ("" if okk else f"   want={want!r}" + (f"\n           {note}" if note else "")))
    if not okk: fails.append(name)

POST("/api/reset")
T0 = "2026-06-16 07:00"
POST("/api/clock", {"action": "set", "ts": T0})
stations = GET("/api/stations?adjusted=1")[1]["stations"]
empty_now = {s["sid"] for s in stations if s["status"] == "empty" and (s["bikes"] or 0) == 0}
healthy = next(s for s in stations if s["status"] == "normal" and (s["bikes"] or 0) >= 3 and (s["spaces"] or 0) >= 3)
print(f"起點 {T0}：零車站 {len(empty_now)} 個")

# 往前走，找「這一刻零車、走 k 步後有車」的真實恢復站；同時找一個走完仍零車的站
recover_sid, k, still_sid = None, 0, None
for step in range(1, 5):
    POST("/api/clock", {"action": "step"})
    nxt = {s["sid"]: s for s in GET("/api/stations?adjusted=1")[1]["stations"]}
    if recover_sid is None:
        cand = [sid for sid in empty_now if (nxt.get(sid, {}).get("bikes") or 0) > 0 and nxt[sid]["status"] == "normal"]
        if cand: recover_sid, k = cand[0], step
    if step == 4:
        stay = [sid for sid in empty_now if nxt.get(sid, {}).get("status") == "empty" and (nxt[sid].get("bikes") or 0) == 0]
        if stay: still_sid = stay[0]
check("在回放資料裡找得到真實的『零車→有車』站", recover_sid is not None, True)
assert recover_sid, "找不到恢復樣本，無法驗 restored"
print(f"恢復樣本 sid={recover_sid}，走 {k} 步後有車；持續零車樣本 sid={still_sid}")

# 回到起點建單。注意 clock set 本身就會 tick，所以建完單要在同一時點再 tick 一次才觀測得到。
POST("/api/clock", {"action": "set", "ts": T0})
s, tk_down = POST("/api/ops/tickets", {"sid": recover_sid, "issue": "煞車異常", "bike_no": "YB-SVC1"})
s, tk_ok = POST("/api/ops/tickets", {"sid": healthy["sid"], "issue": "鏈條異常", "bike_no": "YB-SVC2"})
# 先接單讓它變成 manual，模擬流程就不會自動推進狀態，才驗得出「服務觀測不動工單狀態」
POST(f"/api/ops/tickets/{tk_down['id']}/transition", {"to": "accepted", "version": tk_down["version"], "crew": "板橋維修1組"})
POST("/api/clock", {"action": "set", "ts": T0})          # 同一時點再 tick
d1 = GET(f"/api/ops/tickets/{tk_down['id']}")[1]
n1 = GET(f"/api/ops/tickets/{tk_ok['id']}")[1]
check("零車站的工單被觀測為服務中斷", d1["service_state"], "degraded")
check("中斷有記錄觀測跨度（沿用政府端 observed_run）", "span_min" in (d1.get("service_basis") or {}), True)
check("中斷的用詞是觀測跨度，不是確定中斷時間",
      (d1.get("service_basis") or {}).get("wording"), "觀測跨度，不是確定的連續中斷時間")
check("service_basis.observed 是字串 down（曾被 detail 的同名 key 蓋成數字 0）",
      (d1.get("service_basis") or {}).get("observed"), "down")
check("有車站的工單是『觀測未見中斷』而不是 restored", n1["service_state"], "nominal",
      "沒中斷過就不存在恢復，講 restored 是無中生有")
check("degraded 有記錄起始時間", bool(d1.get("degraded_since")), True)
status_before = d1["status"]

# 走 k 步到恢復時點
for _ in range(k): POST("/api/clock", {"action": "step"})
d2 = GET(f"/api/ops/tickets/{tk_down['id']}")[1]
check("站點恢復後服務狀態轉為 restored", d2["service_state"], "restored")
check("restored 有記錄恢復時間", bool(d2.get("restored_at")), True)
check("服務觀測不會動到工單處理狀態", d2["status"], status_before)
check("服務恢復不會把設備驗收狀態改成已修好", d2["asset_state"], "suspect")
check("restored_not_closed 旗標出現", "restored_not_closed" in (d2.get("service_flags") or []), True)

# 落差旗標二：已處理但站點仍中斷
if still_sid:
    POST("/api/clock", {"action": "set", "ts": T0})
    s, tk2 = POST("/api/ops/tickets", {"sid": still_sid, "issue": "煞車異常", "bike_no": "YB-SVC3"})
    for to in ("accepted", "on_site", "recovered"):
        s, tk2 = POST(f"/api/ops/tickets/{tk2['id']}/transition", {"to": to, "version": tk2["version"]})
    POST("/api/clock", {"action": "set", "ts": T0})
    d3 = GET(f"/api/ops/tickets/{tk2['id']}")[1]
    check("已處理但站點服務仍中斷 → handled_not_restored",
          "handled_not_restored" in (d3.get("service_flags") or []), True,
          f"service_state={d3.get('service_state')} status={d3.get('status')}")
    check("維修已處理不代表服務已恢復（兩個欄位分開）",
          (d3["status"], d3["service_state"]), ("recovered", "degraded"))
else:
    print("[SKIP] 找不到持續零車的站，handled_not_restored 這次沒驗到")

# 契約有公布新的值域
c = GET("/api/ops/contract")[1]
check("契約版本升到 b-2", c["contract_version"], "b-2")
check("契約列出四種服務狀態", sorted(c["states"]["service_state"]), ["degraded", "nominal", "restored", "unknown"])
check("契約列出兩種落差旗標", sorted(c["states"]["service_flags"]), ["handled_not_restored", "restored_not_closed"])
check("契約講清楚快照不是交易", "快照不是交易" in c["state_meaning"]["service_state"], True)

POST("/api/reset")
print()
print("全部通過" if not fails else f"未通過 {len(fails)} 項：{fails}")
sys.exit(1 if fails else 0)
