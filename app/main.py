"""总装入口：引擎 + Telegram Bot + Web 控制台，单进程 asyncio。
启动: python -m app.main
"""
import asyncio
import html
import logging
import sys

import uvicorn

from .config import get_config
from .db import DB
from .bot.telegram import TgBot
from .engine.core import Engine
from .engine.linked_rules import unaudited_tracks
from .engine.trader import LiveTrader
from .web.server import create_app, load_setting_overrides

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("main")

# 日报里各 track 的显示名; 没列在这里的 track 直接显示它的名字
TRACK_LABELS = {
    "second_buy": "二买(多)",
    "second_sell": "二卖(空)",
    "btcbreak_pump": "BTC破位→PUMP(模拟)",
}
_TRACK_FIRST = ("second_buy", "second_sell")


def daily_track_lines(rows) -> list[str]:
    """日报的分 track 明细: 近24h 有平仓的每个 track 各一行(二买/二卖在前, 其余按名字排)。
    只是把同一批平仓记录按 track 拆开看, 不改总计。rows: 含 pnl / track 的行。"""
    by: dict[str, list] = {}
    for r in rows:
        by.setdefault(str(r["track"] or "other"), []).append(r)
    order = [t for t in _TRACK_FIRST if t in by] + sorted(t for t in by if t not in _TRACK_FIRST)
    lines = []
    for tr in order:
        sub = by[tr]
        w = sum(1 for r in sub if (r["pnl"] or 0) > 0)
        nm = TRACK_LABELS.get(tr) or html.escape(tr)
        lines.append(f"　{nm}: {len(sub)}笔 赚{w}/亏{len(sub)-w} "
                     f"{sum((r['pnl'] or 0) for r in sub):+.1f}U")
    return lines


def daily_close_lines(rows, hidden=frozenset()) -> list[str]:
    """日报里「近24h 平仓」这一段。rows: 含 pnl / track 的平仓行; hidden: 还没通过出图审核的 track。
    未审核的 track(CLAUDE.md 第七条: 审核通过前不出胜率/期望)不进 已平仓/胜率/当日盈亏/手续费 这几行,
    也不出 赚/亏 明细, 只在最后各占一行报笔数。其余行与旧版逐字相同。"""
    rows = list(rows or [])
    shown = [r for r in rows if str(r["track"] or "other") not in hidden]
    lines: list[str] = []
    if shown:
        win = sum(1 for r in shown if (r["pnl"] or 0) > 0)
        loss = len(shown) - win
        pnl = sum((r["pnl"] or 0) for r in shown)
        wr = win / len(shown) * 100
        lines.append(f"已平仓: <b>{len(shown)}</b> 笔 — 赚 {win} / 亏 {loss}（胜率 {wr:.0f}%）")
        lines.append(f"当日盈亏: <b>{pnl:+.1f}U</b>")
        fee = len(shown) * 0.45      # 500U名义×双边0.045% ≈ 0.45U/笔
        lines.append(f"扣手续费后约: <b>{pnl - fee:+.1f}U</b>（费≈{fee:.1f}U）")
        # 按 track 拆(每个有平仓的 track 一行)
        lines.extend(daily_track_lines(shown))
    else:
        lines.append("已平仓: 0 笔")
    held: dict[str, int] = {}
    for r in rows:
        tr = str(r["track"] or "other")
        if tr in hidden:
            held[tr] = held.get(tr, 0) + 1
    for tr in sorted(held):
        nm = TRACK_LABELS.get(tr) or html.escape(tr)
        lines.append(f"　{nm}: 平仓 {held[tr]} 笔（未审核，不计胜率与盈亏）")
    return lines


async def amain() -> None:
    cfg = get_config()
    db = DB(cfg.db_path)
    load_setting_overrides(cfg, db)   # settings 表热参数灌回

    engine = Engine(cfg, db)
    trader = LiveTrader(cfg, db, engine.rest)
    engine.trader = trader            # 让引擎可在 auto_trade 开启时直接下单
    bot = TgBot(cfg, db, trader)
    bot.engine = engine               # Telegram 的「提醒」指令直接找 engine.alerts / engine.linked(只读写提醒, 不涉及下单)
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
                n_open = (db.one("SELECT COUNT(*) c FROM paper_trades WHERE result='open'")
                          or {"c": 0})["c"]
                lines = [f"📅 <b>日报</b>（近24h）",
                         f"触发信号: <b>{n_sig}</b> 个"]
                # 未通过出图审核的联动规则: 只报笔数, 不进胜率/盈亏(第七条)
                lines.extend(daily_close_lines(rows, unaudited_tracks(cfg)))
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

    # 订单流页(/flow.html)的后台任务：深度记录器 + 状态卡/CVD/清算图刷新。
    # 与引擎隔离：这里只是 create_task，不 await 任何触网的东西；启动失败只记日志；
    # 任务内部每一轮都有 try/except + 超时 + 退避，重计算在线程里，永远不会把异常带到这里。
    try:
        from .flow.api import start_flow_tasks
        flow_tasks = start_flow_tasks(cfg, engine)
        log.info("flow tasks started: %d", len(flow_tasks))
    except Exception:
        log.exception("订单流后台任务启动失败（不影响交易引擎）")

    server = uvicorn.Server(uvicorn.Config(
        app, host=cfg.get("web.host", "0.0.0.0"), port=cfg.get("web.port", 8488),
        log_level="warning",
    ))
    log.info("web console on http://%s:%s", cfg.get("web.host"), cfg.get("web.port"))
    try:
        await server.serve()
    finally:
        try:
            try:                              # 先停订单流任务（最多等 5 秒）
                from .flow.api import stop_flow_tasks
                await asyncio.wait_for(stop_flow_tasks(), 5)
            except Exception:
                log.warning("订单流后台任务收尾异常", exc_info=True)
        finally:                              # 它停不停得干净，都不能挡住 bot / 引擎收尾
            await bot.stop()
            await engine.stop()


if __name__ == "__main__":
    try:
        asyncio.run(amain())
    except KeyboardInterrupt:
        pass
