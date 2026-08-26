import asyncio
import hashlib
import hmac

import pytest

from app.review.bybit_client import BybitReadClient, ReadOnlyKeyRequired, sign_v5


def test_v5_signature_uses_sorted_query():
    query = "category=linear&endTime=2&startTime=1"
    expected = hmac.new(b"secret", b"1000key5000" + query.encode(), hashlib.sha256).hexdigest()
    assert sign_v5("secret", "1000", "key", "5000", query) == expected


def test_cursor_pagination_deduplicates_exec_id():
    calls = []

    async def requester(path, params, private):
        calls.append(dict(params))
        if "cursor" not in params:
            return {"result": {"nextPageCursor": "next", "list": [execution("e1")]}}
        return {"result": {"nextPageCursor": "", "list": [execution("e1"), execution("e2")]}}

    client = BybitReadClient("key", "secret", requester=requester)
    rows = asyncio.run(client.executions("linear", 1, 2))
    assert [row.exec_id for row in rows] == ["e1", "e2"]
    assert calls[1]["cursor"] == "next"


def test_string_false_is_not_treated_as_maker_fill():
    async def requester(path, params, private):
        row = execution("e1")
        row["isMaker"] = "false"
        return {"result": {"nextPageCursor": "", "list": [row]}}

    rows = asyncio.run(BybitReadClient("key", "secret", requester=requester)
                       .executions("linear", 1, 2))
    assert rows[0].is_maker is False


def test_position_index_is_preserved_for_hedge_mode():
    async def requester(path, params, private):
        row = execution("e1")
        row["positionIdx"] = 2
        return {"result": {"nextPageCursor": "", "list": [row]}}

    rows = asyncio.run(BybitReadClient("key", "secret", requester=requester)
                       .executions("linear", 1, 2))
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
    txs = asyncio.run(client.transactions("linear", 1, 2))
    pnls = asyncio.run(client.closed_pnl("linear", 1, 2))
    wallet = asyncio.run(client.wallet_balance())
    assert txs[0].change == pnls[0].closed_pnl == 9
    assert wallet["total_equity"] == 1009


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
