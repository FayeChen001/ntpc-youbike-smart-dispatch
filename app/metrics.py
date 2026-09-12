"""
metrics.py — 驗收指標：從觀測快照計算服務中斷事件，並彙整流程時效與調度效益。

界線（前端會原文顯示）：
- 資料是每 30 分鐘一筆的庫存快照，不是借還交易。零車快照不等於有人借不到，也不等於失敗旅次。
- 一段零車觀測只能給「觀測跨度」與「可能上界」：首末兩筆零車快照相距 k 個分箱，跨度 k×30 分鐘。
  跨度不是確定的連續中斷時間——快照之間是否曾短暫恢復，資料證明不了。
  上界 =（事件後第一筆確認正常 − 事件前最後一筆確認正常）× 30 分鐘，前後任一側沒有確認正常的觀測時
  上界為「未知」，不以跨度加一格之類的數字充數。
- 缺測不視為恢復，也不視為持續中斷：缺測會切斷事件並標記。
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

        # 口徑（N01／N02）：
        # span_min 是「首末兩筆零快照的時間差」，只是觀測跨度。08:00/08:30/09:00 三筆零快照的跨度是
        # 60 分鐘，不代表確定連續中斷 60 分鐘，更不是 90 分鐘——快照之間是否曾短暫恢復，資料證明不了。
        # upper_min 只有在事件前後都各有一筆「確認正常」的觀測時才存在；否則保留未知，不以 span+30 充數。
        span = (s1 - s0) * BIN_MIN
        zero_min = (s1 - s0 + 1) * BIN_MIN          # 觀測到的零值快照換算站‧分鐘（每筆快照代表一個分箱）
        open_left, open_right = pv < 0, nx >= T
        upper_known = ~(open_left | open_right)
        upper = np.where(upper_known, (nx - pv) * BIN_MIN, -1)
        gap_inside = (~open_left & (pv != s0 - 1)) | (~open_right & (nx != s1 + 1))
        up = upper[upper_known]

        bins = self.P.bins
        names = st["name"].values
        dists = st["district"].values

        res = {
            "window": {"key": w["key"], "label": w["label"], "t0": str(bins[t0]), "t1": str(bins[t1 - 1]),
                       "bins": int(T), "days": round(T * BIN_MIN / 1440, 1), "resolution_min": BIN_MIN},
            "kind": kind, "kind_label": KIND_LABEL[kind], "target_min": TARGET_MIN,
            "counts": {"events": int(len(span)),
                       "span_ge30": int((span >= 30).sum()), "span_ge60": int((span >= 60).sum()),
                       "span_ge120": int((span >= 120).sum()), "single_snapshot": int((span == 0).sum())},
            "upper": {"known": int(upper_known.sum()), "unknown": int((~upper_known).sum()),
                      "ge30": int((up >= 30).sum()), "ge60": int((up >= 60).sum()), "ge120": int((up >= 120).sum()),
                      "gap_inside": int(gap_inside.sum())},
            "events_per_day": round(len(span) / max(1e-9, T * BIN_MIN / 1440), 1),
            "stations_affected": int(len(np.unique(sid))) if len(sid) else 0,
            "stations_total": int(len(st)),
            "outage_cells": outage_cells,
            "zero_snapshot_station_min": int(outage_cells * BIN_MIN),
            "span_over_target_station_min": int(np.maximum(0, span - TARGET_MIN).sum()),
            "duration": {"span_p50": _pct(span, 50), "span_p90": _pct(span, 90),
                         "span_max": int(span.max()) if len(span) else 0,
                         "upper_p50": _pct(up, 50), "upper_p90": _pct(up, 90),
                         "upper_max": int(up.max()) if len(up) else None},
            "data_gaps": {"runs": gap_runs, "cells": excl["no_data"]},
            "excluded_cells": excl,
            "definitions": {
                "span_min": "首末兩筆零值快照的時間差。只出現一筆時為 0。這是觀測跨度，不是確定的連續中斷時間。",
                "upper_min": "事件前最後一筆確認正常的觀測，到事件後第一筆確認正常的觀測之間的時間差。"
                             "任一側沒有確認正常的觀測（延伸到期間邊界，或相鄰是缺測）時，上界為未知，不以其他數字代替。",
                "zero_snapshot_station_min": "觀測到的零值快照數 × 30 分鐘。這是觀測量，不宣稱期間連續中斷。",
                "not_claimed": "零車快照不等於有人借不到，也不等於失敗旅次。本節沒有任何一個數字是需求或到站成功率。",
            },
            "source": f"回放觀測快照（{BIN_MIN} 分鐘一筆）真實計算；未使用模擬資料",
        }

        # 觀測長度分佈
        edges = [0, 30, 60, 90, 120, 180, 10 ** 9]
        labels = ["單筆快照", "30 分", "60 分", "90 分", "120–150 分", "180 分以上"]
        hist = []
        for i, lab in enumerate(labels):
            lo, hi = edges[i], edges[i + 1]
            hist.append({"label": lab,
                         "span": int(((span >= lo) & (span < hi)).sum()),
                         "upper": int(((up >= lo) & (up < hi)).sum())})
        res["duration_hist"] = hist
        res["upper_unknown"] = int((~upper_known).sum())

        # 依行政區
        by_d = {}
        for i in range(len(span)):
            d = dists[sid[i]]
            r = by_d.setdefault(d, {"district": d, "events": 0, "span_ge60": 0, "zero_min": 0})
            r["events"] += 1
            r["span_ge60"] += int(span[i] >= 60)
            r["zero_min"] += int(zero_min[i])
        res["by_district"] = sorted(by_d.values(), key=lambda r: -r["zero_min"])[:14]

        # 依時段（事件起始的小時）
        hours = np.zeros(24, dtype=np.int64)
        hours_ge60 = np.zeros(24, dtype=np.int64)
        if len(s0):
            hh = bins[t0 + s0].hour.values
            np.add.at(hours, hh, 1)
            np.add.at(hours_ge60, hh[span >= 60], 1)
        res["by_hour"] = [{"hour": h, "events": int(hours[h]), "span_ge60": int(hours_ge60[h])} for h in range(24)]

        # 最嚴重站點
        by_s = {}
        for i in range(len(span)):
            r = by_s.setdefault(int(sid[i]), {"sid": int(sid[i]), "name": names[sid[i]], "district": dists[sid[i]],
                                              "events": 0, "zero_min": 0, "max_span": 0})
            r["events"] += 1
            r["zero_min"] += int(zero_min[i])
            r["max_span"] = max(r["max_span"], int(span[i]))
        res["top_stations"] = sorted(by_s.values(), key=lambda r: -r["zero_min"])[:12]

        # 最長事件
        if len(span):
            ordr = np.argsort(-span)[:10]
            res["longest"] = [{"sid": int(sid[i]), "name": names[sid[i]], "district": dists[sid[i]],
                               "start": str(bins[t0 + s0[i]]), "end": str(bins[t0 + s1[i]]),
                               "span_min": int(span[i]), "upper_min": (None if upper[i] < 0 else int(upper[i])),
                               "snapshots": int(s1[i] - s0[i] + 1), "gap_inside": bool(gap_inside[i])} for i in ordr]
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
            "candidate_cells": int(n),
            "candidate_cells_per_hour": round(n / test_hours, 1),
            "precision": round(hit / n, 3) if n else None,
            "recall": round(hit / positives, 3) if positives else None,
            "false_candidates_per_hour": round((n - hit) / test_hours, 1),
        })
    pick = None
    if duty_alerts_per_hour:
        ok = [r for r in rows if r["candidate_cells_per_hour"] <= duty_alerts_per_hour]
        pick = min(ok, key=lambda r: r["threshold"])["threshold"] if ok else None
    floor = rows[-1]["candidate_cells_per_hour"]   # 門檻拉到 0.9 仍然被觸發的候選量
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
        "unit": "候選觸發的站×時點筆數（每 30 分鐘一個時點、全市 1,583 站）",
        "dedup_warning": ("這一欄是「候選觸發量」，不是使用者會收到的通知量。實際系統對同一站同一類型"
                          "開啟中的事件不重發，並對逐則通知節流，去重後的通知量會低很多——但去重比例取決於"
                          "事件持續多久，無法從校準分箱推算，所以這裡不提供通知量的估計值。"),
        "note": ("門檻只能落在校準分箱邊界（每 0.1）；候選量為全市 1,583 站每小時平均。"
                 f"≥0.9 分箱多為「當下已{zw}且延續」，會墊高整體精準度，"
                 f"新發生事件召回 {e['new_event_recall']:.1%} 才是預警價值。"),
        "conclusion": (f"門檻拉到 0.9，全市每小時仍有 {floor} 個站×時點被觸發（候選量，非去重後的通知量），"
                       f"其中大多是「已經{zw}、持續中」的站，"
                       f"而真正新發生的事件本來就有每小時 {new_per_hour} 件。"
                       "把門檻調高不會把候選量降到值班可處理的範圍；要降量必須改變告警定義"
                       f"（只對新發生、且 500 公尺內沒有替代站、需要{'派車補車' if kind == 'empty' else '派車運出'}的{gw}發告警），"
                       "並以行政區或站群彙總，而不是逐站逐則。"),
        "source": "reports/model_eval.json（六月測試期一次評估）",
    }


# ---------------------------------------------------------------- 流程時效
STAGE_DEFS = [
    ("open_to_ack", "開啟 → 有人看到（ack）", "opened", "acked"),
    ("ack_to_assign", "看到 → 指派負責人", "acked", "assigned_at"),
    ("assign_to_dispatch", "指派 → 轉營運處理", "assigned_at", "dispatched"),
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
    assigned = sum(1 for a in svc if a.get("owner"))        # 指派＝有負責人，和 ack 是兩回事
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
        "funnel": {"opened": opened, "acked": acked, "assigned": assigned, "dispatched": dispatched,
                   "resolved_manual": resolved_manual, "resolved_auto": resolved_auto,
                   "still_open": len(still_open),
                   "acked_not_assigned": max(0, acked - assigned),
                   "unassigned_ratio": round(1 - assigned / opened, 3) if opened else None,
                   "unacked_ratio": round(1 - acked / opened, 3) if opened else None,
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
        "note": ("本場回放的操作時間：事件開啟與自動解除由觀測與預測觸發，確認／指派／轉營運由人在介面按下，"
                 "出車、抵達、完成為依假設推進的模擬狀態。非真實派工系統量測。"),
        "definitions": {
            "ack": "有人在介面上看到並確認這件事，不代表已經指派給誰，也不代表已經有人到現場。",
            "assigned": "事件上有指定的負責人（owner）。未指派比例以此計算，不用 ack 代替。",
            "auto_resolved": "站點恢復有車，條件消失而自動關閉。不代表有人處理過。",
            "no_patrol_inference": "本系統沒有巡查或人員定位紀錄，任務清單裡沒有這一站，只代表排程沒有涵蓋，"
                                   "不能據此推論「沒有人去過現場」。",
        },
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
