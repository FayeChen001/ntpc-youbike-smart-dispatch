#!/usr/bin/env python3
"""重算 docs/POSITIONING.md 引用的每一個數字。

只讀 data/processed 的凍結資料，不寫任何檔案、不碰模型。
    python3 reports/gap_vs_taipei.py

口徑（與 docs/POSITIONING.md 一致，改這裡就要同步改那裡）：
  * 半小時快照，valid=True，2026/01/01–06/30
  * 日間定義 06:00–23:59，對齊台北「見車率」的 18 小時
  * 排除雙零快照（bikes=0 且 spaces=0）——資料異常，不判定為空站或滿站
  * 排除全期車數毫無變化的站——疑似未投車／停用，屬待查清單
  * 尖峰 07:00–09:59 與 17:00–19:59，僅平日
"""
import numpy as np
import pandas as pd
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PROC = ROOT / "data" / "processed"


def load():
    st = pd.read_parquet(PROC / "stations.parquet")[["sid", "name", "district"]]
    o = pd.read_parquet(PROC / "obs.parquet", columns=["sid", "bin", "bikes", "spaces", "valid"])
    o = o[o.valid]
    dz = (o.bikes == 0) & (o.spaces == 0)
    print(f"雙零快照 {int(dz.sum()):,} 格、涉及 {o[dz].sid.nunique()} 站 → 排除")
    o = o[~dz]
    var = o.groupby("sid").bikes.nunique()
    flat = var[var <= 1].index
    print(f"全期車數毫無變化 {len(flat)} 站 → 排除（疑似未投車／停用，屬待查清單）")
    print("  " + "、".join(st[st.sid.isin(flat)].name.tolist()))
    return st, o[~o.sid.isin(flat)]


def peak(o, lo, hi):
    return o[o.bin.dt.hour.between(lo, hi) & (o.bin.dt.dayofweek < 5)]


def top(o, st, col, lo, hi, k=10):
    d = peak(o, lo, hi)
    t = d.groupby("sid").agg(rate=(col, lambda s: (s == 0).mean()), n=(col, "size"))
    t = t[t.n > 200].merge(st, on="sid").sort_values("rate", ascending=False).head(k)
    t["rate"] = (t["rate"] * 100).round(1)
    return t[["name", "district", "rate"]]


def alt_gap(o, st, col):
    """事件發生當下，500 公尺內找不到任何可用替代站的比例。"""
    day = o[o.bin.dt.hour.between(6, 23)]
    piv = day.pivot_table(index="sid", columns="bin", values=col, aggfunc="first")
    sids, X = piv.index.to_numpy(), piv.to_numpy()
    M = ~np.isnan(X)
    nb = pd.read_parquet(PROC / "neighbors_800m.parquet")
    nb = nb[nb.dist_m <= 500]
    pos = {s: i for i, s in enumerate(sids)}
    nmap = {}
    for s, g in nb.groupby("sid"):
        idx = [pos[x] for x in g.nsid if x in pos]
        if s in pos and idx:
            nmap[pos[s]] = np.array(idx)
    ev = (X == 0) & M
    tot = bad = noalt = 0
    for i in range(len(sids)):
        z = ev[i]
        if not z.any():
            continue
        tot += int(z.sum())
        if i not in nmap:                       # 500 公尺內根本沒有其他站
            bad += int(z.sum())
            noalt += int(z.sum())
            continue
        ok = (X[nmap[i]] >= 1) & M[nmap[i]]     # 鄰站當下可用
        bad += int((z & ~ok.any(0)).sum())
    return tot, bad, noalt


def main():
    st, o = load()
    mrt = set(st[st.name.str.contains("捷運")].sid)

    print("\n【1】早峰 07:00–09:59 的兩端失敗（平日）")
    am = peak(o, 7, 9)
    for label, sel in (("捷運站", am.sid.isin(mrt)), ("非捷運站", ~am.sid.isin(mrt))):
        d = am[sel]
        print(f"  {label}：無車可借 {(d.bikes == 0).mean():.1%}　無位可還 {(d.spaces == 0).mean():.1%}")

    print("\n【2】早峰無位可還 前 10\n", top(o, st, "spaces", 7, 9).to_string(index=False))
    print("\n【3】早峰無車可借 前 10\n", top(o, st, "bikes", 7, 9).to_string(index=False))
    print("\n【4】晚峰 17:00–19:59 無車可借 前 10（捷運站翻轉成借不到）\n",
          top(o, st, "bikes", 17, 19).to_string(index=False))

    jz = st[st.name.str.contains("江子翠")]
    d = am[am.sid.isin(jz.sid)].merge(jz, on="sid")
    g = d.groupby("name").agg(無車可借=("bikes", lambda s: (s == 0).mean()),
                              無位可還=("spaces", lambda s: (s == 0).mean())).mul(100).round(1)
    print("\n【5】江子翠站群早峰（%）——同站群差異極大，走 200 公尺就解決\n", g.to_string())

    print("\n【6】替代站是否存在（500 公尺內，日間）")
    for col, what in (("bikes", "零車時找不到有車替代站"), ("spaces", "零位時找不到可還替代站")):
        tot, bad, noalt = alt_gap(o, st, col)
        print(f"  {what}：{bad:,}/{tot:,} = {bad / tot:.1%}（其中根本沒有鄰站 {noalt / tot:.1%}）")

    nb = pd.read_parquet(PROC / "neighbors_800m.parquet")
    allst = pd.read_parquet(PROC / "stations.parquet")
    for dist in (300, 500, 800):
        c = nb[nb.dist_m <= dist].groupby("sid").size()
        k = int((allst.sid.map(c).fillna(0) == 0).sum())
        print(f"  {dist} 公尺內沒有任何其他站的孤站：{k}/{len(allst)} = {k / len(allst):.1%}")

    day = o[o.bin.dt.hour.between(6, 23)]
    seen = 1 - day.groupby("sid").bikes.apply(lambda s: (s == 0).mean())
    print(f"\n【7】見車率（快照口徑，非連續時間）平均 {seen.mean():.1%}；"
          f"低於 90% 的站 {int((seen < 0.9).sum())}/{len(seen)}")
    print("\n提醒：以上都是半小時快照比例，不是連續中斷時間，也不是旅次成功率。")


if __name__ == "__main__":
    main()
