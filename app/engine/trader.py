"""实盘下单（live 模式）：市价入场 + 止盈/止损条件单。paper 模式不会走到这里。"""
import logging
import math
import time

log = logging.getLogger("trader")


def round_step(value: float, step: float) -> float:
    if not step or step <= 0:
        return value
    return math.floor(value / step) * step


class LiveTrader:
    def __init__(self, cfg, db, rest):
        self.cfg = cfg
        self.db = db
        self.rest = rest

    async def execute_signal(self, sid: int, sig_row) -> dict:
        """sig_row: signals 表的行。返回 {ok, message, orders}。"""
        symbol = sig_row["symbol"]
        direction = sig_row["direction"]
        meta = self.db.one("SELECT * FROM symbols WHERE symbol=?", (symbol,))
        if not meta:
            return {"ok": False, "message": f"{symbol} 元数据缺失"}

        lev = int(self.cfg.get("risk.leverage", 5))
        margin = float(self.cfg.get("live.fixed_margin_u", 0) or 0)
        margin_pct = float(self.cfg.get("live.fixed_margin_pct", 0) or 0)
        fixed = float(self.cfg.get("live.fixed_notional_u", 0) or 0)
        equity = float(self.cfg.get("risk.account_equity", 0) or 0)
        entry_px = float(sig_row["entry"]) or 0
        step = meta["step_size"] or 0
        if margin > 0 and entry_px > 0:
            qty = round_step(margin * lev / entry_px, step)             # 固定保证金: 名义=保证金×杠杆, 张数=名义/价
        elif margin_pct > 0 and equity > 0 and entry_px > 0:
            qty = round_step(equity * margin_pct / 100.0 * lev / entry_px, step)
        elif fixed > 0 and entry_px > 0:
            qty = round_step(fixed / entry_px, step)                    # 固定名义额: 张数=名义/价
        else:
            qty = round_step(float(sig_row["suggested_qty"]), step)
        if qty <= 0:
            return {"ok": False, "message": "数量过小，按精度取整后为0"}
        tick = meta["tick_size"] or 0

        def round_price(p: float) -> float:
            return round_step(p, tick) if tick else p

        side = "BUY" if direction == "long" else "SELL"
        close_side = "SELL" if direction == "long" else "BUY"
        orders = []
        try:
            await self.rest.set_leverage(symbol, lev)
            entry = await self.rest.place_order(
                symbol=symbol, side=side, type="MARKET", quantity=qty,
                newClientOrderId=f"chan{sid}e",
            )
            orders.append(entry)
            sl = await self.rest.place_order(
                symbol=symbol, side=close_side, type="STOP_MARKET",
                stopPrice=round_price(float(sig_row["sl"])),
                closePosition="true", workingType="MARK_PRICE",
                newClientOrderId=f"chan{sid}s",
            )
            orders.append(sl)
            tp = await self.rest.place_order(
                symbol=symbol, side=close_side, type="TAKE_PROFIT_MARKET",
                stopPrice=round_price(float(sig_row["tp"])),
                closePosition="true", workingType="MARK_PRICE",
                newClientOrderId=f"chan{sid}t",
            )
            orders.append(tp)
        except Exception as e:
            log.exception("execute signal #%d failed", sid)
            self.db.log("error", "trader", f"#{sid} {symbol} 下单失败: {e}")
            return {"ok": False, "message": f"下单失败: {e}", "orders": orders}

        for o in orders:
            self.db.execute(
                "INSERT INTO orders (signal_id, created_at, binance_order_id, client_order_id, symbol, side, type, qty, price, status, payload) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (sid, int(time.time()), str(o.get("orderId")), o.get("clientOrderId"),
                 symbol, o.get("side"), o.get("type"), qty,
                 float(o.get("avgPrice") or o.get("stopPrice") or 0), o.get("status"), str(o)[:1500]),
            )
        self.db.log("info", "trader", f"#{sid} {symbol} {direction} 实盘下单成功 qty={qty} lev={lev}")
        return {"ok": True, "message": f"已下单 {symbol} {side} qty={qty} 杠杆{lev}x，TP/SL已挂", "orders": orders}
