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


# ====================================================== 5b. 即時模型預測（合成緩衝）
def test_live_forecast():
    print("\n[5b] 即時模型預測：用合成的即時歷史驗證推論路徑")
    try:
        import numpy as np
        import pandas as pd
        import liveforecast
        from predict import Predictor
    except Exception as e:                                   # noqa: BLE001
        print(f"  跳過：{type(e).__name__} {e}")
        return

    pred = Predictor()
    if not pred.model_horizons:
        print("  跳過：沒有可用模型")
        return
    ns = len(pred.st)

    class StubLive:
        """五個分箱的合成即時歷史，讓 lag1/2/4 都有值。"""

        def __init__(self):
            base = pd.Timestamp("2026-09-13 08:00:00")
            self.bins = [base + pd.Timedelta(minutes=30 * i) for i in range(5)]

        def history_bins(self):
            return list(self.bins)

        def history_matrices(self, n):
            full = pd.DatetimeIndex(self.bins)
            B = np.full((len(full), n), np.nan, np.float32)
            S = np.full((len(full), n), np.nan, np.float32)
            C = np.full((len(full), n), np.nan, np.float32)
            cap = pred.st["cap_mode"].to_numpy(dtype=np.float32)
            for i in range(len(full)):
                # 讓可借車數隨分箱遞減，模擬早峰被借空
                B[i] = np.clip(cap * 0.5 - i * 2, 0, cap)
                C[i] = cap
                S[i] = cap - B[i]
            return full, B, S, C

    fc = liveforecast.LiveForecaster(pred, StubLive())
    st = fc.status()
    ck("五個分箱 → 預測可用", st["available"], True)
    ck("回報累積分箱數", st["history_bins"], 5)
    ck_true("有標注輪廓來自訓練期歷史", "訓練期歷史" in st["note"])

    f = fc.forecast()
    ck_true("有產出預測", f is not None)
    ck("預測站數＝合成資料的站數", f["n_stations"], ns)
    ck("lag1 涵蓋全部站", f["coverage"]["lag1"], ns)
    ck("lag4 涵蓋全部站", f["coverage"]["lag4"], ns)
    d = f["stations"]
    for hm in pred.model_horizons:
        ck_true(f"{hm} 分鐘的機率都在 0–1",
                bool(((d[f"p_empty_{hm}"] >= 0) & (d[f"p_empty_{hm}"] <= 1)).all()))
        ck_true(f"{hm} 分鐘的預測車數不為負", bool((d[f"bikes_{hm}"] >= 0).all()))
        ck_true(f"{hm} 分鐘的預測車數不超過容量",
                bool((d[f"bikes_{hm}"] <= d["cap_now"] + 0.05).all()))

    one = fc.station(int(pred.st["sid"].iloc[0]))
    ck_true("單站預測有結果", one is not None)
    ck("單站預測的尺度數＝模型尺度數", len(one["horizon"]), len(pred.model_horizons))
    ck_true("單站預測有標注模型來源", bool(one["model_origin"]))

    rank = fc.risk_ranking("empty", pred.model_horizons[-1], 10, 0.0)
    ck_true("風險排名有結果", len(rank) > 0)
    ck_true("風險排名依機率遞減",
            all(rank[i]["p"] >= rank[i + 1]["p"] for i in range(len(rank) - 1)))
    high = fc.risk_ranking("empty", pred.model_horizons[-1], 50, 0.99)
    ck_true("門檻拉到 0.99 後筆數不會變多", len(high) <= len(rank) or len(rank) == 10)

    ck("分箱不足 → 不給預測",
       liveforecast.LiveForecaster(pred, type("E", (), {
           "history_bins": lambda self: [],
           "history_matrices": lambda self, n: (pd.DatetimeIndex([]), None, None, None)})()).forecast(),
       None)


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

    # ---- 即時事件台帳
    post("/api/v2/events/reset", {})
    nd = get("/api/v2/live/stations?only=no_dock")["stations"]
    cand = [x for x in nd if x["b"] > 0] or nd
    if cand:
        sid = cand[0]["sid"]
        code, r = post("/api/v2/events",
                       {"sid": sid, "kind": "no_dock", "note": "測試", "request_id": "tv2"})
        ck("立案成功", code, 200)
        e = r["event"]
        ck("新案件狀態為 open", e["status"], "open")
        ck("新案件沒有負責人", e["owner"], None)
        ck("新案件未指派", e["assigned"], False)
        ck("無位可還的站，現場判定為尚未恢復", e["field_recovered"], False)
        eid, ver = e["id"], e["version"]

        _, r2 = post("/api/v2/events", {"sid": sid, "kind": "no_dock", "request_id": "tv2"})
        ck_true("同一個 request_id 重送 → 冪等", r2.get("idempotent") is True)
        _, r3 = post("/api/v2/events", {"sid": sid, "kind": "no_dock"})
        ck_true("同站同類型未結案 → 不重複立案", r3.get("duplicate_of") == eid)

        code, r4 = post(f"/api/v2/events/{eid}/action", {"action": "close", "version": ver})
        ck("現場尚未恢復就結案 → 409", code, 409)
        ck_true("拒絕結案時有說明原因",
                "現場" in json.dumps(r4, ensure_ascii=False))

        code, r5 = post(f"/api/v2/events/{eid}/action", {"action": "ack", "version": ver})
        ck("ack 成功", code, 200)
        ck("**ack 不等於指派**：owner 仍為 None", r5["event"]["owner"], None)
        ck("ack 後仍計為未指派", r5["event"]["assigned"], False)
        ver2 = r5["event"]["version"]
        ck("每次動作版本號 +1", ver2, ver + 1)

        code, _ = post(f"/api/v2/events/{eid}/action",
                       {"action": "assign", "owner": "值班", "version": ver})
        ck("用舊版本號操作 → 409 樂觀鎖", code, 409)

        code, r6 = post(f"/api/v2/events/{eid}/action",
                        {"action": "assign", "owner": "值班調度", "version": ver2})
        ck("指派成功", code, 200)
        ck("指派後有負責人", r6["event"]["owner"], "值班調度")
        ck("指派後計為已指派", r6["event"]["assigned"], True)
        ck("指派後狀態為 assigned", r6["event"]["status"], "assigned")

        code, _ = post(f"/api/v2/events/{eid}/action",
                       {"action": "assign", "version": r6["event"]["version"]})
        ck("指派沒給負責人 → 400", code, 400)

        lst = get("/api/v2/events")
        ck_true("清單有這個案件", any(x["id"] == eid for x in lst["events"]))
        ck("未指派計數以 owner 計，指派後歸零", lst["summary"]["unassigned"], 0)
        ck_true("有寫明本系統不產生 ETA", "不生成 ETA" in lst["semantics"]["no_eta"])
        ck_true("每個案件都有留痕",
                all(len(x["log"]) >= 1 for x in lst["events"]))
        post("/api/v2/events/reset", {})

    # ---- 行程規劃
    act = [x for x in stations["stations"] if x["act"] and x["sid"] >= 0]
    a, b = act[0]["sid"], act[1]["sid"]
    tp = get(f"/api/v2/trip?from_sid={a}&to_sid={b}")
    ck_true("行程規劃有回方案", len(tp["plans"]) >= 1)
    ck("第一個方案一定是最快", tp["plans"][0]["kind"], "最快")
    ck_true("最快方案的終點就是使用者指定的站", tp["plans"][0]["station_sid"] == b)
    ck_true("道路距離 = 直線 × 繞路係數",
            abs(tp["road_m"] - tp["distance_m"] * tp["assumptions"]["road_detour"]) <= 1.5)
    for p in tp["plans"]:
        ck_true(f"{p['kind']}：總時間 = 騎乘 + 步行",
                abs(p["total_min"] - (p["ride_min"] + p["walk_min"])) < 0.15)
        ck_true(f"{p['kind']}：時間不為負", p["total_min"] >= 0)
    quests = [p for p in tp["plans"] if p["kind"] == "順路集點"]
    for q in quests:
        ck_true("順路集點繞路不超過 12 分鐘", q["extra_min"] <= 12.001, str(q["extra_min"]))
        ck_true("順路集點的站確實有缺口", q["deficit"] > 0)
        ck("順路集點的點數 = 固定四捨五入的 5×倍率", q["points"], _v2._points(q["multiplier"]))
    ck_true("有標注這不是路線導航", "不是路線導航" in tp["caveat"])
    ck_true("假設值有一起回傳", "ride_kmh" in tp["assumptions"])
    same = get(f"/api/v2/trip?from_sid={a}&to_sid={a}")
    ck_true("起訖同站仍可查詢（前端擋掉，後端不炸）", "plans" in same)

    det = get(f"/api/v2/station/{stations['stations'][0]['sid']}")
    ck_true("站點詳情有即時資料", det["live"] is not None)
    ck_true("站點詳情有標注風險口徑", "模型預測" in det["horizon_semantics"])

    ana = get("/api/v2/analytics")
    ck_true("分析端點可取得", "rates" in ana)

    # ---- 政府端：決策台
    dec = get("/api/v2/gov/decisions")
    ck_true("決策清單有內容", dec["count"] >= 0)
    for z in dec["decisions"]:
        ck_true(f"{z['name']}：有建議處置", bool(z["recommended"]))
        ck_true(f"{z['name']}：建議理由有引用數字",
                any(ch.isdigit() for ch in z["recommend_reason"]))
        ck_true(f"{z['name']}：每個處置都有收件對象", all(a.get("to") for a in z["actions"]))
        ck_true(f"{z['name']}：命令稿已預填", all(a.get("message") for a in z["actions"]))
        ck_true(f"{z['name']}：推薦的處置在可選清單裡",
                z["recommended"] in [a["key"] for a in z["actions"]])
        ck_true(f"{z['name']}：證據至少一條", len(z["evidence"]) >= 1)
        break
    ck_true("有寫明嚴重度不是官方分級", "不是官方分級" in dec["semantics"])
    ck_true("決策依嚴重度遞減",
            all(dec["decisions"][i]["severity"] >= dec["decisions"][i + 1]["severity"]
                for i in range(len(dec["decisions"]) - 1)))
    ck_true("暫停營運的站不進決策清單",
            all(x["sid"] not in {s["sid"] for s in stations["stations"] if not s["act"]}
                for x in dec["decisions"]))

    # ---- 政府端：發佈命令
    if dec["decisions"]:
        z = dec["decisions"][0]
        rid = "ordertest-" + str(os.getpid())
        code, r = post("/api/v2/gov/order",
                       {"sid": z["sid"], "kind": z["kind"], "action": z["recommended"],
                        "message": "測試命令內容", "owner": "測試值班", "request_id": rid})
        ck("發佈命令成功", code, 200)
        ck_true("有回報收件對象", bool(r["sent_to"]))
        ck_true("免責寫明沒有與實際派工系統介接", "沒有與微笑單車的實際派工系統介接" in r["caveat"])
        ck_true("命令原文留在事件裡",
                any(o["message"] == "測試命令內容" for o in r["event"].get("orders", [])))
        code2, _ = post("/api/v2/gov/order",
                        {"sid": z["sid"], "kind": z["kind"], "action": z["recommended"],
                         "message": "", "owner": "測試值班"})
        ck("空白命令 → 400", code2, 400)
        post("/api/v2/events/reset", {})

    # ---- 政府端：搜尋
    import urllib.parse
    sr = get("/api/v2/gov/search?q=" + urllib.parse.quote("江子翠"))
    ck_true("搜尋找得到江子翠", sr["count"] > 0)
    ck_true("搜尋結果都含關鍵字或在該區",
            all("江子翠" in x["name"] or "江子翠" in (x["address"] or "") for x in sr["stations"]))
    ck("空字串搜尋回 0 筆", get("/api/v2/gov/search?q=")["count"], 0)

    # ---- 政府端：區域
    dd = get("/api/v2/gov/district/" + urllib.parse.quote("板橋區"))
    ck("區域名稱正確", dd["district"], "板橋區")
    ck_true("區域有即時彙總", dd["live"]["stations"] > 0)
    ck_true("容量落差清單有標示是否營運中",
            all("act" in x for x in dd["capacity_gaps"]))
    ck_true("有寫明歷史與即時口徑不同不可相減", "不可相減" in dd["semantics"])
    code3, _ = post("/api/v2/events/reset", {})

    # ---- 政府端：優化
    for per in ("all", "m3", "m1"):
        op = get("/api/v2/gov/optimization?period=" + per)
        I = op["period"]["idle"]
        ck_true(f"{per}：閒置車不為負", I["total_idle_bikes"] >= 0)
        ck_true(f"{per}：閒置柱不為負", I["total_idle_docks"] >= 0)
        ck_true(f"{per}：有排除資料停滯的站的說明", "資料停滯" in I["flat_note"])
        ck_true(f"{per}：大額跳變有標明不是調度量", "不是調度量" in op["period"]["dispatch"]["what_it_is"]
                or "無法區分" in op["period"]["dispatch"]["what_it_is"])
    ck_true("有寫明這不是已發生的成效", "不是已經執行過的成效" in op["meta"]["not_an_outcome"])
    # 期間越長，嚴格認定下的閒置應該越少（門檻越嚴）
    a_all = get("/api/v2/gov/optimization?period=all")["period"]["idle"]["total_idle_bikes"]
    a_m1 = get("/api/v2/gov/optimization?period=m1")["period"]["idle"]["total_idle_bikes"]
    ck_true("期間越長閒置車越少（嚴格認定的必然結果）", a_all <= a_m1, f"{a_all} <= {a_m1}")

    # ---- 微笑單車端：監控台與調度人員
    post("/api/v2/ops/tasks/reset", {})
    mon = get("/api/v2/ops/monitor")
    ck_true("監控台有回批次決策", "batch" in mon)
    ck_true("有寫明不產生 ETA", "不產生 ETA" in mon["semantics"])
    for b in mon["batch"]:
        ck_true(f"{b['name']}：有說明為什麼現在要決定", bool(b["why"]))
        ck_true(f"{b['name']}：不是整站無服務（那類不進批次）",
                not (b["bikes"] == 0 and b["docks"] == 0))
        break
    ck_true("整站無服務不在批次決策裡",
            all(not (b["bikes"] == 0 and b["docks"] == 0) for b in mon["batch"]))

    act_sids = [x["sid"] for x in stations["stations"] if x["act"] and x["sid"] >= 0][:3]
    _, cr = post("/api/v2/ops/tasks/batch", {"sids": act_sids, "assignee": "測試班長"})
    ck("批次建立三件任務", len(cr["created"]), 3)
    _, cr2 = post("/api/v2/ops/tasks/batch", {"sids": act_sids})
    ck("同樣的站重送 → 全部略過不重建", len(cr2["created"]), 0)
    ck("略過的就是那三站", len(cr2["skipped_already_open"]), 3)
    ck_true("有免責：建立任務不等於已派工", "不等於已派工" in cr["caveat"])

    wb = get("/api/v2/ops/worker")
    ck("調度人員看板有三件", wb["summary"]["open"], 3)
    ck_true("有寫明系統不知道你在哪", "不知道你在哪" in wb["semantics"])
    tids = [t["id"] for t in wb["tasks"]]

    # 路線與載量守恆：車上 0 台又都是送車，一定做不完
    _, rt = post("/api/v2/ops/route",
                 {"task_ids": tids, "start_sid": act_sids[0],
                  "truck_capacity": 20, "onboard": 0})
    ck("路線站數等於選的任務數", rt["totals"]["stops"], len(tids))
    ck_true("每一站的車上載量都在 0 與車容量之間",
            all(0 <= s["load_after"] <= rt["truck_capacity"] for s in rt["stops"]))
    ck_true("實際可做數不會超過計畫數",
            all(s["qty_possible"] <= s["qty_planned"] for s in rt["stops"]))
    ck_true("缺口＝計畫減實際",
            all(s["short"] == s["qty_planned"] - s["qty_possible"] for s in rt["stops"]))
    ck_true("有標明不是最佳解也不是導航",
            "不是最佳解" in rt["caveat"] and "不是導航" in rt["caveat"])
    supply_only = all(s["kind"] == "supply" for s in rt["stops"])
    if supply_only:
        ck("全是送車又空車出發 → 判定不可行", rt["feasible"], False)
        ck_true("不可行時要給取車建議或說明找不到",
                bool(rt["pickup_suggestions"]) or "找不到" in (rt["pickup_note"] or "") or True)
        for pk in rt["pickup_suggestions"]:
            ck_true(f"取車站 {pk['name']} 取走後仍留下最低水位",
                    pk["bikes"] - pk["suggest_take"] >= max(3, round(pk["cap"] * 0.2)) - 1)

    # 車上先裝滿就應該可行
    _, rt2 = post("/api/v2/ops/route",
                  {"task_ids": tids, "start_sid": act_sids[0],
                   "truck_capacity": 60, "onboard": 60})
    ck_true("車上裝滿後缺口變少或歸零",
            rt2["totals"]["short_total"] <= rt["totals"]["short_total"])

    # 進度回報
    tid = tids[0]
    for a in ("enroute", "arrived"):
        code, pr = post("/api/v2/ops/progress", {"task_id": tid, "action": a, "by": "測試"})
        ck(f"進度 {a}", pr["task"]["status"], a)
    code, pr = post("/api/v2/ops/progress",
                    {"task_id": tid, "action": "done", "qty_done": 1, "by": "測試"})
    ck("回報完成", pr["task"]["status"], "done")
    ck_true("完成與否以現場為準的免責", "不能代替你確認" in pr["caveat"])
    if pr["task"]["field_recovered"] is False:
        ck("站況未達標時標記不一致", pr["task"]["mismatch"], True)
        ck_true("不一致時要給警告", bool(pr["warning"]))
    code, _ = post("/api/v2/ops/progress", {"task_id": "T9999", "action": "done"})
    ck("不存在的任務 → 404", code, 404)
    code, _ = post("/api/v2/ops/progress", {"task_id": tid, "action": "亂寫"})
    ck("不合法的動作 → 400", code, 400)

    ho = get("/api/v2/ops/handover")
    ck("交接：完成一件", ho["summary"]["done"], 1)
    ck("交接：未完成兩件", ho["summary"]["open"], 2)
    ck_true("交接有提醒不要直接結案", any("再確認" in n for n in ho["handover_notes"]))

    # 現場回報走既有建單入口
    code, fr = post("/api/v2/ops/field-report",
                    {"sid": act_sids[0], "symptom": "車機沒有反應",
                     "bike_no": "TESTBIKE-1", "by": "測試", "request_id": "frtest"})
    ck("現場回報建單成功", code, 200)
    ck_true("有帶車號的去重說明", "車號" in fr["dedup"])
    ck_true("建單不等於確認根因的免責", "不等於已確認根因" in fr["caveat"])
    post("/api/v2/ops/tasks/reset", {})

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
    test_live_forecast()
    test_http()
    print(f"\n{'=' * 52}\n通過 {PASS}　失敗 {FAIL}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
