"""全新独立策略：5分钟「三笔浅回调」二买/二卖（用户 2026-06-16 定义）。

结构(做多·二买)：
  下跌笔 T0→B1  →  反弹笔 B1→T2  →  再次下跌 T2→B2(不破B1新低)
  且 回调幅度 (T2-B2)/(T2-B1) ≤ pullback_max(默认50%)
  且 B2 形成新底分型(免停顿) → 形成后最新5mK收盘买入
  止损 = B2 底分型最低；止盈 = T2 反弹高点。
做空·二卖：镜像(上涨笔→回调笔→再涨不破新高→顶分型卖；止损=顶分型高，止盈=回调低)。

与现有 chan_bi 策略完全独立，不复用其分型质量/放量/停顿/背驰过滤——
只看「三笔结构 + 不破新低/高 + 浅回调 + 分型」这一套。
"""
from .chan_bi import build_bi


def detect_pullback_2nd(klines: list, min_merged: int = 5, pullback_max: float = 0.5):
    """在给定窗口末端检测一个二买/二卖结构。返回 dict 或 None。
    dict: direction/entry/sl/tp/retrace/struct_anchor/fx_anchor/prior_extreme。
    entry = 窗口最后一根K收盘(=形成后最新K买入)。"""
    merged, seq = build_bi(klines, min_merged)
    if len(seq) < 4:
        return None
    f1, f2, f3, f4 = seq[-4], seq[-3], seq[-2], seq[-1]
    entry = float(klines[-1]["close"])

    if f4.kind == "bottom":
        # 做多二买： T0(top) B1(bottom) T2(top) B2(bottom=末端)
        T0, B1, T2, B2 = f1, f2, f3, f4
        if not (T0.kind == "top" and B1.kind == "bottom" and T2.kind == "top"):
            return None
        if B2.extreme_price <= B1.extreme_price:      # 必须不破前低(更高的低点)
            return None
        up = T2.extreme_price - B1.extreme_price      # 反弹一笔幅度
        drop = T2.extreme_price - B2.extreme_price    # 回调幅度
        if up <= 0 or drop <= 0 or drop > pullback_max * up:
            return None
        sl, tp = B2.extreme_price, T2.extreme_price
        if not (sl < entry < tp):
            return None
        return {"direction": "long", "entry": entry, "sl": sl, "tp": tp,
                "retrace": round(drop / up, 3),
                "struct_anchor": int(T2.open_time), "fx_anchor": int(B2.open_time),
                "prior_extreme": B1.extreme_price}

    else:
        # 做空二卖： B0(bottom) T1(top) B2(bottom) T2(top=末端)
        B0, T1, B2, T2 = f1, f2, f3, f4
        if not (B0.kind == "bottom" and T1.kind == "top" and B2.kind == "bottom"):
            return None
        if T2.extreme_price >= T1.extreme_price:      # 必须不破前高(更低的高点)
            return None
        down = T1.extreme_price - B2.extreme_price    # 回调一笔幅度
        rise = T2.extreme_price - B2.extreme_price    # 再次上涨幅度
        if down <= 0 or rise <= 0 or rise > pullback_max * down:
            return None
        sl, tp = T2.extreme_price, B2.extreme_price
        if not (tp < entry < sl):
            return None
        return {"direction": "short", "entry": entry, "sl": sl, "tp": tp,
                "retrace": round(rise / down, 3),
                "struct_anchor": int(B2.open_time), "fx_anchor": int(T2.open_time),
                "prior_extreme": T1.extreme_price}


# ------------------------- 回测 -------------------------
import asyncio
import bisect


def _settle(s: dict, series: list, opens: list, entry_idx: int):
    """从入场下一根开始扫 TP/SL(同根双触按止损,保守)。写入 result/pnl_r/bars_held。"""
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


def _walk_symbol(sym: str, series: list, min_merged: int, pullback_max: float, window: int = 160):
    n = len(series)
    seen = set()
    sigs = []
    for i in range(60, n):
        win = series[max(0, i - window): i + 1]
        sig = detect_pullback_2nd(win, min_merged, pullback_max)
        if not sig:
            continue
        key = (sig["direction"], sig["struct_anchor"])
        if key in seen:
            continue
        seen.add(key)
        sig["symbol"] = sym
        sig["entry_idx"] = i
        sig["created_at"] = int(series[i]["open_time"]) // 1000
        sigs.append(sig)
    opens = [int(b["open_time"]) for b in series]
    for s in sigs:
        _settle(s, series, opens, s["entry_idx"])
    return sigs


def _bucket(rows: list):
    out = {}
    for s in rows:
        b = out.setdefault(s["direction"], {"signals": 0, "closed": 0, "wins": 0,
                                            "total_r": 0.0, "open": 0, "rr_sum": 0.0})
        b["signals"] += 1
        b["rr_sum"] += (abs(s["tp"] - s["entry"]) / abs(s["entry"] - s["sl"])) if s["entry"] != s["sl"] else 0
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


async def run_pullback_backtest(rest, symbols: list, days: int = 7,
                                min_merged: int = 5, pullback_max: float = 0.5,
                                progress=None) -> dict:
    from .backtest import fetch_series
    import time as _t
    t0 = _t.time()
    sem = asyncio.Semaphore(6)
    all_sigs = []
    done = [0]

    async def one(sym):
        async with sem:
            try:
                series = await fetch_series(rest, sym, "5m", days)
            except Exception:
                series = []
        if len(series) >= 80:
            sigs = await asyncio.to_thread(_walk_symbol, sym, series, min_merged, pullback_max)
            all_sigs.extend(sigs)
        done[0] += 1
        if progress and done[0] % 10 == 0:
            progress(done[0], len(symbols), sym)

    await asyncio.gather(*(one(s) for s in symbols))
    by_dir = _bucket(all_sigs)
    total = _bucket([dict(s, direction="all") for s in all_sigs]).get("all", {})
    return {"period_days": days, "symbols": len(symbols), "pullback_max": pullback_max,
            "min_merged": min_merged, "elapsed_s": round(_t.time() - t0, 1),
            "total": total, "by_direction": by_dir,
            "n_signals": len(all_sigs),
            "samples": [{"t": s["created_at"], "sym": s["symbol"], "dir": s["direction"],
                         "entry": s["entry"], "sl": s["sl"], "tp": s["tp"],
                         "retrace": s["retrace"], "result": s["result"],
                         "pnl_r": s["pnl_r"], "bars": s["bars_held"]} for s in all_sigs[-40:]]}
