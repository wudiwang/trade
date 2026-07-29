"""LSR + 30m趋势前置过滤 回测(用户 2026-07-29)。
形态(LSR)已经用户审核通过; 这里加"30m必须已走出上升/下降趋势"的前置层, 并对比两种方向口径:
  口径A·顺势:  30m上升→多, 30m下降→空
  口径B·反转:  30m上升→空(顶部反转), 30m下降→多(底部反转)
30m趋势: 5m聚合成30m, 价在EMA20上方且EMA上行=up, 反之down, 否则range。
严格无未来: 信号时点只用已收盘的30m(close_time<=t, ffill)。

用法: .venv/Scripts/python scripts/lsr_run2.py --days-report 7
"""
import argparse
import glob
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass
import numpy as np
import pandas as pd
import lsr_strategy as L

CACHE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".btcache")
FEE = 0.00045


def build_df(kl):
    df = pd.DataFrame(kl)
    df["ts"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
    df = df.set_index("ts").sort_index()
    for c in ("open", "high", "low", "close", "volume"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    return df[["open", "high", "low", "close", "volume"]].dropna()


def trend30_by_5m(df5, span=20, slope_bars=3):
    """返回 index=5m时间 的 30m趋势('up'/'down'/'range'), 无未来函数(用已收盘30m ffill)。"""
    g = df5.resample("30min")
    df30 = pd.DataFrame({"high": g["high"].max(), "low": g["low"].min(),
                         "close": g["close"].last()}).dropna()
    if len(df30) < span + slope_bars + 2:
        return pd.Series("range", index=df5.index)
    ema = df30["close"].ewm(span=span, adjust=False).mean()
    up = (df30["close"] > ema) & (ema > ema.shift(slope_bars))
    down = (df30["close"] < ema) & (ema < ema.shift(slope_bars))
    tr = pd.Series("range", index=df30.index)
    tr[up] = "up"; tr[down] = "down"
    tr.index = df30.index + pd.Timedelta("30min")   # 该30m收盘时间才可用
    return tr.reindex(df5.index, method="ffill").fillna("range")


def net_exp(tr):
    if tr.empty:
        return None
    risk = (tr["entry"] - tr["stop"]).abs().replace(0, np.nan)
    net = (tr["r"] - 2 * FEE * tr["entry"] / risk).dropna()
    return round(net.mean(), 4) if len(net) else None


def stats(tr):
    if tr.empty:
        return dict(n=0)
    r = tr["r"]
    return dict(n=len(r), win=round((r > 0).mean() * 100, 1),
                gross=round(r.mean(), 4), total=round(r.sum(), 1), net=net_exp(tr))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days-report", type=int, default=7)
    a = ap.parse_args()
    p = L.Params()
    tag = "_5m_30d.json"
    files = [f for f in glob.glob(os.path.join(CACHE, f"*{tag}"))
             if not os.path.basename(f).startswith("sig_")]
    print(f"LSR + 30m趋势前置 | {len(files)}币 | 最近{a.days_report}天入场\n", flush=True)

    buckets = {"无过滤(参照)": [], "A·顺势": [], "B·反转": []}
    cutoff = None
    t0 = time.time(); done = 0
    for f in files:
        try:
            kl = json.load(open(f))
        except Exception:
            continue
        if len(kl) < 300:
            continue
        sym = os.path.basename(f)[:-len(tag)]
        df5 = build_df(kl)
        trend = trend30_by_5m(df5)
        if cutoff is None:
            cutoff = df5.index[-1] - pd.Timedelta(days=a.days_report)
        for direction, mk in (("long", False), ("short", True)):
            dfi = L.add_indicators(L.mirror(df5) if mk else df5, p)
            try:
                cands = L.scan_candidates(dfi, p)
            except Exception:
                continue
            if cands.empty:
                continue
            tr = L.simulate(dfi, cands, p, list(p.required))
            if tr.empty:
                continue
            tr = tr[tr["entry_time"] >= cutoff].copy()
            if tr.empty:
                continue
            tr["trend"] = trend.reindex(tr["entry_time"]).to_numpy()
            buckets["无过滤(参照)"].append(tr.assign(dir=direction))
            if direction == "long":
                buckets["A·顺势"].append(tr[tr["trend"] == "up"].assign(dir=direction))
                buckets["B·反转"].append(tr[tr["trend"] == "down"].assign(dir=direction))
            else:
                buckets["A·顺势"].append(tr[tr["trend"] == "down"].assign(dir=direction))
                buckets["B·反转"].append(tr[tr["trend"] == "up"].assign(dir=direction))
        done += 1
        if done % 150 == 0:
            print(f"  ...{done}/{len(files)} ({round(time.time()-t0)}s)", flush=True)

    print(f"\n扫描完成 {round(time.time()-t0)}s\n")
    print(f"{'口径':<16}{'笔数':>6}{'胜率':>7}{'毛期望R':>9}{'总R':>8}{'扣费净期望R':>13}")
    for name, chunks in buckets.items():
        tr = pd.concat(chunks, ignore_index=True) if chunks else pd.DataFrame()
        s = stats(tr)
        if s["n"] == 0:
            print(f"{name:<16}{0:>6}"); continue
        print(f"{name:<16}{s['n']:>6}{s['win']:>6}%{s['gross']:>9}{s['total']:>8}{str(s['net']):>13}")
    # A口径再拆多空看
    print("\n--- A·顺势 拆多空 ---")
    ac = pd.concat(buckets["A·顺势"], ignore_index=True) if buckets["A·顺势"] else pd.DataFrame()
    for d in ("long", "short"):
        s = stats(ac[ac["dir"] == d]) if not ac.empty else dict(n=0)
        lbl = "30m上升→做多" if d == "long" else "30m下降→做空"
        print(f"  {lbl:<16} n={s.get('n',0)} 胜{s.get('win','-')}% 毛{s.get('gross','-')} 净{s.get('net','-')}")


if __name__ == "__main__":
    main()
