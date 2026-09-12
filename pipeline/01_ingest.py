"""
01_ingest.py  —  將 12 份原始 CSV 正規化為 parquet。
原則：原始檔不修改；保留原始時間戳；容量矛盾列只隔離(valid=False)不修改；
      缺行政區只用全期唯一同名站補連結；缺座標用同站眾數補，不憑空造站。
"""
import json, os, sys, time
import numpy as np, pandas as pd

RAW_DIR = "/Users/chenhongfei/Desktop/資料集"
OUT = "/Users/chenhongfei/CC/ntpc-youbike/data/processed"
MANIFEST = "/Users/chenhongfei/Desktop/Claude_YouBike專案交接包/原始資料清單.json"
os.makedirs(OUT, exist_ok=True)

files = json.load(open(MANIFEST))["files"]
COLS = ["日期","城市","行政區","場站名稱","總車柱數","可借車數","可還位數","經度","緯度"]

def enc_for(name):
    return "utf-8-sig" if ("一月" in name or "二月" in name or "六月" in name) else "cp950"

frames, log = [], []
t0 = time.time()
for i, f in enumerate(files):
    p = os.path.join(RAW_DIR, f["name"])
    df = pd.read_csv(p, encoding=enc_for(f["name"]), dtype=str, usecols=COLS)
    n_raw = len(df)
    df["ts"] = pd.to_datetime(df["日期"], errors="coerce")
    bad_ts = int(df["ts"].isna().sum())
    df = df.dropna(subset=["ts"])
    for c in ["總車柱數","可借車數","可還位數"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df["lon"] = pd.to_numeric(df["經度"], errors="coerce")
    df["lat"] = pd.to_numeric(df["緯度"], errors="coerce")
    df["district"] = df["行政區"].str.strip()
    df["name"] = df["場站名稱"].str.strip()
    out = pd.DataFrame({
        "ts": df["ts"].values,
        "district": df["district"].values,
        "name": df["name"].values,
        "cap": df["總車柱數"].fillna(-1).astype("int16").values,
        "bikes": df["可借車數"].fillna(-1).astype("int16").values,
        "spaces": df["可還位數"].fillna(-1).astype("int16").values,
        "lon": df["lon"].astype("float32").values,
        "lat": df["lat"].astype("float32").values,
        "src": np.int8(i),
    })
    frames.append(out)
    log.append({"file": f["name"], "encoding": enc_for(f["name"]), "rows": n_raw, "bad_ts": bad_ts,
                "null_district": int(out["district"].isna().sum()), "null_coord": int(out["lon"].isna().sum())})
    print(f"[{i+1}/12] {f['name']} rows={n_raw:,} null_district={log[-1]['null_district']} ({time.time()-t0:.0f}s)", flush=True)
    del df

obs = pd.concat(frames, ignore_index=True); del frames
print("total rows", len(obs))

# --- 站點鍵：行政區＋站名；缺行政區僅用全期唯一同名站補 ---
known = obs.dropna(subset=["district"])[["name","district"]].drop_duplicates()
uniq = known.groupby("name")["district"].nunique()
uniq_names = set(uniq[uniq == 1].index)
name2dist = known[known["name"].isin(uniq_names)].set_index("name")["district"].to_dict()
miss = obs["district"].isna()
fillable = miss & obs["name"].isin(list(name2dist))
obs.loc[fillable, "district"] = obs.loc[fillable, "name"].map(name2dist)
unfilled = int(obs["district"].isna().sum())
print("district filled:", int(fillable.sum()), "still missing:", unfilled)
obs = obs.dropna(subset=["district"])  # 無法連結者不造站

obs["key"] = obs["district"] + "|" + obs["name"]
keys = pd.Index(sorted(obs["key"].unique()))
obs["sid"] = keys.get_indexer(obs["key"]).astype("int32")
print("stations:", len(keys))

# --- 半小時分箱（floor）；保留 ts ---
obs["bin"] = obs["ts"].dt.floor("30min")
# 同站同分箱重複：保留最後一筆
obs = obs.sort_values(["sid","bin","ts"]).drop_duplicates(["sid","bin"], keep="last")
print("after dedupe:", len(obs))

# --- 容量一致性 ---
obs["valid"] = (obs["cap"] > 0) & (obs["bikes"] >= 0) & (obs["spaces"] >= 0) & (obs["bikes"] + obs["spaces"] <= obs["cap"])
print("valid:", int(obs["valid"].sum()), "quarantined:", int((~obs["valid"]).sum()))

# --- 站點主檔：座標眾數、容量眾數 ---
def mode_or_nan(s):
    s = s.dropna(); 
    return s.mode().iloc[0] if len(s) else np.nan
st = obs.groupby("sid").agg(
    district=("district","first"), name=("name","first"),
    lon=("lon", mode_or_nan), lat=("lat", mode_or_nan),
    cap_mode=("cap", mode_or_nan), cap_min=("cap","min"), cap_max=("cap","max"),
    n_obs=("bin","size"), n_valid=("valid","sum"),
    first_bin=("bin","min"), last_bin=("bin","max"),
).reset_index()
st["cap_changed"] = st["cap_min"] != st["cap_max"]
print("stations w/o coord:", int(st["lon"].isna().sum()), " cap changed:", int(st["cap_changed"].sum()))

# 缺座標的列補站眾數
obs = obs.drop(columns=["lon","lat"]).merge(st[["sid","lon","lat"]], on="sid", how="left")

obs[["sid","ts","bin","cap","bikes","spaces","valid","src"]].to_parquet(f"{OUT}/obs.parquet", index=False)
st.to_parquet(f"{OUT}/stations.parquet", index=False)

# --- 鄰站表（直線距離 ≤ 800m；直線只作候選篩選，不代表步行路程）---
R = 6371000.0
lat = np.radians(st["lat"].values.astype(float)); lon = np.radians(st["lon"].values.astype(float))
dlat = lat[:,None]-lat[None,:]; dlon = lon[:,None]-lon[None,:]
a = np.sin(dlat/2)**2 + np.cos(lat[:,None])*np.cos(lat[None,:])*np.sin(dlon/2)**2
d = 2*R*np.arcsin(np.sqrt(a))
ii, jj = np.where((d <= 800) & (d > 0))
nb = pd.DataFrame({"sid": st["sid"].values[ii], "nsid": st["sid"].values[jj], "dist_m": d[ii,jj].astype("float32")})
nb.to_parquet(f"{OUT}/neighbors_800m.parquet", index=False)
print("neighbor pairs ≤800m:", len(nb))

json.dump({"files": log, "rows_total": int(len(obs)), "stations": int(len(keys)), "unfilled_district_rows": unfilled,
           "valid_rows": int(obs["valid"].sum()), "quarantined_rows": int((~obs["valid"]).sum()),
           "seconds": round(time.time()-t0)}, open(f"{OUT}/ingest_log.json","w"), ensure_ascii=False, indent=1)
print("done in", round(time.time()-t0), "s")
