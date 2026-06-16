"""弹簧策略回测：复用线上 SignalEngine 状态机逐根回放历史K线（多空同测）。

保证与实盘同一套代码路径，唯一区别是数据来源（REST历史 vs WS实时）。
结算规则与 paper 相同：先看止损后看止盈（同根K双触按止损算，保守）。
"""
import asyncio
import bisect
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


def walk_symbol_mtf(cfg, symbol: str, series_by_tf: dict, btc_lookup, tfs: list[str]) -> list[dict]:
    """多级别联立回放(与实盘 evaluate_all 同一路径)：把各级别收盘事件合并成时间线，
    逐事件用全部级别的当前窗口调用 evaluate_all(5m收盘→5m自身+15m结构; 15m收盘→1h结构)。
    一个 SignalEngine 实例贯穿整条时间线(去重/一买二买链状态与实盘一致)。
    所有信号统一在 5m 序列上结算 TP/SL(最细粒度,最贴近实盘)。CPU密集，放线程跑。"""
    eng = SignalEngine(cfg, _NullDB())
    # 收盘事件 (close_time, -tf_sec, tf, idx)：同一时刻大级别先处理，保证次级触发能看到刚收的高级别K
    events = []
    for tf in tfs:
        sec = TF_MS.get(tf, 900)
        for idx, bar in enumerate(series_by_tf.get(tf, [])):
            events.append((int(bar["open_time"]) + sec * 1000, -sec, tf, idx))
    events.sort()

    closed = {tf: 0 for tf in tfs}
    sigs: list[dict] = []
    for ct, _ns, tf, idx in events:
        closed[tf] = idx + 1
        if tf not in ("5m", "15m"):          # 只有5m/15m收盘会产出信号(见 evaluate_all)
            continue
        if closed[tf] <= WARMUP:
            continue
        kbt = {t: series_by_tf[t][max(0, closed[t] - WINDOW): closed[t]] for t in tfs}
        eng.btc_trend = btc_lookup(ct)
        eng.macro_view = {
            "direction": "long" if eng.btc_trend == 1 else "short" if eng.btc_trend == -1 else "neutral",
            "note": "backtest proxy from BTC trend",
            "at": ct // 1000,
        }
        try:
            out = eng.evaluate_all(symbol, tf, kbt)
        except Exception:
            log.exception("evaluate_all failed %s %s @%d", symbol, tf, ct)
            continue
        for sig in out:
            d = sig.to_db()
            d["created_at"] = ct // 1000
            d["close_time"] = ct
            sigs.append(d)

    # 结算：每个信号从其收盘时刻之后，在 5m 序列上逐根扫 TP/SL(同根双触按止损,保守)
    fine = series_by_tf.get("5m", [])
    opens = [int(b["open_time"]) for b in fine]
    n5 = len(fine)
    for s in sigs:
        s["result"], s["pnl_r"], s["bars_held"] = "open", None, None
        entry, sl, tp = s["entry"], s["sl"], s["tp"]
        risk = abs(entry - sl)
        if risk <= 0:
            continue
        start = bisect.bisect_left(opens, s["close_time"])   # 第一根开盘≥信号收盘的5m(即下一根)
        for j in range(start, n5):
            k = fine[j]
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
                s["bars_held"] = j - start
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
    sem = asyncio.Semaphore(12)
    total = len(symbols)
    done = 0
    all_sigs: list[dict] = []

    # BTC 趋势时间线（btc_filter 开时生效）
    if cfg.get("spring.btc_filter", True):
        btc15 = await fetch_series(rest, "BTCUSDT", "15m", days + 3)
        btc_lookup = build_btc_trend_lookup(btc15)
    else:
        btc_lookup = lambda t: 0

    async def one(sym: str):
        nonlocal done
        series_by_tf: dict = {}
        async with sem:
            for tf in tfs:
                try:
                    series_by_tf[tf] = await fetch_series(rest, sym, tf, days)
                except Exception as e:
                    log.warning("fetch %s %s failed: %s", sym, tf, e)
                    series_by_tf[tf] = []
        if len(series_by_tf.get("5m", [])) > WARMUP + 5:
            sigs = await asyncio.to_thread(walk_symbol_mtf, cfg, sym, series_by_tf, btc_lookup, tfs)
            all_sigs.extend(sigs)
        done += 1
        if progress and (done % 2 == 0 or done == total):
            progress(done, total, sym)

    await asyncio.gather(*(one(s) for s in symbols))
    all_sigs.sort(key=lambda s: s["created_at"])

    snapshot = {k: cfg.get(k) for k in (
        "chan.bi_min_bars", "chan.stall_max_gap", "chan.fractal_vol_mult",
        "chan.fractal_vol_ma", "chan.require_divergence", "chan.mtf_tol_pct",
        "spring.min_rr", "spring.btc_filter", "macro_pullback.enabled",
        "macro_pullback.impulse_min_pct", "macro_pullback.retest_tolerance_pct",
        "macro_pullback.volume_decay_ratio", "macro_pullback.min_rr")}
    result = {
        "period_days": days, "tfs": tfs, "symbols": len(symbols),
        "elapsed_s": round(time.time() - t0, 1),
        "params": snapshot,
        "total": _bucket(all_sigs, lambda s: "all").get("all",
                 {"signals": 0, "closed": 0, "wins": 0, "win_rate": 0, "total_r": 0, "avg_r": 0, "open": 0}),
        "by_tf": _bucket(all_sigs, lambda s: s["tf"]),
        "by_path": _bucket(all_sigs, lambda s: s["extra"].get("path", "?")),
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
