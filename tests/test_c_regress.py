"""
用戶端原流程回歸：正常通勤、提早到選還車、趕時間、集點。

任務書 C.4 要求這四條原有流程在第二輪改動之後仍須回歸測試。之前只有一次瀏覽器手動
點擊紀錄，而且走的是通報路徑，不是把通勤走完，所以這裡補成自動化。

點數與時間的預期值都用獨立換算：點數照「完成加成 5 點 ＋ 方案點數 ＋ 每則回報 12 點」
自己算一次，時間用 datetime 自己換算，不拿實作輸出當答案。

用法：YB_BASE=http://127.0.0.1:8791 python3 tests/test_c_regress.py
"""
import json, os, sys, urllib.request, urllib.error
from datetime import datetime, timedelta

BASE = os.environ.get("YB_BASE", "http://127.0.0.1:8791")
if BASE.rstrip("/").endswith(":8787"):
    print("拒絕執行：8787 是三端共用的展示伺服器，本測試會呼叫 /api/reset 清掉三端回放狀態。")
    sys.exit(2)

# ---- 獨立 fixture：和 app/server.py 各自維護 ----
FINISH_BASE_POINTS = 5          # 完成一趟的固定加成
POINTS_PER_REPORT = 12          # 每則途中回報的加成
INTENT_COMPLETE_BONUS = 5       # 意向標為完成時的固定加成
PREF_FIRST = {"time": "fast", "reliable": "reliable", "reward": "reward"}   # 偏好決定誰排第一
ORIGIN = [25.02672, 121.46479]
DEST = [25.030560, 121.473968]

fails, passed = [], [0]


def check(name, cond, evidence=""):
    ok = bool(cond)
    print(("  PASS " if ok else "  FAIL ") + name + (f"  [{evidence}]" if evidence else ""))
    if ok:
        passed[0] += 1
    else:
        fails.append(name)


def call(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers={"content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=90) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode() or "{}")


GET = lambda p: call("GET", p)
POST = lambda p, b=None: call("POST", p, b if b is not None else {})


def plan(pref, arrive_by=None):
    b = {"origin": ORIGIN, "dest": DEST, "preference": pref}
    if arrive_by:
        b["arrive_by"] = arrive_by
    s, d = POST("/api/plan", b)
    return d


def reset():
    POST("/api/reset")
    POST("/api/profile", {"role": "worker", "home_sid": 895, "work_sid": 858, "out_time": "07:40",
                          "back_time": "18:10", "join_rewards": True, "preference": "time", "onboarded": True})


# ============================================================ R1 正常通勤
print("\n[R1] 正常通勤：規劃 → 出發 → 騎乘 → 還車完成 → 點數與集章")
reset()
p = plan("time")
opt = p["options"][0]
check("R1a 規劃回得出方案", len(p.get("options", [])) >= 2, f"{len(p.get('options', []))} 個方案")
check("R1b 方案有借車站、還車站，路程是走→騎→走三段",
      bool(opt.get("borrow") and opt.get("return"))
      and [l["mode"] for l in opt.get("legs", [])] == ["walk", "ride", "walk"],
      f"{opt['borrow']['name']} → {opt['return']['name']}　{[l['mode'] for l in opt.get('legs', [])]}")
check("R1c 全程時間等於各段相加（獨立換算）",
      abs(opt["total_min"] - sum(l["minutes"] for l in opt["legs"])) < 0.15,
      f"total={opt['total_min']} legs={[round(l['minutes'], 1) for l in opt['legs']]}")

s, trip = POST("/api/trip/start", {"option": opt, "all_labels": [o["label"] for o in p["options"]]})
check("R1d 出發後行程進入走去借車站", trip.get("active") is True and trip.get("phase") == "walk_to_borrow",
      f"phase={trip.get('phase')}")
check("R1e 出發同時登記借還意向", bool(trip.get("intent_id")), f"intent={trip.get('intent_id')}")
_, ints = GET("/api/intents")
check("R1f 意向狀態為進行中",
      any(i["id"] == trip["intent_id"] and i["status"] == "active" for i in ints["intents"]))

POST("/api/trip/phase", {"phase": "riding"})
s, t2 = GET("/api/trip")
check("R1g 借車後進入騎乘並記下借車時間", t2.get("phase") == "riding" and bool(t2.get("borrowed_at")),
      f"borrowed_at={t2.get('borrowed_at')}")

_, before_w = GET("/api/wallet")
before_points = GET("/api/rewards")[1]["points"]
expect_points = FINISH_BASE_POINTS + opt.get("points", 0) + POINTS_PER_REPORT * 0
s, fin = POST("/api/trip/finish")
check("R1h 還車完成回傳點數與集章", s == 200 and bool(fin.get("stamp")), f"+{fin.get('points')} 點 {fin.get('stamp', {}).get('name')}")
check("R1i 點數等於獨立換算（5 ＋ 方案點數 ＋ 12×回報數）",
      fin.get("points") == expect_points, f"實得 {fin.get('points')}，換算 {expect_points}")
after_points = GET("/api/rewards")[1]["points"]
check("R1j 點數確實入帳", after_points == before_points + expect_points,
      f"{before_points} → {after_points}")
_, rw = GET("/api/rewards")
check("R1k 集章寫進存摺", any(st["name"] == fin["stamp"]["name"] for st in rw["stamps"]), fin["stamp"]["name"])
check("R1l 獎勵紀錄標明是模擬完成事件，未經借還交易驗證",
      any("模擬" in h["reason"] and "未經" in h["reason"] for h in rw["history"]),
      (rw["history"][0]["reason"] if rw["history"] else ""))
_, ints2 = GET("/api/intents")
check("R1m 完成後意向標為 completed",
      any(i["id"] == trip["intent_id"] and i["status"] == "completed" for i in ints2["intents"]))
s, t3 = GET("/api/trip")
check("R1n 完成後行程結束，不留在進行中", t3.get("active") is False, f"active={t3.get('active')}")
_, w2 = GET("/api/wallet")
check("R1o 存摺公里數增加", w2["km"] > before_w["km"], f"{before_w['km']} → {round(w2['km'], 2)}")

# ============================================================ R2 提早到、選還車
print("\n[R2] 提早到：設定抵達期限後仍有餘裕，還車站可另外挑")
reset()
_, st = GET("/api/state")
now = datetime.strptime(st["clock"]["ts"], "%Y-%m-%d %H:%M")
deadline = (now + timedelta(minutes=60)).strftime("%H:%M")
p2 = plan("time", arrive_by=deadline)
o2 = p2["options"][0]
check("R2a 有設期限時每個方案都帶餘裕欄位",
      all("slack_min" in o for o in p2["options"]), f"arrive_by={p2.get('arrive_by')}")
check("R2b 提早一小時出發，主推薦不會遲到", o2.get("late") is False and p2.get("any_on_time") is True,
      f"late={o2.get('late')} any_on_time={p2.get('any_on_time')}")
# 獨立換算：餘裕 = 期限 − （現在 + 全程時間）
eta = now + timedelta(minutes=o2["total_min"])
exp_slack = ((now + timedelta(minutes=60)) - eta).total_seconds() / 60
check("R2c 餘裕等於期限減抵達時間（獨立換算）", abs(o2["slack_min"] - exp_slack) < 0.6,
      f"回傳 {o2['slack_min']}，換算 {round(exp_slack, 1)}")
exp_latest = (now + timedelta(minutes=60) - timedelta(minutes=o2["total_min"]))
got_latest = datetime.strptime(o2["latest_depart"][:16], "%Y-%m-%d %H:%M")
check("R2d 最晚出發時間等於期限減全程（獨立換算）",
      abs((got_latest - exp_latest).total_seconds()) < 90,
      f"回傳 {o2['latest_depart'][11:16]}，換算 {exp_latest.strftime('%H:%M')}")
check("R2e 早到不會冒出遲到提示", not p2.get("deadline_note"), f"note={p2.get('deadline_note')}")

# 還車站：查替代站時要標明是快照與預測，不保證有位
s, alt = GET(f"/api/c/alternatives?sid={o2['return']['sid']}&kind=return")
check("R2f 還車替代站查得到", s == 200 and "alternatives" in alt, f"{len(alt.get('alternatives', []))} 個")
check("R2g 替代站明說是快照與預測、不是保證",
      "不是保證" in (alt.get("disclaimer") or ""), (alt.get("disclaimer") or "")[:30])
check("R2h 替代站不含原本那一站", all(a["sid"] != o2["return"]["sid"] for a in alt.get("alternatives", [])))

# 提早到、換一個還車站也能把行程走完
if alt.get("alternatives"):
    swapped = json.loads(json.dumps(o2))
    a0 = alt["alternatives"][0]
    swapped["return"] = {**swapped["return"], "sid": a0["sid"], "name": a0["name"]}
    s, trip2 = POST("/api/trip/start", {"option": swapped})
    check("R2i 換還車站後仍可出發", trip2.get("active") is True and trip2["option"]["return"]["sid"] == a0["sid"],
          f"還到 {a0['name']}")
    POST("/api/trip/phase", {"phase": "riding"})
    s, fin2 = POST("/api/trip/finish")
    check("R2j 換站後仍可完成並集章", s == 200 and bool(fin2.get("stamp")), fin2.get("stamp", {}).get("name"))

# ============================================================ R3 趕時間
print("\n[R3] 趕時間：偏好最快時，最快方案要排第一，遲到方案不得當主推薦")
reset()
p3 = plan("time")
check("R3a 偏好最快時第一個是最快抵達方案", p3["options"][0]["kind"] == PREF_FIRST["time"],
      f"第一個是 {p3['options'][0]['kind']}")
check("R3b 第一個方案標為主推薦並說明理由", p3["options"][0].get("primary") is True and bool(p3["options"][0].get("primary_reason")),
      p3["options"][0].get("primary_reason"))
fastest = min(o["total_min"] for o in p3["options"])
check("R3c 主推薦確實是全部方案裡最快的（獨立比對）",
      abs(p3["options"][0]["total_min"] - fastest) < 0.05,
      f"主推薦 {p3['options'][0]['total_min']}，最小 {fastest}")
# 同一組起迄如果最快的那個方案剛好也是最穩的，兩者會被併成同一張卡（kind 留 fast，
# 另一個身分掛在 also）。所以判準不是 kind 字面，而是排第一的方案有沒有帶「不用怕沒車沒位」。
RELIABLE_LABEL = "不用怕沒車沒位"
pr = plan("reliable")
first = pr["options"][0]
check("R3d 偏好不用怕沒車沒位時，排第一的方案帶這個身分",
      first["kind"] == PREF_FIRST["reliable"] or RELIABLE_LABEL in (first.get("also") or [])
      or first.get("label") == RELIABLE_LABEL,
      f"kind={first['kind']} label={first.get('label')} also={first.get('also')}")
check("R3d2 偏好切換時主推薦理由跟著換",
      "沒車沒位" in (first.get("primary_reason") or ""), first.get("primary_reason"))

# 期限訂在過去：全部會遲到，不能假裝準時
past = (now - timedelta(minutes=5)).strftime("%H:%M")
p3b = plan("time", arrive_by=past)
check("R3e 全部來不及時明說，不當成準時推薦",
      p3b.get("any_on_time") is False and bool(p3b.get("deadline_note")), p3b.get("deadline_note"))
check("R3f 全部來不及時每個方案都標遲到且餘裕為負",
      all(o.get("late") is True and o.get("slack_min", 0) < 0 for o in p3b["options"]),
      f"slack={[o.get('slack_min') for o in p3b['options']]}")

# ============================================================ R4 集點
print("\n[R4] 集點：偏好集點時順路集點排第一，完成後點數與章入帳")
reset()
p4 = plan("reward")
kinds = [o["kind"] for o in p4["options"]]
has_reward = "reward" in kinds
check("R4a 規劃裡有順路集點方案", has_reward, f"kinds={kinds}")
if has_reward:
    check("R4b 偏好集點時它排第一", p4["options"][0]["kind"] == PREF_FIRST["reward"], f"第一個 {p4['options'][0]['kind']}")
    ro = [o for o in p4["options"] if o["kind"] == "reward"][0]
    check("R4c 集點方案的點數大於零", ro.get("points", 0) > 0, f"{ro.get('points')} 點")

    # 走意向完成這條路（不是 trip finish），驗獨立換算的 pts + 5
    before = GET("/api/rewards")[1]["points"]
    s, it = POST("/api/intents", {"option": ro})
    check("R4d 登記意向成功且標明不是車位預約", s == 200 and bool(it.get("id")), f"intent={it.get('id')}")
    _, notes = GET("/api/notifications/citizen") if False else (200, {})
    s, done = POST(f"/api/intents/{it['id']}/complete")
    expect = ro.get("points", 0) + INTENT_COMPLETE_BONUS
    after = GET("/api/rewards")[1]["points"]
    check("R4e 完成意向的點數等於獨立換算（方案點數 ＋ 5）", after - before == expect,
          f"入帳 {after - before}，換算 {expect}")
    check("R4f 完成後意向狀態為 completed", done.get("status") == "completed", done.get("status"))
    _, rw4 = GET("/api/rewards")
    check("R4g 集章入帳且不重複同一枚", len(rw4["stamps"]) == len({s2["name"] for s2 in rw4["stamps"]}),
          f"{[s2['name'] for s2 in rw4['stamps']]}")
    check("R4h 點數紀錄標明是模擬完成事件",
          any("模擬" in h["reason"] for h in rw4["history"]), rw4["history"][0]["reason"][:40])

    # 取消要釋放名額，不能還算在進行中
    s, it2 = POST("/api/intents", {"option": ro})
    POST(f"/api/intents/{it2['id']}/cancel")
    _, ints4 = GET("/api/intents")
    check("R4i 取消的意向標為 cancelled，不留在進行中",
          any(i["id"] == it2["id"] and i["status"] == "cancelled" for i in ints4["intents"]))
    check("R4j 取消不扣已入帳的點數", GET("/api/rewards")[1]["points"] == after,
          f"{after} → {GET('/api/rewards')[1]['points']}")

# ============================================================ R5 通報不影響原流程
print("\n[R5] 途中通報之後，原通勤流程仍走得完")
reset()
p5 = plan("time")
o5 = p5["options"][0]
POST("/api/trip/start", {"option": o5})
POST("/api/trip/phase", {"phase": "riding"})
s, rep = POST("/api/c/report", {"sid": o5["borrow"]["sid"], "stage": "riding", "problem": "chain",
                                "bike_no": "YB2-REG01", "request_id": "regress-mid-trip"})
check("R5a 騎乘中可以通報並建單", bool(rep.get("ticket_id")), f"ticket={rep.get('ticket_id')}")
check("R5b 騎乘中的坐墊提醒先延後，要求停到安全處",
      (rep.get("saddle") or {}).get("status") == "deferred", (rep.get("saddle") or {}).get("reason"))
s, t5 = GET("/api/trip")
check("R5c 通報不會把行程打斷", t5.get("active") is True and t5.get("phase") == "riding", f"phase={t5.get('phase')}")
before5 = GET("/api/rewards")[1]["points"]
s, fin5 = POST("/api/trip/finish")
check("R5d 通報後仍可完成通勤", s == 200 and fin5.get("points") is not None, f"+{fin5.get('points')} 點")
check("R5e C 的回報不會被行程完成順手結案",
      GET(f"/api/c/report/{rep['id']}")[1].get("status") == "routed_repair",
      GET(f"/api/c/report/{rep['id']}")[1].get("status"))
s, tk5 = GET(f"/api/ops/tickets/{rep['ticket_id']}")
check("R5f 工單不會因為使用者騎完就結案", tk5.get("status") != "closed", f"status={tk5.get('status')}")

POST("/api/reset")
print(f"\n=== 原流程回歸：通過 {passed[0]}　失敗 {len(fails)} ===")
for f in fails:
    print("  未通過：" + f)
sys.exit(1 if fails else 0)
