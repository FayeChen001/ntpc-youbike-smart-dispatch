"""
test_a_numeric.py — A 主線數值口徑的手算驗證（N01／N02／N07 的 ack≠指派）。

期望值一律在這裡手算，不呼叫被測程式來產生答案。
執行：python3 tests/test_a_numeric.py
"""
import os
import sys

import numpy as np
import pandas as pd

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(ROOT, "app"))
sys.path.insert(0, os.path.join(ROOT, "pipeline"))

import metrics as MX  # noqa: E402

NAN = float("nan")
CAP = 10.0

# 五個分箱：07:30 08:00 08:30 09:00 09:30
BINS = pd.DatetimeIndex(pd.date_range("2026-06-16 07:30", periods=5, freq="30min"))

# 三站的可借車數。NaN＝該分箱沒有觀測。
#   站0：正常 → 零 零 零 → 正常        （N01：三筆零快照，跨度手算 60 分）
#   站1：缺測 → 零 → 缺測 → 零 → 缺測  （N02：前後都沒有確認正常的觀測，上界必須是未知）
#   站2：正常 → 缺測 → 零 零 → 正常    （N02：中間有缺測，上界已知但必須標記含缺測）
B = np.array([
    [3.0, NAN, 5.0],
    [0.0, 0.0, NAN],
    [0.0, NAN, 0.0],
    [0.0, 0.0, 0.0],
    [2.0, NAN, 4.0],
], dtype=np.float32)
S = np.where(np.isnan(B), NAN, CAP - np.nan_to_num(B)).astype(np.float32)


class _Ctx:
    def __init__(self):
        self.B, self.S = B, S
        self.C = np.full_like(B, CAP)
        self.FLAT = np.zeros_like(B)


class _FakePredictor:
    def __init__(self):
        self.ctx = _Ctx()
        self.bins = BINS
        self.invalid = set()
        self.st = pd.DataFrame({
            "sid": [0, 1, 2],
            "name": ["測試站甲", "測試站乙", "測試站丙"],
            "district": ["測試區", "測試區", "測試區"],
        })

    def neighbors(self, sid, radius_m=500):
        return []


FAILS = []


def check(name, got, want):
    ok = got == want
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}: got={got!r} want={want!r}")
    if not ok:
        FAILS.append(name)


def check_true(name, cond, detail=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}{(' — ' + detail) if detail else ''}")
    if not cond:
        FAILS.append(name)


def main():
    m = MX.ServiceMetrics(_FakePredictor())
    r = m.compute("day", 4, "empty")

    print("N01 觀測跨度，不得宣稱確定連續中斷")
    # 站0 的零快照在 index 1,2,3。首末相差 2 個分箱 → 手算跨度 2×30 = 60 分。
    # 三筆快照涵蓋三個分箱 → 零值快照站‧分鐘 3×30 = 90。兩者不是同一件事。
    ev0 = [e for e in r["longest"] if e["sid"] == 0]
    check("站0 事件數", len(ev0), 1)
    check("站0 觀測跨度(分)", ev0[0]["span_min"], 60)
    check("站0 零值快照數", ev0[0]["snapshots"], 3)
    check_true("事件欄位沒有 lower_min 這種會被讀成『確定下界』的名字",
               "lower_min" not in ev0[0], f"keys={sorted(ev0[0])}")
    check_true("counts 用 span_ 前綴", all(k in r["counts"] for k in ("span_ge30", "span_ge60", "span_ge120")))
    check("跨度 ≥60 分的事件數", r["counts"]["span_ge60"], 1)
    check("跨度 ≥120 分的事件數", r["counts"]["span_ge120"], 0)
    check_true("定義文字明講跨度不是確定連續中斷",
               "不是確定的連續中斷時間" in r["definitions"]["span_min"])

    print("N01 全體計數")
    # 事件：站0 一段(1..3)；站1 兩段(1..1)(3..3)；站2 一段(2..3) → 手算 4 段
    check("事件總數", r["counts"]["events"], 4)
    # 零快照格數：站0 三格 + 站1 兩格 + 站2 兩格 = 7 → 7×30 = 210
    check("零值快照站‧分鐘", r["zero_snapshot_station_min"], 210)
    check("單筆快照事件數", r["counts"]["single_snapshot"], 2)

    print("N02 未知上界必須保留未知")
    ev1 = sorted([e for e in r["longest"] if e["sid"] == 1], key=lambda e: e["start"])
    check("站1 事件數", len(ev1), 2)
    for e in ev1:
        check_true(f"站1 {e['start'][11:16]} 上界為 None",
                   e["upper_min"] is None, f"upper_min={e['upper_min']!r}")
        check_true(f"站1 {e['start'][11:16]} 未捏造成 跨度+30",
                   e["upper_min"] != e["span_min"] + 30)
    check("上界未知的事件數", r["upper"]["unknown"], 2)
    check("上界已知的事件數", r["upper"]["known"], 2)

    print("N02 含缺測但上界可界定者，要標記缺測")
    ev2 = [e for e in r["longest"] if e["sid"] == 2]
    check("站2 事件數", len(ev2), 1)
    # 站2：事件前最後一筆確認正常是 index0，事件後第一筆確認正常是 index4 → 手算上界 4×30 = 120
    check("站2 上界(分)", ev2[0]["upper_min"], 120)
    check("站2 觀測跨度(分)", ev2[0]["span_min"], 30)
    check_true("站2 標記區間內含缺測", ev2[0]["gap_inside"] is True)
    check("含缺測的事件數", r["upper"]["gap_inside"], 1)
    # 站0 前後緊鄰都是確認正常的觀測，不該被標成含缺測
    check_true("站0 未被誤標含缺測", ev0[0]["gap_inside"] is False)

    print("N02 分佈圖只統計已知上界")
    # 已知上界只有兩件，都是 120 分 → 落在「120–150 分」那一桶
    hist = {h["label"]: h for h in r["duration_hist"]}
    check("上界分佈 120–150 分桶", hist["120–150 分"]["upper"], 2)
    check("上界分佈 單筆快照桶", hist["單筆快照"]["upper"], 0)
    check("另外列出的上界未知件數", r["upper_unknown"], 2)

    print("N07 ack 不等於指派")
    now = "2026-06-16 09:30"
    log = [
        # 甲：有人按了確認，但沒有指派負責人
        {"id": "e1", "type": "persistent_empty", "status": "acked", "opened": "2026-06-16 08:00",
         "acked": "2026-06-16 08:30", "owner": None, "resolved": None},
        # 乙：確認並指派
        {"id": "e2", "type": "persistent_empty", "status": "acked", "opened": "2026-06-16 08:00",
         "acked": "2026-06-16 08:30", "assigned_at": "2026-06-16 09:00", "owner": "值班調度 A", "resolved": None},
        # 丙：完全沒人碰
        {"id": "e3", "type": "persistent_full", "status": "open", "opened": "2026-06-16 09:00",
         "acked": None, "owner": None, "resolved": None},
    ]
    f = MX.flow_metrics(log, [], now)
    fn = f["funnel"]
    check("開啟數", fn["opened"], 3)
    check("ack 數", fn["acked"], 2)
    check("指派數（有 owner）", fn["assigned"], 1)
    check("已確認但未指派", fn["acked_not_assigned"], 1)
    # 未指派比例＝1 − 1/3，手算 0.667；若誤用 ack 會變成 1 − 2/3 = 0.333
    check("未指派比例以 owner 計", fn["unassigned_ratio"], 0.667)
    check("未確認比例另計", fn["unacked_ratio"], 0.333)
    check_true("定義文字明講 ack 不等於指派", "不代表已經指派給誰" in f["definitions"]["ack"])
    check_true("定義文字禁止以任務清單推論人員未到場",
               "不能據此推論" in f["definitions"]["no_patrol_inference"])
    # 甲 08:30 確認、乙 08:30 確認 → 開啟到確認都是 30 分，手算中位數 30
    st = {x["key"]: x for x in f["stages"]}
    check("開啟→ack 樣本數", st["open_to_ack"]["n"], 2)
    check("開啟→ack 中位數(分)", st["open_to_ack"]["p50"], 30.0)
    # 只有乙有 assigned_at：08:30 → 09:00，手算 30 分，樣本 1
    check("ack→指派 樣本數", st["ack_to_assign"]["n"], 1)
    check("ack→指派 中位數(分)", st["ack_to_assign"]["p50"], 30.0)

    print("候選觸發量不得叫做通知量")
    rep_path = os.path.join(ROOT, "reports", "model_eval.json")
    if os.path.exists(rep_path):
        import json
        t = MX.threshold_tradeoff(json.load(open(rep_path)), 120, "empty", 20)
        row = t["rows"][0]
        check_true("欄位改名為 candidate_cells", "candidate_cells" in row and "alerts" not in row,
                   f"keys={sorted(row)}")
        check_true("單位說明存在", "站×時點" in t["unit"])
        check_true("明講不是通知量", "不是使用者會收到的通知量" in t["dedup_warning"])
        check_true("不提供通知量估計", "不提供通知量的估計值" in t["dedup_warning"])
    else:
        print("  [SKIP] reports/model_eval.json 不存在")

    print()
    if FAILS:
        print(f"FAILED {len(FAILS)}: {FAILS}")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
