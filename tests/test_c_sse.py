"""I03：SSE 實際斷線後重連，GET 是否補回權威狀態、是否重複建單。"""
import json, os, sys, time, threading, urllib.request, socket

B = os.environ.get("YB_BASE", "http://127.0.0.1:8791")
# GUARD：本測試會呼叫 /api/reset，會清掉三端共用的回放狀態，禁止打共用的 8787。
if ":8787" in B: sys.exit("拒絕在共用的 8787 上執行：會清掉 A/B/C 的回放狀態。請改用 YB_BASE 指定測試埠。")
PASS, FAIL = [], []
def check(t, d, c, x=""):
    (PASS if c else FAIL).append((t, d, x)); print(("  PASS " if c else "  FAIL ") + f"{t} {d}" + (f"  [{x}]" if x else ""))
def call(m, p, b=None):
    req = urllib.request.Request(B + p, data=json.dumps(b).encode() if b is not None else None, method=m,
                                 headers={"content-type": "application/json"} if b is not None else {})
    with urllib.request.urlopen(req, timeout=60) as r: return json.loads(r.read().decode())

print("[I03] SSE 斷線重連")
call("POST", "/api/c/reset")
got = {"n": 0, "events": []}
stop = threading.Event()

def listen(budget, want_real=False):
    """實際開 SSE 連線。want_real=True 時忽略 hello，等到真正的事件才算數；
    讀到後強制 close() 關閉 socket，模擬斷線。"""
    try:
        r = urllib.request.urlopen(B + "/api/events", timeout=budget)
        t0 = time.time()
        while time.time() - t0 < budget and not stop.is_set():
            line = r.readline()
            if not line: break
            if not line.startswith(b"data:"): continue
            try: typ = json.loads(line[5:].decode())["type"]
            except Exception: continue
            got["events"].append(typ)
            if want_real and typ == "hello": continue
            got["n"] += 1
            break
        r.close()          # 強制關閉，模擬斷線
        return True
    except Exception as e:
        print("   listen error:", type(e).__name__); return False

th = threading.Thread(target=listen, args=(12,), daemon=True); th.start(); th.join(14)
check("I03-0", "SSE 可建立連線並收到事件", got["n"] >= 1, f"收到 {got['n']} 則 {got['events'][:2]}")

# 斷線期間建立一筆回報（客戶端收不到事件）
r1 = call("POST", "/api/c/report", {"request_id": "sse-001", "sid": 858, "stage": "before_borrow",
                                    "problem": "brake", "bike_no": "YB2-88001"})
print(f"   斷線期間建立 {r1['id']} / 工單 {r1['ticket_id']}")

# 重連：GET 補回權威狀態
after = call("GET", "/api/c/reports")
ids = [x["id"] for x in after["items"]]
check("I03a", "重連後 GET 補回斷線期間的事件", r1["id"] in ids, f"items={ids}")
one = call("GET", f"/api/c/report/{r1['id']}")
check("I03b", "補回的資料含工單與權威狀態", one.get("ticket", {}).get("id") == r1["ticket_id"],
      f"ticket={one.get('ticket',{}).get('id')} status={one.get('ticket',{}).get('status_label')}")

# 重連後重送同一 request_id：不得重複建單
n_before = len(call("GET", "/api/ops/tickets/dedup")["tickets"])
r2 = call("POST", "/api/c/report", {"request_id": "sse-001", "sid": 858, "stage": "before_borrow",
                                    "problem": "brake", "bike_no": "YB2-88001"})
n_after = len(call("GET", "/api/ops/tickets/dedup")["tickets"])
check("I03c", "重連後重送不重複建單", r2.get("idempotent") is True and n_before == n_after,
      f"{n_before} → {n_after} 張，idempotent={r2.get('idempotent')}")

# 第二次連線仍可收到新事件
got["n"] = 0; got["events"] = []
th2 = threading.Thread(target=listen, args=(15, True), daemon=True); th2.start()
time.sleep(1.5)
call("POST", "/api/c/report", {"sid": 858, "stage": "passing", "problem": "cannot_use"})
th2.join(18)
real = [e for e in got["events"] if e != "hello"]
check("I03d", "重連後能收到斷線後發生的新事件（非 hello）", len(real) >= 1, f"事件序列 {got['events'][:4]}")

print(f"\n=== SSE 段：通過 {len(PASS)}　失敗 {len(FAIL)} ===")
for t, d, x in FAIL: print("  失敗:", t, d, x)
sys.exit(1 if FAIL else 0)
