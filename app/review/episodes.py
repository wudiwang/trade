from __future__ import annotations

import hashlib
from dataclasses import replace
from decimal import Decimal

from .models import ClosedPnl, EpisodeFill, Execution, TradeEpisode


def closed_pnl_for_episode(episode: TradeEpisode, rows: list[ClosedPnl]) -> Decimal:
    closing_orders = {
        fill.execution.order_id for fill in episode.fills
        if fill.action in {"reduce", "close"}
    }
    return sum(
        (row.closed_pnl for row in rows if row.order_id in closing_orders),
        Decimal("0"),
    )


def unmatched_closed_orders(episodes: list[TradeEpisode], rows: list[ClosedPnl]) -> set[str]:
    recognized = {
        fill.execution.order_id
        for episode in episodes
        for fill in episode.fills
        if fill.action in {"reduce", "close"}
    }
    return {row.order_id for row in rows if row.order_id not in recognized}


def missing_closed_pnl_orders(episodes: list[TradeEpisode], rows: list[ClosedPnl]) -> set[str]:
    returned = {row.order_id for row in rows}
    expected = {
        fill.execution.order_id
        for episode in episodes
        for fill in episode.fills
        if fill.action in {"reduce", "close"}
    }
    return expected - returned


def derive_initial_positions(
    current_positions: dict[tuple[str, str, int], Decimal],
    executions_after_start: list[Execution],
) -> dict[tuple[str, str, int], Decimal]:
    initial = dict(current_positions)
    for execution in executions_after_start:
        key = (execution.category, execution.symbol, execution.position_idx)
        initial[key] = initial.get(key, Decimal("0")) - _signed(execution)
    return {key: value for key, value in initial.items() if value}


def episode_price_return_pct(episode: TradeEpisode) -> Decimal | None:
    if episode.status != "closed" or episode.inherited_position:
        return None
    entries = [fill.execution for fill in episode.fills if fill.action in {"entry", "add"}]
    exits = [fill.execution for fill in episode.fills if fill.action in {"reduce", "close"}]
    entry_qty = sum((row.qty for row in entries), Decimal("0"))
    exit_qty = sum((row.qty for row in exits), Decimal("0"))
    if not entry_qty or not exit_qty:
        return None
    entry_price = sum((row.price * row.qty for row in entries), Decimal("0")) / entry_qty
    exit_price = sum((row.price * row.qty for row in exits), Decimal("0")) / exit_qty
    move = ((entry_price - exit_price) if episode.direction == "short"
            else (exit_price - entry_price)) / entry_price
    return move * 100


def episode_net_from_executions(episode: TradeEpisode) -> Decimal | None:
    """Independent realized PnL for a fully observed, fully closed episode (excluding funding)."""
    if episode.status != "closed" or episode.inherited_position:
        return None
    entries = [fill.execution for fill in episode.fills if fill.action in {"entry", "add"}]
    exits = [fill.execution for fill in episode.fills if fill.action in {"reduce", "close"}]
    entry_qty = sum((row.qty for row in entries), Decimal("0"))
    exit_qty = sum((row.qty for row in exits), Decimal("0"))
    if entry_qty != exit_qty or not entry_qty:
        return None
    entry_value = sum((row.price * row.qty for row in entries), Decimal("0"))
    exit_value = sum((row.price * row.qty for row in exits), Decimal("0"))
    gross = exit_value - entry_value if episode.direction == "long" else entry_value - exit_value
    fees = sum((fill.execution.fee for fill in episode.fills), Decimal("0"))
    return gross - fees


def episode_closed_pnl_difference(
    episodes: list[TradeEpisode], rows: list[ClosedPnl]
) -> Decimal | None:
    differences: list[Decimal] = []
    for episode in episodes:
        independent = episode_net_from_executions(episode)
        if independent is None:
            return None
        closing_by_order: dict[str, list[Execution]] = {}
        for fill in episode.fills:
            if fill.action in {"reduce", "close"}:
                closing_by_order.setdefault(fill.execution.order_id, []).append(fill.execution)
        pnl_by_order: dict[str, list[ClosedPnl]] = {}
        for row in rows:
            if row.order_id in closing_by_order:
                pnl_by_order.setdefault(row.order_id, []).append(row)
        for order_id, fills in closing_by_order.items():
            matched = pnl_by_order.get(order_id, [])
            if len(matched) != 1:
                return None
            row = matched[0]
            closed_qty = sum((fill.qty for fill in fills), Decimal("0"))
            if (row.symbol != episode.symbol or row.side.lower() != fills[0].side.lower() or
                    row.closed_size != closed_qty):
                return None
        differences.append(abs(independent - closed_pnl_for_episode(episode, rows)))
    return sum(differences, Decimal("0")) if differences else None


def _signed(execution: Execution) -> Decimal:
    return execution.qty if execution.side.lower() == "buy" else -execution.qty


def _part(execution: Execution, qty: Decimal) -> Execution:
    if qty == execution.qty:
        return execution
    ratio = qty / execution.qty
    return replace(
        execution,
        qty=qty,
        value=execution.value * ratio,
        fee=execution.fee * ratio,
    )


def _new_episode(execution: Execution, direction: str) -> TradeEpisode:
    raw = (
        f"{execution.account_id}:{execution.category}:{execution.symbol}:"
        f"{execution.position_idx}:{execution.exec_id}"
    )
    episode_id = hashlib.sha256(raw.encode()).hexdigest()[:24]
    return TradeEpisode(
        episode_id=episode_id,
        category=execution.category,
        symbol=execution.symbol,
        direction=direction,
        position_idx=execution.position_idx,
        opened_ms=execution.timestamp_ms,
    )


def reconstruct(
    executions: list[Execution],
    initial_positions: dict[tuple[str, str, int], Decimal] | None = None,
    window_start_ms: int | None = None,
) -> list[TradeEpisode]:
    ordered = sorted(
        executions,
        key=lambda row: (row.timestamp_ms, row.exec_id, row.order_id, row.leaves_qty),
    )
    states: dict[tuple[str, str, int], tuple[Decimal, TradeEpisode | None]] = {
        key: (position, None) for key, position in (initial_positions or {}).items() if position
    }
    output: list[TradeEpisode] = []
    for execution in ordered:
        key = (execution.category, execution.symbol, execution.position_idx)
        position, episode = states.get(key, (Decimal("0"), None))
        if position and episode is None:
            episode = _new_episode(execution, "long" if position > 0 else "short")
            episode.opened_ms = window_start_ms or execution.timestamp_ms
            episode.max_abs_qty = abs(position)
            episode.inherited_position = True
            output.append(episode)
        delta = _signed(execution)
        remaining = abs(delta)
        delta_sign = Decimal("1") if delta > 0 else Decimal("-1")

        if position == 0:
            direction = "long" if delta > 0 else "short"
            episode = _new_episode(execution, direction)
            episode.fills.append(EpisodeFill(execution, "entry"))
            position = delta
            episode.max_abs_qty = abs(position)
            output.append(episode)
            states[key] = (position, episode)
            continue

        position_sign = Decimal("1") if position > 0 else Decimal("-1")
        if delta_sign == position_sign:
            assert episode is not None
            episode.fills.append(EpisodeFill(execution, "add"))
            position += delta
            episode.max_abs_qty = max(episode.max_abs_qty, abs(position))
            states[key] = (position, episode)
            continue

        assert episode is not None
        closing_qty = min(abs(position), remaining)
        close_action = "close" if closing_qty == abs(position) else "reduce"
        episode.fills.append(EpisodeFill(_part(execution, closing_qty), close_action))
        position += delta_sign * closing_qty
        remaining -= closing_qty
        if position == 0:
            episode.status = "closed"
            episode.closed_ms = execution.timestamp_ms
            episode = None

        if remaining > 0:
            opening = _part(execution, remaining)
            direction = "long" if delta_sign > 0 else "short"
            episode = _new_episode(opening, direction)
            episode.fills.append(EpisodeFill(opening, "entry"))
            episode.max_abs_qty = remaining
            output.append(episode)
            position = delta_sign * remaining
        states[key] = (position, episode)
    return sorted(output, key=lambda row: (row.opened_ms, row.episode_id))
