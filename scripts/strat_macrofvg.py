"""FVG二买/二卖(用户 2026-07-11): 线上 macro_pullback 的结构 + 新出场 + FVG门槛。

存档策略, 只回测不上线(不改 app/engine/macro_pullback.py 线上逻辑)。

结构(照抄线上):
  爆量扫低K → 缠论底分型 L1(=一买) → 上涨一笔到 H1 → 回落出缠论底分型 L2(=二买, 不破L1)
  → L2 后停顿K(收盘>右K高) → 停顿后的下一根K 收盘价入场

本策略新增/改动:
  1) 止损 = L1(一买)低点          [线上是 L2 低点]
  2) 止盈 = 固定 1:3 (RR=3)       [线上是 RR2]
  3) FVG门槛: L1→H1 的上涨一笔中必须出现 FVG(三K缺口 low[i+1] > high[i-1]),
     且 L2 必须回落进该 FVG 区间 [gap_lo, gap_hi], 不能跌穿 gap_lo。
     做空侧对称(下跌一笔中的看跌FVG, H2 反弹进区间, 不能升穿 gap_hi)。

参数变体(用于 A/B, 见 VARIANTS):
  use_fvg      : 是否启用 FVG 门槛
  sl_anchor    : "L1"(一买低点) | "L2"(线上原样)
  rr           : 止盈盈亏比
  pierce_by    : "wick"(影线不得跌穿, 严) | "close"(收盘不得跌穿, 松)
  min_gap_pct  : FVG 最小宽度(%), 过滤掉贴合的假缺口
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from app.engine.chan import find_fractals, merge_klines
from app.engine.macro_pullback import (
    _body_reclaim_level, _effective_bar_count, _f, _vol_ratio,
)

# 线上 config.yaml 的 macro_pullback 结构参数(硬编码进存档策略, 与线上解耦)
BASE = dict(
    vol_ma=20, vol_mult=3.0, lookback=20, reclaim_bars=4, reclaim_body_pct=80,
    wyckoff_fractal_window=5, min_leg_pct=0.8, second_tolerance_pct=0.2,
    min_effective_bars_between=5, max_entry_leg_ratio=0.5,
    max_signal_bars_after_second=2, stop_buffer_pct=0.0,
    # 本策略新增
    use_fvg=True, sl_anchor="L1", rr=3.0, pierce_by="wick", min_gap_pct=0.0,
)


# ------------------------- FVG -------------------------
def find_fvgs(klines, lo_idx, hi_idx, direction, min_gap_pct=0.0):
    """一笔(lo_idx..hi_idx)内的 FVG 列表。

    看涨FVG(上涨一笔): 三根K i-1,i,i+1 满足 low[i+1] > high[i-1] → 区间 [high[i-1], low[i+1]]
    看跌FVG(下跌一笔): high[i+1] < low[i-1]                     → 区间 [high[i+1], low[i-1]]
    """
    out = []
    for i in range(max(lo_idx + 1, 1), min(hi_idx, len(klines) - 1)):
        if direction == "long":
            gap_lo, gap_hi = _f(klines[i - 1], "high"), _f(klines[i + 1], "low")
        else:
            gap_lo, gap_hi = _f(klines[i + 1], "high"), _f(klines[i - 1], "low")
        if gap_hi <= gap_lo:
            continue
        mid = (gap_hi + gap_lo) / 2
        if (gap_hi - gap_lo) / max(mid, 1e-12) * 100 < min_gap_pct:
            continue
        out.append({"idx": i, "lo": gap_lo, "hi": gap_hi,
                    "time": int(klines[i - 1]["open_time"])})
    return out


def fvg_hit(klines, fvgs, second_idx, leg_end_idx, direction, pierce_by="wick"):
    """回落/反弹是否"进了FVG且没穿": 返回命中的FVG, 否则 None。

    进入 = 二买极值 落进区间;   跌穿 = 二买极值(或收盘)越过区间远端。
    """
    for g in fvgs:
        if direction == "long":
            l2 = _f(klines[second_idx], "low")
            if l2 > g["hi"]:            # 没回落到FVG里
                continue
            if pierce_by == "wick":
                pierced = any(_f(k, "low") < g["lo"] for k in klines[g["idx"] + 1:second_idx + 1])
            else:
                pierced = any(_f(k, "close") < g["lo"] for k in klines[g["idx"] + 1:second_idx + 1])
            if pierced:
                continue
        else:
            h2 = _f(klines[second_idx], "high")
            if h2 < g["lo"]:
                continue
            if pierce_by == "wick":
                pierced = any(_f(k, "high") > g["hi"] for k in klines[g["idx"] + 1:second_idx + 1])
            else:
                pierced = any(_f(k, "close") > g["hi"] for k in klines[g["idx"] + 1:second_idx + 1])
            if pierced:
                continue
        return g
    return None


# ------------------------- 结构 + 出场 -------------------------
def _settle(s, k5, fee_pct_side=0.045):
    """按K线逐根结算(同一根内先判止损, 保守)。同时算扣费后净R。"""
    s["result"], s["pnl_r"], s["bars_held"] = "open", None, None
    entry, sl, tp = float(s["entry"]), float(s["sl"]), float(s["tp"])
    risk = abs(entry - sl)
    if risk <= 0:
        return s
    start = int(s["entry_idx"])
    for j in range(start, len(k5)):
        lo, hi = _f(k5[j], "low"), _f(k5[j], "high")
        if s["direction"] == "long":
            if lo <= sl:
                s["result"], s["pnl_r"] = "sl", -1.0
            elif hi >= tp:
                s["result"], s["pnl_r"] = "tp", round((tp - entry) / risk, 3)
        else:
            if hi >= sl:
                s["result"], s["pnl_r"] = "sl", -1.0
            elif lo <= tp:
                s["result"], s["pnl_r"] = "tp", round((entry - tp) / risk, 3)
        if s["result"] != "open":
            s["bars_held"] = j - start
            break
    if s["pnl_r"] is not None:
        cost = 2 * (fee_pct_side / 100.0) * entry / risk      # 往返手续费, 折算成R
        s["net_r"] = round(s["pnl_r"] - cost, 3)
    return s


def walk(sym, k5, P):
    """扫一个币的全部历史信号。"""
    vol_ma, lookback = int(P["vol_ma"]), int(P["lookback"])
    vol_mult, reclaim_bars = float(P["vol_mult"]), int(P["reclaim_bars"])
    fw, body_pct = int(P["wyckoff_fractal_window"]), float(P["reclaim_body_pct"]) / 100.0
    min_leg, tol = float(P["min_leg_pct"]) / 100.0, float(P["second_tolerance_pct"]) / 100.0
    min_bars, max_leg_ratio = int(P["min_effective_bars_between"]), float(P["max_entry_leg_ratio"])
    stop_buf, rr = float(P["stop_buffer_pct"]) / 100.0, float(P["rr"])
    use_fvg, sl_anchor = bool(P["use_fvg"]), str(P["sl_anchor"])
    pierce_by, min_gap = str(P["pierce_by"]), float(P["min_gap_pct"])

    if len(k5) < max(80, vol_ma + lookback + 10):
        return []
    fx = find_fractals(k5, merge_klines(k5))
    bottoms = [int(f.extreme_src_idx) for f in fx if f.kind == "bottom"]
    tops = [int(f.extreme_src_idx) for f in fx if f.kind == "top"]

    out, fired, done_first = [], set(), set()

    def emit(direction, first, sec, gap):
        ei = sec["entry_idx"]
        entry = _f(k5[ei], "close")
        if direction == "long":
            anchor_px = sec["L1"] if sl_anchor == "L1" else sec["L2"]
            sl = anchor_px * (1 - stop_buf)
            if sl >= entry:
                return
            tp = entry + rr * (entry - sl)
        else:
            anchor_px = sec["H1"] if sl_anchor == "L1" else sec["H2"]
            sl = anchor_px * (1 + stop_buf)
            if sl <= entry:
                return
            tp = entry - rr * (sl - entry)
        # 入场不能离二买太远(照线上 max_entry_leg_ratio)
        leg = abs(float(sec["H1"]) - float(sec["L1"]))
        detached = entry - float(sec["L2"]) if direction == "long" else float(sec["H2"]) - entry
        if not (leg > 0 and detached <= max_leg_ratio * leg):
            return
        anchor_t = sec["L2_time"] if direction == "long" else sec["H2_time"]
        key = (direction, anchor_t)
        if key in fired:
            return
        fired.add(key)
        row = {
            "strat": "macrofvg", "symbol": sym, "tf": "5m", "direction": direction,
            "type": "second_buy" if direction == "long" else "second_sell",
            "stage": "second_buy" if direction == "long" else "second_sell",
            "created_at": int(sec["entry_time"]) // 1000, "entry_time": int(sec["entry_time"]),
            "entry_idx": ei, "entry": round(entry, 8), "sl": round(sl, 8), "tp": round(tp, 8),
            "rr": rr, "vol_ratio": float(first["vol_ratio"]), "climaxX": float(first["vol_ratio"]),
            "anchor": anchor_t,
            "extra": {"path": "macro_fvg", "wyckoff": first, "structure": sec, "fvg": gap},
        }
        _settle(row, k5)
        out.append(row)

    def seconds(direction, first):
        """L1(或H1)之后, 找出合法的二买/二卖结构。"""
        p1 = int(first["idx"])
        if direction == "long":
            px1 = _f(k5[p1], "low")
            cands = bottoms
        else:
            px1 = _f(k5[p1], "high")
            cands = tops
        for p2 in cands:
            if p2 < p1 + 3 or p2 > p1 + lookback:
                continue
            if direction == "long":
                if _f(k5[p2], "low") < px1 * (1 - tol):      # 二买不能破一买
                    continue
                leg_i = max(range(p1 + 1, p2 + 1), key=lambda x: _f(k5[x], "high"))
                leg_px = _f(k5[leg_i], "high")
                if (leg_px - px1) / max(px1, 1e-12) < min_leg:
                    continue
            else:
                if _f(k5[p2], "high") > px1 * (1 + tol):
                    continue
                leg_i = min(range(p1 + 1, p2 + 1), key=lambda x: _f(k5[x], "low"))
                leg_px = _f(k5[leg_i], "low")
                if (px1 - leg_px) / max(px1, 1e-12) < min_leg:
                    continue
            if _effective_bar_count(k5, p1, leg_i) < min_bars:
                continue
            if _effective_bar_count(k5, leg_i, p2) < min_bars:
                continue

            # —— FVG 门槛: 一买后的上涨一笔(p1..leg_i)内要有FVG, 且二买回落进去不跌穿 ——
            gap = None
            if use_fvg:
                fvgs = find_fvgs(k5, p1, leg_i, direction, min_gap)
                if not fvgs:
                    continue
                gap = fvg_hit(k5, fvgs, p2, leg_i, direction, pierce_by)
                if gap is None:
                    continue

            right, stall, ei = p2 + 1, p2 + 2, p2 + 3
            if ei >= len(k5):
                continue
            if direction == "long":
                if _f(k5[stall], "close") <= _f(k5[right], "high"):    # 需要停顿K
                    continue
                if any(_f(k, "low") < px1 for k in k5[p1 + 1:ei + 1]):  # 期间不得破一买
                    continue
                sec = {"L1": px1, "H1": leg_px, "L2": _f(k5[p2], "low"),
                       "L1_idx": p1, "H1_idx": leg_i, "L2_idx": p2,
                       "L1_time": int(k5[p1]["open_time"]), "L2_time": int(k5[p2]["open_time"])}
            else:
                if _f(k5[stall], "close") >= _f(k5[right], "low"):
                    continue
                if any(_f(k, "high") > px1 for k in k5[p1 + 1:ei + 1]):
                    continue
                sec = {"H1": px1, "L1": leg_px, "H2": _f(k5[p2], "high"),
                       "H1_idx": p1, "L1_idx": leg_i, "H2_idx": p2,
                       "H1_time": int(k5[p1]["open_time"]), "H2_time": int(k5[p2]["open_time"])}
            sec.update(stall_idx=stall, stall_time=int(k5[stall]["open_time"]),
                       entry_idx=ei, entry_time=int(k5[ei]["open_time"]))
            yield sec, gap

    # 爆量扫低/扫高 → 一买/一卖
    for i in range(vol_ma, len(k5) - 4):
        start = max(0, i - lookback)
        if i - start < 3:
            continue
        vr = _vol_ratio(k5, i, vol_ma)
        if vr < vol_mult:
            continue
        prior_low = min(_f(k, "low") for k in k5[start:i])
        if _f(k5[i], "low") < prior_low:
            lvl = _body_reclaim_level(k5[i], "long", body_pct)
            end = min(len(k5) - 1, i + reclaim_bars)
            rec = next((j for j in range(i + 1, end + 1) if _f(k5[j], "close") >= lvl), None)
            if rec is not None:
                near = [x for x in bottoms if abs(x - i) <= fw and rec >= x]
                if near:
                    l1 = min(near, key=lambda x: _f(k5[x], "low"))
                    if l1 not in done_first:
                        done_first.add(l1)
                        first = {"kind": "spring", "idx": l1, "sweep_idx": i, "reclaimed_at": rec,
                                 "level": prior_low, "vol_ratio": round(vr, 2)}
                        for sec, gap in seconds("long", first):
                            emit("long", first, sec, gap)
        prior_high = max(_f(k, "high") for k in k5[start:i])
        if _f(k5[i], "high") > prior_high:
            lvl = _body_reclaim_level(k5[i], "short", body_pct)
            end = min(len(k5) - 1, i + reclaim_bars)
            rec = next((j for j in range(i + 1, end + 1) if _f(k5[j], "close") <= lvl), None)
            if rec is not None:
                near = [x for x in tops if abs(x - i) <= fw and rec >= x]
                if near:
                    h1 = max(near, key=lambda x: _f(k5[x], "high"))
                    if h1 not in done_first:
                        done_first.add(h1)
                        first = {"kind": "utad", "idx": h1, "sweep_idx": i, "reclaimed_at": rec,
                                 "level": prior_high, "vol_ratio": round(vr, 2)}
                        for sec, gap in seconds("short", first):
                            emit("short", first, sec, gap)
    return out


# ------------------------- 变体(A/B用) -------------------------
VARIANTS = {
    # 用户要的正式版: FVG + 止损放一买 + 1:3
    "macrofvg": dict(BASE),
    # 对照组
    "macrofvg_nofvg": dict(BASE, use_fvg=False),                       # 只换出场, 不加FVG
    "macrofvg_slL2": dict(BASE, sl_anchor="L2"),                       # FVG + 止损放二买
    "macrofvg_base": dict(BASE, use_fvg=False, sl_anchor="L2", rr=2.0),  # ≈线上原版
}


def _scan(name):
    P = VARIANTS[name]

    def scan(C):
        out = []
        for sym, k5 in C("5m").items():
            for s in walk(sym, k5, P):
                s["strat"] = name
                out.append(s)
        return out
    return scan


SCANS = {n: _scan(n) for n in VARIANTS}
META = {
    "macrofvg": {"label": "FVG二买二卖·1:3", "tf": "5m", "logic": [
        "线上二买/二卖结构 + FVG门槛 + 新出场(用户 2026-07-11)。",
        "① 爆量扫低K + 缠论底分型 = 一买 L1;上涨一笔到 H1(≥0.8%)",
        "② 上涨一笔中必须出现 FVG(三K缺口): 区间 [前K高, 后K低]",
        "③ 回落出的缠论二买 L2 必须落进该 FVG 区间, 且影线不得跌穿区间下沿",
        "④ L2 后停顿K, 下一根K收盘入场",
        "⑤ 止损 = 一买 L1 低点; 止盈 = 固定 1:3",
        "做空侧对称。",
    ]},
    "macrofvg_nofvg": {"label": "[对照]二买二卖·无FVG·1:3", "tf": "5m",
                       "logic": ["同 macrofvg 但不要求 FVG,用于量化 FVG 门槛的边际贡献"]},
    "macrofvg_slL2": {"label": "[对照]FVG·止损放二买·1:3", "tf": "5m",
                      "logic": ["同 macrofvg 但止损仍放 L2/H2,用于量化止损位的影响"]},
    "macrofvg_base": {"label": "[对照]线上原版(止损L2·RR2)", "tf": "5m",
                      "logic": ["≈线上 macro_pullback,作为基线"]},
}
