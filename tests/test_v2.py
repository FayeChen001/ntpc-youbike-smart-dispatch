#!/usr/bin/env python3
"""v2 一站式的驗收測試。

離線部分不需要伺服器；HTTP 部分預設打 8790，要打別的埠設 YB_BASE。
**不要對 8787（共用展示機）跑**——本檔會 POST 領取獎勵，會改到展示狀態。

期望值一律在本檔內手算，不取被測程式的輸出。

    python3 tests/test_v2.py
"""
import json
import math
import os
import sys
import urllib.error
import urllib.request

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(ROOT, "app"))
BASE = os.environ.get("YB_BASE", "http://127.0.0.1:8790")

if BASE.rstrip("/").endswith(":8787"):
    print("拒絕執行：8787 是三端共用展示機，本檔會建立獎勵紀錄。請另開埠並設 YB_BASE。")
    sys.exit(2)

PASS = FAIL = 0


def ck(name, got, want):
    global PASS, FAIL
    if got == want:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}\n       實得 {got!r}\n       應為 {want!r}")


def ck_true(name, cond, hint=""):
    ck(name + (f" ({hint})" if hint else ""), bool(cond), True)


def get(path):
    with urllib.request.urlopen(BASE + path, timeout=40) as r:
        return json.loads(r.read().decode())


def post(path, payload):
    req = urllib.request.Request(BASE + path, method="POST",
                                 data=json.dumps(payload).encode(),
                                 headers={"content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=40) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode() or "{}")


# ====================================================== 1. 離線：倍率公式
def test_multiplier():
    print("\n[1] 動態加碼倍率（docs/REWARDS.md 第 1 層）")
    import v2api

    m = v2api._multiplier
    # 倍率 = 1 + min(4, 缺口權重 + 急迫權重 + 距離權重)
    # 缺口權重 = min(2, 缺口比例 × 2)；急迫 <60 分=1.5、<120=1.0、其他=0.5；距離 ≥8 分=0.5
    ck("無缺口、不急、不繞路 → 1+0+0.5=1.5", m(0.0, 999, 0), 1.5)
    ck("缺口一半、60 分內 → 1+1.0+1.5=3.5", m(0.5, 30, 0), 3.5)
    ck("缺口全滿、60 分內 → 1+2.0+1.5=4.5", m(1.0, 30, 0), 4.5)
    ck("缺口全滿、60 分內、要多走 8 分 → 封頂 5.0", m(1.0, 30, 8), 5.0)
    ck("缺口超過 1 也封在 2.0 → 4.5", m(3.0, 30, 0), 4.5)
    ck("120 分以上急迫權重 0.5", m(0.0, 150, 0), 1.5)
    ck("剛好 120 分算「其他」→ 0.5", m(0.0, 120, 0), 1.5)
    ck("119 分算 <120 → 1.0", m(0.0, 119, 0), 2.0)
    ck("距離未達 8 分不加權", m(0.0, 999, 7), 1.5)
    # 單次點數 = 5 × 倍率，固定四捨五入（不可用內建 round()，那是銀行家捨入）
    pts = v2api._points
    ck("倍率 3.5 → 17.5 → 18 點", pts(m(0.5, 30, 0)), 18)
    ck("倍率 4.5 → 22.5 → 23 點（內建 round 會給 22）", pts(m(1.0, 30, 0)), 23)
    ck("倍率 5.0 → 25 點，剛好是 25 元上限", pts(m(1.0, 30, 8)), 25)
    ck("倍率 1.5 → 7.5 → 8 點", pts(1.5), 8)
    ck("倍率 2.5 → 12.5 → 13 點（半數一律進位）", pts(2.5), 13)


# ====================================================== 2. 離線：站鍵對應
def test_match():
    print("\n[2] 即時站鍵對應")
    import pandas as pd
    import live

    st = pd.DataFrame([
        {"sid": 10, "name": "板橋車站", "lat": 25.0143, "lon": 121.4637},
        {"sid": 11, "name": "瓦磘溝(福真里)", "lat": 24.9900, "lon": 121.5100},
    ])
    ls = live.LiveStore(st)
    ck("站名完全相同 → 用名稱對上",
       ls._match({"sna": "YouBike2.0_板橋車站", "lat": "25.0143", "lng": "121.4637"}),
       (10, "name"))
    ck("前綴 YouBike2.0_ 會被去掉",
       ls._match({"sna": "YouBike2.0_瓦磘溝(福真里)", "lat": "0", "lng": "0"})[0], 11)
    # 歷史 CSV 罕用字寫成 ?：名稱對不上，但座標同一點 → 60 公尺內補配
    ck("名稱不符但座標相同 → 經緯度補配",
       ls._match({"sna": "YouBike2.0_瓦?溝(福真里)", "lat": "24.9900", "lng": "121.5100"}),
       (11, "geo"))
    # 距離超過 60 公尺且名稱對不上 → 視為新站，給穩定的合成負數 sid
    far = {"sna": "YouBike2.0_全新的站", "lat": "25.2000", "lng": "121.7000", "sno": "500999001"}
    first = ls._match(far)
    second = ls._match(far)
    ck("查無對應 → 合成負數 sid", first, (-1, "new"))
    ck("同一個官方站號重複查 → 給同一個合成 sid（穩定）", second, (-1, "new"))
    other = ls._match({"sna": "YouBike2.0_另一個新站", "lat": "25.21", "lng": "121.71", "sno": "500999002"})
    ck("不同站號 → 不同合成 sid", other, (-2, "new"))
    ck("沒有站號又對不上 → 放棄",
       ls._match({"sna": "無名", "lat": "25.3", "lng": "121.8"}), (None, "none"))


# ====================================================== 3. 離線：容量落差與彙總
def test_summary():
    print("\n[3] 容量落差與全市彙總")
    import pandas as pd
    import live

    ls = live.LiveStore(pd.DataFrame([{"sid": 1, "name": "A", "lat": 25.0, "lon": 121.0}]))
    snap = {
        # 容量 20＝可借 8＋可還 12，對得上 → 沒有落差
        1: {"sid": 1, "district": "板橋區", "capacity": 20, "bikes": 8, "docks": 12,
            "bikes_electric": 2, "capacity_gap": 0, "active": True,
            "no_bike": False, "no_dock": False, "both_zero": False, "age_min": 4.0, "match": "name"},
        # 容量 30，可借 0、可還 25 → 落差 5，且無車可借
        2: {"sid": 2, "district": "板橋區", "capacity": 30, "bikes": 0, "docks": 25,
            "bikes_electric": 0, "capacity_gap": 5, "active": True,
            "no_bike": True, "no_dock": False, "both_zero": False, "age_min": 6.0, "match": "name"},
        # 整站無服務：容量 48、借還皆 0 → 落差 48
        3: {"sid": 3, "district": "三芝區", "capacity": 48, "bikes": 0, "docks": 0,
            "bikes_electric": 0, "capacity_gap": 48, "active": True,
            "no_bike": True, "no_dock": True, "both_zero": True, "age_min": 45.0, "match": "name"},
        # 官方標示暫停營運：不計入無車／無位
        4: {"sid": 4, "district": "三芝區", "capacity": 10, "bikes": 0, "docks": 0,
            "bikes_electric": 0, "capacity_gap": 10, "active": False,
            "no_bike": True, "no_dock": True, "both_zero": True, "age_min": 3.0, "match": "new"},
    }
    s = ls._summarize(snap, unmatched=0)
    ck("站數", s["stations"], 4)
    ck("暫停營運 1 站", s["inactive"], 1)
    ck("無車可借只算營運中的（2、3 號站）", s["no_bike"], 2)
    ck("無位可還只算營運中的（3 號站）", s["no_dock"], 1)
    ck("借還同時為 0 且營運中 → 1 站", s["both_zero"], 1)
    ck("容量落差站數（2、3、4 號站）", s["capacity_gap_stations"], 3)
    ck("容量落差車柱總數 5+48+10", s["capacity_gap_units"], 63)
    ck("容量落差比例 3/4", s["capacity_gap_pct"], 75.0)
    ck("在站車輛合計 8+0+0+0", s["bikes_total"], 8)
    ck("電輔車合計", s["bikes_electric"], 2)
    ck("新增站計數（4 號站 match=new）", s["new_stations"], 1)
    ck("資料齡中位數 (4+6)/2 與 (45+3)/2 的中位＝(6+45)/2 的中位", s["data_age_min_median"], 5.0)
    ck("超過 30 分未更新 1 站", s["stale_stations"], 1)
    ck("板橋區彙總無車 1 站", s["by_district"]["板橋區"]["no_bike"], 1)
    ck("三芝區落差車柱 48+10", s["by_district"]["三芝區"]["gap_units"], 58)


# ====================================================== 4. 離線：NaN 防護
def test_nan_guard():
    print("\n[4] NaN 防護（FastAPI 嚴格編碼遇 NaN 會直接 500）")
    import v2api

    src = {"a": float("nan"), "b": [1.0, float("inf"), {"c": float("-inf")}], "d": "x", "e": 3}
    out = v2api._sanitize(src)
    ck("NaN → None", out["a"], None)
    ck("正無限大 → None", out["b"][1], None)
    ck("負無限大 → None", out["b"][2]["c"], None)
    ck("正常數值不動", out["b"][0], 1.0)
    ck("字串不動", out["d"], "x")
    ck("整數不動", out["e"], 3)
    ck_true("清理後可用嚴格 JSON 編碼", json.dumps(out, allow_nan=False) is not None)


# ====================================================== 5. 離線：分析檔口徑
def test_analytics_file():
    print("\n[5] 分析輸出的口徑與自洽")
    p = os.path.join(ROOT, "data", "analytics", "summary.json")
    if not os.path.exists(p):
        print("  跳過：尚未產生 data/analytics/summary.json")
        return
    with open(p, encoding="utf-8") as f:
        a = json.load(f)
    r = a["rates"]
    for key in ("avail_june", "park_june"):
        b = r[key]
        ck(f"{key} 三級加總等於站數", b["high"] + b["mid"] + b["low"], b["n"])
    ck_true("見車率平均在 0–100 之間", 0 <= r["avail_june"]["mean"] <= 100)
    ck_true("見位率平均在 0–100 之間", 0 <= r["park_june"]["mean"] <= 100)
    ck_true("有標注臺北對照的出處", "臺北城市儀表板" in r["taipei_source"])
    ck_true("有標注兩邊演算法不同的警告", "不是同一個演算法" in r["compare_caveat"])
    ck_true("口徑說明寫明是快照比例", "快照" in a["meta"]["caveat"])
    ck_true("已排除停用站", a["meta"]["dead_stations"] > 0)
    ck_true("納入分析的站數少於全市站數", a["meta"]["stations_analyzed"] < a["meta"]["stations_total"])
    cw = a["curves"]["weekday"]
    ck("平日曲線 18 個小時（06–23）", len(cw["no_bike"]), 18)
    ck("平日曲線時間軸也是 18 點", len(cw["hours"]), 18)
    ck("日間定義從 06 時開始", cw["hours"][0], 6)
    ck("日間定義到 23 時結束", cw["hours"][-1], 23)
    # 早晚峰翻轉：早峰非捷運站缺車較嚴重、捷運站缺位較嚴重；晚峰捷運站轉為缺車
    am, pm = a["mrt_compare"]["am"], a["mrt_compare"]["pm"]
    ck_true("早峰：非捷運站無車 > 捷運站無車", am["non_mrt"]["no_bike"] > am["mrt"]["no_bike"])
    ck_true("早峰：捷運站無位 > 非捷運站無位", am["mrt"]["no_dock"] > am["non_mrt"]["no_dock"])
    ck_true("晚峰：捷運站無車 > 非捷運站無車（翻轉）", pm["mrt"]["no_bike"] > pm["non_mrt"]["no_bike"])
    ck_true("稀釋效應清單非空", len(a["dilution"]) > 0)
    for d in a["dilution"][:5]:
        ck_true(f"稀釋案例 {d['name']}：尖峰失效率高於月平均的失效率",
                d["am_no_dock"] > (100 - d["park_rate"]))


# ====================================================== 6. HTTP
def test_http():
    print(f"\n[6] HTTP（{BASE}）")
    try:
        live = get("/api/v2/live")
    except Exception as e:                                   # noqa: BLE001
        print(f"  跳過 HTTP：連不上 {BASE}（{e}）")
        return

    st, status = live["stats"], live["status"]
    ck_true("即時資料可用", status["available"])
    ck_true("有標注資料來源", "新北市" in status["source"])
    ck_true("有標注授權", "政府資料開放授權" in status["license"])
    ck_true("站數合理（1400–1800）", 1400 <= st["stations"] <= 1800, str(st["stations"]))
    ck("未對應站數為 0（全部都有 sid）", st["unmatched"], 0)
    ck_true("容量落差有被算出來", st["capacity_gap_stations"] > 0)
    ck_true("落差站數不超過總站數", st["capacity_gap_stations"] <= st["stations"])
    ck_true("落差語意有寫明不判定根因", "不判定根因" in live["semantics"]["capacity_gap"])
    ck_true("有寫明即時風險不是模型預測", "不是模型預測" in live["semantics"]["risk"])

    stations = get("/api/v2/live/stations")
    ck("站點清單筆數與彙總一致", stations["count"], st["stations"])
    ck_true("每站都有經緯度", all(s["lat"] and s["lon"] for s in stations["stations"]))

    only = get("/api/v2/live/stations?only=no_dock")
    ck_true("篩選無位可還：每一筆 d 都是 0", all(s["d"] == 0 for s in only["stations"]))
    ck_true("篩選無位可還：每一筆都在營運中", all(s["act"] for s in only["stations"]))

    board = get("/api/v2/ops/board")
    t = board["totals"]
    ck_true("看板有分出整站無服務", "整站無服務" in t)
    offline_sids = {c["sid"] for c in board["offline"]}
    cand_sids = {c["sid"] for c in board["candidates"]}
    ck("整站無服務不得混進調度候選", offline_sids & cand_sids, set())
    ck_true("每個整站無服務的站借還都是 0",
            all(c["bikes"] == 0 and c["docks"] == 0 for c in board["offline"]))
    ck_true("候選清單不含借還同時為 0 的站",
            all(not (c["bikes"] == 0 and c["docks"] == 0) for c in board["candidates"]))
    ck_true("候選依優先度遞減排序",
            all(board["candidates"][i]["priority"] >= board["candidates"][i + 1]["priority"]
                for i in range(len(board["candidates"]) - 1)))
    ck_true("看板明講不產生 ETA", "不產生 ETA" in board["semantics"])
    ck_true("候選沒有任何 eta 欄位",
            all("eta" not in c for c in board["candidates"]))

    ins = get("/api/v2/insights")
    ck_true("建議清單非空", ins["count"] > 0)
    ck_true("每條建議都有口徑說明", all(i.get("caveat") for i in ins["insights"]))
    ck_true("每條建議都有處置建議", all(i.get("action") for i in ins["insights"]))

    rb = get("/api/v2/rewards/board")
    ck_true("任務板有規則說明", "倍率" in rb["rules"]["formula"])
    ck_true("有免責：不代表可實際兌換", "不代表可實際兌換" in rb["caveat"])
    ck_true("每個任務的倍率都在 1.0–5.0", all(1.0 <= q["multiplier"] <= 5.0 for q in rb["quests"]))
    ck_true("每個任務都有缺口（不缺的站不加碼）", all(q["deficit"] > 0 for q in rb["quests"]))
    ck_true("任務依倍率遞減排序",
            all(rb["quests"][i]["multiplier"] >= rb["quests"][i + 1]["multiplier"]
                for i in range(len(rb["quests"]) - 1)))

    if rb["quests"]:
        q = rb["quests"][0]
        before = rb["wallet"]["points"]
        rid = "test-" + str(os.getpid())
        code, r1 = post("/api/v2/rewards/claim",
                        {"sid": q["sid"], "kind": q["kind"], "request_id": rid})
        ck("領取成功", code, 200)
        ck_true("點數有增加", r1["points"] > before)
        import v2api as _v2
        ck("增加的點數 = 固定四捨五入的 5×倍率", r1["gained"], _v2._points(r1["multiplier"]))
        code2, r2 = post("/api/v2/rewards/claim",
                         {"sid": q["sid"], "kind": q["kind"], "request_id": rid})
        ck("同一個 request_id 重送 → 冪等", code2, 200)
        ck_true("冪等時有標記", r2.get("idempotent") is True)
        ck("冪等時點數不再增加", r2["points"], r1["points"])

    code3, _ = post("/api/v2/rewards/claim", {"sid": 999999, "kind": "還車到這站"})
    ck("不存在的站 → 404", code3, 404)

    det = get(f"/api/v2/station/{stations['stations'][0]['sid']}")
    ck_true("站點詳情有即時資料", det["live"] is not None)
    ck_true("站點詳情有標注風險口徑", "模型預測" in det["horizon_semantics"])

    ana = get("/api/v2/analytics")
    ck_true("分析端點可取得", "rates" in ana)

    page = urllib.request.urlopen(BASE + "/v2", timeout=30).read().decode()
    ck_true("一站式頁面可取得", "新北 YouBike 智慧調度" in page)
    ck_true("頁面有標注快照口徑", "快照比例" in page)
    ck_true("頁面有標注推播是應用內示意", "背景推播" in page or "應用內" in page)


def main():
    print(f"v2 驗收測試　BASE={BASE}")
    test_multiplier()
    test_match()
    test_summary()
    test_nan_guard()
    test_analytics_file()
    test_http()
    print(f"\n{'=' * 52}\n通過 {PASS}　失敗 {FAIL}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
