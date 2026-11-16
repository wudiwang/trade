"""Read-only Bybit V5 client for the daily review of real trades.

SAFETY
======
Every request goes through ``BybitReadClient._get``: HTTP GET only, and only to the paths in
``READ_PATHS``.  A path outside that allow-list raises before anything is sent, also when a
custom ``requester`` is injected.  Nothing here can place, amend or cancel an order, move funds
or change settings.  The trading-stop and amend pages below were *read* to understand how the
data looks afterwards; those POST endpoints are never called.

Doc pages (fetched 2026-11-17).  D = https://bybit-exchange.github.io/docs/v5
  [HIST]  D/order/order-list            GET  /v5/order/history
  [OPEN]  D/order/open-order            GET  /v5/order/realtime
  [EXEC]  D/order/execution             GET  /v5/execution/list
  [POS]   D/position                    GET  /v5/position/list
  [ENUM]  D/enum                        orderStatus, createType, stopOrderType, execType, cancelType
  [TSTOP] D/position/trading-stop       (POST - documentation only)
  [AMEND] D/order/amend-order           (POST - documentation only)
  [PLACE] D/order/create-order          (POST - documentation only)


WHAT THE EXCHANGE KEEPS, AND FOR HOW LONG  [HIST]
=================================================
* last 7 days: all closed statuses "except Cancelled, Rejected, Deactivated";
* last 24 hours only: Cancelled ("fully cancelled order"), Rejected and Deactivated orders;
* older than 7 days: "orders which have fills only".
* at most 7 days per request (endTime - startTime <= 7 days); cursor pagination; limit 1..50.
* the page does NOT say whether startTime/endTime filter on created or updated time, nor the
  sort order.  ``order_history`` therefore returns every row the exchange returns for the
  window and does not filter by time client-side.

Consequence for the review: a protective stop that never fired (the position was closed some
other way, or the user removed the stop) ends as Cancelled/Deactivated and DISAPPEARS from
history after 24 hours.  Proof that "a stop existed" then survives only (a) as
``attached_sl`` on the filled entry order, or (b) in rows that were synced and stored locally
while they were still visible.  Sync at least every few hours and keep every observation.

[OPEN] returns open orders (New, PartiallyFilled, Untriggered) and, with openOnly=1, "recent
500 closed status" records that are cleared when the exchange service restarts.


FIELD MAPPING  raw Bybit row -> models.OrderRecord
==================================================
order_id        orderId                                                            [HIST]
symbol, side    symbol, side ("Buy" | "Sell")                                      [HIST]
position_idx    positionIdx: 0 one-way, 1 hedge Buy side, 2 hedge Sell side        [ENUM positionIdx]
order_type      orderType "Market" | "Limit"; "For TP/SL orders, it is the type
                after triggering"                                                  [HIST]
qty             qty "Order qty".  For position TP/SL the system adjusts qty "to
                the open position size"                                            [HIST][TSTOP]
filled_qty      cumExecQty "Cumulative executed order qty"                         [HIST]
price           price "Order price"; "" / 0 -> None                                [HIST]
avg_fill_price  avgPrice 'returns "" for those orders without avg price'; the doc
                example shows "0" for an unfilled order -> None                    [HIST]
trigger_price   triggerPrice; "" / 0 -> None.  "If stopOrderType=TrailingStop, it
                is activate price"                                                 [HIST]
attached_sl     stopLoss "Stop loss price"; "" / 0 -> None                         [HIST]
attached_tp     takeProfit "Take profit price"; "" / 0 -> None                     [HIST]
created_ms      createdTime "Order created timestamp (ms)"                         [HIST]
updated_ms      updatedTime "Order updated timestamp (ms)"                         [HIST]
reduce_only     reduceOnly ("true means reduce position size") OR closeOnTrigger
                ("For a closing order. It can only reduce your position, not
                increase it")                                                      [HIST][PLACE]
close_position  True only when the row itself is a TP/SL/trailing order (origin
                stop_loss / take_profit / trailing_stop) AND tpslMode == "Full"
                ("Full: entire position for TP/SL. Partial: partial position
                tp/sl").  closeOnTrigger alone does NOT set it: the docs only say
                such an order cannot increase the position, not that it closes all
                of it.  tpslMode on an ENTRY order describes its attached TP/SL,
                so an entry order is never close_position.                         [HIST][PLACE]

status  <- orderStatus                                                             [ENUM orderStatus]
  New                      -> "new"               "order has been placed successfully"
  PartiallyFilled          -> "partially_filled"
  Untriggered              -> "untriggered"       "Conditional orders are created"
  Triggered                -> "new"               "instantaneous state for conditional orders
                                                  from Untriggered to New"
  Filled                   -> "filled"
  Cancelled                -> "cancelled"         "In derivatives, orders with this status may
                                                  have an executed qty" (check filled_qty)
  PartiallyFilledCanceled  -> "cancelled"         "Only spot has this order status"
  Deactivated              -> "cancelled"         "UTA: Spot tp/sl order, conditional order, OCO
                                                  order are cancelled before they are triggered"
  Rejected                 -> "rejected"
  anything else            -> "unknown"           undocumented value, never guessed
  "expired" is in the shared vocabulary but no documented Bybit status maps to it.

origin  <- createType (who/what produced the order) + stopOrderType                [ENUM createType]
  createType "Order create type. Spot does not have this key"                      [ENUM stopOrderType]
  CreateByLiq                    -> "liquidation"   "Laddered liquidation to reduce the required
                                                    maintenance margin"
  CreateByTakeOver_PassThrough   -> "liquidation"   "taken over by the liquidation engine and
                                                    closed at the bankruptcy price"
  CreateByAdl_PassThrough        -> "adl"           "Auto-Deleveraging(ADL)"
  CreateByStopLoss               -> "stop_loss"     "Futures stop loss order"
  CreateByPartialStopLoss        -> "stop_loss"     "Futures partial stop loss order"
  CreateByTakeProfit             -> "take_profit"   "Futures take profit order"
  CreateByPartialTakeProfit      -> "take_profit"   "Futures partial take profit order"
  CreateByTrailingStop           -> "trailing_stop" "Futures trailing stop order"
  CreateByTrailingProfit         -> "trailing_stop" "Futures trailing take profit order"
  CreateByUser                   -> "user"
  CreateByClosing                -> "user"          "The close order placed via web or app
                                                    position area"
  CreateByStopOrder              -> "user"          "Futures conditional order" (placed by the
                                                    user; it can be an entry OR a hand-made
                                                    stop: read trigger_price + reduce_only)
  CreateByBboOrder               -> "user"          "BBO order"
  CreateByChaseOrder             -> "user"          "Chase limit order"
  CreateByAdminClosing           -> "system"
  CreateBySettle                 -> "system"        "position closed as a result of the delisting
                                                    of a contract"
  CreateByMmRateClose            -> "system"        "Order created by Mm rate close function"
  bots / TWAP / iceberg / TV signal / arbitrage / DDH / spread / block / move position,
  a missing createType, or any undocumented value   -> no verdict from createType
  stopOrderType refines it:
  StopLoss, PartialStopLoss      -> "stop_loss"
  TakeProfit, PartialTakeProfit  -> "take_profit"
  TrailingStop                   -> "trailing_stop"
  MmRateClose                    -> "system"        "On web or app can set MMR to close position"
  Stop                           -> "user"          (conditional order; no description in docs)
  "" / "UNKNOWN"                 -> no verdict      [EXEC]: "If the order is not stop order, it
                                                    either returns UNKNOWN or ''"
  Combination rule: liquidation/adl from createType always win; if both fields give a verdict
  and they agree -> that verdict; if one says "user" and the other names a TP/SL/trailing kind
  -> the TP/SL kind (who placed it does not change what it is); any other disagreement ->
  "unknown"; one verdict -> it; no verdict -> "unknown".

Execution.origin  <- execType first, then the rule above                           [ENUM execType]
  AdlTrade  -> "adl"          "Auto-Deleveraging"
  BustTrade -> "liquidation"  "Takeover liquidation"
  Delivery  -> "system"       "USDT futures delivery; Position closed due to delisting"
  Settle    -> "system"       "Inverse futures settlement"
  Trade / missing -> createType + stopOrderType of the fill, as for orders
  Funding   -> the row is NOT a fill ("Funding fee"); ``executions`` drops it
  BlockTrade, MovePosition, FutureSpread, *Split*, UNKNOWN -> "unknown"
  Execution.closed_size = closedSize "Closed position size" (0 / "" on an opening fill).
  Execution.position_idx: the documented execution row has NO positionIdx.  ``executions``
  therefore yields 0 for every fill; join with the order rows through ``assign_position_idx``
  before reconstructing positions, or hedge-mode legs are merged.

HOW EACH EXIT LOOKS (derived from the tables above)
  stop triggered     closing fill with origin "stop_loss"; its order: status "filled",
                     trigger_price set, created_ms = when the stop was set
  target triggered   closing fill with origin "take_profit"
  target via a resting reduce-only LIMIT order: origin "user", order_type "Limit",
                     reduce_only True, no trigger_price, created_ms well before the fill.
                     The exchange does not label it a take-profit; compare price with entry.
  manual close       origin "user", usually order_type "Market", reduce_only True
  liquidation / ADL  origin "liquidation" / "adl"
  trailing stop      origin "trailing_stop"


WHICH orderFilter SHOWS POSITION TP/SL  [OPEN][HIST][TSTOP]
==========================================================
orderFilter values: "Order: active order", "StopOrder: conditional order for Futures and
Spot", and three spot-only kinds (tpslOrder, OcoOrder, BidirectionalTpslOrder).  When it is
omitted the pages say "all kinds of orders by default".  [TSTOP] says setting TP/SL on a
position "will create conditional orders by the system internally", so position TP/SL are
conditional orders.  The pages never state in so many words that they are listed under
StopOrder, so both fetchers query twice - once without orderFilter and once with
orderFilter=StopOrder - and merge by orderId (newest updatedTime wins).


CAN A MOVED STOP BE SEEN?  (plain answer: NOT RELIABLY)
=======================================================
What the docs say:
* [TSTOP] full-position TP/SL: "This API can be used to modify the parameters of existing
  TP/SL orders"; partial-position: "This API can only add partial position TP/SL orders".
  The response is empty - no order id.
* [AMEND] only "unfilled or partially filled orders" can be amended; the request "is
  asynchronous"; nothing about how the amended order is listed afterwards.
* [HIST] "Amending take-profit or stop-loss orders does not change the parentOrderLinkId";
  for futures, updating attached TP/SL through trading-stop does not change it either.
* An order row has exactly ONE triggerPrice, one createdTime and one updatedTime.  No V5
  REST endpoint returns earlier trigger prices or an amendment log.
What the docs do NOT say: whether a modified stop keeps its orderId, and whether triggerPrice
in history is the original or the latest value.

Therefore the original trigger price of a stop that was modified in place is NOT recoverable
from exchange data.  A check "止损是否被移动" must answer "无法从交易所数据判断" unless one of
the evidence sources below settles it; equal-looking data is never proof of "not moved".
Evidence that IS exposed here:
  1. ``attached_sl`` / ``attached_tp`` of the filled entry order = the value carried by the
     order at placement.  If the stop order that later existed for the same position has a
     different ``trigger_price``, the stop was changed after entry.  (Whether Bybit rewrites
     the entry row's stopLoss after a later trading-stop change is undocumented - verify once
     on real data.)
  2. Every TP/SL row for the symbol with created_ms / updated_ms / status
     (``protective_orders``).  A cancelled stop followed by a new stop at another trigger
     price is a visible replacement - but only if the cancelled row was synced within 24 h.
     updated_ms > created_ms on a stop does not prove a move: the system also updates the row
     when the position size changes [TSTOP].
  3. Repeated observations of the same order id taken by our own syncs
     (``trigger_price_changes``).  This is the only complete record, and only from the moment
     the syncs run: store every (order_id, updated_ms) observation instead of overwriting.
"""
from __future__ import annotations

import hashlib
import hmac
import time
from dataclasses import replace
from decimal import Decimal
from typing import Any, Awaitable, Callable, Iterable
from urllib.parse import urlencode

import aiohttp

from .models import ClosedPnl, Execution, Kline, OrderRecord, Transaction, decimal


Requester = Callable[[str, dict[str, Any], bool], Awaitable[dict[str, Any]]]

# The complete set of endpoints this client may call.  All are documented as HTTP GET queries.
READ_PATHS = frozenset({
    "/v5/user/query-api",
    "/v5/execution/list",
    "/v5/order/history",
    "/v5/order/realtime",
    "/v5/account/transaction-log",
    "/v5/position/closed-pnl",
    "/v5/position/list",
    "/v5/account/wallet-balance",
    "/v5/market/kline",
})

WINDOW_MS = 7 * 86_400_000          # [HIST][EXEC] endTime - startTime <= 7 days
ORDER_PAGE_LIMIT = 50               # [HIST][OPEN] limit range [1, 50]
ORDER_FILTERS: tuple[str | None, ...] = (None, "StopOrder")
PROTECTIVE_ORIGINS = frozenset({"stop_loss", "take_profit", "trailing_stop"})

_STATUS = {
    "New": "new",
    "PartiallyFilled": "partially_filled",
    "Untriggered": "untriggered",
    "Triggered": "new",
    "Filled": "filled",
    "Cancelled": "cancelled",
    "PartiallyFilledCanceled": "cancelled",
    "Deactivated": "cancelled",
    "Rejected": "rejected",
}
_FORCED_CREATE = {
    "CreateByLiq": "liquidation",
    "CreateByTakeOver_PassThrough": "liquidation",
    "CreateByAdl_PassThrough": "adl",
}
_CREATE_ORIGIN = {
    "CreateByStopLoss": "stop_loss",
    "CreateByPartialStopLoss": "stop_loss",
    "CreateByTakeProfit": "take_profit",
    "CreateByPartialTakeProfit": "take_profit",
    "CreateByTrailingStop": "trailing_stop",
    "CreateByTrailingProfit": "trailing_stop",
    "CreateByUser": "user",
    "CreateByClosing": "user",
    "CreateByStopOrder": "user",
    "CreateByBboOrder": "user",
    "CreateByChaseOrder": "user",
    "CreateByAdminClosing": "system",
    "CreateBySettle": "system",
    "CreateByMmRateClose": "system",
}
_STOP_ORIGIN = {
    "StopLoss": "stop_loss",
    "PartialStopLoss": "stop_loss",
    "TakeProfit": "take_profit",
    "PartialTakeProfit": "take_profit",
    "TrailingStop": "trailing_stop",
    "MmRateClose": "system",
    "Stop": "user",
}
_EXEC_ORIGIN = {
    "AdlTrade": "adl",
    "BustTrade": "liquidation",
    "Delivery": "system",
    "Settle": "system",
}
_EXEC_BY_ORDER = {"", "Trade"}      # classify these fills by their order's createType/stopOrderType
NON_FILL_EXEC_TYPES = frozenset({"Funding"})


class BybitError(RuntimeError):
    pass


class ReadOnlyKeyRequired(BybitError):
    pass


def _boolean(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"true", "1", "yes"}
    return bool(value)


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _optional_price(value: Any) -> Decimal | None:
    """Bybit writes "not set" as "", "0" or "0.00"; a real price is never zero."""
    if value in (None, ""):
        return None
    number = Decimal(str(value))
    return number if number != 0 else None


def _millis(value: Any) -> int:
    return int(value) if value not in (None, "") else 0


def order_origin(create_type: Any, stop_order_type: Any) -> str:
    """Normalise who/what produced an order.  Mapping table: see the module docstring."""
    create = _text(create_type)
    if create in _FORCED_CREATE:
        return _FORCED_CREATE[create]
    by_create = _CREATE_ORIGIN.get(create)
    by_stop = _STOP_ORIGIN.get(_text(stop_order_type))
    if by_create is None or by_stop is None:
        return by_create or by_stop or "unknown"
    if by_create == by_stop:
        return by_create
    if by_create == "user" and by_stop in PROTECTIVE_ORIGINS:
        return by_stop
    if by_stop == "user" and by_create in PROTECTIVE_ORIGINS:
        return by_create
    return "unknown"


def execution_origin(exec_type: Any, create_type: Any, stop_order_type: Any) -> str:
    """Normalise how a fill happened.  Mapping table: see the module docstring."""
    kind = _text(exec_type)
    if kind in _EXEC_ORIGIN:
        return _EXEC_ORIGIN[kind]
    if kind in _EXEC_BY_ORDER:
        return order_origin(create_type, stop_order_type)
    return "unknown"


def normalize_order(row: dict[str, Any], account_id: str = "default") -> OrderRecord:
    """One raw /v5/order/history or /v5/order/realtime row -> OrderRecord (pure)."""
    order_id = _text(row.get("orderId"))
    if not order_id:
        raise BybitError("order row without orderId")
    origin = order_origin(row.get("createType"), row.get("stopOrderType"))
    return OrderRecord(
        exchange="bybit",
        account_id=account_id,
        order_id=order_id,
        symbol=_text(row.get("symbol")),
        side=_text(row.get("side")),
        position_idx=int(row.get("positionIdx", 0) or 0),
        order_type=_text(row.get("orderType")),
        qty=decimal(row.get("qty")),
        filled_qty=decimal(row.get("cumExecQty")),
        price=_optional_price(row.get("price")),
        avg_fill_price=_optional_price(row.get("avgPrice")),
        trigger_price=_optional_price(row.get("triggerPrice")),
        reduce_only=_boolean(row.get("reduceOnly", False)) or _boolean(row.get("closeOnTrigger", False)),
        close_position=origin in PROTECTIVE_ORIGINS and _text(row.get("tpslMode")) == "Full",
        status=_STATUS.get(_text(row.get("orderStatus")), "unknown"),
        origin=origin,
        attached_sl=_optional_price(row.get("stopLoss")),
        attached_tp=_optional_price(row.get("takeProfit")),
        created_ms=_millis(row.get("createdTime")),
        updated_ms=_millis(row.get("updatedTime")),
    )


def protective_orders(
    orders: Iterable[OrderRecord], symbol: str, position_idx: int | None = None
) -> list[OrderRecord]:
    """Every stop / target candidate for one position, oldest first (pure).

    Included: exchange-labelled TP/SL/trailing orders, and user orders that can only reduce the
    position and carry a trigger price (a hand-made conditional stop or target).  Resting
    reduce-only limit orders without a trigger are NOT included; they are ordinary exits.
    """
    rows = [
        order for order in orders
        if order.symbol == symbol
        and (position_idx is None or order.position_idx == position_idx)
        and (order.origin in PROTECTIVE_ORIGINS
             or (order.reduce_only and order.trigger_price is not None))
    ]
    return sorted(rows, key=lambda order: (order.created_ms, order.updated_ms, order.order_id))


def trigger_price_changes(
    observations: Iterable[OrderRecord],
) -> list[tuple[str, int, Decimal | None, Decimal | None]]:
    """Trigger-price moves proven by repeated observations of the same order id (pure).

    ``observations`` are OrderRecords collected by several syncs (each sync sees only the
    latest state).  Returns ``(order_id, updated_ms_of_the_new_state, old, new)`` for every
    consecutive pair of observations whose trigger price differs, oldest first.  An empty
    result means "no move seen between our observations" - not "never moved".
    """
    by_order: dict[str, dict[int, OrderRecord]] = {}
    for order in observations:
        by_order.setdefault(order.order_id, {})[order.updated_ms] = order
    changes: list[tuple[str, int, Decimal | None, Decimal | None]] = []
    for order_id, seen in by_order.items():
        ordered = [seen[key] for key in sorted(seen)]
        for before, after in zip(ordered, ordered[1:]):
            if before.trigger_price != after.trigger_price:
                changes.append((order_id, after.updated_ms, before.trigger_price,
                                after.trigger_price))
    return sorted(changes, key=lambda change: (change[1], change[0]))


def assign_position_idx(
    executions: Iterable[Execution], orders: Iterable[OrderRecord]
) -> tuple[list[Execution], list[str]]:
    """Give every fill the position_idx of its order (pure).

    The documented /v5/execution/list response has NO positionIdx field [EXEC]; only order
    rows carry it [HIST].  Without this join every fill looks like one-way mode (0) and the two
    legs of a hedge-mode symbol would be merged into one position.

    Returns ``(fills, unresolved_exec_ids)``.  A fill whose order is not among ``orders`` keeps
    the value it already has and its exec_id is reported as unresolved - the caller must not
    assume one-way mode for those when the symbol is traded in hedge mode.
    """
    idx_by_order = {order.order_id: order.position_idx for order in orders}
    fills: list[Execution] = []
    unresolved: list[str] = []
    for execution in executions:
        idx = idx_by_order.get(execution.order_id)
        if idx is None:
            unresolved.append(execution.exec_id)
            fills.append(execution)
        elif idx == execution.position_idx:
            fills.append(execution)
        else:
            fills.append(replace(execution, position_idx=idx))
    return fills, unresolved


def sign_v5(secret: str, timestamp: str, api_key: str, recv_window: str, query: str) -> str:
    payload = f"{timestamp}{api_key}{recv_window}{query}".encode()
    return hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()


def encode_query(params: dict[str, Any]) -> str:
    clean = {key: value for key, value in params.items() if value not in (None, "")}
    return urlencode(sorted(clean.items()))


class BybitReadClient:
    def __init__(
        self,
        api_key: str,
        api_secret: str,
        *,
        base_url: str = "https://api.bybit.com",
        recv_window: int = 5000,
        requester: Requester | None = None,
    ):
        self.api_key = api_key
        self._api_secret = api_secret
        self.base_url = base_url.rstrip("/")
        self.recv_window = str(recv_window)
        self._transport = requester or self._request
        self._requester = self._get

    async def _get(self, path: str, params: dict[str, Any], private: bool) -> dict[str, Any]:
        """The single door to the exchange: allow-listed read paths only."""
        if path not in READ_PATHS:
            raise BybitError(f"blocked non-read endpoint: {path}")
        return await self._transport(path, params, private)

    async def _request(self, path: str, params: dict[str, Any], private: bool) -> dict[str, Any]:
        query = encode_query(params)
        headers = {}
        if private:
            timestamp = str(int(time.time() * 1000))
            headers = {
                "X-BAPI-API-KEY": self.api_key,
                "X-BAPI-TIMESTAMP": timestamp,
                "X-BAPI-RECV-WINDOW": self.recv_window,
                "X-BAPI-SIGN": sign_v5(
                    self._api_secret, timestamp, self.api_key, self.recv_window, query
                ),
            }
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as session:
            url = f"{self.base_url}{path}" + (f"?{query}" if query else "")
            async with session.get(url, headers=headers) as response:
                response.raise_for_status()
                payload = await response.json()
        if int(payload.get("retCode", 0)) != 0:
            raise BybitError(f"{path}: {payload.get('retCode')} {payload.get('retMsg', '')}")
        return payload

    async def verify_read_only(self) -> dict[str, Any]:
        payload = await self._requester("/v5/user/query-api", {}, True)
        result = payload.get("result") or {}
        if int(result.get("readOnly", 0)) != 1:
            raise ReadOnlyKeyRequired("Bybit API key must be read-only")
        return {"id": str(result.get("id", "")), "read_only": True}

    async def _page_rows(self, path: str, params: dict[str, Any]) -> list[dict]:
        """Follow nextPageCursor to the end; every row of every page, in arrival order."""
        cursor = ""
        seen_cursors: set[str] = set()
        rows: list[dict] = []
        while True:
            page_params = dict(params)
            if cursor:
                page_params["cursor"] = cursor
            payload = await self._requester(path, page_params, True)
            result = payload.get("result") or {}
            rows.extend(result.get("list") or [])
            cursor = str(result.get("nextPageCursor") or "")
            if not cursor:
                break
            if cursor in seen_cursors:
                raise BybitError(f"{path}: pagination cursor repeated")
            seen_cursors.add(cursor)
        return rows

    async def _pages(self, path: str, params: dict[str, Any], id_key: str) -> list[dict]:
        rows: dict[str, dict] = {}
        for row in await self._page_rows(path, params):
            rows.setdefault(str(row.get(id_key, "")), row)
        return list(rows.values())

    async def executions(
        self, category: str, start_ms: int, end_ms: int, *, include_non_fills: bool = False
    ) -> list[Execution]:
        """Fills in [start_ms, end_ms).  Funding rows are not fills and are dropped by default."""
        raw_by_id: dict[str, dict] = {}
        cursor_ms = start_ms
        while cursor_ms < end_ms:
            chunk_end = min(end_ms, cursor_ms + WINDOW_MS)
            chunk = await self._pages(
                "/v5/execution/list",
                {"category": category, "startTime": cursor_ms,
                 "endTime": chunk_end - 1, "limit": 100},
                "execId",
            )
            for item in chunk:
                raw_by_id[str(item.get("execId", ""))] = item
            cursor_ms = chunk_end
        raw = list(raw_by_id.values())
        rows = [
            Execution(
                exec_id=str(item["execId"]),
                order_id=str(item.get("orderId", "")),
                category=category,
                symbol=str(item.get("symbol", "")),
                side=str(item.get("side", "")),
                qty=decimal(item.get("execQty")),
                price=decimal(item.get("execPrice")),
                value=decimal(item.get("execValue")),
                fee=decimal(item.get("execFee")),
                timestamp_ms=int(item.get("execTime", 0)),
                leaves_qty=decimal(item.get("leavesQty")),
                is_maker=_boolean(item.get("isMaker", False)),
                position_idx=int(item.get("positionIdx", 0) or 0),
                exec_type=_text(item.get("execType")),
                stop_order_type=_text(item.get("stopOrderType")),
                create_type=_text(item.get("createType")),
                closed_size=decimal(item.get("closedSize")),
                order_type=_text(item.get("orderType")),
                origin=execution_origin(
                    item.get("execType"), item.get("createType"), item.get("stopOrderType")
                ),
            )
            for item in raw
        ]
        rows = [row for row in rows if start_ms <= row.timestamp_ms < end_ms]
        if not include_non_fills:
            rows = [row for row in rows if row.exec_type not in NON_FILL_EXEC_TYPES]
        return sorted(rows, key=lambda row: (row.timestamp_ms, row.exec_id, row.order_id, row.leaves_qty))

    @staticmethod
    def _keep_newest(rows: dict[str, dict], row: dict) -> None:
        order_id = _text(row.get("orderId"))
        if not order_id:
            raise BybitError("order row without orderId")
        known = rows.get(order_id)
        if known is None or _millis(row.get("updatedTime")) > _millis(known.get("updatedTime")):
            rows[order_id] = row

    async def order_history_raw(
        self,
        category: str,
        start_ms: int,
        end_ms: int,
        *,
        symbol: str | None = None,
        order_filters: tuple[str | None, ...] = ORDER_FILTERS,
    ) -> list[dict]:
        """Raw rows of GET /v5/order/history, one per orderId (newest updatedTime wins).

        The window is split into <= 7-day requests.  Rows are NOT filtered by time here: the
        docs do not say which timestamp the exchange filters on.  Ask for a window that starts
        early enough to contain the creation of every stop you care about.
        """
        newest: dict[str, dict] = {}
        for order_filter in order_filters:
            cursor_ms = start_ms
            while cursor_ms < end_ms:
                chunk_end = min(end_ms, cursor_ms + WINDOW_MS)
                params: dict[str, Any] = {
                    "category": category, "startTime": cursor_ms, "endTime": chunk_end - 1,
                    "limit": ORDER_PAGE_LIMIT,
                }
                if symbol:
                    params["symbol"] = symbol
                if order_filter:
                    params["orderFilter"] = order_filter
                for row in await self._page_rows("/v5/order/history", params):
                    self._keep_newest(newest, row)
                cursor_ms = chunk_end
        return sorted(newest.values(),
                      key=lambda row: (_millis(row.get("createdTime")), _text(row.get("orderId"))))

    async def order_history(
        self,
        category: str,
        start_ms: int,
        end_ms: int,
        *,
        symbol: str | None = None,
        account_id: str = "default",
        order_filters: tuple[str | None, ...] = ORDER_FILTERS,
    ) -> list[OrderRecord]:
        """Closed and open orders the exchange still lists for the window, oldest first.

        Retention limits (cancelled stops vanish after 24 h) are in the module docstring.
        """
        raw = await self.order_history_raw(
            category, start_ms, end_ms, symbol=symbol, order_filters=order_filters
        )
        return [normalize_order(row, account_id) for row in raw]

    async def open_orders_raw(
        self,
        category: str,
        *,
        symbol: str | None = None,
        settle_coin: str = "USDT",
        include_recent_closed: bool = False,
        order_filters: tuple[str | None, ...] = ORDER_FILTERS,
    ) -> list[dict]:
        """Raw rows of GET /v5/order/realtime, one per orderId (newest updatedTime wins).

        For linear "either symbol, baseCoin or settleCoin is required": ``symbol`` is sent when
        given, otherwise ``settle_coin``.  ``include_recent_closed`` sends openOnly=1, which
        adds the exchange's volatile list of up to 500 recently closed orders.
        """
        newest: dict[str, dict] = {}
        for order_filter in order_filters:
            params: dict[str, Any] = {"category": category, "limit": ORDER_PAGE_LIMIT}
            if symbol:
                params["symbol"] = symbol
            else:
                params["settleCoin"] = settle_coin
            if include_recent_closed:
                params["openOnly"] = 1
            if order_filter:
                params["orderFilter"] = order_filter
            for row in await self._page_rows("/v5/order/realtime", params):
                self._keep_newest(newest, row)
        return sorted(newest.values(),
                      key=lambda row: (_millis(row.get("createdTime")), _text(row.get("orderId"))))

    async def open_orders(
        self,
        category: str,
        *,
        symbol: str | None = None,
        settle_coin: str = "USDT",
        account_id: str = "default",
        include_recent_closed: bool = False,
        order_filters: tuple[str | None, ...] = ORDER_FILTERS,
    ) -> list[OrderRecord]:
        """Orders that are live right now: resting orders, untriggered conditional orders and
        position TP/SL (status "new" / "partially_filled" / "untriggered"), oldest first."""
        raw = await self.open_orders_raw(
            category, symbol=symbol, settle_coin=settle_coin,
            include_recent_closed=include_recent_closed, order_filters=order_filters,
        )
        return [normalize_order(row, account_id) for row in raw]

    async def transactions(self, category: str, start_ms: int, end_ms: int) -> list[Transaction]:
        raw = await self._pages(
            "/v5/account/transaction-log",
            {"accountType": "UNIFIED", "category": category, "startTime": start_ms,
             "endTime": end_ms, "limit": 50},
            "id",
        )
        rows = [
            Transaction(
                tx_id=str(item["id"]), category=str(item.get("category") or category),
                symbol=str(item.get("symbol", "")), timestamp_ms=int(item.get("transactionTime", 0)),
                tx_type=str(item.get("type", "")), cash_flow=decimal(item.get("cashFlow")),
                funding=decimal(item.get("funding")), fee=decimal(item.get("fee")),
                change=decimal(item.get("change")), currency=str(item.get("currency", "")),
            ) for item in raw
        ]
        return sorted([row for row in rows if start_ms <= row.timestamp_ms < end_ms],
                      key=lambda row: (row.timestamp_ms, row.tx_id))

    async def closed_pnl(self, category: str, start_ms: int, end_ms: int) -> list[ClosedPnl]:
        raw = await self._pages(
            "/v5/position/closed-pnl",
            {"category": category, "startTime": start_ms, "endTime": end_ms, "limit": 100},
            "orderId",
        )
        rows = [
            ClosedPnl(
                order_id=str(item["orderId"]), category=category,
                symbol=str(item.get("symbol", "")), side=str(item.get("side", "")),
                qty=decimal(item.get("qty")), closed_size=decimal(item.get("closedSize")),
                avg_entry_price=decimal(item.get("avgEntryPrice")),
                avg_exit_price=decimal(item.get("avgExitPrice")),
                closed_pnl=decimal(item.get("closedPnl")), leverage=decimal(item.get("leverage")),
                open_fee=decimal(item.get("openFee")), close_fee=decimal(item.get("closeFee")),
                updated_ms=int(item.get("updatedTime", 0)),
            ) for item in raw
        ]
        return sorted([row for row in rows if start_ms <= row.updated_ms < end_ms],
                      key=lambda row: (row.updated_ms, row.order_id))

    async def _position_rows(self, category: str) -> list[dict]:
        rows: list[dict] = []
        cursor = ""
        seen_cursors: set[str] = set()
        while True:
            params = {"category": category, "settleCoin": "USDT", "limit": 200}
            if cursor:
                params["cursor"] = cursor
            payload = await self._requester("/v5/position/list", params, True)
            page = payload.get("result") or {}
            rows.extend(page.get("list") or [])
            cursor = str(page.get("nextPageCursor") or "")
            if not cursor:
                break
            if cursor in seen_cursors:
                raise BybitError("position pagination cursor repeated")
            seen_cursors.add(cursor)
        return rows

    @staticmethod
    def _position_key(category: str, item: dict) -> tuple[str, str, int]:
        return (category, str(item.get("symbol", "")), int(item.get("positionIdx", 0) or 0))

    @staticmethod
    def _signed_size(item: dict) -> Decimal:
        size = decimal(item.get("size"))
        return -size if str(item.get("side", "")).lower() == "sell" else size

    async def positions(self, category: str) -> dict[tuple[str, str, int], Any]:
        return {
            self._position_key(category, item): self._signed_size(item)
            for item in await self._position_rows(category)
        }

    async def position_protection(self, category: str) -> dict[tuple[str, str, int], dict[str, Any]]:
        """The stop / target standing on each OPEN position right now ([POS]).

        ``stop_loss`` / ``take_profit`` are the current prices ("Stop loss price" / "Take profit
        price"), None when not set; ``trailing_stop`` is "the distance from market price".
        This is a snapshot: it says nothing about earlier values.  Empty positions are skipped.
        """
        result: dict[tuple[str, str, int], dict[str, Any]] = {}
        for item in await self._position_rows(category):
            size = self._signed_size(item)
            if not size:
                continue
            result[self._position_key(category, item)] = {
                "size": size,
                "avg_price": _optional_price(item.get("avgPrice")),
                "stop_loss": _optional_price(item.get("stopLoss")),
                "take_profit": _optional_price(item.get("takeProfit")),
                "trailing_stop": _optional_price(item.get("trailingStop")),
                "created_ms": _millis(item.get("createdTime")),
                "updated_ms": _millis(item.get("updatedTime")),
            }
        return result

    async def wallet_balance(self) -> dict[str, Any]:
        payload = await self._requester(
            "/v5/account/wallet-balance", {"accountType": "UNIFIED"}, True
        )
        rows = (payload.get("result") or {}).get("list") or []
        if not rows:
            raise BybitError("wallet balance returned no account")
        row = rows[0]
        return {
            "total_equity": decimal(row.get("totalEquity")),
            "wallet_balance": decimal(row.get("totalWalletBalance")),
            "unrealized_pnl": decimal(row.get("totalPerpUPL")),
        }

    async def klines(
        self, category: str, symbol: str, interval: str, start_ms: int, end_ms: int
    ) -> list[Kline]:
        # [ENUM interval] 1 3 5 15 30 60 120 240 360 720 are minutes (D/W/M are not paged here).
        interval_ms = {"1": 60_000, "3": 180_000, "5": 300_000, "15": 900_000,
                       "30": 1_800_000, "60": 3_600_000, "120": 7_200_000,
                       "240": 14_400_000, "360": 21_600_000,
                       "720": 43_200_000}.get(str(interval))
        if interval_ms is None:
            raise ValueError(f"unsupported paged kline interval: {interval}")
        cursor = start_ms // interval_ms * interval_ms
        aligned_end = ((end_ms + interval_ms - 1) // interval_ms) * interval_ms
        collected: dict[int, Kline] = {}
        while cursor < aligned_end:
            chunk_end = min(aligned_end, cursor + interval_ms * 1000)
            payload = await self._requester(
                "/v5/market/kline",
                {"category": category, "symbol": symbol, "interval": interval,
                 "start": cursor, "end": chunk_end - 1, "limit": 1000},
                False,
            )
            for item in ((payload.get("result") or {}).get("list") or []):
                row = Kline(int(item[0]), decimal(item[1]), decimal(item[2]), decimal(item[3]),
                            decimal(item[4]), decimal(item[5]), decimal(item[6]))
                collected[row.start_ms] = row
            cursor = chunk_end
        return sorted(
            (row for row in collected.values() if row.start_ms < end_ms),
            key=lambda row: row.start_ms,
        )
