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
    engine.trader = trader            # 让引擎可在 auto_trade 开启时直接下单
    bot = TgBot(cfg, db, trader)
    engine.signal_subscribers.append(bot.on_signal)
    engine.trade_close_subscribers.append(bot.on_trade_close)
    engine.notice_subscribers.append(bot.notify)

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
                since = int(_t.time()) - 86400
                n_sig = db.one("SELECT COUNT(*) c FROM signals WHERE created_at > ?",
                               (since,))["c"]
                # 近24h【平仓】明细: 赚/亏笔数 + 当日盈亏 (2026-08-10 用户要求)
                rows = db.query(
                    "SELECT pnl, pnl_r, result, track FROM paper_trades "
                    "WHERE closed_at > ? AND result IN ('tp','sl','rev')", (since,)) or []
                win = sum(1 for r in rows if (r["pnl"] or 0) > 0)
                loss = len(rows) - win
                pnl = sum((r["pnl"] or 0) for r in rows)
                n_open = (db.one("SELECT COUNT(*) c FROM paper_trades WHERE result='open'")
                          or {"c": 0})["c"]
                lines = [f"📅 <b>日报</b>（近24h）",
                         f"触发信号: <b>{n_sig}</b> 个"]
                if rows:
                    wr = win / len(rows) * 100
                    lines.append(f"已平仓: <b>{len(rows)}</b> 笔 — 赚 {win} / 亏 {loss}（胜率 {wr:.0f}%）")
                    lines.append(f"当日盈亏: <b>{pnl:+.1f}U</b>")
                    fee = len(rows) * 0.45      # 500U名义×双边0.045% ≈ 0.45U/笔
                    lines.append(f"扣手续费后约: <b>{pnl - fee:+.1f}U</b>（费≈{fee:.1f}U）")
                    # 按方向拆
                    for tr in ("second_buy", "second_sell"):
                        sub = [r for r in rows if r["track"] == tr]
                        if sub:
                            w = sum(1 for r in sub if (r["pnl"] or 0) > 0)
                            nm = "二买(多)" if tr == "second_buy" else "二卖(空)"
                            lines.append(f"　{nm}: {len(sub)}笔 赚{w}/亏{len(sub)-w} "
                                         f"{sum((r['pnl'] or 0) for r in sub):+.1f}U")
                else:
                    lines.append("已平仓: 0 笔")
                lines.append(f"当前持仓: {n_open} 笔")
                await bot.notify("\n".join(lines))
                log.info("daily report sent")
    asyncio.create_task(daily_report())

    async def orderflow_brief():
        """曼谷时间每天 09:00 / 21:00 推送 BTC 订单流简报（用户 2026-11-12）。
        内容：CVD×价格背离(1h线)、资金费率(按天)、持仓量 OI 四象限。"""
        import time as _t
        from .engine.orderflow import brief_payload, build_brief
        HOURS = (9, 21)
        last = None
        while True:
            await asyncio.sleep(60)
            t = _t.gmtime(_t.time() + 7 * 3600)          # 曼谷 UTC+7
            slot = (t.tm_year, t.tm_yday, t.tm_hour)
            if t.tm_hour not in HOURS or slot == last or not bot.enabled:
                continue
            last = slot
            try:
                payload = await brief_payload(engine.rest, "BTCUSDT")
                await bot.notify(build_brief(payload))
                log.info("orderflow brief sent (bkk %02d:00)", t.tm_hour)
            except Exception as e:                        # 取数失败不该拖垮主进程
                log.warning("orderflow brief failed: %s", e)
                try:
                    await bot.notify(f"⚠️ BTC 订单流简报生成失败：{e}")
                except Exception:
                    pass
    asyncio.create_task(orderflow_brief())


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
