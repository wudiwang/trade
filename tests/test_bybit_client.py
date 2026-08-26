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


def test_read_write_key_is_rejected():
    async def requester(path, params, private):
        return {"result": {"readOnly": 0, "id": "account"}}

    client = BybitReadClient("key", "secret", requester=requester)
    with pytest.raises(ReadOnlyKeyRequired):
        asyncio.run(client.verify_read_only())


def test_no_mutating_public_methods_exist():
    names = set(dir(BybitReadClient))
    assert not names.intersection({"place_order", "cancel_order", "transfer", "withdraw"})


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
