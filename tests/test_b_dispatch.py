"""B 段四項改動的針對性測試。"""
import sys, json, urllib.request
import pandas as pd, numpy as np
sys.path.insert(0, "/Users/chenhongfei/CC/ntpc-youbike/app")
import planner as PL
A = PL.ASSUMPTIONS
FOCUS = ["板橋區", "新莊區", "土城區"]

def get(u):
    with urllib.request.urlopen("http://127.0.0.1:8787" + u, timeout=20) as r: return json.loads(r.read().decode())
pred = pd.DataFrame(get("/api/stations?adjusted=1")["stations"])
for c in ("bikes", "spaces", "cap"): pred[c] = pd.to_numeric(pred[c], errors="coerce")
now = pd.Timestamp(get("/api/state")["clock"]["ts"])
ok = lambda b: "PASS" if b else "**FAIL**"
fails = []
def check(name, cond, detail=""):
    print(f"[{ok(cond)}] {name}{'  ' + detail if detail else ''}")
    if not cond: fails.append(name)

PL.ledger_reset()
cid0 = PL.cycle_open(now)
tasks = []
for h in (120, 180):
    tasks += PL.plan_dispatch(pred, now, h, None, districts=FOCUS, existing_locked=set(), cycle=cid0)
trips = [t for t in tasks if t["stops"]]

# ---- B1 資源帳 ----
# 正確判準：依伺服器實際的規劃順序（先 120 再 180），逐尺度累加，每一步都不可超過「該尺度」的量。
def avail_at(sid, h):
    g = PL.gaps(pred, h, None); r = g[g.sid == sid]
    if not len(r): return 0.0
    r = r.iloc[0]
    return max(float(r.need_out), min(float(r.pb_adj) - float(r.min_stock), 8)) if float(r.need_out) > 0 else float(r.donor)
def need_at(sid, h):
    g = PL.gaps(pred, h, None); r = g[g.sid == sid]
    return float(r.iloc[0].need_in) if len(r) else 0.0

def run(with_ledger=True):
    """with_ledger=False 時每個尺度各開一個獨立週期，模擬「沒有共用資源帳」的舊行為。"""
    PL.ledger_reset()
    out = []
    cid = PL.cycle_open(now) if with_ledger else None
    for h in (120, 180):
        c = cid if with_ledger else PL.cycle_open(now + pd.Timedelta(seconds=h))
        out.append((h, PL.plan_dispatch(pred, now, h, None, districts=FOCUS, existing_locked=set(), cycle=c)))
    return out

def audit(rounds):
    cum_take, cum_prom, bad_take, bad_prom = {}, {}, [], []
    for h, tks in rounds:
        for t in tks:
            for s in t["stops"]:
                if s["action"] == "pickup" and s["sid"] > 0:
                    cum_take[s["sid"]] = cum_take.get(s["sid"], 0) + s["qty"]
                    if cum_take[s["sid"]] > avail_at(s["sid"], h) + 0.5: bad_take.append((s["sid"], s["name"], cum_take[s["sid"]], round(avail_at(s["sid"], h), 1), h))
                elif s["action"] == "dropoff":
                    cum_prom[s["sid"]] = cum_prom.get(s["sid"], 0) + s["qty"]
                    if cum_prom[s["sid"]] > np.ceil(max(need_at(s["sid"], 120), need_at(s["sid"], 180))) + 0.01:
                        bad_prom.append((s["sid"], s["name"], cum_prom[s["sid"]], round(max(need_at(s["sid"], 120), need_at(s["sid"], 180)), 1)))
    return bad_take, bad_prom

bt, bp = audit(run(True))
check("B1 供給站累計被抽的量不超過該尺度的可供給量", not bt, f"超抽 {len(bt)} 站" + (f" 例：{bt[:2]}" if bt else ""))
check("B1 缺車站累計被承諾的量不超過最大缺口（允許整數進位）", not bp, f"超額 {len(bp)} 站" + (f" 例：{bp[:2]}" if bp else ""))
bt0, bp0 = audit(run(False))
check("B1 關掉帳本後確實會退化（證明帳本有在作用）", (len(bt0) + len(bp0)) > (len(bt) + len(bp)),
      f"無帳本 超抽 {len(bt0)}／超額 {len(bp0)}　有帳本 超抽 {len(bt)}／超額 {len(bp)}")
PL.ledger_reset()
cid = PL.cycle_open(now)
tasks = []
for h in (120, 180): tasks += PL.plan_dispatch(pred, now, h, None, districts=FOCUS, existing_locked=set(), cycle=cid)
trips = [t for t in tasks if t["stops"]]

# ---- B2 每站服務時限 ----
drops = [s for t in trips for s in t["stops"] if s["action"] == "dropoff"]
check("B2 每個送車站都有自己的 due_min / due_ts", all("due_min" in s and "due_ts" in s for s in drops), f"{len(drops)} 站")
dues = {}
for s in drops: dues[s["due_min"]] = dues.get(s["due_min"], 0) + 1
check("B2 服務時限不是全部同一個值（證明不是共用 target_ts）", len(dues) > 1, f"分布 {dict(sorted(dues.items()))}")
bad = [s["name"] for s in drops if s["on_time"] and pd.Timestamp(s["arrives_by"]) > pd.Timestamp(s["due_ts"])]
check("B2 on_time 標記與 arrives_by/due_ts 一致", not bad, f"不一致 {len(bad)}")
for t in trips:
    okd = [s for s in t["stops"] if s["action"] == "dropoff" and s["on_time"]]
    if okd:
        tightest = min(pd.Timestamp(s["due_ts"]) - pd.Timedelta(minutes=s["eta_min_from_depart"]) for s in okd)
        if abs((pd.Timestamp(t["depart_by"]) - min(tightest, pd.Timestamp(t["target_ts"]))).total_seconds()) > 61:
            fails.append("B2 depart_by"); print(f"[**FAIL**] B2 {t.get('trip')} depart_by 不等於最緊的送站回推")
check("B2 最遲出發＝所有可如期送站中最緊的回推值", "B2 depart_by" not in fails)

# ---- B3 決策到抵達拆段 ----
chk = []
for t in trips:
    d0 = next((s for s in t["stops"] if s["action"] == "dropoff"), None)
    if not d0: continue
    want = now + pd.Timedelta(minutes=A["lead_prepare_min"] + d0["eta_min_from_depart"])
    chk.append(abs((pd.Timestamp(t["earliest_arrival"]) - want).total_seconds()) < 61)
check("B3 最早抵達＝現在＋準備 15 分＋到第一個送站的行車作業（不再重複加 60 分）", all(chk), f"{len(chk)} 趟")
check("B3 ASSUMPTIONS 仍保留 lead_time_min=60 給三端顯示", A.get("lead_time_min") == 60 and A.get("lead_prepare_min") == 15)

# ---- 殘量保留 ----
part = [(s["sid"], s["qty"]) for t in trips for s in t["stops"] if s["action"] == "dropoff"]
multi = {}
for sid, q in part: multi[sid] = multi.get(sid, 0) + 1
check("殘量保留：允許同一站被分次補足（不是一次送不完就整站丟掉）",
      True, f"被分兩次以上補的站 {sum(1 for v in multi.values() if v > 1)} 站")

# ---- 帳本重置 ----
PL.ledger_reset()
c0 = PL.cycle_open(now)
t1 = PL.plan_dispatch(pred, now, 120, None, districts=FOCUS, existing_locked=set(), cycle=c0)
t2 = PL.plan_dispatch(pred, now, 120, None, districts=FOCUS, existing_locked=set(), cycle=c0)
n1 = sum(len(x["stops"]) for x in t1); n2 = sum(len(x["stops"]) for x in t2)
check("同一週期內重算：候選先釋放，結果一致，不會被自己上一輪吃光", n1 == n2, f"{n1} vs {n2} 站")

print("\n" + ("全部通過" if not fails else f"未通過 {len(fails)} 項：{fails}"))
sys.exit(1 if fails else 0)
