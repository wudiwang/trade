"""独立策略存档:「反转战法」(用户 2026-06-16,多空都识别)。

规格见 docs/strat_spring_reclaim.md。核心:
  背景(1h): 深跌(做多)/ 深涨(做空) + 低位/高位 + 趋势配合
  扳机(5m): 爆量「标志K」插穿前低/前高 → 收盘收回到标志K「起跌位(开盘价)」之上/之下
  企稳: 收回后横盘 hold_bars 根不破针尖、不顺势 → Entry-1(轻仓)
  加仓: 平台内出现底分型(做多)/顶分型(做空) → Entry-2(加仓)
  失效: 任意收盘越过针尖(P_spring) → 作废

v1 用 5m 验逻辑;起跌位/针尖取 5m 标志K(1m 精修为后续精度升级)。
完全独立,不接实盘、不改引擎,仅回测。回测只做多(detect 支持多空)。
"""
import asyncio


def _arr(k):
    return ([float(x["open"]) for x in k], [float(x["high"]) for x in k],
            [float(x["low"]) for x in k], [float(x["close"]) for x in k],
            [float(x["volume"]) for x in k])


def deep_context(k1h: list, direction: str, w1: int = 48, drop_min: float = 0.30,
                 pos_max: float = 0.5, ma_1h: int = 50) -> bool:
    """1h 背景:做多=深跌+低位+趋势下行;做空=深涨+高位+趋势上行。"""
    if len(k1h) < max(w1, ma_1h + 6):
        return False
    win = k1h[-w1:]
    hi = max(float(k["high"]) for k in win)
    lo = min(float(k["low"]) for k in win)
    close = float(k1h[-1]["close"])
    if hi <= 0 or lo <= 0 or hi <= lo:
        return False
    closes = [float(k["close"]) for k in k1h]
    ma_now = sum(closes[-ma_1h:]) / ma_1h
    ma_prev = sum(closes[-ma_1h - 5:-5]) / ma_1h
    pos = (close - lo) / (hi - lo)
    if direction == "long":
        drop = (hi - lo) / hi
        return drop >= drop_min and pos <= pos_max and close < ma_now and ma_now < ma_prev
    else:
        rise = (hi - lo) / lo
        return rise >= drop_min and pos >= (1 - pos_max) and close > ma_now and ma_now > ma_prev


def _ctx_timeline(k1h: list, direction: str, P: dict):
    """预计算 1h 背景命中时间线: (close_time_ms, ok)。"""
    times, oks = [], []
    for j in range(max(P["w1"], P["ma_1h"] + 6), len(k1h)):
        times.append(int(k1h[j]["open_time"]) + 3600 * 1000)
        oks.append(deep_context(k1h[:j + 1], direction, P["w1"], P["drop_min"],
                                P["pos_max"], P["ma_1h"]))
    import bisect

    def lookup(t_ms):
        i = bisect.bisect_right(times, t_ms) - 1
        return oks[i] if i >= 0 else False
    return lookup


def detect_reversal(k5: list, ctx_lookup, direction: str, P: dict) -> list[dict]:
    """事件式扫描整段 5m,返回该方向的 Entry-1/Entry-2 信号(未结算)。无前视。"""
    n = len(k5)
    o, h, l, c, v = _arr(k5)
    long = direction == "long"
    sigs = []
    vol_ma = P["vol_ma"]
    cap_lb = P["cap_lookback"]
    i = max(vol_ma, cap_lb)
    while i < n - 1:
        # --- 标志K判定 ---
        avgv = sum(v[i - vol_ma:i]) / vol_ma
        is_cli = False
        if avgv > 0 and v[i] >= avgv * P["climax_mult"]:
            if long and c[i] < o[i] and l[i] <= min(l[i - cap_lb:i + 1]):
                is_cli = True
            if (not long) and c[i] > o[i] and h[i] >= max(h[i - cap_lb:i + 1]):
                is_cli = True
        if not is_cli:
            i += 1
            continue
        if not ctx_lookup(int(k5[i]["open_time"]) + 300 * 1000, direction):
            i += 1
            continue
        P_start = o[i]                       # 起跌位 = 标志K开盘
        P_spring = l[i] if long else h[i]    # 针尖 = 标志K极值(止损基准)

        # --- 收回起跌位(收盘价) ---
        r = None
        for j in range(i + 1, min(n, i + 1 + P["reclaim_bars"])):
            if long and c[j] < P_spring:
                break
            if (not long) and c[j] > P_spring:
                break
            if long and c[j] > P_start:
                r = j; break
            if (not long) and c[j] < P_start:
                r = j; break
        if r is None:
            i += 1
            continue

        # --- 横盘企稳 hold_bars 根 → Entry-1 ---
        end = r + P["hold_bars"]
        if end >= n:
            break
        hold_ok = True
        for j in range(r + 1, end + 1):
            if long and c[j] < P_spring:
                hold_ok = False; break
            if (not long) and c[j] > P_spring:
                hold_ok = False; break
            if abs(c[j] - P_start) / P_start > P["hold_tol_pct"] / 100.0:
                hold_ok = False; break
        if not hold_ok:
            i = r + 1
            continue

        anchor = int(k5[i]["open_time"])
        buf = P["sl_buf_pct"] / 100.0
        rr = P["rr_target"]

        def mk(stage, idx, sl):
            entry = c[idx]
            risk = (entry - sl) if long else (sl - entry)
            if risk <= 0:
                return None
            tp = entry + rr * risk if long else entry - rr * risk
            return {"direction": direction, "stage": stage, "entry": entry, "sl": sl,
                    "tp": tp, "anchor": anchor, "entry_idx": idx,
                    "created_at": int(k5[idx]["open_time"]) // 1000,
                    "P_start": P_start, "P_spring": P_spring}

        sl1 = P_spring * (1 - buf) if long else P_spring * (1 + buf)
        s1 = mk("entry1", end, sl1)
        if s1:
            sigs.append(s1)

        # --- 平台底/顶分型 → Entry-2(原始3K分型,确认在中间K的下一根) ---
        e2_idx = None
        f_lo = None
        for k in range(end, min(n - 1, end + P["platform_window"])):
            if k - 1 < 0:
                continue
            if long and l[k] < l[k - 1] and l[k] < l[k + 1] and l[k] >= P_spring:
                e2_idx = k + 1; f_lo = l[k]; break
            if (not long) and h[k] > h[k - 1] and h[k] > h[k + 1] and h[k] <= P_spring:
                e2_idx = k + 1; f_lo = h[k]; break
        if e2_idx is not None and e2_idx < n:
            sl2 = (f_lo * (1 - buf)) if long else (f_lo * (1 + buf))
            s2 = mk("entry2", e2_idx, sl2)
            if s2:
                sigs.append(s2)

        i = max(end, e2_idx or end) + 1
    return sigs


# ------------------------- 回测 -------------------------
def _settle(s, series):
    s["result"], s["pnl_r"], s["bars_held"] = "open", None, None
    entry, sl, tp = s["entry"], s["sl"], s["tp"]
    risk = abs(entry - sl)
    if risk <= 0:
        return
    long = s["direction"] == "long"
    for j in range(s["entry_idx"] + 1, len(series)):
        lo, hi = float(series[j]["low"]), float(series[j]["high"])
        if long:
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
            s["bars_held"] = j - s["entry_idx"]
            break


def _bucket(rows, keyfn):
    out = {}
    for s in rows:
        b = out.setdefault(keyfn(s), {"signals": 0, "closed": 0, "wins": 0,
                                      "total_r": 0.0, "open": 0, "rr_sum": 0.0})
        b["signals"] += 1
        b["rr_sum"] += abs(s["tp"] - s["entry"]) / abs(s["entry"] - s["sl"]) if s["entry"] != s["sl"] else 0
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
        b["avg_rr"] = round(b["rr_sum"] / b["signals"], 2) if b["signals"] else 0.0
        b["total_r"] = round(b["total_r"], 2)
    return out


def _walk(sym, k5, k1h, directions, P):
    sigs = []
    for d in directions:
        lookup = _ctx_timeline(k1h, d, P)
        for s in detect_reversal(k5, lookup, d, P):
            s["symbol"] = sym
            _settle(s, k5)
            sigs.append(s)
    return sigs


async def run_reversal_backtest(rest, symbols, days=7, directions=("long",), rr_target=2.0,
                                w1=48, drop_min=0.30, pos_max=0.5, ma_1h=50,
                                cap_lookback=30, vol_ma=20, climax_mult=3.0,
                                reclaim_bars=6, hold_bars=4, hold_tol_pct=3.0,
                                platform_window=20, sl_buf_pct=0.3, progress=None):
    from .backtest import fetch_series
    import time as _t
    t0 = _t.time()
    P = dict(w1=w1, drop_min=drop_min, pos_max=pos_max, ma_1h=ma_1h, cap_lookback=cap_lookback,
             vol_ma=vol_ma, climax_mult=climax_mult, reclaim_bars=reclaim_bars,
             hold_bars=hold_bars, hold_tol_pct=hold_tol_pct, platform_window=platform_window,
             sl_buf_pct=sl_buf_pct, rr_target=rr_target)
    sem = asyncio.Semaphore(5)
    alls = []
    done = [0]

    async def one(sym):
        async with sem:
            try:
                k5 = await fetch_series(rest, sym, "5m", days)
                k1h = await fetch_series(rest, sym, "1h", days + 6)
            except Exception:
                k5, k1h = [], []
        if len(k5) >= 80 and len(k1h) >= ma_1h + 10:
            alls.extend(await asyncio.to_thread(_walk, sym, k5, k1h, list(directions), P))
        done[0] += 1
        if progress and done[0] % 10 == 0:
            progress(done[0], len(symbols), sym)

    await asyncio.gather(*(one(s) for s in symbols))
    return {"period_days": days, "symbols": len(symbols), "directions": list(directions),
            "params": P, "elapsed_s": round(_t.time() - t0, 1), "n_signals": len(alls),
            "by_stage": _bucket(alls, lambda s: f"{s['direction']}/{s['stage']}"),
            "total": _bucket(alls, lambda s: "all").get("all", {}),
            "samples": [{"t": s["created_at"], "sym": s["symbol"], "dir": s["direction"],
                         "stage": s["stage"], "entry": round(s["entry"], 6),
                         "sl": round(s["sl"], 6), "tp": round(s["tp"], 6),
                         "result": s["result"], "pnl_r": s["pnl_r"], "bars": s["bars_held"]}
                        for s in alls[-40:]]}
