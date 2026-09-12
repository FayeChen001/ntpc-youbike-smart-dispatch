"""
metrics.py — 驗收指標：從觀測快照計算服務中斷事件，並彙整流程時效與調度效益。

界線（前端會原文顯示）：
- 資料是每 30 分鐘一筆的庫存快照，不是借還交易。零車快照不等於有人借不到，也不等於失敗旅次。
- 一段零車觀測只能給「觀測下界」與「可能上界」：首末兩筆零車快照相距 k 個分箱，下界 k×30 分鐘；
  真正的起訖落在前一筆正常觀測與後一筆正常觀測之間，上界 = (後一筆正常 − 前一筆正常) × 30 分鐘。
- 缺測不視為恢復，也不視為持續中斷：缺測會切斷事件並標記，該事件的上界不可收斂。
- 雙零、24 小時以上庫存無變化、容量矛盾的站點分箱一律排除，那是待查狀態不是服務中斷事件。
"""
import json, os
import numpy as np
import pandas as pd

BIN_MIN = 30          # 觀測解析度
TARGET_MIN = 30       # 訪談轉述的恢復目標（待主管確認，非官方 SLA）
STALE_BINS = 48       # 24 小時無變化

WINDOWS = [
    {"key": "day", "label": "過去 24 小時", "bins": 48},
    {"key": "week", "label": "過去 7 天", "bins": 336},
    {"key": "month", "label": "過去 30 天", "bins": 1440},
    {"key": "all", "label": "全期 1–6 月", "bins": None},
]
KIND_LABEL = {"empty": "無車可借", "full": "無位可還"}


def _pct(a, q):
    return None if len(a) == 0 else round(float(np.percentile(a, q)), 1)


class ServiceMetrics:
    """以回放矩陣計算服務中斷事件。結果依 (視窗, 類型) 快取。"""

    def __init__(self, predictor):
        self.P = predictor
        inv = sorted(predictor.invalid)
        if inv:
            a = np.asarray(inv, dtype=np.int32)
            self.inv_t, self.inv_s = a[:, 0], a[:, 1]
        else:
            self.inv_t = self.inv_s = np.zeros(0, dtype=np.int32)
        self._cache = {}

    # ------------------------------------------------------------------ 視窗
    def window_range(self, key, t_idx):
        n = len(self.P.bins)
        w = next((w for w in WINDOWS if w["key"] == key), WINDOWS[0])
        t1 = min(t_idx + 1, n)                      # 含當下這一筆
        t0 = 0 if w["bins"] is None else max(0, t1 - w["bins"])
        return t0, t1, w

    # ------------------------------------------------------------------ 事件
    def compute(self, key, t_idx, kind="empty"):
        t0, t1, w = self.window_range(key, t_idx)
        ck = (t0, t1, kind)
        if ck in self._cache:
            return self._cache[ck]
        ctx = self.P.ctx
        T = t1 - t0
        N = ctx.B.shape[1]
        st = self.P.st

        # 容量矛盾分箱（該視窗內）
        m = (self.inv_t >= t0) & (self.inv_t < t1)
        inv_t, inv_s = self.inv_t[m] - t0, self.inv_s[m]

        ev_sid, ev_s0, ev_s1, ev_prev, ev_next = [], [], [], [], []
        excl = {"no_data": 0, "both_zero": 0, "stale_flat": 0, "cap_conflict": int(len(inv_t))}
        outage_cells = 0
        gap_runs = 0

        CH = 320                                    # 分批處理，控制記憶體
        for c0 in range(0, N, CH):
            c1 = min(N, c0 + CH)
            Bc = ctx.B[t0:t1, c0:c1].astype(np.float32, copy=True)
            Sc = ctx.S[t0:t1, c0:c1].astype(np.float32, copy=True)
            Fc = ctx.FLAT[t0:t1, c0:c1]
            valid = ~np.isnan(Bc) & ~np.isnan(Sc)
            both0 = valid & (Bc == 0) & (Sc == 0)
            stale = np.nan_to_num(np.asarray(Fc, dtype=np.float32), nan=0.0) >= STALE_BINS
            invc = np.zeros_like(valid)
            sel = (inv_s >= c0) & (inv_s < c1)
            if sel.any():
                invc[inv_t[sel], inv_s[sel] - c0] = True
            drop = (~valid) | both0 | stale | invc
            excl["no_data"] += int((~valid).sum())
            excl["both_zero"] += int((both0 & ~stale).sum())
            excl["stale_flat"] += int(stale.sum())

            zero = valid & ((Bc == 0) if kind == "empty" else (Sc == 0)) & ~both0
            out = zero & ~drop                      # 服務中斷分箱
            ok = valid & ~drop & ~zero              # 確認服務正常的觀測
            outage_cells += int(out.sum())

            # 資料中斷（缺測）連續段數
            gap_runs += _n_runs(~valid)

            if not out.any():
                del Bc, Sc, valid, both0, stale, invc, drop, zero, out, ok
                continue

            idx = np.arange(T, dtype=np.int32)[:, None]
            prev = np.where(ok, idx, np.int32(-1))
            np.maximum.accumulate(prev, axis=0, out=prev)
            nxt = np.where(ok, idx, np.int32(T))
            nxt = np.minimum.accumulate(nxt[::-1], axis=0)[::-1]

            s_t, e_t, cols = _runs(out)
            ev_sid.append(cols + c0)
            ev_s0.append(s_t)
            ev_s1.append(e_t)
            ev_prev.append(prev[s_t, cols])
            ev_next.append(nxt[e_t, cols])
            del Bc, Sc, valid, both0, stale, invc, drop, zero, out, ok, prev, nxt

        if ev_sid:
            sid = np.concatenate(ev_sid)
            s0 = np.concatenate(ev_s0).astype(np.int64)
            s1 = np.concatenate(ev_s1).astype(np.int64)
            pv = np.concatenate(ev_prev).astype(np.int64)
            nx = np.concatenate(ev_next).astype(np.int64)
        else:
            sid = s0 = s1 = pv = nx = np.zeros(0, dtype=np.int64)

        lower = (s1 - s0) * BIN_MIN                                   # 觀測下界
        open_left, open_right = pv < 0, nx >= T
        upper = np.where(open_left | open_right, -1, (nx - pv) * BIN_MIN)
        gap_adj = (~open_left & (pv != s0 - 1)) | (~open_right & (nx != s1 + 1))
        over = np.maximum(0, lower - TARGET_MIN)                      # 超過恢復目標的站分鐘（下界）
        upper_eff = np.where(upper >= 0, upper, lower + BIN_MIN)       # 上界未收斂時，至少以下界＋一格計
        over_up = np.maximum(0, upper_eff - TARGET_MIN)
        up = upper[upper >= 0]

        bins = self.P.bins
        names = st["name"].values
        dists = st["district"].values

        res = {
            "window": {"key": w["key"], "label": w["label"], "t0": str(bins[t0]), "t1": str(bins[t1 - 1]),
                       "bins": int(T), "days": round(T * BIN_MIN / 1440, 1), "resolution_min": BIN_MIN},
            "kind": kind, "kind_label": KIND_LABEL[kind], "target_min": TARGET_MIN,
            "counts": {"events": int(len(lower)),
                       "ge30": int((lower >= 30).sum()), "ge60": int((lower >= 60).sum()),
                       "ge120": int((lower >= 120).sum()), "single_snapshot": int((lower == 0).sum())},
            "counts_upper": {"ge30": int((upper_eff >= 30).sum()), "ge60": int((upper_eff >= 60).sum()),
                             "ge120": int((upper_eff >= 120).sum())},
            "events_per_day": round(len(lower) / max(1e-9, T * BIN_MIN / 1440), 1),
            "stations_affected": int(len(np.unique(sid))) if len(sid) else 0,
            "stations_total": int(len(st)),
            "outage_cells": outage_cells,
            "over_target_station_min": int(over.sum()),
            "over_target_station_min_upper": int(over_up.sum()),
            "recovery": {"p50_lower": _pct(lower, 50), "p90_lower": _pct(lower, 90), "max_lower": int(lower.max()) if len(lower) else 0,
                         "p50_upper": _pct(up, 50), "p90_upper": _pct(up, 90),
                         "bounded": int(len(up)), "open_ended": int((upper < 0).sum()), "gap_adjacent": int(gap_adj.sum())},
            "data_gaps": {"runs": gap_runs, "cells": excl["no_data"]},
            "excluded_cells": excl,
            "source": f"回放觀測快照（{BIN_MIN} 分鐘一筆）真實計算；未使用模擬資料",
        }

        # 觀測長度分佈
        edges = [0, 30, 60, 90, 120, 180, 10 ** 9]
        labels = ["單筆快照", "30 分", "60 分", "90 分", "120–150 分", "180 分以上"]
        hist = []
        for i, lab in enumerate(labels):
            lo, hi = edges[i], edges[i + 1]
            n_l = int(((lower >= lo) & (lower < hi)).sum())
            n_u = int(((upper_eff >= lo) & (upper_eff < hi)).sum())
            hist.append({"label": lab, "lower": n_l, "upper": n_u})
        res["duration_hist"] = hist

        # 依行政區
        by_d = {}
        for i in range(len(lower)):
            d = dists[sid[i]]
            r = by_d.setdefault(d, {"district": d, "events": 0, "ge60": 0, "over_min": 0})
            r["events"] += 1
            r["ge60"] += int(lower[i] >= 60)
            r["over_min"] += int(over[i])
        res["by_district"] = sorted(by_d.values(), key=lambda r: -r["over_min"])[:14]

        # 依時段（事件起始的小時）
        hours = np.zeros(24, dtype=np.int64)
        hours_ge60 = np.zeros(24, dtype=np.int64)
        if len(s0):
            hh = bins[t0 + s0].hour.values
            np.add.at(hours, hh, 1)
            np.add.at(hours_ge60, hh[lower >= 60], 1)
        res["by_hour"] = [{"hour": h, "events": int(hours[h]), "ge60": int(hours_ge60[h])} for h in range(24)]

        # 最嚴重站點
        by_s = {}
        for i in range(len(lower)):
            r = by_s.setdefault(int(sid[i]), {"sid": int(sid[i]), "name": names[sid[i]], "district": dists[sid[i]],
                                              "events": 0, "over_min": 0, "max_lower": 0})
            r["events"] += 1
            r["over_min"] += int(over[i])
            r["max_lower"] = max(r["max_lower"], int(lower[i]))
        res["top_stations"] = sorted(by_s.values(), key=lambda r: -r["over_min"])[:12]

        # 最長事件
        if len(lower):
            ordr = np.argsort(-lower)[:10]
            res["longest"] = [{"sid": int(sid[i]), "name": names[sid[i]], "district": dists[sid[i]],
                               "start": str(bins[t0 + s0[i]]), "end": str(bins[t0 + s1[i]]),
                               "lower_min": int(lower[i]), "upper_min": (None if upper[i] < 0 else int(upper[i])),
                               "snapshots": int(s1[i] - s0[i] + 1), "gap_adjacent": bool(gap_adj[i])} for i in ordr]
        else:
            res["longest"] = []

        self._cache[ck] = res
        if len(self._cache) > 24:
            self._cache.pop(next(iter(self._cache)))
        return res


def _runs(mask):
    """(T,N) 布林矩陣 → 每一段連續 True 的 (起始列, 結束列含, 欄)。"""
    m = mask.astype(np.int8)
    pad = np.zeros((1, m.shape[1]), dtype=np.int8)
    d = np.diff(np.vstack([pad, m, pad]), axis=0)
    s = np.argwhere(d == 1)
    e = np.argwhere(d == -1)
    s = s[np.lexsort((s[:, 0], s[:, 1]))]
    e = e[np.lexsort((e[:, 0], e[:, 1]))]
    return s[:, 0], e[:, 0] - 1, s[:, 1]


def _n_runs(mask):
    if not mask.any():
        return 0
    m = mask.astype(np.int8)
    pad = np.zeros((1, m.shape[1]), dtype=np.int8)
    return int((np.diff(np.vstack([pad, m]), axis=0) == 1).sum())


# ---------------------------------------------------------------- 告警門檻取捨
def threshold_tradeoff(eval_json, horizon, kind="empty", duty_alerts_per_hour=None):
    """
    用測試期（六月）的校準分箱，近似「門檻 → 告警量／精準度／召回」取捨表。
    解析度限制：分箱寬 0.1，門檻只能落在分箱邊界；≥0.9 那一箱幾乎都是「已經零車且延續」，
    因此整體精準度會被墊高，新發生事件的召回要另外看 new_event_recall。
    """
    h = eval_json.get("horizons", {}).get(str(horizon))
    if not h:
        return None
    e = h["hgb"]["all"][kind]
    cal = e["calibration"]
    n_total = e["n"]
    positives = e["positives"]
    test_hours = 30 * 24                        # 六月測試期
    rows = []
    for k in range(1, 10):                      # 門檻 0.1 ... 0.9
        sel = [b for b in cal if b["bin"] >= k]
        n = sum(b["n"] for b in sel)
        hit = sum(b["n"] * b["obs_rate"] for b in sel)
        rows.append({
            "threshold": round(k / 10, 1),
            "alerts": int(n),
            "alerts_per_hour": round(n / test_hours, 1),
            "precision": round(hit / n, 3) if n else None,
            "recall": round(hit / positives, 3) if positives else None,
            "false_per_hour": round((n - hit) / test_hours, 1),
        })
    pick = None
    if duty_alerts_per_hour:
        ok = [r for r in rows if r["alerts_per_hour"] <= duty_alerts_per_hour]
        pick = min(ok, key=lambda r: r["threshold"])["threshold"] if ok else None
    floor = rows[-1]["alerts_per_hour"]         # 門檻拉到 0.9 仍然發出的量
    new_per_hour = round(e["new_events"] / test_hours, 1)
    zw = "零車" if kind == "empty" else "零位"
    gw = "缺車" if kind == "empty" else "缺位"
    return {
        "horizon": horizon, "kind": kind, "kind_label": KIND_LABEL[kind],
        "rows": rows,
        "positives": positives, "n": n_total, "new_events": e["new_events"],
        "new_event_recall": round(e["new_event_recall"], 3),
        "new_event_precision": round(e["new_event_precision"], 3),
        "new_event_pr_auc": round(e["new_event_pr_auc"], 3) if e.get("new_event_pr_auc") else None,
        "current_threshold": 0.6 if horizon == 60 else 0.5,
        "suggested_threshold": pick,
        "duty_alerts_per_hour": duty_alerts_per_hour,
        "test_hours": test_hours,
        "floor_per_hour": floor,
        "new_events_per_hour": new_per_hour,
        "threshold_label": f"{zw}機率門檻",
        "note": ("門檻只能落在校準分箱邊界（每 0.1）；告警量為全市 1,583 站每小時平均。"
                 f"≥0.9 分箱多為「當下已{zw}且延續」，會墊高整體精準度，"
                 f"新發生事件召回 {e['new_event_recall']:.1%} 才是預警價值。"),
        "conclusion": (f"門檻拉到 0.9，全市每小時仍會發出 {floor} 則，其中大多是「已經{zw}、持續中」的站，"
                       f"而真正新發生的事件本來就有每小時 {new_per_hour} 件。"
                       "把門檻調高不會把量降到值班可處理的範圍；要降量必須改變告警定義"
                       f"（只對新發生、且 500 公尺內沒有替代站、需要{'派車補車' if kind == 'empty' else '派車運出'}的{gw}發告警），"
                       "並以行政區或站群彙總，而不是逐站逐則。"),
        "source": "reports/model_eval.json（六月測試期一次評估）",
    }


# ---------------------------------------------------------------- 流程時效
STAGE_DEFS = [
    ("open_to_ack", "開啟 → 值班確認", "opened", "acked"),
    ("ack_to_dispatch", "確認 → 轉調度", "acked", "dispatched"),
    ("open_to_resolve", "開啟 → 結案", "opened", "resolved"),
]


def flow_metrics(alert_log, tasks, now_ts):
    """本場回放的流程時效。時間戳來自回放時鐘，動作由人在介面上按下，流程狀態多為模擬。"""
    svc = [a for a in alert_log if a["type"] not in ("stale_flat", "both_zero", "cap_conflict")]
    stages = []
    for kkey, label, a_key, b_key in STAGE_DEFS:
        vals = []
        for a in svc:
            t0, t1 = a.get(a_key), a.get(b_key)
            if t0 and t1:
                vals.append((pd.Timestamp(t1) - pd.Timestamp(t0)).total_seconds() / 60)
        vals = np.array(vals, dtype=float)
        stages.append({"key": kkey, "label": label, "n": int(len(vals)),
                       "p50": _pct(vals, 50), "p90": _pct(vals, 90),
                       "mean": round(float(vals.mean()), 1) if len(vals) else None})

    opened = len(svc)
    acked = sum(1 for a in svc if a.get("acked"))
    dispatched = sum(1 for a in svc if a.get("dispatched"))
    resolved_manual = sum(1 for a in svc if a.get("resolved") and a.get("resolve_reason", "").find("自動") < 0)
    resolved_auto = sum(1 for a in svc if a.get("resolved") and a.get("resolve_reason", "").find("自動") >= 0)
    still_open = [a for a in svc if a.get("status") in ("open",)]
    aging = [(pd.Timestamp(now_ts) - pd.Timestamp(a["opened"])).total_seconds() / 60 for a in still_open]

    # 派車任務的時限達成（planner 的可行性判定，非實測）
    real = [t for t in tasks if t.get("stops")]
    on_time_stops = sum(1 for t in real for s in t["stops"] if s.get("action") == "dropoff" and s.get("on_time"))
    all_stops = sum(1 for t in real for s in t["stops"] if s.get("action") == "dropoff")
    return {
        "stages": stages,
        "funnel": {"opened": opened, "acked": acked, "dispatched": dispatched,
                   "resolved_manual": resolved_manual, "resolved_auto": resolved_auto,
                   "still_open": len(still_open),
                   "unassigned_ratio": round(1 - acked / opened, 3) if opened else None,
                   "oldest_open_min": round(max(aging), 1) if aging else None},
        "dispatch": {
            "tasks": len(tasks),
            "planned": sum(1 for t in tasks if t.get("status") in ("planned", "dispatched", "en_route", "done")),
            "too_late": sum(1 for t in tasks if t.get("status") == "too_late"),
            "needs_cross_district": sum(1 for t in tasks if t.get("status") == "needs_cross_district"),
            "minor_gap": sum(1 for t in tasks if t.get("status") == "minor_gap"),
            "bikes_moved": sum(int(t.get("load") or 0) for t in real),
            "route_km": round(sum(float(t.get("route_km") or 0) for t in real), 1),
            "drop_stops": all_stops,
            "on_time_stops": on_time_stops,
            "on_time_ratio": round(on_time_stops / all_stops, 3) if all_stops else None,
        },
        "note": ("本場回放的操作時間：告警開啟與自動解除由觀測與預測觸發，確認／轉調度由人在介面按下，"
                 "出車、抵達、完成為依假設推進的模擬狀態。非真實派工系統量測。"),
    }


# ---------------------------------------------------------------- 尚無法計算
NOT_MEASURABLE = [
    {"item": "診斷分流正確率（車輛／車柱／站端）", "need": "車號—柱號對應、租借錯誤碼、設備心跳、維修驗收結果",
     "why": "目前只有站點層級庫存快照，沒有單車或車柱識別，無法建立真值，也無法驗證分流是否分對。"},
    {"item": "通知送達率與手機確認時間", "need": "Web Push 訂閱、平台回執、開啟與確認事件",
     "why": "現在是頁內即時通知，沒有背景推播，送達與否無法量測，不能宣稱已送達。"},
    {"item": "調度改善的實際成效", "need": "試辦對照組（有調度／無調度）與同期真實借還紀錄",
     "why": "回放只能算模擬方案的預計缺口補足，沒有對照就不能把模擬改善說成實測成效。"},
    {"item": "失敗旅次與到站成功率", "need": "借還交易與 App 查詢紀錄",
     "why": "快照零車不等於有人借不到；鄰站有車也不等於使用者走得到。"},
]
