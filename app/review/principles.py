"""第一准则复盘:只有止盈与止损,没有中途的情绪变化。

纯函数,不做任何 I/O。输入是已重建的交易回合(TradeEpisode)、标准化订单历史(OrderRecord)
和 1m K 线;输出是逐条可核对的检查(Check)和一个结论(verdict)。

两条不可破的约束:
1. 盈亏结果不参与合规判定——硬检查只看"订单是怎么来的、什么时候挂的、挂在什么价位、价位怎么变的"。
   成交价不参与任何硬检查(追踪止损的实际止损价交易所不记录,所以它只按"何时加的"判断)。
2. 每条 detail 都写明所依据的时间和价位,方便本人对照记忆核实。时间按 params.tz_offset_hours
   显示(单独调用时默认曼谷 UTC+7;日报流水线传入 review.timezone,和报告页眉一致)。

"计划"的定义:进场后宽限期(sl_grace_seconds)内**有记录确认**的止损价 / 止盈价。能确认的来源只有
两种:进场单自带的 attached_sl / attached_tp;或一份更新时间落在宽限期内的订单快照。
订单最后一行(成交/取消时)的价位只是"最后的价位",不能当成"挂出时的价位":

数据边界(Bybit 官方文档):
- https://bybit-exchange.github.io/docs/v5/position/trading-stop
  "Full position TP/SL orders: This API can be used to modify the parameters of existing
  TP/SL orders." —— 整仓止盈止损是在原订单上改的,订单历史只留最后的触发价。
  "Partial position TP/SL orders: This API can only add partial position TP/SL orders."
  "The system will cancel these orders if the position is closed" —— 所以创建时间早于本回合
  首次进场的交易所止盈止损单,属于更早的仓位,不记到这一笔头上。
- https://bybit-exchange.github.io/docs/v5/order/order-list
  "the orders with "Cancelled" (fully cancelled order), "Rejected", "Deactivated" can be query"
  仅限最近 24 小时;updatedTime = "Order updated timestamp (ms)"(文档没有写改价是否刷新它,
  这里按"会刷新"处理,需用一次真实数据核实)。
因此:进场时的价位没有记录时,相关检查给"未知",不给"通过";已取消的止损单可能已超过保留期时,
"没看到止损单"不等于"没有止损"。进场单上的 attached_sl / attached_tp 被当作"下单时的原始值"
(文档没有说明之后改止损时是否会回写进场单;见 bybit_client.py 的说明)。

离场的统一口径(同一种行为,不管用的是哪个按钮,结论相同):
- 平仓单的价位在进场价的亏损一侧或保本带内 = 止损离场(收紧过会加注,不算破规矩);
- 价位到了或超过进场时计划的止盈位 = 止盈离场;
- 价位在进场价和计划止盈位之间(止盈被拉近、止损被拖到盈利一侧、中途挂的限价/条件平仓单),
  或进场时没有目标、中途挂出后几分钟内就成交 = 中途下车;
- 追踪止损:进场时就设的算计划内;中途才加的算中途改计划。
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from decimal import Context, Decimal, InvalidOperation
from enum import Enum
from typing import Any, Iterable

from .models import Kline, TradeEpisode

try:  # models.py 由另一位同事并行扩展;没有 OrderRecord 时本模块仍可导入(只按属性取值)。
    from .models import OrderRecord
except ImportError:  # pragma: no cover - 仅在契约类型尚未落地时触发
    OrderRecord = Any  # type: ignore[assignment,misc]


class CheckState(str, Enum):
    PASS = "pass"
    FAIL = "fail"
    UNKNOWN = "unknown"
    NA = "na"


@dataclass(frozen=True)
class Check:
    key: str
    label: str
    tier: str                      # "hard" | "day" | "reference"
    state: CheckState
    detail: str = ""
    value: Decimal | str | None = None


@dataclass(frozen=True)
class ExitLeg:
    time_ms: int
    qty: Decimal
    price: Decimal
    kind: str                      # "stop_loss"|"take_profit"|"manual"|"liquidation"|"trailing_stop"|
                                   # "adl"|"unknown"。止盈单/平仓单被挪到计划之外成交,记为 "manual"。


@dataclass(frozen=True)
class PrincipleReview:
    episode_id: str
    verdict: str
    checks: dict[str, Check]
    exits: tuple[ExitLeg, ...]
    planned_sl: Decimal | None     # 宽限期内有记录确认的价位;确认不了就是 None
    planned_tp: Decimal | None
    planned_rr: Decimal | None
    realized_r: Decimal | None
    hold_seconds: int | None


@dataclass(frozen=True)
class PrincipleParams:
    sl_grace_seconds: int = 120            # 进场后多少秒内设好止损/止盈算"进场即设"
    min_rr: Decimal = Decimal("2.0")       # 计划盈亏比达标线(仅参考)
    horizon_hours: int = 72                # 中途下车后向前看多久
    flip_minutes: int = 30                 # 平仓后多少分钟内反向开仓算反手
    reentry_minutes: int = 30              # 止损后多少分钟内同向重进算"立刻重进"
    max_trades_per_day: int | None = None  # 未配置则当日笔数只报数
    entry_window_seconds: int = 120        # 首笔成交后多少秒内的同向成交仍算同一次进场
    sl_gap_seconds: int = 120              # 止损被撤后裸奔超过多少秒算"撤掉止损"
    # 订单挂出时的价位没有记录(只有最后一行)时,是否直接当作从没改过。默认否:给"未知"。
    assume_single_stop_snapshot_unchanged: bool = False
    orders_synced_ms: int | None = None    # 订单历史的拉取时刻(用于判断已取消订单是否还查得到)
    cancelled_order_retention_hours: int = 24   # 交易所保留已取消订单的时长
    tz_offset_hours: float = 7             # detail 里的时间所用时区;单独调用时默认曼谷
    target_min_rest_seconds: int = 300     # 进场时没有目标位:中途挂的止盈单至少挂多久才算目标而不是平仓
    stop_into_profit_is_exit: bool = True  # 止损被移到盈利一侧后触发 / 中途加的追踪止损,是否算中途下车
    breakeven_band_pct: Decimal = Decimal("0.2")   # 进场价盈利一侧这么多百分比以内仍算保本止损
    # 这个时刻之后被取消的订单保证都在 orders 里(本地快照连续覆盖的起点);
    # 不给时按 orders_synced_ms - 保留时长 计算。
    orders_complete_since_ms: int | None = None
    # 只按这个时刻之前的订单状态评价(日报用:成交截到当天结束,订单证据也截到同一刻)。
    as_of_ms: int | None = None


VERDICTS = ("合规", "中途下车", "无止损", "止损被放宽", "逆向加仓", "证据不足", "继承仓位不评价", "持仓中")

HARD_KEYS = ("sl_at_entry", "sl_not_loosened", "no_averaging_down", "exit_by_plan")
REFERENCE_KEYS = ("tp_at_entry", "planned_rr", "early_exit_cost", "hold_time")
LABELS = {
    "sl_at_entry": "进场即设止损",
    "exit_by_plan": "只有止盈与止损两种离场",
    "sl_not_loosened": "止损没有被放宽",
    "no_averaging_down": "没有逆向加仓",
    "tp_at_entry": "止盈目标已锚定",
    "planned_rr": "盈亏比",
    "early_exit_cost": "离场之后的走势",
    "hold_time": "持仓时长",
    "flip_after_exit": "反手",
    "revenge_reentry": "止损后立刻重进",
    "trade_count": "当日笔数",
}
_FAIL_VERDICT = (
    ("sl_at_entry", "无止损"),
    ("sl_not_loosened", "止损被放宽"),
    ("no_averaging_down", "逆向加仓"),
    ("exit_by_plan", "中途下车"),
)
_JUDGED = ("合规", "中途下车", "无止损", "止损被放宽", "逆向加仓")
_TERMINAL = ("filled", "cancelled", "rejected", "expired")
_KIND_CN = {
    "stop_loss": "止损", "take_profit": "止盈", "manual": "手动", "liquidation": "强平",
    "trailing_stop": "追踪止损", "adl": "ADL 自动减仓", "unknown": "来源不明",
}
_TWO = Decimal("0.01")
_DISPLAY = Context(prec=12)
_SOURCE_RANK = {"attached": 0, "order": 1, "trailing": 2}
_HINDSIGHT = "这只是事后对照,不改变本笔结论"


# --------------------------------------------------------------------------- 基础工具

def _dec(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, Decimal):
        number = value
    else:
        try:
            number = Decimal(str(value).strip())
        except (InvalidOperation, ValueError):
            return None
    return number if number.is_finite() else None


def _price(value: Any) -> Decimal | None:
    number = _dec(value)
    return number if number is not None and number > 0 else None


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return default


def _num(value: Decimal | None) -> str:
    if value is None:
        return "未知"
    text = format(_DISPLAY.plus(value), "f")      # 最多 12 位有效数字,只影响显示
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def _signed(value: Decimal) -> str:
    return ("+" if value >= 0 else "") + _num(value.quantize(_TWO))


def _clock(ms: int, params: PrincipleParams, ref_ms: int | None = None) -> str:
    """params.tz_offset_hours 时区下的 HH:MM;和参照时刻不在同一天时写成 MM-DD HH:MM。"""
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    try:
        offset = timedelta(hours=float(params.tz_offset_hours))
        moment = epoch + timedelta(milliseconds=ms) + offset
        ref = None if ref_ms is None else epoch + timedelta(milliseconds=ref_ms) + offset
    except (OverflowError, ValueError, TypeError):
        return "??:??"
    if ref is not None and ref.date() != moment.date():
        return moment.strftime("%m-%d %H:%M")
    return moment.strftime("%H:%M")


def _span(seconds: int) -> str:
    seconds = max(0, int(seconds))
    days, rest = divmod(seconds, 86_400)
    hours, rest = divmod(rest, 3_600)
    minutes, secs = divmod(rest, 60)
    if days:
        return f"{days} 天 {hours} 小时"
    if hours:
        return f"{hours} 小时 {minutes} 分"
    if minutes:
        return f"{minutes} 分 {secs} 秒" if secs else f"{minutes} 分钟"
    return f"{secs} 秒"


def _vwap(rows: Iterable[Any]) -> Decimal | None:
    qty = cost = Decimal("0")
    for row in rows:
        qty += row.qty
        cost += row.qty * row.price
    return cost / qty if qty > 0 else None


def _check(key: str, tier: str, state: CheckState, detail: str = "",
           value: Decimal | str | None = None) -> Check:
    return Check(key, LABELS[key], tier, state, detail, value)


# --------------------------------------------------------------------------- 输入归一

@dataclass(frozen=True)
class _Fill:
    time_ms: int
    qty: Decimal
    price: Decimal
    order_id: str
    action: str
    origin: str
    order_type: str


@dataclass(frozen=True)
class _Snap:
    order_id: str
    symbol: str
    account_id: str
    side: str
    position_idx: int
    order_type: str
    qty: Decimal | None
    price: Decimal | None
    avg_fill_price: Decimal | None
    trigger_price: Decimal | None
    reduce_only: bool
    close_position: bool
    status: str
    origin: str
    attached_sl: Decimal | None
    attached_tp: Decimal | None
    created_ms: int
    updated_ms: int
    price_known: bool = True       # False:截止时刻(as_of_ms)订单还在,但当时的价位没有记录


@dataclass(frozen=True)
class _Group:
    """同一 order_id 的全部快照,按更新时间排序;last 是最新状态。"""
    snaps: tuple[_Snap, ...]

    @property
    def first(self) -> _Snap:
        return self.snaps[0]

    @property
    def last(self) -> _Snap:
        return self.snaps[-1]

    @property
    def end_ms(self) -> int | None:
        return self.last.updated_ms if self.last.status in _TERMINAL else None


@dataclass(frozen=True)
class _Point:
    """某一刻有记录确认的一个价位。"""
    time_ms: int
    price: Decimal
    order_id: str
    source: str        # "attached" | "order"
    live: bool = False  # 持仓中仍有效的订单的现值(数据最晚时刻)


@dataclass(frozen=True)
class _Track:
    """一张止损单(或目标单)从挂出到结束的全部已知价位。"""
    order_id: str
    source: str                  # "attached" | "order" | "trailing"
    start_ms: int                # 从何时起存在(不早于首次进场)
    end_ms: int | None           # 结束时刻;None = 一直有效
    cancelled: bool              # 是被取消的(而不是成交)
    points: tuple[_Point, ...]
    pinned: bool                 # 挂出时(其后宽限期内)的价位有记录
    last_price: Decimal | None   # 最后记录的价位(可能是改过之后的)
    last_ms: int | None          # 最后一行的更新时刻
    conditional: bool = False    # 本人挂的条件单(分不清方向时用)


def _fills(episode: Any) -> list[_Fill]:
    rows: list[_Fill] = []
    for item in list(getattr(episode, "fills", None) or []):
        execution = getattr(item, "execution", None)
        action = str(getattr(item, "action", "") or "").lower()
        qty = _dec(getattr(execution, "qty", None))
        price = _price(getattr(execution, "price", None))
        if execution is None or qty is None or qty <= 0 or price is None:
            continue
        if action not in ("entry", "add", "reduce", "close"):
            continue
        rows.append(_Fill(
            _int(getattr(execution, "timestamp_ms", 0)), qty, price,
            str(getattr(execution, "order_id", "") or ""), action,
            str(getattr(execution, "origin", "") or "").lower(),
            str(getattr(execution, "order_type", "") or "").lower(),
        ))
    rows.sort(key=lambda row: row.time_ms)
    return rows


def _snap(order: Any) -> _Snap | None:
    order_id = str(getattr(order, "order_id", "") or "")
    if not order_id:
        return None
    created = _int(getattr(order, "created_ms", 0))
    updated = _int(getattr(order, "updated_ms", 0)) or created
    return _Snap(
        order_id=order_id,
        symbol=str(getattr(order, "symbol", "") or "").upper(),
        account_id=str(getattr(order, "account_id", "") or ""),
        side=str(getattr(order, "side", "") or "").lower(),
        position_idx=_int(getattr(order, "position_idx", 0)),
        order_type=str(getattr(order, "order_type", "") or "").lower(),
        qty=_dec(getattr(order, "qty", None)),
        price=_price(getattr(order, "price", None)),
        avg_fill_price=_price(getattr(order, "avg_fill_price", None)),
        trigger_price=_price(getattr(order, "trigger_price", None)),
        reduce_only=bool(getattr(order, "reduce_only", False)),
        close_position=bool(getattr(order, "close_position", False)),
        status=str(getattr(order, "status", "") or "").lower(),
        origin=str(getattr(order, "origin", "") or "unknown").lower(),
        attached_sl=_price(getattr(order, "attached_sl", None)),
        attached_tp=_price(getattr(order, "attached_tp", None)),
        created_ms=created,
        updated_ms=max(updated, created),
    )


def _groups(orders: Iterable[Any], cutoff_ms: int | None = None) -> dict[str, _Group]:
    """按 order_id 归并快照。给了 cutoff_ms 时只保留那一刻之前的状态:之后才挂的订单不算;
    那一刻还有效、之后才成交/取消的订单记为"当时仍有效,价位没有记录"。"""
    buckets: dict[str, list[_Snap]] = {}
    for order in orders:
        snap = _snap(order)
        if snap is not None:
            buckets.setdefault(snap.order_id, []).append(snap)
    result: dict[str, _Group] = {}
    for order_id, snaps in buckets.items():
        ordered = sorted(snaps, key=lambda row: (row.updated_ms, row.status in _TERMINAL))
        if cutoff_ms is not None:
            if min(row.created_ms for row in ordered) >= cutoff_ms:
                continue
            kept = [row for row in ordered if row.updated_ms < cutoff_ms]
            later = [row for row in ordered if row.updated_ms >= cutoff_ms]
            if later and not kept:
                seen = later[0]
                kept = [replace(seen, updated_ms=seen.created_ms, price_known=False,
                                status="untriggered" if seen.trigger_price is not None else "new")]
            ordered = kept
        unique: list[_Snap] = []
        for row in ordered:
            if not unique or row != unique[-1]:
                unique.append(row)
        result[order_id] = _Group(tuple(unique))
    return result


# --------------------------------------------------------------------------- 单笔评估

@dataclass
class _Context:
    params: PrincipleParams
    episode_id: str
    sign: Decimal                 # 多 +1 / 空 -1
    direction_cn: str
    inherited: bool
    is_open: bool
    fills: list[_Fill]
    entries: list[_Fill]
    exits: list[_Fill]
    first_entry_ms: int
    end_ms: int | None            # 回合结束时刻;持仓中为 None
    entry_ref: Decimal | None     # 首次进场均价(进场窗口内的成交)
    avg_entry: Decimal | None     # 全部进场/加仓均价
    initial_ids: set[int]         # 属于首次进场的成交(按 id())
    max_qty: Decimal
    groups: dict[str, _Group]     # 订单历史(全部)
    protective: list[_Group]      # 针对这笔仓位、与其存续期有交集的平仓方向订单
    coverage: str                 # "full" | "partial" | "none"
    grace_ms: int
    now_ms: int                   # 数据里能看到的最晚时刻(订单拉取时刻优先)
    complete_since: int | None    # 这个时刻之后被取消的订单一定看得到;None = 不考虑保留期
    roles: dict[str, str]

    def clock(self, ms: int) -> str:
        return _clock(ms, self.params, self.first_entry_ms)

    @property
    def deadline(self) -> int:
        return self.first_entry_ms + self.grace_ms

    @property
    def assume(self) -> bool:
        return bool(self.params.assume_single_stop_snapshot_unchanged)

    def loss_side(self, price: Decimal) -> Decimal:
        """> 0 表示该价位在进场价的亏损一侧,< 0 在盈利一侧。"""
        assert self.entry_ref is not None
        return (self.entry_ref - price) * self.sign

    def in_stop_zone(self, price: Decimal) -> bool:
        """亏损一侧,或盈利一侧的保本带之内。"""
        assert self.entry_ref is not None
        band = self.entry_ref * (_dec(self.params.breakeven_band_pct) or Decimal("0")) / 100
        return self.loss_side(price) >= -max(band, Decimal("0"))

    def farther(self, price: Decimal, than: Decimal) -> bool:
        """止损价 price 是否比 than 离进场更远(亏损更大)。"""
        return (than - price) * self.sign > 0


def _context(episode: Any, orders: list[Any], params: PrincipleParams) -> _Context:
    direction = str(getattr(episode, "direction", "") or "").lower()
    if direction not in ("long", "short"):
        raise ValueError("方向未知")
    sign = Decimal("1") if direction == "long" else Decimal("-1")
    fills = _fills(episode)
    entries = [row for row in fills if row.action in ("entry", "add")]
    exits = [row for row in fills if row.action in ("reduce", "close")]
    inherited = bool(getattr(episode, "inherited_position", False))
    opened_ms = _int(getattr(episode, "opened_ms", 0))
    closed_raw = getattr(episode, "closed_ms", None)
    closed_ms = None if closed_raw is None else _int(closed_raw)
    is_open = str(getattr(episode, "status", "") or "").lower() != "closed" or closed_ms is None
    first_entry_ms = entries[0].time_ms if entries and not inherited else opened_ms
    grace_ms = max(0, _int(params.sl_grace_seconds)) * 1000
    window_ms = max(0, _int(params.entry_window_seconds)) * 1000

    initial: list[_Fill] = []
    if entries and not inherited:
        first = entries[0]
        initial = [row for row in entries
                   if (first.order_id and row.order_id == first.order_id)
                   or row.time_ms - first.time_ms <= window_ms]
    entry_ref = _vwap(initial)
    avg_entry = None if inherited else _vwap(entries)

    end_ms = None if is_open else closed_ms
    cutoff = None if params.as_of_ms is None else _int(params.as_of_ms)
    if cutoff is not None and end_ms is not None and end_ms >= cutoff:
        cutoff = None             # 回合在截止时刻之后才结束:成交已经看到了,订单也用全量
    groups = _groups(orders, cutoff)
    symbol = str(getattr(episode, "symbol", "") or "").upper()
    position_idx = _int(getattr(episode, "position_idx", 0))
    close_side = "sell" if direction == "long" else "buy"
    protective: list[_Group] = []
    for group in groups.values():
        last = group.last
        if last.symbol != symbol or last.position_idx != position_idx or last.side != close_side:
            continue
        if last.status == "rejected":
            continue
        if end_ms is not None and group.first.created_ms > end_ms:
            continue
        if group.end_ms is not None and group.end_ms <= first_entry_ms:
            continue
        if (not inherited and last.origin in ("stop_loss", "take_profit", "trailing_stop")
                and group.first.created_ms < first_entry_ms - 5_000):
            # 仓位平掉时交易所会撤掉它的止盈止损单(trading-stop 文档);早于本次进场创建的
            # 只能属于上一笔仓位——哪怕本地只留下它"未触发"的旧快照。
            continue
        protective.append(group)
    protective.sort(key=lambda group: (group.first.created_ms, group.first.order_id))

    fill_ids = {row.order_id for row in fills if row.order_id}
    found = {order_id for order_id in fill_ids if order_id in groups}
    coverage = "full" if fill_ids and found == fill_ids else ("partial" if found else "none")

    max_qty = _dec(getattr(episode, "max_abs_qty", None)) or Decimal("0")
    if max_qty <= 0:
        max_qty = sum((row.qty for row in entries), Decimal("0"))

    synced = None if params.orders_synced_ms is None else _int(params.orders_synced_ms)
    if end_ms is not None:
        now_ms = end_ms
    elif synced is not None or cutoff is not None:
        now_ms = min(value for value in (synced, cutoff) if value is not None)
    else:
        now_ms = max([first_entry_ms] + [row.time_ms for row in fills]
                     + [group.last.updated_ms for group in protective])

    complete_since: int | None = None
    if params.orders_complete_since_ms is not None:
        complete_since = _int(params.orders_complete_since_ms)
    elif synced is not None:
        keep_ms = max(0, _int(params.cancelled_order_retention_hours)) * 3_600_000
        complete_since = synced - keep_ms

    ctx = _Context(
        params=params, episode_id=str(getattr(episode, "episode_id", "") or ""), sign=sign,
        direction_cn="多" if direction == "long" else "空", inherited=inherited, is_open=is_open,
        fills=fills, entries=entries, exits=exits, first_entry_ms=first_entry_ms, end_ms=end_ms,
        entry_ref=entry_ref, avg_entry=avg_entry, initial_ids={id(row) for row in initial},
        max_qty=max_qty, groups=groups, protective=protective, coverage=coverage,
        grace_ms=grace_ms, now_ms=now_ms, complete_since=complete_since, roles={},
    )
    ctx.roles = _assign_roles(ctx)
    return ctx


def _base_role(ctx: _Context, group: _Group) -> str:
    """平仓方向订单的角色:止损、止盈目标,还是一次手动离场。只看订单本身,不看结果。"""
    last = group.last
    origin = last.origin
    if origin in ("stop_loss", "take_profit", "trailing_stop", "liquidation", "adl"):
        return origin
    if origin != "user":
        return "unknown"
    if ctx.entry_ref is None:
        return "manual"
    closing = last.reduce_only or last.close_position
    triggers = [snap.trigger_price for snap in group.snaps if snap.trigger_price is not None]
    if triggers:
        if any(ctx.loss_side(price) >= 0 for price in triggers):
            # 亏损一侧(或保本位)的条件平仓单 = 自己挂的止损。没勾"只减仓"但数量不超过仓位的
            # 条件单触发后同样只会平掉这笔仓位,也按止损算(Bybit 条件单默认不勾只减仓)。
            qty = last.qty
            if closing or (qty is not None and 0 < qty <= ctx.max_qty):
                return "stop_loss"
            return "manual"
        return "take_profit" if closing else "manual"
    if closing and last.order_type == "limit" and last.price is not None \
            and ctx.loss_side(last.price) < 0:
        return "take_profit"              # 只减仓的限价单挂在盈利一侧 = 目标单(何时挂的另行判断)
    return "manual"


def _assign_roles(ctx: _Context) -> dict[str, str]:
    roles = {group.first.order_id: _base_role(ctx, group) for group in ctx.protective}
    gap_ms = max(0, _int(ctx.params.sl_gap_seconds)) * 1000
    # 自己挂的条件单接在一张被撤掉的止损单后面 = 同一条止损的延续(哪怕新价位已到盈利一侧)。
    for group in ctx.protective:
        order_id = group.first.order_id
        if roles[order_id] != "take_profit" or group.last.origin != "user" \
                or group.last.trigger_price is None:
            continue
        for other in ctx.protective:
            if other is group or roles[other.first.order_id] != "stop_loss":
                continue
            if other.last.status not in ("cancelled", "expired") or other.end_ms is None:
                continue
            if abs(group.first.created_ms - other.end_ms) <= gap_ms:
                roles[order_id] = "stop_loss"
                break
    return roles


def _order_level(snap: _Snap) -> Decimal | None:
    return snap.trigger_price or (snap.price if snap.order_type == "limit" else None)


def _order_track(ctx: _Context, group: _Group, trailing: bool = False) -> _Track:
    order_id = group.first.order_id
    start = max(group.first.created_ms, ctx.first_entry_ms)
    points: list[_Point] = []
    if not trailing:
        for snap in group.snaps:
            price = _order_level(snap)
            if price is None or not snap.price_known:
                continue
            points.append(_Point(max(snap.updated_ms, start), price, order_id, "order"))
        last = group.last
        if ctx.is_open and last.status not in _TERMINAL and points and last.price_known:
            # 持仓中仍然有效的订单:这个价位就是数据最晚时刻的现值
            points.append(_Point(max(ctx.now_ms, points[-1].time_ms), points[-1].price, order_id,
                                 "order", True))
    pinned = any(point.time_ms <= start + ctx.grace_ms and not point.live for point in points)
    known = [snap for snap in group.snaps if snap.price_known and _order_level(snap) is not None]
    return _Track(
        order_id=order_id, source="trailing" if trailing else "order", start_ms=start,
        end_ms=group.end_ms, cancelled=group.last.status in ("cancelled", "expired"),
        points=tuple(points), pinned=pinned,
        last_price=None if trailing or not known else _order_level(known[-1]),
        last_ms=known[-1].updated_ms if known else None,
        conditional=group.last.origin == "user" and group.last.trigger_price is not None,
    )


def _attached_tracks(ctx: _Context, field: str) -> list[_Track]:
    tracks: list[_Track] = []
    seen: set[str] = set()
    for row in ctx.entries:
        if not row.order_id or row.order_id in seen:
            continue
        seen.add(row.order_id)
        group = ctx.groups.get(row.order_id)
        price = None if group is None else getattr(group.last, field)
        if price is not None:
            point = _Point(row.time_ms, price, row.order_id, "attached")
            tracks.append(_Track(row.order_id, "attached", row.time_ms, None, False, (point,),
                                 True, price, row.time_ms))
    return tracks


def _with_attached_pin(ctx: _Context, tracks: list[_Track], attached: list[_Track],
                       origin: str) -> list[_Track]:
    """进场单带着止损/止盈时,交易所随进场生成的那张单,挂出价就是进场单上的值。"""
    if not any(item.start_ms <= ctx.deadline for item in attached):
        return tracks
    out = []
    for track in tracks:
        group = ctx.groups.get(track.order_id)
        if (not track.pinned and track.source == "order" and group is not None
                and group.last.origin == origin and track.start_ms <= ctx.deadline):
            track = replace(track, pinned=True)
        out.append(track)
    return out


def _sorted_tracks(tracks: list[_Track]) -> list[_Track]:
    return sorted(tracks, key=lambda item: (item.start_ms, _SOURCE_RANK[item.source], item.order_id))


def _stop_tracks(ctx: _Context) -> list[_Track]:
    attached = _attached_tracks(ctx, "attached_sl")
    tracks = []
    for group in ctx.protective:
        role = ctx.roles.get(group.first.order_id)
        if role in ("stop_loss", "trailing_stop"):
            tracks.append(_order_track(ctx, group, trailing=role == "trailing_stop"))
    return _sorted_tracks(attached + _with_attached_pin(ctx, tracks, attached, "stop_loss"))


def _target_tracks(ctx: _Context) -> list[_Track]:
    attached = _attached_tracks(ctx, "attached_tp")
    tracks = [_order_track(ctx, group) for group in ctx.protective
              if ctx.roles.get(group.first.order_id) == "take_profit"]
    tracks = [track for track in tracks if track.last_price is not None or track.points]
    return _sorted_tracks(attached + _with_attached_pin(ctx, tracks, attached, "take_profit"))


@dataclass(frozen=True)
class _Plan:
    """进场时的计划:只收宽限期内有记录确认的价位。"""
    points: tuple[_Point, ...]     # 宽限期内确认的价位(assume 模式下 = 进场时就在的订单的最早已知价位)
    assumed: bool
    unconfirmed: tuple[_Track, ...]   # 进场时就挂了、但挂出价没有记录的订单

    @property
    def first(self) -> Decimal | None:
        return self.points[0].price if self.points else None


def _plan(ctx: _Context, tracks: list[_Track]) -> _Plan:
    priced = [point for track in tracks for point in track.points]
    priced.sort(key=lambda point: (point.time_ms, _SOURCE_RANK[point.source],
                                   abs(point.price - (ctx.entry_ref or point.price))))
    early = [point for point in priced if point.time_ms <= ctx.deadline and not point.live]
    in_grace = [track for track in tracks if track.source == "order" and track.start_ms <= ctx.deadline]
    unconfirmed = tuple(track for track in in_grace if not track.pinned)
    assumed = False
    if ctx.assume and unconfirmed:
        extra = [track.points[0] for track in unconfirmed if track.points]
        early = sorted(early + extra, key=lambda point: (point.time_ms, _SOURCE_RANK[point.source]))
        unconfirmed, assumed = (), bool(extra)
    return _Plan(tuple(early), assumed, unconfirmed)


def _retention_expired(ctx: _Context) -> bool:
    """回合结束得太早:它的止损单即便存在过,被取消后也可能已经从交易所历史里消失。"""
    if ctx.complete_since is None or ctx.end_ms is None:
        return False
    return ctx.end_ms < ctx.complete_since


def _set_how(ctx: _Context, track: _Track) -> str:
    delay_ms = track.start_ms - ctx.first_entry_ms
    if track.source == "attached" and delay_ms <= 0:
        return "随进场单一起设置"
    if delay_ms < 1000:
        return f"{ctx.clock(track.start_ms)} 与进场同时设置"
    return f"{ctx.clock(track.start_ms)} 设置(进场后 {_span(delay_ms // 1000)})"


def _track_text(ctx: _Context, track: _Track, noun: str) -> str:
    """"止损 95 10:00 设置(...)";挂出价没有记录时如实写成"最后记录的价位"。"""
    if track.source == "trailing":
        return f"追踪止损 {_set_how(ctx, track)}"
    if track.pinned or ctx.assume:
        price = track.points[0].price if track.points else track.last_price
        return f"{noun} {_num(price)} {_set_how(ctx, track)}"
    delay_ms = track.start_ms - ctx.first_entry_ms
    when = (f"{ctx.clock(track.start_ms)} 与进场同时挂出" if delay_ms < 1000 else
            f"{ctx.clock(track.start_ms)} 挂出(进场后 {_span(delay_ms // 1000)})")
    if track.last_price is None:
        return f"{noun}单 {when},当时的价位没有记录"
    return f"{noun}单 {when},最后记录的价位 {_num(track.last_price)}(挂出时的价位没有留存)"


def _plan_text(planned_sl: Decimal | None, planned_tp: Decimal | None) -> str:
    def show(value: Decimal | None) -> str:
        return "无记录" if value is None else _num(value)
    return f"计划止损 {show(planned_sl)}、止盈 {show(planned_tp)}"


def _check_sl_at_entry(ctx: _Context, stops: list[_Track], targets: list[_Track]) -> Check:
    key, grace = "sl_at_entry", ctx.params.sl_grace_seconds
    if ctx.inherited:
        return _check(key, "hard", CheckState.UNKNOWN, "仓位是统计窗口之前开的,进场时的止损不在本次数据里")
    if not ctx.entries:
        return _check(key, "hard", CheckState.UNKNOWN, "没有可用的进场成交")
    entry_at = ctx.clock(ctx.first_entry_ms)
    horizon = ctx.complete_since
    # 进场时就挂了、但分不清原本是止损还是止盈的条件单(只留下最后的触发价,且在盈利一侧)
    ambiguous = [track for track in targets if track.conditional and not track.pinned
                 and track.start_ms <= ctx.deadline]
    if stops:
        first = stops[0]
        delay_ms = first.start_ms - ctx.first_entry_ms
        if delay_ms <= ctx.grace_ms:
            return _check(key, "hard", CheckState.PASS,
                          f"{entry_at} 进场 {_num(ctx.entry_ref)},{_track_text(ctx, first, '止损')},"
                          f"宽限 {grace} 秒", "on_time")
        what = "追踪止损" if first.source == "trailing" else f"止损单(最后价位 {_num(first.last_price)})"
        if ambiguous and not ctx.assume:
            return _check(key, "hard", CheckState.UNKNOWN,
                          f"{entry_at} 进场 {_num(ctx.entry_ref)}。{_track_text(ctx, ambiguous[0], '条件平仓')},"
                          f"分不清它挂出时是止损还是止盈", "ambiguous")
        if horizon is not None and ctx.first_entry_ms + ctx.grace_ms < horizon:
            # 进场时的止损单如果在 horizon 之前被取消(例如被这张新单替换),交易所已不再返回它。
            hours = ctx.params.cancelled_order_retention_hours
            limit_ms = max(0, _int(ctx.params.sl_gap_seconds)) * 1000
            certain_from = max(horizon, ctx.first_entry_ms)
            if first.start_ms - certain_from <= limit_ms:
                return _check(
                    key, "hard", CheckState.UNKNOWN,
                    f"{entry_at} 进场 {_num(ctx.entry_ref)},看到的第一张{what}是 {ctx.clock(first.start_ms)} 挂的;"
                    f"更早的止损单如果已被取消,交易所只保留 {hours} 小时,现在查不到,"
                    f"无法确认进场时有没有止损", "late_unconfirmed")
            return _check(
                key, "hard", CheckState.FAIL,
                f"{entry_at} 进场 {_num(ctx.entry_ref)},看到的第一张{what}到 {ctx.clock(first.start_ms)} 才挂出;"
                f"至少从 {ctx.clock(certain_from)} 起的 {_span((first.start_ms - certain_from) // 1000)}"
                f"仓位没有止损单(更早被取消的订单已超过 {hours} 小时保留期,进场时有没有设过无法确认)",
                "late")
        return _check(
            key, "hard", CheckState.FAIL,
            f"{entry_at} 进场 {_num(ctx.entry_ref)},{what} 到 {ctx.clock(first.start_ms)} 才设置,"
            f"晚了 {_span(delay_ms // 1000)}(宽限 {grace} 秒),这段时间仓位没有止损",
            "late")
    if ctx.coverage != "full":
        return _check(key, "hard", CheckState.UNKNOWN,
                      f"{entry_at} 进场,但订单历史没有覆盖这笔交易的全部成交,无法确认有没有止损")
    if _retention_expired(ctx):
        hours = ctx.params.cancelled_order_retention_hours
        return _check(key, "hard", CheckState.UNKNOWN,
                      f"{entry_at} 进场,订单历史里没有止损单;但这笔在拉取订单前 {hours} 小时以上就已结束,"
                      f"交易所不再返回已取消的止损单,无法确认")
    if ambiguous and not ctx.assume:
        return _check(key, "hard", CheckState.UNKNOWN,
                      f"{entry_at} 进场 {_num(ctx.entry_ref)}。{_track_text(ctx, ambiguous[0], '条件平仓')},"
                      f"分不清它挂出时是止损还是止盈", "ambiguous")
    end = "至今" if ctx.end_ms is None else f"到 {ctx.clock(ctx.end_ms)} 离场"
    older = ""
    if horizon is not None and ctx.first_entry_ms < horizon:
        older = f"({ctx.clock(horizon)} 之前被取消的订单交易所已不再返回)"
    return _check(key, "hard", CheckState.FAIL,
                  f"{entry_at} 进场 {_num(ctx.entry_ref)},{end}期间订单历史里没有任何止损单,"
                  f"进场单也没有附带止损{older}", "none")


def _stop_gap(ctx: _Context, stops: list[_Track]) -> tuple[int, int, bool, bool] | None:
    """止损单被撤掉后仓位裸奔的第一段空档 (起, 止, 是否确定, 起点是否就是撤单时刻)。
    只看被取消/过期的止损单。空档里早于 complete_since 的部分可能有一张已超过保留期的止损单,
    不算确定。"""
    intervals = [(track.start_ms, track.end_ms if track.cancelled else None)
                 for track in stops if track.source != "attached"]
    if not intervals:
        return None
    horizon_end = ctx.now_ms if ctx.end_ms is None else ctx.end_ms
    limit_ms = max(0, _int(ctx.params.sl_gap_seconds)) * 1000
    intervals.sort(key=lambda item: item[0])
    covered_until: int = intervals[0][0]
    gap: tuple[int, int] | None = None
    for start, end in intervals:
        if start > covered_until and start - covered_until > limit_ms:
            gap = (covered_until, start)
            break
        if end is None:
            return None
        covered_until = max(covered_until, end)
    else:
        if horizon_end - covered_until > limit_ms:
            gap = (covered_until, horizon_end)
    if gap is None:
        return None
    start, end = gap
    if ctx.complete_since is None or start >= ctx.complete_since:
        return start, end, True, True
    certain_from = max(start, ctx.complete_since)
    if end - certain_from > limit_ms:
        return certain_from, end, True, False
    return start, end, False, True


def _succeeds(earlier: _Track, later: _Track) -> bool:
    """later 是不是接在 earlier 之后的(同一张单,或 earlier 结束之后才挂出)。"""
    if earlier.order_id == later.order_id:
        return True
    return earlier.end_ms is not None and later.start_ms >= earlier.end_ms


def _check_sl_not_loosened(ctx: _Context, stops: list[_Track], plan: _Plan) -> Check:
    key = "sl_not_loosened"
    if not stops:
        if ctx.coverage == "full" and not ctx.inherited:
            return _check(key, "hard", CheckState.NA, "全程没有止损,无从比较")
        return _check(key, "hard", CheckState.UNKNOWN, "订单历史里看不到这笔仓位的止损")
    priced = [track for track in stops if track.points]
    if not priced:
        if any(track.source == "trailing" for track in stops):
            return _check(key, "hard", CheckState.UNKNOWN, "只有追踪止损,订单历史不记录它的实际止损价")
        return _check(key, "hard", CheckState.UNKNOWN, "止损单在这段时间里有效,但当时的价位没有记录")
    by_order = {track.order_id: track for track in priced}
    points = sorted((point for track in priced for point in track.points),
                    key=lambda point: (point.time_ms, _SOURCE_RANK[point.source]))

    gap = _stop_gap(ctx, stops)
    if not plan.points:
        # 没有任何一个进场时确认过的止损价:不能把"最后的价位"当成"最初的止损"。
        if gap is not None and gap[2]:      # 撤掉止损后裸奔,不需要知道最初的价位
            start, end, _, exact = gap
            began = f"{ctx.clock(start)} 止损单被撤掉,之后" if exact else f"至少从 {ctx.clock(start)} 起的"
            return _check(key, "hard", CheckState.FAIL,
                          f"{began} {_span((end - start) // 1000)}仓位没有止损", "removed")
        wider = None
        for index, early in enumerate(points):
            for late in points[index + 1:]:
                if ctx.farther(late.price, early.price) \
                        and _succeeds(by_order[early.order_id], by_order[late.order_id]):
                    wider = (early, late)
                    break
            if wider:
                break
        if wider:
            early, late = wider
            return _check(
                key, "hard", CheckState.UNKNOWN,
                f"记录里的止损价从 {_num(early.price)}({ctx.clock(early.time_ms)})变成了 "
                f"{_num(late.price)}({ctx.clock(late.time_ms)}),离进场价更远;但进场时的止损价没有留存,"
                f"无法确认是不是比最初的止损更远", late.price)
        seen = "、".join(dict.fromkeys(_num(point.price) for point in points))
        first = priced[0]
        many = f"看到 {len(priced)} 张止损单,最后的价位 {seen}" if len(priced) > 1 else \
            f"只看到止损单最后的价位 {seen}"
        return _check(
            key, "hard", CheckState.UNKNOWN,
            f"{many}({ctx.clock(first.start_ms)} 挂出,最后更新于 {ctx.clock(points[-1].time_ms)})。"
            f"在仓位面板改止损是改在原订单上,交易所只留最后的价位;挂出时的价位没有留存,"
            f"无法确认中途有没有放宽过", points[-1].price)

    base = min(plan.points, key=lambda point: point.price * ctx.sign)   # 离进场最远的那个
    origin = "进场单附带" if base.source == "attached" else "止损单"
    base_text = f"最初止损 {_num(base.price)}({ctx.clock(base.time_ms)} {origin})"
    early_ids = {id(point) for point in plan.points}
    later = [point for point in points if id(point) not in early_ids]
    looser = [point for point in later if ctx.farther(point.price, base.price)]
    tighter = [point for point in later if ctx.farther(base.price, point.price)]
    if looser:
        worst = min(looser, key=lambda point: point.price * ctx.sign)
        entry = "" if ctx.entry_ref is None else f",进场价 {_num(ctx.entry_ref)}"
        return _check(key, "hard", CheckState.FAIL,
                      f"{base_text},到 {ctx.clock(worst.time_ms)} 止损价已是 {_num(worst.price)},"
                      f"离进场价更远{entry}", worst.price)
    if gap is not None and gap[2]:
        start, end, _, exact = gap
        began = f"{ctx.clock(start)} 止损单被撤掉,之后" if exact else f"至少从 {ctx.clock(start)} 起的"
        return _check(key, "hard", CheckState.FAIL,
                      f"{base_text},{began} {_span((end - start) // 1000)}仓位没有止损", "removed")
    loose_ends = [] if ctx.assume else [track for track in priced
                                        if track.source == "order" and not track.pinned]
    if loose_ends:
        track = loose_ends[0]
        return _check(
            key, "hard", CheckState.UNKNOWN,
            f"{base_text}。但 {ctx.clock(track.start_ms)} 挂出的止损单只留下最后的价位 "
            f"{_num(track.last_price)}(最后更新于 {ctx.clock(track.last_ms or track.start_ms)}),"
            f"它挂出时的价位没有记录,无法确认中途有没有比 {_num(base.price)} 更远", base.price)
    if gap is not None:
        start, end = gap[0], gap[1]
        hours = ctx.params.cancelled_order_retention_hours
        return _check(
            key, "hard", CheckState.UNKNOWN,
            f"{base_text}。{ctx.clock(start)} 到 {ctx.clock(end)} 之间看不到止损单,但这段时间里被取消的"
            f"订单已超过交易所 {hours} 小时保留期,无法确认当时有没有止损", base.price)
    if later:
        last = later[-1]
        if tighter:
            final = tighter[-1]
            tail = (f"到 {ctx.clock(final.time_ms)} 已收紧为 {_num(final.price)}"
                    f"(收紧不算放宽),没有比最初更远")
        else:
            tail = f"最后一次记录({ctx.clock(last.time_ms)})仍是 {_num(last.price)}"
        return _check(key, "hard", CheckState.PASS, f"{base_text},{tail}", base.price)
    if ctx.end_ms is not None and ctx.end_ms <= ctx.deadline:
        return _check(key, "hard", CheckState.PASS,
                      f"{base_text},这笔在进场后 {ctx.params.sl_grace_seconds} 秒宽限期内就已结束",
                      base.price)
    if ctx.assume:
        return _check(key, "hard", CheckState.PASS,
                      f"{base_text},订单历史只有这一个止损价,按未改动处理", base.price)
    if base.source == "attached":
        why = "订单历史里找不到之后的止损单记录(已取消的订单交易所只保留一段时间),无法确认之后有没有放宽过"
    else:
        why = "之后没有这张止损单的其他记录,无法确认中途有没有放宽过"
    return _check(key, "hard", CheckState.UNKNOWN,
                  f"只看到一个止损价 {_num(base.price)}({ctx.clock(base.time_ms)} {origin})。{why}",
                  base.price)


def _check_no_averaging_down(ctx: _Context) -> Check:
    key = "no_averaging_down"
    if ctx.inherited:
        return _check(key, "hard", CheckState.UNKNOWN, "仓位是统计窗口之前开的,原始均价不在本次数据里")
    if not ctx.entries:
        return _check(key, "hard", CheckState.UNKNOWN, "没有可用的进场成交")
    qty = cost = Decimal("0")
    order_basis: dict[str, Decimal] = {}     # 每张加仓单开始成交时的持仓均价
    adds: dict[str, dict[str, Any]] = {}
    for index, row in enumerate(ctx.fills):
        if row.action in ("reduce", "close"):
            if qty > 0:
                average = cost / qty
                closed = min(qty, row.qty)
                qty -= closed
                cost -= average * closed
            continue
        if id(row) not in ctx.initial_ids and qty > 0:
            order_key = row.order_id or f"#{index}"
            basis = order_basis.setdefault(order_key, cost / qty)
            item = adds.setdefault(order_key, {"time": row.time_ms, "qty": Decimal("0"),
                                               "cost": Decimal("0"), "basis": basis})
            item["qty"] += row.qty
            item["cost"] += row.qty * row.price
        qty += row.qty
        cost += row.qty * row.price
    if not adds:
        return _check(key, "hard", CheckState.PASS,
                      f"{ctx.clock(ctx.first_entry_ms)} 进场 {_num(ctx.entry_ref)} 后没有再加仓",
                      Decimal("0"))
    bad: list[str] = []
    for item in adds.values():
        price = item["cost"] / item["qty"]
        if (item["basis"] - price) * ctx.sign > 0:
            bad.append(f"{ctx.clock(item['time'])} 在 {_num(price)} 加{ctx.direction_cn} {_num(item['qty'])},"
                       f"当时持仓均价 {_num(item['basis'])},仓位处于浮亏")
    if bad:
        return _check(key, "hard", CheckState.FAIL, ";".join(bad), Decimal(len(bad)))
    return _check(key, "hard", CheckState.PASS,
                  f"加仓 {len(adds)} 次,成交价都在当时持仓均价的盈利一侧", Decimal(len(adds)))


@dataclass(frozen=True)
class _Leg:
    time_ms: int
    qty: Decimal
    price: Decimal
    kind: str
    origin: str                   # 细分来源,用于措辞(adl / liquidation / ...)
    order_id: str
    how: str = ""                 # 手动离场的具体方式,仅用于措辞
    level: Decimal | None = None  # 该订单的触发价/限价(不是成交价)
    placed_ms: int | None = None  # 该订单的挂出时刻
    pinned: bool = False          # 挂出时的价位有记录
    own: bool = False             # 本人挂的单(不是交易所按仓位止盈止损生成的)
    outcome: str = "ok"           # "ok" | "early" | "unknown"
    code: str = ""                # 判定代码(写进 check.value)
    reason: str = ""              # 一句说明


def _exit_legs(ctx: _Context) -> list[_Leg]:
    buckets: dict[str, list[_Fill]] = {}
    for index, row in enumerate(ctx.exits):
        buckets.setdefault(row.order_id or f"#{index}", []).append(row)
    legs: list[_Leg] = []
    for order_key, rows in buckets.items():
        group = ctx.groups.get(order_key)
        how = ""
        level = placed = None
        pinned = own = False
        if group is not None:
            role = ctx.roles.get(order_key) or _base_role(ctx, group)
            last = group.last
            own = last.origin == "user"
            placed = group.first.created_ms
            if own or role == "manual":
                when = ctx.clock(placed)
                if last.trigger_price is not None:
                    how = f"{when} 挂的条件单成交"
                elif last.order_type == "limit":
                    how = f"{when} 挂的限价平仓单成交"
                else:
                    how = "市价手动平仓"
            if role not in ("manual", "trailing_stop"):
                level = _order_level(last) if last.price_known else None
            if role in ("stop_loss", "take_profit"):
                pinned = _order_track(ctx, group).pinned
        else:       # 订单历史里没有这张单:退回成交记录自带的来源
            origin = rows[0].origin
            if origin in ("stop_loss", "take_profit", "trailing_stop", "liquidation", "adl"):
                role = origin
            elif origin == "user" and rows[0].order_type == "market":
                role, how = "manual", "市价手动平仓"
            else:
                role = "unknown"
        qty = sum((row.qty for row in rows), Decimal("0"))
        legs.append(_Leg(rows[0].time_ms, qty, _vwap(rows) or rows[0].price, role, role,
                         order_key, how, level, placed, pinned, own))
    legs.sort(key=lambda leg: leg.time_ms)
    return legs


def _judge_leg(ctx: _Context, leg: _Leg, targets: _Plan) -> _Leg:
    """止损/止盈/追踪止损单触发的离场,是不是按进场时的计划。只看订单的价位和挂出时间。"""
    params = ctx.params
    if leg.kind == "trailing_stop":
        late = ctx.inherited or (leg.placed_ms is not None and leg.placed_ms > ctx.deadline)
        if late and params.stop_into_profit_is_exit:
            seconds = max(0, ((leg.placed_ms or ctx.first_entry_ms) - ctx.first_entry_ms) // 1000)
            return replace(leg, outcome="early", code="late_trailing",
                           reason=f"追踪止损是进场后 {_span(seconds)}才加的(宽限 {params.sl_grace_seconds} 秒),"
                                  f"属于持仓中途改计划")
        if late:
            return replace(leg, code="note", reason=f"追踪止损是 {ctx.clock(leg.placed_ms or 0)} 才加的")
        return leg
    if leg.level is None or ctx.entry_ref is None:
        return leg                 # 没有订单价位可看(订单行缺失等):按交易所标注的止损/止盈处理
    level = leg.level
    placed = "" if leg.placed_ms is None else f"{ctx.clock(leg.placed_ms)} 挂出"
    if ctx.loss_side(level) >= 0 or (leg.kind == "stop_loss" and ctx.in_stop_zone(level)):
        return leg
    # 以下:平仓单的价位在盈利一侧
    confirmed = [point.price for point in targets.points]
    nearest = min(confirmed, key=lambda price: abs(price - ctx.entry_ref)) if confirmed else None
    if leg.kind == "stop_loss":
        if nearest is not None and (level - nearest) * ctx.sign >= 0:
            return replace(leg, code="note",
                           reason=f"止损已上移到计划止盈 {_num(nearest)} 之外的 {_num(level)} 后触发")
        if not params.stop_into_profit_is_exit:
            return replace(leg, code="note", reason=f"止损被移到盈利一侧的 {_num(level)} 后触发")
        if nearest is None and targets.unconfirmed:
            return replace(leg, outcome="unknown", code="target_unconfirmed",
                           reason=f"止损被移到盈利一侧的 {_num(level)} 后触发;进场时的止盈价位没有留存,"
                                  f"无法确认它是否已过目标位")
        goal = "也没有到任何事先定的止盈位" if nearest is None else f"没有等到计划止盈 {_num(nearest)}"
        return replace(leg, outcome="early", code="stop_in_profit",
                       reason=f"止损被移到盈利一侧的 {_num(level)} 后触发:这不是进场时定的止损,{goal}")
    # 止盈类(交易所止盈单,或本人挂在盈利一侧的只减仓限价/条件单)
    what = "平仓单" if leg.own else "止盈单"
    if nearest is not None:
        if (level - nearest) * ctx.sign >= 0:
            if leg.placed_ms is not None and leg.placed_ms > ctx.deadline:
                return replace(leg, code="note",
                               reason=f"{placed}的{what}价位 {_num(level)} 不比计划止盈 {_num(nearest)} 近")
            return leg
        moved = "" if leg.placed_ms is None or leg.placed_ms > ctx.deadline else "被拉近后"
        return replace(leg, outcome="early", code="target_pulled_in",
                       reason=f"{what}({placed}){moved}在 {_num(level)} 成交,比进场时计划的止盈 "
                              f"{_num(nearest)} 近,没有等到目标位")
    if targets.unconfirmed:
        return replace(leg, outcome="unknown", code="target_unconfirmed",
                       reason=f"{what}在 {_num(level)} 成交;进场时挂的止盈单只留下最后的价位,"
                              f"挂出时的价位没有留存,无法确认止盈有没有被拉近")
    # 进场时根本没有目标位:这是一张中途才挂的目标单
    rest_ms = max(0, _int(params.target_min_rest_seconds)) * 1000
    waited = None if leg.placed_ms is None else leg.time_ms - leg.placed_ms
    if waited is not None and waited < rest_ms:
        return replace(leg, outcome="early", code="target_at_market",
                       reason=f"进场时没有止盈位;{what} {placed},{_span(max(0, waited) // 1000)}后就成交,"
                              f"等同于手动平仓")
    if not leg.pinned and not ctx.assume:
        return replace(leg, outcome="unknown", code="late_target_unconfirmed",
                       reason=f"进场时没有止盈位;{what} {placed},成交价位 {_num(level)} 是什么时候定的"
                              f"没有记录,无法确认是不是临成交前才挪过来的")
    return replace(leg, code="note",
                   reason=f"止盈位 {_num(level)} 是 {ctx.clock(leg.placed_ms or 0)} 才设的,不是进场时的计划")


def _leg_text(ctx: _Context, leg: _Leg) -> str:
    share = ""
    if ctx.max_qty > 0:
        share = f"(占最大仓位 {_num((leg.qty / ctx.max_qty * 100).quantize(Decimal('0.1')))}%)"
    name = "ADL 自动减仓" if leg.origin == "adl" else _KIND_CN.get(leg.origin, leg.origin)
    if leg.kind == "manual" and leg.how:
        name = leg.how
    return f"{ctx.clock(leg.time_ms)} {name} {_num(leg.qty)}{share} @{_num(leg.price)}"


def _check_exit_by_plan(ctx: _Context, legs: list[_Leg], planned_sl: Decimal | None,
                        planned_tp: Decimal | None, stops: list[_Track],
                        stop_plan: _Plan) -> Check:
    key = "exit_by_plan"
    if not legs:
        if ctx.is_open:
            return _check(key, "hard", CheckState.NA, "仍在持仓,还没有离场成交")
        return _check(key, "hard", CheckState.UNKNOWN, "回合已结束,但没有可用的平仓成交")
    plan = _plan_text(planned_sl, planned_tp)
    everything = ";".join(_leg_text(ctx, leg) for leg in legs)
    forced = [leg for leg in legs if leg.origin == "liquidation"]
    adl = [leg for leg in legs if leg.origin == "adl"]
    manual = [leg for leg in legs if leg.origin == "manual"]
    early = [leg for leg in legs if leg.outcome == "early"]
    unknown = [leg for leg in legs if leg.origin == "unknown" or leg.outcome == "unknown"]
    if forced:
        return _check(key, "hard", CheckState.FAIL,
                      f"被交易所强平,不是止损也不是止盈离场。{everything}。{plan}", "liquidation")
    if manual or early:
        reasons = ";".join(dict.fromkeys(leg.reason for leg in early if leg.reason))
        if manual:
            head = f"有 {len(manual) + len(early)} 笔离场不是由止损或止盈单触发的"
            if early:
                head = f"有 {len(manual) + len(early)} 笔离场不是按进场时的止损/止盈计划触发的"
            value = "manual"
        else:
            head, value = f"有 {len(early)} 笔离场不是按进场时的止损/止盈计划触发的", early[0].code
        why = f"。{reasons}" if reasons else ""
        return _check(key, "hard", CheckState.FAIL, f"{head}{why}。{everything}。{plan}", value)
    if unknown:
        reasons = ";".join(dict.fromkeys(leg.reason for leg in unknown if leg.reason))
        if reasons:
            return _check(key, "hard", CheckState.UNKNOWN,
                          f"{reasons}。{everything}。{plan}", unknown[0].code or "unknown")
        return _check(key, "hard", CheckState.UNKNOWN,
                      f"有 {len(unknown)} 笔平仓找不到对应订单或来源不明,无法判断离场方式。{everything}",
                      "unknown")
    if adl and len(adl) == len(legs):
        return _check(key, "hard", CheckState.UNKNOWN,
                      f"被交易所 ADL 自动减仓,不属于本人操作,这笔的离场不评价。{everything}。{plan}", "adl")
    # 以下只加注:离场确实是按计划的止损/止盈触发的,但中间有过不改变结论的调整。
    notes: list[str] = [leg.reason for leg in legs if leg.code == "note" and leg.reason]
    if adl:
        notes.append("另有 ADL 自动减仓,不属于本人操作")
    base = None if not stop_plan.points else min(stop_plan.points,
                                                 key=lambda point: point.price * ctx.sign)
    if base is not None:
        for leg in legs:
            if leg.kind == "stop_loss" and leg.level is not None and ctx.in_stop_zone(leg.level) \
                    and ctx.farther(base.price, leg.level):
                notes.append(f"止损从最初的 {_num(base.price)} 收紧到 {_num(leg.level)} 后触发")
    note = "。注意:" + ";".join(dict.fromkeys(notes)) if notes else ""
    return _check(key, "hard", CheckState.PASS,
                  f"全部由止损/止盈单离场。{everything}。{plan}{note}",
                  "plan_changed" if notes else "planned")


def _check_tp_at_entry(ctx: _Context, targets: list[_Track], plan: _Plan) -> Check:
    key, grace = "tp_at_entry", ctx.params.sl_grace_seconds
    if ctx.inherited:
        return _check(key, "reference", CheckState.UNKNOWN, "仓位是统计窗口之前开的,进场时的止盈不在本次数据里")
    if not ctx.entries:
        return _check(key, "reference", CheckState.UNKNOWN, "没有可用的进场成交")
    entry_at = ctx.clock(ctx.first_entry_ms)
    if targets:
        first = targets[0]
        delay_ms = first.start_ms - ctx.first_entry_ms
        shown = plan.first if plan.first is not None else first.last_price
        levels = list(dict.fromkeys(
            [_num(shown)] + [_num(point.price) for track in targets for point in track.points]))
        more = f"(全程出现过的目标位:{'、'.join(levels)})" if len(levels) > 1 else ""
        if delay_ms <= ctx.grace_ms:
            return _check(key, "reference", CheckState.PASS,
                          f"{entry_at} 进场,{_track_text(ctx, first, '止盈')}{more}", plan.first)
        return _check(key, "reference", CheckState.FAIL,
                      f"{entry_at} 进场,止盈单到 {ctx.clock(first.start_ms)} 才挂出"
                      f"(最后价位 {_num(first.last_price)}),"
                      f"晚了 {_span(delay_ms // 1000)}(宽限 {grace} 秒){more}", first.last_price)
    if ctx.coverage != "full":
        return _check(key, "reference", CheckState.UNKNOWN, "订单历史没有覆盖这笔交易,无法确认有没有止盈目标")
    if _retention_expired(ctx):
        return _check(key, "reference", CheckState.UNKNOWN,
                      "订单历史里没有止盈单;但已取消的订单交易所只保留一段时间,无法确认")
    return _check(key, "reference", CheckState.FAIL,
                  f"{entry_at} 进场后,订单历史里没有止盈单,也没有挂只减仓的目标限价单")


def _check_planned_rr(ctx: _Context, planned_sl: Decimal | None, planned_tp: Decimal | None,
                      rr: Decimal | None, stop_plan: _Plan, target_plan: _Plan) -> Check:
    key = "planned_rr"
    if ctx.inherited or ctx.entry_ref is None:
        return _check(key, "reference", CheckState.UNKNOWN, "进场价不在本次数据里,无法计算")
    if planned_sl is None or planned_tp is None:
        missing = [name for name, value in (("止损", planned_sl), ("止盈", planned_tp)) if value is None]
        unconfirmed = [name for name, value, item in (("止损", planned_sl, stop_plan),
                                                      ("止盈", planned_tp, target_plan))
                       if value is None and item.unconfirmed]
        if unconfirmed:
            return _check(key, "reference", CheckState.UNKNOWN,
                          f"进场时挂了{'、'.join(unconfirmed)}单,但挂出时的价位没有留存,算不出事前的盈亏比")
        state = CheckState.NA if ctx.coverage == "full" else CheckState.UNKNOWN
        return _check(key, "reference", state,
                      f"进场后 {ctx.params.sl_grace_seconds} 秒内没有设好{'、'.join(missing)},没有事前的盈亏比可算")
    if rr is None:
        return _check(key, "reference", CheckState.UNKNOWN,
                      f"进场 {_num(ctx.entry_ref)}、止损 {_num(planned_sl)}、止盈 {_num(planned_tp)} "
                      f"不在进场价两侧,算不出盈亏比")
    state = CheckState.PASS if rr >= ctx.params.min_rr else CheckState.FAIL
    return _check(key, "reference", state,
                  f"进场 {_num(ctx.entry_ref)},止损 {_num(planned_sl)},止盈 {_num(planned_tp)},"
                  f"盈亏比 {_num(rr)}(参考线 {_num(_dec(ctx.params.min_rr))})", rr)


def _check_early_exit_cost(ctx: _Context, legs: list[_Leg], exit_check: Check,
                           planned_sl: Decimal | None, planned_tp: Decimal | None,
                           rr: Decimal | None, realized_r: Decimal | None,
                           klines: list[Any]) -> Check:
    """手动离场之后,价格先到原止损还是原止盈。只是事后对照,不参与任何判定。"""
    key = "early_exit_cost"
    manual = [leg for leg in legs if leg.kind == "manual"]
    if exit_check.state is not CheckState.FAIL or not manual:
        return _check(key, "reference", CheckState.NA, "没有手动离场")
    if planned_sl is None or planned_tp is None or rr is None:
        return _check(key, "reference", CheckState.NA, "没有可用的事前止损/止盈计划,无法和原计划比较")
    exit_ms = manual[0].time_ms
    start_ms = -(-exit_ms // 60_000) * 60_000          # 离场后的下一根完整 1m
    limit_ms = exit_ms + max(0, _int(ctx.params.horizon_hours)) * 3_600_000
    exit_minute = exit_ms // 60_000 * 60_000
    by_start: dict[int, tuple[Decimal, Decimal]] = {}
    for bar in klines:
        start = _int(getattr(bar, "start_ms", None), -1)
        high, low = _price(getattr(bar, "high", None)), _price(getattr(bar, "low", None))
        if start < 0 or high is None or low is None or not exit_minute <= start < limit_ms:
            continue
        by_start[start] = (high, low)
    actual = "未知" if realized_r is None else f"{_signed(realized_r)}R"
    head = f"{ctx.clock(exit_ms)} 手动离场 @{_num(manual[0].price)}"
    plan = f"原止损 {_num(planned_sl)}、原止盈 {_num(planned_tp)}"

    def touched(high: Decimal, low: Decimal) -> tuple[bool, bool]:
        if ctx.sign > 0:
            return low <= planned_sl, high >= planned_tp
        return high >= planned_sl, low <= planned_tp

    if exit_minute != start_ms and exit_minute in by_start and any(touched(*by_start[exit_minute])):
        return _check(key, "reference", CheckState.UNKNOWN,
                      f"{head}。离场所在的这一分钟({ctx.clock(exit_minute)})K线本身就触及了{plan}中的一个,"
                      f"分不清是在离场前还是离场后,不做对照。{_HINDSIGHT}", "exit_minute")
    # 只走从离场后第一根起首尾相接的 K 线:中间缺一根就停,不跨缺口下结论。
    bars: list[tuple[int, Decimal, Decimal]] = []
    expected = start_ms
    while expected < limit_ms and expected in by_start:
        bars.append((expected, *by_start[expected]))
        expected += 60_000
    if not bars:
        return _check(key, "reference", CheckState.UNKNOWN,
                      f"{head},缺少离场之后的 1m K线,无法比较。{plan}", "no_data")
    for start, high, low in bars:
        hit_sl, hit_tp = touched(high, low)
        if not (hit_sl or hit_tp):
            continue
        after = _span((start - exit_ms) // 1000)
        if hit_sl and hit_tp:
            return _check(key, "reference", CheckState.UNKNOWN,
                          f"{head}。{ctx.clock(start)} 这一根 1m 内同时触及{plan},分不出先后。"
                          f"{_HINDSIGHT}", "same_bar")
        if hit_tp:
            return _check(key, "reference", CheckState.NA,
                          f"{head}。之后价格在 {ctx.clock(start)}(离场后 {after})先到原止盈 {_num(planned_tp)},"
                          f"没有先碰原止损 {_num(planned_sl)}。按原计划是 {_signed(rr)}R,实际 {actual}(不含手续费)。"
                          f"{_HINDSIGHT}", "target_first")
        return _check(key, "reference", CheckState.NA,
                      f"{head}。之后价格在 {ctx.clock(start)}(离场后 {after})先到原止损 {_num(planned_sl)},"
                      f"没有先碰原止盈 {_num(planned_tp)}。{_HINDSIGHT}:离场依据不是止损也不是止盈",
                      "stop_first")
    last_end = bars[-1][0] + 60_000
    if last_end >= limit_ms:
        return _check(key, "reference", CheckState.NA,
                      f"{head}。之后 {ctx.params.horizon_hours} 小时内{plan}都没有触及。"
                      f"{_HINDSIGHT}", "neither")
    hole = "(之后的K线有缺口)" if any(start >= last_end for start in by_start) else ""
    return _check(key, "reference", CheckState.UNKNOWN,
                  f"{head}。连续的K线只到 {ctx.clock(last_end)}(离场后 {_span((last_end - exit_ms) // 1000)})"
                  f"{hole},到那时{plan}都还没有触及,之后无法确认", "incomplete")


def _check_hold_time(ctx: _Context) -> tuple[Check, int | None]:
    key = "hold_time"
    if ctx.inherited:
        return _check(key, "reference", CheckState.UNKNOWN, "仓位是统计窗口之前开的,开仓时间未知"), None
    if not ctx.entries:
        return _check(key, "reference", CheckState.UNKNOWN, "没有可用的进场成交"), None
    if ctx.end_ms is None:
        return _check(key, "reference", CheckState.NA,
                      f"{ctx.clock(ctx.first_entry_ms)} 进场,仍在持仓"), None
    seconds = max(0, (ctx.end_ms - ctx.first_entry_ms) // 1000)
    return _check(key, "reference", CheckState.NA,
                  f"{ctx.clock(ctx.first_entry_ms)} 进场 → {ctx.clock(ctx.end_ms)} 离场,持仓 {_span(seconds)}",
                  Decimal(seconds)), int(seconds)


def _verdict(inherited: bool, is_open: bool, checks: dict[str, Check]) -> str:
    if inherited:
        return "继承仓位不评价"
    if is_open:
        return "持仓中"
    for key, verdict in _FAIL_VERDICT:
        if checks[key].state is CheckState.FAIL:
            return verdict
    if any(checks[key].state is CheckState.UNKNOWN for key in HARD_KEYS):
        return "证据不足"
    return "合规"


def _exit_kind(leg: _Leg) -> str:
    """对外的离场方式。止盈类订单被挪到计划之外成交 = 用挂单完成的手动离场。"""
    if leg.origin == "take_profit" and leg.outcome == "early":
        return "manual"
    return leg.origin


def _review(episode: Any, orders: list[Any], klines: list[Any],
            params: PrincipleParams) -> PrincipleReview:
    ctx = _context(episode, orders, params)
    stops = _stop_tracks(ctx)
    targets = _target_tracks(ctx)
    stop_plan = _plan(ctx, stops)
    target_plan = _plan(ctx, targets)
    planned_sl = planned_tp = None
    if not ctx.inherited and ctx.entries:
        planned_sl, planned_tp = stop_plan.first, target_plan.first

    risk = reward = None
    if ctx.entry_ref is not None and planned_sl is not None:
        risk = (ctx.entry_ref - planned_sl) * ctx.sign
        risk = risk if risk > 0 else None
    if ctx.entry_ref is not None and planned_tp is not None:
        reward = (planned_tp - ctx.entry_ref) * ctx.sign
        reward = reward if reward > 0 else None
    planned_rr = (reward / risk).quantize(_TWO) if risk is not None and reward is not None else None

    legs = []
    for leg in _exit_legs(ctx):
        if leg.kind in ("stop_loss", "take_profit", "trailing_stop"):
            leg = _judge_leg(ctx, leg, target_plan)
            leg = replace(leg, kind=_exit_kind(leg))
            if leg.kind == "manual" and not leg.own:
                leg = replace(leg, how="被拉近的止盈单成交" if leg.code == "target_pulled_in"
                              else "中途挂的止盈单成交")
        legs.append(leg)
    realized_r = None
    exit_price = _vwap(ctx.exits)
    if risk is not None and ctx.avg_entry is not None and exit_price is not None:
        realized_r = ((exit_price - ctx.avg_entry) * ctx.sign / risk).quantize(_TWO)

    checks: dict[str, Check] = {}
    checks["sl_at_entry"] = _check_sl_at_entry(ctx, stops, targets)
    checks["exit_by_plan"] = _check_exit_by_plan(ctx, legs, planned_sl, planned_tp, stops, stop_plan)
    checks["sl_not_loosened"] = _check_sl_not_loosened(ctx, stops, stop_plan)
    checks["no_averaging_down"] = _check_no_averaging_down(ctx)
    checks["tp_at_entry"] = _check_tp_at_entry(ctx, targets, target_plan)
    checks["planned_rr"] = _check_planned_rr(ctx, planned_sl, planned_tp, planned_rr,
                                             stop_plan, target_plan)
    checks["early_exit_cost"] = _check_early_exit_cost(
        ctx, legs, checks["exit_by_plan"], planned_sl, planned_tp, planned_rr, realized_r, klines)
    checks["hold_time"], hold_seconds = _check_hold_time(ctx)

    return PrincipleReview(
        episode_id=ctx.episode_id,
        verdict=_verdict(ctx.inherited, ctx.is_open, checks),
        checks=checks,
        exits=tuple(ExitLeg(leg.time_ms, leg.qty, leg.price,
                            leg.kind if leg.kind in _KIND_CN else "unknown") for leg in legs),
        planned_sl=planned_sl, planned_tp=planned_tp, planned_rr=planned_rr,
        realized_r=realized_r, hold_seconds=hold_seconds,
    )


def _fallback(episode: Any, reason: str) -> PrincipleReview:
    detail = f"数据异常,无法评估({reason})"
    checks = {key: _check(key, "hard", CheckState.UNKNOWN, detail) for key in HARD_KEYS}
    checks.update({key: _check(key, "reference", CheckState.UNKNOWN, detail)
                   for key in REFERENCE_KEYS})
    ordered = {key: checks[key] for key in
               ("sl_at_entry", "exit_by_plan", "sl_not_loosened", "no_averaging_down") + REFERENCE_KEYS}
    try:
        inherited = bool(getattr(episode, "inherited_position", False))
        is_open = str(getattr(episode, "status", "closed") or "").lower() != "closed"
        episode_id = str(getattr(episode, "episode_id", "") or "")
    except Exception:  # noqa: BLE001 - 兜底路径本身不能再抛
        inherited, is_open, episode_id = False, False, ""
    return PrincipleReview(episode_id, _verdict(inherited, is_open, ordered), ordered, (),
                           None, None, None, None, None)


def review_episode(episode: TradeEpisode, orders: list[OrderRecord], klines_1m: list[Kline],
                   params: PrincipleParams | None = None) -> PrincipleReview:
    """按第一准则评估一笔交易。永不抛异常:数据残缺时对应检查为 UNKNOWN。"""
    params = params if isinstance(params, PrincipleParams) else PrincipleParams()
    try:
        return _review(episode, _as_list(orders), _as_list(klines_1m), params)
    except Exception as exc:  # noqa: BLE001 - 复盘不能因为一笔脏数据整体失败
        return _fallback(episode, str(exc) or type(exc).__name__)


def _as_list(value: Any) -> list[Any]:
    if value is None or isinstance(value, (str, bytes)):
        return []
    try:
        return [item for item in value if item is not None]
    except TypeError:
        return []


# --------------------------------------------------------------------------- 当日层面

def _attr(item: Any, name: str, default: Any = None) -> Any:
    """取字段;同时接受对象和 asdict() 之后的字典。"""
    if isinstance(item, dict):
        return item.get(name, default)
    return getattr(item, name, default)


def _day_rows(episodes: Any, window_start_ms: int | None) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for episode in _as_list(episodes):
        direction = str(getattr(episode, "direction", "") or "").lower()
        if direction not in ("long", "short"):
            continue
        closed_raw = getattr(episode, "closed_ms", None)
        closed = str(getattr(episode, "status", "") or "").lower() == "closed" and closed_raw is not None
        opened = _int(getattr(episode, "opened_ms", 0))
        closed_ms = _int(closed_raw) if closed else None
        before = window_start_ms is not None and opened < window_start_ms
        rows.append({
            "id": str(getattr(episode, "episode_id", "") or ""),
            "market": (str(getattr(episode, "category", "") or "").lower(),
                       str(getattr(episode, "symbol", "") or "").upper()),
            "symbol": str(getattr(episode, "symbol", "") or "").upper(),
            "direction": direction,
            "opened": opened,
            "closed": closed_ms,
            "inherited": bool(getattr(episode, "inherited_position", False)) or before,
            # 窗口开始之前就已平掉的回合:只用来对照"平仓后多久又开了仓",不算当日的交易
            "context": window_start_ms is not None and closed_ms is not None
                       and closed_ms < window_start_ms,
        })
    rows.sort(key=lambda row: (row["opened"], row["id"]))
    return rows


def _final_exit(review: Any) -> tuple[str | None, bool]:
    """(最后一笔离场的方式, 这笔是不是按计划的止盈离场)。"""
    exits = list(_attr(review, "exits") or [])
    if not exits:
        return None, False
    last = max(exits, key=lambda leg: _int(_attr(leg, "time_ms", 0)))
    kind = str(_attr(last, "kind", "") or "unknown")
    checks = _attr(review, "checks")
    state = _attr(checks.get("exit_by_plan"), "state") if isinstance(checks, dict) else None
    by_plan = kind == "take_profit" and state == CheckState.PASS
    return kind, by_plan


def _review_day(episodes: Any, reviews: Any, params: PrincipleParams,
                window_start_ms: int | None) -> list[Check]:
    rows = _day_rows(episodes, window_start_ms)
    today = [row for row in rows if not row["context"]]
    by_id = {str(_attr(review, "episode_id", "") or ""): review for review in _as_list(reviews)}
    side_cn = {"long": "多", "short": "空"}
    flip_ms = max(0, _int(params.flip_minutes)) * 60_000
    reentry_ms = max(0, _int(params.reentry_minutes)) * 60_000

    flips: list[str] = []
    after_target: list[str] = []
    revenge: list[str] = []
    unsure: list[str] = []
    for first in rows:
        if first["closed"] is None:
            continue
        for second in today:
            if second is first or second["inherited"] or second["market"] != first["market"]:
                continue
            gap = second["opened"] - first["closed"]
            if gap < 0:
                continue
            closed_at = _clock(first["closed"], params)
            opened_at = _clock(second["opened"], params, first["closed"])
            kind, by_plan = _final_exit(by_id.get(first["id"]))
            exit_word = _KIND_CN.get(kind or "", "离场方式不明")
            if second["direction"] != first["direction"] and gap <= flip_ms:
                text = (f"{first['symbol']} {closed_at} 平{side_cn[first['direction']]}({exit_word})"
                        f"→ {opened_at} 开{side_cn[second['direction']]},间隔 {_span(gap // 1000)}")
                # 准则说的反手是"踏空/被止损后转头开反向仓";止盈位到了之后的反向单不在其列。
                (after_target if by_plan else flips).append(text)
            if second["direction"] == first["direction"] and gap <= reentry_ms:
                text = (f"{first['symbol']} {closed_at} {side_cn[first['direction']]}单{exit_word}离场"
                        f"→ {opened_at} 同向重进,间隔 {_span(gap // 1000)}")
                if kind in ("stop_loss", "trailing_stop"):
                    revenge.append(text)
                elif kind in (None, "unknown"):
                    unsure.append(text)

    checks: list[Check] = []
    if not today:
        checks.append(_check("flip_after_exit", "day", CheckState.NA, "当日没有交易", Decimal("0")))
        checks.append(_check("revenge_reentry", "day", CheckState.NA, "当日没有交易", Decimal("0")))
    else:
        info = ""
        if after_target:
            info = "(止盈触发后的反向单不算反手:" + ";".join(after_target) + ")"
        if flips:
            checks.append(_check("flip_after_exit", "day", CheckState.FAIL, ";".join(flips) + info,
                                 Decimal(len(flips))))
        else:
            checks.append(_check("flip_after_exit", "day", CheckState.PASS,
                                 f"没有平仓后 {params.flip_minutes} 分钟内在同一品种反向开仓{info}",
                                 Decimal("0")))
        if revenge:
            checks.append(_check("revenge_reentry", "day", CheckState.FAIL, ";".join(revenge),
                                 Decimal(len(revenge))))
        elif unsure:
            checks.append(_check("revenge_reentry", "day", CheckState.UNKNOWN,
                                 "离场方式不明,无法确认是不是止损后重进:" + ";".join(unsure),
                                 Decimal("0")))
        else:
            checks.append(_check("revenge_reentry", "day", CheckState.PASS,
                                 f"没有止损后 {params.reentry_minutes} 分钟内同品种同方向重进", Decimal("0")))

    opened_today = [row for row in today if not row["inherited"]]
    carried = len(today) - len(opened_today)
    count = len(opened_today)
    tail = f",另有 {carried} 笔是之前留下的仓位" if carried else ""
    limit = params.max_trades_per_day
    if limit is None:
        checks.append(_check("trade_count", "day", CheckState.NA,
                             f"当日新开 {count} 笔{tail}(未设置每日笔数上限)", Decimal(count)))
    else:
        state = CheckState.PASS if count <= _int(limit) else CheckState.FAIL
        checks.append(_check("trade_count", "day", state,
                             f"当日新开 {count} 笔{tail},上限 {_int(limit)} 笔", Decimal(count)))
    return checks


def review_day(episodes: list[TradeEpisode], reviews: list[PrincipleReview],
               params: PrincipleParams | None = None, *,
               window_start_ms: int | None = None) -> list[Check]:
    """当日层面的检查(反手、止损后立刻重进、笔数)。永不抛异常。

    window_start_ms:当天的起点。给了之后,在它之前开的仓不算当日新开,也不会被算成反手/重进的
    后一笔;在它之前就平掉的回合只作为"前一笔"参与对照(跨零点的反手记在后一笔开仓的那一天)。
    """
    params = params if isinstance(params, PrincipleParams) else PrincipleParams()
    try:
        start = None if window_start_ms is None else int(window_start_ms)
        return _review_day(episodes, reviews, params, start)
    except Exception as exc:  # noqa: BLE001
        detail = f"数据异常,无法评估({str(exc) or type(exc).__name__})"
        return [_check(key, "day", CheckState.UNKNOWN, detail)
                for key in ("flip_after_exit", "revenge_reentry", "trade_count")]


def day_score(reviews: list[PrincipleReview]) -> dict:
    """当日合规统计。judged 只数已有明确结论的交易(不含继承仓位、持仓中、证据不足);
    most_broken 也只数这些交易里没过的硬检查。"""
    by_verdict = {verdict: 0 for verdict in VERDICTS}
    broken: dict[str, int] = {key: 0 for key in HARD_KEYS}
    total = 0
    for review in _as_list(reviews):
        verdict = _attr(review, "verdict")
        if not isinstance(verdict, str):
            continue
        total += 1
        by_verdict[verdict] = by_verdict.get(verdict, 0) + 1
        if verdict not in _JUDGED:
            continue
        checks = _attr(review, "checks")
        if not isinstance(checks, dict):
            continue
        for key in HARD_KEYS:
            if _attr(checks.get(key), "state") == CheckState.FAIL:
                broken[key] += 1
    worst = max(HARD_KEYS, key=lambda key: broken[key])   # 并列时按硬检查的固定顺序取第一个
    most_broken = None if broken[worst] == 0 else {
        "key": worst, "label": LABELS[worst], "count": broken[worst]}
    return {
        "total": total,
        "judged": sum(by_verdict.get(verdict, 0) for verdict in _JUDGED),
        "compliant": by_verdict.get("合规", 0),
        "by_verdict": by_verdict,
        "most_broken": most_broken,
    }
