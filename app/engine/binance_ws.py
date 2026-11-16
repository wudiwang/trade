"""币安合约 WebSocket 行情：多币种 kline 订阅，收盘K回调，自动重连+REST补缺口。"""
import asyncio
import json
import logging
import time
from typing import Awaitable, Callable

import aiohttp

log = logging.getLogger("binance.ws")

# 一条连接最多塞多少路 stream（官方上限1024，留余量）
MAX_STREAMS_PER_CONN = 400


# 实时报价(tick)通路: 同一币种两次回调的最小间隔(毫秒, 按行情事件时间算)
TICK_MIN_INTERVAL_MS = 1000
# tick_symbols() 的结果缓存多久重读一次(秒)
TICK_SYMBOLS_TTL_S = 1.0


class KlineWS:
    """订阅 N 个 symbol × M 个 timeframe 的 kline流。
    on_closed(symbol, tf, bar) 仅在K线收盘时回调。
    bar = (open_time, open, high, low, close, volume, quote_volume, closed=1)

    可选的实时报价通路(不传 on_tick 时与原来完全一样):
      on_tick(symbol, price, event_time_ms) —— 同步函数, 未收盘K线的推送里取最新价回调。
      tick_symbols: 返回"需要报价的币种集合"的函数(每秒最多重读一次); None=全部币种。
      tick_tf:      只取这个级别的流(每个币只出一路报价, 不是三路); None=start() 时取第一个级别。
      每个币每 ~1 秒最多回调一次。报价通路里的任何异常都只记日志, 不影响收盘K通路。
    """

    def __init__(self, ws_base: str,
                 on_closed: Callable[[str, str, tuple], Awaitable[None]],
                 on_tick: Callable[[str, float, int], object] | None = None,
                 tick_symbols: Callable[[], set] | None = None,
                 tick_tf: str | None = None):
        self.ws_base = ws_base.rstrip("/")
        self.on_closed = on_closed
        self._tasks: list[asyncio.Task] = []
        self._stop = asyncio.Event()
        self.last_msg_at: float = 0.0   # 健康度指标
        self.connected_conns = 0
        # ---- 实时报价通路 ----
        self.on_tick = on_tick
        self.tick_symbols = tick_symbols
        self.tick_tf = tick_tf
        self.tick_min_interval_ms = TICK_MIN_INTERVAL_MS
        self._tick_set: frozenset | set | None = None if tick_symbols is None else frozenset()  # None=全部币种
        self._tick_set_next = 0.0                        # 下次重读 tick_symbols() 的时刻(monotonic)
        self._tick_last: dict[str, int] = {}             # symbol -> 上次回调的事件时间(ms)
        self.tick_count = 0                              # 已回调的报价数
        self.tick_errors = 0
        self._tick_err_logged_at = -1e9

    def streams_for(self, symbols: list[str], tfs: list[str]) -> list[str]:
        return [f"{s.lower()}@kline_{tf}" for s in symbols for tf in tfs]

    async def start(self, symbols: list[str], tfs: list[str]) -> None:
        await self.stop()
        self._stop = asyncio.Event()
        if self.on_tick is not None and self.tick_tf is None and tfs:
            self.tick_tf = tfs[0]       # 没指定就用第一个级别的流出报价
        streams = self.streams_for(symbols, tfs)
        chunks = [streams[i:i + MAX_STREAMS_PER_CONN]
                  for i in range(0, len(streams), MAX_STREAMS_PER_CONN)]
        log.info("starting %d ws connection(s) for %d streams", len(chunks), len(streams))
        self._tasks = [asyncio.create_task(self._run_conn(i, c)) for i, c in enumerate(chunks)]

    async def stop(self) -> None:
        self._stop.set()
        for t in self._tasks:
            t.cancel()
        for t in self._tasks:
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
        self._tasks = []
        self.connected_conns = 0

    async def _run_conn(self, idx: int, streams: list[str]) -> None:
        # 2026-04 起币安合约行情流必须走 /market 前缀（旧 /stream 已停推）
        url = f"{self.ws_base}/market/stream?streams={'/'.join(streams)}"
        backoff = 1
        while not self._stop.is_set():
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.ws_connect(url, heartbeat=180, max_msg_size=0) as ws:
                        log.info("ws#%d connected (%d streams)", idx, len(streams))
                        self.connected_conns += 1
                        backoff = 1
                        try:
                            async for msg in ws:
                                if msg.type == aiohttp.WSMsgType.TEXT:
                                    self.last_msg_at = time.time()
                                    await self._handle(msg.data)
                                elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                                    break
                        finally:
                            self.connected_conns -= 1
            except asyncio.CancelledError:
                return
            except Exception as e:
                log.warning("ws#%d error: %s", idx, e)
            if self._stop.is_set():
                return
            log.info("ws#%d reconnecting in %ds", idx, backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)

    async def _handle(self, raw: str) -> None:
        try:
            d = json.loads(raw)
            data = d.get("data", {})
            k = data.get("k")
            if not k or not k.get("x"):   # x=true 才是收盘
                if k and self.on_tick is not None:
                    self._tick(k, data)   # 未收盘K的推送 → 实时报价(同步, 自带异常隔离)
                return
            bar = (int(k["t"]), float(k["o"]), float(k["h"]), float(k["l"]),
                   float(k["c"]), float(k["v"]), float(k["q"]), float(k.get("V") or 0), 1)
            await self.on_closed(k["s"], k["i"], bar)
        except Exception:
            log.exception("handle ws message failed")

    def _tick(self, k: dict, data: dict) -> None:
        """实时报价通路。非报价币种只多两三次字典/集合查找; 永不抛异常。"""
        try:
            tf = self.tick_tf
            if tf is not None and k.get("i") != tf:      # 只取一个级别的流 → 每币一路
                return
            now = time.monotonic()
            if now >= self._tick_set_next:               # 订阅集合每秒最多重读一次
                self._tick_set_next = now + TICK_SYMBOLS_TTL_S
                fn = self.tick_symbols
                if fn is None:
                    self._tick_set = None
                else:
                    try:
                        self._tick_set = fn() or frozenset()
                    except Exception:
                        if self._tick_set is None:       # 取不到集合时宁可不出报价, 也不放开全部币种
                            self._tick_set = frozenset()
                        raise
            sym = k.get("s")
            syms = self._tick_set
            if syms is not None and sym not in syms:
                return
            ev = data.get("E")
            ev = int(ev) if ev is not None else int(time.time() * 1000)
            last = self._tick_last.get(sym)
            if last is not None and 0 <= ev - last < self.tick_min_interval_ms:
                return                                   # 节流: 每币每 ~1 秒最多一笔
            self._tick_last[sym] = ev
            self.tick_count += 1
            self.on_tick(sym, float(k["c"]), ev)
        except Exception:
            self.tick_errors += 1
            now = time.monotonic()
            if now - self._tick_err_logged_at >= 60:     # 限速: 每分钟最多一条堆栈
                self._tick_err_logged_at = now
                log.exception("ws tick path failed (累计 %d 次)", self.tick_errors)
