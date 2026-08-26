from decimal import Decimal

from app.review.episodes import (closed_pnl_for_episode, derive_initial_positions,
                                 episode_closed_pnl_difference, episode_net_from_executions,
                                 episode_price_return_pct, missing_closed_pnl_orders,
                                 reconstruct, unmatched_closed_orders)
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


def test_old_position_close_is_flagged_instead_of_guessed_as_new_entry():
    episodes = reconstruct([fill("Sell", 1, exec_id="old-close", timestamp_ms=10)])
    closed = [
        ClosedPnl("order-old-close", "linear", "BTCUSDT", "Sell", Decimal("1"),
                  Decimal("1"), Decimal("100"), Decimal("90"), Decimal("10"),
                  Decimal("2"), Decimal("0"), Decimal("0"), 10),
    ]
    assert unmatched_closed_orders(episodes, closed) == {"order-old-close"}


def test_current_position_and_later_fills_derive_reliable_window_open_position():
    after_start = [fill("Buy", 1, exec_id="add", timestamp_ms=2),
                   fill("Sell", 3, exec_id="close", timestamp_ms=3)]
    current = {("linear", "BTCUSDT", 0): Decimal("0")}
    initial = derive_initial_positions(current, after_start)
    assert initial[("linear", "BTCUSDT", 0)] == Decimal("2")
    episode = reconstruct(after_start, initial, window_start_ms=1)[0]
    assert episode.inherited_position is True
    assert episode.direction == "long"
    assert [row.action for row in episode.fills] == ["add", "close"]


def test_missing_closed_pnl_is_detected_in_reverse_direction():
    episodes = reconstruct([fill("Sell", 1, exec_id="entry", timestamp_ms=1),
                            fill("Buy", 1, exec_id="close", timestamp_ms=2)])
    assert missing_closed_pnl_orders(episodes, []) == {"order-close"}


def test_price_return_uses_quantity_weighted_entry_and_exit_prices():
    episode = reconstruct([
        fill("Sell", 1, exec_id="entry-1", timestamp_ms=1),
        fill("Sell", 3, exec_id="entry-2", timestamp_ms=2),
        fill("Buy", 2, exec_id="exit-1", timestamp_ms=3),
        fill("Buy", 2, exec_id="exit-2", timestamp_ms=4),
    ])[0]
    episode.fills[0].execution = episode.fills[0].execution.__class__(
        **{**episode.fills[0].execution.__dict__, "price": Decimal("100")})
    episode.fills[1].execution = episode.fills[1].execution.__class__(
        **{**episode.fills[1].execution.__dict__, "price": Decimal("110")})
    episode.fills[2].execution = episode.fills[2].execution.__class__(
        **{**episode.fills[2].execution.__dict__, "price": Decimal("100")})
    episode.fills[3].execution = episode.fills[3].execution.__class__(
        **{**episode.fills[3].execution.__dict__, "price": Decimal("90")})
    assert episode_price_return_pct(episode) == (Decimal("107.5") - Decimal("95")) / Decimal("107.5") * 100


def test_execution_pnl_independently_recomputes_complete_episode_net():
    episode = reconstruct([
        fill("Sell", 2, exec_id="entry", timestamp_ms=1),
        fill("Buy", 2, exec_id="close", timestamp_ms=2),
    ])[0]
    episode.fills[0].execution = episode.fills[0].execution.__class__(
        **{**episode.fills[0].execution.__dict__, "price": Decimal("100"),
           "fee": Decimal("0.2")})
    episode.fills[1].execution = episode.fills[1].execution.__class__(
        **{**episode.fills[1].execution.__dict__, "price": Decimal("90"),
           "fee": Decimal("0.2")})
    assert episode_net_from_executions(episode) == Decimal("19.6")


def test_per_episode_pnl_errors_cannot_cancel_at_daily_total():
    episodes = reconstruct([
        fill("Sell", 1, exec_id="entry-1", timestamp_ms=1),
        fill("Buy", 1, exec_id="close-1", timestamp_ms=2),
        fill("Sell", 1, exec_id="entry-2", timestamp_ms=3),
        fill("Buy", 1, exec_id="close-2", timestamp_ms=4),
    ])
    # Both independently compute -0.02 because helper prices are equal and each roundtrip has 0.02 fee.
    rows = [
        ClosedPnl("order-close-1", "linear", "BTCUSDT", "Buy", Decimal("1"),
                  Decimal("1"), Decimal("100"), Decimal("100"), Decimal("4.98"),
                  Decimal("2"), Decimal("0.01"), Decimal("0.01"), 2),
        ClosedPnl("order-close-2", "linear", "BTCUSDT", "Buy", Decimal("1"),
                  Decimal("1"), Decimal("100"), Decimal("100"), Decimal("-5.02"),
                  Decimal("2"), Decimal("0.01"), Decimal("0.01"), 4),
    ]
    assert sum((row.closed_pnl for row in rows), Decimal("0")) == Decimal("-0.04")
    assert sum((episode_net_from_executions(row) for row in episodes), Decimal("0")) == Decimal("-0.04")
    assert episode_closed_pnl_difference(episodes, rows) == Decimal("10")


def test_closed_pnl_identity_mismatch_cannot_pass_amount_reconciliation():
    episode = reconstruct([fill("Sell", 1, exec_id="entry", timestamp_ms=1),
                           fill("Buy", 1, exec_id="close", timestamp_ms=2)])[0]
    row = ClosedPnl("order-close", "linear", "ETHUSDT", "Buy", Decimal("1"),
                    Decimal("1"), Decimal("100"), Decimal("100"), Decimal("-0.02"),
                    Decimal("2"), Decimal("0.01"), Decimal("0.01"), 2)
    assert episode_closed_pnl_difference([episode], [row]) is None
