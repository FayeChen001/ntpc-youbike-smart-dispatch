"""
awsloc.py — Amazon Location Service 整合。
- geo-routes：步行／騎乘／調度車路線，含繁體中文逐段指示（Languages=zh-TW）。
- geo-places：地址地理編碼。
- geo-maps：底圖圖磚與靜態地圖（由伺服器代簽，前端不需憑證）。
沒有憑證或呼叫失敗時回傳 None，由呼叫端退回本地估算並標示來源。
"""
import os, time, threading, json
from concurrent.futures import ThreadPoolExecutor

REGION = os.environ.get("AWS_REGION", "us-west-2")
PROFILE = os.environ.get("AWS_PROFILE", "hackathon")
MODE = {"walk": "Pedestrian", "ride": "Scooter", "drive": "Car"}   # Scooter 作為單車近似（無 Bicycle 模式，屬明示假設）

_cli = {}; _lock = threading.Lock(); _cache = {}; _pool = ThreadPoolExecutor(max_workers=6)
STATUS = {"routes": 0, "places": 0, "tiles": 0, "static": 0, "errors": 0, "last_error": None, "enabled": True}

def session():
    """本機用具名 profile，EC2 上走執行個體角色。空字串的 AWS_PROFILE 會讓 botocore 找不到設定檔，先清掉。"""
    import boto3
    prof = (os.environ.get("AWS_PROFILE") or "").strip()
    if not prof:
        os.environ.pop("AWS_PROFILE", None); os.environ.pop("AWS_DEFAULT_PROFILE", None)
        return boto3.Session()
    return boto3.Session(profile_name=prof)

def client(svc):
    with _lock:
        if svc not in _cli:
            _cli[svc] = session().client(svc, region_name=REGION)
        return _cli[svc]

def route(a, b, mode="walk"):
    """a,b=(lat,lon)。回傳 dist_m / geometry[[lat,lon]] / steps / aws_duration_s / source。失敗回 None。"""
    key = (round(a[0], 5), round(a[1], 5), round(b[0], 5), round(b[1], 5), mode)
    with _lock:
        if key in _cache: return _cache[key]
    if not STATUS["enabled"]: return None
    try:
        r = client("geo-routes").calculate_routes(
            Origin=[b_ for b_ in (a[1], a[0])], Destination=[b[1], b[0]],
            TravelMode=MODE.get(mode, "Pedestrian"), LegGeometryFormat="Simple",
            LegAdditionalFeatures=["TravelStepInstructions", "Summary"],
            InstructionsMeasurementSystem="Metric", Languages=["zh-TW"])
        leg = r["Routes"][0]["Legs"][0]
        det = leg.get("PedestrianLegDetails") or leg.get("VehicleLegDetails") or {}
        ov = det.get("Summary", {}).get("Overview", {})
        steps = []
        for st in det.get("TravelSteps", []):
            txt = (st.get("Instruction") or "").split(",")[0].strip()
            steps.append({"text": txt or "直行", "dist_m": int(st.get("Distance", 0)),
                          "turn": (st.get("TurnStepDetails") or {}).get("SteeringDirection", ""), "type": st.get("Type", "")})
        out = {"dist_m": int(ov.get("Distance", 0)), "aws_duration_s": int(ov.get("Duration", 0)),
               "geometry": [[p[1], p[0]] for p in leg.get("Geometry", {}).get("LineString", [])],
               "steps": steps, "source": f"Amazon Location Service Routes（{MODE.get(mode)} 模式，繁體中文指示）"}
        if not out["geometry"] or out["dist_m"] <= 0: return None
        STATUS["routes"] += 1
        with _lock: _cache[key] = out
        return out
    except Exception as e:
        STATUS["errors"] += 1; STATUS["last_error"] = f"routes: {type(e).__name__}: {str(e)[:160]}"
        return None

def route_multi(points, mode="drive"):
    """多點路線（調度車趟次）。points=[(lat,lon),...]"""
    if len(points) < 2: return None
    try:
        r = client("geo-routes").calculate_routes(
            Origin=[points[0][1], points[0][0]], Destination=[points[-1][1], points[-1][0]],
            Waypoints=[{"Position": [p[1], p[0]]} for p in points[1:-1]][:20],
            TravelMode=MODE.get(mode, "Car"), LegGeometryFormat="Simple",
            LegAdditionalFeatures=["Summary"], InstructionsMeasurementSystem="Metric", Languages=["zh-TW"])
        rt = r["Routes"][0]; geom = []; dist = 0; dur = 0
        for leg in rt["Legs"]:
            det = leg.get("VehicleLegDetails") or leg.get("PedestrianLegDetails") or {}
            ov = det.get("Summary", {}).get("Overview", {}); dist += ov.get("Distance", 0); dur += ov.get("Duration", 0)
            geom += [[p[1], p[0]] for p in leg.get("Geometry", {}).get("LineString", [])]
        STATUS["routes"] += 1
        return {"dist_m": int(dist), "aws_duration_s": int(dur), "geometry": geom,
                "source": "Amazon Location Service Routes（Car 模式多點趟次）"}
    except Exception as e:
        STATUS["errors"] += 1; STATUS["last_error"] = f"route_multi: {type(e).__name__}: {str(e)[:160]}"
        return None

# 新北市中心附近，用來把地理編碼的結果拉回本地
NTPC_BIAS = [121.4628, 25.0128]          # [lon, lat]，Amazon Location 的順序
NTPC_BBOX = [121.20, 24.60, 122.06, 25.35]   # 涵蓋新北與台北


def geocode(text, bias=True):
    """地址／地標地理編碼。

    一定要限制國別並給定位置偏好，否則「板橋車站」會回日本東京都板橋區——
    Amazon Location 是全球資料，不加限制就會這樣。
    """
    try:
        kw = dict(QueryText=text, MaxResults=5, Language="zh-TW")
        if bias:
            kw["Filter"] = {"IncludeCountries": ["TWN"]}
            kw["BiasPosition"] = NTPC_BIAS
        r = client("geo-places").geocode(**kw)
        STATUS["places"] += 1
        out = []
        for x in r.get("ResultItems", []):
            p = x.get("Position")
            if not p:
                continue
            lon, lat = p[0], p[1]
            if bias and not (NTPC_BBOX[0] <= lon <= NTPC_BBOX[2]
                             and NTPC_BBOX[1] <= lat <= NTPC_BBOX[3]):
                continue                  # 落在雙北範圍外的直接丟掉
            out.append({"title": x.get("Title"), "lat": lat, "lon": lon})
        return out[:3]
    except Exception as e:
        STATUS["errors"] += 1; STATUS["last_error"] = f"places: {type(e).__name__}: {str(e)[:160]}"; return []

def tile(tileset, z, x, y):
    try:
        r = client("geo-maps").get_tile(Tileset=tileset, Z=str(z), X=str(x), Y=str(y))
        STATUS["tiles"] += 1; return r["Blob"].read()
    except Exception as e:
        STATUS["errors"] += 1; STATUS["last_error"] = f"tile: {type(e).__name__}: {str(e)[:120]}"; return None

def static_map(bbox, w=640, h=400, style="Standard"):
    try:
        r = client("geo-maps").get_static_map(BoundingBox=bbox, FileName="map", Width=w, Height=h, Style=style)
        STATUS["static"] += 1; return r["Blob"].read()
    except Exception as e:
        STATUS["errors"] += 1; STATUS["last_error"] = f"static: {type(e).__name__}: {str(e)[:120]}"; return None

def route_async(a, b, mode): return _pool.submit(route, a, b, mode)
