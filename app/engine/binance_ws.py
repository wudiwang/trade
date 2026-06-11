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


class KlineWS:
    """订阅 N 个 symbol × M 个 timeframe 的 kline流。
    on_closed(symbol, tf, bar) 仅在K线收盘时回调。
    bar = (open_time, open, high, low, close, volume, quote_volume, closed=1)
    """

    def __init__(self, ws_base: str,
                 on_closed: Callable[[str, str, tuple], Awaitable[None]]):
        self.ws_base = ws_base.rstrip("/")
        self.on_closed = on_closed
        self._tasks: list[asyncio.Task] = []
        self._stop = asyncio.Event()
        self.last_msg_at: float = 0.0   # 健康度指标
        self.connected_conns = 0

    def streams_for(self, symbols: list[str], tfs: list[str]) -> list[str]:
        return [f"{s.lower()}@kline_{tf}" for s in symbols for tf in tfs]

    async def start(self, symbols: list[str], tfs: list[str]) -> None:
        await self.stop()
        self._stop = asyncio.Event()
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
            k = d.get("data", {}).get("k")
            if not k or not k.get("x"):   # x=true 才是收盘
                return
            bar = (int(k["t"]), float(k["o"]), float(k["h"]), float(k["l"]),
                   float(k["c"]), float(k["v"]), float(k["q"]), 1)
            await self.on_closed(k["s"], k["i"], bar)
        except Exception:
            log.exception("handle ws message failed")
