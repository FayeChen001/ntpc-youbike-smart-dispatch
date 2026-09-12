"""
test_a_actions.py — A 主線事件動作的契約驗證（N07：ack≠指派、重送只做一次、舊版本 409）。

期望值全部手寫，不呼叫被測程式產生答案。
執行：python3 tests/test_a_actions.py
"""
import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(ROOT, "app"))
sys.path.insert(0, os.path.join(ROOT, "pipeline"))

import events as EV  # noqa: E402

FAILS = []
NOW = "2026-06-16 10:00"


def iso(x):
    return str(x)


def check(name, got, want):
    ok = got == want
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}: got={got!r} want={want!r}")
    if not ok:
        FAILS.append(name)


def check_true(name, cond, detail=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}{(' — ' + detail) if detail else ''}")
    if not cond:
        FAILS.append(name)


def new_event():
    return {"id": "ev-test-1", "sid": 7, "station": "測試站", "district": "測試區",
            "type": "persistent_empty", "severity": "high", "message": "測試事件",
            "status": "open", "opened": "2026-06-16 09:00", "acked": None, "resolved": None,
            "level": "P1", "owner": None, "timeline": []}


def main():
    print("N07-a　ack 不等於指派")
    ev = new_event()
    body, code = EV.apply_action(ev, "ack", {}, NOW, iso)
    check("ack 回傳碼", code, 200)
    check("ack 後有 acked 時間", ev["acked"], NOW)
    check("ack 後 owner 仍為 None", ev["owner"], None)
    check("ack 後沒有 assigned_at", ev.get("assigned_at"), None)
    check("ack 後版本遞增", ev["version"], 2)
    check_true("ack 的留痕明講不等於指派", "不代表已經指派給誰" in ev["timeline"][-1]["note"])

    print("N07-a　指派才算指派")
    body, code = EV.apply_action(ev, "assign", {"owner": "土城維運組", "version": 2}, NOW, iso)
    check("assign 回傳碼", code, 200)
    check("owner", ev["owner"], "土城維運組")
    check("assigned_at", ev["assigned_at"], NOW)
    check("版本", ev["version"], 3)

    print("N07-b　同一個決策重送只執行一次")
    ev2 = new_event()
    r1, c1 = EV.apply_action(ev2, "request_ops", {"request_id": "req-abc", "note": "請派人"}, NOW, iso)
    v_after_first = ev2["version"]
    n_after_first = len(ev2["ops_requests"])
    r2, c2 = EV.apply_action(ev2, "request_ops", {"request_id": "req-abc", "note": "請派人"}, NOW, iso)
    check("第一次回傳碼", c1, 200)
    check("第二次回傳碼", c2, 200)
    check("第一次不是重播", r1["idempotent"], False)
    check("第二次標記為重播", r2["idempotent"], True)
    check("要求處理只記一筆", len(ev2["ops_requests"]), n_after_first)
    check("重送不遞增版本", ev2["version"], v_after_first)
    tl_actions = [t["action"] for t in ev2["timeline"]]
    check("時間線只有一筆 request_ops", tl_actions.count("request_ops"), 1)

    print("N07-c　舊版本回 409，且不產生副作用")
    ev3 = new_event()
    EV.apply_action(ev3, "ack", {}, NOW, iso)            # version 1 -> 2
    EV.apply_action(ev3, "track", {"version": 2}, NOW, iso)  # version 2 -> 3
    before_ver = ev3["version"]
    before_owner = ev3["owner"]
    before_tl = len(ev3["timeline"])
    body, code = EV.apply_action(ev3, "assign", {"owner": "板橋維運組", "version": 2}, NOW, iso)
    check("舊版本回傳碼", code, 409)
    check("錯誤代碼", body["error"], "version_conflict")
    check("回傳目前版本", body["expected_version"], before_ver)
    check("回傳送來的版本", body["your_version"], 2)
    check("409 後版本不變", ev3["version"], before_ver)
    check("409 後 owner 不變", ev3["owner"], before_owner)
    check("409 後沒有多寫時間線", len(ev3["timeline"]), before_tl)

    print("N07-c　帶舊版本但同 request_id 的重送，視為重送不是衝突")
    ev4 = new_event()
    EV.apply_action(ev4, "assign", {"owner": "新莊維運組", "version": 1, "request_id": "req-xyz"}, NOW, iso)
    v = ev4["version"]
    body, code = EV.apply_action(ev4, "assign", {"owner": "新莊維運組", "version": 1, "request_id": "req-xyz"}, NOW, iso)
    check("重送回傳碼（不是 409）", code, 200)
    check("重送標記為冪等", body["idempotent"], True)
    check("重送後版本不變", ev4["version"], v)

    print("政府端動作不得建立或修改派車任務")
    ev5 = new_event()
    tasks = [{"id": "T001", "district": "測試區", "status": "planned", "stops": [], "load": 5}]
    snapshot = repr(tasks)
    for act, pl in [("track", {}), ("request_ops", {"note": "請處理"}),
                    ("coordinate", {"districts": ["板橋區"], "note": "需要跨區支援"}),
                    ("assign", {"owner": "值班調度 A"}), ("note", {"note": "已電話聯繫"})]:
        EV.apply_action(ev5, act, pl, NOW, iso)
    check("任務清單未被動到", repr(tasks), snapshot)
    check_true("事件沒有長出 task 欄位", "task" not in ev5 and "stops" not in ev5,
               f"keys={sorted(ev5)}")
    co = ev5["coordination"][-1]
    check("跨區協調記錄了行政區", co["districts"], ["板橋區"])
    check_true("協調留痕明講不是派車任務",
               "不是派車任務" in [t["note"] for t in ev5["timeline"] if t["action"] == "coordinate"][0])
    check_true("要求處理留痕明講派工由營運端決定",
               "由營運端決定" in [t["note"] for t in ev5["timeline"] if t["action"] == "request_ops"][0])

    print("防呆")
    ev6 = new_event()
    body, code = EV.apply_action(ev6, "assign", {}, NOW, iso)
    check("指派沒帶 owner", code, 400)
    body, code = EV.apply_action(ev6, "note", {"note": "   "}, NOW, iso)
    check("空白處理紀錄", code, 400)
    body, code = EV.apply_action(ev6, "delete_everything", {}, NOW, iso)
    check("未知動作", code, 400)
    check("版本在失敗後不變", ev6["version"], 1)

    print()
    if FAILS:
        print(f"FAILED {len(FAILS)}: {FAILS}")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
