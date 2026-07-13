"""灵感001: 5m FVG 回踩 + 1m 缠论底分型入场(用户 2026-07-12)。

来源: research/ideas/001-fvg-retest-1m-entry/  (EDGEUSDT 实盘灵感)

链路(全部无未来函数):
  ① 5m: 底分型 → 顶分型 的一段上涨, 涨幅 ≥ min_leg_pct
  ② 该上涨段内出现 FVG(缺口): high[i-1] < low[i+1] → 区间 [high[i-1], low[i+1]]
  ③ 顶分型后回踩: 价格低点进入 FVG 区间(取回踩实际碰到的那个缺口, 由新到老)
     作废条件: 1m 收盘跌穿缺口下沿
  ④ 1m: 进入区间后, 第一个缠论底分型 = 止跌信号; 在【确认K收盘】那一刻买入(confirm_src_idx)
  ⑤ 止损三方案 A/B/C(本策略的核心A/B, 见 timeline: 实盘死在止损太窄)
     A=1m止跌分型低点(最窄)  B=FVG下沿  C=整段回踩最低点-buffer
  ⑥ 止盈: RR2 / RR3 / 前高(= 上涨段的顶分型高点)

结算在 1m 上逐根走, 同根内先判止损(保守)。手续费 taker 单边 0.045%, 往返扣。
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from app.engine.chan import find_fractals, merge_klines

FEE_SIDE = 0.045 / 100.0

BASE = dict(
    min_leg_pct=2.0,          # 上涨段最小涨幅
    max_leg_bars=36,          # 上涨段最长(5m根数) = 3小时
    min_gap_pct=0.10,         # FVG最小宽度%
    max_wait_bars=24,         # 顶分型后最多等多少根5m出现回踩
    max_entry_wait_1m=60,     # 进入FVG后最多等60根1m出现止跌分型
    sl_buf_pct=0.0,           # 止损缓冲
    c_buf_pct=0.3,            # 方案C额外留的buffer
    stop="A",                 # A/B/C
    tp="rr2",                 # rr2 / rr3 / prehigh
    max_hold_1m=480,          # 最长持有8小时, 超时按市价平(避免永远open)
)


def _f(k, n):
    return float(k[n])


def find_fvgs(k5, a, b, min_gap_pct):
    """上涨段 a..b 内的看涨FVG(缺口), 由新到老返回。"""
    out = []
    for i in range(a + 1, min(b, len(k5) - 1)):
        lo, hi = _f(k5[i - 1], "high"), _f(k5[i + 1], "low")
        if hi <= lo:
            continue
        if (hi - lo) / ((hi + lo) / 2) * 100 < min_gap_pct:
            continue
        out.append({"i": i, "lo": lo, "hi": hi, "t": int(k5[i - 1]["open_time"])})
    return list(reversed(out))          # 新的在前: 回踩先碰到的通常是最近的缺口


def _idx_at(times, ts):
    """times 已排序; 返回第一个 >= ts 的下标。"""
    lo, hi = 0, len(times)
    while lo < hi:
        m = (lo + hi) // 2
        if times[m] < ts:
            lo = m + 1
        else:
            hi = m
    return lo


def walk(sym, k5, k1, P):
    """扫一个币, 返回信号列表。k5/k1 = 5m/1m K线。"""
    if len(k5) < 60 or len(k1) < 300:
        return []
    fx5 = find_fractals(k5, merge_klines(k5))
    bots = [f for f in fx5 if f.kind == "bottom"]
    tops = [f for f in fx5 if f.kind == "top"]
    if not bots or not tops:
        return []

    t1 = [int(x["open_time"]) // 1000 for x in k1]
    fx1 = find_fractals(k1, merge_klines(k1))
    # 1m 底分型: 按"确认K"下标索引 —— 确认K收盘那一刻才知道有这个分型(无未来函数)
    bot1 = sorted([(f.confirm_src_idx, f.extreme_src_idx) for f in fx1 if f.kind == "bottom"])

    min_leg = float(P["min_leg_pct"]) / 100.0
    out, used = [], set()

    for b in bots:
        lo_idx = b.extreme_src_idx
        low = _f(k5[lo_idx], "low")
        # ① 找这个低点之后的第一个够高的顶分型 = 上涨段
        for t in tops:
            hi_idx = t.extreme_src_idx
            if hi_idx <= lo_idx or hi_idx - lo_idx > int(P["max_leg_bars"]):
                continue
            high = _f(k5[hi_idx], "high")
            if (high - low) / low < min_leg:
                continue
            if hi_idx in used:
                break
            # ② 段内的缺口
            gaps = find_fvgs(k5, lo_idx, hi_idx, float(P["min_gap_pct"]))
            if not gaps:
                break
            used.add(hi_idx)
            # ③ 顶分型之后回踩, 找第一个被碰到的缺口
            hit = None
            end = min(len(k5), hi_idx + 1 + int(P["max_wait_bars"]))
            for j in range(hi_idx + 1, end):
                lo_j = _f(k5[j], "low")
                for g in gaps:
                    if lo_j <= g["hi"]:          # 回踩进了这个缺口(碰到上沿即算)
                        hit = (g, j)
                        break
                if hit:
                    break
                if _f(k5[j], "close") > high:    # 没回踩就先破前高 → 这次机会作废
                    break
            if not hit:
                break
            g, j5 = hit
            sig = _entry_1m(sym, k5, k1, t1, bot1, b, t, g, j5, low, high, P)
            if sig:
                out.append(sig)
            break
    return out


def _entry_1m(sym, k5, k1, t1, bot1, b5, t5, g, j5, leg_low, leg_high, P):
    """④ 回踩进缺口后, 在1m上等第一个底分型(确认K收盘) → 入场。"""
    enter_ts = int(k5[j5]["open_time"]) // 1000          # 回踩那根5m的开盘时刻
    i0 = _idx_at(t1, enter_ts)
    i_end = min(len(k1) - 1, i0 + int(P["max_entry_wait_1m"]))
    if i0 >= len(k1) - 2:
        return None

    for cf, ext in bot1:
        if cf <= i0:
            continue
        if cf > i_end:
            return None                                   # 等太久 → 放弃
        # 作废: 入场前 1m 收盘跌穿缺口下沿
        if any(_f(k1[x], "close") < g["lo"] for x in range(i0, cf + 1)):
            return None
        low_ext = _f(k1[ext], "low")
        if low_ext > g["hi"]:                             # 分型没落在缺口里 → 不算这次回踩的止跌
            continue
        entry = _f(k1[cf], "close")                       # 止跌信号刚出那一刻
        pull_low = min(_f(k1[x], "low") for x in range(i0, cf + 1))

        st = P["stop"]
        buf = float(P["sl_buf_pct"]) / 100.0
        if st == "A":
            sl = low_ext * (1 - buf)
        elif st == "B":
            sl = g["lo"] * (1 - buf)
        else:
            sl = pull_low * (1 - float(P["c_buf_pct"]) / 100.0)
        if sl >= entry:
            return None
        risk = entry - sl

        if P["tp"] == "rr3":
            tp = entry + 3 * risk
        elif P["tp"] == "prehigh":
            tp = leg_high
        else:
            tp = entry + 2 * risk
        if tp <= entry:
            return None

        row = {
            "strat": "fvg1m", "symbol": sym, "tf": "1m", "direction": "long",
            "stage": "fvg_retest_1m", "created_at": t1[cf], "entry_time": t1[cf] * 1000,
            "entry": round(entry, 10), "sl": round(sl, 10), "tp": round(tp, 10),
            "rr": round((tp - entry) / risk, 2),
            "stop_pct": round(risk / entry * 100, 3),
            "leg_pct": round((leg_high - leg_low) / leg_low * 100, 2),
            "gap_pct": round((g["hi"] - g["lo"]) / ((g["hi"] + g["lo"]) / 2) * 100, 3),
            "wait_1m": cf - i0,
            "anchor": int(k5[t5.extreme_src_idx]["open_time"]),
            "extra": {"path": "fvg1m", "fvg": g,
                      "structure": {"L1": leg_low, "H1": leg_high,
                                    "L1_time": int(k5[b5.extreme_src_idx]["open_time"]),
                                    "H1_time": int(k5[t5.extreme_src_idx]["open_time"]),
                                    "entry_time": t1[cf] * 1000}},
        }
        _settle(row, k1, cf, int(P["max_hold_1m"]))
        return row
    return None


def _settle(s, k1, i_entry, max_hold):
    s["result"], s["pnl_r"], s["bars_held"] = "open", None, None
    entry, sl, tp = s["entry"], s["sl"], s["tp"]
    risk = entry - sl
    end = min(len(k1), i_entry + max_hold + 1)
    for j in range(i_entry, end):
        lo, hi = _f(k1[j], "low"), _f(k1[j], "high")
        if lo <= sl:                                   # 同根内先判止损(保守)
            s["result"], s["pnl_r"] = "sl", -1.0
        elif hi >= tp:
            s["result"], s["pnl_r"] = "tp", round((tp - entry) / risk, 3)
        if s["result"] != "open":
            s["bars_held"] = j - i_entry
            break
    if s["result"] == "open" and end - 1 > i_entry and end < len(k1) + 1:
        px = _f(k1[end - 1], "close")                  # 超时平仓, 按市价
        s["result"], s["pnl_r"] = "timeout", round((px - entry) / risk, 3)
        s["bars_held"] = end - 1 - i_entry
    if s["pnl_r"] is not None:
        s["net_r"] = round(s["pnl_r"] - 2 * FEE_SIDE * entry / risk, 3)
    return s
