"""app/review/system_match.py: 用合成K线逐项验证趋势/顺逆势/触发/归类, 以及因果性与健壮性。"""
import json
from dataclasses import asdict, replace
from decimal import Decimal as D

import pytest

from app.review.models import EpisodeFill, Execution, Kline, TradeEpisode
from app.review.system_match import (
    INTERVAL_MS,
    SETUPS,
    CheckState,
    MatchParams,
    SystemFit,
    closed_before,
    match_episode,
    to_engine_rows,
)

BASE = 14_400_000 * 118_100          # 同时是 5m/15m/1h/4h 的整点
ENTRY = BASE + 30_000                # 整点后 30 秒入场
W = D("0.4")


def zigzag(pivots, legs, tf, end_ms=BASE):
    """从 pivots[0] 出发, 第 i 段用 legs[i] 根K线性走到 pivots[i+1]; 最后一根在 end_ms 收盘。
    每根K: 高=中心+W, 低=中心-W, 实体恒为 W -> 相邻K无包含, 分型只出现在拐点。"""
    rows = []
    price = D(str(pivots[0]))
    for target, count in zip(pivots[1:], legs):
        step = (D(str(target)) - price) / count
        for i in range(1, count + 1):
            centre = price + step * i
            open_, close = ((centre - W / 2, centre + W / 2) if step > 0
                            else (centre + W / 2, centre - W / 2))
            rows.append((open_, centre + W, centre - W, close))
        price = D(str(target))
    return from_rows(rows, tf, end_ms)


def from_rows(rows, tf, end_ms=BASE):
    interval = INTERVAL_MS[tf]
    start = end_ms - len(rows) * interval
    return [Kline(start + i * interval, D(str(o)), D(str(h)), D(str(l)), D(str(c)),
                  D("100"), D("10000"))
            for i, (o, h, l, c) in enumerate(rows)]


def mirror(bars, around=240):
    m = D(str(around))
    return [replace(bar, open=m - bar.open, high=m - bar.low, low=m - bar.high,
                    close=m - bar.close) for bar in bars]


def episode(direction="long", price="107", entry_ms=ENTRY):
    side = "Buy" if direction == "long" else "Sell"
    fill = Execution("e1", "o1", "linear", "BTCUSDT", side, D("1"), D(price), D(price),
                     D("0.05"), entry_ms)
    item = TradeEpisode("ep", "linear", "BTCUSDT", direction, 0, entry_ms)
    item.fills.append(EpisodeFill(fill, "entry"))
    return item


def evidence(fit, key):
    return next(item for item in fit.evidence if item.key == key)


# ---- 各级别的合成行情 -------------------------------------------------------

UP_PIVOTS = [100, 110, 105, 112, 107, 130, 124, 132, 126, 140]      # 两个中枢, 后枢在前枢之上
RANGE_PIVOTS = [100, 110, 102, 109, 101, 110, 103, 109, 102, 110]    # 只有一个中枢


def uptrend(tf):
    return zigzag(UP_PIVOTS, [14] * 9, tf)


def downtrend(tf):
    return mirror(uptrend(tf))


def ranging(tf):
    return zigzag(RANGE_PIVOTS, [14] * 9, tf)


def pullback_long(tf="5m", after=2):
    """上涨 -> 回调成笔到 106 -> 底分型 -> 右K -> 停顿K (after=2 时停顿K是最后一根)。"""
    return zigzag([100, 112, 106, 106 + 0.8 * after], [16, 8, after], tf)


def straight(tf, start=100, end=130, count=40):
    return zigzag([start, end], [count], tf)


def shift_short_15m(last_target=112, last_bars=5):
    """顶1 120 -> 底1 110 -> 顶2 116(更低) -> 跌破底1到 104 -> 反抽。"""
    return zigzag([100, 120, 110, 116, 104, last_target], [10, 8, 6, 8, last_bars], "15m")


def consolidation_15m():
    # 最后一段走 9 根: 103 处的底分型+停顿早已超出 6 根回看, 入场时只是在中枢里漂
    return zigzag([100, 110, 102, 109, 101, 110, 103, 107.5], [6, 6, 6, 6, 6, 6, 9], "15m")


def displacement_long_5m(confirmed=True):
    """缓涨 24 根(实体 0.4) -> 一根实体 8 的大阴 -> 底分型 -> 右K -> 停顿K。"""
    bars = zigzag([100, 112], [24], "5m")
    rows = [(bar.open, bar.high, bar.low, bar.close) for bar in bars]
    rows.append(("112", "112.2", "103.5", "104"))        # 位移K
    rows.append(("104", "104.5", "102", "103"))          # 底分型中K
    if confirmed:
        rows.append(("104.4", "105.5", "103", "105.2"))  # 右K
        rows.append(("105.6", "107", "104.5", "106.5"))  # 停顿K: 收盘 > 右K最高 105.5
    return from_rows(rows, "5m")


# ---- 1. 趋势 ---------------------------------------------------------------

def test_trend_up_down_range_follow_engine_two_centre_definition():
    fit = match_episode(episode(), {"4h": uptrend("4h"), "1h": downtrend("1h")})
    assert (fit.trend_4h, fit.trend_1h) == ("up", "down")
    assert "后枢下沿 125.6 > 前枢上沿 110.4" in evidence(fit, "trend_4h").detail
    assert "后枢上沿 114.4 < 前枢下沿 129.6" in evidence(fit, "trend_1h").detail

    fit = match_episode(episode(), {"4h": ranging("4h")})
    assert fit.trend_4h == "range"
    assert "只有1个中枢" in evidence(fit, "trend_4h").detail
    assert evidence(fit, "trend_4h").state is CheckState.NA      # 趋势只是描述, 没有"符合/不符合"


def test_trend_unknown_when_history_short_missing_or_without_centre():
    short = uptrend("4h")[-80:]                      # 少于 120 根
    fit = match_episode(episode(), {"4h": short, "1h": straight("1h", count=130)})
    assert fit.trend_4h == "unknown"
    assert "少于120根" in evidence(fit, "trend_4h").detail
    assert fit.trend_1h == "unknown"                  # 130 根单边, 没有中枢
    assert "未形成中枢" in evidence(fit, "trend_1h").detail
    assert evidence(fit, "trend_1h").state is CheckState.UNKNOWN
    assert fit.alignment == "未知"

    fit = match_episode(episode(), {})
    assert (fit.trend_4h, fit.trend_1h, fit.alignment) == ("unknown", "unknown", "未知")


def test_trend_unknown_when_klines_do_not_reach_entry_or_have_a_gap():
    stale = zigzag(UP_PIVOTS, [14] * 9, "4h", end_ms=BASE - 3 * INTERVAL_MS["4h"])
    fit = match_episode(episode(), {"4h": stale})
    assert fit.trend_4h == "unknown"
    assert "没有覆盖到入场前一根" in evidence(fit, "trend_4h").detail

    bars = uptrend("4h")
    gapped = bars[:60] + bars[61:]                    # 中间缺一根 -> 只剩 65 根连续
    fit = match_episode(episode(), {"4h": gapped})
    assert fit.trend_4h == "unknown"
    assert "共65根" in evidence(fit, "trend_4h").detail


# ---- 2. 顺逆势 -------------------------------------------------------------

@pytest.mark.parametrize("direction,klines,expected,decider", [
    ("long", {"4h": uptrend("4h")}, "顺势", "由 4h(上涨)"),
    ("short", {"4h": uptrend("4h")}, "逆势", "由 4h(上涨)"),
    ("short", {"4h": downtrend("4h")}, "顺势", "由 4h(下跌)"),
    ("long", {"4h": downtrend("4h"), "1h": uptrend("1h")}, "逆势", "1h 与 4h 方向相反"),
    ("long", {"4h": ranging("4h"), "1h": ranging("1h")}, "无序", "由 4h 判定为无序"),
    ("long", {"1h": ranging("1h")}, "无序", "由 1h 判定为无序"),
    ("long", {"4h": ranging("4h"), "1h": uptrend("1h")}, "顺势", "改由 1h 定方向"),
    ("short", {"1h": uptrend("1h")}, "逆势", "改由 1h 定方向"),
    ("long", {}, "未知", "两个级别都无法判定"),
])
def test_alignment_names_the_deciding_timeframe(direction, klines, expected, decider):
    fit = match_episode(episode(direction), klines)
    assert fit.alignment == expected
    assert decider in evidence(fit, "alignment").detail


def test_alignment_can_be_configured_to_stop_at_a_ranging_4h():
    klines = {"4h": ranging("4h"), "1h": uptrend("1h")}
    strict = MatchParams(fallback_to_1h_when_4h_range=False)
    assert match_episode(episode(), klines, strict).alignment == "无序"
    # 4h 是"未知"(不是无序)时, 两种设置都由 1h 定
    assert match_episode(episode(), {"1h": uptrend("1h")}, strict).alignment == "顺势"


# ---- 3. 触发 ---------------------------------------------------------------

def test_trigger_bottom_fractal_plus_stall_counts_closed_bars_before_entry():
    fit = match_episode(episode(), {"5m": pullback_long(after=2)})
    assert fit.trigger == "5m 底分型+停顿, 入场前1根"
    detail = evidence(fit, "trigger").detail
    assert "底分型 105.6" in detail and "> 右K最高 107.2" in detail and "收盘 107.8" in detail

    fit = match_episode(episode(), {"5m": pullback_long(after=4)})
    assert fit.trigger == "5m 底分型+停顿, 入场前3根"


def test_trigger_short_uses_top_fractal_and_ignores_opposite_fractal():
    bars = mirror(pullback_long(after=3))
    assert match_episode(episode("short"), {"5m": bars}).trigger == "5m 顶分型+停顿, 入场前2根"
    fit = match_episode(episode("long"), {"5m": bars})        # 做多只认底分型
    assert fit.trigger == "无"
    assert evidence(fit, "trigger").state is CheckState.FAIL


def test_trigger_outside_lookback_or_not_yet_stalled_is_none():
    fit = match_episode(episode(), {"5m": pullback_long(after=8)})     # 停顿K在入场前第7根
    assert fit.trigger == "无"
    assert "入场前第7根, 超出范围" in evidence(fit, "trigger").detail
    wider = MatchParams(trigger_lookback_bars=7)
    assert match_episode(episode(), {"5m": pullback_long(after=8)}, wider).trigger.endswith("入场前7根")

    assert match_episode(episode(), {"5m": pullback_long(after=1)}).trigger == "无"   # 只有右K
    assert match_episode(episode(), {"5m": straight("5m")}).trigger == "无"            # 没有分型


def test_trigger_broken_fractal_does_not_count():
    bars = pullback_long(after=2)
    rows = [(b.open, b.high, b.low, b.close) for b in bars]
    rows.append(("107.6", "107.9", "105", "105.2"))       # 新低 105 < 底分型低点 105.6
    fit = match_episode(episode(), {"5m": from_rows(rows, "5m")})
    assert fit.trigger == "无"
    assert "被击穿(105)" in evidence(fit, "trigger").detail


def test_trigger_falls_back_to_15m():
    fit = match_episode(episode(), {"15m": pullback_long("15m", after=3)})
    assert fit.trigger == "15m 底分型+停顿, 入场前2根"
    assert "5m 未提供K线" in evidence(fit, "trigger").detail

    both = {"5m": straight("5m"), "15m": pullback_long("15m", after=2)}
    fit = match_episode(episode(), both)
    assert fit.trigger == "15m 底分型+停顿, 入场前1根"
    assert "5m 入场前6根已收盘K" in evidence(fit, "trigger").detail     # 5m 没找到的记录仍在

    fit = match_episode(episode(), {"5m": pullback_long()[-5:]})
    assert fit.trigger == "未知"
    assert evidence(fit, "trigger").state is CheckState.UNKNOWN


# ---- 4. 归类 ---------------------------------------------------------------

def test_setup_trend_pullback_is_the_core_model():
    fit = match_episode(episode(), {"4h": uptrend("4h"), "5m": pullback_long()})
    assert (fit.alignment, fit.setup) == ("顺势", "顺势回调(关键位未核对)")
    assert "关键位" in evidence(fit, "setup").detail and "不等于核心模型" in evidence(fit, "setup").detail
    pullback = evidence(fit, "pullback")
    assert pullback.state is CheckState.PASS
    assert "顶分型 112.4" in pullback.detail and "底分型 105.6" in pullback.detail

    short = match_episode(episode("short"), {"4h": downtrend("4h"), "5m": mirror(pullback_long())})
    assert (short.alignment, short.setup) == ("顺势", "顺势回调(关键位未核对)")
    assert evidence(short, "pullback").label == "反弹成笔"


def test_with_trend_entry_without_pullback_or_trigger_is_outside_the_system():
    klines = {"4h": uptrend("4h"), "15m": straight("15m"), "5m": straight("5m")}
    fit = match_episode(episode(), klines)
    assert (fit.alignment, fit.trigger, fit.setup) == ("顺势", "无", "系统外")
    reason = evidence(fit, "setup").detail
    assert "反向笔未完成" in reason and "无小级别触发" in reason


def test_counter_trend_entry_with_nothing_else_is_outside_the_system():
    klines = {"4h": downtrend("4h"), "15m": straight("15m"), "5m": straight("5m")}
    fit = match_episode(episode(), klines)
    assert (fit.alignment, fit.setup) == ("逆势", "系统外")
    assert "大级别逆势" in evidence(fit, "setup").detail


def test_setup_structure_shift_reversal_short_and_long_mirror():
    fit = match_episode(episode("short", "112"), {"15m": shift_short_15m()})
    assert fit.setup == "趋势转换反转"
    shift = evidence(fit, "structure_shift")
    assert shift.state is CheckState.PASS
    assert "顶1 120.4" in shift.detail and "底1 109.6" in shift.detail and "顶2 116.4" in shift.detail
    assert "跌破底1" in shift.detail
    assert evidence(fit, "shift_retest").state is CheckState.PASS

    fit = match_episode(episode("long", "128"), {"15m": mirror(shift_short_15m())})
    assert fit.setup == "趋势转换反转"
    assert "升破顶1" in evidence(fit, "structure_shift").detail


def test_structure_shift_needs_retest_and_must_not_be_reclaimed():
    chasing = match_episode(episode("short", "105"), {"15m": shift_short_15m()})
    assert evidence(chasing, "structure_shift").state is CheckState.PASS
    assert evidence(chasing, "shift_retest").state is CheckState.FAIL
    assert chasing.setup != "趋势转换反转"

    reclaimed = match_episode(episode("short", "119"),
                              {"15m": shift_short_15m(last_target=119, last_bars=8)})
    check = evidence(reclaimed, "structure_shift")
    assert check.state is CheckState.FAIL and "结构已被化解" in check.detail

    wrong_side = match_episode(episode("long", "112"), {"15m": shift_short_15m()})
    assert evidence(wrong_side, "structure_shift").state is CheckState.FAIL


def test_setup_imbalance_reversal_needs_reverse_structure():
    fit = match_episode(episode("long", "106.5"), {"5m": displacement_long_5m()})
    assert fit.setup == "失衡反转(辅助)"
    imbalance = evidence(fit, "imbalance")
    assert imbalance.state is CheckState.PASS
    assert imbalance.value == D("20.00")
    assert "开 112 收 104" in imbalance.detail

    knife = match_episode(episode("long", "103"),
                          {"5m": displacement_long_5m(confirmed=False),
                           "15m": straight("15m", 130, 100), "4h": downtrend("4h")})
    assert evidence(knife, "imbalance").state is CheckState.PASS
    assert (knife.trigger, knife.setup) == ("无", "系统外")

    same_side = match_episode(episode("short", "106.5"), {"5m": displacement_long_5m()})
    check = evidence(same_side, "imbalance")
    assert check.state is CheckState.FAIL and "与入场同向(向下)的位移K" in check.detail


def test_displacement_after_the_trigger_is_not_an_imbalance_reversal():
    # 先有底分型+停顿, 之后才出现一根没破底的大阴: 不是"位移后等反向结构确认"
    bars = pullback_long(after=3)
    rows = [(b.open, b.high, b.low, b.close) for b in bars]
    rows.append(("108.4", "108.6", "105.9", "106.0"))     # 实体 2.4 = 6 倍, 低点 105.9 未破 105.6
    klines = {"5m": from_rows(rows, "5m"), "15m": straight("15m"), "4h": downtrend("4h")}
    fit = match_episode(episode("long", "106"), klines)
    assert evidence(fit, "imbalance").state is CheckState.PASS
    assert fit.trigger == "5m 底分型+停顿, 入场前3根"
    assert fit.setup == "系统外"
    assert "反向位移K出现在触发之后" in evidence(fit, "setup").detail


def test_setup_consolidation_anticipation_is_flagged_forbidden():
    klines = {"15m": consolidation_15m(), "5m": straight("5m", 100, 106, 30)}
    for direction in ("long", "short"):
        fit = match_episode(episode(direction, "106"), klines)
        assert fit.setup == "盘整区预判(禁入)"
        check = evidence(fit, "consolidation")
        assert check.state is CheckState.PASS
        assert "[ZD 101.6, ZG 109.4]" in check.detail and check.value == "56%"

    # 同样在中枢里, 但小级别给了触发 -> 不再算"预判"
    triggered = match_episode(episode("long", "106"),
                              {"15m": consolidation_15m(), "5m": pullback_long()})
    assert triggered.setup != "盘整区预判(禁入)"


def test_consolidation_falls_back_to_1h_and_requires_price_inside_a_live_centre():
    fit = match_episode(episode("long", "106"), {"1h": ranging("1h")[:-8]})   # 时间错位 -> 1h 也不可用
    assert evidence(fit, "consolidation").state is CheckState.UNKNOWN

    inside = zigzag([100, 110, 102, 109, 101, 110, 103, 106], [6, 6, 6, 6, 6, 6, 3], "1h")
    assert evidence(match_episode(episode("long", "106"), {"1h": inside}),
                    "consolidation").state is CheckState.PASS

    left = zigzag([100, 110, 102, 109, 101, 110, 103, 125], [6, 6, 6, 6, 6, 6, 12], "15m")
    check = evidence(match_episode(episode("long", "125"), {"15m": left}), "consolidation")
    assert check.state is CheckState.FAIL and "中枢上方" in check.detail


def test_setup_unknown_when_evidence_is_missing_rather_than_outside():
    fit = match_episode(episode(), {"4h": downtrend("4h")})       # 没有任何小级别K线
    assert (fit.alignment, fit.trigger, fit.setup) == ("逆势", "未知", "未知")
    assert "小级别触发" in evidence(fit, "setup").detail
    assert fit.setup in SETUPS


# ---- 5. 因果性 / 不读结果 / 健壮性 -------------------------------------------

def scenario():
    return {"4h": uptrend("4h"), "1h": ranging("1h"), "15m": consolidation_15m(),
            "5m": pullback_long(), "1m": straight("1m", count=60)}


def future_bars(tf, count=40):
    """入场之后的K线: 先是一根入场时尚未收盘的巨幅K, 然后暴跌再暴涨。"""
    interval = INTERVAL_MS[tf]
    rows = [("107", "400", "1", "2")] + [(50 - i, 60 - i, 40 - i, 45 - i) for i in range(count // 2)]
    rows += [(300 + i, 320 + i, 290 + i, 310 + i) for i in range(count // 2)]
    return [Kline(BASE + i * interval, D(str(o)), D(str(h)), D(str(l)), D(str(c)),
                  D("999"), D("99999")) for i, (o, h, l, c) in enumerate(rows)]


def test_no_look_ahead_bars_after_entry_never_change_the_result():
    past = scenario()
    with_future = {tf: bars + future_bars(tf) for tf, bars in past.items()}
    assert match_episode(episode(), with_future) == match_episode(episode(), past)

    # 入场越早, 能看到的越少: 把入场挪到停顿K收盘之前, 触发必须消失
    earlier = episode(entry_ms=BASE - INTERVAL_MS["5m"] + 1_000)
    assert match_episode(earlier, past).trigger == "无"
    assert match_episode(episode(), past).trigger == "5m 底分型+停顿, 入场前1根"


def test_bar_still_open_at_entry_is_excluded():
    bars = pullback_long(after=1)                      # 已收盘部分只有右K, 没有停顿
    forming = Kline(BASE, D("107"), D("130"), D("106.9"), D("129"), D("1"), D("1"))
    assert match_episode(episode(), {"5m": bars + [forming]}).trigger == "无"
    tail, reason = closed_before(bars + [forming], INTERVAL_MS["5m"], ENTRY)
    assert reason == "" and tail[-1].start_ms == BASE - INTERVAL_MS["5m"]


def test_outcome_data_is_never_read():
    base = episode("long", "107")
    expected = match_episode(base, scenario())
    for realized, exit_price in ((D("500"), "150"), (D("-500"), "60")):
        item = episode("long", "107")
        item.realized_pnl = realized
        item.status = "closed"
        item.closed_ms = ENTRY + 3_600_000
        item.fills.append(EpisodeFill(Execution(
            "e2", "o2", "linear", "BTCUSDT", "Sell", D("1"), D(exit_price), D(exit_price),
            D("0.05"), ENTRY + 3_600_000), "close"))
        assert match_episode(item, scenario()) == expected


def test_first_entry_fill_defines_the_entry_bar_even_with_later_adds():
    item = episode("long", "107")
    later = Execution("e3", "o3", "linear", "BTCUSDT", "Buy", D("1"), D("90"), D("90"),
                      D("0.05"), ENTRY + 7_200_000)
    item.fills.append(EpisodeFill(later, "add"))
    assert match_episode(item, scenario()) == match_episode(episode("long", "107"), scenario())


def test_inherited_position_is_described_as_unknown():
    item = episode()
    item.inherited_position = True
    fit = match_episode(item, scenario())
    assert (fit.trend_1h, fit.trend_4h, fit.alignment, fit.trigger, fit.setup) == (
        "unknown", "unknown", "未知", "未知", "未知")
    assert "继承仓位" in fit.evidence[0].detail

    empty = TradeEpisode("ep2", "linear", "BTCUSDT", "long", 0, ENTRY)
    assert match_episode(empty, scenario()).setup == "未知"


@pytest.mark.parametrize("klines", [
    {}, None,
    {"5m": [], "15m": [], "1h": [], "4h": []},
    {"5m": pullback_long()[-3:], "15m": consolidation_15m()[-2:], "1h": uptrend("1h")[-1:]},
    {"5m": future_bars("5m"), "4h": future_bars("4h")},            # 只有入场之后的K线
    {"2h": uptrend("1h")},                                           # 不认识的级别
])
def test_short_missing_or_odd_history_never_raises(klines):
    fit = match_episode(episode(), klines)
    assert isinstance(fit, SystemFit)
    assert fit.alignment == "未知" and fit.setup == "未知"
    assert all(item.tier == "reference" for item in fit.evidence)


def test_unsorted_and_duplicated_bars_give_the_same_result():
    past = scenario()
    shuffled = {tf: list(reversed(bars)) + bars[:5] for tf, bars in past.items()}
    assert match_episode(episode(), shuffled) == match_episode(episode(), past)


def test_flat_bars_do_not_break_any_check():
    flat = {tf: from_rows([("100", "100", "100", "100")] * 150, tf)
            for tf in ("5m", "15m", "1h", "4h")}
    fit = match_episode(episode("long", "100"), flat)
    assert fit.trigger == "无"
    assert evidence(fit, "imbalance").state is CheckState.UNKNOWN     # 实体中位数为 0, 无从比较


def test_every_label_is_backed_by_reference_evidence_and_serialises():
    fit = match_episode(episode(), scenario())
    keys = [item.key for item in fit.evidence]
    assert keys == ["entry", "trend_4h", "trend_1h", "alignment", "trigger", "pullback",
                    "structure_shift", "shift_retest", "imbalance", "consolidation", "setup"]
    assert all(item.tier == "reference" and item.detail for item in fit.evidence)
    assert evidence(fit, "alignment").value == fit.alignment
    assert evidence(fit, "trigger").value == fit.trigger
    assert evidence(fit, "setup").value == fit.setup
    payload = json.loads(json.dumps(asdict(fit), ensure_ascii=False, default=str))
    assert payload["setup"] == fit.setup
    assert payload["evidence"][0]["state"] == "na"               # 入场时刻: 说明行


def test_adapter_keeps_engine_row_shape():
    rows = to_engine_rows(pullback_long()[:2])
    assert set(rows[0]) == {"open_time", "open", "high", "low", "close", "volume"}
    assert all(isinstance(rows[0][key], float) for key in ("open", "high", "low", "close", "volume"))
    assert isinstance(rows[0]["open_time"], int)


# ---- 6. 补充分支 -----------------------------------------------------------

def test_two_overlapping_centres_are_reported_as_range(monkeypatch):
    import app.review.system_match as module

    def fake(rows, *args):
        prev = {"ZG": 110.0, "ZD": 104.0, "bis": 3, "start_time": rows[0]["open_time"],
                "end_time": rows[40]["open_time"]}
        last = {"ZG": 112.0, "ZD": 108.0, "bis": 4, "start_time": rows[60]["open_time"],
                "end_time": rows[-1]["open_time"]}
        return {"state": "range", "n_zs": 2, "last": last, "prev": prev, "pos": "inside"}

    monkeypatch.setattr(module, "trend_by_zhongshu", fake)
    fit = match_episode(episode(), {"4h": uptrend("4h")})
    assert fit.trend_4h == "range"
    assert "两个中枢区间有重叠" in evidence(fit, "trend_4h").detail


def test_trend_is_range_once_price_is_on_the_far_side_of_the_last_centre():
    # 两个上移的中枢之后急跌到后枢下方: 不再叫"上涨"(否则这时做多会被写成顺势), 记为无序
    bars = zigzag(UP_PIVOTS + [112], [14] * 9 + [10], "4h")
    fit = match_episode(episode(), {"4h": bars})
    assert fit.trend_4h == "range" and fit.alignment == "无序"
    detail = evidence(fit, "trend_4h").detail
    assert "价格已走到后枢的反方向一侧" in detail and "记为无序" in detail
    assert evidence(fit, "trend_4h").value == "range"
    # 旧口径仍可通过参数取回: 只按中枢位置
    engine = match_episode(episode(), {"4h": bars},
                           MatchParams(range_when_price_left_last_centre=False))
    assert engine.trend_4h == "up"


def test_centre_noise_filter_is_passed_through_to_the_engine():
    wide_only = MatchParams(zs_min_width_pct=50.0)        # 所有中枢都被过滤掉
    fit = match_episode(episode(), {"4h": uptrend("4h")}, wide_only)
    assert fit.trend_4h == "unknown"


def test_unknown_direction_is_described_not_guessed():
    item = episode()
    item.direction = "flat"
    fit = match_episode(item, scenario())
    assert (fit.alignment, fit.trigger, fit.setup) == ("未知", "未知", "未知")
    assert fit.trend_4h == "up"                           # 趋势本身与方向无关, 照常给出


def test_top_fractal_broken_before_short_entry_does_not_count():
    bars = mirror(pullback_long(after=2))
    rows = [(b.open, b.high, b.low, b.close) for b in bars]
    rows.append(("132.4", "135", "132.1", "134.8"))       # 新高 135 > 顶分型高点 134.4
    fit = match_episode(episode("short"), {"5m": from_rows(rows, "5m")})
    assert fit.trigger == "无"
    assert "被击穿(135)" in evidence(fit, "trigger").detail


def test_lower_high_without_break_and_reclaimed_long_shift_are_absent():
    no_break = zigzag([100, 120, 110, 116, 113], [10, 8, 6, 6], "15m")
    check = evidence(match_episode(episode("short", "113"), {"15m": no_break}), "structure_shift")
    assert check.state is CheckState.FAIL and "没有收盘跌破底1" in check.detail

    reclaimed = mirror(shift_short_15m(last_target=119, last_bars=8))
    check = evidence(match_episode(episode("long", "121"), {"15m": reclaimed}), "structure_shift")
    assert check.state is CheckState.FAIL and "跌回底2 下方" in check.detail


def test_chasing_the_break_with_all_evidence_known_is_outside_the_system():
    breaking = zigzag([100, 120, 110, 116, 104], [10, 8, 6, 8], "15m")    # 刚跌破, 还没反抽
    klines = {"4h": downtrend("4h"), "15m": breaking, "5m": straight("5m", 116, 104, 30)}
    fit = match_episode(episode("short", "104"), klines)
    assert (fit.alignment, fit.setup) == ("顺势", "系统外")
    assert "有 15m 结构转换但入场未回抽到位" in evidence(fit, "setup").detail
    assert "追着破位入场" in evidence(fit, "shift_retest").detail


def test_centre_already_left_by_a_stroke_is_not_consolidation():
    left = zigzag([100, 110, 102, 109, 101, 110, 103, 125, 118, 121],
                  [6, 6, 6, 6, 6, 6, 8, 6, 3], "15m")
    check = evidence(match_episode(episode("long", "121"), {"15m": left}), "consolidation")
    assert check.state is CheckState.FAIL and "已有一笔离开该中枢" in check.detail


def test_doji_bars_in_the_imbalance_window_are_skipped():
    bars = zigzag([100, 115], [30], "5m")
    rows = [(b.open, b.high, b.low, b.close) for b in bars]
    rows.append(("115.2", "116", "114.9", "115.2"))       # 十字星: 实体为 0
    check = evidence(match_episode(episode("short", "115"), {"5m": from_rows(rows, "5m")}),
                     "imbalance")
    assert check.state is CheckState.FAIL


def test_detail_times_follow_the_configured_timezone():
    beijing = evidence(match_episode(episode(), scenario()), "entry").detail
    bangkok = evidence(match_episode(episode(), scenario(), MatchParams(tz_offset_hours=7)),
                       "entry").detail
    assert "11-22 16:00(UTC+8)" in beijing
    assert "11-22 15:00(UTC+7)" in bangkok
