import asyncio
from dataclasses import replace
from decimal import Decimal

from app.review.models import ClosedPnl, Execution, Transaction
from app.review.narrator import Narrative
from scripts import bybit_daily_review as cli


def test_live_report_uses_current_position_to_reconstruct_old_position_with_in_window_add(monkeypatch):
    executions = [
        Execution("add", "order-add", "linear", "BTCUSDT", "Buy", Decimal("1"),
                  Decimal("100"), Decimal("100"), Decimal("0.1"), 1100),
        Execution("close", "order-close", "linear", "BTCUSDT", "Sell", Decimal("3"),
                  Decimal("105"), Decimal("315"), Decimal("0.1"), 1200),
    ]
    transactions = [
        Transaction("tx-add", "linear", "BTCUSDT", 1100, "TRADE", Decimal("0"),
                    Decimal("0"), Decimal("0.1"), Decimal("-0.1"), "USDT"),
        Transaction("tx-close", "linear", "BTCUSDT", 1200, "TRADE", Decimal("10"),
                    Decimal("0"), Decimal("0.1"), Decimal("9.9"), "USDT"),
    ]
    closed = [
        ClosedPnl("order-close", "linear", "BTCUSDT", "Sell", Decimal("3"),
                  Decimal("3"), Decimal("100"), Decimal("105"), Decimal("9.8"),
                  Decimal("2"), Decimal("0.1"), Decimal("0.1"), 1200),
    ]

    class Client:
        def __init__(self, *args, **kwargs): pass
        async def verify_read_only(self): return {"read_only": True}
        async def executions(self, *args): return executions
        async def transactions(self, *args): return transactions
        async def closed_pnl(self, *args): return closed
        async def wallet_balance(self): return {"total_equity": Decimal("1000")}
        async def positions(self, category): return {}
        async def klines(self, *args): return []

    monkeypatch.setenv("BYBIT_API_KEY", "read-only-test-key")
    monkeypatch.setenv("BYBIT_API_SECRET", "test-secret")
    monkeypatch.setattr(cli, "BybitReadClient", Client)
    monkeypatch.setattr(cli, "generate_narrative", lambda review: Narrative(
        "测试", ("高波动候选：测试",), "测试", "low", "test"))
    report = asyncio.run(cli.build_live_report("1970-01-01", 1000, 2000))

    assert report["status"] == "mismatch"
    assert report["episodes"][0]["inherited_position"] is True
    assert report["episodes"][0]["direction"] == "long"
    assert report["episodes"][0]["verdict"] == "入场不在窗口"
    assert report["missing_closed_pnl_orders"] == []


def test_complete_intraday_roundtrip_reconciles_amounts_across_three_sources(monkeypatch):
    executions = [
        Execution("entry", "order-entry", "linear", "BTCUSDT", "Sell", Decimal("1"),
                  Decimal("100"), Decimal("100"), Decimal("0.1"), 1100),
        Execution("close", "order-close", "linear", "BTCUSDT", "Buy", Decimal("1"),
                  Decimal("90"), Decimal("90"), Decimal("0.1"), 1200),
    ]
    transactions = [
        Transaction("tx-entry", "linear", "BTCUSDT", 1100, "TRADE", Decimal("0"),
                    Decimal("0"), Decimal("0.1"), Decimal("-0.1"), "USDT"),
        Transaction("tx-close", "linear", "BTCUSDT", 1200, "TRADE", Decimal("10"),
                    Decimal("0"), Decimal("0.1"), Decimal("9.9"), "USDT"),
    ]
    closed = [
        ClosedPnl("order-close", "linear", "BTCUSDT", "Buy", Decimal("1"),
                  Decimal("1"), Decimal("100"), Decimal("90"), Decimal("9.8"),
                  Decimal("2"), Decimal("0.1"), Decimal("0.1"), 1200),
    ]

    class Client:
        def __init__(self, *args, **kwargs): pass
        async def verify_read_only(self): return {"read_only": True}
        async def executions(self, *args): return executions
        async def transactions(self, *args): return transactions
        async def closed_pnl(self, *args): return closed
        async def wallet_balance(self): return {"total_equity": Decimal("1000")}
        async def positions(self, category): return {}
        async def klines(self, *args): return []

    monkeypatch.setenv("BYBIT_API_KEY", "read-only-test-key")
    monkeypatch.setenv("BYBIT_API_SECRET", "test-secret")
    monkeypatch.setattr(cli, "BybitReadClient", Client)
    monkeypatch.setattr(cli, "generate_narrative", lambda review: Narrative(
        "测试", ("高波动候选：测试",), "测试", "low", "test"))
    report = asyncio.run(cli.build_live_report("1970-01-01", 1000, 2000))
    assert report["status"] == "reconciled"
    assert report["closed_pnl_difference"] == 0
    assert report["ledger_closed_difference"] == 0

    closed[:] = [replace(closed[0], closed_pnl=Decimal("99.8"))]
    bad = asyncio.run(cli.build_live_report("1970-01-01", 1000, 2000))
    assert bad["status"] == "mismatch"
    assert bad["closed_pnl_difference"] == 90
