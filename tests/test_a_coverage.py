"""
test_a_coverage.py — A-5：缺車靠 dropoff、缺位靠 pickup，兩者不得互用。

期望值手寫，不呼叫被測程式產生答案。
執行：python3 tests/test_a_coverage.py
"""
import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(ROOT, "app"))
sys.path.insert(0, os.path.join(ROOT, "pipeline"))

import events as EV  # noqa: E402

FAILS = []
NOW = "2026-06-16 10:00"
SID = 42


def check(name, got, want):
    ok = got == want
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}: got={got!r} want={want!r}")
    if not ok:
        FAILS.append(name)


def check_true(name, cond, detail=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}{(' — ' + detail) if detail else ''}")
    if not cond:
        FAILS.append(name)


def task(tid, action):
    return {"id": tid, "district": "測試區", "status": "planned", "horizon": 120,
            "stops": [{"sid": SID, "name": "測試站", "action": action, "qty": 5,
                       "arrives_by": "2026-06-16 11:00", "on_time": True}]}


DROP_ONLY = [task("T-DROP", "dropoff")]
PICK_ONLY = [task("T-PICK", "pickup")]
BOTH = [task("T-DROP", "dropoff"), task("T-PICK", "pickup")]


def main():
    print("缺車事件：只有 dropoff 任務才算有覆蓋")
    tk, st = EV.covering_task(DROP_ONLY, SID, "empty")
    check("缺車 × 只有送車任務", tk["id"] if tk else None, "T-DROP")
    tk, st = EV.covering_task(PICK_ONLY, SID, "empty")
    check("缺車 × 只有運出任務（不算覆蓋）", tk["id"] if tk else None, None)

    print("缺位事件：只有 pickup 任務才算有覆蓋")
    tk, st = EV.covering_task(PICK_ONLY, SID, "full")
    check("缺位 × 只有運出任務", tk["id"] if tk else None, "T-PICK")
    tk, st = EV.covering_task(DROP_ONLY, SID, "full")
    check("缺位 × 只有送車任務（不算覆蓋）", tk["id"] if tk else None, None)

    print("兩種任務都在時，各自對到正確那一張")
    check("缺車 → dropoff 那張", EV.covering_task(BOTH, SID, "empty")[0]["id"], "T-DROP")
    check("缺位 → pickup 那張", EV.covering_task(BOTH, SID, "full")[0]["id"], "T-PICK")

    print("原因推論：缺位事件在只有送車任務時，理由要說成『沒有任務為這一站運出騰位』")
    state = {"tasks": PICK_ONLY, "tickets": [], "scenario": {}}
    ev_full = {"sid": SID, "district": "測試區", "type": "persistent_full",
               "observed": {"last_data_ts": NOW}}
    c = EV.infer_cause(ev_full, state, NOW)
    check("缺位 × 有運出任務 → 不是『沒排到任務』", c["code"], "unknown")

    state_drop = {"tasks": DROP_ONLY, "tickets": [], "scenario": {}}
    c2 = EV.infer_cause(ev_full, state_drop, NOW)
    check("缺位 × 只有送車任務 → 沒排到任務", c2["code"], "no_task")
    txt = c2["evidence"][0]["text"]
    check_true("理由文字用『運出騰位』而不是『送車』", "運出騰位" in txt, txt)
    check_true("理由文字仍保留不可推論人員未到場的但書", "不能據此推論沒有人到過現場" in txt)

    ev_empty = {"sid": SID, "district": "測試區", "type": "persistent_empty",
                "observed": {"last_data_ts": NOW}}
    c3 = EV.infer_cause(ev_empty, state_drop, NOW)
    check("缺車 × 有送車任務 → 不是『沒排到任務』", c3["code"], "unknown")
    c4 = EV.infer_cause(ev_empty, {"tasks": PICK_ONLY, "tickets": [], "scenario": {}}, NOW)
    check("缺車 × 只有運出任務 → 沒排到任務", c4["code"], "no_task")
    check_true("缺車的理由文字用『送車補給』", "送車補給" in c4["evidence"][0]["text"])

    print("動作標籤對照")
    check("缺車對應動作", EV.COVER_ACTION["empty"], "dropoff")
    check("缺位對應動作", EV.COVER_ACTION["full"], "pickup")

    print()
    if FAILS:
        print(f"FAILED {len(FAILS)}: {FAILS}")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
