from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from typing import Iterable


BEIJING = timezone(timedelta(hours=8), name="Asia/Shanghai")
TRADING_TYPES = {"TRADE", "SETTLEMENT", "SESSION_SETTLE_PNL", "MOVE_POSITION"}


def decimal(value) -> Decimal:
    if value in (None, ""):
        return Decimal("0")
    return Decimal(str(value))


def beijing_day(day: str | date) -> tuple[int, int]:
    d = date.fromisoformat(day) if isinstance(day, str) else day
    start = datetime.combine(d, time.min, tzinfo=BEIJING)
    end = start + timedelta(days=1)
    return int(start.timestamp() * 1000), int(end.timestamp() * 1000)


@dataclass(frozen=True)
class Transaction:
    tx_id: str
    category: str
    symbol: str
    timestamp_ms: int
    tx_type: str
    cash_flow: Decimal
    funding: Decimal
    fee: Decimal
    change: Decimal
    currency: str
    account_id: str = "default"


@dataclass(frozen=True)
class LedgerSummary:
    closed_cash_flow: Decimal = Decimal("0")
    funding: Decimal = Decimal("0")
    fees: Decimal = Decimal("0")
    net_change: Decimal = Decimal("0")
    external_change: Decimal = Decimal("0")


def summarize_ledger(rows: Iterable[Transaction]) -> LedgerSummary:
    cash = funding = fees = net = external = Decimal("0")
    for row in rows:
        if row.tx_type.upper() in TRADING_TYPES:
            cash += row.cash_flow
            funding += row.funding
            fees += row.fee
            net += row.change
        else:
            external += row.change
    return LedgerSummary(cash, funding, fees, net, external)


@dataclass(frozen=True)
class Execution:
    exec_id: str
    order_id: str
    category: str
    symbol: str
    side: str
    qty: Decimal
    price: Decimal
    value: Decimal
    fee: Decimal
    timestamp_ms: int
    leaves_qty: Decimal = Decimal("0")
    is_maker: bool = False
    account_id: str = "default"
    position_idx: int = 0


@dataclass(frozen=True)
class Order:
    order_id: str
    category: str
    symbol: str
    side: str
    position_idx: int = 0
    order_type: str = ""
    stop_order_type: str = ""


@dataclass(frozen=True)
class ClosedPnl:
    order_id: str
    category: str
    symbol: str
    side: str
    qty: Decimal
    closed_size: Decimal
    avg_entry_price: Decimal
    avg_exit_price: Decimal
    closed_pnl: Decimal
    leverage: Decimal
    open_fee: Decimal
    close_fee: Decimal
    updated_ms: int


@dataclass(frozen=True)
class Kline:
    start_ms: int
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    turnover: Decimal


@dataclass
class EpisodeFill:
    execution: Execution
    action: str


@dataclass
class TradeEpisode:
    episode_id: str
    category: str
    symbol: str
    direction: str
    position_idx: int
    opened_ms: int
    closed_ms: int | None = None
    status: str = "open"
    fills: list[EpisodeFill] = field(default_factory=list)
    max_abs_qty: Decimal = Decimal("0")
    realized_pnl: Decimal = Decimal("0")

