"""FVG二买二卖 回测跑分(用户 2026-07-11): 正式版 vs 3个对照组, 一屏看清每条规则的边际。

用法:
  .venv/Scripts/python scripts/macrofvg_bt.py --days 30            # 全市场
  .venv/Scripts/python scripts/macrofvg_bt.py --days 30 --limit 40 # 抽样快跑
"""
import argparse
import json
import os
import sys
import time
from concurrent.futures import ProcessPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

import bt_registry as R
import strat_macrofvg as MF

FEE = 0.045      # 单边 taker 手续费 %


def _job(args):
    sym, path, names = args
    k5 = json.load(open(path))
    return {n: MF.walk(sym, k5, MF.VARIANTS[n]) for n in names}   # 保留 extra(看图器画FVG要用)


def stats(rows):
    closed = [s for s in rows if s["result"] in ("tp", "sl")]
    n, c = len(rows), len(closed)
    if not c:
        return dict(n=n, closed=0, win=0.0, exp_r=0.0, net_r=0.0, total_net=0.0)
    wins = sum(1 for s in closed if s["result"] == "tp")
    r = sum(s["pnl_r"] for s in closed)
    nr = sum(s["net_r"] for s in closed)
    return dict(n=n, closed=c, win=round(wins / c * 100, 1),
                exp_r=round(r / c, 3), net_r=round(nr / c, 3), total_net=round(nr, 1))


def line(tag, s):
    return (f"  {tag:<10} 信号{s['n']:>5}  已结{s['closed']:>5}  胜率{s['win']:>5.1f}%  "
            f"期望{s['exp_r']:>+7.3f}R  扣费后{s['net_r']:>+7.3f}R  累计{s['total_net']:>+8.1f}R")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--strats", default=",".join(MF.VARIANTS))
    a = ap.parse_args()
    names = [x for x in a.strats.split(",") if x]

    tag = f"_5m_{a.days}d.json"
    files = []
    for f in sorted(os.listdir(R.CACHE)):
        if f.endswith(tag) and not f.startswith("sig_"):
            files.append((f[: -len(tag)], os.path.join(R.CACHE, f)))
    if a.limit:
        files = files[:a.limit]
    print(f"[bt] {len(files)} 个币 × {a.days}天 5m, 变体: {', '.join(names)}", flush=True)

    t0 = time.time()
    agg = {n: [] for n in names}
    with ProcessPoolExecutor() as ex:
        for i, res in enumerate(ex.map(_job, [(s, p, names) for s, p in files], chunksize=8)):
            for n in names:
                agg[n].extend(res[n])
            if (i + 1) % 100 == 0:
                print(f"  ...{i+1}/{len(files)}  {round(time.time()-t0)}s", flush=True)
    print(f"[bt] 扫描完成 {round(time.time()-t0,1)}s\n", flush=True)

    for n in names:
        rows = agg[n]
        print(f"{n}  ({MF.META.get(n, {}).get('label', n)})")
        print(line("全部", stats(rows)))
        for d in ("long", "short"):
            sub = [s for s in rows if s["direction"] == d]
            if sub:
                print(line("做多" if d == "long" else "做空", stats(sub)))
        print()
        out = os.path.join(R.CACHE, f"sig_{n}_{a.days}d.json")
        json.dump(rows, open(out, "w"))
    print(f"信号已写入 .btcache/sig_<变体>_{a.days}d.json (看图器可直接看)")


if __name__ == "__main__":
    main()
