"""
最低驗收矩陣裡屬於 C、之前沒有針對性測試的幾格：

  M1 無車號柱號        沒有資產識別時只關聯到站點，不盲目跟別台車合併
  M2 車機無回應        刷卡沒反應／借不到不判定車輛故障，走租借與交易待查
  M3 模擬通知深連結    sw.js 依角色路由到 /gov、/ops、/citizen，參數名各端不同

M3 直接把 app/static/sw.js 載進一個假的 Service Worker 環境執行，驗真正會開出去的
網址，不是看原始碼字串。需要 node；沒有 node 就跳過並明說跳過。

用法：YB_BASE=http://127.0.0.1:8791 python3 tests/test_c_matrix.py
"""
import json, os, shutil, subprocess, sys, tempfile, urllib.request, urllib.error

BASE = os.environ.get("YB_BASE", "http://127.0.0.1:8791")
if BASE.rstrip("/").endswith(":8787"):
    print("拒絕執行：8787 是三端共用的展示伺服器，本測試會呼叫 /api/reset。請改用其他 port。")
    sys.exit(2)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
fails, passed, skipped = [], [0], [0]


def check(name, cond, evidence=""):
    ok = bool(cond)
    print(("  PASS " if ok else "  FAIL ") + name + (f"  [{evidence}]" if evidence else ""))
    if ok:
        passed[0] += 1
    else:
        fails.append(name)


def skip(name, why):
    skipped[0] += 1
    print("  SKIP " + name + f"  [{why}]")


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


def a_row(tid):
    _, led = GET("/api/ledger")
    for r in ((led.get("overview") or {}).get("equipment") or {}).get("rows", []):
        if r.get("ticket_id") == tid:
            return r
    return None


# ============================================================ M1 無車號柱號
print("\n[M1] 沒有車號也沒有柱號：只關聯站點，不跟別台車合併")
POST("/api/reset")
_, sts = GET("/api/stations?adjusted=1")
SID = sts["stations"][0]["sid"]
anon = {"sid": SID, "stage": "passing", "problem": "tire"}

s, a = POST("/api/c/report", {**anon, "request_id": "m1-a"})
check("M1a 沒有識別也能建單", bool(a.get("ticket_id")), f"ticket={a.get('ticket_id')}")
check("M1b 缺識別保留 null，不用空字串冒充",
      a.get("bike_no") is None and a.get("dock_id") is None,
      f"bike_no={a.get('bike_no')!r} dock_id={a.get('dock_id')!r}")

s, b = POST("/api/c/report", {**anon, "request_id": "m1-b"})
check("M1c 同站同問題、兩邊都沒有識別，才合併成同一件",
      b.get("ticket_id") == a.get("ticket_id") and b.get("ticket_action") == "merged",
      f"ticket={b.get('ticket_id')} action={b.get('ticket_action')}")

s, c = POST("/api/c/report", {**anon, "bike_no": "YB2-M1C", "request_id": "m1-c"})
check("M1d 有車號的那筆不跟匿名那筆合併",
      c.get("ticket_id") != a.get("ticket_id") and c.get("ticket_action") == "created",
      f"匿名 {a.get('ticket_id')} vs 有車號 {c.get('ticket_id')}")

s, d = POST("/api/c/report", {**anon, "dock_no": "05", "request_id": "m1-d"})
check("M1e 有柱號的那筆也不跟匿名那筆合併",
      d.get("ticket_id") not in (a.get("ticket_id"), None), f"ticket={d.get('ticket_id')}")
check("M1f 柱號輸入 dock_no，對外一律是 dock_id", d.get("dock_id") == "05", f"{d.get('dock_id')}")

row = a_row(a["ticket_id"])
check("M1g A 端主動標示這張單沒有資產識別",
      any(x.get("code") == "no_asset_id" for x in (row or {}).get("divergence", [])),
      f"divergence={[x.get('code') for x in (row or {}).get('divergence', [])]}")
check("M1h 合併後回報數會累加，但仍只有一張單", (row or {}).get("reports") == 2, f"reports={(row or {}).get('reports')}")
_, eq = GET("/api/ledger")
board = ((eq.get("overview") or {}).get("equipment") or {})
check("M1i A 端統計得出「沒有資產識別」的張數", board.get("without_asset_id", 0) >= 1,
      f"without_asset_id={board.get('without_asset_id')}")

# ============================================================ M2 車機無回應
print("\n[M2] 刷卡沒反應／車機無回應：不判定車輛故障")
POST("/api/reset")
s, m2 = POST("/api/c/report", {"sid": SID, "stage": "before_borrow", "problem": "cannot_use",
                               "txn_state": "not_started", "error_code": "E14", "dock_no": "12",
                               "free_text": "刷卡完全沒反應，螢幕不亮", "request_id": "m2-a"})
check("M2a 車機問題可以受理", m2.get("accepted") is True and s == 200, f"status={m2.get('status')}")
check("M2b 不建維修工單", m2.get("ticket_id") is None, f"ticket={m2.get('ticket_id')}")
_, tks = GET("/api/tickets")
check("M2c 整個系統也沒有因此多出工單", len(tks["tickets"]) == 0, f"{len(tks['tickets'])} 張")
check("M2d 疑似方向是租借服務，不是車輛", (m2.get("triage") or {}).get("suspect") == "service",
      (m2.get("triage") or {}).get("suspect"))
check("M2e 明說不能直接判定車輛壞掉", "不能直接判定車輛壞掉" in (m2.get("triage") or {}).get("text", ""),
      (m2.get("triage") or {}).get("text", "")[:30])
check("M2f 走車柱／車機／鎖具／交易待查", "待查" in (m2.get("triage") or {}).get("route", ""),
      (m2.get("triage") or {}).get("route"))
check("M2g 不引導去反轉一台正常車的坐墊",
      (m2.get("saddle") or {}).get("applicable") is False
      and "正常車" in (m2.get("saddle") or {}).get("reason", ""),
      (m2.get("saddle") or {}).get("reason"))
check("M2h 借車前沒騎走，可以直接換一台", (m2.get("guidance") or {}).get("may_swap") is True)
check("M2i 柱號與錯誤碼有結構化保留", m2.get("dock_id") == "12" and m2.get("error_code") == "E14",
      f"dock_id={m2.get('dock_id')} error_code={m2.get('error_code')}")

_, notes = GET("/api/notifications?channel=ops")
ops_notes = notes.get("items") or []
mine = [n for n in ops_notes if (n.get("extra") or {}).get("report_id") == m2["id"]]
check("M2j 營運端收到的通知帶得回 report_id", bool(mine), f"{len(ops_notes)} 則 ops 通知")
if mine:
    ex = mine[0].get("extra") or {}
    check("M2k 通知本身就帶柱號與錯誤碼，B 不必回頭查 C",
          ex.get("dock_id") == "12" and ex.get("error_code") == "E14",
          f"dock_id={ex.get('dock_id')} error_code={ex.get('error_code')}")
    check("M2l 通知註明是自述、未判定車輛故障",
          "未判定車輛故障" in (mine[0].get("body", "") + ex.get("note", "")),
          (ex.get("note") or "")[:40])

s, m2b = POST("/api/c/report", {"sid": SID, "stage": "before_borrow", "problem": "cannot_use",
                                "txn_state": "unsure", "request_id": "m2-b"})
lines = " ".join((m2b.get("guidance") or {}).get("lines", []))
check("M2m 交易不明時不引導反覆試借", "不要反覆" in lines, lines[:40])
check("M2n 交易不明時不宣稱可以安全換車", (m2b.get("guidance") or {}).get("may_swap") is not True,
      f"may_swap={(m2b.get('guidance') or {}).get('may_swap')}")

# ============================================================ M3 通知深連結
print("\n[M3] 模擬通知的深連結：sw.js 依角色路由，各端參數名不同")
node = shutil.which("node")
if not node:
    skip("M3 sw.js 路由", "找不到 node，這段未執行")
else:
    harness = r"""
const fs = require('fs');
const sw = fs.readFileSync(process.argv[2], 'utf8');
const handlers = {};
const opened = [];
global.self = {
  addEventListener: (t, fn) => { handlers[t] = fn; },
  location: { origin: 'https://example.test' },
  registration: { showNotification: () => {} },
  skipWaiting: () => {}, clients: { claim: () => {} },
};
global.caches = { open: async () => ({ add: async () => {} }), keys: async () => [], match: async () => null, delete: async () => {} };
global.clients = {
  matchAll: async () => [],                       // 沒有已開著的分頁 → 會走 openWindow
  openWindow: (u) => { opened.push(u); return Promise.resolve(); },
};
eval(sw);
const cases = JSON.parse(process.argv[3]);
(async () => {
  const out = [];
  for (const c of cases) {
    opened.length = 0;
    let pending = null;
    const ev = {
      notification: { close: () => {}, data: c },
      waitUntil: (p) => { pending = p; },      // openWindow 是在 matchAll 之後才呼叫，一定要等
    };
    handlers['notificationclick'](ev);
    if (pending) { try { await pending; } catch (e) {} }
    out.push(opened[0] === undefined ? null : opened[0]);
  }
  console.log('@@JSON@@' + JSON.stringify(out));
})();
"""
    cases = [
        {"url": "/gov", "event_id": "A12", "role": "gov"},
        {"url": "/ops", "event_id": "R007", "role": "ops"},
        {"url": "/citizen", "event_id": "C003", "role": "citizen"},
        {"url": "/citizen", "event_id": None, "role": "citizen"},
        {"url": "/gov", "event_id": "A 1&x=2", "role": "gov"},
    ]
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as f:
        f.write(harness)
        hp = f.name
    try:
        r = subprocess.run([node, hp, os.path.join(ROOT, "app", "static", "sw.js"),
                            json.dumps(cases)], capture_output=True, text=True, timeout=60)
        line = [l for l in r.stdout.splitlines() if l.startswith("@@JSON@@")]
        got = json.loads(line[0][len("@@JSON@@"):]) if line else None
        if got is None:
            print(r.stdout[-500:], r.stderr[-500:])
        check("M3-0 sw.js 能在假環境裡跑起來並處理通知點擊", got is not None, f"{got}")
        if got:
            check("M3a 政府通知開 /gov 並用 event 參數", got[0] == "/gov?event=A12", got[0])
            check("M3b 營運通知開 /ops 並用 ticket 參數", got[1] == "/ops?ticket=R007", got[1])
            check("M3c 民眾通知開 /citizen 並用 report 參數", got[2] == "/citizen?report=C003", got[2])
            check("M3d 沒有事件識別就不硬加參數", got[3] == "/citizen", got[3])
            check("M3e 識別會做 URL 編碼，不會被注入成第二個參數",
                  got[4] == "/gov?event=A%201%26x%3D2", got[4])
            bases = [(u or "").split("?")[0] for u in got[:3]]
            check("M3f 三端不會全部跳到 /citizen", len(set(bases)) == 3 and "" not in bases, f"{bases}")
    finally:
        os.unlink(hp)

# /citizen 的 report 參數要真的有人讀
citizen_html = open(os.path.join(ROOT, "app", "static", "citizen.html"), encoding="utf-8").read()
check("M3g /citizen 有實作讀取 report 參數並開該筆進度",
      "q.get('report')" in citizen_html and "renderTrack" in citizen_html)

POST("/api/reset")
print(f"\n=== 最低矩陣補齊：通過 {passed[0]}　失敗 {len(fails)}　跳過 {skipped[0]} ===")
for f in fails:
    print("  未通過：" + f)
sys.exit(1 if fails else 0)
