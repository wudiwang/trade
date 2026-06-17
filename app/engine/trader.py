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
            sl = await self.rest.place_algo_order(
                algoType="CONDITIONAL",
                symbol=symbol, side=close_side, type="STOP_MARKET",
                triggerPrice=round_price(float(sig_row["sl"])),
                closePosition="true", workingType="MARK_PRICE",
                clientAlgoId=f"chan{sid}s",
            )
            orders.append(sl)
            tp = await self.rest.place_algo_order(
                algoType="CONDITIONAL",
                symbol=symbol, side=close_side, type="TAKE_PROFIT_MARKET",
                triggerPrice=round_price(float(sig_row["tp"])),
                closePosition="true", workingType="MARK_PRICE",
                clientAlgoId=f"chan{sid}t",
            )
            orders.append(tp)
        except Exception as e:
            log.exception("execute signal #%d failed", sid)
            has_entry = any(o.get("type") == "MARKET" for o in orders)
            has_stop = any((o.get("type") or o.get("orderType")) == "STOP_MARKET" for o in orders)
            if has_entry and not has_stop:
                try:
                    emergency = await self.rest.place_order(
                        symbol=symbol, side=close_side, type="MARKET", quantity=qty,
                        reduceOnly="true", newClientOrderId=f"chan{sid}x",
                    )
                    orders.append(emergency)
                    self.db.log("error", "trader", f"#{sid} {symbol} 保护单失败，已尝试reduceOnly市价平仓")
                except Exception as close_err:
                    self.db.log("error", "trader", f"#{sid} {symbol} 保护单失败且保护平仓失败: {close_err}")
            self._log_orders(sid, symbol, qty, orders)
            self.db.log("error", "trader", f"#{sid} {symbol} 下单失败: {e}")
            return {"ok": False, "message": f"下单失败: {e}", "orders": orders}

        self._log_orders(sid, symbol, qty, orders)
        self.db.log("info", "trader", f"#{sid} {symbol} {direction} 实盘下单成功 qty={qty} lev={lev}")
        return {"ok": True, "message": f"已下单 {symbol} {side} qty={qty} 杠杆{lev}x，TP/SL已挂", "orders": orders}

    def _log_orders(self, sid: int, symbol: str, qty: float, orders: list[dict]) -> None:
        for o in orders:
            self.db.execute(
                "INSERT INTO orders (signal_id, created_at, binance_order_id, client_order_id, symbol, side, type, qty, price, status, payload) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (sid, int(time.time()), str(o.get("orderId") or o.get("algoId")),
                 o.get("clientOrderId") or o.get("clientAlgoId"),
                 symbol, o.get("side"), o.get("type") or o.get("orderType"), qty,
                 float(o.get("avgPrice") or o.get("stopPrice") or o.get("triggerPrice") or 0),
                 o.get("status") or o.get("algoStatus"), str(o)[:1500]),
            )
