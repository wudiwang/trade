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


def signal_card(sid: int, s: dict, mode: str) -> str:
    d = "做多 🟢" if s["direction"] == "long" else "做空 🔴"
    star = "⭐强信号" if s["strength"] == "strong" else ""
    lines = [
        f"📊 <b>#{sid} {s['symbol']} {s['tf']} {d}</b> {star}",
        f"级别: {'主信号 RR≥5' if s['kind'] == 'primary' else '次级 RR≥2.5'}   模式: {mode}",
        "",
        f"入场: <code>{fmt_price(s['entry'])}</code>",
        f"止损: <code>{fmt_price(s['sl'])}</code>",
        f"止盈: <code>{fmt_price(s['tp'])}</code>(密集成交区)",
        f"盈亏比: <b>{s['rr']}</b>   量能: {s['vol_ratio']}x均量",
        f"建议仓位: <code>{s['suggested_qty']:.6g}</code> 张 (风险 {s['risk_usdt']} U)",
        "",
        f"依据: {s['reason']}",
    ]
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

    # ---------- 推送 ----------
    async def on_signal(self, sid: int, sig) -> None:
        """engine.signal_subscribers 回调。只推主信号（primary）。"""
        if not self.enabled:
            return
        s = sig.to_db() if hasattr(sig, "to_db") else dict(sig)
        if s.get("kind") != "primary":
            return
        kb = {"inline_keyboard": [[
            {"text": "✅ 确认买入", "callback_data": f"c:{sid}"},
            {"text": "❌ 忽略", "callback_data": f"i:{sid}"},
        ]]}
        r = await self.api(
            "sendMessage", chat_id=self.chat_id, text=signal_card(sid, s, self.cfg.mode),
            parse_mode="HTML", reply_markup=kb,
        )
        if r.get("ok"):
            self.db.update_signal(sid, status="notified", tg_message_id=r["result"]["message_id"])

    async def on_trade_close(self, trade: dict) -> None:
        """paper 平仓播报（只报 rr5 track 避免刷屏；rr25 看网页）。"""
        if not self.enabled or trade.get("track") != "rr5":
            return
        emo = "🎯止盈" if trade["result"] == "tp" else "🛑止损"
        await self.api(
            "sendMessage", chat_id=self.chat_id,
            text=f"{emo} [paper] {trade['symbol']} {trade['tf']} {trade['direction']} "
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
                r5, r25 = st["stats_rr5"], st["stats_rr25"]
                await self.notify(
                    f"⚙️ 运行 {st['uptime_s']//3600}h{st['uptime_s']%3600//60}m | {st['symbols']}币 | "
                    f"ws {st['ws_conns']}连接\n"
                    f"RR5轨: {r5['closed']}平/{r5['open']}持 胜率{r5['win_rate']}% pnl={r5['total_pnl']}U\n"
                    f"RR2.5轨: {r25['closed']}平/{r25['open']}持 胜率{r25['win_rate']}% pnl={r25['total_pnl']}U"
                )

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
