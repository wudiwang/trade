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

    async def daily_report():
        """每天北京时间08:00发送双轨统计日报。"""
        import time as _t
        last_day = None
        while True:
            await asyncio.sleep(60)
            t = _t.gmtime(_t.time() + 8 * 3600)  # UTC+8
            day = (t.tm_year, t.tm_yday)
            if t.tm_hour == 8 and day != last_day and bot.enabled:
                last_day = day
                log.info("daily report firing")
                r5 = engine.paper.stats("rr5")
                r25 = engine.paper.stats("rr25")
                n_sig = db.one("SELECT COUNT(*) c FROM signals WHERE created_at > ?",
                               (int(_t.time()) - 86400,))["c"]
                await bot.notify(
                    f"📅 <b>日报</b>（近24h）\n"
                    f"信号: {n_sig} 个\n"
                    f"RR≥5 轨: {r5['closed']}平/{r5['open']}持 | 胜率 {r5['win_rate']}% | "
                    f"累计 {r5['total_pnl']}U | 期望 {r5['expectancy_r']}R\n"
                    f"RR≥2.5 轨: {r25['closed']}平/{r25['open']}持 | 胜率 {r25['win_rate']}% | "
                    f"累计 {r25['total_pnl']}U | 期望 {r25['expectancy_r']}R"
                )
                log.info("daily report sent")
    asyncio.create_task(daily_report())

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
