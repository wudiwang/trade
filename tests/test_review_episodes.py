from decimal import Decimal

from app.review.episodes import closed_pnl_for_episode, reconstruct
from app.review.models import ClosedPnl, Execution


def fill(side, qty, *, exec_id, position_idx=0, timestamp_ms=1000):
    return Execution(
        exec_id=exec_id,
        order_id=f"order-{exec_id}",
        category="linear",
        symbol="BTCUSDT",
        side=side,
        qty=Decimal(str(qty)),
        price=Decimal("100"),
        value=Decimal(str(Decimal(str(qty)) * 100)),
        fee=Decimal("0.01"),
        timestamp_ms=timestamp_ms,
        position_idx=position_idx,
    )


def test_add_reduce_close_builds_one_episode():
    fills = [
        fill("Sell", 2, exec_id="1", timestamp_ms=1),
        fill("Sell", 1, exec_id="2", timestamp_ms=2),
        fill("Buy", 1, exec_id="3", timestamp_ms=3),
        fill("Buy", 2, exec_id="4", timestamp_ms=4),
    ]
    episodes = reconstruct(fills)
    assert len(episodes) == 1
    assert [item.action for item in episodes[0].fills] == ["entry", "add", "reduce", "close"]
    assert episodes[0].status == "closed"
    assert episodes[0].max_abs_qty == Decimal("3")


def test_flip_closes_then_opens_opposite_episode():
    episodes = reconstruct([
        fill("Sell", 1, exec_id="1", timestamp_ms=1),
        fill("Buy", 2, exec_id="2", timestamp_ms=2),
    ])
    assert [(row.direction, row.status) for row in episodes] == [
        ("short", "closed"),
        ("long", "open"),
    ]
    assert episodes[0].fills[-1].action == "close"
    assert episodes[1].fills[0].action == "entry"


def test_hedge_legs_are_never_merged():
    episodes = reconstruct([
        fill("Buy", 1, exec_id="long", position_idx=1, timestamp_ms=1),
        fill("Sell", 1, exec_id="short", position_idx=2, timestamp_ms=2),
    ])
    assert len(episodes) == 2
    assert {row.position_idx for row in episodes} == {1, 2}
    assert all(row.status == "open" for row in episodes)


def test_same_millisecond_order_is_stable_by_exec_id():
    episodes = reconstruct([
        fill("Buy", 1, exec_id="b", timestamp_ms=1),
        fill("Sell", 1, exec_id="a", timestamp_ms=1),
    ])
    assert episodes[0].fills[0].execution.exec_id == "a"


def test_closed_pnl_is_matched_by_closing_order_not_duplicated_by_symbol():
    episodes = reconstruct([
        fill("Sell", 1, exec_id="entry-1", timestamp_ms=1),
        fill("Buy", 1, exec_id="close-1", timestamp_ms=2),
        fill("Sell", 1, exec_id="entry-2", timestamp_ms=3),
        fill("Buy", 1, exec_id="close-2", timestamp_ms=4),
    ])
    rows = [
        ClosedPnl("order-close-1", "linear", "BTCUSDT", "Buy", Decimal("1"),
                  Decimal("1"), Decimal("100"), Decimal("90"), Decimal("10"),
                  Decimal("2"), Decimal("0"), Decimal("0"), 2),
        ClosedPnl("order-close-2", "linear", "BTCUSDT", "Buy", Decimal("1"),
                  Decimal("1"), Decimal("100"), Decimal("105"), Decimal("-5"),
                  Decimal("2"), Decimal("0"), Decimal("0"), 4),
    ]
    assert closed_pnl_for_episode(episodes[0], rows) == Decimal("10")
    assert closed_pnl_for_episode(episodes[1], rows) == Decimal("-5")
