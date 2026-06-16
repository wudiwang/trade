"""独立策略存档：「深跌后企稳」形态(用户 2026-06-16 定义，多空都跑)。

形态(多时间框架)：
  A 层 · 1h「跌了很多」：近 w1 根1h内 (高-低)/高 ≥ drop_min，且现价位于该区间下部
        (pos ≤ pos_max)，且 收盘 < 1h MA(ma_1h) 且该均线斜率向下。
  B 层 · 15m「急跌见底 + 企稳」：
        ① 恐慌低点 = 近 cap_lookback 根内的最低低点，且该根放量 ≥ 均量×cap_vol_mult；
        ② 低点企稳 = 该低点后 ≥ hold_min 根不创新低；
        ③ 波动收缩 = 近10根ATR < 急跌段ATR × contract_ratio；
        ④ 短均收敛走平 = |MA7-MA25|/价 ≤ conv_eps 且 MA7 斜率近 0。
  A 且 B → 命中。入场=当前15m收盘。
  做多：止损=企稳区间低，止盈=入场+rr_target×风险；
  做空：止损=企稳区间高，止盈=入场-rr_target×风险。

完全独立，不接实盘、不改引擎。仅供单独回测与逐项检查。
"""
import asyncio
import bisect


def _sma(vals, n):
    return sum(vals[-n:]) / n if len(vals) >= n else None


def _atr(kl, n):
    trs = []
    for i in range(max(1, len(kl) - n), len(kl)):
        h, l = float(kl[i]["high"]), float(kl[i]["low"])
        pc = float(kl[i - 1]["close"])
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    return sum(trs) / len(trs) if trs else 0.0


def deep_drop_1h(k1h: list, w1: int = 48, drop_min: float = 0.30,
                 pos_max: float = 0.5, ma_1h: int = 50) -> dict | None:
    """A 层：1h 是否「跌了很多 + 现价处于低位 + 趋势向下」。"""
    if len(k1h) < max(w1, ma_1h + 6):
        return None
    win = k1h[-w1:]
    hi = max(float(k["high"]) for k in win)
    lo = min(float(k["low"]) for k in win)
    close = float(k1h[-1]["close"])
    if hi <= 0 or hi <= lo:
        return None
    drop = (hi - lo) / hi
    pos = (close - lo) / (hi - lo)
    closes = [float(k["close"]) for k in k1h]
    ma_now = sum(closes[-ma_1h:]) / ma_1h
    ma_prev = sum(closes[-ma_1h - 5:-5]) / ma_1h
    if drop >= drop_min and pos <= pos_max and close < ma_now and ma_now < ma_prev:
        return {"drop": round(drop, 3), "pos": round(pos, 3)}
    return None


def base_15m(k15: list, cap_lookback: int = 30, cap_vol_mult: float = 3.0,
             hold_min: int = 10, contract_ratio: float = 0.7,
             conv_eps: float = 0.02) -> dict | None:
    """B 层：15m 急跌见底 + 企稳。返回企稳区间信息或 None。"""
    n = len(k15)
    if n < 60:
        return None
    closes = [float(k["close"]) for k in k15]
    vols = [float(k["volume"]) for k in k15]
    lows = [float(k["low"]) for k in k15]
    # ① 恐慌低点：近 cap_lookback 根最低低点
    seg_start = n - cap_lookback
    cap_idx = min(range(seg_start, n), key=lambda i: lows[i])
    if cap_idx < 20 or cap_idx >= n - 1:
        return None
    cap_low = lows[cap_idx]
    avgv = sum(vols[cap_idx - 20:cap_idx]) / 20
    if avgv <= 0 or vols[cap_idx] < avgv * cap_vol_mult:   # 该低点要放量
        return None
    # ② 低点企稳：之后 ≥ hold_min 根且不创新低
    if (n - 1 - cap_idx) < hold_min:
        return None
    if min(lows[cap_idx + 1:]) < cap_low:
        return None
    # ③ 波动收缩：近10根 ATR < 急跌段 ATR × ratio
    atr_recent = _atr(k15, 10)
    atr_drop = _atr(k15[:cap_idx + 1], 10)
    if atr_drop <= 0 or atr_recent > atr_drop * contract_ratio:
        return None
    # ④ 短均收敛走平
    ma7 = sum(closes[-7:]) / 7
    ma25 = sum(closes[-25:]) / 25
    if abs(ma7 - ma25) / closes[-1] > conv_eps:
        return None
    ma7_prev = sum(closes[-12:-5]) / 7
    if abs(ma7 - ma7_prev) / closes[-1] > conv_eps:
        return None
    rng = k15[cap_idx:]
    rh = max(float(k["high"]) for k in rng)
    rl = min(float(k["low"]) for k in rng)
    return {"cap_low": cap_low, "cap_idx": cap_idx, "anchor": int(k15[cap_idx]["open_time"]),
            "range_high": rh, "range_low": rl, "atr_recent": round(atr_recent, 8)}


def make_signals(k15: list, base: dict, rr_target: float = 2.0) -> list[dict]:
    """命中形态后生成 多/空 两个信号(入场=当前15m收盘)。"""
    entry = float(k15[-1]["close"])
    rl, rh = base["range_low"], base["range_high"]
    out = []
    # 做多：止损=企稳区间低
    risk_l = entry - rl
    if risk_l > 0:
        out.append({"direction": "long", "entry": entry, "sl": rl,
                    "tp": entry + rr_target * risk_l})
    # 做空：止损=企稳区间高
    risk_s = rh - entry
    if risk_s > 0:
        out.append({"direction": "short", "entry": entry, "sl": rh,
                    "tp": entry - rr_target * risk_s})
    for s in out:
        s["anchor"] = base["anchor"]
        s["cap_low"] = base["cap_low"]
    return out


# ------------------------- 回测 -------------------------
def _settle(s: dict, series: list, entry_idx: int):
    s["result"], s["pnl_r"], s["bars_held"] = "open", None, None
    entry, sl, tp = s["entry"], s["sl"], s["tp"]
    risk = abs(entry - sl)
    if risk <= 0:
        return
    for j in range(entry_idx + 1, len(series)):
        lo, hi = float(series[j]["low"]), float(series[j]["high"])
        if s["direction"] == "long":
            if lo <= sl:
                s["result"], s["pnl_r"] = "sl", -1.0
            elif hi >= tp:
                s["result"], s["pnl_r"] = "tp", (tp - entry) / risk
        else:
            if hi >= sl:
                s["result"], s["pnl_r"] = "sl", -1.0
            elif lo <= tp:
                s["result"], s["pnl_r"] = "tp", (entry - tp) / risk
        if s["result"] != "open":
            s["bars_held"] = j - entry_idx
            break


def _bucket(rows: list):
    out = {}
    for s in rows:
        b = out.setdefault(s["direction"], {"signals": 0, "closed": 0, "wins": 0,
                                            "total_r": 0.0, "open": 0})
        b["signals"] += 1
        if s["result"] == "open":
            b["open"] += 1
        else:
            b["closed"] += 1
            b["total_r"] += s["pnl_r"]
            if s["result"] == "tp":
                b["wins"] += 1
    for b in out.values():
        b["win_rate"] = round(b["wins"] / b["closed"] * 100, 1) if b["closed"] else 0.0
        b["expectancy_r"] = round(b["total_r"] / b["closed"], 3) if b["closed"] else 0.0
        b["total_r"] = round(b["total_r"], 2)
    return out


def _walk(sym, k15, k1h, params):
    """1h 状态时间线 + 15m 逐根。返回信号列表(已结算)。"""
    # 预计算 1h 命中时间线
    h_times, h_ok = [], []
    ma_1h = params["ma_1h"]
    for j in range(max(params["w1"], ma_1h + 6), len(k1h)):
        h_times.append(int(k1h[j]["open_time"]) + 3600 * 1000)   # 该1h收盘时刻
        h_ok.append(deep_drop_1h(k1h[:j + 1], params["w1"], params["drop_min"],
                                 params["pos_max"], ma_1h) is not None)

    def hot_1h(t_close_ms):
        k = bisect.bisect_right(h_times, t_close_ms) - 1
        return h_ok[k] if k >= 0 else False

    sigs = []
    seen = set()
    WIN = 120
    for i in range(60, len(k15)):
        t_close = int(k15[i]["open_time"]) + 900 * 1000
        if not hot_1h(t_close):
            continue
        win15 = k15[max(0, i - WIN): i + 1]
        base = base_15m(win15, params["cap_lookback"], params["cap_vol_mult"],
                        params["hold_min"], params["contract_ratio"], params["conv_eps"])
        if not base:
            continue
        for s in make_signals(win15, base, params["rr_target"]):
            key = (s["direction"], s["anchor"])
            if key in seen:
                continue
            seen.add(key)
            s["symbol"] = sym
            s["entry_idx"] = i
            s["created_at"] = int(k15[i]["open_time"]) // 1000
            _settle(s, k15, i)
            sigs.append(s)
    return sigs


async def run_deepbase_backtest(rest, symbols: list, days: int = 7, rr_target: float = 2.0,
                                w1: int = 48, drop_min: float = 0.30, pos_max: float = 0.5,
                                ma_1h: int = 50, cap_lookback: int = 30, cap_vol_mult: float = 3.0,
                                hold_min: int = 10, contract_ratio: float = 0.7,
                                conv_eps: float = 0.02, progress=None) -> dict:
    from .backtest import fetch_series
    import time as _t
    t0 = _t.time()
    params = dict(w1=w1, drop_min=drop_min, pos_max=pos_max, ma_1h=ma_1h,
                  cap_lookback=cap_lookback, cap_vol_mult=cap_vol_mult, hold_min=hold_min,
                  contract_ratio=contract_ratio, conv_eps=conv_eps, rr_target=rr_target)
    sem = asyncio.Semaphore(5)
    all_sigs = []
    done = [0]

    async def one(sym):
        async with sem:
            try:
                k15 = await fetch_series(rest, sym, "15m", days)
                k1h = await fetch_series(rest, sym, "1h", days + 6)   # 1h 多拉历史给 MA/区间
            except Exception:
                k15, k1h = [], []
        if len(k15) >= 80 and len(k1h) >= ma_1h + 10:
            sigs = await asyncio.to_thread(_walk, sym, k15, k1h, params)
            all_sigs.extend(sigs)
        done[0] += 1
        if progress and done[0] % 10 == 0:
            progress(done[0], len(symbols), sym)

    await asyncio.gather(*(one(s) for s in symbols))
    by_dir = _bucket(all_sigs)
    total = _bucket([dict(s, direction="all") for s in all_sigs]).get("all", {})
    return {"period_days": days, "symbols": len(symbols), "rr_target": rr_target,
            "params": params, "elapsed_s": round(_t.time() - t0, 1),
            "n_signals": len(all_sigs), "total": total, "by_direction": by_dir,
            "samples": [{"t": s["created_at"], "sym": s["symbol"], "dir": s["direction"],
                         "entry": round(s["entry"], 6), "sl": round(s["sl"], 6),
                         "tp": round(s["tp"], 6), "result": s["result"],
                         "pnl_r": s["pnl_r"], "bars": s["bars_held"]} for s in all_sigs[-40:]]}
