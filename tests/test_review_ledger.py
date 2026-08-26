from decimal import Decimal

from app.review.models import ClosedPnl, Execution, Transaction, beijing_day, summarize_ledger
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


def test_store_persists_raw_rows_and_daily_review_idempotently(tmp_path):
    store = ReviewStore(tmp_path / "review.db")
    execution = Execution(
        exec_id="exec-1", order_id="order-1", category="linear", symbol="BTCUSDT",
        side="Sell", qty=Decimal("1"), price=Decimal("100"), value=Decimal("100"),
        fee=Decimal("0.1"), timestamp_ms=1787763600000,
    )
    closed = ClosedPnl(
        order_id="close-1", category="linear", symbol="BTCUSDT", side="Sell",
        qty=Decimal("1"), closed_size=Decimal("1"), avg_entry_price=Decimal("100"),
        avg_exit_price=Decimal("90"), closed_pnl=Decimal("9.8"), leverage=Decimal("2"),
        open_fee=Decimal("0.1"), close_fee=Decimal("0.1"), updated_ms=1787767200000,
    )
    report = {"date": "2026-08-27", "window_start_ms": 1, "window_end_ms": 2,
              "net_pnl": 9.8}

    for _ in range(2):
        store.upsert_executions([execution])
        store.upsert_closed_pnl([closed])
        store.replace_review("2026-08-27", report)

    assert store.count("bybit_executions") == 1
    assert store.count("bybit_closed_pnl") == 1
    assert store.count("daily_trade_reviews") == 1
