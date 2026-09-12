"""app/tickets.py 的 fixture 單元測試。
預期值全部手算，不拿實作輸出當答案；不 import server，不連伺服器。"""
import sys, datetime as dt
sys.path.insert(0, "/Users/chenhongfei/CC/ntpc-youbike/app")
import tickets as TK

T0 = dt.datetime(2026, 6, 16, 8, 0, 0)
NOW = {"t": T0}
def now_str(): return NOW["t"].strftime("%Y-%m-%d %H:%M:%S")
def now_iso(): return NOW["t"].strftime("%Y-%m-%d %H:%M")
def age_s(tk): return (NOW["t"] - dt.datetime.strptime(tk["ts"], "%Y-%m-%d %H:%M:%S")).total_seconds()
def advance(mins): NOW["t"] = NOW["t"] + dt.timedelta(minutes=mins)

fails = []
def check(name, got, want):
    okk = got == want
    print(f"[{'PASS' if okk else '**FAIL**'}] {name}\n        got={got!r}\n        want={want!r}" if not okk else f"[PASS] {name}  → {got!r}")
    if not okk: fails.append(name)

def fresh():
    NOW["t"] = T0; TK.SERVICE.reset(); return []

def submit(tks, **body):
    return TK.SERVICE.submit(tks, body, station_name="測試站", now_iso=now_iso(), now_str=now_str(), age_s=age_s)

# ---- C01 同 request_id 重送只建一張 ----
tks = fresh()
t1, a1 = submit(tks, sid=1, issue="輪胎破損", request_id="req-abc", source="citizen", symptom_keys=["tire"])
t2, a2 = submit(tks, sid=1, issue="輪胎破損", request_id="req-abc", source="citizen", symptom_keys=["tire"])
check("C01 第一次是建立", a1, "created")
check("C01 同 request_id 重送是冪等", a2, "idempotent")
check("C01 只有一張工單", len(tks), 1)
check("C01 兩次拿到同一個 ticket_id", t1["id"] == t2["id"], True)
check("C01 沒有圖片也能建單（明確機械問題直接報修）", t1["diagnosis"], "direct_repair")

# ---- I02 同站兩台不同車不合併 ----
tks = fresh()
b1, _ = submit(tks, sid=1, issue="煞車異常", bike_no="YB-001")
b2, _ = submit(tks, sid=1, issue="煞車異常", bike_no="YB-002")
check("I02 同站同問題但不同車號 → 兩張單", len(tks), 2)
check("I02 兩張單的 id 不同", b1["id"] != b2["id"], True)
b3, a3 = submit(tks, sid=1, issue="煞車異常", bike_no="YB-001")
check("I02 同一台車再回報 → 合併", a3, "merged")
check("I02 合併後回報次數 2", b3["reports"], 2)
check("I02 工單總數仍為 2", len(tks), 2)

# ---- I02 dock_no 相容並結構化保留 ----
tks = fresh()
d1, _ = submit(tks, sid=5, issue="卡樁", dock_no="07")
check("I02 dock_no 正規化成 dock_id", d1["dock_id"], "07")
check("I02 去重依據是柱號", d1["dedup_basis"], "dock")
check("I02 去重鍵含站號", d1["dedup_key"], "dock:5:07")
d2, a = submit(tks, sid=5, issue="卡樁", dock_id="08")
check("I02 同站不同柱 → 不合併", len(tks), 2)
d3, a = submit(tks, sid=5, issue="卡樁", dock_no="07")
check("I02 同柱再報 → 合併", a, "merged")

# ---- 無資產識別：只關聯不盲目合併 ----
tks = fresh()
x1, _ = submit(tks, sid=9, issue="車身異常", bike_no="YB-777")
x2, a = submit(tks, sid=9, issue="車身異常")          # 沒有任何識別
check("無識別的回報不與『有車號』的工單合併", a, "created")
check("無識別的回報會關聯到同站未結案工單", x1["id"] in x2["related_ids"], True)
x3, a = submit(tks, sid=9, issue="車身異常")          # 同樣沒識別、同站同問題、同一時刻
check("兩筆都沒識別且同站同問題 2 小時內 → 合併", a, "merged")
check("合併後總數仍為 2", len(tks), 2)
advance(121)
x4, a = submit(tks, sid=9, issue="車身異常")
check("超過 2 小時 → 不再合併", a, "created")

# ---- 空字串不能冒充識別 ----
tks = fresh()
e1, _ = submit(tks, sid=3, issue="其他", bike_no="   ", dock_no="")
check("空白車號收斂成 None", e1["bike_no"], None)
check("空柱號收斂成 None", e1["dock_id"], None)
check("沒有識別時退回站點層級", e1["dedup_basis"], "station")

# ---- 不確定問題保留待診斷 ----
tks = fresh()
u1, _ = submit(tks, sid=2, issue="借不到", symptom_keys=["cannot_borrow"], source="citizen")
check("不確定／供需類問題不直接判機械故障", u1["diagnosis"], "pending_triage")
check("沒有資產識別時設備驗收狀態不預設為可疑", u1["asset_state"], "not_applicable")

# ---- C03 坐墊標記不結案、不改工單狀態 ----
tks = fresh()
s1, _ = submit(tks, sid=1, issue="鏈條異常", bike_no="YB-900")
v_before = s1["version"]
TK.SERVICE.set_saddle(s1, "skipped", source="user_report", now_iso=now_iso())
check("C03 略過坐墊後工單狀態不變", s1["status"], "reported")
check("C03 坐墊標記記為 skipped", s1["saddle_marker"]["status"], "skipped")
check("C03 坐墊標記來源記為 user_report", s1["saddle_marker"]["source"], "user_report")
check("C03 坐墊標記不改設備驗收狀態", s1["asset_state"], "suspect")
check("C03 坐墊標記會讓版本前進（可被追蹤）", s1["version"], v_before + 1)
TK.SERVICE.set_saddle(s1, "done", source="user_report", now_iso=now_iso())
check("C03 標記完成仍不等於已維修", (s1["status"], s1["asset_state"]), ("reported", "suspect"))

# ---- N07 版本衝突：同決策重送只派一次、舊版本 409 ----
tks = fresh()
p1, _ = submit(tks, sid=1, issue="煞車異常", bike_no="YB-555")
ver = p1["version"]
r1, c1 = TK.SERVICE.transition(p1, "accepted", actor="維修一組", now_iso=now_iso(), version=ver, crew="維修一組")
r2, c2 = TK.SERVICE.transition(p1, "accepted", actor="維修一組", now_iso=now_iso(), version=ver, crew="維修一組")
check("N07 第一次接單成功", c1, "ok")
check("N07 帶舊版本重送 → 衝突", c2, "conflict")
check("N07 重送不會重複推進（仍只接一次單）", p1["status"], "accepted")
check("N07 指派的班組被記錄", p1["crew"], "維修一組")
r3, c3 = TK.SERVICE.transition(p1, "reported", actor="x", now_iso=now_iso())
check("流程不可倒退", c3, "backwards")
r4, c4 = TK.SERVICE.transition(p1, "accepted", actor="x", now_iso=now_iso())
check("推進到同一狀態視為 noop，不重複寫歷程", c4, "noop")

# ---- reset 要清掉自有狀態 ----
tks = fresh()
submit(tks, sid=1, issue="x", request_id="keep-me")
TK.SERVICE.reset()
tks2 = []
t, a = submit(tks2, sid=1, issue="x", request_id="keep-me")
check("reset 後同一個 request_id 會重新建立（冪等表已清）", a, "created")

print()
print("全部通過" if not fails else f"未通過 {len(fails)} 項：{fails}")
sys.exit(1 if fails else 0)
