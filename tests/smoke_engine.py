"""整机冒烟：Engine 启动（刷新币种→回填→WS）→ 状态健康 → 停止。"""
import asyncio
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

from app.config import get_config
from app.db import DB
from app.engine.core import Engine


async def main():
    cfg = get_config()
    db = DB(cfg.db_path)
    engine = Engine(cfg, db)

    sigs = []
    engine.signal_subscribers.append(lambda sid, s: sigs.append((sid, s)) or asyncio.sleep(0))

    t0 = time.time()
    await engine.start()
    startup = time.time() - t0
    print(f"启动耗时 {startup:.1f}s")

    # 等90秒收些实时K（5m/15m不一定有收盘，看ws健康度即可）
    await asyncio.sleep(90)
    st = engine.status()
    print("status:", st)
    assert st["symbols"] > 30, "币种数异常"
    assert st["ws_conns"] >= 1, "ws未连接"
    # 回填后缓存应有数据
    key = (engine.symbols[0], "15m")
    assert len(engine.cache.get(key, [])) > 400, f"缓存回填不足: {len(engine.cache.get(key, []))}"
    await engine.stop()
    print("ENGINE SMOKE OK")


asyncio.run(main())
