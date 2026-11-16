import asyncio
import copy
import hashlib
import hmac
import inspect
import json
from decimal import Decimal
from pathlib import Path

import pytest

from app.review.bybit_client import (
    READ_PATHS, BybitError, BybitReadClient, ReadOnlyKeyRequired, assign_position_idx,
    encode_query, execution_origin, normalize_order, order_origin, protective_orders, sign_v5,
    trigger_price_changes,
)
from app.review.models import ORDER_ORIGINS, ORDER_STATUSES, OrderRecord


def test_v5_signature_uses_sorted_query():
    query = "category=linear&endTime=2&startTime=1"
    expected = hmac.new(b"secret", b"1000key5000" + query.encode(), hashlib.sha256).hexdigest()
    assert sign_v5("secret", "1000", "key", "5000", query) == expected


def test_signed_query_is_the_exact_sorted_query_sent_on_wire():
    params = {"startTime": 1, "category": "linear", "empty": "", "endTime": 2}
    assert encode_query(params) == "category=linear&endTime=2&startTime=1"


def test_cursor_pagination_deduplicates_exec_id():
    calls = []

    async def requester(path, params, private):
        calls.append(dict(params))
        if "cursor" not in params:
            return {"result": {"nextPageCursor": "next", "list": [execution("e1")]}}
        return {"result": {"nextPageCursor": "", "list": [execution("e1"), execution("e2")]}}

    client = BybitReadClient("key", "secret", requester=requester)
    rows = asyncio.run(client.executions("linear", 0, 2000))
    assert [row.exec_id for row in rows] == ["e1", "e2"]
    assert calls[1]["cursor"] == "next"


def test_string_false_is_not_treated_as_maker_fill():
    async def requester(path, params, private):
        row = execution("e1")
        row["isMaker"] = "false"
        return {"result": {"nextPageCursor": "", "list": [row]}}

    rows = asyncio.run(BybitReadClient("key", "secret", requester=requester)
                       .executions("linear", 0, 2000))
    assert rows[0].is_maker is False


def test_position_index_is_preserved_for_hedge_mode():
    async def requester(path, params, private):
        row = execution("e1")
        row["positionIdx"] = 2
        return {"result": {"nextPageCursor": "", "list": [row]}}

    rows = asyncio.run(BybitReadClient("key", "secret", requester=requester)
                       .executions("linear", 0, 2000))
    assert rows[0].position_idx == 2


def test_read_write_key_is_rejected():
    async def requester(path, params, private):
        return {"result": {"readOnly": 0, "id": "account"}}

    client = BybitReadClient("key", "secret", requester=requester)
    with pytest.raises(ReadOnlyKeyRequired):
        asyncio.run(client.verify_read_only())


def test_no_mutating_public_methods_exist():
    names = set(dir(BybitReadClient))
    assert not names.intersection({"place_order", "cancel_order", "transfer", "withdraw"})


def test_private_financial_endpoints_are_normalized():
    async def requester(path, params, private):
        if path.endswith("transaction-log"):
            return {"result": {"nextPageCursor": "", "list": [{
                "id": "tx", "category": "linear", "symbol": "BTCUSDT",
                "transactionTime": "10", "type": "TRADE", "cashFlow": "12",
                "funding": "-1", "fee": "2", "change": "9", "currency": "USDT"
            }]}}
        if path.endswith("closed-pnl"):
            return {"result": {"nextPageCursor": "", "list": [{
                "orderId": "o", "symbol": "BTCUSDT", "side": "Sell", "qty": "1",
                "closedSize": "1", "avgEntryPrice": "100", "avgExitPrice": "90",
                "closedPnl": "9", "leverage": "2", "openFee": "0.5", "closeFee": "0.5",
                "updatedTime": "20"
            }]}}
        if path.endswith("wallet-balance"):
            return {"result": {"list": [{"totalEquity": "1009", "totalWalletBalance": "1009",
                                            "totalPerpUPL": "0"}]}}
        raise AssertionError(path)

    client = BybitReadClient("key", "secret", requester=requester)
    txs = asyncio.run(client.transactions("linear", 0, 30))
    pnls = asyncio.run(client.closed_pnl("linear", 0, 30))
    wallet = asyncio.run(client.wallet_balance())
    assert txs[0].change == pnls[0].closed_pnl == 9
    assert wallet["total_equity"] == 1009


def test_private_rows_enforce_left_closed_right_open_window():
    async def requester(path, params, private):
        return {"result": {"nextPageCursor": "", "list": [
            {**execution("inside"), "execTime": "100"},
            {**execution("next-day"), "execTime": "200"},
        ]}}

    rows = asyncio.run(BybitReadClient("key", "secret", requester=requester)
                       .executions("linear", 100, 200))
    assert [row.exec_id for row in rows] == ["inside"]


def test_current_positions_are_signed_and_keep_hedge_legs():
    async def requester(path, params, private):
        return {"result": {"list": [
            {"symbol": "BTCUSDT", "side": "Buy", "size": "2", "positionIdx": 1},
            {"symbol": "BTCUSDT", "side": "Sell", "size": "3", "positionIdx": 2},
        ]}}

    positions = asyncio.run(BybitReadClient("key", "secret", requester=requester)
                            .positions("linear"))
    assert positions[("linear", "BTCUSDT", 1)] == 2
    assert positions[("linear", "BTCUSDT", 2)] == -3


def test_public_klines_are_sorted_oldest_first():
    async def requester(path, params, private):
        assert private is False
        return {"result": {"list": [
            ["200", "2", "3", "1", "2.5", "10", "20"],
            ["100", "1", "2", "0.5", "1.5", "9", "15"],
        ]}}

    client = BybitReadClient("", "", requester=requester)
    rows = asyncio.run(client.klines("linear", "BTCUSDT", "1", 1, 300))
    assert [row.start_ms for row in rows] == [100, 200]


def test_public_klines_page_ranges_longer_than_1000_bars():
    calls = []

    async def requester(path, params, private):
        calls.append(params)
        return {"result": {"list": []}}

    client = BybitReadClient("", "", requester=requester)
    asyncio.run(client.klines("linear", "BTCUSDT", "1", 0, 1_200 * 60_000))
    assert len(calls) == 2


def test_kline_pages_align_non_round_context_start_without_a_gap():
    calls = []

    async def requester(path, params, private):
        calls.append(params)
        return {"result": {"list": []}}

    client = BybitReadClient("", "", requester=requester)
    asyncio.run(client.klines("linear", "BTCUSDT", "1", 30_000, 1_001 * 60_000))
    assert calls[0]["start"] == 0
    assert calls[1]["start"] == calls[0]["end"] + 1


def execution(exec_id):
    return {
        "execId": exec_id,
        "orderId": f"o-{exec_id}",
        "symbol": "BTCUSDT",
        "side": "Sell",
        "execQty": "1",
        "execPrice": "100",
        "execValue": "100",
        "execFee": "0.05",
        "execTime": "1000",
        "leavesQty": "0",
        "isMaker": False,
    }


# ----------------------------------------------------------------------------------------------
# Order layer: order history / open orders / fill classification (fixtures: orders_*.json)
# ----------------------------------------------------------------------------------------------

FIXTURES = Path(__file__).parent / "fixtures" / "bybit_review"
ORDER_FIXTURES = sorted(path.stem for path in FIXTURES.glob("orders_*.json"))
MUTATING_PATHS = {
    "/v5/order/create", "/v5/order/amend", "/v5/order/cancel", "/v5/order/cancel-all",
    "/v5/order/create-batch", "/v5/order/amend-batch", "/v5/order/cancel-batch",
    "/v5/position/trading-stop", "/v5/position/set-leverage", "/v5/position/switch-mode",
    "/v5/position/set-auto-add-margin", "/v5/position/add-margin",
    "/v5/asset/transfer/inter-transfer", "/v5/asset/withdraw/create",
    "/v5/account/set-margin-mode",
}


def load_fixture(name):
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


def fixture_requester(fixture, calls=None):
    """Serve a fixture the way the exchange would: page by cursor, honour orderFilter."""
    async def requester(path, params, private):
        if calls is not None:
            calls.append((path, dict(params), private))
        pages = fixture["responses"][path]
        cursor = params.get("cursor", "")
        payload = copy.deepcopy(pages[int(cursor.split("-")[1]) - 1 if cursor else 0])
        if params.get("orderFilter") == "StopOrder":
            payload["result"]["list"] = [
                row for row in payload["result"]["list"]
                if row.get("stopOrderType") not in ("", "UNKNOWN")
            ]
        return payload
    return requester


def fetch_fixture(name, calls=None):
    fixture = load_fixture(name)
    client = BybitReadClient("key", "secret", requester=fixture_requester(fixture, calls))
    window = fixture["window"]
    history = asyncio.run(client.order_history("linear", window["start_ms"], window["end_ms"]))
    open_now = asyncio.run(client.open_orders("linear"))
    fills = asyncio.run(client.executions("linear", window["start_ms"], window["end_ms"]))
    return fixture, client, history, open_now, fills


def expected_value(value):
    if isinstance(value, str) and value.replace(".", "", 1).replace("-", "", 1).isdigit():
        return Decimal(value)
    return value


@pytest.mark.parametrize("name", ORDER_FIXTURES)
def test_order_fixtures_normalize_to_the_contract(name):
    fixture, _, history, open_now, fills = fetch_fixture(name)
    orders = {order.order_id: order for order in history + open_now}
    assert len(orders) == len(history) + len(open_now)
    for order in orders.values():
        assert isinstance(order, OrderRecord)
        assert order.exchange == "bybit" and order.account_id == "default"
        assert order.status in ORDER_STATUSES
        assert order.origin in ORDER_ORIGINS
        assert order.side in {"Buy", "Sell"}
        assert isinstance(order.qty, Decimal) and isinstance(order.filled_qty, Decimal)
        for price in (order.price, order.avg_fill_price, order.trigger_price,
                      order.attached_sl, order.attached_tp):
            assert price is None or (isinstance(price, Decimal) and price > 0)
        assert order.created_ms > 0 and order.updated_ms >= order.created_ms
    for order_id, fields in fixture["expect"]["orders"].items():
        for key, value in fields.items():
            assert getattr(orders[order_id], key) == expected_value(value), (order_id, key)

    fills, unresolved = assign_position_idx(fills, orders.values())
    assert unresolved == []
    by_exec = {fill.exec_id: fill for fill in fills}
    for exec_id, fields in fixture["expect"].get("executions", {}).items():
        for key, value in fields.items():
            assert getattr(by_exec[exec_id], key) == expected_value(value), (exec_id, key)
    for exec_id in fixture["expect"].get("dropped_exec_ids", []):
        assert exec_id not in by_exec


def test_all_scenarios_requested_by_the_review_have_a_fixture():
    assert set(ORDER_FIXTURES) >= {
        "orders_attached_tpsl_stop_hit", "orders_hedge_mode_both_sides",
        "orders_liquidation_and_adl", "orders_no_stop_manual_close", "orders_pagination",
        "orders_partial_manual_then_stop", "orders_stop_added_late",
        "orders_target_reduce_only_limit",
    }


def test_stop_hit_shows_when_the_stop_was_set_and_that_it_closed_the_trade():
    _, _, history, _, fills = fetch_fixture("orders_attached_tpsl_stop_hit")
    orders = {order.order_id: order for order in history}
    entry, stop = orders["a-entry"], orders["a-sl"]
    closing = [fill for fill in fills if fill.closed_size > 0]
    assert [fill.origin for fill in closing] == ["stop_loss"]
    assert closing[0].order_id == stop.order_id
    assert stop.trigger_price == entry.attached_sl == Decimal("95900.00")
    assert stop.created_ms - entry.created_ms < 1000          # set together with the entry
    assert [order.order_id for order in protective_orders(history, "BTCUSDT")] == ["a-sl", "a-tp"]


def test_trade_without_any_stop_has_no_protective_order_and_a_manual_close():
    fixture, _, history, open_now, fills = fetch_fixture("orders_no_stop_manual_close")
    assert protective_orders(history + open_now, "ETHUSDT") == []
    assert all(order.attached_sl is None and order.attached_tp is None for order in history)
    closing = [fill for fill in fills if fill.closed_size > 0]
    assert [(fill.origin, fill.order_type, fill.create_type) for fill in closing] == [
        ("user", "Market", "CreateByClosing")]
    assert fixture["expect"]["protective_order_ids"] == []


def test_late_stop_exposes_the_delay_after_the_entry_fill():
    fixture, client, history, open_now, fills = fetch_fixture("orders_stop_added_late")
    entry_fill = next(fill for fill in fills if fill.order_id == "c-entry")
    stop = protective_orders(history + open_now, "SOLUSDT")[0]
    assert stop.status == "untriggered" and stop.origin == "stop_loss"
    assert stop.created_ms - entry_fill.timestamp_ms == fixture["expect"]["stop_delay_ms"] == 600_000
    protection = asyncio.run(client.position_protection("linear"))
    position = protection[("linear", "SOLUSDT", 0)]
    assert position["size"] == Decimal("-12.0")
    assert position["stop_loss"] == Decimal("243.00")
    assert position["take_profit"] is None and position["trailing_stop"] is None
    assert asyncio.run(client.positions("linear")) == {("linear", "SOLUSDT", 0): Decimal("-12.0")}


def test_target_hit_through_a_resting_reduce_only_limit_is_a_user_order_not_a_tp_order():
    _, _, history, _, fills = fetch_fixture("orders_target_reduce_only_limit")
    orders = {order.order_id: order for order in history}
    target = orders["d-target"]
    assert (target.origin, target.order_type, target.reduce_only, target.trigger_price) == (
        "user", "Limit", True, None)
    assert target.updated_ms - target.created_ms > 4 * 3_600_000   # rested for hours before it filled
    assert [order.order_id for order in protective_orders(history, "BTCUSDT")] == ["d-sl"]
    assert orders["d-sl"].status == "cancelled" and orders["d-sl"].filled_qty == 0
    assert [fill.origin for fill in fills if fill.closed_size > 0] == ["user"]


def test_partial_manual_close_then_stop_on_the_rest_keeps_every_exit_leg():
    _, _, history, _, fills = fetch_fixture("orders_partial_manual_then_stop")
    exits = [(fill.origin, fill.closed_size) for fill in fills if fill.closed_size > 0]
    assert exits == [("user", Decimal("1000")), ("stop_loss", Decimal("1200")),
                     ("stop_loss", Decimal("800"))]
    stop = next(order for order in history if order.order_id == "e-sl")
    assert stop.qty == stop.filled_qty == Decimal("2000")
    assert stop.updated_ms > stop.created_ms


def test_liquidation_and_adl_are_classified_and_funding_rows_are_not_fills():
    fixture, client, history, _, fills = fetch_fixture("orders_liquidation_and_adl")
    assert {fill.exec_id: fill.origin for fill in fills if fill.closed_size > 0} == {
        "f-liq-1": "liquidation", "f2-adl-1": "adl"}
    assert all(fill.exec_type != "Funding" for fill in fills)
    window = fixture["window"]
    everything = asyncio.run(client.executions(
        "linear", window["start_ms"], window["end_ms"], include_non_fills=True))
    funding = [row for row in everything if row.exec_type == "Funding"]
    assert [row.exec_id for row in funding] == ["f-funding-1"]
    assert funding[0].origin == "unknown"


def test_hedge_mode_legs_are_separated_by_joining_fills_with_their_orders():
    fixture, client, history, open_now, fills = fetch_fixture("orders_hedge_mode_both_sides")
    assert {fill.position_idx for fill in fills} == {0}      # the execution list has no positionIdx
    orders = history + open_now
    joined, unresolved = assign_position_idx(fills, orders)
    assert unresolved == []
    assert {fill.exec_id: fill.position_idx for fill in joined} == {
        "g-long-entry-1": 1, "g-short-entry-1": 2, "g-short-ptp-1": 2}
    expected = fixture["expect"]["protective_order_ids_by_position"]
    for idx in (1, 2):
        assert [order.order_id for order in protective_orders(orders, "BTCUSDT", idx)] == \
            expected[str(idx)]
    positions = asyncio.run(client.position_protection("linear"))
    assert positions[("linear", "BTCUSDT", 1)]["stop_loss"] == Decimal("94300.00")
    assert positions[("linear", "BTCUSDT", 2)]["stop_loss"] is None
    assert positions[("linear", "BTCUSDT", 2)]["size"] == Decimal("-0.005")


def test_fills_without_a_known_order_are_reported_as_unresolved():
    _, _, history, _, fills = fetch_fixture("orders_hedge_mode_both_sides")
    joined, unresolved = assign_position_idx(fills, [o for o in history if o.position_idx == 1])
    assert unresolved == ["g-short-entry-1", "g-short-ptp-1"]
    assert [fill.position_idx for fill in joined] == [1, 0, 0]


def test_order_history_follows_cursors_and_keeps_the_newest_row_per_order():
    calls = []
    fixture, _, history, open_now, _ = fetch_fixture("orders_pagination", calls)
    history_calls = [params for path, params, _ in calls if path == "/v5/order/history"]
    assert [params.get("cursor", "") for params in history_calls] == [
        "", "page-2", "page-3", "", "page-2", "page-3"]
    assert [params.get("orderFilter") for params in history_calls] == [
        None, None, None, "StopOrder", "StopOrder", "StopOrder"]
    assert [order.order_id for order in history] == fixture["expect"]["history_order_ids"]
    hand_stop = next(order for order in history if order.order_id == "h-hand-stop")
    assert hand_stop.status == "filled"                      # not the stale "untriggered" copy
    assert [order.order_id for order in protective_orders(history + open_now, "SUIUSDT")] == \
        fixture["expect"]["protective_order_ids"]
    assert [order.order_id for order in open_now] == ["h-resting"]


def test_order_history_splits_long_ranges_into_seven_day_requests():
    calls = []

    async def requester(path, params, private):
        calls.append((path, dict(params), private))
        return {"result": {"list": [], "nextPageCursor": ""}}

    client = BybitReadClient("key", "secret", requester=requester)
    day = 86_400_000
    assert asyncio.run(client.order_history("linear", 0, 15 * day, symbol="BTCUSDT")) == []
    assert len(calls) == 6                                   # 3 windows x (all kinds + StopOrder)
    for path, params, private in calls:
        assert path == "/v5/order/history" and private is True
        assert params["category"] == "linear" and params["symbol"] == "BTCUSDT"
        assert params["limit"] == 50
        assert 0 <= params["endTime"] - params["startTime"] <= 7 * day
    starts = [params["startTime"] for _, params, _ in calls[:3]]
    ends = [params["endTime"] for _, params, _ in calls[:3]]
    assert starts == [0, 7 * day, 14 * day]
    assert ends == [7 * day - 1, 14 * day - 1, 15 * day - 1]


def test_open_orders_query_all_kinds_and_conditional_orders():
    calls = []

    async def requester(path, params, private):
        calls.append((path, dict(params), private))
        return {"result": {"list": [], "nextPageCursor": ""}}

    client = BybitReadClient("key", "secret", requester=requester)
    asyncio.run(client.open_orders("linear"))
    asyncio.run(client.open_orders("linear", symbol="ETHUSDT", include_recent_closed=True))
    assert [(path, private) for path, _, private in calls] == [("/v5/order/realtime", True)] * 4
    assert calls[0][1] == {"category": "linear", "limit": 50, "settleCoin": "USDT"}
    assert calls[1][1] == {"category": "linear", "limit": 50, "settleCoin": "USDT",
                           "orderFilter": "StopOrder"}
    assert calls[2][1] == {"category": "linear", "limit": 50, "symbol": "ETHUSDT", "openOnly": 1}
    assert calls[3][1]["orderFilter"] == "StopOrder" and "settleCoin" not in calls[3][1]


def test_repeated_pagination_cursor_is_an_error_not_an_endless_loop():
    async def requester(path, params, private):
        return {"result": {"list": [], "nextPageCursor": "same"}}

    client = BybitReadClient("key", "secret", requester=requester)
    with pytest.raises(BybitError):
        asyncio.run(client.open_orders("linear"))


@pytest.mark.parametrize("raw,expected", [
    ("New", "new"), ("PartiallyFilled", "partially_filled"), ("Untriggered", "untriggered"),
    ("Triggered", "new"), ("Filled", "filled"), ("Cancelled", "cancelled"),
    ("PartiallyFilledCanceled", "cancelled"), ("Deactivated", "cancelled"),
    ("Rejected", "rejected"), ("SomethingNew", "unknown"), ("", "unknown"),
])
def test_order_status_mapping(raw, expected):
    assert normalize_order({**order_row(), "orderStatus": raw}).status == expected


@pytest.mark.parametrize("create,stop,expected", [
    ("CreateByUser", "UNKNOWN", "user"),
    ("CreateByUser", "", "user"),
    ("CreateByClosing", "", "user"),
    ("CreateByStopOrder", "Stop", "user"),
    ("CreateByBboOrder", "", "user"),
    ("CreateByChaseOrder", "", "user"),
    ("CreateByStopLoss", "StopLoss", "stop_loss"),
    ("CreateByPartialStopLoss", "PartialStopLoss", "stop_loss"),
    ("CreateByTakeProfit", "TakeProfit", "take_profit"),
    ("CreateByPartialTakeProfit", "PartialTakeProfit", "take_profit"),
    ("CreateByTrailingStop", "TrailingStop", "trailing_stop"),
    ("CreateByTrailingProfit", "", "trailing_stop"),
    ("CreateByLiq", "", "liquidation"),
    ("CreateByTakeOver_PassThrough", "StopLoss", "liquidation"),
    ("CreateByAdl_PassThrough", "", "adl"),
    ("CreateByAdminClosing", "", "system"),
    ("CreateBySettle", "", "system"),
    ("CreateByMmRateClose", "MmRateClose", "system"),
    ("CreateByUser", "StopLoss", "stop_loss"),          # who placed it does not change what it is
    ("CreateByStopOrder", "TakeProfit", "take_profit"),
    ("CreateByStopLoss", "Stop", "stop_loss"),
    ("", "StopLoss", "stop_loss"),                      # createType missing, stopOrderType decides
    (None, "TrailingStop", "trailing_stop"),
    ("CreateByStopLoss", "TakeProfit", "unknown"),      # the two fields contradict each other
    ("CreateByAdminClosing", "StopLoss", "unknown"),
    ("CreateByFGridBot", "", "unknown"),
    ("CreateByTWAP", "UNKNOWN", "unknown"),
    ("CreateBySomethingNew", "", "unknown"),
    ("", "", "unknown"),
    (None, None, "unknown"),
    ("CreateByUser", "tpslOrder", "user"),              # spot-only kind: no verdict from it
])
def test_order_origin_mapping(create, stop, expected):
    assert order_origin(create, stop) == expected


@pytest.mark.parametrize("exec_type,create,stop,expected", [
    ("Trade", "CreateByUser", "UNKNOWN", "user"),
    ("Trade", "CreateByStopLoss", "StopLoss", "stop_loss"),
    ("Trade", "CreateByLiq", "", "liquidation"),
    ("", "CreateByTakeProfit", "TakeProfit", "take_profit"),
    ("AdlTrade", "CreateByUser", "", "adl"),
    ("BustTrade", "", "", "liquidation"),
    ("Delivery", "", "", "system"),
    ("Settle", "", "", "system"),
    ("Funding", "", "", "unknown"),
    ("BlockTrade", "CreateByUser", "", "unknown"),
    ("MovePosition", "CreateByUser", "", "unknown"),
    ("UNKNOWN", "CreateByUser", "", "unknown"),
    ("Trade", "", "", "unknown"),
])
def test_execution_origin_mapping(exec_type, create, stop, expected):
    assert execution_origin(exec_type, create, stop) == expected


def test_unset_prices_are_none_never_zero():
    for blank in ("", "0", "0.00", None):
        record = normalize_order({**order_row(), "price": blank, "avgPrice": blank,
                                  "triggerPrice": blank, "stopLoss": blank, "takeProfit": blank})
        assert (record.price, record.avg_fill_price, record.trigger_price,
                record.attached_sl, record.attached_tp) == (None,) * 5


def test_close_position_needs_a_tpsl_order_in_full_mode():
    def record(**fields):
        return normalize_order({**order_row(), **fields})

    full_stop = record(createType="CreateByStopLoss", stopOrderType="StopLoss", tpslMode="Full",
                       reduceOnly=True, closeOnTrigger=True)
    partial_stop = record(createType="CreateByPartialStopLoss", stopOrderType="PartialStopLoss",
                          tpslMode="Partial", reduceOnly=True, closeOnTrigger=True)
    entry_with_tpsl = record(tpslMode="Full", stopLoss="90", takeProfit="120")
    hand_made = record(createType="CreateByStopOrder", stopOrderType="Stop",
                       closeOnTrigger=True, triggerPrice="95")
    assert (full_stop.close_position, full_stop.reduce_only) == (True, True)
    assert (partial_stop.close_position, partial_stop.reduce_only) == (False, True)
    assert (entry_with_tpsl.close_position, entry_with_tpsl.reduce_only) == (False, False)
    assert (hand_made.close_position, hand_made.reduce_only) == (False, True)


def test_order_row_without_id_is_rejected():
    with pytest.raises(BybitError):
        normalize_order({**order_row(), "orderId": ""})


def test_moved_stop_is_only_provable_from_repeated_observations():
    def seen(order_id, updated_ms, trigger, status="Untriggered"):
        return normalize_order({
            **order_row(), "orderId": order_id, "createType": "CreateByStopLoss",
            "stopOrderType": "StopLoss", "tpslMode": "Full", "orderStatus": status,
            "triggerPrice": trigger, "createdTime": "1000", "updatedTime": str(updated_ms),
        }, "acct")

    first, second = seen("sl", 1000, "100"), seen("sl", 5000, "103")
    third = seen("sl", 9000, "103", "Filled")
    assert trigger_price_changes([first]) == []                    # one look proves nothing
    assert trigger_price_changes([third]) == []                    # history alone: latest value only
    assert trigger_price_changes([third, first, second, seen("other", 2000, "50")]) == [
        ("sl", 5000, Decimal("100"), Decimal("103"))]


def test_every_fetcher_stays_on_the_read_only_allow_list():
    calls = []

    async def requester(path, params, private):
        calls.append(path)
        if path == "/v5/user/query-api":
            return {"result": {"readOnly": 1, "id": "k"}}
        if path == "/v5/account/wallet-balance":
            return {"result": {"list": [{"totalEquity": "1"}]}}
        return {"result": {"list": [], "nextPageCursor": ""}}

    client = BybitReadClient("key", "secret", requester=requester)

    async def run_all():
        await client.verify_read_only()
        await client.executions("linear", 0, 10)
        await client.order_history("linear", 0, 10)
        await client.open_orders("linear")
        await client.transactions("linear", 0, 10)
        await client.closed_pnl("linear", 0, 10)
        await client.positions("linear")
        await client.position_protection("linear")
        await client.wallet_balance()
        await client.klines("linear", "BTCUSDT", "240", 0, 14_400_000)

    asyncio.run(run_all())
    assert set(calls) == READ_PATHS
    assert not READ_PATHS & MUTATING_PATHS


def test_paths_outside_the_allow_list_never_reach_the_transport():
    sent = []

    async def requester(path, params, private):
        sent.append(path)
        return {"result": {}}

    client = BybitReadClient("key", "secret", requester=requester)
    for path in sorted(MUTATING_PATHS):
        with pytest.raises(BybitError):
            asyncio.run(client._requester(path, {}, True))
    assert sent == []


def test_transport_can_only_send_http_get():
    import app.review.bybit_client as module

    source = inspect.getsource(module)
    code = source.split('"""', 2)[2]                          # skip the module docstring
    for verb in (".post(", ".put(", ".patch(", ".delete(", ".request("):
        assert verb not in code
    assert code.count("session.get(") == 1


def test_public_methods_are_all_queries():
    public = {name for name in dir(BybitReadClient) if not name.startswith("_")}
    assert public == {
        "verify_read_only", "executions", "order_history", "order_history_raw", "open_orders",
        "open_orders_raw", "transactions", "closed_pnl", "positions", "position_protection",
        "wallet_balance", "klines",
    }


def order_row():
    return {
        "orderId": "o1", "symbol": "BTCUSDT", "side": "Buy", "positionIdx": 0,
        "orderType": "Market", "qty": "1", "cumExecQty": "0", "price": "0", "avgPrice": "",
        "triggerPrice": "", "stopLoss": "", "takeProfit": "", "tpslMode": "",
        "reduceOnly": False, "closeOnTrigger": False, "orderStatus": "New",
        "createType": "CreateByUser", "stopOrderType": "", "createdTime": "1000",
        "updatedTime": "1000",
    }
