"""Telegram Bot：信号卡片推送 + [确认买入]两步确认 + 下单/记录。
轻量实现（aiohttp 直连 Bot API，long polling），不依赖重框架。
"""
import asyncio
import html
import json
import logging
import math
import re
import time

import aiohttp

# 「币种/价格」写法规则与状态文本，和网页端共用一份，定义在 web 层（web 不能反过来导入 bot）
from ..web.server import (ALERT_MAX_ACTIVE, ALERT_NOTE_MAX, alert_status_summary, fmt_alert_px, health_summary,
                          kv_text_lines, nfkc, norm_symbol, own_summary, parse_price)

log = logging.getLogger("tgbot")

API = "https://api.telegram.org/bot{token}/{method}"


# ---------- 价格提醒：指令解析 + 回复文本 ----------
# 这一段全是纯函数（不触网、不碰引擎内部），Telegram 与网页端共用同一套「币种/价格」写法规则。
# 提醒只有 设 / 查 / 删 三个动作，回复是一条纯文本——与下单路径（trader / 确认买入按钮）没有任何交集。

ALERT_OP_TEXT = {"below": "跌破", "above": "涨破"}
ALERT_USAGE = ("用法: 提醒 btc 63000 [备注] ｜ 提醒 btc 跌破 63000 ｜ 提醒 pump 涨破 0.00283\n"
               "查看: 提醒 ｜ 删除: 删除提醒 3")
ALERT_DISABLED = "🔕 价格提醒未启用（引擎未运行或已关闭）"

_ALERT_CMD_RE = re.compile(r"^/?alerts?(?:@\w+)?$", re.I)          # /alert /alerts alert（可带 @机器人名）
_ALERT_DEL_PREFIX = ("删除提醒", "取消提醒", "移除提醒", "删提醒")
_ALERT_DEL_WORDS = frozenset({"del", "delete", "remove", "删除", "取消", "移除", "删"})
_ALERT_LIST_WORDS = frozenset({"list", "ls", "列表", "查看", "全部"})
_ALERT_DIR = {**{w: "below" for w in ("跌破", "下破", "跌穿", "低于", "小于", "跌到", "跌至",
                                      "below", "under", "<=", "<", "≤")},
              **{w: "above" for w in ("涨破", "上破", "突破", "升破", "站上", "高于", "大于", "涨到", "涨至",
                                      "above", "over", ">=", ">", "≥")}}
# 可以和币种/价格粘在一起写的方向词（中文词与比较符号；英文词必须用空格隔开，免得切坏币种名）
_ALERT_DIR_GLUE = sorted((w for w in _ALERT_DIR if not w.isascii() or not w.isalpha()), key=len, reverse=True)
_ALERT_ID_RE = re.compile(r"^#?([0-9]{1,9})$")


def _clip(s: str, n: int) -> str:
    return s if len(s) <= n else s[:max(0, n - 1)] + "…"


def _split_glued_dir(tok: str, at_start: bool):
    """从一个词里拆出粘着写的方向词。at_start=True 只认开头（'跌破63000'）；
    否则认词中间（'btc跌破63000'）。返回 (前段, 方向, 后段) 或 None。"""
    best = None
    for w in _ALERT_DIR_GLUE:
        i = tok.find(w)
        if i < 0 or (at_start and i != 0) or (not at_start and i == 0):
            continue
        if best is None or i < best[0]:                       # 取最靠前的；同位置时长词在前（已按长度排序）
            best = (i, w)
    if best is None:
        return None
    i, w = best
    return tok[:i], _ALERT_DIR[w], tok[i + len(w):]


def parse_alert_command(text: str) -> dict | None:
    """把一条聊天文本解析成提醒指令。
    返回 None = 与提醒无关（调用方保持沉默）；否则是下面之一：
      {"action": "list"}
      {"action": "add", "symbol": "BTCUSDT", "price": 63000.0, "op": None | "below" | "above", "note": "…"}
      {"action": "remove", "id": 3}
      {"action": "usage"}        # 以提醒关键词开头但没看懂 → 回两行用法
    """
    toks = (text or "").split()
    if not toks:
        return None
    head, rest = nfkc(toks[0]), toks[1:]
    removing = False
    if not _ALERT_CMD_RE.fullmatch(head):
        for kw in _ALERT_DEL_PREFIX:
            if head.startswith(kw):
                removing, tail = True, head[len(kw):]
                break
        else:
            if not head.startswith("提醒"):
                return None
            tail = head[2:]
        tail = tail.lstrip(":,")                              # "提醒：btc 63000"
        if tail:
            rest = [tail] + rest                              # "提醒btc 63000" / "删除提醒3"

    if removing or (rest and nfkc(rest[0]).lower() in _ALERT_DEL_WORDS):
        ids = rest if removing else rest[1:]
        m = _ALERT_ID_RE.fullmatch(nfkc(ids[0])) if len(ids) == 1 else None
        if m and int(m.group(1)) > 0:
            return {"action": "remove", "id": int(m.group(1))}
        return {"action": "usage"}
    if not rest or (len(rest) == 1 and nfkc(rest[0]).lower() in _ALERT_LIST_WORDS):
        return {"action": "list"}

    # 新增：<币种> [方向] <价格> [备注…]，方向词可以粘着写
    first, queue, op = nfkc(rest[0]), list(rest[1:]), None
    glued = _split_glued_dir(first, at_start=False)
    if glued:                                                 # "btc跌破63000" / "btc<63000"
        first, op, tail = glued
        if tail:
            queue.insert(0, tail)
    elif queue:
        nxt = nfkc(queue[0])
        if nxt.lower() in _ALERT_DIR:                         # "btc 跌破 63000" / "btc below 63000"
            op = _ALERT_DIR[nxt.lower()]
            queue.pop(0)
        else:
            g = _split_glued_dir(nxt, at_start=True)          # "btc 跌破63000" / "btc <63000"
            if g:
                op, queue[0] = g[1], g[2]
    symbol = norm_symbol(first)
    price = parse_price(queue[0]) if queue else None
    if symbol is None or price is None:
        return {"action": "usage"}
    note = _clip(" ".join(queue[1:]).strip(), ALERT_NOTE_MAX)
    return {"action": "add", "symbol": symbol, "price": price, "op": op, "note": note}


def _dist_pct(level, last) -> str:
    """提醒价相对现价的距离，如 -1.75%；算不出返回空串。"""
    try:
        level, last = float(level), float(last)
    except (TypeError, ValueError):
        return ""
    if not (math.isfinite(level) and math.isfinite(last)) or last <= 0:
        return ""
    return f"{(level - last) / last * 100:+.2f}%"


def kv_lines(d, limit: int = 16, width: int = 200) -> list[str]:
    """别的模块给的 status 字典 → 「键: 值」行，已 HTML 转义，可直接放进 Telegram 消息。"""
    return [html.escape(x) for x in kv_text_lines(d, limit, width)]


def _level_line(a: dict, last) -> str:
    """一条提醒 → 一行 Telegram-HTML：#3 BTCUSDT 跌破 63,000 · 现价 64,120.5 (-1.75%) · 备注"""
    op = ALERT_OP_TEXT.get(a.get("op"), str(a.get("op") or "?"))
    s = (f"<b>#{html.escape(str(a.get('id', '?')))}</b> {html.escape(str(a.get('symbol') or '?'))} "
         f"{html.escape(op)} <code>{fmt_alert_px(a.get('price'))}</code>")
    if last is not None:
        dist = _dist_pct(a.get("price"), last)
        s += f" · 现价 {fmt_alert_px(last)}" + (f" ({dist})" if dist else "")
    note = str(a.get("note") or "").strip()
    if note:
        s += " · " + html.escape(_clip(note, ALERT_NOTE_MAX))
    return s


def _status_block(title: str, obj, absent: str, summarize=own_summary) -> list[str]:
    """obj.status() → 一行摘要（summarize 给得出的话），否则 标题 + 「键: 值」行。
    obj 为 None 写「未启用」；取状态抛异常写「读取失败」。"""
    if obj is None:
        return [f"{title}: {absent}"]
    try:
        st = obj.status()
        brief = summarize(st)
        if brief:
            return [f"{title}: {html.escape(brief)}"]
        kv = kv_lines(st)
    except Exception:
        log.exception("status() failed for %s", title)
        return [f"{title}: 状态读取失败"]
    return [f"<b>{title}</b>"] + kv if kv else [f"{title}: 无"]


def _fit(lines: list[str], budget: int) -> list[str]:
    """按整行截到 budget 个字符以内：每行的标签都是闭合的，整行取舍不会切出坏 HTML。"""
    out, used = [], 0
    for ln in lines:
        if used + len(ln) + 1 > budget:
            out.append("…")
            break
        out.append(ln)
        used += len(ln) + 1
    return out


def _alert_list_text(alerts, linked, health=None) -> str:
    lines: list[str] = []
    tail: list[str] = []                                      # 自动提醒 + 联动规则状态：先留出位置，不被长列表挤掉
    if alerts is None:
        lines.append(ALERT_DISABLED)
    else:
        try:
            levels = [dict(r) for r in alerts.list_levels()]
        except Exception:
            log.exception("list_levels failed")
            levels = None
        if levels is None:
            lines.append("⚠️ 提醒列表读取失败")
        elif not levels:
            lines.append("🔔 <b>价格提醒</b>: 暂无")
        else:
            lines.append(f"🔔 <b>价格提醒</b> {len(levels)} 条")
            prices: dict = {}
            for a in levels[:40]:
                sym = a.get("symbol")
                if sym not in prices:
                    try:
                        prices[sym] = alerts.last_price(sym)
                    except Exception:
                        prices[sym] = None
                lines.append(_level_line(a, prices[sym]))
            if len(levels) > 40:
                lines.append(f"…其余 {len(levels) - 40} 条略")
        tail.append("")
        tail += _status_block("⚙️ 自动提醒", alerts, "未启用", alert_status_summary)
    tail.append("")
    tail += _status_block("🔗 联动规则", linked, "未启用")
    bad = health_summary(health)                              # 通知通路出过问题(发送失败/丢弃/异常)才多这一行
    if bad:
        tail += ["", "⚠️ 通知通路: " + html.escape(bad)]
    tail = _fit(tail, 1800)                                   # Telegram 单条上限 4096 字符
    return "\n".join(_fit(lines, 3800 - sum(len(x) + 1 for x in tail)) + tail)


def alert_reply(cmd: dict, alerts, linked=None, health=None) -> str:
    """执行一条已解析的提醒指令，返回回给用户的一条 Telegram-HTML 文本。
    alerts = engine.alerts（可为 None）；linked = engine.linked（可为 None）；
    health = engine.notice_health() 的结果（可为 None；有发送失败/丢弃/异常时列表末尾多一行）。
    只调用提醒引擎的 查/设/删（内存 + 一次本地写库）；不涉及、也接触不到任何下单路径。"""
    act = cmd.get("action")
    if act == "usage":
        return ALERT_USAGE
    if act == "list":
        return _alert_list_text(alerts, linked, health)
    if alerts is None:
        return ALERT_DISABLED
    if act == "remove":
        aid = int(cmd["id"])
        return f"🗑 已删除提醒 #{aid}" if alerts.remove_level(aid) else f"⚠️ 没有生效中的提醒 #{aid}"
    if act == "add":
        sym, price, op, note = cmd["symbol"], cmd["price"], cmd.get("op"), cmd.get("note") or ""
        if len(alerts.list_levels()) >= ALERT_MAX_ACTIVE:
            return f"⚠️ 未添加: 生效中的提醒已达上限 {ALERT_MAX_ACTIVE} 条"
        res = alerts.add_level(sym, price, op, note)
        if not (isinstance(res, dict) and res.get("ok")):
            err = res.get("error") if isinstance(res, dict) else None
            return "⚠️ 未添加: " + html.escape(_clip(str(err or "原因不明"), 200))
        try:                                                  # 已经加成功了：后面取展示信息出任何岔子都不改口
            got = dict(res.get("alert") or {})
        except Exception:
            got = {}
        a = {"id": "?", "symbol": sym, "op": op, "price": price, "note": note, **got}
        try:
            last = alerts.last_price(a.get("symbol") or sym)
        except Exception:
            last = None
        return "🔔 已设提醒 " + _level_line(a, last)
    return ALERT_USAGE


def signal_confirmable(row) -> bool:
    """signals 表的一行能不能走「确认买入」。只认主信号(kind=primary：只有它带按钮发出过)，
    且不是只模拟的(status=sim)、不是标了 paper_only 的。读不出来按「不能」算。"""
    try:
        if str(row["kind"] or "") != "primary" or str(row["status"] or "") == "sim":
            return False
        extra = row["extra"]
        if isinstance(extra, str) and extra:
            extra = json.loads(extra)
        return not (isinstance(extra, dict) and extra.get("paper_only"))
    except Exception:
        return False


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
        self.engine = None            # 可选：总装时挂上引擎；没挂就用 web 层登记的那个（见 _engine）
        self.notify_retry_after = None   # 最近一次 notify 失败时 Telegram 要求的等待秒数（没有则 None）
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

    async def notify(self, text: str) -> bool | None:
        """发一条纯文字。返回 True=已送达 / False=Telegram 明确没收(ok=false: 限流 429、解析错误…) /
        None=机器人未启用。不比以前多抛任何异常(网络错误照旧由 api() 向上抛); 以前的调用方都不看返回值,
        行为不变 —— 只有引擎的提醒通路(_deliver_notices)据此重试, 一次性的提醒才不会因为一次发送失败就丢。"""
        if not self.enabled:
            return None
        data = await self.api("sendMessage", chat_id=self.chat_id, text=text, parse_mode="HTML")
        ok = isinstance(data, dict) and bool(data.get("ok"))
        wait = None
        if not ok and isinstance(data, dict):
            try:
                wait = float((data.get("parameters") or {}).get("retry_after"))
            except (TypeError, ValueError, AttributeError):
                wait = None
        self.notify_retry_after = wait        # Telegram 要求的等待秒数(429 时才有), 供重试方参考
        return ok

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

    def _engine(self):
        eng = self.engine
        if eng is None:
            from ..web.server import APP_STATE
            eng = APP_STATE.get("engine")
        return eng

    async def _on_message(self, m: dict) -> None:
        if str(m.get("chat", {}).get("id")) != str(self.chat_id):
            return
        raw = (m.get("text") or "").strip()
        text = raw.lower()
        if text in ("/status", "status", "状态"):
            eng = self._engine()
            if eng:
                st = eng.status()
                lines = [f"⚙️ 运行 {st['uptime_s']//3600}h{st['uptime_s']%3600//60}m | "
                         f"{st['symbols']}币 | ws {st['ws_conns']}连接"]
                for name, t in st.get("tracks", {}).items():
                    lines.append(f"{name}: {t['closed']}平/{t['open']}持 "
                                 f"胜率{t['win_rate']}% pnl={t['total_pnl']}U 期望{t['expectancy_r']}R")
                await self.notify("\n".join(lines))
            return
        await self._on_alert_text(raw)

    async def _on_alert_text(self, raw: str) -> None:
        """价格提醒指令：设 / 查 / 删，回一条纯文本。与提醒无关的消息保持沉默（同以前）。
        任何异常都吞在这里：不能让一条聊天消息拖住轮询循环（确认按钮的回调也走那个循环）。"""
        try:
            cmd = parse_alert_command(raw)
            if cmd is None:
                return
            eng = self._engine()
            health = None
            try:
                fn = getattr(eng, "notice_health", None)
                health = fn() if callable(fn) else None
            except Exception:
                log.exception("notice_health failed")
            reply = alert_reply(cmd, getattr(eng, "alerts", None), getattr(eng, "linked", None), health)
        except Exception:
            log.exception("alert command failed")
            reply = "⚠️ 提醒指令处理失败，稍后再试"
        try:
            await self.notify(reply)
        except Exception:
            log.exception("alert reply send failed")

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

        # 第二道闸：只有「带确认按钮发出去的主信号」才能走确认/下单。提示型(kind=alert)与只模拟的行
        # (status=sim，如联动规则)本来就不带按钮；万一旧消息上的按钮指到了这样一行(库被重置/恢复后 id 对不上)，
        # 这里直接拒绝，不改它的状态。只会让能下单的情形变少，不会变多。
        if action in ("c", "c2") and not signal_confirmable(row):
            await self._finish(cq_id, msg, f"⛔ #{sid} 不是可下单的信号（提示/模拟记录）", "该信号不可下单")
            return

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
