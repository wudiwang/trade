from decimal import Decimal

from app.review.models import Transaction, beijing_day, summarize_ledger
from app.review.store import ReviewStore


def transaction(**overrides):
    values = {
        "tx_id": "tx-1",
        "category": "linear",
        "symbol": "BTCUSDT",
        "timestamp_ms": 1787763600000,
        "tx_type": "TRADE",
        "cash_flow": Decimal("12"),
        "funding": Decimal("-1"),
        "fee": Decimal("2"),
        "change": Decimal("9"),
        "currency": "USDT",
    }
    values.update(overrides)
    return Transaction(**values)


def test_beijing_day_is_left_closed_right_open():
    start, end = beijing_day("2026-08-27")
    assert start == 1787760000000
    assert end - start == 86_400_000


def test_transaction_change_is_financial_truth():
    result = summarize_ledger([transaction()])
    assert result.closed_cash_flow == Decimal("12")
    assert result.funding == Decimal("-1")
    assert result.fees == Decimal("2")
    assert result.net_change == Decimal("9")


def test_transfers_are_separate_from_trading_performance():
    result = summarize_ledger([
        transaction(),
        transaction(tx_id="transfer", tx_type="TRANSFER_IN", cash_flow=Decimal("100"),
                    funding=Decimal("0"), fee=Decimal("0"), change=Decimal("100")),
    ])
    assert result.net_change == Decimal("9")
    assert result.external_change == Decimal("100")


def test_store_upsert_is_idempotent(tmp_path):
    store = ReviewStore(tmp_path / "review.db")
    store.upsert_transactions([transaction()])
    store.upsert_transactions([transaction()])
    assert store.count("bybit_transactions") == 1
