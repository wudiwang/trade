"""总装入口：引擎 + Telegram Bot + Web 控制台，单进程 asyncio。
启动: python -m app.main
"""
import asyncio
import logging
import sys

import uvicorn

from .config import get_config
from .db import DB
from .bot.telegram import TgBot
from .engine.core import Engine
from .engine.trader import LiveTrader
from .web.server import create_app, load_setting_overrides

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("main")


async def amain() -> None:
    cfg = get_config()
    db = DB(cfg.db_path)
    load_setting_overrides(cfg, db)   # settings 表热参数灌回

    engine = Engine(cfg, db)
    trader = LiveTrader(cfg, db, engine.rest)
    bot = TgBot(cfg, db, trader)
    engine.signal_subscribers.append(bot.on_signal)
    engine.trade_close_subscribers.append(bot.on_trade_close)

    app = create_app(cfg, db, engine, bot)

    await engine.start()
    await bot.start()
    if bot.enabled:
        await bot.notify(
            f"🚀 交易系统已启动\n模式: {cfg.mode} | 监控 {len(engine.symbols)} 币种 | 级别 {'/'.join(cfg.timeframes)}\n"
            f"主信号门槛 RR≥{cfg.get('signal.min_rr_primary')}，量能≥{cfg.get('signal.vol_multiplier')}x"
        )

    server = uvicorn.Server(uvicorn.Config(
        app, host=cfg.get("web.host", "0.0.0.0"), port=cfg.get("web.port", 8488),
        log_level="warning",
    ))
    log.info("web console on http://%s:%s", cfg.get("web.host"), cfg.get("web.port"))
    try:
        await server.serve()
    finally:
        await bot.stop()
        await engine.stop()


if __name__ == "__main__":
    try:
        asyncio.run(amain())
    except KeyboardInterrupt:
        pass
