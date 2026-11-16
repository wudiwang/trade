"""行情流重启不得打断在途的实盘下单序列(2026-11-17 审查发现的裸仓隐患)。

自动下单在行情流任务里 await; ws.start()/stop() 会 cancel 这些任务。若正好卡在
"市价进场已成交、止损未挂"之间, 会留下没有止损的真实持仓。修复: 重启/停机前先拿自动下单锁。
"""
import asyncio
import types

from app.engine.core import Engine


class _WS:
    def __init__(self, log):
        self.log = log

    async def start(self, symbols, tfs):
        self.log.append("ws_start")

    async def stop(self):
        self.log.append("ws_stop")


class _Rest:
    async def close(self):
        pass


def _engine(log):
    e = object.__new__(Engine)
    e._auto_order_lock = asyncio.Lock()
    e.ws = _WS(log)
    e.rest = _Rest()
    e.symbols = ["BTCUSDT"]
    e.cfg = types.SimpleNamespace(timeframes=["5m"])
    e._tasks, e._bg_notices = [], set()
    e._linked_refetch_task = e._backlog_task = None
    return e


async def _order_sequence(e, log, hold=0.15):
    async with e._auto_order_lock:           # 与 Engine._auto_execute 同一把锁
        log.append("entry_filled")
        await asyncio.sleep(hold)            # 进场与挂止损之间
        log.append("stop_placed")


def test_restart_ws_waits_for_inflight_order_sequence():
    async def run():
        log = []
        e = _engine(log)
        t = asyncio.create_task(_order_sequence(e, log))
        await asyncio.sleep(0.02)            # 下单序列已进入临界区
        await e.restart_ws()
        await t
        return log, e._auto_order_lock.locked()
    log, locked = asyncio.run(run())
    assert log == ["entry_filled", "stop_placed", "ws_start"]
    assert not locked                        # 锁已释放, 不影响后续下单


def test_stop_waits_for_inflight_order_sequence():
    async def run():
        log = []
        e = _engine(log)
        t = asyncio.create_task(_order_sequence(e, log))
        await asyncio.sleep(0.02)
        await e.stop()
        await t
        return log
    assert asyncio.run(run()) == ["entry_filled", "stop_placed", "ws_stop"]


def test_restart_ws_proceeds_when_order_sequence_hangs():
    async def run():
        log = []
        e = _engine(log)
        await e._auto_order_lock.acquire()   # 模拟下单序列卡死
        got = await e._order_quiet(timeout=0.05)
        return got
    assert asyncio.run(run()) is False       # 超时不拿锁, 调用方照旧继续


def test_restart_ws_without_orders_is_immediate():
    async def run():
        log = []
        e = _engine(log)
        await e.restart_ws()
        return log, e._auto_order_lock.locked()
    log, locked = asyncio.run(run())
    assert log == ["ws_start"] and not locked
