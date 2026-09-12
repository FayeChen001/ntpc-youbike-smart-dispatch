"""資源帳與逐站期限的 fixture 測試（N03/N04/N05）。
用自己搭的小型 fixture，預期值手算；不連伺服器、不用實作輸出當答案。"""
import sys, math
import pandas as pd, numpy as np
sys.path.insert(0, "/Users/chenhongfei/CC/ntpc-youbike/app")
import planner as PL
A = PL.ASSUMPTIONS
NOW = pd.Timestamp("2026-06-16 08:00:00")
fails = []
def check(name, got, want):
    okk = got == want
    print(("[PASS] " if okk else "[**FAIL**] ") + name + f"  → {got!r}" + ("" if okk else f"   want={want!r}"))
    if not okk: fails.append(name)

H = (30, 60, 120, 180)
def stn(sid, name, lat, lon, cap, bikes, pb, pe):
    """pb/pe 可給單一值或 dict{h:值}。spaces 由 cap-bikes 推。"""
    pbd = pb if isinstance(pb, dict) else {h: pb for h in H}
    ped = pe if isinstance(pe, dict) else {h: pe for h in H}
    r = {"sid": sid, "district": "測試區", "name": name, "lat": lat, "lon": lon,
         "cap": cap, "bikes": bikes, "spaces": cap - bikes, "status": "normal"}
    for h in H:
        r[f"pb_{h}"] = pbd[h]; r[f"ps_{h}"] = cap - pbd[h]
        r[f"pe_{h}"] = ped[h]; r[f"pf_{h}"] = 0.0
    return r

def min_stock(cap): return max(A["min_stock_abs"], math.ceil(cap * A["min_stock_ratio"]))

# ============ N03：來源可供 10 輛、兩個缺車站各要 8 輛 ============
# 手算：S0 cap=40, pb=30 → min_stock=max(2,ceil(6))=6, keep=max(6, 40*0.5=20)=20 → donor=30-20=10
#       S1/S2 cap=60, pb=1 → min_stock=max(2,ceil(9))=9 → need_in=9-1=8
print("== N03 ==")
check("N03 手算：供給站可供給量", 30 - max(min_stock(40), 40 * A["donor_keep_ratio"]), 10.0)
check("N03 手算：每個缺車站的缺口", min_stock(60) - 1, 8)
pred = pd.DataFrame([
    stn(0, "供給站", 25.000, 121.400, 40, 30, 30, 0.0),
    stn(1, "缺車站甲", 25.002, 121.402, 60, 1, 1, 0.9),
    stn(2, "缺車站乙", 25.004, 121.404, 60, 1, 1, 0.9),
])
PL.ledger_reset()
saved = A["depot_supply"]; A["depot_supply"] = False        # 關掉跨區整車補給，才看得出區內來源的上限
try:
    cid = PL.cycle_open(NOW)
    tasks = PL.plan_dispatch(pred, NOW, 120, None, districts=["測試區"], existing_locked=set(), cycle=cid)
finally:
    A["depot_supply"] = saved
took = sum(s["qty"] for t in tasks for s in t["stops"] if s["action"] == "pickup" and s["sid"] == 0)
sent = sum(s["qty"] for t in tasks for s in t["stops"] if s["action"] == "dropoff")
check("N03 從供給站抽走的總量不超過 10", took <= 10, True)
check("N03 實際抽走", took, 10)
check("N03 送出的量等於抽走的量（載量守恆）", sent, took)
snap = PL.cycle_snapshot(cid)
sup = next(x for x in snap["by_station"] if x["kind"] == "supply" and x["sid"] == 0)
check("N03 帳上對該供給站的承諾合計 ≤ 10", sup["candidate"] <= 10, True)
gap = [t for t in tasks if t["status"] == "gap_summary"]
check("N03 有出缺口摘要", len(gap), 1)
check("N03 缺口總量 16（8+8）", gap[0]["deficit_total"], 16)
check("N03 剩餘缺口明示為 6（16−10）", gap[0]["remaining"], 6)

# ============ N04：同週期重算不重扣、取消釋放、GET 無副作用 ============
print("\n== N04 ==")
PL.ledger_reset()
A["depot_supply"] = False
try:
    cid = PL.cycle_open(NOW)
    t1 = PL.plan_dispatch(pred, NOW, 120, None, districts=["測試區"], existing_locked=set(), cycle=cid)
    took1 = sum(s["qty"] for t in t1 for s in t["stops"] if s["action"] == "pickup" and s["sid"] == 0)
    t2 = PL.plan_dispatch(pred, NOW, 120, None, districts=["測試區"], existing_locked=set(), cycle=cid)
    took2 = sum(s["qty"] for t in t2 for s in t["stops"] if s["action"] == "pickup" and s["sid"] == 0)
    check("N04 同一尺度重算：抽走量不會累加（候選先釋放）", (took1, took2), (10, 10))
    s2 = PL.cycle_snapshot(cid)
    sup2 = next(x for x in s2["by_station"] if x["kind"] == "supply" and x["sid"] == 0)
    check("N04 重算後帳上有效承諾仍是 10，不是 20", sup2["candidate"], 10.0)
    check("N04 被釋放的舊候選記為 released", sup2["released"], 10.0)

    # 確認其中一趟後再重算，已確認的不可以被釋放
    trip = next(t for t in t2 if t["stops"])
    PL.cycle_bind_task(cid, trip["reservations"], "T900")
    moved = PL.cycle_set_state(cid, "T900", "confirmed")
    check("N04 確認派工後預約狀態改為 confirmed", moved > 0, True)
    t3 = PL.plan_dispatch(pred, NOW, 120, None, districts=["測試區"], existing_locked=set(), cycle=cid)
    s3 = PL.cycle_snapshot(cid)
    sup3 = next(x for x in s3["by_station"] if x["kind"] == "supply" and x["sid"] == 0)
    check("N04 重算不會釋放已確認的承諾", sup3["confirmed"] > 0, True)
    check("N04 已確認＋新候選合計仍不超過可供給量 10", sup3["confirmed"] + sup3["candidate"] <= 10, True)

    # GET（快照）不可有副作用
    before = PL.cycle_snapshot(cid)
    for _ in range(3): PL.cycle_snapshot(cid)
    after = PL.cycle_snapshot(cid)
    check("N04 快照重複讀取不改變帳本", before == after, True)

    # 取消要釋放
    PL.cycle_set_state(cid, "T900", "released")
    s4 = PL.cycle_snapshot(cid)
    sup4 = next(x for x in s4["by_station"] if x["kind"] == "supply" and x["sid"] == 0)
    check("N04 取消後已確認歸零", sup4["confirmed"], 0.0)
finally:
    A["depot_supply"] = saved

# ============ N05：第二個送站超時、第一站準時 ============
print("\n== N05 ==")
# 甲站近、期限 180 分；乙站遠、期限 30 分。手算：ready=now+15，乙站 eta 遠大於 15 分 → 必晚
pred2 = pd.DataFrame([
    stn(0, "供給站", 25.000, 121.400, 60, 50, 50, 0.0),
    stn(1, "近站甲", 25.003, 121.400, 60, 1, {30: 20, 60: 15, 120: 10, 180: 1}, {30: 0.0, 60: 0.1, 120: 0.2, 180: 0.9}),
    stn(2, "遠站乙", 25.035, 121.400, 60, 1, {30: 1, 60: 1, 120: 1, 180: 1}, {30: 0.9, 60: 0.9, 120: 0.9, 180: 0.9}),
])
PL.ledger_reset()
tk = PL.plan_dispatch(pred2, NOW, 180, None, districts=["測試區"], existing_locked=set())
trip = next(t for t in tk if t["stops"])
drops = [s for s in trip["stops"] if s["action"] == "dropoff"]
by = {s["name"]: s for s in drops}
check("N05 兩個送車站都在同一趟", sorted(by.keys()), ["近站甲", "遠站乙"])
check("N05 近站甲的服務時限是 180 分", by["近站甲"]["due_min"], 180)
check("N05 遠站乙的服務時限是 30 分", by["遠站乙"]["due_min"], 30)
check("N05 近站甲判定為可如期", by["近站甲"]["on_time"], True)
check("N05 遠站乙判定為趕不上", by["遠站乙"]["on_time"], False)
check("N05 趕不上的站有記錄晚幾分鐘", by["遠站乙"]["late_min"] > 0, True)
check("N05 任務的 late_stops 只列遠站乙", trip["late_stops"], ["遠站乙"])
check("N05 可如期站數為 1，不是全部", trip["on_time_stops"], 1)
check("N05 理由不可宣稱全部準時", "都能在各自的服務時限前抵達" in trip["reason"], False)
check("N05 理由要說出有幾站趕不上", "趕不上自己的服務時限" in trip["reason"], True)

# ============ 逐段載量守恆：路途中任何一刻都不可超過車容量，也不可為負 ============
print("\n== 逐段載量 ==")
PL.ledger_reset()
pred3 = pd.DataFrame([
    stn(0, "供給甲", 25.000, 121.400, 60, 50, 50, 0.0),
    stn(1, "供給乙", 25.002, 121.401, 60, 45, 45, 0.0),
    stn(2, "缺車甲", 25.004, 121.402, 60, 1, 1, 0.9),
    stn(3, "缺車乙", 25.006, 121.403, 60, 1, 1, 0.9),
    stn(4, "缺車丙", 25.008, 121.404, 60, 1, 1, 0.9),
])
tk3 = PL.plan_dispatch(pred3, NOW, 120, None, districts=["測試區"], existing_locked=set())
bad = []
for t in [x for x in tk3 if x["stops"]]:
    load = 0; trace = []
    for s_ in t["stops"]:
        load += s_["qty"] if s_["action"] == "pickup" else -s_["qty"]
        trace.append(load)
        if load < 0 or load > A["truck_capacity"]: bad.append((t.get("trip"), s_["name"], load))
    if load != 0: bad.append((t.get("trip"), "結束時未清空", load))
check("逐段載量never超過車容量也不為負，且回場時清空", bad, [])
check("每趟載量都在 0..上限之間", all(0 <= x["load"] <= A["truck_capacity"] for x in tk3 if x["stops"]), True)

# ============ 前置時間拆段：新動員與在勤改道是兩個不同的值 ============
print("\n== 前置拆段 ==")
check("新動員前置存在且為情境值 15 分", A.get("lead_prepare_min"), 15)
check("在勤改道前置存在且較短", A.get("divert_prepare_min") < A.get("lead_prepare_min"), True)
check("訪談的 60 分總量仍保留給三端顯示", A.get("lead_time_min"), 60)

print()
print("全部通過" if not fails else f"未通過 {len(fails)} 項：{fails}")
sys.exit(1 if fails else 0)
