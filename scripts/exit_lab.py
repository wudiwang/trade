"""出场实验室(用户 2026-06-26):在某策略的真实信号上, 对比不同止盈/止损规则的胜率+扣费期望。
不改入场, 只换出场: RR降档 / 1R分批+移动止损 / 最小止损距离。多窗看一致性。

用法: .venv/Scripts/python scripts/exit_lab.py macro_pullback
"""
import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass
import bt_registry as R
if R.ROOT not in sys.path:
    sys.path.insert(0, R.ROOT)

FEE = 0.00045


def _atr(kl, i, n=14):
    if i < n:
        return 0.0
    tr = []
    for j in range(i - n + 1, i + 1):
        h, l = float(kl[j]["high"]), float(kl[j]["low"]); pc = float(kl[j - 1]["close"])
        tr.append(max(h - l, abs(h - pc), abs(l - pc)))
    return sum(tr) / n


def resettle(kl, i, long, entry, risk, cfg):
    """返回 (净R, 是否赢)。risk=止损距离(价)。cfg定义出场规则。"""
    # 最小止损距离
    min_abs = 0.0
    if cfg.get("min_pct"):
        min_abs = max(min_abs, cfg["min_pct"] / 100.0 * entry)
    if cfg.get("min_atr"):
        min_abs = max(min_abs, cfg["min_atr"] * _atr(kl, i))
    rk = max(risk, min_abs) if min_abs else risk
    if rk <= 0:
        return None, None
    sl = entry - rk if long else entry + rk
    rr = cfg.get("rr", 2.0)
    fee_r = 2 * FEE * entry / rk
    end = min(len(kl), i + 150)
    if not cfg.get("partial"):
        tp = entry + rr * rk if long else entry - rr * rk
        for j in range(i + 1, end):
            lo, hi = float(kl[j]["low"]), float(kl[j]["high"])
            if (lo <= sl) if long else (hi >= sl):
                return -1.0 - fee_r, False
            if (hi >= tp) if long else (lo <= tp):
                return rr - fee_r, True
        return None, None
    # 分批: 到1R平一半+移动止损到入场, 余下跑到2R
    oneR = entry + rk if long else entry - rk
    twoR = entry + 2 * rk if long else entry - 2 * rk
    got, slc = False, sl
    fee_r3 = 3 * FEE * entry / rk   # 三条腿手续费
    for j in range(i + 1, end):
        lo, hi = float(kl[j]["low"]), float(kl[j]["high"])
        if not got:
            if (lo <= slc) if long else (hi >= slc):
                return -1.0 - fee_r, False
            if (hi >= oneR) if long else (lo <= oneR):
                got, slc = True, entry
        else:
            if (lo <= slc) if long else (hi >= slc):
                return 0.5 - fee_r3, True   # 半仓+1R, 余下保本→ +0.5R
            if (hi >= twoR) if long else (lo <= twoR):
                return 1.5 - fee_r3, True   # 半仓+1R + 半仓+2R → +1.5R
    return None, None


CONFIGS = [
    ("基线 RR2(现状)", {"rr": 2.0}),
    ("RR1.5", {"rr": 1.5}),
    ("RR1.0", {"rr": 1.0}),
    ("1R分批+移动止损", {"partial": True}),
    ("最小止损0.3% + RR2", {"rr": 2.0, "min_pct": 0.3}),
    ("最小止损0.5ATR + RR2", {"rr": 2.0, "min_atr": 0.5}),
    ("RR1.5 + 最小0.5ATR", {"rr": 1.5, "min_atr": 0.5}),
    ("分批 + 最小0.5ATR", {"partial": True, "min_atr": 0.5}),
]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("strat")
    a = ap.parse_args()
    C = R.cache_loader(30)
    k5map = C("5m")
    sigs = R.SCANS[a.strat](C)
    print(f"{a.strat}: {len(sigs)} 信号, 重算各出场规则…", flush=True)
    # 预处理: 每个信号 → (kl, i, long, entry, risk, t)
    items = []
    idxcache = {}
    for s in sigs:
        sym = s["symbol"]; kl = k5map.get(sym)
        if not kl:
            continue
        if sym not in idxcache:
            idxcache[sym] = {int(k["open_time"]) // 1000: ix for ix, k in enumerate(kl)}
        i = idxcache[sym].get(s["created_at"])
        if i is None or i < 20 or i >= len(kl) - 2:
            continue
        risk = abs(s["entry"] - s["sl"])
        items.append((kl, i, s["direction"] == "long", s["entry"], risk, s["created_at"]))
    ref = max(t for *_, t in items)

    def stats(rows):
        cl = [r for r in rows if r[0] is not None]
        if not cl:
            return (0, 0.0, 0.0)
        wins = sum(1 for r in cl if r[1])
        exp = sum(r[0] for r in cl) / len(cl)
        return (len(cl), round(wins / len(cl) * 100, 1), round(exp, 3))

    print(f"\n{'出场规则':<22}{'已结':>6}{'胜率':>8}{'扣费期望':>10}   近4窗扣费期望(一致性)")
    for name, cfg in CONFIGS:
        res = [resettle(kl, i, lo, e, rk, cfg) for kl, i, lo, e, rk, t in items]
        paired = list(zip(res, items))
        n, wr, exp = stats([r for r, _ in paired])
        # 4窗
        wins_str = []
        span = ref - min(t for *_, t in items)
        for w in range(4):
            ws = ref - span + (w) * span / 4 + 0.40 * span / 4 * 0  # 简单四等分
            ws = ref - span * (4 - w) / 4
            we = ref - span * (3 - w) / 4
            wr_rows = [r for r, it in paired if ws <= it[5] < we]
            _, _, we_exp = stats(wr_rows)
            wins_str.append(f"{we_exp:+.2f}")
        print(f"{name:<22}{n:>6}{wr:>7}%{exp:>+10}   [{' '.join(wins_str)}]")


if __name__ == "__main__":
    main()
