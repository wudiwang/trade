#!/usr/bin/env python
"""反弹质量三门槛的 A/B 回测: 关掉门槛(基准) vs 打开门槛(新版), 同一批K线同一套结算。

输出扣费前/后的期望值(R) —— 手续费口径与 exit_lab.py 一致(FEE=0.00045, 进出两条腿)。
用法: .venv/bin/python scripts/bt_gate_ab.py [--days 30] [--conc 6]
"""
import argparse
import json
import os
import statistics as st
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
sys.path.insert(0, ROOT)

import bt_registry as R  # noqa: E402

FEE = 0.00045   # 与 exit_lab.py 同口径: 单边费率, 进+出共两条腿


def _syms(days: int):
    out = []
    for f in os.listdir(R.CACHE):
        if f.endswith(f"_5m_{days}d.json") and not f.startswith("sig_"):
            out.append(f[: -len(f"_5m_{days}d.json")])
    return sorted(out)


_ORIG_PARAMS = R._macro_params   # 必须存原函数: 否则第二轮会读到第一轮打的补丁


def _run(gates_on: bool, days: int, conc: int):
    """跑一遍全市场扫描。gates_on=False 时把四个阈值抹掉(等于回到加门槛之前)。"""
    base = _ORIG_PARAMS()
    if not gates_on:
        for k in ("retrace_min", "retrace_max", "leg_body_ratio_min", "max_leg_pct"):
            base.pop(k, None)
    R._macro_params = lambda _b=base: dict(_b)   # 让 scan 拿到我们要的参数

    syms = _syms(days)
    rows = []

    def load(sym, tf):
        p = os.path.join(R.CACHE, f"{sym}_{tf}_{days}d.json")
        if not os.path.exists(p):
            return None
        try:
            return json.load(open(p, encoding="utf-8"))
        except Exception:
            return None

    # C(tf) 的契约是返回 {symbol: klines} 整批 —— 分批喂, 免得 2.7G 全进内存
    BATCH = 40
    for i in range(0, len(syms), BATCH):
        chunk = syms[i: i + BATCH]
        with ThreadPoolExecutor(max_workers=conc) as ex:
            loaded = dict(zip(chunk, ex.map(lambda s: load(s, "5m"), chunk)))
        book = {s: k for s, k in loaded.items() if k}
        try:
            rows.extend(R.SCANS["macro_pullback"](lambda tf, _b=book: _b if tf == "5m" else {}) or [])
        except Exception as e:
            print(f"  ! batch {i}: {type(e).__name__}: {e}", flush=True)
        if (i // BATCH) % 4 == 0:
            print(f"  {min(i+BATCH,len(syms))}/{len(syms)} 币, {len(rows)} 信号", flush=True)
    return rows, len(syms)


def _stats(rows, days):
    closed = [r for r in rows if r.get("result") in ("tp", "sl") and r.get("pnl_r") is not None]
    if not closed:
        return {"signals": len(rows), "closed": 0}
    rs = [float(r["pnl_r"]) for r in closed]
    # 扣费: 每笔进出两条腿, 折算成 R = 2*FEE*entry/风险距离
    net = []
    for r in closed:
        risk = abs(float(r["entry"]) - float(r["sl"]))
        fee_r = 2 * FEE * float(r["entry"]) / risk if risk > 0 else 0.0
        net.append(float(r["pnl_r"]) - fee_r)
    wins = [x for x in rs if x > 0]
    eq, peak, dd = 0.0, 0.0, 0.0
    for x in net:
        eq += x
        peak = max(peak, eq)
        dd = max(dd, peak - eq)
    return {
        "signals": len(rows), "closed": len(closed),
        "per_day": round(len(rows) / days, 1),
        "win_rate": round(len(wins) / len(rs) * 100, 1),
        "exp_gross": round(st.mean(rs), 4),
        "exp_net": round(st.mean(net), 4),
        "fee_drag": round(st.mean(rs) - st.mean(net), 4),
        "total_net_R": round(sum(net), 1),
        "max_dd_R": round(dd, 1),
        "avg_win": round(st.mean(wins), 2) if wins else 0,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--conc", type=int, default=6)
    args = ap.parse_args()

    res = {}
    for label, on in (("基准(无门槛)", False), ("新版(三门槛)", True)):
        t0 = time.time()
        print(f"\n=== {label} ===", flush=True)
        rows, n = _run(on, args.days, args.conc)
        res[label] = _stats(rows, args.days)
        res[label]["symbols"] = n
        print(f"  用时 {time.time()-t0:.0f}s", flush=True)

    print("\n" + "=" * 78)
    keys = [("signals", "信号数"), ("per_day", "次/天"), ("closed", "已结算"),
            ("win_rate", "胜率%"), ("exp_gross", "期望R(扣费前)"),
            ("exp_net", "期望R(扣费后)"), ("fee_drag", "费用拖累R"),
            ("total_net_R", "累计净R"), ("max_dd_R", "最大回撤R"), ("avg_win", "均盈R")]
    a, b = res["基准(无门槛)"], res["新版(三门槛)"]
    print(f"{'指标':<16}{'基准(无门槛)':>16}{'新版(三门槛)':>16}")
    for k, lab in keys:
        print(f"{lab:<16}{str(a.get(k,'-')):>18}{str(b.get(k,'-')):>18}")
    if a.get("closed") and b.get("closed"):
        print(f"\n边际贡献(扣费后期望): {b['exp_net']-a['exp_net']:+.4f}R")
    json.dump(res, open(os.path.join(R.CACHE, "gate_ab.json"), "w"),
              ensure_ascii=False, indent=1)


if __name__ == "__main__":
    main()
