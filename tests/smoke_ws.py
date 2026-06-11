"""阶段B冒烟：WS连接 → 收到1m收盘K回调（最多等90秒）。"""
import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import get_config
from app.engine.binance_ws import KlineWS

got: list = []


async def on_closed(symbol, tf, bar):
    got.append((symbol, tf, bar))
    print(f"closed bar: {symbol} {tf} open_time={bar[0]} close={bar[4]} (延迟 {time.time() - (bar[0]/1000 + 60):.1f}s)")


async def main():
    cfg = get_config()
    ws = KlineWS(cfg.get("binance.ws_base"), on_closed)
    syms = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT", "DOGEUSDT"]
    await ws.start(syms, ["1m"])
    deadline = time.time() + 90
    while time.time() < deadline and len(got) < 3:
        await asyncio.sleep(1)
    await ws.stop()
    assert ws.last_msg_at > 0, "没收到任何ws消息"
    assert len(got) >= 3, f"90秒内只收到{len(got)}根收盘K(期望>=3)"
    print(f"WS SMOKE OK: 收到{len(got)}根收盘K")


asyncio.run(main())
