"""Telegram Bot：信号卡片推送 + [确认买入]两步确认 + 下单/记录。
轻量实现（aiohttp 直连 Bot API，long polling），不依赖重框架。
"""
import asyncio
import json
import logging
import time

import aiohttp

log = logging.getLogger("tgbot")

API = "https://api.telegram.org/bot{token}/{method}"


def fmt_price(p: float) -> str:
    if p >= 100:
        return f"{p:,.2f}"
    return f"{p:.6g}"


def compute_order(cfg, entry: float, direction: str, sl: float, tp: float) -> dict:
    """按线上下单公式(与 trader.py 一致)预估本单名义额/保证金/杠杆/预期盈亏(U)。"""
    lev = float(cfg.get("risk.leverage", 5) or 5)
    margin = float(cfg.get("live.fixed_margin_u", 0) or 0)
    margin_pct = float(cfg.get("live.fixed_margin_pct", 0) or 0)
    fixed = float(cfg.get("live.fixed_notional_u", 0) or 0)
    equity = float(cfg.get("risk.account_equity", 0) or 0)
    if margin > 0:
        margin_used, notional = margin, margin * lev
    elif margin_pct > 0 and equity > 0:
        margin_used = equity * margin_pct / 100.0
        notional = margin_used * lev
    elif fixed > 0:
        notional = fixed
        margin_used = fixed / lev if lev else fixed
    else:
        notional = margin_used = 0.0           # 风险算法: 名义额由 suggested_qty 决定, 不预估
    profit = loss = 0.0
    if notional > 0 and entry > 0:
        if direction == "long":
            profit, loss = notional * (tp - entry) / entry, notional * (entry - sl) / entry
        else:
            profit, loss = notional * (entry - tp) / entry, notional * (sl - entry) / entry
    return {"notional": notional, "margin": margin_used, "lev": lev,
            "profit": profit, "loss": loss, "equity": equity}


def signal_card(sid: int, s: dict, mode: str, chart_url: str | None = None,
                order: dict | None = None, acct: dict | None = None) -> str:
    d = "做多 🟢" if s["direction"] == "long" else "做空 🔴"
    star = "⭐强信号" if s["strength"] == "strong" else ""
    mode_tag = f"{mode} ⚠️真实下单" if mode == "live" else mode
    lines = [
        f"📊 <b>#{sid} {s['symbol']} {s['tf']} {d}</b> {star}",
        f"级别: {'主信号 RR≥5' if s['kind'] == 'primary' else '次级 RR≥2.5'}   模式: {mode_tag}",
        "",
        f"入场: <code>{fmt_price(s['entry'])}</code>   止损: <code>{fmt_price(s['sl'])}</code>   止盈: <code>{fmt_price(s['tp'])}</code>",
        f"盈亏比: <b>{s['rr']}</b>   量能: {s['vol_ratio']}x均量",
    ]
    if order and order["notional"] > 0:
        lines += [
            "",
            f"💵 本单: 名义 <b>{order['notional']:.0f}U</b> / 保证金 {order['margin']:.1f}U / 杠杆 {order['lev']:.0f}x",
            f"   预期盈利: <b>+{order['profit']:.2f}U</b> (价到止盈)",
            f"   预期亏损: <b>−{order['loss']:.2f}U</b> (价到止损)",
        ]
    else:
        lines.append(f"建议仓位: <code>{s['suggested_qty']:.6g}</code> 张 (风险 {s['risk_usdt']} U)")
    if acct is not None:
        ps = acct.get("positions", [])
        if ps:
            txt = " · ".join(
                f"{p['symbol'].replace('USDT','')} {'多' if float(p['positionAmt'])>0 else '空'} {float(p['unRealizedProfit']):+.1f}U"
                for p in ps[:5])
            lines += ["", f"📦 当前持仓: {len(ps)}个  {txt}"]
        else:
            lines += ["", "📦 当前持仓: 无"]
        eq = order.get("equity", 0) if order else 0
        lines.append(f"💰 可用保证金: {acct.get('avail', 0):.1f}U" + (f" / 本金 {eq:.0f}U" if eq else ""))
    lines += ["", f"依据: {s['reason']}"]
    if chart_url:
        lines += ["", f"图形: {chart_url}"]
    return "\n".join(lines)


class TgBot:
    def __init__(self, cfg, db, trader=None):
        self.cfg = cfg
        self.db = db
        self.trader = trader          # live 模式注入 LiveTrader
        self.token = cfg.tg_token
        self.chat_id = cfg.tg_chat_id
        self._session: aiohttp.ClientSession | None = None
        self._offset = 0
        self._task: asyncio.Task | None = None
        self.enabled = bool(self.token and self.chat_id and cfg.get("telegram.enabled", True))

    async def session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60))
        return self._session

    async def api(self, method: str, **params):
        s = await self.session()
        url = API.format(token=self.token, method=method)
        async with s.post(url, json=params) as resp:
            data = await resp.json()
            if not data.get("ok"):
                log.warning("tg %s failed: %s", method, data)
            return data

    async def _account_snapshot(self) -> dict | None:
        """拉币安当前持仓 + 可用保证金(仅 live 且有 trader 时)。失败返回 None,不阻塞推送。"""
        rest = getattr(self.trader, "rest", None) if self.trader else None
        if not rest:
            return None
        try:
            pos = await rest.position_risk()
            live = [p for p in pos if abs(float(p.get("positionAmt", 0) or 0)) > 0]
            acct = await rest.account_info()
            return {"positions": live, "avail": float(acct.get("availableBalance", 0) or 0)}
        except Exception as e:
            log.warning("account snapshot failed: %s", e)
            return None

    # ---------- 推送 ----------
    async def on_signal(self, sid: int, sig) -> None:
        """engine.signal_subscribers 回调。只推主信号（primary）。"""
        if not self.enabled:
            return
        s = sig.to_db() if hasattr(sig, "to_db") else dict(sig)
        if s.get("kind") != "primary":
            return
        chart_url = self._chart_url(sid)
        order = compute_order(self.cfg, float(s["entry"]), s["direction"], float(s["sl"]), float(s["tp"]))
        acct = await self._account_snapshot()
        buttons = [[
            {"text": "✅ 确认买入", "callback_data": f"c:{sid}"},
            {"text": "❌ 忽略", "callback_data": f"i:{sid}"},
        ]]
        if chart_url:
            buttons.append([{"text": "📈 查看图形", "url": chart_url}])
        kb = {"inline_keyboard": buttons}
        r = await self.api(
            "sendMessage", chat_id=self.chat_id,
            text=signal_card(sid, s, self.cfg.mode, chart_url, order, acct),
            parse_mode="HTML", reply_markup=kb,
        )
        if r.get("ok"):
            self.db.update_signal(sid, status="notified", tg_message_id=r["result"]["message_id"])

    def _chart_url(self, sid: int) -> str | None:
        base = (self.cfg.get("web.public_url", "") or "").strip()
        if not base:
            return None
        return f"{base.rstrip('/')}/?signal={sid}"

    async def on_trade_close(self, trade: dict) -> None:
        """paper 平仓播报。"""
        if not self.enabled:
            return
        emo = "🎯止盈" if trade["result"] == "tp" else "🛑止损"
        await self.api(
            "sendMessage", chat_id=self.chat_id,
            text=f"{emo} [paper·{trade.get('track')}] {trade['symbol']} {trade['tf']} {trade['direction']} "
                 f"pnl={trade['pnl']:.2f}U ({trade['pnl_r']:+.2f}R)",
        )

    async def notify(self, text: str) -> None:
        if self.enabled:
            await self.api("sendMessage", chat_id=self.chat_id, text=text, parse_mode="HTML")

    # ---------- 回调处理 ----------
    async def start(self) -> None:
        if not self.enabled:
            log.info("tg bot disabled (no token/chat_id)")
            return
        # 清掉可能存在的webhook（webhook会让getUpdates 409）
        await self.api("deleteWebhook")
        self._task = asyncio.create_task(self._poll_loop())
        log.info("tg bot polling started")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
        if self._session and not self._session.closed:
            await self._session.close()

    async def _poll_loop(self) -> None:
        while True:
            try:
                r = await self.api("getUpdates", offset=self._offset,
                                   timeout=self.cfg.get("telegram.poll_timeout", 25),
                                   allowed_updates=["callback_query", "message"])
                for u in r.get("result", []):
                    self._offset = u["update_id"] + 1
                    if "callback_query" in u:
                        await self._on_callback(u["callback_query"])
                    elif "message" in u:
                        await self._on_message(u["message"])
            except asyncio.CancelledError:
                return
            except Exception:
                log.exception("tg poll error")
                await asyncio.sleep(5)

    async def _on_message(self, m: dict) -> None:
        if str(m.get("chat", {}).get("id")) != str(self.chat_id):
            return
        text = (m.get("text") or "").strip().lower()
        if text in ("/status", "status", "状态"):
            from ..web.server import APP_STATE
            eng = APP_STATE.get("engine")
            if eng:
                st = eng.status()
                lines = [f"⚙️ 运行 {st['uptime_s']//3600}h{st['uptime_s']%3600//60}m | "
                         f"{st['symbols']}币 | ws {st['ws_conns']}连接"]
                for name, t in st.get("tracks", {}).items():
                    lines.append(f"{name}: {t['closed']}平/{t['open']}持 "
                                 f"胜率{t['win_rate']}% pnl={t['total_pnl']}U 期望{t['expectancy_r']}R")
                await self.notify("\n".join(lines))

    async def _on_callback(self, cq: dict) -> None:
        data = cq.get("data", "")
        cq_id = cq["id"]
        msg = cq.get("message", {})
        if str(msg.get("chat", {}).get("id")) != str(self.chat_id):
            await self.api("answerCallbackQuery", callback_query_id=cq_id, text="无权操作")
            return
        try:
            action, sid_s = data.split(":", 1)
            sid = int(sid_s)
        except ValueError:
            return
        row = self.db.one("SELECT * FROM signals WHERE id=?", (sid,))
        if not row:
            await self.api("answerCallbackQuery", callback_query_id=cq_id, text="信号不存在")
            return

        ttl = self.cfg.get("telegram.confirm_ttl_minutes", 30) * 60
        expired = time.time() - row["created_at"] > ttl

        if action == "i":
            self.db.update_signal(sid, status="ignored")
            await self._finish(cq_id, msg, f"❌ 已忽略 #{sid}", "已忽略")
        elif action == "c":
            if expired:
                self.db.update_signal(sid, status="expired")
                await self._finish(cq_id, msg, f"⏰ #{sid} 已超时失效", "已超时")
                return
            kb = {"inline_keyboard": [[
                {"text": "⚠️ 二次确认，立即下单", "callback_data": f"c2:{sid}"},
                {"text": "取消", "callback_data": f"x:{sid}"},
            ]]}
            await self.api("editMessageReplyMarkup", chat_id=msg["chat"]["id"],
                           message_id=msg["message_id"], reply_markup=kb)
            await self.api("answerCallbackQuery", callback_query_id=cq_id, text="请二次确认")
        elif action == "x":
            kb = {"inline_keyboard": [[
                {"text": "✅ 确认买入", "callback_data": f"c:{sid}"},
                {"text": "❌ 忽略", "callback_data": f"i:{sid}"},
            ]]}
            await self.api("editMessageReplyMarkup", chat_id=msg["chat"]["id"],
                           message_id=msg["message_id"], reply_markup=kb)
            await self.api("answerCallbackQuery", callback_query_id=cq_id, text="已取消")
        elif action == "c2":
            if expired:
                self.db.update_signal(sid, status="expired")
                await self._finish(cq_id, msg, f"⏰ #{sid} 已超时失效", "已超时")
                return
            if self.cfg.mode == "live" and self.trader:
                res = await self.trader.execute_signal(sid, row)
                status = "confirmed" if res["ok"] else "error"
                self.db.update_signal(sid, status=status)
                await self._finish(cq_id, msg,
                                   ("🚀 " if res["ok"] else "⚠️ ") + res["message"],
                                   "完成" if res["ok"] else "失败")
            else:
                self.db.update_signal(sid, status="confirmed")
                await self._finish(
                    cq_id, msg,
                    f"✅ #{sid} 已确认（paper 模式：虚拟仓已在跟踪，胜负自动结算；切 live 后此操作=真实下单）",
                    "已确认",
                )

    async def _finish(self, cq_id: str, msg: dict, note: str, toast: str) -> None:
        await self.api("answerCallbackQuery", callback_query_id=cq_id, text=toast)
        await self.api("editMessageReplyMarkup", chat_id=msg["chat"]["id"],
                       message_id=msg["message_id"], reply_markup={"inline_keyboard": []})
        await self.api("sendMessage", chat_id=msg["chat"]["id"], text=note,
                       reply_to_message_id=msg["message_id"])
