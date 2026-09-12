"""HTTP 整合測試：驗證兩個建單入口真的走同一個服務。跑在臨時的 8789，不動共用的 8787。
預期值由契約手算，不採信實作輸出。"""
import json, os, sys, urllib.request, urllib.error
B = os.environ.get("YB_BASE", "http://127.0.0.1:8789")   # 預設打測試機，不要對共用機亂跑
def req(method, path, body=None):
    d = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(B + path, data=d, method=method,
                               headers={"content-type": "application/json"})
    try:
        with urllib.request.urlopen(r, timeout=30) as resp: return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e: return e.code, json.loads(e.read().decode() or "{}")
GET = lambda p: req("GET", p); POST = lambda p, b=None: req("POST", p, b or {})

fails = []
def check(name, got, want):
    okk = got == want
    print(("[PASS] " if okk else "[**FAIL**] ") + name + f"  → {got!r}" + ("" if okk else f"   want={want!r}"))
    if not okk: fails.append(name)

POST("/api/reset")
sid = GET("/api/stations?adjusted=1")[1]["stations"][0]["sid"]
n0 = len(GET("/api/tickets")[1]["tickets"])
check("起點沒有工單", n0, 0)

# C01：同 request_id 重送只建一張（走舊路由 /api/tickets，證明它已委派同一服務）
s1, t1 = POST("/api/tickets", {"sid": sid, "issue": "輪胎破損", "request_id": "http-req-1", "source": "citizen", "symptom_keys": ["tire"]})
s2, t2 = POST("/api/tickets", {"sid": sid, "issue": "輪胎破損", "request_id": "http-req-1", "source": "citizen", "symptom_keys": ["tire"]})
check("C01 舊路由建單成功", s1, 200)
check("C01 重送拿到同一張單", t1["id"] == t2["id"], True)
check("C01 工單總數 1", len(GET("/api/tickets")[1]["tickets"]), 1)

# I02：同站兩台不同車 → 兩張單（一個走舊路由、一個走新路由，證明同一服務）
POST("/api/reset")
_, a = POST("/api/tickets", {"sid": sid, "issue": "煞車異常", "bike_no": "YB-A1"})
_, b = POST("/api/ops/tickets", {"sid": sid, "issue": "煞車異常", "bike_no": "YB-A2"})
tks = GET("/api/tickets")[1]["tickets"]
check("I02 兩個入口各建一張，不互相合併", len(tks), 2)
check("I02 兩張單 id 不同", a["id"] != b["id"], True)
_, c = POST("/api/tickets", {"sid": sid, "issue": "煞車異常", "bike_no": "YB-A1"})
check("I02 同一台車跨入口回報 → 合併回第一張", c["id"], a["id"])
check("I02 合併後仍是兩張單", len(GET("/api/tickets")[1]["tickets"]), 2)

# dock_no 相容：舊民眾端欄位名也要結構化保留
POST("/api/reset")
_, d = POST("/api/tickets", {"sid": sid, "issue": "卡樁", "dock_no": "07"})
check("dock_no 正規化成 dock_id 並保留", d["dock_id"], "07")
check("去重依據是柱號", d["dedup_basis"], "dock")
_, e = POST("/api/tickets", {"sid": sid, "issue": "卡樁", "dock_no": "08"})
check("同站不同柱不合併", len(GET("/api/tickets")[1]["tickets"]), 2)

# C03：坐墊標記不結案、不改狀態
POST("/api/reset")
_, f = POST("/api/ops/tickets", {"sid": sid, "issue": "鏈條異常", "bike_no": "YB-S1"})
_, g = POST(f"/api/ops/tickets/{f['id']}/saddle", {"status": "skipped", "source": "user_report"})
check("C03 略過坐墊後工單仍在處理中", g["status"], "reported")
check("C03 坐墊標記記錄為 skipped", g["saddle_marker"]["status"], "skipped")
check("C03 設備驗收狀態未被改成已維修", g["asset_state"], "suspect")

# N07：版本衝突，重送不重派
POST("/api/reset")
_, h = POST("/api/ops/tickets", {"sid": sid, "issue": "煞車異常", "bike_no": "YB-V1"})
v = h["version"]
s_ok, r1 = POST(f"/api/ops/tickets/{h['id']}/transition", {"to": "accepted", "version": v, "crew": "維修一組", "eta": "08:40"})
s_cf, r2 = POST(f"/api/ops/tickets/{h['id']}/transition", {"to": "accepted", "version": v, "crew": "維修一組"})
check("N07 第一次接單 200", s_ok, 200)
check("N07 舊版本重送回 409", s_cf, 409)
check("N07 狀態仍只推進一次", GET(f"/api/ops/tickets/{h['id']}")[1]["status"], "accepted")
check("N07 班組與 ETA 有被記錄", (r1["crew"], r1["eta"]), ("維修一組", "08:40"))
# 修復不等於自動驗收
_, r3 = POST(f"/api/ops/tickets/{h['id']}/transition", {"to": "recovered", "version": r1["version"]})
check("I01 處理完成後設備狀態是已維修、尚未驗收", (r3["status"], r3["asset_state"]), ("recovered", "repaired"))
check("I01 未驗收前不得為結案", r3["status"] != "closed", True)

# GET 無副作用
before = json.dumps(GET("/api/tickets")[1]["tickets"], sort_keys=True)
GET("/api/ops/tickets"); GET("/api/ops/tickets/dedup"); GET(f"/api/ops/tickets/{h['id']}"); GET("/api/ops/contract")
after = json.dumps(GET("/api/tickets")[1]["tickets"], sort_keys=True)
check("I03 GET 不改狀態（無副作用）", before == after, True)

# reset 清掉冪等表
POST("/api/reset")
_, z1 = POST("/api/tickets", {"sid": sid, "issue": "x", "request_id": "reset-me"})
POST("/api/reset")
_, z2 = POST("/api/tickets", {"sid": sid, "issue": "x", "request_id": "reset-me"})
check("reset 後同 request_id 會重新建立", len(GET("/api/tickets")[1]["tickets"]), 1)

# 待診斷分流
POST("/api/reset")
_, u = POST("/api/ops/tickets", {"sid": sid, "issue": "刷卡沒反應", "symptom_keys": ["cannot_borrow"], "source": "citizen"})
check("借不到／刷卡沒反應 → 待診斷，不直接判車壞", u["diagnosis"], "pending_triage")
check("待診斷清單撈得到", len(GET("/api/ops/tickets?pending=1")[1]["tickets"]), 1)

POST("/api/reset")
print()
print("全部通過" if not fails else f"未通過 {len(fails)} 項：{fails}")
sys.exit(1 if fails else 0)
