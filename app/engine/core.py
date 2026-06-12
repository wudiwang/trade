"""引擎主循环：币种管理 → 回填 → WS实时 → 收盘评估 → 信号入库/推送 → paper结算。"""
import asyncio
import collections
import logging
import time
from typing import Awaitable, Callable

from .binance_rest import BinanceRest
from .binance_ws import KlineWS
from .paper import PaperBroker
from .signals import SignalEngine

log = logging.getLogger("core")

COLS = ("open_time", "open", "high", "low", "close", "volume", "quote_volume", "taker_buy", "closed")


class Engine:
    def __init__(self, cfg, db):
        self.cfg = cfg
        self.db = db
        self.rest = BinanceRest(cfg.get("binance.rest_base"), cfg.binance_key, cfg.binance_secret)
        self.signal_engine = SignalEngine(cfg, db)
        self.paper = PaperBroker(cfg, db)
        self.ws = KlineWS(cfg.get("binance.ws_base"), self.on_closed_bar)
        # 内存K线缓存 (symbol, tf) -> deque[dict]
        self.cache: dict[tuple, collections.deque] = {}
        self.symbols: list[str] = []
        # 新信号订阅者（telegram bot、web 推送都挂这里）
        self.signal_subscribers: list[Callable[[int, object], Awaitable[None]]] = []
        self.trade_close_subscribers: list[Callable[[dict], Awaitable[None]]] = []
        self.notice_subscribers: list[Callable[[str], Awaitable[None]]] = []
        self._tasks: list[asyncio.Task] = []
        self.started_at = 0
        self.last_eval_ms = 0.0

    # ---------- 生命周期 ----------
    async def start(self) -> None:
        self.started_at = int(time.time())
        await self.refresh_universe()
        await self.backfill_all()
        btc15 = self.cache.get(("BTCUSDT", "15m"))
        if btc15:
            from .chan import trend_direction
            self.signal_engine.btc_trend = trend_direction(
                list(btc15), self.cfg.get("signal.trend_ema_period", 50))
        await self.ws.start(self.symbols, self.cfg.timeframes)
        self._tasks.append(asyncio.create_task(self._universe_loop()))
        self._tasks.append(asyncio.create_task(self._watchdog_loop()))
        self._tasks.append(asyncio.create_task(self._maintenance_loop()))
        self._tasks.append(asyncio.create_task(self._funding_loop()))
        log.info("engine started: %d symbols, tfs=%s", len(self.symbols), self.cfg.timeframes)
        self.db.log("info", "engine", f"started with {len(self.symbols)} symbols")

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        await self.ws.stop()
        await self.rest.close()

    # ---------- 币种 ----------
    async def refresh_universe(self) -> None:
        syms = await self.rest.usdt_perp_symbols()
        vols = await self.rest.ticker_24h()
        min_vol = self.cfg.get("universe.min_quote_volume_24h", 5e7)
        exclude = set(self.cfg.get("universe.exclude", []) or [])
        rows = []
        for s in syms:
            qv = vols.get(s["symbol"], 0.0)
            enabled = 1 if qv >= min_vol and s["symbol"] not in exclude else 0
            rows.append({**s, "quote_volume_24h": qv, "enabled": enabled})
        self.db.upsert_symbols(rows)
        self.symbols = self.db.enabled_symbols()

    async def _universe_loop(self) -> None:
        interval = self.cfg.get("universe.refresh_minutes", 60) * 60
        while True:
            await asyncio.sleep(interval)
            try:
                old = set(self.symbols)
                await self.refresh_universe()
                new = set(self.symbols)
                if new != old:
                    log.info("universe changed %d -> %d, restarting ws", len(old), len(new))
                    await self.backfill_all(only=sorted(new - old))
                    await self.ws.start(self.symbols, self.cfg.timeframes)
            except Exception:
                log.exception("universe refresh failed")

    async def _watchdog_loop(self) -> None:
        """3分钟没有任何ws消息则强制重启ws并补缺口。"""
        while True:
            await asyncio.sleep(60)
            if self.ws.last_msg_at and time.time() - self.ws.last_msg_at > 180:
                log.warning("watchdog: no ws message for 3min, restarting")
                self.db.log("warn", "watchdog", "ws silent 3min, restart + gap fill")
                try:
                    await self.backfill_all()
                    await self.ws.start(self.symbols, self.cfg.timeframes)
                except Exception:
                    log.exception("watchdog restart failed")

    async def _funding_loop(self) -> None:
        """每5分钟刷新资金费率，供因子使用。"""
        while True:
            try:
                self.signal_engine.funding = await self.rest.funding_rates()
            except Exception as e:
                log.warning("funding refresh failed: %s", e)
            await asyncio.sleep(self.cfg.get("factors.refresh_funding_minutes", 5) * 60)

    async def _maintenance_loop(self) -> None:
        """每小时：修剪K线表防膨胀；把超过TTL未处理的信号标记为过期。"""
        while True:
            await asyncio.sleep(3600)
            try:
                keep = self.cfg.get("data.kline_keep_bars", 1500)
                for sym in self.symbols:
                    for tf in self.cfg.timeframes:
                        self.db.trim_klines(sym, tf, keep)
                ttl_s = self.cfg.get("telegram.confirm_ttl_minutes", 30) * 60
                cur = self.db.execute(
                    "UPDATE signals SET status='expired' WHERE status IN ('new','notified') AND created_at < ?",
                    (int(time.time()) - ttl_s,),
                )
                if cur.rowcount:
                    log.info("maintenance: expired %d stale signals", cur.rowcount)
                log.info("maintenance done")
            except Exception:
                log.exception("maintenance failed")

    # ---------- 数据 ----------
    async def backfill_all(self, only: list[str] | None = None) -> None:
        bars = self.cfg.get("data.backfill_bars", 500)
        targets = only or self.symbols
        sem = asyncio.Semaphore(8)

        async def fill(sym: str, tf: str):
            async with sem:
                try:
                    ks = await self.rest.klines(sym, tf, limit=bars)
                    closed = [r for r in ks if r[8] == 1]  # r[8]=closed (r[7]=taker_buy)
                    self.db.upsert_klines(sym, tf, closed)
                    self.cache[(sym, tf)] = collections.deque(
                        (dict(zip(COLS, r)) for r in closed), maxlen=bars + 50
                    )
                except Exception as e:
                    log.warning("backfill %s %s failed: %s", sym, tf, e)

        t0 = time.time()
        await asyncio.gather(*(fill(s, tf) for s in targets for tf in self.cfg.timeframes))
        log.info("backfill %d symbols done in %.1fs", len(targets), time.time() - t0)

    # ---------- 核心回调：K线收盘 ----------
    async def on_closed_bar(self, symbol: str, tf: str, bar: tuple) -> None:
        t0 = time.perf_counter()
        key = (symbol, tf)
        dq = self.cache.get(key)
        if dq is None:
            dq = collections.deque(maxlen=self.cfg.get("data.backfill_bars", 500) + 50)
            self.cache[key] = dq
        bd = dict(zip(COLS, bar))
        if dq and dq[-1]["open_time"] == bd["open_time"]:
            dq[-1] = bd
        else:
            dq.append(bd)
        self.db.upsert_klines(symbol, tf, [bar])

        # BTC 15m 收盘 → 刷新大盘趋势（btc_resonance 因子用）
        if symbol == "BTCUSDT" and tf == "15m":
            try:
                from .chan import trend_direction
                self.signal_engine.btc_trend = trend_direction(
                    list(dq), self.cfg.get("signal.trend_ema_period", 50))
            except Exception:
                log.exception("btc trend update failed")

        # paper 结算优先（先看持仓有没有打到TP/SL）
        try:
            for closed in self.paper.on_closed_bar(symbol, tf, bar):
                for sub in self.trade_close_subscribers:
                    await sub(closed)
        except Exception:
            log.exception("paper settle failed %s %s", symbol, tf)

        # 状态机文字通知（失效/解除）转发
        try:
            while self.signal_engine.notices:
                note = self.signal_engine.notices.pop(0)
                for sub in self.notice_subscribers:
                    await sub(note)
        except Exception:
            log.exception("notice forward failed")

        # 信号评估
        try:
            klines = list(dq)
            k15 = list(self.cache.get((symbol, "15m"), ())) or None
            sig = self.signal_engine.evaluate(symbol, tf, klines, k15)
            if sig:
                sid = self.db.insert_signal(sig.to_db())
                self.paper.open_from_signal(sid, sig)
                log.info("SIGNAL #%d %s %s %s %s rr=%.2f", sid, sig.kind, symbol, tf, sig.direction, sig.rr)
                self.db.log("info", "signal", f"#{sid} {sig.kind} {symbol} {tf} {sig.direction} rr={sig.rr}")
                for sub in self.signal_subscribers:
                    try:
                        await sub(sid, sig)
                    except Exception:
                        log.exception("signal subscriber failed")
        except Exception:
            log.exception("evaluate failed %s %s", symbol, tf)
        self.last_eval_ms = (time.perf_counter() - t0) * 1000

    # ---------- 状态 ----------
    def status(self) -> dict:
        return {
            "started_at": self.started_at,
            "uptime_s": int(time.time()) - self.started_at if self.started_at else 0,
            "symbols": len(self.symbols),
            "ws_conns": self.ws.connected_conns,
            "ws_last_msg_age_s": round(time.time() - self.ws.last_msg_at, 1) if self.ws.last_msg_at else None,
            "last_eval_ms": round(self.last_eval_ms, 1),
            "mode": self.cfg.mode,
            "tracks": {t: self.paper.stats(t)
                       for t in ("watch", "buy1", "buy2", "spring")},
            "funnel": dict(self.signal_engine.funnel),
        }
