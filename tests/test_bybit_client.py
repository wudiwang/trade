import asyncio
import hashlib
import hmac

import pytest

from app.review.bybit_client import BybitReadClient, ReadOnlyKeyRequired, encode_query, sign_v5


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
