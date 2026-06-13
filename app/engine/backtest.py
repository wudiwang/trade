"""弹簧策略回测：复用线上 SignalEngine 状态机逐根回放历史K线（多空同测）。

保证与实盘同一套代码路径，唯一区别是数据来源（REST历史 vs WS实时）。
结算规则与 paper 相同：先看止损后看止盈（同根K双触按止损算，保守）。
"""
import asyncio
import logging
import time

from .chan import trend_direction
from .signals import TF_MS, SignalEngine

log = logging.getLogger("backtest")

COLS = ("open_time", "open", "high", "low", "close", "volume", "quote_volume", "taker_buy", "closed")
WINDOW = 260          # 状态机所需回看窗口(>=VP200+余量)
WARMUP = 60


class _NullDB:
    def log(self, *a, **kw):
        pass


async def fetch_series(rest, symbol: str, tf: str, days: int) -> list[dict]:
    """分页拉取 days 天的已收盘K线。"""
    tf_ms = TF_MS[tf] * 1000
    end = int(time.time() * 1000)
    cur = end - days * 86400 * 1000 - WARMUP * tf_ms   # 多拉预热段
    out: list[dict] = []
    while cur < end:
        batch = await rest.klines(symbol, tf, limit=1500, start_time=cur)
        if not batch:
            break
        out.extend(dict(zip(COLS, b)) for b in batch if b[8] == 1)
        nxt = int(batch[-1][0]) + tf_ms
        if nxt <= cur or len(batch) < 2:
            break
        cur = nxt
        if len(batch) < 1500:
            break
    # 去重排序
    seen = {}
    for k in out:
        seen[k["open_time"]] = k
    return [seen[t] for t in sorted(seen)]


def build_btc_trend_lookup(btc15: list[dict], ema_period: int = 50):
    """预计算 BTC 趋势时间线: open_time(ms) -> -1/0/1。"""
    times, trends = [], []
    for i in range(ema_period * 4 + 10, len(btc15)):
        times.append(int(btc15[i]["open_time"]))
        trends.append(trend_direction(btc15[: i + 1], ema_period))

    def lookup(t_ms: int) -> int:
        import bisect
        j = bisect.bisect_right(times, t_ms) - 1
        return trends[j] if j >= 0 else 0
    return lookup


def walk_symbol(cfg, symbol: str, tf: str, series: list[dict], btc_lookup) -> list[dict]:
    """逐根回放一条K线序列，返回带结算结果的信号列表。CPU密集，放线程跑。"""
    eng = SignalEngine(cfg, _NullDB())
    sigs: list[dict] = []
    n = len(series)
    for i in range(WARMUP, n):
        window = series[max(0, i - WINDOW + 1): i + 1]
        eng.btc_trend = btc_lookup(int(series[i]["open_time"]))
        try:
            sig = eng.evaluate(symbol, tf, window)
        except Exception:
            log.exception("evaluate failed %s %s @%d", symbol, tf, i)
            continue
        if not sig:
            continue
        d = sig.to_db()
        d["bar_idx"] = i
        d["created_at"] = int(series[i]["open_time"]) // 1000
        sigs.append(d)

    # 结算：从信号K之后逐根扫TP/SL
    for s in sigs:
        s["result"], s["pnl_r"], s["bars_held"] = "open", None, None
        entry, sl, tp = s["entry"], s["sl"], s["tp"]
        risk = abs(entry - sl)
        if risk <= 0:
            continue
        for j in range(s["bar_idx"] + 1, n):
            k = series[j]
            lo, hi = float(k["low"]), float(k["high"])
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
                s["bars_held"] = j - s["bar_idx"]
                break
    return sigs


def _bucket(rows: list[dict], keyfn) -> dict:
    out: dict = {}
    for s in rows:
        k = keyfn(s)
        b = out.setdefault(k, {"signals": 0, "closed": 0, "wins": 0, "total_r": 0.0, "open": 0})
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
        b["avg_r"] = round(b["total_r"] / b["closed"], 3) if b["closed"] else 0.0
        b["total_r"] = round(b["total_r"], 2)
    return out


async def run_backtest(cfg, rest, symbols: list[str], tfs: list[str], days: int,
                       progress=None) -> dict:
    """全量回测。progress(done, total, msg) 可选回调。"""
    t0 = time.time()
    sem = asyncio.Semaphore(5)
    total = len(symbols) * len(tfs)
    done = 0
    all_sigs: list[dict] = []

    # BTC 趋势时间线（btc_filter 开时生效）
    if cfg.get("spring.btc_filter", True):
        btc15 = await fetch_series(rest, "BTCUSDT", "15m", days + 3)
        btc_lookup = build_btc_trend_lookup(btc15)
    else:
        btc_lookup = lambda t: 0

    async def one(sym: str, tf: str):
        nonlocal done
        async with sem:
            try:
                series = await fetch_series(rest, sym, tf, days)
            except Exception as e:
                log.warning("fetch %s %s failed: %s", sym, tf, e)
                series = []
        if len(series) > WARMUP + 5:
            sigs = await asyncio.to_thread(walk_symbol, cfg, sym, tf, series, btc_lookup)
            all_sigs.extend(sigs)
        done += 1
        if progress and (done % 10 == 0 or done == total):
            progress(done, total, f"{sym} {tf}")

    await asyncio.gather(*(one(s, tf) for s in symbols for tf in tfs))
    all_sigs.sort(key=lambda s: s["created_at"])

    snapshot = {k: cfg.get(k) for k in (
        "spring.vol_mult", "spring.newlow_lookback", "spring.body_min",
        "spring.fractal_window", "spring.buy2_window", "spring.tp_lookback",
        "spring.min_rr", "spring.btc_filter")}
    result = {
        "period_days": days, "tfs": tfs, "symbols": len(symbols),
        "elapsed_s": round(time.time() - t0, 1),
        "params": snapshot,
        "total": _bucket(all_sigs, lambda s: "all").get("all",
                 {"signals": 0, "closed": 0, "wins": 0, "win_rate": 0, "total_r": 0, "avg_r": 0, "open": 0}),
        "by_tf": _bucket(all_sigs, lambda s: s["tf"]),
        "by_type": _bucket(all_sigs, lambda s: s["extra"].get("type", "?")),
        "by_direction": _bucket(all_sigs, lambda s: s["direction"]),
        "signals": [
            {"time": s["created_at"], "symbol": s["symbol"], "tf": s["tf"],
             "direction": s["direction"], "type": s["extra"].get("type"),
             "score": s["extra"].get("score"),
             "entry": s["entry"], "sl": s["sl"], "tp": s["tp"], "rr": s["rr"],
             "result": s["result"], "pnl_r": s["pnl_r"], "bars_held": s["bars_held"]}
            for s in all_sigs[-300:]
        ],
    }
    return result
