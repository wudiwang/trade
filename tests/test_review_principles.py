"""第一准则复盘(app/review/principles.py)的测试。

订单与成交都是按 Bybit V5 文档语义手工构造的标准化记录(OrderRecord / Execution),不接触交易所。
时间基准 T0 = 曼谷时间 2026-11-17 10:00,所有 detail 里的 HH:MM 都能直接心算核对。
"""
import inspect
import json
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.review import principles
from app.review.episodes import reconstruct
from app.review.models import EpisodeFill, Execution, Kline, TradeEpisode
from app.review.principles import (HARD_KEYS, VERDICTS, Check, CheckState, ExitLeg,
                                   PrincipleParams, PrincipleReview, day_score, review_day,
                                   review_episode)

try:
    from app.review.models import OrderRecord
except ImportError:  # 契约类型尚未落地时的临时等价定义(仅测试辅助用)
    @dataclass(frozen=True)
    class OrderRecord:  # type: ignore[no-redef]
        exchange: str
        account_id: str
        order_id: str
        symbol: str
        side: str
        position_idx: int
        order_type: str
        qty: Decimal
        filled_qty: Decimal
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


T0 = int(datetime(2026, 11, 17, 3, 0, tzinfo=timezone.utc).timestamp() * 1000)   # 曼谷 10:00
MIN = 60_000
PASS, FAIL, UNKNOWN, NA = CheckState.PASS, CheckState.FAIL, CheckState.UNKNOWN, CheckState.NA


def D(value):
    return None if value is None else Decimal(str(value))


def at(minute=0, sec=0):
    return T0 + int(minute * MIN) + sec * 1000


def ex(exec_id, order_id, side, qty, price, minute, *, sec=0, idx=0, symbol="BTCUSDT"):
    return Execution(exec_id, order_id, "linear", symbol, side, D(qty), D(price),
                     D(qty) * D(price), Decimal("0.01"), at(minute, sec), position_idx=idx)


def order(order_id, side, *, origin="user", status="filled", order_type="Market", qty=1,
          price=None, trigger=None, reduce_only=False, close_position=False, sl=None, tp=None,
          created=0, created_sec=0, updated=None, idx=0, symbol="BTCUSDT"):
    created_ms = at(created, created_sec)
    return OrderRecord(
        exchange="bybit", account_id="acct", order_id=order_id, symbol=symbol, side=side,
        position_idx=idx, order_type=order_type, qty=D(qty), filled_qty=D(qty), price=D(price),
        avg_fill_price=None, trigger_price=D(trigger), reduce_only=reduce_only,
        close_position=close_position, status=status, origin=origin, attached_sl=D(sl),
        attached_tp=D(tp), created_ms=created_ms,
        updated_ms=created_ms if updated is None else at(updated),
    )


def trade(direction="long", *, entry=100, sl=95, tp=110, attach=True, exit_kind="stop_loss",
          exit_price=None, exit_min=60, start=0, idx=0, symbol="BTCUSDT", tag="t"):
    """一笔标准交易:进场单(可附带止损止盈)+ 系统生成的止损/止盈单 + 一种离场方式。"""
    open_side, close_side = ("Buy", "Sell") if direction == "long" else ("Sell", "Buy")
    step = 3 if direction == "long" else -3
    execs = [ex(f"{tag}-e", f"{tag}-entry", open_side, 1, entry, start, idx=idx, symbol=symbol)]
    orders = [order(f"{tag}-entry", open_side, created=start, idx=idx, symbol=symbol,
                    sl=sl if attach else None, tp=tp if attach else None)]
    end = start + exit_min
    closing = dict(reduce_only=True, close_position=True, idx=idx, symbol=symbol)
    sl_status = "filled" if exit_kind == "stop_loss" else ("untriggered" if exit_kind == "open" else "cancelled")
    tp_status = "filled" if exit_kind == "take_profit" else ("untriggered" if exit_kind == "open" else "cancelled")
    updated = None if exit_kind == "open" else end
    if sl is not None:
        orders.append(order(f"{tag}-sl", close_side, origin="stop_loss", status=sl_status,
                            trigger=sl, created=start, updated=updated, **closing))
    if tp is not None:
        orders.append(order(f"{tag}-tp", close_side, origin="take_profit", status=tp_status,
                            trigger=tp, created=start, updated=updated, **closing))
    if exit_kind == "stop_loss":
        execs.append(ex(f"{tag}-x", f"{tag}-sl", close_side, 1, exit_price or sl, end, idx=idx, symbol=symbol))
    elif exit_kind == "take_profit":
        execs.append(ex(f"{tag}-x", f"{tag}-tp", close_side, 1, exit_price or tp, end, idx=idx, symbol=symbol))
    elif exit_kind == "manual":
        execs.append(ex(f"{tag}-x", f"{tag}-close", close_side, 1, exit_price or entry + step, end,
                        idx=idx, symbol=symbol))
        orders.append(order(f"{tag}-close", close_side, created=end, reduce_only=True, idx=idx,
                            symbol=symbol))
    return execs, orders


def one(execs):
    episodes = reconstruct(execs)
    assert len(episodes) == 1
    return episodes[0]


def review(execs, orders, klines=(), **params):
    return review_episode(one(execs), list(orders), list(klines), PrincipleParams(**params))


def swap(orders, order_id, **changes):
    assert any(row.order_id == order_id for row in orders)
    return [replace(row, **changes) if row.order_id == order_id else row for row in orders]


def drop(orders, *order_ids):
    return [row for row in orders if row.order_id not in order_ids]


def bar(minute, high, low):
    return Kline(at(minute), D((high + low) / 2), D(high), D(low), D((high + low) / 2),
                 Decimal("1"), Decimal("1"))


def bars(first, last, high, low):
    """从 first 到 last(含)每分钟一根、首尾相接的 1m K线。"""
    return [bar(minute, high, low) for minute in range(first, last + 1)]


def seen_at_creation(orders, *order_ids):
    """加一份"刚挂出时"的订单快照(更新时间 = 创建时间):这才是挂出价的记录。"""
    extra = []
    for order_id in order_ids:
        row = next(item for item in orders if item.order_id == order_id)
        status = "untriggered" if row.trigger_price is not None else "new"
        extra.append(replace(row, status=status, updated_ms=row.created_ms))
    return extra + list(orders)


# ----------------------------------------------------------------------------- 基线

def test_clean_stop_loss_trade_is_compliant_and_details_quote_times_and_prices():
    result = review(*trade())
    assert result.verdict == "合规"
    assert [result.checks[key].state for key in HARD_KEYS] == [PASS, PASS, PASS, PASS]
    assert all(result.checks[key].tier == "hard" for key in HARD_KEYS)
    assert result.planned_sl == Decimal("95") and result.planned_tp == Decimal("110")
    assert result.planned_rr == Decimal("2.00")
    assert result.realized_r == Decimal("-1.00")
    assert result.hold_seconds == 3600
    assert result.exits == (ExitLeg(at(60), Decimal("1"), Decimal("95"), "stop_loss"),)
    entry_detail = result.checks["sl_at_entry"].detail
    assert "10:00" in entry_detail and "95" in entry_detail and "100" in entry_detail
    assert "11:00" in result.checks["exit_by_plan"].detail


def test_take_profit_trade_is_compliant():
    result = review(*trade(exit_kind="take_profit"))
    assert result.verdict == "合规"
    assert result.exits[0].kind == "take_profit"
    assert result.realized_r == Decimal("2.00")


def test_short_trade_mirrors_long():
    result = review(*trade("short", sl=105, tp=85, exit_kind="take_profit"))
    assert result.verdict == "合规"
    assert result.planned_rr == Decimal("3.00")
    assert result.realized_r == Decimal("3.00")


def test_review_is_serialisable_with_asdict():
    result = review(*trade(exit_kind="manual"))
    payload = json.loads(json.dumps(asdict(result), default=str, ensure_ascii=False))
    assert payload["verdict"] == "中途下车"
    assert payload["checks"]["exit_by_plan"]["state"] == "fail"
    assert payload["exits"][0]["kind"] == "manual"
    assert set(payload["checks"]) == set(HARD_KEYS) | {"tp_at_entry", "planned_rr",
                                                       "early_exit_cost", "hold_time"}


# ----------------------------------------------------------------------------- sl_at_entry

def test_sl_at_entry_pass_with_stop_order_set_inside_grace():
    execs, orders = trade(attach=False)
    orders = swap(orders, "t-sl", created_ms=at(0, 45))
    check = review(execs, orders).checks["sl_at_entry"]
    assert check.state is PASS and check.value == "on_time"
    assert "45 秒" in check.detail and "95" in check.detail


def test_sl_at_entry_fail_late_says_how_late_and_gives_no_planned_stop():
    execs, orders = trade(attach=False)
    orders = swap(orders, "t-sl", created_ms=at(10))
    result = review(execs, orders)
    check = result.checks["sl_at_entry"]
    assert check.state is FAIL and check.value == "late"
    assert "10:10" in check.detail and "10 分钟" in check.detail and "120 秒" in check.detail
    assert result.verdict == "无止损"
    assert result.planned_sl is None and result.realized_r is None


def test_sl_at_entry_grace_is_configurable():
    execs, orders = trade(attach=False)
    orders = swap(orders, "t-sl", created_ms=at(10))
    assert review(execs, orders, sl_grace_seconds=900).checks["sl_at_entry"].state is PASS


def test_sl_at_entry_fail_when_no_stop_ever_existed():
    result = review(*trade(sl=None, exit_kind="manual"))
    check = result.checks["sl_at_entry"]
    assert check.state is FAIL and check.value == "none"
    assert "10:00" in check.detail and "11:00" in check.detail
    assert result.checks["sl_not_loosened"].state is NA
    assert result.verdict == "无止损"


def test_sl_at_entry_unknown_when_order_history_does_not_cover_the_trade():
    execs, _ = trade(exit_kind="manual")
    result = review(execs, [])
    assert result.checks["sl_at_entry"].state is UNKNOWN
    assert result.checks["exit_by_plan"].state is UNKNOWN
    assert result.checks["tp_at_entry"].state is UNKNOWN
    assert result.verdict == "证据不足"


def test_sl_at_entry_unknown_when_only_part_of_the_orders_are_present():
    execs, orders = trade(sl=None, exit_kind="manual")
    result = review(execs, drop(orders, "t-close"))
    assert result.checks["sl_at_entry"].state is UNKNOWN


def test_sl_at_entry_unknown_when_cancelled_stops_have_aged_out_of_history():
    execs, orders = trade(sl=None, exit_kind="manual")
    late_sync = review(execs, orders, orders_synced_ms=at(60) + 48 * 3_600_000)
    assert late_sync.checks["sl_at_entry"].state is UNKNOWN
    assert late_sync.verdict == "中途下车"            # 手动离场是确定的;止损有无才是未知
    same_day = review(execs, orders, orders_synced_ms=at(60) + 3_600_000)
    assert same_day.checks["sl_at_entry"].state is FAIL


def test_rejected_stop_order_does_not_count_as_a_stop():
    execs, orders = trade(attach=False, exit_kind="manual")
    orders = swap(orders, "t-sl", status="rejected")
    assert review(execs, orders).checks["sl_at_entry"].state is FAIL


def test_stop_on_another_symbol_or_position_index_is_not_borrowed():
    execs, orders = trade(attach=False, exit_kind="manual")
    other_symbol = swap(orders, "t-sl", symbol="ETHUSDT")
    other_index = swap(orders, "t-sl", position_idx=2)
    assert review(execs, other_symbol).checks["sl_at_entry"].state is FAIL
    assert review(execs, other_index).checks["sl_at_entry"].state is FAIL


def test_user_conditional_reduce_only_order_on_loss_side_counts_as_stop():
    execs, orders = trade(sl=None, tp=None, exit_kind="open")
    execs.append(ex("t-x", "t-mystop", "Sell", 1, 96, 40))
    orders.append(order("t-mystop", "Sell", origin="user", trigger=96, reduce_only=True,
                        created=0, created_sec=20, updated=40))
    result = review(execs, orders)
    assert result.checks["sl_at_entry"].state is PASS
    assert result.exits[0].kind == "stop_loss"
    assert result.checks["exit_by_plan"].state is PASS
    assert result.planned_sl is None                    # 只有成交时的那一行:挂出价没有记录
    assert "最后记录的价位 96" in result.checks["sl_at_entry"].detail
    pinned = review(execs, seen_at_creation(orders, "t-mystop"))
    assert pinned.planned_sl == Decimal("96") and pinned.verdict == "合规"


def test_trailing_stop_alone_counts_as_a_stop_but_its_price_is_unknown():
    execs, orders = trade(sl=None, tp=None, exit_kind="open")
    execs.append(ex("t-x", "t-trail", "Sell", 1, 104, 50))
    orders.append(order("t-trail", "Sell", origin="trailing_stop", reduce_only=True,
                        close_position=True, created=0, created_sec=30, updated=50))
    result = review(execs, orders)
    assert result.checks["sl_at_entry"].state is PASS
    assert "追踪止损" in result.checks["sl_at_entry"].detail
    assert result.checks["sl_not_loosened"].state is UNKNOWN
    assert result.checks["exit_by_plan"].state is PASS
    assert result.exits[0].kind == "trailing_stop"
    assert result.planned_sl is None
    assert result.verdict == "证据不足"


# ----------------------------------------------------------------------------- exit_by_plan

def test_manual_exit_is_mid_trade_exit_and_detail_shows_plan():
    result = review(*trade(exit_kind="manual", exit_price=103, exit_min=30))
    check = result.checks["exit_by_plan"]
    assert check.state is FAIL and check.value == "manual"
    assert result.verdict == "中途下车"
    for expected in ("10:30", "103", "100%", "计划止损 95", "止盈 110", "市价手动平仓"):
        assert expected in check.detail
    assert result.realized_r == Decimal("0.60")


def test_partial_manual_exit_then_take_profit_reports_both_legs():
    execs, orders = trade(exit_kind="open")
    execs += [ex("t-x1", "t-close", "Sell", "0.4", 103, 30), ex("t-x2", "t-tp", "Sell", "0.6", 110, 60)]
    orders = swap(orders, "t-tp", status="filled", updated_ms=at(60))
    orders = swap(orders, "t-sl", status="cancelled", updated_ms=at(60))
    orders.append(order("t-close", "Sell", created=30, reduce_only=True, qty="0.4"))
    result = review(execs, orders)
    assert [(leg.kind, leg.qty) for leg in result.exits] == [
        ("manual", Decimal("0.4")), ("take_profit", Decimal("0.6"))]
    check = result.checks["exit_by_plan"]
    assert check.state is FAIL and "40%" in check.detail and "60%" in check.detail
    assert result.verdict == "中途下车"
    assert result.realized_r == Decimal("1.44")      # (0.4*3 + 0.6*10) / 5


def test_fills_of_one_closing_order_merge_into_one_leg():
    execs, orders = trade(exit_kind="open")
    execs += [ex("t-x1", "t-sl", "Sell", "0.3", 95, 60), ex("t-x2", "t-sl", "Sell", "0.7", "94.9", 60, sec=1)]
    orders = swap(orders, "t-sl", status="filled", updated_ms=at(60, 1))
    result = review(execs, orders)
    assert len(result.exits) == 1
    assert result.exits[0].qty == Decimal("1") and result.exits[0].price == Decimal("94.93")
    assert result.checks["exit_by_plan"].state is PASS


def test_reduce_only_limit_placed_at_entry_is_the_target():
    execs, orders = trade(tp=None, exit_kind="open")
    execs.append(ex("t-x", "t-limit", "Sell", 1, 110, 60))
    orders = swap(orders, "t-sl", status="cancelled", updated_ms=at(60))
    orders.append(order("t-limit", "Sell", order_type="Limit", price=110, reduce_only=True,
                        created=0, created_sec=40, updated=60))
    result = review(execs, seen_at_creation(orders, "t-limit"))
    assert result.exits[0].kind == "take_profit"
    assert result.checks["exit_by_plan"].state is PASS
    assert result.checks["tp_at_entry"].state is PASS
    assert result.planned_tp == Decimal("110")
    assert result.verdict == "合规"
    # 只有成交时的那一行:110 是不是挂出时的价位没有记录,不给"合规"
    blind = review(execs, orders)
    assert blind.planned_tp is None and blind.checks["tp_at_entry"].state is PASS
    assert blind.checks["exit_by_plan"].state is UNKNOWN and blind.verdict == "证据不足"


def test_reduce_only_limit_placed_mid_trade_short_of_the_target_is_a_manual_exit():
    execs, orders = trade(exit_kind="open")              # 进场单带止损 95、止盈 110
    execs.append(ex("t-x", "t-limit", "Sell", 1, 104, 60))
    orders = swap(orders, "t-sl", status="cancelled", updated_ms=at(60))
    orders = swap(orders, "t-tp", status="cancelled", updated_ms=at(60))
    orders.append(order("t-limit", "Sell", order_type="Limit", price=104, reduce_only=True,
                        created=30, updated=60))
    result = review(execs, orders)
    check = result.checks["exit_by_plan"]
    assert result.exits[0].kind == "manual"
    assert check.state is FAIL and check.value == "target_pulled_in"
    assert "10:30 挂的限价平仓单" in check.detail and "110" in check.detail
    assert result.verdict == "中途下车"


def test_limit_without_reduce_only_is_not_treated_as_target():
    execs, orders = trade(tp=None, exit_kind="open")
    execs.append(ex("t-x", "t-limit", "Sell", 1, 110, 60))
    orders.append(order("t-limit", "Sell", order_type="Limit", price=110, created=0, updated=60))
    assert review(execs, orders).exits[0].kind == "manual"


def test_liquidation_fails_with_its_own_wording():
    execs, orders = trade(exit_kind="manual", exit_price=90)
    orders = swap(orders, "t-close", origin="liquidation")
    result = review(execs, orders)
    check = result.checks["exit_by_plan"]
    assert check.state is FAIL and check.value == "liquidation" and "强平" in check.detail
    assert result.exits[0].kind == "liquidation"
    assert result.verdict == "中途下车"


def test_adl_is_not_the_traders_action_and_is_not_judged():
    execs, orders = trade("short", sl=105, tp=90, exit_kind="manual", exit_price=92)
    orders = swap(orders, "t-close", origin="adl")
    result = review(execs, orders)
    check = result.checks["exit_by_plan"]
    assert check.state is UNKNOWN and check.value == "adl"
    assert "ADL" in check.detail and "不属于本人操作" in check.detail
    assert result.exits[0].kind == "adl"
    assert result.verdict == "证据不足"
    assert day_score([result])["most_broken"] is None
    assert result.checks["early_exit_cost"].state is NA


def test_trailing_stop_added_mid_trade_is_a_change_of_plan():
    execs, orders = trade(exit_kind="open")
    execs.append(ex("t-x", "t-trail", "Sell", 1, 104, 60))
    orders = swap(orders, "t-sl", status="cancelled", updated_ms=at(60))
    orders.append(order("t-trail", "Sell", origin="trailing_stop", reduce_only=True,
                        close_position=True, created=30, updated=60))
    result = review(execs, orders)
    check = result.checks["exit_by_plan"]
    assert check.state is FAIL and check.value == "late_trailing"
    assert "30 分钟" in check.detail
    assert result.exits[0].kind == "trailing_stop"
    assert result.verdict == "中途下车"


def test_trailing_stop_set_at_entry_is_a_planned_stop():
    execs, orders = trade(exit_kind="open")
    execs.append(ex("t-x", "t-trail", "Sell", 1, 104, 60))
    orders = swap(orders, "t-sl", status="cancelled", updated_ms=at(60))
    orders.append(order("t-trail", "Sell", origin="trailing_stop", reduce_only=True,
                        close_position=True, created=0, created_sec=60, updated=60))
    result = review(execs, orders)
    assert result.checks["exit_by_plan"].state is PASS
    assert result.verdict == "合规"


def test_take_profit_dragged_closer_and_filled_is_a_mid_trade_exit():
    """进场时止盈 110,持仓中把止盈线拖到 104 成交:用止盈按钮完成的中途下车。"""
    execs, orders = trade(exit_kind="take_profit", exit_price=104)
    orders = swap(orders, "t-tp", trigger_price=Decimal("104"))
    result = review(execs, orders)
    check = result.checks["exit_by_plan"]
    assert check.state is FAIL and check.value == "target_pulled_in"
    for expected in ("104", "110", "拉近", "没有等到目标位"):
        assert expected in check.detail
    assert result.planned_tp == Decimal("110") and result.planned_rr == Decimal("2.00")
    assert result.exits[0].kind == "manual"
    assert result.verdict == "中途下车"
    assert review(*trade(exit_kind="take_profit")).checks["exit_by_plan"].value == "planned"


def test_take_profit_added_mid_trade_and_held_passes_with_a_note():
    execs, orders = trade(attach=False, exit_kind="take_profit", exit_price=104)
    orders = swap(orders, "t-tp", trigger_price=Decimal("104"), created_ms=at(40))
    orders = seen_at_creation(orders, "t-sl", "t-tp")
    result = review(execs, orders)
    check = result.checks["exit_by_plan"]
    assert check.state is PASS and check.value == "plan_changed" and "10:40" in check.detail
    assert result.checks["tp_at_entry"].state is FAIL
    assert result.exits[0].kind == "take_profit" and result.verdict == "合规"


def test_exit_fill_without_order_record_is_unknown():
    execs, orders = trade()
    result = review(execs, drop(orders, "t-sl"))
    assert result.exits[0].kind == "unknown"
    assert result.checks["exit_by_plan"].state is UNKNOWN
    assert result.verdict == "证据不足"


def test_exit_fill_without_order_record_falls_back_to_the_fill_origin():
    if "origin" not in Execution.__dataclass_fields__:
        pytest.skip("Execution.origin 尚未加入 models")
    execs, orders = trade()
    execs[-1] = replace(execs[-1], origin="stop_loss")
    result = review(execs, drop(orders, "t-sl"))
    assert result.exits[0].kind == "stop_loss"
    assert result.checks["exit_by_plan"].state is PASS


def test_system_origin_exit_is_unknown_not_manual():
    execs, orders = trade(exit_kind="manual")
    orders = swap(orders, "t-close", origin="system")
    result = review(execs, orders)
    assert result.exits[0].kind == "unknown"
    assert result.checks["exit_by_plan"].state is UNKNOWN


def test_open_position_has_no_exit_judgement():
    result = review(*trade(exit_kind="open"))
    assert result.verdict == "持仓中"
    assert result.checks["exit_by_plan"].state is NA
    assert result.exits == () and result.hold_seconds is None
    assert result.checks["sl_at_entry"].state is PASS
    assert result.checks["sl_not_loosened"].state is PASS     # 进场单附带值 + 仍有效的止损单现值


def test_open_position_whose_stop_was_cancelled_is_flagged():
    execs, orders = trade(exit_kind="open")
    orders = swap(orders, "t-sl", status="cancelled", updated_ms=at(20))
    result = review(execs, orders, orders_synced_ms=at(90))
    check = result.checks["sl_not_loosened"]
    assert check.state is FAIL and check.value == "removed" and "10:20" in check.detail
    assert result.verdict == "持仓中"


def test_open_position_with_a_widened_live_stop_is_flagged():
    execs, orders = trade(exit_kind="open")
    orders = swap(orders, "t-sl", trigger_price=Decimal("90"), updated_ms=at(45))
    assert review(execs, orders).checks["sl_not_loosened"].state is FAIL


# ----------------------------------------------------------------------------- sl_not_loosened

def test_stop_moved_further_from_entry_fails_long():
    execs, orders = trade(exit_price=93)
    orders = swap(orders, "t-sl", trigger_price=Decimal("93"))
    result = review(execs, orders)
    check = result.checks["sl_not_loosened"]
    assert check.state is FAIL and check.value == Decimal("93")
    for expected in ("95", "93", "10:00", "11:00", "更远"):
        assert expected in check.detail
    assert result.verdict == "止损被放宽"
    assert result.planned_sl == Decimal("95")          # 计划值仍是最初那个
    assert result.realized_r == Decimal("-1.40")


def test_stop_moved_further_from_entry_fails_short():
    execs, orders = trade("short", sl=105, tp=90, exit_price=107)
    orders = swap(orders, "t-sl", trigger_price=Decimal("107"))
    result = review(execs, orders)
    assert result.checks["sl_not_loosened"].state is FAIL
    assert result.verdict == "止损被放宽"


def test_stop_moved_closer_is_not_a_failure_but_is_noted():
    execs, orders = trade(exit_price=99)
    orders = swap(orders, "t-sl", trigger_price=Decimal("99"))
    result = review(execs, orders)
    check = result.checks["sl_not_loosened"]
    assert check.state is PASS and "收紧" in check.detail and "99" in check.detail
    assert "收紧" in result.checks["exit_by_plan"].detail
    assert result.verdict == "合规"


def test_single_stop_snapshot_cannot_show_earlier_values():
    execs, orders = trade(attach=False)
    result = review(execs, orders)
    check = result.checks["sl_not_loosened"]
    assert result.checks["sl_at_entry"].state is PASS
    assert check.state is UNKNOWN and "95" in check.detail and "11:00" in check.detail
    assert result.verdict == "证据不足"
    trusting = review(execs, orders, assume_single_stop_snapshot_unchanged=True)
    assert trusting.checks["sl_not_loosened"].state is PASS
    assert trusting.verdict == "合规"


def test_attached_stop_without_any_later_record_is_unknown():
    execs, orders = trade(exit_kind="manual")
    result = review(execs, drop(orders, "t-sl"))
    assert result.checks["sl_at_entry"].state is PASS
    assert result.checks["sl_not_loosened"].state is UNKNOWN


def test_snapshots_of_the_same_stop_order_reveal_an_in_place_move():
    execs, orders = trade(attach=False, exit_price=93)
    final = next(row for row in orders if row.order_id == "t-sl")
    early = replace(final, status="untriggered", updated_ms=final.created_ms)
    moved = replace(final, trigger_price=Decimal("93"))
    assert review(execs, drop(orders, "t-sl") + [moved, early]).checks["sl_not_loosened"].state is FAIL
    unchanged = review(execs, drop(orders, "t-sl") + [early, final])
    assert unchanged.checks["sl_not_loosened"].state is PASS
    assert unchanged.verdict == "合规"


def test_cancel_and_replace_with_a_wider_stop_fails():
    execs, orders = trade(attach=False, exit_kind="open")
    orders = swap(orders, "t-sl", status="cancelled", updated_ms=at(20))
    orders.append(order("t-sl2", "Sell", origin="stop_loss", trigger=92, reduce_only=True,
                        close_position=True, created=20, updated=60))
    execs.append(ex("t-x", "t-sl2", "Sell", 1, 92, 60))
    result = review(execs, seen_at_creation(orders, "t-sl"))
    check = result.checks["sl_not_loosened"]
    assert check.state is FAIL and "92" in check.detail
    assert result.verdict == "止损被放宽"
    # 第一张止损单只有被撤时的那一行:95 可能是它收紧后的价位,不能据此说"比最初更远"
    blind = review(execs, orders).checks["sl_not_loosened"]
    assert blind.state is UNKNOWN and "95" in blind.detail and "92" in blind.detail


def test_cancel_and_replace_at_the_same_price_passes():
    execs, orders = trade(attach=False, exit_kind="open")
    orders = swap(orders, "t-sl", status="cancelled", updated_ms=at(20))
    orders.append(order("t-sl2", "Sell", origin="stop_loss", trigger=95, reduce_only=True,
                        close_position=True, created=20, updated=60))
    execs.append(ex("t-x", "t-sl2", "Sell", 1, 95, 60))
    assert review(execs, seen_at_creation(orders, "t-sl", "t-sl2")).checks["sl_not_loosened"].state is PASS
    assert review(execs, orders).checks["sl_not_loosened"].state is UNKNOWN
    assert review(execs, seen_at_creation(orders, "t-sl")).checks["sl_not_loosened"].state is UNKNOWN


def test_stop_cancelled_while_position_stays_open_counts_as_loosened():
    execs, orders = trade(exit_kind="manual")
    orders = swap(orders, "t-sl", updated_ms=at(20))
    result = review(execs, orders)
    check = result.checks["sl_not_loosened"]
    assert check.state is FAIL and check.value == "removed"
    assert "10:20" in check.detail and "40 分钟" in check.detail
    assert result.verdict == "止损被放宽"


def test_stop_cancelled_by_the_close_itself_is_not_a_removal():
    result = review(*trade(exit_kind="manual"))
    assert result.checks["sl_not_loosened"].state is PASS


def test_two_partial_stops_set_at_entry_are_both_the_original_plan():
    execs, orders = trade(attach=False, exit_kind="open")
    first = next(row for row in orders if row.order_id == "t-sl")
    orders = drop(orders, "t-sl") + [
        replace(first, status="untriggered", updated_ms=at(0, 10), created_ms=at(0, 10)),
        replace(first, order_id="t-sl-b", trigger_price=Decimal("93"), status="untriggered",
                created_ms=at(0, 40), updated_ms=at(0, 40)),
        replace(first, status="filled", updated_ms=at(60), created_ms=at(0, 10)),
        replace(first, order_id="t-sl-b", trigger_price=Decimal("93"), status="cancelled",
                created_ms=at(0, 40), updated_ms=at(60)),
    ]
    execs.append(ex("t-x", "t-sl", "Sell", 1, 95, 60))
    assert review(execs, orders).checks["sl_not_loosened"].state is PASS


def test_trade_that_ends_inside_the_grace_period_cannot_have_been_loosened_later():
    execs, orders = trade(attach=False, exit_min=1)
    assert review(execs, orders).checks["sl_not_loosened"].state is PASS


# ----------------------------------------------------------------------------- no_averaging_down

def add_fill(execs, orders, side, price, minute, *, sec=0, order_id="t-add", qty=1):
    execs.insert(-1, ex(f"{order_id}-f{len(execs)}", order_id, side, qty, price, minute, sec=sec))
    if not any(row.order_id == order_id for row in orders):
        orders.append(order(order_id, side, created=minute, created_sec=sec))


def with_adds(direction, adds, **kwargs):
    execs, orders = trade(direction, **kwargs)
    open_side, close_side = ("Buy", "Sell") if direction == "long" else ("Sell", "Buy")
    total = Decimal("1")
    for index, (price, minute, sec) in enumerate(adds):
        add_fill(execs, orders, open_side, price, minute, sec=sec, order_id=f"t-add{index}")
        total += 1
    execs[-1] = replace(execs[-1], qty=total, value=total * execs[-1].price)
    return execs, orders


def test_adding_below_average_on_a_long_is_averaging_down():
    result = review(*with_adds("long", [(98, 10, 0)]))
    check = result.checks["no_averaging_down"]
    assert check.state is FAIL
    for expected in ("10:10", "98", "均价 100", "浮亏"):
        assert expected in check.detail
    assert result.verdict == "逆向加仓"


def test_adding_above_average_on_a_short_is_averaging_down():
    result = review(*with_adds("short", [(102, 10, 0)], sl=105, tp=90))
    assert result.checks["no_averaging_down"].state is FAIL
    assert result.verdict == "逆向加仓"


def test_adding_in_profit_is_not_averaging_down():
    result = review(*with_adds("long", [(103, 10, 0)], exit_kind="take_profit"))
    check = result.checks["no_averaging_down"]
    assert check.state is PASS and check.value == Decimal("1")
    assert result.verdict == "合规"


def test_running_average_not_first_entry_decides_under_water():
    # 100 → 加 104(均价 102)→ 再加 101:高于首次进场价,但低于当时均价 102 = 浮亏中加仓
    result = review(*with_adds("long", [(104, 10, 0), (101, 20, 0)], exit_kind="take_profit"))
    check = result.checks["no_averaging_down"]
    assert check.state is FAIL and "均价 102" in check.detail and "10:20" in check.detail


def test_multi_fill_entry_order_is_one_entry_not_an_add():
    execs, orders = trade()
    execs.insert(1, ex("t-e2", "t-entry", "Buy", 1, "99.5", 5))      # 同一张进场单 5 分钟后补成交
    execs[-1] = replace(execs[-1], qty=Decimal("2"))
    result = review(execs, orders)
    check = result.checks["no_averaging_down"]
    assert check.state is PASS and "没有再加仓" in check.detail
    assert result.planned_rr == Decimal("2.16")                      # 进场均价 99.75
    assert result.verdict == "合规"


def test_second_order_inside_entry_window_is_part_of_the_entry():
    inside = review(*with_adds("long", [(99, 0, 30)]))
    assert inside.checks["no_averaging_down"].state is PASS
    outside = review(*with_adds("long", [(99, 3, 0)]))
    assert outside.checks["no_averaging_down"].state is FAIL
    widened = review(*with_adds("long", [(99, 3, 0)]), entry_window_seconds=600)
    assert widened.checks["no_averaging_down"].state is PASS


def test_one_adding_order_filled_in_pieces_is_judged_once_against_the_prior_average():
    execs, orders = trade(exit_kind="take_profit")
    add_fill(execs, orders, "Buy", 104, 10, order_id="t-add")
    add_fill(execs, orders, "Buy", "101.5", 10, sec=1, order_id="t-add")   # 低于新均价 102,但同一张单
    execs[-1] = replace(execs[-1], qty=Decimal("3"))
    assert review(execs, orders).checks["no_averaging_down"].state is PASS


# ----------------------------------------------------------------------------- 参考项

def test_tp_at_entry_states():
    assert review(*trade()).checks["tp_at_entry"].state is PASS
    missing = review(*trade(tp=None))
    assert missing.checks["tp_at_entry"].state is FAIL
    assert missing.checks["tp_at_entry"].tier == "reference"
    assert missing.verdict == "合规"                     # 参考项不改变结论
    execs, orders = trade(attach=False)
    orders = swap(orders, "t-sl", created_ms=at(0, 5))
    late = review(execs, swap(orders, "t-tp", created_ms=at(15)))
    assert late.checks["tp_at_entry"].state is FAIL and "10:15" in late.checks["tp_at_entry"].detail
    assert late.planned_tp is None


def test_planned_rr_states():
    good = review(*trade())
    assert good.checks["planned_rr"].state is PASS and good.checks["planned_rr"].value == Decimal("2.00")
    thin = review(*trade(tp=105))
    assert thin.checks["planned_rr"].state is FAIL and thin.planned_rr == Decimal("1.00")
    assert thin.verdict == "合规"                        # 盈亏比不足只是参考
    assert review(*trade(tp=105), min_rr=Decimal("1")).checks["planned_rr"].state is PASS
    none = review(*trade(tp=None))
    assert none.checks["planned_rr"].state is NA and none.planned_rr is None
    wrong_side = review(*trade(sl=101, exit_price=101))
    assert wrong_side.checks["planned_rr"].state is UNKNOWN and wrong_side.planned_rr is None


def manual_exit_case(klines, **params):
    execs, orders = trade(exit_kind="manual", exit_price=103, exit_min=30)
    execs[-1] = replace(execs[-1], timestamp_ms=at(30, 20))
    return review(execs, orders, klines, **params).checks["early_exit_cost"]


def test_early_exit_cost_target_touched_first():
    check = manual_exit_case(bars(31, 94, 106, 102) + [bar(95, 110.5, 108)])
    assert check.state is NA and check.value == "target_first" and check.tier == "reference"
    for expected in ("10:30", "11:35", "110", "+2R", "+0.6R", "不改变本笔结论"):
        assert expected in check.detail


def test_early_exit_cost_stop_touched_first():
    check = manual_exit_case(bars(31, 49, 104, 102) + [bar(50, 99, 94.5)] + bars(51, 95, 111, 108))
    assert check.value == "stop_first"
    assert "10:50" in check.detail and "95" in check.detail
    # 提前走了、之后价格又到了原止损:只陈述事实,不写"比按计划多赚了多少"
    assert "+0.6R" not in check.detail and "-1R" not in check.detail
    assert "不改变本笔结论" in check.detail


def test_early_exit_cost_both_inside_one_bar_is_not_decided():
    check = manual_exit_case([bar(31, 111, 94)])
    assert check.state is UNKNOWN and check.value == "same_bar" and "同时触及" in check.detail


def test_early_exit_cost_neither_within_horizon():
    bars = [bar(31 + i, 104, 102) for i in range(2 * 60)]
    check = manual_exit_case(bars, horizon_hours=2)
    assert check.state is NA and check.value == "neither" and "2 小时内" in check.detail
    beyond = manual_exit_case(bars + [bar(31 + 2 * 60 + 5, 111, 108)], horizon_hours=2)
    assert beyond.value == "neither"                     # 超出观察期的触及不算


def test_early_exit_cost_incomplete_klines_say_so():
    check = manual_exit_case([bar(31, 104, 102), bar(32, 104, 102)])
    assert check.state is UNKNOWN and check.value == "incomplete" and "10:33" in check.detail
    assert manual_exit_case([]).value == "no_data"


def test_early_exit_cost_exit_minute_touching_a_level_is_ambiguous():
    check = manual_exit_case([bar(30, 111, 102), bar(31, 104, 102)])
    assert check.state is UNKNOWN and check.value == "exit_minute" and "10:30" in check.detail
    quiet = manual_exit_case([bar(30, 104, 102), bar(31, 104, 102)])
    assert quiet.value == "incomplete"                  # 离场那一分钟没碰到任何一条线:照常往后看


def test_early_exit_cost_stops_at_the_first_hole_in_the_klines():
    """离场后 20 根,然后缺了一大段,再出现的 K 线已经在 110:不能跨缺口说"先到原止盈"。"""
    holed = bars(31, 50, 104, 102) + bars(2400, 2410, 111, 109)
    check = manual_exit_case(holed)
    assert check.state is UNKNOWN and check.value == "incomplete"
    assert "10:51" in check.detail and "缺口" in check.detail
    missing_first = manual_exit_case(bars(33, 60, 111, 109))
    assert missing_first.value == "no_data"             # 离场后的第一根就缺


def test_early_exit_cost_only_applies_to_manual_exits_with_a_full_plan():
    assert review(*trade(), [bar(61, 111, 90)]).checks["early_exit_cost"].state is NA
    no_target = review(*trade(tp=None, exit_kind="manual"), [bar(61, 111, 90)])
    assert no_target.checks["early_exit_cost"].state is NA


def test_early_exit_cost_short_direction():
    execs, orders = trade("short", sl=105, tp=90, exit_kind="manual", exit_price=98, exit_min=30)
    check = review(execs, orders, bars(30, 39, 99, 97) + [bar(40, 96, 89.5)]).checks["early_exit_cost"]
    assert check.value == "target_first" and "+2R" in check.detail and "+0.4R" in check.detail


def test_hold_time_reports_span():
    result = review(*trade(exit_min=192))
    check = result.checks["hold_time"]
    assert result.hold_seconds == 192 * 60 and check.value == Decimal(192 * 60)
    assert "10:00" in check.detail and "13:12" in check.detail and "3 小时 12 分" in check.detail


def test_times_on_another_day_carry_the_date():
    result = review(*trade(exit_min=16 * 60))            # 次日 02:00 离场
    assert "11-18 02:00" in result.checks["hold_time"].detail


# ----------------------------------------------------------------------------- 结论优先级

def messy_trade(*, stop=True, loosen=False, average=False, manual=True):
    execs, orders = trade(sl=95 if stop else None, exit_kind="manual" if manual else "stop_loss",
                          exit_price=97 if manual else None)
    if loosen:
        orders = swap(orders, "t-sl", trigger_price=Decimal("90"))
    if average:
        add_fill(execs, orders, "Buy", 98, 10)
        execs[-1] = replace(execs[-1], qty=Decimal("2"))
    return execs, orders


@pytest.mark.parametrize("kwargs, verdict", [
    (dict(stop=False, average=True, manual=True), "无止损"),
    (dict(loosen=True, average=True, manual=True), "止损被放宽"),
    (dict(average=True, manual=True), "逆向加仓"),
    (dict(manual=True), "中途下车"),
    (dict(manual=False), "合规"),
])
def test_verdict_precedence_on_real_trades(kwargs, verdict):
    assert review(*messy_trade(**kwargs)).verdict == verdict


def test_verdict_precedence_exhaustive_on_check_states():
    def checks(**states):
        return {key: Check(key, key, "hard", states.get(key, PASS)) for key in HARD_KEYS}

    verdict = principles._verdict
    everything_failed = checks(**{key: FAIL for key in HARD_KEYS})
    assert verdict(True, True, everything_failed) == "继承仓位不评价"
    assert verdict(False, True, everything_failed) == "持仓中"
    assert verdict(False, False, everything_failed) == "无止损"
    assert verdict(False, False, checks(sl_not_loosened=FAIL, no_averaging_down=FAIL,
                                        exit_by_plan=FAIL, sl_at_entry=UNKNOWN)) == "止损被放宽"
    assert verdict(False, False, checks(no_averaging_down=FAIL, exit_by_plan=FAIL)) == "逆向加仓"
    assert verdict(False, False, checks(exit_by_plan=FAIL, sl_not_loosened=UNKNOWN)) == "中途下车"
    assert verdict(False, False, checks(sl_not_loosened=UNKNOWN)) == "证据不足"
    assert verdict(False, False, checks(sl_not_loosened=NA)) == "合规"
    assert verdict(False, False, checks()) == "合规"
    assert set(VERDICTS) == {"合规", "中途下车", "无止损", "止损被放宽", "逆向加仓", "证据不足",
                             "继承仓位不评价", "持仓中"}


def test_inherited_position_is_not_judged():
    execs = [ex("x", "close", "Sell", 1, 97, 30)]
    episodes = reconstruct(execs, {("linear", "BTCUSDT", 0): Decimal("1")}, window_start_ms=at(-60))
    result = review_episode(episodes[0], [order("close", "Sell", created=30, reduce_only=True)], [])
    assert result.verdict == "继承仓位不评价"
    assert result.checks["sl_at_entry"].state is UNKNOWN
    assert result.checks["no_averaging_down"].state is UNKNOWN
    assert result.exits[0].kind == "manual"
    assert result.hold_seconds is None and result.realized_r is None


# ----------------------------------------------------------------------------- 对冲 / 连续交易

def test_hedge_mode_legs_are_reviewed_independently():
    long_execs, long_orders = trade("long", sl=None, tp=None, exit_kind="manual", idx=1, tag="L")
    short_execs, short_orders = trade("short", sl=105, tp=90, exit_kind="take_profit", idx=2, tag="S")
    episodes = reconstruct(long_execs + short_execs)
    assert len(episodes) == 2
    orders = long_orders + short_orders
    by_direction = {ep.direction: review_episode(ep, orders, []) for ep in episodes}
    assert by_direction["long"].verdict == "无止损"
    assert by_direction["short"].verdict == "合规"
    assert by_direction["short"].planned_sl == Decimal("105")


def test_stop_of_an_earlier_trade_is_not_credited_to_the_next_one():
    first_execs, first_orders = trade(tag="a")
    second_execs, second_orders = trade(sl=None, tp=None, exit_kind="manual", start=90, tag="b")
    episodes = reconstruct(first_execs + second_execs)
    orders = first_orders + second_orders
    assert [review_episode(ep, orders, []).verdict for ep in episodes] == ["合规", "无止损"]


# ----------------------------------------------------------------------------- 盈亏无关性

def mirrored(execs):
    """把每笔平仓成交价按进场均价镜像:盈利变亏损、亏损变盈利;订单保持不变。"""
    first_side = execs[0].side
    opening = [row for row in execs if row.side == first_side]
    entry = sum(row.qty * row.price for row in opening) / sum(row.qty for row in opening)
    return [row if row.side == first_side else replace(row, price=2 * entry - row.price,
                                                       value=(2 * entry - row.price) * row.qty)
            for row in execs]


OUTCOME_CASES = {
    "止损离场": lambda: trade(),
    "止盈离场": lambda: trade(exit_kind="take_profit"),
    "手动盈利离场": lambda: trade(exit_kind="manual", exit_price=104),
    "手动亏损离场": lambda: trade(exit_kind="manual", exit_price=97),
    "无止损": lambda: trade(sl=None, exit_kind="manual", exit_price=108),
    "止损迟设": lambda: (lambda e, o: (e, swap(o, "t-sl", created_ms=at(10))))(*trade(attach=False)),
    "止损放宽": lambda: (lambda e, o: (e, swap(o, "t-sl", trigger_price=Decimal("93"))))(*trade()),
    "单一快照": lambda: trade(attach=False),
    "逆向加仓": lambda: messy_trade(average=True, manual=False),
    "空单止盈": lambda: trade("short", sl=105, tp=90, exit_kind="take_profit"),
    "空单手动": lambda: trade("short", sl=105, tp=90, exit_kind="manual", exit_price=96),
    # 判定只看订单挂在什么价位,不看成交价:把成交价镜像过去,结论必须不变
    "止盈被拉近": lambda: (lambda e, o: (e, swap(o, "t-tp", trigger_price=Decimal("104"))))(
        *trade(exit_kind="take_profit", exit_price=104)),
    "止损移到盈利一侧": lambda: (lambda e, o: (e, swap(o, "t-sl", trigger_price=Decimal("103"))))(
        *trade(exit_price=103)),
    "止损收紧": lambda: (lambda e, o: (e, swap(o, "t-sl", trigger_price=Decimal("98"))))(
        *trade(exit_price=98)),
    "迟挂的止盈": lambda: (lambda e, o: (e, seen_at_creation(
        swap(o, "t-tp", created_ms=at(40)), "t-sl", "t-tp")))(
        *trade(attach=False, exit_kind="take_profit")),
}


@pytest.mark.parametrize("name", sorted(OUTCOME_CASES))
def test_outcome_does_not_influence_verdict_or_hard_checks(name):
    execs, orders = OUTCOME_CASES[name]()
    original_episode = one(execs)
    flipped_episode = one(mirrored(execs))
    original_episode.realized_pnl = Decimal("123.45")
    flipped_episode.realized_pnl = Decimal("-123.45")
    klines = [bar(200, 120, 80)]
    original = review_episode(original_episode, orders, klines)
    flipped = review_episode(flipped_episode, orders, klines)
    assert original.verdict == flipped.verdict
    for key in HARD_KEYS:
        assert original.checks[key].state is flipped.checks[key].state, key
        assert original.checks[key].value == flipped.checks[key].value, key
    assert [leg.kind for leg in original.exits] == [leg.kind for leg in flipped.exits]
    assert (original.planned_sl, original.planned_tp, original.planned_rr) == (
        flipped.planned_sl, flipped.planned_tp, flipped.planned_rr)
    if original.realized_r is not None and original.realized_r != 0:
        assert flipped.realized_r == -original.realized_r     # 结果确实被翻转了


def test_module_never_reads_account_pnl_fields():
    source = inspect.getsource(principles)
    assert "realized_pnl" not in source and "closed_pnl" not in source
    assert ".fee" not in source


# ----------------------------------------------------------------------------- 永不抛异常

@pytest.mark.parametrize("episode, orders, klines", [
    (None, None, None),
    (object(), [object()], ["x", 3]),
    (TradeEpisode("empty", "linear", "BTCUSDT", "long", 0, T0), [], []),
    (TradeEpisode("closed-empty", "linear", "BTCUSDT", "short", 0, T0, T0 + 1, "closed"), None, None),
    (TradeEpisode("odd-direction", "linear", "BTCUSDT", "sideways", 0, T0), [], []),
    (SimpleNamespace(episode_id="ns", direction="long", fills=[SimpleNamespace(execution=None, action="entry")],
                     status="closed", closed_ms="oops", opened_ms=None), "not-a-list", 42),
])
def test_review_episode_never_raises_on_odd_input(episode, orders, klines):
    result = review_episode(episode, orders, klines, None)
    assert isinstance(result, PrincipleReview)
    assert result.verdict in VERDICTS
    assert set(HARD_KEYS) <= set(result.checks)
    assert all(isinstance(check.state, CheckState) for check in result.checks.values())


def test_review_episode_survives_garbage_orders_and_fills():
    episode = TradeEpisode("g", "linear", "BTCUSDT", "long", 0, T0, T0 + MIN, "closed")
    good = Execution("e", "o1", "linear", "BTCUSDT", "Buy", Decimal("1"), Decimal("100"),
                     Decimal("100"), Decimal("0"), T0)
    zero = replace(good, exec_id="z", qty=Decimal("0"))
    episode.fills += [EpisodeFill(good, "entry"), EpisodeFill(zero, "add"), EpisodeFill(good, "???")]
    garbage = [
        SimpleNamespace(order_id="o1", symbol=None, side=None, position_idx="x", status=None,
                        origin=None, trigger_price="abc", attached_sl="NaN", attached_tp=-1,
                        created_ms=None, updated_ms="later"),
        SimpleNamespace(order_id=""), None, 7,
    ]
    result = review_episode(episode, garbage, [SimpleNamespace(start_ms="x", high=None, low=None)],
                            PrincipleParams(sl_grace_seconds=-5, horizon_hours=-1))
    assert result.verdict in VERDICTS
    assert result.checks["exit_by_plan"].state is UNKNOWN     # 已结束却没有平仓成交
    assert result.planned_sl is None and result.planned_tp is None


def test_day_functions_never_raise_on_odd_input():
    assert [check.key for check in review_day(None, None)] == [
        "flip_after_exit", "revenge_reentry", "trade_count"]
    assert review_day([object(), None], [object()])[2].value == Decimal("0")
    assert day_score(None)["total"] == 0
    assert day_score([object(), None, "x"])["total"] == 0


# ----------------------------------------------------------------------------- 当日层面

def day(*trades, **params):
    execs, orders = [], []
    for item_execs, item_orders in trades:
        execs += item_execs
        orders += item_orders
    episodes = reconstruct(execs)
    settings = PrincipleParams(**params)
    reviews = [review_episode(ep, orders, [], settings) for ep in episodes]
    return episodes, reviews, {check.key: check for check in review_day(episodes, reviews, settings)}


def test_flip_after_exit_detected_with_times():
    _, _, checks = day(trade(tag="a"),
                       trade("short", sl=105, tp=90, exit_kind="take_profit", start=70, tag="b"))
    check = checks["flip_after_exit"]
    assert check.state is FAIL and check.tier == "day" and check.value == Decimal("1")
    for expected in ("BTCUSDT", "11:00", "11:10", "10 分钟", "平多", "开空", "止损"):
        assert expected in check.detail
    assert checks["revenge_reentry"].state is PASS


def test_flip_outside_window_or_on_another_symbol_passes():
    _, _, checks = day(trade(tag="a"),
                       trade("short", sl=105, tp=90, exit_kind="take_profit", start=100, tag="b"))
    assert checks["flip_after_exit"].state is PASS
    _, _, checks = day(trade(tag="a"), trade("short", sl=105, tp=90, exit_kind="take_profit",
                                             start=70, tag="b", symbol="ETHUSDT"))
    assert checks["flip_after_exit"].state is PASS
    _, _, checks = day(trade(tag="a"), trade("short", sl=105, tp=90, exit_kind="take_profit",
                                             start=100, tag="b"), flip_minutes=45)
    assert checks["flip_after_exit"].state is FAIL


def test_flip_through_zero_in_one_order_is_a_flip():
    execs = [ex("e", "o1", "Buy", 1, 100, 0), ex("x", "o2", "Sell", 2, 99, 30)]
    episodes = reconstruct(execs)
    checks = {check.key: check for check in review_day(episodes, [])}
    assert checks["flip_after_exit"].state is FAIL and "0 秒" in checks["flip_after_exit"].detail


def test_revenge_reentry_after_stop_loss():
    _, _, checks = day(trade(tag="a"), trade(start=75, tag="b"))
    check = checks["revenge_reentry"]
    assert check.state is FAIL and check.value == Decimal("1")
    for expected in ("11:00", "11:15", "15 分钟", "同向重进"):
        assert expected in check.detail
    assert checks["flip_after_exit"].state is PASS


def test_reentry_after_take_profit_or_outside_window_is_not_revenge():
    _, _, checks = day(trade(exit_kind="take_profit", tag="a"), trade(start=75, tag="b"))
    assert checks["revenge_reentry"].state is PASS
    _, _, checks = day(trade(tag="a"), trade(start=95, tag="b"))
    assert checks["revenge_reentry"].state is PASS
    _, _, checks = day(trade(tag="a"), trade(start=95, tag="b"), reentry_minutes=60)
    assert checks["revenge_reentry"].state is FAIL


def test_revenge_reentry_unknown_when_exit_kind_is_not_known():
    first, second = trade(tag="a"), trade(start=75, tag="b")
    episodes = reconstruct(first[0] + second[0])
    checks = {check.key: check for check in review_day(episodes, [])}
    assert checks["revenge_reentry"].state is UNKNOWN


def test_trade_count_reports_number_and_respects_optional_limit():
    episodes, _, checks = day(trade(tag="a"), trade(start=200, tag="b"))
    assert checks["trade_count"].state is NA and checks["trade_count"].value == Decimal("2")
    assert "2 笔" in checks["trade_count"].detail
    _, _, checks = day(trade(tag="a"), trade(start=200, tag="b"), max_trades_per_day=1)
    assert checks["trade_count"].state is FAIL
    _, _, checks = day(trade(tag="a"), trade(start=200, tag="b"), max_trades_per_day=2)
    assert checks["trade_count"].state is PASS


def test_empty_day():
    checks = {check.key: check for check in review_day([], [])}
    assert checks["flip_after_exit"].state is NA and checks["revenge_reentry"].state is NA
    assert checks["trade_count"].value == Decimal("0")


def test_inherited_position_is_not_counted_as_a_new_trade_or_a_flip_target():
    execs = [ex("x", "close", "Buy", 1, 97, 30)] + trade(start=40, tag="b")[0]
    episodes = reconstruct(execs, {("linear", "BTCUSDT", 0): Decimal("-1")}, window_start_ms=at(-60))
    checks = {check.key: check for check in review_day(episodes, [])}
    assert checks["trade_count"].value == Decimal("1") and "另有 1 笔" in checks["trade_count"].detail
    assert checks["flip_after_exit"].state is FAIL       # 平掉继承的空单后 10 分钟开多


def test_day_score_counts_and_most_broken():
    _, reviews, _ = day(
        trade(tag="a"),                                                   # 合规
        trade(exit_kind="manual", start=100, tag="b"),                    # 中途下车
        trade(exit_kind="manual", start=200, tag="c"),                    # 中途下车
        trade(sl=None, exit_kind="manual", start=300, tag="d"),           # 无止损(同时手动离场)
        trade(attach=False, start=400, tag="e"),                          # 证据不足
        trade(exit_kind="open", start=500, tag="f"),                      # 持仓中
    )
    score = day_score(reviews)
    assert score["total"] == 6 and score["judged"] == 4 and score["compliant"] == 1
    assert score["by_verdict"]["中途下车"] == 2 and score["by_verdict"]["无止损"] == 1
    assert score["by_verdict"]["证据不足"] == 1 and score["by_verdict"]["持仓中"] == 1
    assert list(score["by_verdict"]) == list(VERDICTS)
    assert score["most_broken"] == {"key": "exit_by_plan", "label": "只有止盈与止损两种离场", "count": 3}


def test_day_functions_accept_reviews_already_converted_to_dicts():
    episodes, reviews, checks = day(trade(tag="a"), trade(start=75, tag="b"),
                                    trade(exit_kind="manual", start=300, tag="c"))
    as_dicts = [asdict(item) for item in reviews]
    assert day_score(as_dicts) == day_score(reviews)
    again = {check.key: check for check in review_day(episodes, as_dicts)}
    assert again["revenge_reentry"] == checks["revenge_reentry"]
    assert again["revenge_reentry"].state is FAIL


def test_day_score_without_failures_has_no_most_broken():
    _, reviews, _ = day(trade(tag="a"))
    score = day_score(reviews)
    assert score["most_broken"] is None and score["compliant"] == 1
    assert day_score([])["most_broken"] is None
