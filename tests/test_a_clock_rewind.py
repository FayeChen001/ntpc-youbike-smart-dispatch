"""
test_a_clock_rewind.py — 回放時鐘往回跳不得讓 on_tick 崩掉。

原始缺陷：progress_tickets() 的 steps 在時鐘回捲時是負數，
TICKET_FLOW[min(len-1, steps)] 會反向索引而 IndexError，整個 on_tick 回 500，
該次 tick 後面的 progress_tasks／check_reminders 都不會執行。

這支測試會改動伺服器的回放時鐘與工單，**不要對 8787 跑**。
執行：
    python3 -m uvicorn app.server:app --host 127.0.0.1 --port 8788 &
    YB_BASE=http://127.0.0.1:8788 python3 tests/test_a_clock_rewind.py
"""
import json
import os
import sys
import urllib.request

BASE = os.environ.get("YB_BASE", "http://127.0.0.1:8788")
FAILS = []


def req(method, path, body=None, timeout=120):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(BASE + path, data=data, method=method,
                               headers={"content-type": "application/json"})
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        try:
            return e.code, json.loads(body or "{}")
        except Exception:
            return e.code, {"raw": body[:200]}


def check(name, got, want):
    ok = got == want
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}: got={got!r} want={want!r}")
    if not ok:
        FAILS.append(name)


def check_true(name, cond, detail=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}{(' — ' + detail) if detail else ''}")
    if not cond:
        FAILS.append(name)


def clock():
    st, d = req("GET", "/api/state")
    return d["clock"]["ts"]


def main():
    print(f"對象伺服器 {BASE}")
    st, _ = req("GET", "/api/state")
    if st != 200:
        print("伺服器沒有回應，先啟動 uvicorn 再跑這個測試")
        return 2

    LATE = "2026-06-16 12:00"
    EARLY = "2026-06-01 01:30"     # 比工單建立時間早兩週

    print("步驟 1　把時鐘設在較晚的時刻，建立一張工單")
    st, _ = req("POST", "/api/clock", {"action": "set", "ts": LATE})
    check("設定時鐘", st, 200)
    st, tk = req("POST", "/api/ops/tickets",
                 {"sid": 172, "issue": "鏈條掉落", "bike_no": "YB-RW-" + os.urandom(3).hex().upper(),
                  "asset_type": "bike"})
    check("建立工單", st, 200)
    tid = tk["id"]
    st, tk0 = req("GET", f"/api/ops/tickets/{tid}")
    status_before = tk0["status"]
    print(f"    工單 {tid} 建立於 {tk0['ts']}，狀態 {status_before}")

    print("步驟 2　把時鐘往回跳到工單建立之前")
    st, d = req("POST", "/api/clock", {"action": "set", "ts": EARLY})
    check("時鐘回捲不得回 500", st, 200)
    check_true("回應不是錯誤內容", "raw" not in d, str(d)[:120])
    check("時鐘確實移動了", clock(), EARLY)

    print("步驟 3　回捲後工單不得倒退，也不得被刪掉")
    st, tk1 = req("GET", f"/api/ops/tickets/{tid}")
    check("工單仍在", st, 200)
    check("工單狀態未倒退", tk1["status"], status_before)

    print("步驟 4　回捲後同一個 tick 的後續步驟仍要執行（台帳算得出來）")
    st, led = req("GET", "/api/ledger")
    check("台帳可讀", st, 200)
    check_true("台帳有事件或至少有彙總", "counts" in led, str(list(led))[:120])
    check_true("全域儀表板仍在", "overview" in led and "supply_demand" in led["overview"])

    print("步驟 5　再往前走一步，時鐘與 tick 都要正常")
    st, d = req("POST", "/api/clock", {"action": "step"})
    check("往前一步", st, 200)
    check_true("時鐘前進", clock() > EARLY, clock())

    print("步驟 6　連續回捲兩次也不能崩")
    for ts in ("2026-06-10 08:00", "2026-06-02 03:00"):
        st, _ = req("POST", "/api/clock", {"action": "set", "ts": ts})
        check(f"回捲到 {ts}", st, 200)

    print()
    if FAILS:
        print(f"FAILED {len(FAILS)}: {FAILS}")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
