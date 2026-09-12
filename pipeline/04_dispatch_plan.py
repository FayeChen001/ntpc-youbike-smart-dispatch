"""
04_dispatch_plan.py — 從資料導出「怎麼調度才能讓使用率最大化」的具體方案。
界線：觀測流量為下界（同區間一借一還互相抵銷；零車時的需求被右設限，看不到）。
"""
import os, sys, json, time
import numpy as np, pandas as pd
sys.path.insert(0, os.path.dirname(__file__))
from features import build_matrices, calendar

ROOT="/Users/chenhongfei/CC/ntpc-youbike"; P=f"{ROOT}/data/processed"; R=f"{ROOT}/reports"
t0=time.time(); out={}
obs=pd.read_parquet(f"{P}/obs.parquet"); st=pd.read_parquet(f"{P}/stations.parquet").sort_values("sid").reset_index(drop=True)
nb=pd.read_parquet(f"{P}/neighbors_800m.parquet"); ns=len(st)
bins,B,S,C=build_matrices(obs,ns,"2026-01-01","2026-06-30 23:30"); del obs
slot,wd,daytype=calendar(bins)
D=np.diff(B,axis=0); vp=~np.isnan(B[:-1])&~np.isnan(B[1:]); D=np.where(vp,D,np.nan)
OUTF=np.where(D<0,-D,0); INF=np.where(D>0,D,0)
nobs=np.sum(~np.isnan(B),axis=0); days=np.maximum(nobs/48.0,1)
cap=np.nan_to_num(np.nanmedian(C,axis=0),nan=0); cap=np.where(cap<=0,st["cap_mode"].values,cap)
alive=(np.nansum((B==0)&(S==0),axis=0)/np.maximum(nobs,1))<0.5

# ---- 全市真實周轉（修正重複計數：一趟旅次＝一次外流＋一次還入）----
trips_day=float(np.nansum(OUTF))/float(np.nanmean(days))
fleet=float(np.nansum(np.nanmean(B,axis=0)))
out["fleet"]={"docks":int(cap.sum()),"mean_bikes_in_docks":round(fleet),
  "observed_trips_per_day_lower_bound":round(trips_day),
  "trips_per_bike_per_day":round(trips_day/max(fleet,1),2),
  "trips_per_dock_per_day":round(trips_day/max(cap.sum(),1),2),
  "note":"以觀測到的庫存下降量估算旅次，為下界；30 分鐘內一借一還會抵銷，零車期間的需求看不到。"}

# ---- 尖峰視窗 ----
WIN={"早尖峰 07:00-09:30":(14,19),"晚尖峰 17:00-19:30":(34,39)}
plan={}
for wname,(a,b) in WIN.items():
    m=(slot[:-1]>=a)&(slot[:-1]<=b)&(daytype[:-1]==0)
    ndays=len(np.unique(bins[:-1][m].date))
    need=np.nansum(OUTF[m],axis=0)/max(ndays,1)              # 該視窗每日平均外流（下界）
    start_idx=np.where((slot==a)&(daytype==0))[0]
    stock=np.nanmean(B[start_idx],axis=0)                     # 視窗開始時的平均庫存
    zero_share=np.nansum(B[:-1][m]==0,axis=0)/max(m.sum(),1)  # 視窗內零車比例
    deficit=np.maximum(0,need*1.15-stock)                     # 目標庫存＝視窗需求×1.15
    deficit=np.where(alive&(need>=3),deficit,0)
    surplus=np.maximum(0,stock-np.maximum(2,need*1.15))
    surplus=np.where(alive&(need<stock*0.5),surplus,0)        # 只從「需求遠低於庫存」的站抽
    df=pd.DataFrame({"sid":st.sid,"district":st.district,"name":st.name,"lat":st.lat,"lon":st.lon,"cap":cap,
                     "need":need.round(1),"stock":stock.round(1),"deficit":deficit.round(1),
                     "surplus":surplus.round(1),"zero_share":(100*zero_share).round(1)})
    plan[wname]={"window_days":int(ndays),
      "stations_with_deficit":int((df.deficit>=2).sum()),
      "total_deficit_bikes":round(float(df.deficit.sum())),
      "total_surplus_bikes":round(float(df.surplus.sum())),
      "top_deficit":df.nlargest(20,"deficit")[["sid","district","name","cap","stock","need","deficit","zero_share"]].to_dict("records"),
      "top_surplus":df.nlargest(15,"surplus")[["sid","district","name","cap","stock","need","surplus"]].to_dict("records"),
      "by_district":df.groupby("district").agg(deficit=("deficit","sum"),surplus=("surplus","sum")).round(0).query("deficit>0 or surplus>0").sort_values("deficit",ascending=False).head(12).reset_index().to_dict("records")}
    df.to_csv(f"{R}/prepos_{'am' if a<20 else 'pm'}.csv",index=False)
out["prepositioning"]=plan

# ---- 分流 vs 派車：缺車時鄰站有無資源 ----
A3=np.zeros((ns,ns),np.float32); A5=np.zeros((ns,ns),np.float32)
n3=nb[nb.dist_m<=300]; n5=nb[nb.dist_m<=500]
A3[n3.sid.values,n3.nsid.values]=1; A5[n5.sid.values,n5.nsid.values]=1
Bf=np.nan_to_num(B); NB3=Bf@A3; NB5=Bf@A5
em=(B==0)
c3=((NB3>=3)&em).sum(); c5=((NB5>=3)&em).sum(); tot=em.sum()
out["diversion_vs_truck"]={"empty_halfhours":int(tot),
  "covered_within_300m":int(c3),"covered_within_500m":int(c5),
  "pct_300m":round(100*c3/max(tot,1),1),"pct_500m":round(100*c5/max(tot,1),1),
  "must_dispatch_pct":round(100*(tot-c5)/max(tot,1),1),
  "isolated_stations_no_neighbor_500m":int((A5.sum(axis=1)==0).sum()),
  "note":"鄰站當下有車只是候選資源存在，不代表走得到或到站仍有車。"}

# ---- 死庫存：雙零與長期不變 ----
bothz=np.nansum((B==0)&(S==0),axis=0)/np.maximum(nobs,1)
flat=np.zeros(ns)
for s_ in range(ns):
    col=B[:,s_]; ok=~np.isnan(col)
    if ok.sum()<10: continue
    v=col[ok]; flat[s_]=np.mean(v[1:]==v[:-1])
dead=pd.DataFrame({"sid":st.sid,"district":st.district,"name":st.name,"cap":cap,
                   "both_zero_pct":(100*bothz).round(1),"flat_pct":(100*flat).round(1),
                   "mean_bikes":np.nanmean(B,axis=0).round(1),"flow_day":(np.nansum(OUTF,axis=0)/days).round(1)})
dd=dead[(dead.both_zero_pct>=30)].sort_values("cap",ascending=False)
out["dead_capacity"]={"stations":int(len(dd)),"docks_locked":int(dd.cap.sum()),
  "pct_of_city_docks":round(100*dd.cap.sum()/cap.sum(),1),
  "top":dd.head(20).to_dict("records"),
  "note":"雙零可能是停站、整修或資料未更新，必須查核狀態，不可直接派車。"}
low=dead[(dead.flow_day<1)&(dead.both_zero_pct<30)&(dead.cap>=15)].sort_values("cap",ascending=False)
out["low_use_stations"]={"stations":int(len(low)),"docks":int(low.cap.sum()),"top":low.head(15).to_dict("records")}

# ---- 車柱配置診斷 ----
flow_day=np.nansum(OUTF+INF,axis=0)/days
prod=flow_day/np.maximum(cap,1)
zd=np.nansum(S==0,axis=0)/np.maximum(nobs,1)
cfg=pd.DataFrame({"sid":st.sid,"district":st.district,"name":st.name,"cap":cap,"prod":prod.round(2),
                  "zero_dock_pct":(100*zd).round(1),"flow_day":flow_day.round(1),"alive":alive})
cfg=cfg[cfg.alive]
out["dock_config"]={
  "expand":cfg[(cfg.zero_dock_pct>=8)&(cfg["prod"]>=cfg["prod"].quantile(.7))].nlargest(15,"zero_dock_pct")[["sid","district","name","cap","flow_day","zero_dock_pct","prod"]].to_dict("records"),
  "shrink":cfg[(cfg.cap>=30)&(cfg["prod"]<=cfg["prod"].quantile(.1))].nlargest(15,"cap")[["sid","district","name","cap","flow_day","prod"]].to_dict("records")}

# ---- 趟次估算 ----
for wname,v in plan.items():
    d=v["total_deficit_bikes"]; s_=v["total_surplus_bikes"]
    v["trips_needed_cap20"]=int(np.ceil(min(d,s_)/20)) if s_>0 else None
    v["uncovered_by_local_surplus"]=int(max(0,d-s_))
json.dump(out,open(f"{R}/dispatch_plan.json","w"),ensure_ascii=False,indent=1,default=str)
print("DONE",round(time.time()-t0),"s")
