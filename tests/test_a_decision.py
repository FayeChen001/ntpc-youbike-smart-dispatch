"""
test_a_decision.py — 主管決策卡：方案只能來自營運端、決策留痕、防雙重派工。

期望值手寫，不呼叫被測程式產生答案。
執行：python3 tests/test_a_decision.py
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
    return {"id": "ev-dec", "sid": SID, "station": "測試站", "district": "測試區",
            "type": "persistent_empty", "severity": "high", "message": "測試事件",
            "status": "open", "opened": "2026-06-16 09:00", "acked": None, "resolved": None,
            "level": "P0", "owner": None, "timeline": []}


KEEP_TASK = {"id": "T-KEEP", "district": "測試區", "status": "planned", "horizon": 120,
             "version": 3, "cycle": "CY-1", "reason": "區內供給足夠",
             "stops": [{"sid": SID, "name": "測試站", "action": "dropoff", "qty": 5,
                        "arrives_by": "2026-06-16 11:00", "on_time": True}]}
CROSS_TASK = {"id": "G-CROSS", "district": "測試區", "status": "needs_cross_district",
              "horizon": 120, "stops": [], "reason": "區內無可供給站，需跨區調車"}


def main():
    ev = new_event()

    print("方案來源：只採用營運端既有任務，沒有的一律標未提供")
    o = EV.decision_options({"tasks": [KEEP_TASK]}, ev)
    check("方案數", len(o["options"]), 1)
    k = o["options"][0]
    check("方案 id", k["id"], "keep:T-KEEP")
    check("帶營運端任務版本", k["task_version"], 3)
    check("ETA 來自營運端任務", k["eta"], "2026-06-16 11:00")
    check("動作標示為送車補給", k["action"], "送車補給")
    check("缺的方案數（跨區未提供）", len(o["unavailable"]), 1)
    check("缺的是跨區", o["unavailable"][0]["id"], "cross")
    check_true("規則文字明講政府端不估算 ETA", "政府端不估算抵達時間" in o["source_rule"])
    check_true("標示營運端尚未發布改派方案端點", o["contract_gap"] is not None)

    print("完全沒有任務時，兩個方案都要標未提供，且不得自行生出 ETA")
    o2 = EV.decision_options({"tasks": []}, ev)
    check("方案數", len(o2["options"]), 0)
    check("缺的方案數", len(o2["unavailable"]), 2)
    check_true("沒有任何方案帶 eta", all("eta" not in x for x in o2["unavailable"]))

    print("跨區任務存在時，代價照抄營運端理由，ETA 保持未知")
    o3 = EV.decision_options({"tasks": [CROSS_TASK]}, ev)
    cross = [x for x in o3["options"] if x["id"].startswith("cross:")][0]
    check("跨區方案代價照抄", cross["cost"], "區內無可供給站，需跨區調車")
    check("跨區方案 ETA 為 None", cross["eta"], None)
    check_true("說明為何沒有 ETA", "政府端不自行估算" in cross["eta_missing_reason"])

    print("決策必須帶方案與理由")
    body, code = EV.apply_action(ev, "decide", {"note": "只有理由沒有方案"}, NOW, iso)
    check("缺方案", code, 400)
    check("錯誤碼", body["error"], "option_required")
    body, code = EV.apply_action(ev, "decide", {"option": {"id": "keep:T-KEEP"}}, NOW, iso)
    check("缺理由", code, 400)
    check("錯誤碼", body["error"], "rationale_required")
    check("失敗後版本不變", ev["version"], 1)

    print("決策留痕：方案、理由、任務版本、事件版本、時間")
    body, code = EV.apply_action(ev, "decide",
                                 {"option": k, "note": "區內有供給，維持原排程最快", "request_id": "D1"},
                                 NOW, iso)
    check("回傳碼", code, 200)
    d = ev["decisions"][-1]
    check("決策方案", d["option_label"], "維持現行方案")
    check("決策理由", d["rationale"], "區內有供給，維持原排程最快")
    check("保存營運端任務版本", d["task_version"], 3)
    check("保存決策當下的事件版本", d["event_version_at_decision"], 1)
    check("保存時間", d["ts"], NOW)
    check("尚未被取代", d["superseded"], False)
    tl = [t for t in ev["timeline"] if t["action"] == "decide"]
    check("時間線有一筆決策", len(tl), 1)
    check_true("留痕明講派工仍由營運端執行", "派工仍由營運端執行" in tl[0]["note"])

    print("防雙重派工：已有有效決策時再決策要回 409")
    body, code = EV.apply_action(ev, "decide",
                                 {"option": k, "note": "再派一次"}, NOW, iso)
    check("重複決策", code, 409)
    check("錯誤碼", body["error"], "decision_exists")
    check_true("回傳既有決策", body["existing"]["option_id"] == "keep:T-KEEP")
    check("409 後仍只有一筆決策", len(ev["decisions"]), 1)

    print("要改決策必須明講取代，且舊決策標記為已取代")
    v_before = ev["version"]
    body, code = EV.apply_action(ev, "decide",
                                 {"option": {"id": "cross:G-CROSS", "label": "跨區支援",
                                             "source": "營運端任務（needs_cross_district）"},
                                  "note": "區內車源已用盡，改跨區", "supersede": True}, NOW, iso)
    check("取代決策", code, 200)
    check("決策共兩筆", len(ev["decisions"]), 2)
    check("舊決策標記已取代", ev["decisions"][0]["superseded"], True)
    check("舊決策記錄取代原因", ev["decisions"][0]["superseded_reason"], "區內車源已用盡，改跨區")
    check("目前有效決策", EV.active_decision(ev)["option_id"], "cross:G-CROSS")
    check("版本遞增", ev["version"], v_before + 1)

    print("同 request_id 重送決策不會再決一次")
    ev2 = new_event()
    EV.apply_action(ev2, "decide", {"option": k, "note": "第一次", "request_id": "DX"}, NOW, iso)
    n1 = len(ev2["decisions"])
    b2, c2 = EV.apply_action(ev2, "decide", {"option": k, "note": "第一次", "request_id": "DX"}, NOW, iso)
    check("重送回傳碼", c2, 200)
    check("標記為冪等", b2["idempotent"], True)
    check("決策筆數不變", len(ev2["decisions"]), n1)

    print("決策不得動到營運端任務")
    tasks = [dict(KEEP_TASK)]
    snap = repr(tasks)
    ev3 = new_event()
    EV.apply_action(ev3, "decide", {"option": k, "note": "維持"}, NOW, iso)
    check("任務清單未被動到", repr(tasks), snap)

    print()
    if FAILS:
        print(f"FAILED {len(FAILS)}: {FAILS}")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
