"""live.py — 新北市官方 YouBike2.0 即時站況接入層（v2 新增）。

資料來源：新北市政府資料開放平臺 資料集 010e5b15-3823-4b20-b401-b1cf000550c5
          https://data.ntpc.gov.tw/api/datasets/.../json  每 5 分鐘更新、政府資料開放授權 1.0

真實計算：站況本身、容量矛盾偵測、全市彙總、與歷史站鍵的對應。
明示口徑：
  * 「容量矛盾」＝ tot_quantity != sbi_quantity + bemp。差額是名目上存在、
    但官方 API 既不計入可借、也不計入可還的車柱。**不判定根因。**
  * act != 1 代表官方標示暫停營運，與「無車可借」是兩件事。
  * 本模組不產生任何預測；風險估計在 app/v2api.py 的 Risk 查表，並明示為歷史同時段分布。
  * 合成負數 sid 代表 2026-06 之後才新增、歷史資料沒有的站，詳情頁會明示無歷史可比。
"""
import json
import re
import threading
import time
import urllib.request
from datetime import datetime, timezone, timedelta

import numpy as np

NTPC_URL = ("https://data.ntpc.gov.tw/api/datasets/"
            "010e5b15-3823-4b20-b401-b1cf000550c5/json?size=3000")
TPE_URL = "https://tcgbusfs.blob.core.windows.net/dotapp/youbike/v2/youbike_immediate.json"
TZ = timezone(timedelta(hours=8))
POLL_SEC = 120          # 官方每 5 分鐘更新，我們每 2 分鐘取一次
TIMEOUT = 25


def _norm(name: str) -> str:
    return re.sub(r"^YouBike2?\.?0?_", "", (name or "")).strip()


def _fetch(url: str):
    req = urllib.request.Request(url, headers={"User-Agent": "ntpc-youbike-hackathon/2.0"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return json.loads(r.read().decode("utf-8"))


class LiveStore:
    """持有最近一次成功抓取的即時站況；抓取失敗不清空，改標記 stale。"""

    def __init__(self, stations_df):
        self.st = stations_df
        self._lock = threading.Lock()
        self._snap = None          # {sid: rec}
        self._stats = None
        self._fetched_at = None
        self._error = None
        self._ok_count = 0
        self._err_count = 0
        self._name2sid = {r["name"]: int(r["sid"]) for _, r in stations_df.iterrows()}
        self._ll = np.array([[float(r.lat), float(r.lon)] for _, r in stations_df.iterrows()])
        self._sids = stations_df["sid"].to_numpy()
        self._thread = None
        self._new_ids = {}       # 官方站號 → 合成負數 sid（六月後新增、歷史資料沒有的站）

    # ---------------------------------------------------------- 站鍵對應
    def _match(self, rec):
        """先用站名，再用 60 公尺內最近站補配（歷史 CSV 有罕用字被寫成 ? 的情形）。"""
        nm = _norm(rec.get("sna", ""))
        sid = self._name2sid.get(nm)
        if sid is not None:
            return sid, "name"
        try:
            lat, lon = float(rec["lat"]), float(rec["lng"])
        except (KeyError, TypeError, ValueError):
            return None, "none"
        d = np.hypot((self._ll[:, 0] - lat) * 111000.0, (self._ll[:, 1] - lon) * 101000.0)
        i = int(d.argmin())
        if d[i] < 60.0:
            return int(self._sids[i]), "geo"
        # 歷史資料裡沒有這一站（2026 年 6 月之後才設的）。給一個穩定的合成負數 sid，
        # 讓它照樣進地圖與調度看板，但站點詳情會明示「無歷史同時段資料」。
        sno = str(rec.get("sno") or "").strip()
        if not sno:
            return None, "none"
        if sno not in self._new_ids:
            self._new_ids[sno] = -(len(self._new_ids) + 1)
        return self._new_ids[sno], "new"

    # ---------------------------------------------------------- 抓取
    def refresh(self):
        raw = _fetch(NTPC_URL)
        snap, unmatched = {}, 0
        for rec in raw:
            try:
                tot = int(rec["tot_quantity"]); rent = int(rec["sbi_quantity"]); ret = int(rec["bemp"])
            except (KeyError, TypeError, ValueError):
                continue
            sid, how = self._match(rec)
            if sid is None:
                unmatched += 1
                continue
            mday = rec.get("mday", "")
            try:      # 格式 20260913T003403
                ts = datetime.strptime(mday, "%Y%m%dT%H%M%S").replace(tzinfo=TZ)
            except ValueError:
                ts = None
            gap = tot - rent - ret
            snap[sid] = {
                "sid": sid, "name": _norm(rec.get("sna", "")), "district": rec.get("sarea", ""),
                "address": rec.get("ar", ""), "lat": float(rec["lat"]), "lon": float(rec["lng"]),
                "capacity": tot, "bikes": rent, "docks": ret,
                "bikes_general": int(rec.get("yb2_quantity") or 0),
                "bikes_electric": int(rec.get("eyb_quantity") or 0),
                "active": rec.get("act") == "1",
                "info_time": ts.isoformat() if ts else None,
                "age_min": round((datetime.now(TZ) - ts).total_seconds() / 60.0, 1) if ts else None,
                "capacity_gap": gap,               # 名目上存在但既不可借也不可還
                "no_bike": rent == 0, "no_dock": ret == 0,
                "both_zero": rent == 0 and ret == 0,
                "match": how,
            }
        stats = self._summarize(snap, unmatched)
        with self._lock:
            self._snap, self._stats = snap, stats
            self._fetched_at = datetime.now(TZ)
            self._error = None
            self._ok_count += 1
        return stats

    def _summarize(self, snap, unmatched):
        v = list(snap.values())
        n = len(v) or 1
        act = [x for x in v if x["active"]]
        gap_st = [x for x in v if x["capacity_gap"] != 0]
        ages = [x["age_min"] for x in v if x["age_min"] is not None]
        by_dist = {}
        for x in v:
            d = by_dist.setdefault(x["district"], {"n": 0, "no_bike": 0, "no_dock": 0,
                                                   "bikes": 0, "capacity": 0, "gap_units": 0})
            d["n"] += 1
            d["no_bike"] += int(x["no_bike"] and x["active"])
            d["no_dock"] += int(x["no_dock"] and x["active"])
            d["bikes"] += x["bikes"]; d["capacity"] += x["capacity"]
            d["gap_units"] += max(0, x["capacity_gap"])
        return {
            "stations": len(v),
            "unmatched": unmatched,
            "new_stations": sum(1 for x in v if x["match"] == "new"),
            "inactive": sum(1 for x in v if not x["active"]),
            "no_bike": sum(1 for x in act if x["no_bike"]),
            "no_dock": sum(1 for x in act if x["no_dock"]),
            "both_zero": sum(1 for x in act if x["both_zero"]),
            "no_bike_pct": round(sum(1 for x in act if x["no_bike"]) / n * 100, 1),
            "no_dock_pct": round(sum(1 for x in act if x["no_dock"]) / n * 100, 1),
            "bikes_total": sum(x["bikes"] for x in v),
            "bikes_electric": sum(x["bikes_electric"] for x in v),
            "capacity_total": sum(x["capacity"] for x in v),
            "docks_total": sum(x["docks"] for x in v),
            "capacity_gap_stations": len(gap_st),
            "capacity_gap_pct": round(len(gap_st) / n * 100, 1),
            "capacity_gap_units": sum(max(0, x["capacity_gap"]) for x in v),
            "data_age_min_median": round(float(np.median(ages)), 1) if ages else None,
            "data_age_min_max": round(float(np.max(ages)), 1) if ages else None,
            "stale_stations": sum(1 for a in ages if a > 30),
            "by_district": by_dist,
        }

    # ---------------------------------------------------------- 背景輪詢
    def _loop(self):
        while True:
            try:
                self.refresh()
            except Exception as e:                      # noqa: BLE001 抓取失敗不能弄掛服務
                with self._lock:
                    self._error = f"{type(e).__name__}: {e}"
                    self._err_count += 1
            time.sleep(POLL_SEC)

    def start(self):
        if self._thread is not None:
            return
        try:
            self.refresh()
        except Exception as e:                          # noqa: BLE001
            self._error = f"{type(e).__name__}: {e}"
            self._err_count += 1
        self._thread = threading.Thread(target=self._loop, daemon=True, name="live-poll")
        self._thread.start()

    # ---------------------------------------------------------- 讀取
    def status(self):
        with self._lock:
            age = None
            if self._fetched_at:
                age = round((datetime.now(TZ) - self._fetched_at).total_seconds(), 1)
            return {
                "source": "新北市政府資料開放平臺 YouBike2.0 即時站況",
                "url": NTPC_URL.split("?")[0],
                "license": "政府資料開放授權條款 1.0",
                "official_update": "每 5 分鐘",
                "poll_sec": POLL_SEC,
                "fetched_at": self._fetched_at.isoformat() if self._fetched_at else None,
                "age_sec": age,
                "stale": (age is None) or (age > POLL_SEC * 3),
                "ok": self._ok_count, "errors": self._err_count, "last_error": self._error,
                "available": self._snap is not None,
            }

    def snapshot(self):
        with self._lock:
            return self._snap or {}

    def stats(self):
        with self._lock:
            return self._stats or {}

    def station(self, sid):
        with self._lock:
            return (self._snap or {}).get(int(sid))

    def worst(self, kind="no_dock", limit=20, district=None):
        """目前最嚴重的站。kind: no_dock（還不了）/ no_bike（借不到）/ capacity_gap。"""
        v = [x for x in self.snapshot().values() if x["active"]]
        if district:
            v = [x for x in v if x["district"] == district]
        if kind == "capacity_gap":
            v = [x for x in v if x["capacity_gap"] > 0]
            v.sort(key=lambda x: -x["capacity_gap"])
        elif kind == "no_bike":
            v = [x for x in v if x["bikes"] <= 2]
            v.sort(key=lambda x: (x["bikes"], -x["capacity"]))
        else:
            v = [x for x in v if x["docks"] <= 2]
            v.sort(key=lambda x: (x["docks"], -x["capacity"]))
        return v[:limit]
