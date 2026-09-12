"""
weather.py — 真實天氣資料（Open-Meteo，無需金鑰）。
- 回放期間（2026-01..06）：archive API 的逐時實測重分析（雨量、氣溫、風速）。啟動時抓一次快取。
- 今日即時：forecast API 的 current + 未來數小時降雨機率。
不使用模擬開關；沒有資料就明說沒有。
"""
import os, json, time, threading
import pandas as pd, numpy as np
import sys
sys.path.insert(0, os.path.dirname(__file__))
from planner import http_get_json

LAT, LON = 25.0262, 121.4723          # 板橋（新北市中心區）代表點
CACHE = os.path.join(os.path.dirname(__file__), "..", "data", "processed", "weather_hourly.json")
_hist = None; _lock = threading.Lock()
_live = {"ts": 0, "data": None}

WMO = {0: "晴", 1: "大致晴", 2: "多雲", 3: "陰", 45: "霧", 48: "霧淞", 51: "毛毛雨", 53: "毛毛雨", 55: "毛毛雨",
       61: "小雨", 63: "中雨", 65: "大雨", 80: "陣雨", 81: "陣雨", 82: "強陣雨", 95: "雷雨", 96: "雷雨", 99: "雷雨"}

def _load_history():
    global _hist
    with _lock:
        if _hist is not None: return _hist
        if os.path.exists(CACHE):
            try:
                _hist = json.load(open(CACHE)); return _hist
            except Exception: pass
        url = (f"https://archive-api.open-meteo.com/v1/archive?latitude={LAT}&longitude={LON}"
               "&start_date=2026-01-01&end_date=2026-06-30&hourly=precipitation,temperature_2m,wind_speed_10m,weather_code&timezone=Asia%2FTaipei")
        js = http_get_json(url, timeout=40)
        if not js or "hourly" not in js:
            _hist = {"available": False, "error": "archive API 無回應"}; return _hist
        h = js["hourly"]
        _hist = {"available": True, "source": "Open-Meteo ERA5 重分析逐時實測（archive-api.open-meteo.com）",
                 "point": {"lat": LAT, "lon": LON, "note": "板橋代表點，單點資料不代表全市各區"},
                 "time": h["time"], "precip": h["precipitation"], "temp": h["temperature_2m"],
                 "wind": h["wind_speed_10m"], "code": h["weather_code"]}
        try: json.dump(_hist, open(CACHE, "w"))
        except Exception: pass
        return _hist

def at(ts):
    """回放時點的天氣。回傳 {rain_mm, temp, wind, code, desc, rain_next_3h, is_raining, source}。"""
    h = _load_history()
    if not h.get("available"): return {"available": False, "source": h.get("error", "無資料")}
    key = pd.Timestamp(ts).floor("h").strftime("%Y-%m-%dT%H:00")
    try: i = h["time"].index(key)
    except ValueError: return {"available": False, "source": "回放時點不在天氣資料範圍"}
    nxt = [h["precip"][j] for j in range(i, min(i + 3, len(h["precip"]))) if h["precip"][j] is not None]
    rain = h["precip"][i] or 0.0
    return {"available": True, "ts": key.replace("T", " "), "rain_mm": round(float(rain), 1),
            "temp": h["temp"][i], "wind": h["wind"][i], "code": int(h["code"][i] or 0),
            "desc": WMO.get(int(h["code"][i] or 0), "—"),
            "rain_next_3h": round(float(sum(nxt)), 1), "is_raining": rain >= 0.1,
            "will_rain_3h": sum(nxt) >= 0.5,
            "source": h["source"], "point_note": h["point"]["note"]}

def live():
    """今日即時（給政府端／營運端看現在真實天氣）。"""
    if time.time() - _live["ts"] < 600 and _live["data"]: return _live["data"]
    js = http_get_json(f"https://api.open-meteo.com/v1/forecast?latitude={LAT}&longitude={LON}"
                       "&current=precipitation,rain,temperature_2m,weather_code,wind_speed_10m"
                       "&hourly=precipitation_probability,precipitation&forecast_hours=6&timezone=Asia%2FTaipei", timeout=20)
    if not js or "current" not in js: return {"available": False, "source": "forecast API 無回應"}
    c = js["current"]; hh = js.get("hourly", {})
    d = {"available": True, "ts": c["time"].replace("T", " "), "rain_mm": c["precipitation"], "temp": c["temperature_2m"],
         "wind": c.get("wind_speed_10m"), "code": int(c["weather_code"]), "desc": WMO.get(int(c["weather_code"]), "—"),
         "is_raining": c["precipitation"] >= 0.1,
         "next_hours": [{"t": t[11:16], "p": p, "mm": m} for t, p, m in zip(hh.get("time", []), hh.get("precipitation_probability", []), hh.get("precipitation", []))][:6],
         "source": "Open-Meteo 預報 API（api.open-meteo.com）"}
    _live.update({"ts": time.time(), "data": d}); return d

def ride_factor(w):
    """雨天騎乘時間加成與風險加成（假設值，會在介面標示）。"""
    if not w.get("available") or not w.get("is_raining"): return {"time": 1.0, "risk": 1.0, "label": None}
    mm = w.get("rain_mm", 0)
    if mm >= 4: return {"time": 1.30, "risk": 1.25, "label": "大雨"}
    if mm >= 1: return {"time": 1.18, "risk": 1.15, "label": "有雨"}
    return {"time": 1.08, "risk": 1.05, "label": "毛毛雨"}
