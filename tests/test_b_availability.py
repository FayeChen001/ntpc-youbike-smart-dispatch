"""服務可用性彙總：不重扣官方數字、不整站判不可用、不聲稱鎖車。需 8789 測試機。"""
import json, sys, urllib.request, urllib.error
B = "http://127.0.0.1:8789"
def req(m, p, b=None):
    d = json.dumps(b).encode() if b is not None else None
    r = urllib.request.Request(B + p, data=d, method=m, headers={"content-type": "application/json"})
    try:
        with urllib.request.urlopen(r, timeout=30) as x: return x.status, json.loads(x.read().decode())
    except urllib.error.HTTPError as e: return e.code, json.loads(e.read().decode() or "{}")
GET = lambda p: req("GET", p); POST = lambda p, b=None: req("POST", p, b or {})
fails = []
def check(name, got, want):
    okk = got == want
    print(("[PASS] " if okk else "[**FAIL**] ") + name + f"  → {got!r}" + ("" if okk else f"   want={want!r}"))
    if not okk: fails.append(name)

POST("/api/reset")
sts = GET("/api/stations?adjusted=1")[1]["stations"]
tgt = next(s for s in sts if (s["bikes"] or 0) >= 3)        # 找一個有車的站
sid = tgt["sid"]; official = int(tgt["bikes"])
print(f"測試站 sid={sid} {tgt['name']}，官方可借 {official} 輛")

base = GET(f"/api/ops/availability?sid={sid}")[1]["stations"][0]
check("沒有工單時已知不可用為 0", base["known_unusable_bikes"], 0)
check("沒有工單時不排除推薦", base["excluded_from_recommendation"], False)
check("官方可借數就是快照值", base["official_bikes"], official)

# 開兩張不同車號的工單
POST("/api/ops/tickets", {"sid": sid, "issue": "煞車異常", "bike_no": "YB-X1"})
POST("/api/ops/tickets", {"sid": sid, "issue": "輪胎破損", "bike_no": "YB-X2"})
a = GET(f"/api/ops/availability?sid={sid}")[1]["stations"][0]
check("兩台不同車 → 已知不可用 2", a["known_unusable_bikes"], 2)
check("列出車號", a["known_unusable_bike_nos"], ["YB-X1", "YB-X2"])
check("**官方可借數不被扣掉**（不重扣）", a["official_bikes"], official)
check("排除推薦但不宣稱鎖車", a["excluded_from_recommendation"], True)
check("未達整站門檻時不建議開服務中斷事件", a["may_need_service_event"], official <= 2)

# 無識別的待診斷單不能算進不可用
POST("/api/ops/tickets", {"sid": sid, "issue": "刷卡沒反應", "symptom_keys": ["no_response"]})
b = GET(f"/api/ops/availability?sid={sid}")[1]["stations"][0]
check("沒有資產識別的單不計入不可用", b["known_unusable_bikes"], 2)
check("改列為疑似異常", len(b["suspected_tickets"]) >= 1, True)

# 工單結案後要從不可用移除
tk = GET("/api/ops/tickets")[1]["tickets"]
one = next(t for t in tk if t.get("bike_no") == "YB-X1")
v = one["version"]
for to in ("accepted", "on_site", "recovered", "verified"):
    s, one = POST(f"/api/ops/tickets/{one['id']}/transition", {"to": to, "version": one["version"]})
c = GET(f"/api/ops/availability?sid={sid}")[1]["stations"][0]
check("驗收後該車不再列為不可用", c["known_unusable_bikes"], 1)
check("剩下的車號正確", c["known_unusable_bike_nos"], ["YB-X2"])

# 語意欄位必須存在（避免 A 端自己猜）
sem = GET(f"/api/ops/availability?sid={sid}")[1]["semantics"]
check("語意說明含『未扣除』字樣", "未扣除" in sem["official_bikes"], True)
check("語意說明含『不可相加』", "不可相加" in sem["known_unusable_bikes"], True)
check("旗標說明為建議、需人工確認", "需人工確認" in sem["may_need_service_event"], True)
check("明示沒有遠端停租", "不代表已鎖車" in sem["excluded_from_recommendation"], True)

# GET 無副作用
n1 = len(GET("/api/tickets")[1]["tickets"])
for _ in range(3): GET(f"/api/ops/availability?sid={sid}")
check("重複查詢不改變工單", len(GET("/api/tickets")[1]["tickets"]), n1)

POST("/api/reset")
print()
print("全部通過" if not fails else f"未通過 {len(fails)} 項：{fails}")
sys.exit(1 if fails else 0)
