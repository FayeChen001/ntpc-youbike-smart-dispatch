"""liveforecast.py — 把訓練好的模型跑在官方即時資料上（v2 新增）。

做法：用 app/live.py 累積的即時歷史重建一份 30 分鐘分箱矩陣，
再以**訓練期產出的**站點輪廓（prof）、站統計（st）、鄰站關係（A）、行政區編碼
組成 pipeline/features.py 的 FeatureContext，直接呼叫同一支 make_X。
特徵定義與訓練時完全一致，不是另外寫一套。

誠實邊界（畫面上要跟著標）：
  * 落後特徵（30/60/120 分鐘前）來自我們自己累積的即時歷史。服務剛啟動時還沒累積夠，
    這些特徵會是 NaN——HistGradientBoosting 原生支援缺值，模型仍可推論，但**準確度會下降**。
    `coverage` 欄位如實回報四個落後分箱各有多少站有值。
  * 站點輪廓與站統計是 2026-01~06 訓練期的歷史，不是今天的。若營運型態已改變，會有偏差。
  * 2026-06 之後新增的站沒有輪廓，**不做模型預測**，只給即時現況。
  * 模型卡的離線指標（reports/model_eval.md）是在六月測試集上量的，
    **不等於即時推論的準確度**，兩者不可混為一談。
"""
import os
import sys
import threading
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "pipeline"))
from features import HORIZONS, FeatureContext          # noqa: E402

TZ = timezone(timedelta(hours=8))
MIN_BINS = 2        # 至少要有這麼多分箱才開始推論（否則連 lag1 都沒有）


class LiveForecaster:
    def __init__(self, predictor, live_store):
        self.pred = predictor
        self.live = live_store
        self.ns = len(predictor.st)
        self._lock = threading.Lock()
        self._cache_key = None
        self._cache = None

    # ------------------------------------------------------------------
    def status(self):
        bins = self.live.history_bins()
        return {
            "available": len(bins) >= MIN_BINS and bool(self.pred.model_horizons),
            "history_bins": len(bins),
            "history_hours": round(len(bins) * 0.5, 1),
            "needed_for_full_lags": 5,      # 現在 + lag1/2/4 共需 5 個分箱
            "model_origin": self.pred.model_origin,
            "horizons": self.pred.model_horizons,
            "first_bin": str(bins[0]) if bins else None,
            "last_bin": str(bins[-1]) if bins else None,
            "note": ("落後特徵來自本服務自行累積的即時歷史；累積不足時該特徵為缺值，"
                     "模型仍可推論但準確度下降。輪廓特徵為 2026-01~06 訓練期歷史。"),
        }

    # ------------------------------------------------------------------
    def forecast(self):
        """回傳全站的即時模型預測。以最後一個分箱為 key 做快取。"""
        bins = self.live.history_bins()
        if len(bins) < MIN_BINS or not self.pred.model_horizons:
            return None
        key = (bins[-1], len(bins))
        with self._lock:
            if self._cache_key == key:
                return self._cache

        full, B, S, C = self.live.history_matrices(self.ns)
        if B is None or len(full) < MIN_BINS:
            return None

        c0 = self.pred.ctx
        # 重建 context：矩陣換成即時的，輪廓／站統計／鄰站／行政區沿用訓練期產物
        ctx = FeatureContext(full, B, S, C, c0.prof, c0.st, c0.A, c0.district_code)
        t_idx = len(full) - 1
        s = np.arange(self.ns)
        t = np.full(self.ns, t_idx)

        out = pd.DataFrame({
            "sid": s,
            "name": self.pred.st["name"].values,
            "district": self.pred.st["district"].values,
            "bikes_now": B[t_idx], "docks_now": S[t_idx], "cap_now": C[t_idx],
        })
        coverage = {}
        for lag in (1, 2, 4):
            j = t_idx - lag
            coverage[f"lag{lag}"] = int(np.isfinite(B[j]).sum()) if j >= 0 else 0

        for hm, h in HORIZONS.items():
            if hm not in self.pred.model_horizons:
                continue
            X = ctx.make_X(t, s, h)
            Xv = X.values.astype(np.float32)
            cap = X["cap"].values
            pb = np.clip(np.nan_to_num(X["bikes"].values)
                         + self.pred.models[(hm, "reg_bikes")].predict(Xv), 0, cap)
            ps = np.clip(np.nan_to_num(X["spaces"].values)
                         + self.pred.models[(hm, "reg_spaces")].predict(Xv), 0, cap)
            pe = self.pred.models[(hm, "cls_empty")].predict_proba(Xv)[:, 1]
            pf = self.pred.models[(hm, "cls_full")].predict_proba(Xv)[:, 1]
            out[f"bikes_{hm}"] = np.round(pb, 1)
            out[f"docks_{hm}"] = np.round(ps, 1)
            out[f"p_empty_{hm}"] = np.round(pe, 3)
            out[f"p_full_{hm}"] = np.round(pf, 3)

        # 沒有即時觀測的站（含 6 月後新增、官方未回報的）不給預測
        out = out[np.isfinite(B[t_idx])].reset_index(drop=True)

        res = {"at_bin": str(full[t_idx]), "stations": out,
               "coverage": coverage, "n_stations": int(len(out)),
               "model_origin": self.pred.model_origin,
               "horizons": self.pred.model_horizons}
        with self._lock:
            self._cache_key, self._cache = key, res
        return res

    # ------------------------------------------------------------------
    def station(self, sid):
        f = self.forecast()
        if not f:
            return None
        row = f["stations"][f["stations"].sid == int(sid)]
        if row.empty:
            return None
        r = row.iloc[0]
        horizon = []
        for hm in f["horizons"]:
            horizon.append({
                "ahead_min": int(hm),
                "at": (datetime.now(TZ) + timedelta(minutes=int(hm))).strftime("%H:%M"),
                "bikes": float(r[f"bikes_{hm}"]), "docks": float(r[f"docks_{hm}"]),
                "p_empty": float(r[f"p_empty_{hm}"]), "p_full": float(r[f"p_full_{hm}"]),
            })
        return {"at_bin": f["at_bin"], "bikes_now": float(r["bikes_now"]),
                "docks_now": float(r["docks_now"]), "horizon": horizon,
                "model_origin": f["model_origin"], "coverage": f["coverage"]}

    # ------------------------------------------------------------------
    def prob_map(self, horizon=120):
        """{sid: {"empty": 機率, "full": 機率}}，整批一次算好。

        原本呼叫端是在逐站迴圈裡呼叫 risk_ranking()，每次都重新過濾並排序
        整張表再建幾百個 dict——決策台因此要跑 4.6 秒。查表建一次就好。
        """
        f = self.forecast()
        if not f or horizon not in f["horizons"]:
            return {}
        d = f["stations"]
        # itertuples() 給的是 namedtuple，只能用 getattr，不能用字串索引
        ec, fc_ = f"p_empty_{horizon}", f"p_full_{horizon}"
        return {int(r.sid): {"empty": float(getattr(r, ec)),
                             "full": float(getattr(r, fc_))}
                for r in d.itertuples()}

    # ------------------------------------------------------------------
    def risk_ranking(self, kind="full", horizon=120, limit=20, min_p=0.3,
                     exclude_offline=True):
        """未來最可能出事的站。kind: full（無位可還）/ empty（無車可借）。

        預設排除「此刻借還同時為 0」的站。那些是整站無服務（設備離線或未投車），
        模型當然會預測它們繼續是空的——機率 100%、而且永遠排在最前面，
        把真正需要處理的站擠掉。派車過去也沒有柱位可用，不是調度訊號。
        """
        f = self.forecast()
        if not f or horizon not in f["horizons"]:
            return []
        col = f"p_{'full' if kind == 'full' else 'empty'}_{horizon}"
        d = f["stations"]
        if exclude_offline:
            d = d[~((d["bikes_now"] == 0) & (d["docks_now"] == 0))]
        d = d[d[col] >= min_p].sort_values(col, ascending=False).head(limit)
        now_col = "docks_now" if kind == "full" else "bikes_now"
        return [{"sid": int(r.sid), "name": r["name"], "district": r.district,
                 "now": float(r[now_col]), "p": round(float(r[col]) * 100, 1),
                 "pred_bikes": float(r[f"bikes_{horizon}"]),
                 "pred_docks": float(r[f"docks_{horizon}"])}
                for _, r in d.iterrows()]
