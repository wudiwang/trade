"""LSR 策略跨全市场批量回测(用户 2026-07-29)。原脚本单CSV, 这里循环本地5m缓存全币种。

- 用30天5m做指标/量能基准热身, 只统计【最近7天】入场的成交
- 多头 + 做空镜像 都跑
- 除了原生R, 额外算【扣费净期望】(我们的铁律): net_r = r - 2*FEE*entry/risk
- 跑条件消融(每个质量条件对期望的边际贡献)

用法: .venv/Scripts/python scripts/lsr_run.py [--days-report 7]
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
    for col in ("open", "high", "low", "close", "volume"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    return df[["open", "high", "low", "close", "volume"]].dropna()


def net_expectancy(trades):
    """扣费净期望: 每笔 net_r = r - 2*FEE*entry/risk。"""
    if trades.empty:
        return None, None, 0
    risk = (trades["entry"] - trades["stop"]).abs()
    fee_r = 2 * FEE * trades["entry"] / risk.replace(0, np.nan)
    net = trades["r"] - fee_r
    net = net.dropna()
    if net.empty:
        return None, None, 0
    return round(net.mean(), 4), round((net > 0).mean() * 100, 1), len(net)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days-report", type=int, default=7)
    ap.add_argument("--tf", default="5m")
    ap.add_argument("--days-cache", type=int, default=30)
    a = ap.parse_args()
    p = L.Params()

    tag = f"_{a.tf}_{a.days_cache}d.json"
    files = [f for f in glob.glob(os.path.join(CACHE, f"*{tag}"))
             if not os.path.basename(f).startswith("sig_")]
    print(f"LSR 批量回测 | {a.tf} | {len(files)}币 | 30天热身, 统计最近{a.days_report}天入场\n", flush=True)

    # 消融配置(同原脚本 ablate)
    base_req = list(p.required)
    configs = {"基线(全部必选)": base_req}
    for flag in L.OPTIONAL_FLAGS:
        if flag in base_req:
            configs[f"去掉 {flag}"] = [f for f in base_req if f != flag]
        else:
            configs[f"加上 {flag}"] = base_req + [flag]
    configs["仅结构(ok_breakout)"] = ["ok_breakout"]

    all_trades = {name: [] for name in configs}
    cutoff = None
    t0 = time.time()
    done = 0
    for f in files:
        try:
            kl = json.load(open(f))
        except Exception:
            continue
        if len(kl) < 300:
            continue
        sym = os.path.basename(f)[:-len(tag)]
        for direction, mk in (("long", False), ("short", True)):
            df = build_df(kl)
            if mk:
                df = L.mirror(df)
            df = L.add_indicators(df, p)
            if cutoff is None:
                cutoff = df.index[-1] - pd.Timedelta(days=a.days_report)
            try:
                cands = L.scan_candidates(df, p)
            except Exception:
                continue
            if cands.empty:
                continue
            for name, req in configs.items():
                tr = L.simulate(df, cands, p, req)
                if tr.empty:
                    continue
                tr = tr[tr["entry_time"] >= cutoff]
                if not tr.empty:
                    tr = tr.assign(symbol=sym, dir=direction)
                    all_trades[name].append(tr)
        done += 1
        if done % 100 == 0:
            print(f"  ...{done}/{len(files)} ({round(time.time()-t0)}s)", flush=True)

    print(f"\n扫描完成 {round(time.time()-t0)}s\n")
    print(f"{'配置':<26}{'笔数':>6}{'胜率':>7}{'毛期望R':>9}{'总R':>9}{'PF':>7}   扣费净期望R")
    for name, chunks in all_trades.items():
        tr = pd.concat(chunks, ignore_index=True) if chunks else pd.DataFrame()
        s = L.summarize(tr)
        if s.get("n", 0) == 0:
            print(f"{name:<26}{0:>6}")
            continue
        nexp, nwin, nn = net_expectancy(tr)
        pf = s.get("profit_factor")
        pf = "inf" if pf == float("inf") else pf
        print(f"{name:<26}{s['n']:>6}{s['win_rate']*100:>6.1f}%{s['expectancy_r']:>9}{s['total_r']:>9}{str(pf):>7}   {nexp}  (胜{nwin}%)")


if __name__ == "__main__":
    main()
