"""灵感001 回测: 同一批入场信号 × 3种止损 × 3种止盈 —— 回答"止损该放哪儿"。

用法: .venv/Scripts/python scripts/fvg1m_bt.py --days 30
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
import strat_fvg1m as S

STOPS = {"A": "1m止跌分型低点(最窄)", "B": "FVG缺口下沿", "C": "回踩最低点-0.3%"}
TPS = {"rr2": "固定1:2", "rr3": "固定1:3", "prehigh": "前高(上涨段顶)"}


def _job(args):
    sym, p5, p1, days = args
    k5, k1 = json.load(open(p5)), json.load(open(p1))
    out = {}
    for st in STOPS:
        for tp in TPS:
            P = dict(S.BASE, stop=st, tp=tp)
            out[f"{st}|{tp}"] = S.walk(sym, k5, k1, P)
    return out


def stat(rows):
    c = [r for r in rows if r["result"] in ("tp", "sl", "timeout")]
    if not c:
        return None
    w = sum(1 for r in c if (r["pnl_r"] or 0) > 0)
    gross = sum(r["pnl_r"] for r in c) / len(c)
    net = sum(r["net_r"] for r in c) / len(c)
    stop = sorted(r["stop_pct"] for r in c)
    return dict(n=len(c), win=w / len(c) * 100, gross=gross, net=net,
                total=sum(r["net_r"] for r in c), med_stop=stop[len(stop) // 2],
                fee=sum(r["pnl_r"] - r["net_r"] for r in c) / len(c))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=30)
    a = ap.parse_args()
    tag1, tag5 = f"_1m_{a.days}d.json", f"_5m_{a.days}d.json"
    files = []
    for f in sorted(os.listdir(R.CACHE)):
        if f.endswith(tag1) and not f.startswith("sig_"):
            sym = f[: -len(tag1)]
            p5 = os.path.join(R.CACHE, sym + tag5)
            if os.path.exists(p5):
                files.append((sym, p5, os.path.join(R.CACHE, f), a.days))
    print(f"[bt] {len(files)} 个币(有1m数据) × {a.days}天\n", flush=True)

    t0 = time.time()
    agg = {f"{s}|{t}": [] for s in STOPS for t in TPS}
    with ProcessPoolExecutor() as ex:
        for res in ex.map(_job, files, chunksize=2):
            for k, v in res.items():
                agg[k].extend(v)
    print(f"[bt] 扫描完成 {round(time.time()-t0,1)}s\n")

    print("同一批入场信号, 只换出场 —— 扣费后期望(净R/单):\n")
    print(f"  {'止损方案':<22}{'止盈':<14}{'信号':>5}{'胜率':>7}{'毛R':>8}{'手续费':>8}{'净R':>8}{'累计':>8}{'止损距离':>8}")
    best = None
    for st in STOPS:
        for tp in TPS:
            s = stat(agg[f"{st}|{tp}"])
            if not s:
                continue
            print(f"  {st}·{STOPS[st]:<18}{TPS[tp]:<12}{s['n']:>5}{s['win']:>6.1f}%"
                  f"{s['gross']:>+8.3f}{-s['fee']:>+8.3f}{s['net']:>+8.3f}{s['total']:>+8.1f}{s['med_stop']:>7.2f}%")
            if best is None or s["net"] > best[0]["net"]:
                best = (s, st, tp)
        print()
    if best:
        s, st, tp = best
        print(f"最佳组合: 止损{st}({STOPS[st]}) + 止盈{TPS[tp]}  → 净 {s['net']:+.3f}R/单 (n={s['n']})")
        rows = agg[f"{st}|{tp}"]
        json.dump(rows, open(os.path.join(R.CACHE, f"sig_fvg1m_{a.days}d.json"), "w"))
        print(f"信号已写入 sig_fvg1m_{a.days}d.json (看图器可看)")


if __name__ == "__main__":
    main()
