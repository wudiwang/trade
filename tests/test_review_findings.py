"""复核(三位对抗评审)发现的问题的回归测试:第一准则判定层(app/review/principles.py)。

每个测试对应一条发现;场景都是"本人实际做了什么 + 交易所/本地当时能看到哪些订单行"。
时间基准与辅助函数沿用 test_review_principles.py(T0 = 曼谷 2026-11-17 10:00)。
"""
from dataclasses import replace
from decimal import Decimal

import pytest

from app.review.episodes import reconstruct
from app.review.principles import (CheckState, PrincipleParams, day_score, review_day,
                                   review_episode)
from tests.test_review_principles import (D, at, bar, bars, drop, ex, one, order, review,
                                          seen_at_creation, swap, trade)

PASS, FAIL, UNKNOWN, NA = CheckState.PASS, CheckState.FAIL, CheckState.UNKNOWN, CheckState.NA
HOUR = 60
SL = dict(origin="stop_loss", reduce_only=True, close_position=True)
TP = dict(origin="take_profit", reduce_only=True, close_position=True)


# ----------------------------------------------------------------------------- 止盈被拉近(critical)

def test_panel_take_profit_dragged_to_market_uses_the_snapshot_of_the_original_target():
    """仓位面板设的止盈 110 被一次快照记下;3 小时后拖到 103.1 成交。计划仍是 110,结论是中途下车。"""
    end = 3 * HOUR + 2
    execs = [ex("e", "E", "Buy", 1, 100, 0), ex("x", "TP", "Sell", 1, "103.1", end)]
    orders = [
        order("E", "Buy"),
        order("SL", "Sell", status="untriggered", trigger=95, created_sec=10, **SL),
        order("SL", "Sell", status="cancelled", trigger=95, created_sec=10, updated=end, **SL),
        order("TP", "Sell", status="untriggered", trigger=110, created_sec=10, **TP),
        order("TP", "Sell", status="filled", trigger="103.1", created_sec=10, updated=end, **TP),
    ]
    result = review(execs, orders)
    assert result.planned_tp == Decimal("110") and result.planned_rr == Decimal("2.00")
    assert "110" in result.checks["tp_at_entry"].detail
    check = result.checks["exit_by_plan"]
    assert check.state is FAIL and check.value == "target_pulled_in" and "103.1" in check.detail
    assert result.verdict == "中途下车"


def test_take_profit_known_only_by_its_final_row_is_not_presented_as_the_entry_plan():
    """同一笔交易但没有任何快照:103.1 只是"最后的价位",不能写成进场时的计划,也不能判合规。"""
    end = 3 * HOUR + 2
    execs = [ex("e", "E", "Buy", 1, 100, 0), ex("x", "TP", "Sell", 1, "103.1", end)]
    orders = [
        order("E", "Buy", sl=95),
        order("SL", "Sell", status="cancelled", trigger=95, updated=end, **SL),
        order("TP", "Sell", status="filled", trigger="103.1", created_sec=10, updated=end, **TP),
    ]
    result = review(execs, orders)
    assert result.planned_tp is None and result.planned_rr is None
    assert "最后记录的价位 103.1" in result.checks["tp_at_entry"].detail
    assert result.checks["exit_by_plan"].state is UNKNOWN
    assert result.checks["planned_rr"].state is UNKNOWN
    assert result.verdict == "证据不足"
    trusting = review(execs, orders, assume_single_stop_snapshot_unchanged=True)
    assert trusting.planned_tp == Decimal("103.1") and trusting.verdict == "合规"


def test_take_profit_first_set_mid_trade_right_at_market_is_a_manual_exit():
    """进场时没有目标;3 小时后在现价旁边挂止盈,2 分钟后成交 = 用止盈单平仓。"""
    end = 3 * HOUR + 2
    execs = [ex("e", "E", "Buy", 1, 100, 0), ex("x", "TP", "Sell", 1, "103.1", end)]
    orders = [
        order("E", "Buy", sl=95),
        order("SL", "Sell", status="cancelled", trigger=95, updated=end, **SL),
        order("TP", "Sell", status="filled", trigger="103.1", created=3 * HOUR, updated=end, **TP),
    ]
    result = review(execs, orders)
    check = result.checks["exit_by_plan"]
    assert check.state is FAIL and check.value == "target_at_market"
    assert "13:00" in check.detail and "2 分钟后就成交" in check.detail
    assert result.exits[0].kind == "manual" and result.verdict == "中途下车"


# ----------------------------------------------------------------------------- 迟挂的目标单(high)

def late_limit_target(*, snapshot):
    execs = [ex("e", "E", "Buy", 1, 100, 0), ex("x", "L", "Sell", 1, 110, 6 * HOUR)]
    orders = [
        order("E", "Buy", sl=95),
        order("SL", "Sell", status="cancelled", trigger=95, updated=6 * HOUR, **SL),
        order("L", "Sell", order_type="Limit", price=110, reduce_only=True, created=5,
              updated=6 * HOUR),
    ]
    return execs, seen_at_creation(orders, "L") if snapshot else orders


def test_reduce_only_limit_target_placed_after_the_grace_and_held_is_not_a_manual_exit():
    result = review(*late_limit_target(snapshot=True))
    check = result.checks["exit_by_plan"]
    assert result.exits[0].kind == "take_profit"
    assert check.state is PASS and check.value == "plan_changed" and "10:05" in check.detail
    assert result.checks["tp_at_entry"].state is FAIL     # 晚设目标只记在参考项里
    assert result.verdict == "合规"


def test_same_target_without_a_snapshot_is_unknown_but_never_called_a_manual_exit():
    result = review(*late_limit_target(snapshot=False))
    assert result.exits[0].kind == "take_profit"
    assert result.checks["exit_by_plan"].state is UNKNOWN
    assert "手动" not in result.checks["exit_by_plan"].detail.split("。")[0]
    assert result.verdict == "证据不足"


def test_second_target_placed_after_the_first_one_filled_is_still_a_target():
    execs = [ex("e", "E", "Buy", 2, 100, 0), ex("x1", "TP1", "Sell", 1, 105, 60),
             ex("x2", "TP2", "Sell", 1, 110, 180)]
    orders = [
        order("E", "Buy", qty=2, sl=95, tp=105),
        order("SL", "Sell", status="cancelled", trigger=95, updated=180, **SL),
        order("TP1", "Sell", status="filled", trigger=105, updated=60, **TP),
        order("TP2", "Sell", order_type="Limit", price=110, reduce_only=True, created=61, updated=180),
    ]
    result = review(execs, orders)
    assert [leg.kind for leg in result.exits] == ["take_profit", "take_profit"]
    assert result.checks["exit_by_plan"].state is PASS and result.verdict == "合规"


# ----------------------------------------------------------------------------- 已取消订单的保留期(high)

def test_replaced_stop_that_aged_out_of_history_does_not_make_the_trade_stopless():
    """止损 A(95)进场时就有;26 小时后撤掉换成 B(98),47 小时止损触发;复盘在换单 30 小时后。
    交易所只保留 24 小时内被取消的订单,A 已经查不到:不能说"进场时没有止损"。"""
    replaced, end = 26 * HOUR, 47 * HOUR
    synced = at(replaced) + 30 * 3_600_000
    execs = [ex("e", "E", "Buy", 1, 100, 0), ex("x", "B", "Sell", 1, 98, end)]
    stop_a = order("A", "Sell", status="cancelled", trigger=95, created_sec=10, updated=replaced, **SL)
    stop_b = order("B", "Sell", status="filled", trigger=98, created=replaced, updated=end, **SL)
    visible = [order("E", "Buy"), stop_b]
    result = review(execs, visible, orders_synced_ms=synced)
    check = result.checks["sl_at_entry"]
    assert check.state is UNKNOWN and check.value == "late_unconfirmed"
    assert "24 小时" in check.detail and result.verdict == "证据不足"
    # 本地快照一直连续覆盖(这段时间被取消的订单保证看得到):这时才能确定进场时没有止损
    covered = review(execs, visible, orders_synced_ms=synced, orders_complete_since_ms=at(-60))
    assert covered.checks["sl_at_entry"].state is FAIL and covered.verdict == "无止损"
    # 快照里留着 A:进场即设,且换成的 98 比 95 更紧
    kept = review(execs, seen_at_creation([order("E", "Buy"), stop_a, stop_b], "A", "B"),
                  orders_synced_ms=synced)
    assert kept.checks["sl_at_entry"].state is PASS and kept.verdict == "合规"


def test_gap_between_stops_older_than_the_retention_is_not_called_a_removal():
    execs, orders = trade(exit_kind="open")
    orders = swap(orders, "t-sl", status="cancelled", updated_ms=at(20))
    orders.append(order("t-sl2", "Sell", trigger=95, created=30 * HOUR, updated=40 * HOUR, **SL))
    execs.append(ex("t-x", "t-sl2", "Sell", 1, 95, 40 * HOUR))
    orders = seen_at_creation(orders, "t-sl2")
    recent = review(execs, orders, orders_synced_ms=at(41 * HOUR), orders_complete_since_ms=at(-60))
    assert recent.checks["sl_not_loosened"].state is FAIL          # 全程有快照:确实裸奔了 30 小时
    stale = review(execs, orders, orders_synced_ms=at(60 * HOUR))  # 只保证最近 24 小时
    assert stale.checks["sl_not_loosened"].state is UNKNOWN
    assert "保留期" in stale.checks["sl_not_loosened"].detail


# ----------------------------------------------------------------------------- 止损有没有被放宽(high)

def panel_stop(*rows):
    execs = [ex("e", "E", "Buy", 1, 100, 0), ex("x", "S", "Sell", 1, rows[-1][2], 120)]
    orders = [order("E", "Buy")] + [
        order("S", "Sell", status=status, trigger=price, created_sec=20, updated=updated, **SL)
        for status, updated, price in rows]
    return execs, orders


def test_stop_loosened_before_the_first_snapshot_is_not_blessed_by_that_snapshot():
    """止损 95 在进场后 10 分钟被放宽到 93;第一份快照在第 20 分钟,看到的已经是 93。"""
    execs, orders = panel_stop(("untriggered", 10, 93), ("filled", 120, 93))
    result = review(execs, orders)
    check = result.checks["sl_not_loosened"]
    assert check.state is UNKNOWN and "最初止损" not in check.detail
    assert result.planned_sl is None and result.verdict == "证据不足"
    honest = review(*panel_stop(("untriggered", None, 95), ("filled", 120, 95)))
    assert honest.checks["sl_not_loosened"].state is PASS and honest.verdict == "合规"


def test_stop_tightened_then_restored_to_the_original_is_not_called_loosened():
    """空单:原止损 105 → 原单上收紧到 103 → 撤掉后按原价 105 重挂。只看到 103、105 两行时不能指控。"""
    execs = [ex("e", "E", "Sell", 1, 100, 0), ex("x", "S2", "Buy", 1, 105, 300)]
    first = order("S1", "Buy", status="cancelled", trigger=103, created_sec=20, updated=200, **SL)
    second = order("S2", "Buy", status="filled", trigger=105, created=200, updated=300, **SL)
    blind = review(execs, [order("E", "Sell"), first, second])
    check = blind.checks["sl_not_loosened"]
    assert check.state is UNKNOWN and "最初止损" not in check.detail
    assert "103" in check.detail and "105" in check.detail and blind.verdict == "证据不足"
    original = replace(first, status="untriggered", trigger_price=D(105), updated_ms=first.created_ms)
    full = review(execs, [order("E", "Sell"), original, first]
                  + seen_at_creation([second], "S2"))
    assert full.checks["sl_not_loosened"].state is PASS and full.verdict == "合规"


def test_replacement_stop_known_only_by_its_last_price_cannot_prove_it_was_never_wider():
    execs = [ex("e", "E", "Sell", 1, 100, 0), ex("x", "S2", "Buy", 1, 104, 300)]
    first = order("S1", "Buy", status="cancelled", trigger=105, created_sec=20, updated=200, **SL)
    second = order("S2", "Buy", status="filled", trigger=104, created=200, updated=300, **SL)
    orders = seen_at_creation([order("E", "Sell"), first, second], "S1")
    check = review(execs, orders).checks["sl_not_loosened"]
    assert check.state is UNKNOWN and "最初止损 105" in check.detail and "104" in check.detail


def test_two_partial_stops_known_only_by_final_rows_are_not_called_loosened():
    """进场后 20 秒、25 秒各挂半仓止损 95 与 93,都没动过,价格依次打掉。"""
    execs = [ex("e", "E", "Buy", 2, 100, 0), ex("x1", "S95", "Sell", 1, 95, 120),
             ex("x2", "S93", "Sell", 1, 93, 180)]
    orders = [order("E", "Buy", qty=2),
              order("S95", "Sell", origin="stop_loss", trigger=95, reduce_only=True, created_sec=20,
                    updated=120),
              order("S93", "Sell", origin="stop_loss", trigger=93, reduce_only=True, created_sec=25,
                    updated=180)]
    blind = review(execs, orders)
    assert blind.checks["sl_not_loosened"].state is UNKNOWN and blind.verdict == "证据不足"
    seen = review(execs, seen_at_creation(orders, "S95", "S93"))
    assert seen.checks["sl_not_loosened"].state is PASS and seen.verdict == "合规"


# ----------------------------------------------------------------------------- 同一行为,同一结论(medium)

def stop_into_profit(control):
    """多单 100,止损 95、止盈 110;3 小时后把止损提到 102.9(或加追踪止损)并被打掉。"""
    end = 3 * HOUR + 2
    execs = [ex("e", "E", "Buy", 1, 100, 0)]
    orders = [order("E", "Buy", sl=95, tp=110),
              order("TP", "Sell", status="cancelled", trigger=110, updated=end, **TP)]
    if control == "panel":
        orders.append(order("SL", "Sell", status="filled", trigger="102.9", updated=end, **SL))
        execs.append(ex("x", "SL", "Sell", 1, "102.9", end))
    elif control == "conditional":
        orders += [order("SL", "Sell", status="cancelled", trigger=95, updated=3 * HOUR, **SL),
                   order("C2", "Sell", trigger="102.9", reduce_only=True, created=3 * HOUR, updated=end)]
        execs.append(ex("x", "C2", "Sell", 1, "102.9", end))
    else:
        orders += [order("SL", "Sell", status="cancelled", trigger=95, updated=end, **SL),
                   order("TS", "Sell", origin="trailing_stop", reduce_only=True, close_position=True,
                         created=2 * HOUR, updated=end)]
        execs.append(ex("x", "TS", "Sell", 1, "102.9", end))
    return execs, orders


@pytest.mark.parametrize("control", ["panel", "conditional", "trailing"])
def test_stop_moved_into_profit_and_hit_gets_one_verdict_whatever_control_was_used(control):
    result = review(*stop_into_profit(control))
    assert result.verdict == "中途下车", control
    assert result.checks["exit_by_plan"].state is FAIL
    assert result.exits[0].kind in ("stop_loss", "trailing_stop")
    assert result.checks["tp_at_entry"].state is PASS       # 被上移的止损不会被当成"迟设的止盈"
    lenient = review(*stop_into_profit(control), stop_into_profit_is_exit=False)
    assert lenient.checks["exit_by_plan"].state is PASS and lenient.verdict == "合规", control
    assert lenient.checks["exit_by_plan"].value == "plan_changed"


def test_stop_tightened_on_the_loss_side_or_to_breakeven_is_still_a_stop_loss_exit():
    for level in ("99", "100", "100.1"):                  # 100.1 在 0.2% 保本带内
        execs, orders = trade(exit_price=level)
        orders = swap(orders, "t-sl", trigger_price=D(level))
        result = review(execs, orders)
        assert result.checks["exit_by_plan"].state is PASS, level
        assert result.verdict == "合规", level
    execs, orders = trade(exit_price="100.5")
    result = review(execs, swap(orders, "t-sl", trigger_price=D("100.5")))
    assert result.checks["exit_by_plan"].value == "stop_in_profit"


def test_conditional_order_without_reduce_only_used_as_the_stop_is_recognised():
    """条件市价卖单 @95,没勾只减仓,进场后 10 秒挂出,触发后平掉仓位。"""
    execs = [ex("e", "E", "Buy", 1, 100, 0), ex("x", "C", "Sell", 1, 95, 120)]
    cond = order("C", "Sell", trigger=95, created_sec=10, updated=120)
    result = review(execs, seen_at_creation([order("E", "Buy"), cond], "C"))
    assert result.checks["sl_at_entry"].state is PASS
    assert result.exits[0].kind == "stop_loss" and result.checks["exit_by_plan"].state is PASS
    assert result.planned_sl == Decimal("95") and result.verdict == "合规"
    oversized = replace(cond, qty=D(3), filled_qty=D(3))     # 数量大于仓位:会反手,不算止损
    assert review(execs, [order("E", "Buy"), oversized]).exits[0].kind == "manual"


def test_conditional_order_whose_original_side_is_unknown_is_not_called_a_missing_stop():
    """进场后 30 秒挂的只减仓条件单,只留下最后的触发价 102.9(在盈利一侧):
    它可能是被一路上移的止损,也可能本来就是止盈,不能据此判"无止损"。"""
    execs = [ex("e", "E", "Buy", 1, 100, 0), ex("x", "C", "Sell", 1, "102.9", 180)]
    cond = order("C", "Sell", trigger="102.9", reduce_only=True, created_sec=30, updated=180)
    result = review(execs, [order("E", "Buy"), cond])
    assert result.checks["sl_at_entry"].state is UNKNOWN
    assert result.checks["sl_at_entry"].value == "ambiguous" and result.verdict == "证据不足"
    was_stop = replace(cond, status="untriggered", trigger_price=D(95), updated_ms=cond.created_ms)
    known = review(execs, [order("E", "Buy"), was_stop, cond])
    assert known.checks["sl_at_entry"].state is PASS and known.exits[0].kind == "stop_loss"


# ----------------------------------------------------------------------------- 小项(low)

def test_stale_snapshot_of_an_earlier_positions_stop_is_not_credited_to_the_next_trade():
    """上一笔多单的止损单只留下一份"未触发"的旧快照(它被撤的那一刻没被记到);下一笔多单没设止损。"""
    old_execs, old_orders = trade(sl=None, tp=None, exit_kind="manual", start=-30 * HOUR, tag="old")
    stale = order("old-sl", "Sell", status="untriggered", trigger=95, created=-30 * HOUR, **SL)
    new_execs, new_orders = trade(sl=None, tp=110, attach=False, exit_kind="take_profit", tag="new")
    episodes = reconstruct(old_execs + new_execs)
    orders = old_orders + [stale] + seen_at_creation(new_orders, "new-tp")
    for assume in (False, True):
        params = PrincipleParams(assume_single_stop_snapshot_unchanged=assume)
        result = review_episode(episodes[1], orders, [], params)
        assert result.checks["sl_at_entry"].state is FAIL and result.verdict == "无止损", assume
        assert result.planned_sl is None


def test_trailing_stop_fill_without_its_order_row_falls_back_to_the_fill_origin():
    execs, orders = trade(exit_kind="open")
    execs.append(replace(ex("t-x", "t-trail", "Sell", 1, 104, 60), origin="trailing_stop"))
    result = review(execs, orders)
    assert result.exits[0].kind == "trailing_stop"
    assert result.checks["exit_by_plan"].state is PASS


def test_detail_times_follow_the_configured_time_zone():
    execs, orders = trade()
    assert "10:00" in review(execs, orders).checks["sl_at_entry"].detail                 # 默认曼谷
    assert "11:00" in review(execs, orders, tz_offset_hours=8).checks["sl_at_entry"].detail
    assert "08:30" in review(execs, orders, tz_offset_hours=5.5).checks["sl_at_entry"].detail


# ----------------------------------------------------------------------------- 当日层面

def day(*trades, window_start=None, **params):
    execs, orders = [], []
    for item_execs, item_orders in trades:
        execs += item_execs
        orders += item_orders
    episodes = reconstruct(execs)
    settings = PrincipleParams(**params)
    reviews = [review_episode(ep, orders, [], settings) for ep in episodes]
    checks = review_day(episodes, reviews, settings,
                        window_start_ms=None if window_start is None else at(window_start))
    return reviews, {check.key: check for check in checks}


def test_opposite_trade_after_a_target_was_reached_is_not_a_flip():
    reviews, checks = day(trade(exit_kind="take_profit", tag="a"),
                          trade("short", sl=113, tp=104, entry=110, exit_kind="take_profit",
                                start=80, tag="b"))
    assert [item.verdict for item in reviews] == ["合规", "合规"]
    check = checks["flip_after_exit"]
    assert check.state is PASS and "不算反手" in check.detail and "11:20" in check.detail
    # 止盈是被拉近后成交的:那是中途下车,之后的反向单照样算反手
    execs, orders = trade(exit_kind="take_profit", exit_price=104, tag="a")
    pulled = (execs, swap(orders, "a-tp", trigger_price=D(104)))
    _, checks = day(pulled, trade("short", sl=113, tp=95, entry=104, exit_kind="take_profit",
                                  start=80, tag="b"))
    assert checks["flip_after_exit"].state is FAIL


def test_flip_across_midnight_is_reported_on_the_day_the_second_trade_opened():
    """23:50 空单止损,00:05 开多:第二天的报告里必须有这次反手,且前一天那笔不计入第二天的笔数。"""
    first = trade("short", sl=105, tp=90, start=-20, exit_min=10, tag="a")     # 09:40 → 09:50 止损
    second = trade(start=5, tag="b")                                           # 10:05 开多
    _, checks = day(first, second, window_start=0)                             # 当天从 10:00 起
    check = checks["flip_after_exit"]
    assert check.state is FAIL and "开多" in check.detail and "15 分钟" in check.detail
    assert checks["trade_count"].value == Decimal("1")
    assert "另有" not in checks["trade_count"].detail
    _, checks = day(first, trade("short", sl=105, tp=90, start=5, tag="b"), window_start=0)
    assert checks["revenge_reentry"].state is FAIL
    _, checks = day(first, window_start=0)                                     # 当天其实没有交易
    assert checks["flip_after_exit"].state is NA and checks["trade_count"].value == Decimal("0")


def test_failed_checks_of_open_positions_do_not_enter_most_broken():
    execs, orders = trade(exit_kind="open")
    orders = swap(orders, "t-sl", status="cancelled", updated_ms=at(20))
    open_trade = review(execs, orders, orders_synced_ms=at(90))
    assert open_trade.verdict == "持仓中" and open_trade.checks["sl_not_loosened"].state is FAIL
    score = day_score([open_trade])
    assert score["judged"] == 0 and score["most_broken"] is None


# ----------------------------------------------------------------------------- 截止时刻(critical)

def overnight(exit_kind):
    """空单 22:00 进场(止损止盈随进场单),次日 00:05 离场;当天的窗口到 24:00 为止。"""
    execs, orders = trade("short", sl=105, tp=90, exit_kind=exit_kind, exit_min=125, tag="n")
    cutoff = at(120)
    episode = one([row for row in execs if row.timestamp_ms < cutoff])      # 当天只看到进场
    return episode, orders, cutoff


@pytest.mark.parametrize("exit_kind", ["take_profit", "manual", "stop_loss"])
def test_position_still_open_at_day_end_is_judged_on_order_states_of_that_moment(exit_kind):
    episode, orders, cutoff = overnight(exit_kind)
    assert episode.status == "open"
    params = PrincipleParams(orders_synced_ms=cutoff + 10 * 60_000, as_of_ms=cutoff)
    result = review_episode(episode, orders, [], params)
    assert result.verdict == "持仓中"
    failed = [key for key, check in result.checks.items()
              if check.tier == "hard" and check.state is FAIL]
    assert failed == [], (exit_kind, failed)
    assert result.checks["sl_at_entry"].state is PASS
    assert day_score([result])["most_broken"] is None
    # 不截止(旧行为):次日 00:05 才发生的撤单会被算到"持仓中"的这一天头上
    if exit_kind != "stop_loss":
        raw = review_episode(episode, orders, [], replace(params, as_of_ms=None))
        assert raw.checks["sl_not_loosened"].state is FAIL


def test_cutoff_ignores_orders_placed_after_it_but_keeps_what_was_seen_before():
    execs, orders = trade(exit_kind="open")
    orders = swap(orders, "t-sl", trigger_price=D(90), updated_ms=at(45))      # 10:45 放宽到 90
    episode = one(execs)
    before = review_episode(episode, orders, [], PrincipleParams(as_of_ms=at(30)))
    assert before.checks["sl_not_loosened"].state is not FAIL                  # 10:30 时还没放宽
    after = review_episode(episode, orders, [], PrincipleParams(as_of_ms=at(60)))
    assert after.checks["sl_not_loosened"].state is FAIL
