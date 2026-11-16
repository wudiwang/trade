"""Regenerate day_full.json: one invented trading day for the end-to-end review fixture.

Usage:  python tests/fixtures/bybit_review/make_day_full.py

The file replays the nine read-only GET endpoints the review uses.  Row shapes follow the
official Bybit V5 response tables (pages fetched 2026-11-17); every id, price and time is
invented, nothing was captured from a real account.
  order history     https://bybit-exchange.github.io/docs/v5/order/order-list
  open orders       https://bybit-exchange.github.io/docs/v5/order/open-order
  executions        https://bybit-exchange.github.io/docs/v5/order/execution
  positions         https://bybit-exchange.github.io/docs/v5/position
  transaction log   https://bybit-exchange.github.io/docs/v5/account/transaction-log
                    "change = cashFlow + funding - fee"; fee positive = expense
  closed PnL        https://bybit-exchange.github.io/docs/v5/position/close-pnl
                    the doc example (side Sell, cumEntryValue 3584.9255, cumExitValue 3541.795,
                    closedPnl -47.4065323) shows: side = side of the CLOSING order, and
                    closedPnl = exit value - entry value - open fee - close fee
  API key info      https://bybit-exchange.github.io/docs/v5/user/apikey-info  (readOnly 1)
  kline             https://bybit-exchange.github.io/docs/v5/market/kline
                    (candles are synthesised from "market" by app/review/fixture_exchange.py)

The day (2026-11-16, Asia/Shanghai), four trades, all closed, no funding:
  A  BTCUSDT short  entry order carries SL + TP, take-profit triggers        -> 合规
  B  ETHUSDT long   entry order carries SL + TP, closed by hand in profit,
                    75 minutes later price reaches the original target        -> 中途下车
  C  SOLUSDT short  no stop at all, closed by hand                            -> 无止损
  D  SOLUSDT long   opened 6 minutes after C was closed (a flip); SL + TP on
                    the entry order, stop-loss triggers                       -> 合规 + 当日"反手"
"""
from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

DAY0 = 1794758400000          # 2026-11-16 00:00:00 Asia/Shanghai
HOUR = 3_600_000
DAY = 24 * HOUR
FEE_RATE = Decimal("0.00055")
DOCS = {
    "order_history": "https://bybit-exchange.github.io/docs/v5/order/order-list",
    "open_orders": "https://bybit-exchange.github.io/docs/v5/order/open-order",
    "executions": "https://bybit-exchange.github.io/docs/v5/order/execution",
    "positions": "https://bybit-exchange.github.io/docs/v5/position",
    "transaction_log": "https://bybit-exchange.github.io/docs/v5/account/transaction-log",
    "closed_pnl": "https://bybit-exchange.github.io/docs/v5/position/close-pnl",
    "api_key_info": "https://bybit-exchange.github.io/docs/v5/user/apikey-info",
    "kline": "https://bybit-exchange.github.io/docs/v5/market/kline",
    "enums": "https://bybit-exchange.github.io/docs/v5/enum",
}


def at(hh: int, mm: int, ss: int = 0, ms: int = 0) -> int:
    return DAY0 + ((hh * 60 + mm) * 60 + ss) * 1000 + ms


def order(order_id, symbol, side, qty, created, updated, **kw):
    """One row shaped like the /v5/order/history response table."""
    row = {
        "orderId": order_id, "orderLinkId": "", "parentOrderLinkId": "", "blockTradeId": "",
        "symbol": symbol, "price": "0", "qty": qty, "side": side, "isLeverage": "",
        "positionIdx": 0, "orderStatus": "Filled", "createType": "CreateByUser",
        "cancelType": "UNKNOWN", "rejectReason": "EC_NoError", "avgPrice": "", "leavesQty": "0",
        "leavesValue": "0", "cumExecQty": "0", "cumExecValue": "0", "cumExecFee": "0",
        "timeInForce": "IOC", "orderType": "Market", "stopOrderType": "UNKNOWN", "orderIv": "",
        "marketUnit": "", "slippageToleranceType": "UNKNOWN", "slippageTolerance": "",
        "triggerPrice": "0.00", "takeProfit": "0.00", "stopLoss": "0.00", "tpslMode": "",
        "ocoTriggerBy": "", "tpLimitPrice": "", "slLimitPrice": "", "tpTriggerBy": "UNKNOWN",
        "slTriggerBy": "UNKNOWN", "triggerDirection": 0, "triggerBy": "UNKNOWN",
        "lastPriceOnCreated": "0.00", "basePrice": "", "reduceOnly": False,
        "closeOnTrigger": False, "placeType": "", "smpType": "None", "smpGroup": 0,
        "smpOrderId": "", "createdTime": str(created), "updatedTime": str(updated),
        "extraFees": "", "cumFeeDetail": {}, "rpiTakerAccess": False, "rpiMatchedQty": "0",
    }
    unknown = set(kw) - set(row)
    assert not unknown, unknown
    row.update(kw)
    return row


def filled(row, avg):
    row.update(avgPrice=avg, cumExecQty=row["qty"], orderStatus="Filled", leavesQty="0")
    return row


def entry_order(order_id, symbol, side, qty, when, avg, sl=None, tp=None):
    extra = {}
    if sl or tp:
        extra = dict(stopLoss=sl or "0.00", takeProfit=tp or "0.00", tpslMode="Full",
                     slTriggerBy="LastPrice", tpTriggerBy="LastPrice")
    return filled(order(order_id, symbol, side, qty, when, when + 6, price=avg,
                        lastPriceOnCreated=avg, **extra), avg)


def close_order(order_id, symbol, side, qty, when, avg):
    """The close order placed via web or app position area (createType CreateByClosing)."""
    return filled(order(order_id, symbol, side, qty, when, when + 9, price=avg, reduceOnly=True,
                        createType="CreateByClosing"), avg)


def tpsl(order_id, symbol, side, qty, created, updated, kind, trigger, direction, avg=None):
    """A system-created full-position TP/SL order; filled when ``avg`` is given, else
    deactivated because the position was closed some other way."""
    row = order(
        order_id, symbol, side, qty, created, updated,
        createType={"StopLoss": "CreateByStopLoss", "TakeProfit": "CreateByTakeProfit"}[kind],
        stopOrderType=kind, triggerPrice=trigger, triggerDirection=direction,
        triggerBy="LastPrice", reduceOnly=True, closeOnTrigger=True, tpslMode="Full")
    if avg is not None:
        return filled(row, avg)
    row.update(orderStatus="Deactivated", cancelType="CancelByTpSlTsClear")
    return row


def execution(exec_id, order_id, symbol, side, qty, price, when, **kw):
    """One row shaped like the /v5/execution/list response table (it has no positionIdx)."""
    value = Decimal(qty) * Decimal(price)
    row = {
        "symbol": symbol, "orderType": "Market", "underlyingPrice": "", "orderLinkId": "",
        "side": side, "indexPrice": "", "orderId": order_id, "stopOrderType": "UNKNOWN",
        "leavesQty": "0", "execTime": str(when), "feeCurrency": "", "isMaker": False,
        "execFee": format(value * FEE_RATE, "f"), "feeRate": str(FEE_RATE), "execId": exec_id,
        "tradeIv": "", "blockTradeId": "", "markPrice": price, "execPrice": price, "markIv": "",
        "orderQty": qty, "orderPrice": price, "execValue": format(value, "f"),
        "execType": "Trade", "execQty": qty, "closedSize": "0", "extraFees": "",
        "seq": 4688002127, "createType": "CreateByUser",
    }
    unknown = set(kw) - set(row)
    assert not unknown, unknown
    row.update(kw)
    return row


def position(symbol, side, size, avg, created, updated, **kw):
    """One row shaped like the /v5/position/list response table (an open position)."""
    row = {
        "positionIdx": 0, "riskId": 1, "riskLimitValue": "2000000", "symbol": symbol,
        "side": side, "size": size, "avgPrice": avg, "positionValue": "", "tradeMode": 0,
        "positionStatus": "Normal", "autoAddMargin": 0, "adlRankIndicator": 2,
        "leverage": "10", "positionBalance": "", "markPrice": avg, "liqPrice": "",
        "bustPrice": "", "positionMM": "", "positionIM": "", "tpslMode": "Full",
        "takeProfit": "0.00", "stopLoss": "0.00", "trailingStop": "0.00",
        "unrealisedPnl": "0", "curRealisedPnl": "0", "cumRealisedPnl": "0",
        "seq": 5723621632, "isReduceOnly": False, "mmrSysUpdatedTime": "",
        "leverageSysUpdatedTime": "", "sessionAvgPrice": "",
        "createdTime": str(created), "updatedTime": str(updated),
    }
    unknown = set(kw) - set(row)
    assert not unknown, unknown
    row.update(kw)
    return row


def envelope(rows):
    return [{"retCode": 0, "retMsg": "OK",
             "result": {"list": rows, "nextPageCursor": "", "category": "linear"},
             "retExtInfo": {}, "time": DAY0 + DAY + 15 * 60_000}]


def round_trip(symbol, entry, close, balance):
    """Ledger rows + closed-PnL row for one fully closed trade made of two executions."""
    qty = Decimal(entry["execQty"])
    entry_value, exit_value = Decimal(entry["execValue"]), Decimal(close["execValue"])
    gross = exit_value - entry_value if entry["side"] == "Buy" else entry_value - exit_value
    fees = (Decimal(entry["execFee"]), Decimal(close["execFee"]))
    rows = []
    for fill, cash in ((entry, Decimal("0")), (close, gross)):
        fee = Decimal(fill["execFee"])
        change = cash - fee                                   # change = cashFlow + funding - fee
        balance += change
        closing = fill is close
        rows.append({
            "id": f"tx-{fill['execId']}", "symbol": symbol, "category": "linear",
            "side": fill["side"], "transactionTime": fill["execTime"], "type": "TRADE",
            "transSubType": "", "qty": fill["execQty"],
            "size": "0" if closing else format(qty if fill["side"] == "Buy" else -qty, "f"),
            "currency": "USDT", "tradePrice": fill["execPrice"], "funding": "0",
            "fee": format(fee, "f"), "cashFlow": format(cash, "f"), "change": format(change, "f"),
            "cashBalance": format(balance, "f"), "feeRate": str(FEE_RATE), "bonusChange": "0",
            "tradeId": fill["execId"], "orderId": fill["orderId"], "orderLinkId": "",
            "extraFees": "", "displayType": "",
        })
    closed = {
        "symbol": symbol, "orderType": "Market", "leverage": "10",
        "updatedTime": close["execTime"], "side": close["side"], "orderId": close["orderId"],
        "closedPnl": format(gross - fees[0] - fees[1], "f"),
        "avgEntryPrice": entry["execPrice"], "qty": close["execQty"],
        "cumEntryValue": format(entry_value, "f"), "createdTime": close["execTime"],
        "orderPrice": close["execPrice"], "closedSize": close["execQty"],
        "avgExitPrice": close["execPrice"], "execType": "Trade", "fillCount": "1",
        "cumExitValue": format(exit_value, "f"),
        "openFee": format(fees[0], "f"), "closeFee": format(fees[1], "f"),
    }
    return rows, closed, balance


def zigzag(start_ms, prices, hours):
    """Waypoints ``hours`` apart through ``prices``."""
    return [[start_ms + int(index * hours * HOUR), price] for index, price in enumerate(prices)]


def market():
    before = DAY0 - 56 * DAY
    btc = zigzag(before, [112000, 107500, 111200, 107000, 110600, 106500, 99800,   # 高位中枢 → 下跌
                          100600, 96500, 100100, 96000, 99700, 96300, 92400,       # 中位中枢 → 下跌
                          95700, 92000, 95300, 91800, 95000, 92200, 94600, 92300], 60)
    btc += [[at(4, 0), 93100], [at(7, 30), 94300], [at(8, 20), 94550], [at(9, 10), 95180],
            [at(9, 30), 95010], [at(10, 30), 94250], [at(11, 10), 94600], [at(12, 30), 93300],
            [at(13, 42), 92585], [at(14, 30), 92380], [at(18, 0), 92900], [at(23, 59), 92650],
            [DAY0 + DAY + HOUR, 92700]]
    eth = zigzag(before, [2550, 2740, 2600, 2760, 2620, 2780, 2990,                # 低位中枢 → 上涨
                          2900, 3060, 2920, 3080, 2940, 3090, 3230,                # 中位中枢 → 上涨
                          3120, 3260, 3130, 3270, 3140, 3265, 3150, 3250], 60)
    eth += [[at(3, 0), 3228], [at(7, 40), 3243], [at(9, 45), 3187.5], [at(10, 5), 3200],
            [at(10, 40), 3190.5], [at(11, 20), 3222.4], [at(11, 36), 3211.5], [at(12, 36), 3282],
            [at(13, 30), 3291], [at(18, 0), 3270], [at(23, 59), 3276],
            [DAY0 + DAY + HOUR, 3275]]
    sol = zigzag(before, [236, 245, 233.5, 244.5, 233, 245.5, 234, 244, 232.5, 245, 233.5, 244.5,
                          234, 245, 233, 244, 234.5, 245, 233.5, 244, 234, 243, 236, 241.5], 56)
    sol += [[at(6, 0), 237.2], [at(11, 0), 239.4], [at(13, 0), 238.4], [at(13, 40), 239.0],
            [at(14, 2), 240.0], [at(14, 40), 241.8], [at(14, 46), 241.9], [at(15, 2), 242.7],
            [at(15, 31), 239.35], [at(16, 30), 238.1], [at(20, 0), 240.2], [at(23, 59), 239.6],
            [DAY0 + DAY + HOUR, 239.7]]
    return {
        "BTCUSDT": {"tick": "0.1", "wiggle": 0.0004, "path": btc},
        "ETHUSDT": {"tick": "0.01", "wiggle": 0.0004, "path": eth},
        "SOLUSDT": {"tick": "0.01", "wiggle": 0.0004, "path": sol},
    }


def build() -> dict:
    orders = [
        # A  BTCUSDT short: SL + TP on the entry order, take-profit triggers.
        entry_order("a-entry", "BTCUSDT", "Sell", "0.020", at(9, 30, 0, 120), "95012.40",
                    sl="95900.00", tp="92600.00"),
        tpsl("a-sl", "BTCUSDT", "Buy", "0.020", at(9, 30, 0, 126), at(13, 42, 17, 540),
             "StopLoss", "95900.00", 1),
        tpsl("a-tp", "BTCUSDT", "Buy", "0.020", at(9, 30, 0, 126), at(13, 42, 17, 530),
             "TakeProfit", "92600.00", 2, avg="92598.50"),
        # B  ETHUSDT long: SL + TP on the entry order, closed by hand before either triggers.
        entry_order("b-entry", "ETHUSDT", "Buy", "0.50", at(10, 5, 10, 0), "3200.00",
                    sl="3168.00", tp="3280.00"),
        tpsl("b-sl", "ETHUSDT", "Sell", "0.50", at(10, 5, 10, 6), at(11, 20, 31, 20),
             "StopLoss", "3168.00", 2),
        tpsl("b-tp", "ETHUSDT", "Sell", "0.50", at(10, 5, 10, 6), at(11, 20, 31, 20),
             "TakeProfit", "3280.00", 1),
        close_order("b-close", "ETHUSDT", "Sell", "0.50", at(11, 20, 30, 900), "3222.40"),
        # C  SOLUSDT short: no stop, closed by hand.
        entry_order("c-entry", "SOLUSDT", "Sell", "12", at(14, 2, 5, 0), "240.00"),
        close_order("c-close", "SOLUSDT", "Buy", "12", at(14, 40, 12, 0), "241.80"),
        # D  SOLUSDT long, six minutes after C was closed: SL + TP on the entry order, stop hit.
        entry_order("d-entry", "SOLUSDT", "Buy", "12", at(14, 46, 20, 0), "241.90",
                    sl="239.40", tp="247.00"),
        tpsl("d-sl", "SOLUSDT", "Sell", "12", at(14, 46, 20, 6), at(15, 31, 44, 310),
             "StopLoss", "239.40", 2, avg="239.38"),
        tpsl("d-tp", "SOLUSDT", "Sell", "12", at(14, 46, 20, 6), at(15, 31, 44, 320),
             "TakeProfit", "247.00", 1),
    ]
    trades = [
        ("BTCUSDT",
         execution("a-entry-1", "a-entry", "BTCUSDT", "Sell", "0.020", "95012.40", at(9, 30, 0, 124)),
         execution("a-tp-1", "a-tp", "BTCUSDT", "Buy", "0.020", "92598.50", at(13, 42, 17, 528),
                   closedSize="0.020", stopOrderType="TakeProfit", createType="CreateByTakeProfit")),
        ("ETHUSDT",
         execution("b-entry-1", "b-entry", "ETHUSDT", "Buy", "0.50", "3200.00", at(10, 5, 10, 4)),
         execution("b-close-1", "b-close", "ETHUSDT", "Sell", "0.50", "3222.40", at(11, 20, 30, 950),
                   closedSize="0.50", createType="CreateByClosing")),
        ("SOLUSDT",
         execution("c-entry-1", "c-entry", "SOLUSDT", "Sell", "12", "240.00", at(14, 2, 5, 4)),
         execution("c-close-1", "c-close", "SOLUSDT", "Buy", "12", "241.80", at(14, 40, 12, 5),
                   closedSize="12", createType="CreateByClosing")),
        ("SOLUSDT",
         execution("d-entry-1", "d-entry", "SOLUSDT", "Buy", "12", "241.90", at(14, 46, 20, 4)),
         execution("d-sl-1", "d-sl", "SOLUSDT", "Sell", "12", "239.38", at(15, 31, 44, 305),
                   closedSize="12", stopOrderType="StopLoss", createType="CreateByStopLoss")),
    ]
    return payload(
        orders, trades, market=market(),
        about="一个完整的示例交易日：合规(止盈触发)、中途下车、无止损、反手后止损触发 各一笔。")


def payload(orders, round_trips, *, market=None, about="", open_orders=(), positions=(),
            extra_executions=(), extra_ledger=(), now_ms=None, read_only=1) -> dict:
    """Assemble a fixture: ``round_trips`` are (symbol, entry execution, closing execution)."""
    executions, ledger, closed = list(extra_executions), list(extra_ledger), []
    balance = Decimal("5000")
    for symbol, entry, close in round_trips:
        rows, pnl, balance = round_trip(symbol, entry, close, balance)
        executions += [entry, close]
        ledger += rows
        closed.append(pnl)
    now_ms = DAY0 + DAY + 15 * 60_000 if now_ms is None else now_ms
    return {
        "about": about,
        "note": "Invented data shaped after the official Bybit V5 docs; not captured from a real account.",
        "docs": DOCS,
        "window": {"start_ms": DAY0, "end_ms": DAY0 + DAY, "day": "2026-11-16",
                   "timezone": "Asia/Shanghai"},
        "now_ms": now_ms,
        "responses": {
            "/v5/user/query-api": {"retCode": 0, "retMsg": "",
                                   "result": {"id": "fixture", "note": "fixture",
                                              "readOnly": read_only},
                                   "retExtInfo": {}, "time": now_ms},
            "/v5/execution/list": envelope(executions),
            "/v5/order/history": envelope(list(orders)),
            "/v5/order/realtime": envelope(list(open_orders)),
            "/v5/account/transaction-log": envelope(ledger),
            "/v5/position/closed-pnl": envelope(closed),
            "/v5/position/list": envelope(list(positions)),
        },
        "market": market or {},
    }


if __name__ == "__main__":
    target = Path(__file__).with_name("day_full.json")
    target.write_text(json.dumps(build(), ensure_ascii=False, indent=1) + "\n", encoding="utf-8",
                      newline="\n")
    print(target, target.stat().st_size, "bytes")
