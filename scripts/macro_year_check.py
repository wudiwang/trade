"""线上策略 macro_pullback 的一年复验(用户 2026-07-01, 线B)。

之前只有30天; 现用 top50主流币 5m 一年数据, 做 train(前70%)/holdout(后30%) 样本外
+ 按季度看行情一致性。macro_pullback 只吃5m单级别, 可直接用365d缓存。

用法: .venv/Scripts/python scripts/macro_year_check.py --days 365
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass
import bt_registry as R

FEE = 0.00045


def net_r(s):
    if s.get("result") not in ("tp", "sl") or s.get("pnl_r") is None:
        return None
    risk = abs(s["entry"] - s["sl"])
    if risk <= 0:
        return None
    return s["pnl_r"] - 2 * FEE * s["entry"] / risk


def stats(rows):
    nr = [net_r(s) for s in rows]
    nr = [x for x in nr if x is not None]
    if not nr:
        return (0, None, None)
    wins = sum(1 for x in nr if x > 0)
    return (len(nr), round(wins / len(nr) * 100, 1), round(sum(nr) / len(nr), 4))


def line(tag, rows):
    n, wr, ex = stats(rows)
    print(f"  {tag:<26} n={n:<6} 胜率{wr}%  净期望{ex}R")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=365)
    a = ap.parse_args()
    C = R.cache_loader(a.days)
    print(f"加载 {len(C('5m'))} 币 5m({a.days}天). 扫 macro_pullback…", flush=True)
    sigs = [s for s in R.SCANS["macro_pullback"](C) if s.get("created_at")]
    closed = [s for s in sigs if s.get("result") in ("tp", "sl")]
    print(f"信号 {len(sigs)} (已结算 {len(closed)})", flush=True)
    if not closed:
        return
    closed.sort(key=lambda s: s["created_at"])
    cut = closed[int(len(closed) * 0.70)]["created_at"]
    tr = [s for s in closed if s["created_at"] < cut]
    ho = [s for s in closed if s["created_at"] >= cut]

    print("\n=== 全段 ===")
    line("全部", closed)
    line("做多(二买)", [s for s in closed if s["direction"] == "long"])
    line("做空(二卖)", [s for s in closed if s["direction"] == "short"])

    print("\n=== train(前70%) / holdout(后30%) 样本外 ===")
    line("train 全部", tr); line("holdout 全部", ho)
    line("train 做多", [s for s in tr if s["direction"] == "long"])
    line("holdout 做多", [s for s in ho if s["direction"] == "long"])
    line("train 做空", [s for s in tr if s["direction"] == "short"])
    line("holdout 做空", [s for s in ho if s["direction"] == "short"])

    print("\n=== 按时间四等分(行情一致性) ===")
    t0 = closed[0]["created_at"]; t1 = closed[-1]["created_at"]; span = t1 - t0
    for q in range(4):
        ws, we = t0 + span * q / 4, t0 + span * (q + 1) / 4
        rows = [s for s in closed if ws <= s["created_at"] < we or (q == 3 and s["created_at"] == t1)]
        line(f"第{q+1}段", rows)


if __name__ == "__main__":
    main()
