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


def reconstruct(executions: list[Execution]) -> list[TradeEpisode]:
    ordered = sorted(
        executions,
        key=lambda row: (row.timestamp_ms, row.exec_id, row.order_id, row.leaves_qty),
    )
    states: dict[tuple[str, str, int], tuple[Decimal, TradeEpisode | None]] = {}
    output: list[TradeEpisode] = []
    for execution in ordered:
        key = (execution.category, execution.symbol, execution.position_idx)
        position, episode = states.get(key, (Decimal("0"), None))
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
