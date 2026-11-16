"""The daily review renders one question: "今天我有没有按规矩做?".

Report dicts here are hand-made to the shared contract shape:
per trade {"episode", "principles", "fit", "charts"}; per day {"score", "day_checks", "ledger"}.
"""
import asyncio
import json
import math
import re
from decimal import Decimal

import pytest

from app.review import narrator
from app.review.image import render_summary_png
from app.review.narrator import (Narrative, deterministic_narrative, generate_narrative,
                                 parse_or_fallback)
from app.review.report import (TELEGRAM_MAX_LINES, _aftermath, _chart_svg, _norm_bars,
                               deliver_review, reconciliation_reasons, render_html,
                               telegram_summary)

D = Decimal
T0 = 1_794_790_800_000  # 2026-11-16 09:00 北京时间
MIN = 60_000

# Keys and labels mirror app/review/principles.py (HARD_KEYS / LABELS).
R_STOP = ("sl_at_entry", "进场即设止损")
R_KEEP = ("sl_not_loosened", "止损没有被放宽")
R_ADD = ("no_averaging_down", "没有逆向加仓")
R_EXIT = ("exit_by_plan", "只有止盈与止损两种离场")
R_COST = ("early_exit_cost", "离场之后的走势")
R_HOLD = ("hold_time", "持仓时长")
R_FLIP = ("flip_after_exit", "反手")
R_REENTRY = ("revenge_reentry", "止损后立刻重进")
R_COUNT = ("trade_count", "当日笔数")


def check(rule, state, detail="", tier="hard", value=None):
    return {"key": rule[0], "label": rule[1], "tier": tier, "state": state,
            "detail": detail, "value": value}


def hard_checks(stop="pass", keep="pass", exit_="pass", add="pass", details=None):
    details = details or {}
    rows = [check(R_STOP, stop, details.get("stop", "")), check(R_KEEP, keep, details.get("keep", "")),
            check(R_ADD, add, details.get("add", "")), check(R_EXIT, exit_, details.get("exit", "")),
            check(R_COST, "na", "没有手动离场", tier="reference"),
            check(R_HOLD, "na", details.get("hold", "09:00 进场 → 11:00 离场，持仓 2小时"),
                  tier="reference", value=D("7200"))]
    return {row["key"]: row for row in rows}


def path_bars(start_ms, step_ms, waypoints):
    """Deterministic candles through (bar_count, target_price) waypoints."""
    bars, price, stamp, i = [], waypoints[0][1], start_ms, 0
    for count, target in waypoints[1:]:
        for n in range(1, count + 1):
            nxt = price + (target - price) / (count - n + 1)
            wiggle = abs(target) * 0.0006 * (1 + math.sin(i * 1.7))
            bars.append([stamp, round(price, 6), round(max(price, nxt) + wiggle, 6),
                         round(min(price, nxt) - wiggle, 6), round(nxt, 6)])
            price, stamp, i = nxt, stamp + step_ms, i + 1
    return bars


def trade(episode_id, symbol, direction, verdict, checks, *, entry, entry_ms, exits=(),
          sl=None, tp=None, rr=None, r=None, hold=None, pnl=None, status="closed",
          inherited=False, k1m=None, k15m=None, fit=None, entry_side=None):
    side = entry_side or ("Sell" if direction == "short" else "Buy")
    qty = D("1")
    fills = [] if inherited else [{"exec_id": f"{episode_id}-in", "action": "entry", "side": side,
                                   "time": entry_ms, "price": entry, "qty": qty}]
    for n, leg in enumerate(exits, 1):
        fills.append({"exec_id": f"{episode_id}-out{n}", "side": "Buy" if side == "Sell" else "Sell",
                      "action": "close" if n == len(exits) else "reduce",
                      "time": leg["time_ms"], "price": leg["price"], "qty": leg["qty"]})
    return {
        "episode": {"episode_id": episode_id, "symbol": symbol, "direction": direction,
                    "status": status, "inherited_position": inherited, "net_pnl": pnl,
                    "price_return_pct": None, "fills": fills},
        "principles": {"episode_id": episode_id, "verdict": verdict, "checks": checks,
                       "exits": tuple(exits), "planned_sl": sl, "planned_tp": tp,
                       "planned_rr": rr, "realized_r": r, "hold_seconds": hold},
        "fit": fit or {"episode_id": episode_id, "trend_1h": "down", "trend_4h": "down",
                       "alignment": "顺势", "trigger": "5m 上涨笔完成后跌破最近低点",
                       "setup": "顺势回调",
                       "evidence": (check(("trend_align", "入场方向与 1h/4h 一致"), "pass",
                                          "1h 下跌，4h 下跌", tier="reference"),)},
        "charts": {"15m": {"bars": k15m or [], "markers": []},
                   "1m": {"bars": k1m or [], "markers": []}},
    }


def leg(time_ms, price, kind, qty="1"):
    return {"time_ms": time_ms, "qty": D(qty), "price": D(str(price)), "kind": kind}


def tp_short():
    """Compliant: short BTC, target order filled."""
    k1 = path_bars(T0 - 40 * MIN, MIN, [(0, 68050), (40, 68000), (30, 68250), (90, 67000), (30, 67150)])
    k15 = path_bars(T0 - 80 * 15 * MIN, 15 * MIN, [(0, 69800), (80, 68000), (8, 67000), (4, 67200)])
    return trade("ep-btc", "BTCUSDT", "short", "合规", hard_checks(
        details={"stop": "入场后 4 秒挂出止损 68400", "exit": "止盈单 67000 成交"}),
        entry=D("68000"), entry_ms=T0, exits=[leg(T0 + 120 * MIN, 67000, "take_profit")],
        sl=D("68400"), tp=D("67000"), rr=D("2.5"), r=D("2.46"), hold=7200, pnl=D("98.40"),
        k1m=k1, k15m=k15)


def sl_long():
    """Compliant: long ETH, stop order filled. A loss, and still fully compliant."""
    start = T0 + 4 * 60 * MIN
    k1 = path_bars(start - 40 * MIN, MIN, [(0, 3490), (40, 3500), (20, 3512), (35, 3465), (30, 3440)])
    k15 = path_bars(start - 80 * 15 * MIN, 15 * MIN, [(0, 3380), (80, 3500), (4, 3465), (6, 3440)])
    return trade("ep-eth", "ETHUSDT", "long", "合规", hard_checks(
        details={"stop": "入场后 2 秒挂出止损 3465", "exit": "止损单 3465 成交"}),
        entry=D("3500"), entry_ms=start, exits=[leg(start + 55 * MIN, 3465, "stop_loss")],
        sl=D("3465"), tp=D("3570"), rr=D("2"), r=D("-1.03"), hold=3300, pnl=D("-36.10"),
        k1m=k1, k15m=k15,
        fit={"episode_id": "ep-eth", "trend_1h": "up", "trend_4h": "range", "alignment": "顺势",
             "trigger": "1m 次低点抬高", "setup": "顺势回调", "evidence": ()})


def mid_exit_short():
    """Broke the first principle: closed by hand, then price went on to the target."""
    start = T0 + 7 * 60 * MIN
    k1 = path_bars(start - 40 * MIN, MIN, [(0, 240.6), (40, 240), (25, 237.2), (10, 238.1),
                                           (47, 230.6), (20, 231.5)])
    k15 = path_bars(start - 80 * 15 * MIN, 15 * MIN, [(0, 252), (80, 240), (2, 237.6), (4, 230.6), (4, 231.4)])
    checks = hard_checks(exit_="fail", details={
        "stop": "入场后 6 秒挂出止损 243", "exit": "16:28 市价手动平仓，止盈 231 与止损 243 都未触发"})
    # principles reports the comparison as state "na" with an outcome code in ``value``
    checks["early_exit_cost"] = check(
        R_COST, "na",
        "16:28 手动离场 @237.6。之后价格在 17:19（离场后 51分钟）先到原止盈 231，没有先碰原止损 243。"
        "按原计划是 +3R，实际 +0.8R（不含手续费）",
        tier="reference", value="target_first")
    return trade("ep-sol", "SOLUSDT", "short", "中途下车", checks,
                 entry=D("240"), entry_ms=start, exits=[leg(start + 28 * MIN, 237.6, "manual")],
                 sl=D("243"), tp=D("231"), rr=D("3"), r=D("0.8"), hold=1680, pnl=D("23.50"),
                 k1m=k1, k15m=k15)


def no_stop_long():
    start = T0 + 10 * 60 * MIN
    k1 = path_bars(start - 40 * MIN, MIN, [(0, 0.2012), (40, 0.2), (30, 0.1984), (30, 0.2006)])
    checks = hard_checks(stop="fail", keep="na", exit_="fail", details={
        "stop": "整个持仓期没有任何止损单记录", "exit": "市价手动平仓"})
    return trade("ep-doge", "DOGEUSDT", "long", "无止损", checks,
                 entry=D("0.2"), entry_ms=start, exits=[leg(start + 60 * MIN, 0.2006, "manual")],
                 hold=3600, pnl=D("2.10"), k1m=k1,
                 fit={"episode_id": "ep-doge", "trend_1h": "down", "trend_4h": "down",
                      "alignment": "逆势", "trigger": "无", "setup": "系统外", "evidence": ()})


def inherited_position():
    checks = hard_checks(stop="na", keep="na", exit_="na", add="na")
    return trade("ep-xrp", "XRPUSDT", "long", "继承仓位不评价", checks,
                 entry=D("0"), entry_ms=T0, exits=[leg(T0 + 30 * MIN, 0.61, "unknown")],
                 pnl=None, inherited=True,
                 fit={"episode_id": "ep-xrp", "trend_1h": "unknown", "trend_4h": "unknown",
                      "alignment": "未知", "trigger": "无", "setup": "系统外", "evidence": ()})


def score_of(trades):
    by_verdict, broken = {}, {}
    for item in trades:
        verdict = item["principles"]["verdict"]
        by_verdict[verdict] = by_verdict.get(verdict, 0) + 1
        for row in item["principles"]["checks"].values():
            if row["tier"] == "hard" and row["state"] == "fail":
                slot = broken.setdefault(row["key"], {"key": row["key"], "label": row["label"], "count": 0})
                slot["count"] += 1
    return {
        "total": len(trades),
        "judged": sum(n for v, n in by_verdict.items() if v not in ("证据不足", "继承仓位不评价", "持仓中")),
        "compliant": by_verdict.get("合规", 0), "by_verdict": by_verdict,
        "most_broken": max(broken.values(), key=lambda row: row["count"]) if broken else None,
    }


def day(trades, *, status="reconciled", day_checks=(), net="62.30", fees="4.10", funding="0", **extra):
    return {
        "date": "2026-11-16", "window_label": "北京时间 2026-11-16 00:00–24:00",
        "status": status, "trades": list(trades), "score": score_of(trades),
        "day_checks": list(day_checks),
        "ledger": {"net_pnl": D(net), "closed_pnl": D(net) + D(fees), "fees": D(fees),
                   "funding": D(funding)},
        **extra,
    }


def article(html, number):
    start = html.index(f'id="trade-{number}"')
    return html[start:html.index("</article>", start)]


# --------------------------------------------------------------------- all compliant

def test_all_compliant_day_telegram_text():
    text = telegram_summary(day([tp_short(), sl_long()]))
    assert text == "\n".join([
        "<b>2026-11-16 实盘纪律 · 2 笔，2 笔合规</b>",
        "今日没有破规矩的交易",
        "1. BTCUSDT 空 · <b>合规</b> · 止盈触发 · 计划RR 2.5 · 实际 +2.46R",
        "2. ETHUSDT 多 · <b>合规</b> · 止损触发 · 计划RR 2 · 实际 -1.03R",
        "当日净盈亏 +62.30U（手续费 4.10U，资金费 +0.00U）",
    ])


def test_all_compliant_day_html_puts_the_score_first_and_money_last():
    html = render_html(day([tp_short(), sl_long()]))
    assert "<h1>今天我有没有按规矩做？</h1>" in html
    assert '<div class="big"><b>2</b><span>/ 2 笔合规</span></div>' in html
    assert '<p class="broken ok">今日没有破规矩的交易</p>' in html
    assert "离场之后（事后对照，不改变结论）" not in html
    assert html.index("规矩清单") < html.index('id="trade-1"') < html.index('id="trade-2"')
    assert html.index('id="trade-2"') < html.index('<section class="panel ledger">')
    assert "<script" not in html and "http" not in html.replace("http://www.w3.org", "")


def test_a_stopped_out_loss_is_shown_exactly_like_a_winning_compliant_trade():
    html = render_html(day([tp_short(), sl_long()]))
    win, loss = article(html, 1), article(html, 2)
    for card in (win, loss):
        assert '<span class="badge ok">合规</span>' in card
        hard = card[card.index("硬规矩"):card.index("计划与实际")]
        assert hard.count('<li class="pass">') == 4 and '<li class="fail">' not in card
        # the result line is neutral text placed after the checks and charts
        assert card.index("硬规矩") < card.index("</figure>") < card.index('<p class="result">')
    assert "结果（不参与纪律判定）：本笔已结盈亏 -36.10U" in loss
    assert "结果（不参与纪律判定）：本笔已结盈亏 +98.40U" in win


def test_rule_checklist_counts_each_rule_across_trades():
    html = render_html(day([tp_short(), mid_exit_short(), no_stop_long()]))
    row = html[html.index("<span>只有止盈与止损两种离场</span>"):]
    row = row[:row.index("</li>")]
    assert '<span class="c-pass">✓ 1</span>' in row
    assert '<span class="c-fail">✗ 2</span>' in row
    assert '<span class="c-unknown zero">? 0</span>' in row


# --------------------------------------------------------------------- mid-trade exit

def test_mid_trade_exit_telegram_names_the_broken_rule_not_the_profit():
    lines = telegram_summary(day([tp_short(), mid_exit_short()])).split("\n")
    assert lines[0] == "<b>2026-11-16 实盘纪律 · 2 笔，1 笔合规</b>"
    assert lines[1] == "今日最常破的规矩：只有止盈与止损两种离场（1 次）"
    assert lines[3] == ("2. SOLUSDT 空 · <b>中途下车</b> · 手动平仓 · 计划RR 3 · 实际 +0.80R"
                        " · 未过：只有止盈与止损两种离场")
    assert lines[-1].startswith("当日净盈亏 ")


def test_mid_trade_exit_card_shows_cost_plan_and_what_price_did_next():
    html = render_html(day([tp_short(), mid_exit_short()]))
    card = article(html, 2)
    assert '<span class="badge bad">中途下车</span>' in card
    assert '<li class="fail"><i class="c-fail">✗</i><div><b>只有止盈与止损两种离场</b>' in card
    assert ('<p class="cost"><b>离场之后（事后对照，不改变结论）</b>16:28 手动离场 @237.6。之后价格在 17:19（离场后 51分钟）'
            '先到原止盈 231，没有先碰原止损 243。按原计划是 +3R，实际 +0.8R（不含手续费）</p>') in card
    assert "<dt>计划止损</dt><dd>243</dd>" in card and "<dt>计划止盈</dt><dd>231</dd>" in card
    assert "<dt>实际离场均价</dt><dd>237.6</dd>" in card
    assert "<dt>实际结果（R）</dt><dd>+0.80R</dd>" in card
    assert "离场均价距计划止盈还差 6.6（2.78%）" in card
    assert '<li class="k-manual"><time>16:28:00</time><span class="kind">手动平仓</span>' in card
    # both charts: manual marker, both planned levels, shaded path after the exit, touch ring
    assert card.count('data-kind="manual"') == 2
    assert card.count('class="level"') == 4
    assert card.count('class="after"') == 2 and card.count('class="touch"') == 2
    # the cost line already tells what happened next, so the chart adds no second sentence
    assert 'class="after-note"' not in card
    assert "1h 下跌" in card and "形态：顺势回调" in card and "触发：5m 上涨笔完成后跌破最近低点" in card
    # the cost check is shown once, not repeated among the hard rules or reference items
    assert card.count("离场之后（事后对照，不改变结论）") == 1 and "中途下车的代价" not in card


def test_without_a_cost_check_the_chart_itself_says_what_price_did_next():
    item = mid_exit_short()
    del item["principles"]["checks"]["early_exit_cost"]
    card = article(render_html(day([item])), 1)
    assert card.count('class="after-note"') == 1
    assert ('<p class="after-note">手动平仓后，价格在 11-16 17:19 触及原止盈 231，'
            '此前未触及原止损（按1分钟K线）</p>') in card
    item["charts"]["1m"]["bars"] = []
    card = article(render_html(day([item])), 1)
    assert "触及原止盈 231，此前未触及原止损（按15分钟K线）" in card


def test_cost_and_reference_rows_never_read_as_violations():
    card = article(render_html(day([tp_short()])), 1)
    assert "离场之后（事后对照，不改变结论）" not in card            # "没有手动离场" is not a cost line
    other = card[card.index("其他检查项（参考）"):]
    assert ('<li class="info"><i class="c-info">·</i><div><b>持仓时长</b>'
            '<small>09:00 进场 → 11:00 离场，持仓 2小时</small></div><em class="c-info"></em></li>') in other


def test_times_follow_the_report_time_zone():
    report = day([mid_exit_short()])
    assert "图表与时间线为北京时间。" in render_html(report)
    report["tz_offset_hours"] = 7
    html = render_html(report)
    assert "图表与时间线为曼谷时间。" in html
    assert '<li class="k-manual"><time>15:28:00</time>' in html


def test_compliant_exits_get_no_after_exit_shading():
    card = article(render_html(day([tp_short()])), 1)
    assert 'class="after"' not in card and 'class="after-note"' not in card
    assert card.count('data-kind="take_profit"') == 2 and card.count('data-kind="entry"') == 2


# --------------------------------------------------------------------- no stop

def test_no_stop_trade_is_flagged_even_though_it_made_money():
    report = day([no_stop_long()])
    lines = telegram_summary(report).split("\n")
    assert lines[0] == "<b>2026-11-16 实盘纪律 · 1 笔，0 笔合规</b>"
    assert lines[2] == ("1. DOGEUSDT 多 · <b>无止损</b> · 手动平仓 · 计划RR — · 实际 —"
                        " · 未过：进场即设止损")
    card = article(render_html(report), 1)
    assert '<span class="badge bad">无止损</span>' in card
    assert "<dt>计划止损</dt><dd>无记录</dd>" in card
    assert "<small>整个持仓期没有任何止损单记录</small>" in card
    assert 'class="level"' not in card
    assert '<span class="chip flag">逆势</span>' in card
    assert '<span class="chip flag">形态：系统外</span>' in card
    assert '<p class="rule">对应准则：进场即设止损，提前规划允许承受的最坏结果。</p>' in card


# --------------------------------------------------------------------- inherited

def test_inherited_position_is_listed_but_not_judged():
    report = day([tp_short(), inherited_position()])
    lines = telegram_summary(report).split("\n")
    assert lines[0] == "<b>2026-11-16 实盘纪律 · 2 笔，1 笔合规</b>"
    assert lines[2] == "不计入评价：继承仓位不评价 1"
    assert lines[4] == "2. XRPUSDT 多 · <b>继承仓位不评价</b> · 离场方式未知"
    html = render_html(report)
    assert '<p class="muted">参与评价 1 笔</p>' in html
    assert '<li class="chip na">继承仓位不评价 1</li>' in html
    card = article(html, 2)
    assert '<span class="badge na">继承仓位不评价</span>' in card
    hard = card[card.index("硬规矩"):card.index("计划与实际")]
    assert hard.count('<em class="c-na">不适用</em>') == 4
    assert "K线数据缺失" in card


# --------------------------------------------------------------------- empty day

def test_empty_day_says_there_were_no_fills():
    report = day([], net="0", fees="0")
    assert telegram_summary(report).split("\n") == [
        "<b>2026-11-16 实盘纪律 · 今天没有成交</b>",
        "没有交易需要评价",
        "当日净盈亏 +0.00U（手续费 0.00U，资金费 +0.00U）",
    ]
    html = render_html(report)
    assert '<div class="big"><b>今天没有成交</b></div>' in html
    assert "<article" not in html


# --------------------------------------------------------------------- unreconciled

def unreconciled_day():
    return day([tp_short(), mid_exit_short()], status="mismatch", funding_present=True,
               closed_pnl_difference=90.0, grace_elapsed=True, position_snapshot_stable=True,
               unmatched_closed_orders=[], missing_closed_pnl_orders=["order-9"])


def test_unreconciled_day_is_refused_as_approved_and_says_why():
    report = unreconciled_day()
    assert reconciliation_reasons(report) == [
        "窗口内有资金费，暂不能逐笔归属",
        "回合盈亏与交易所已结盈亏相差 90.00U",
        "1 笔平仓成交缺少已结盈亏记录",
    ]
    lines = telegram_summary(report).split("\n")
    assert lines[:5] == [
        "<b>⚠ 2026-11-16 未核准 · 对账未通过</b>",
        "· 窗口内有资金费，暂不能逐笔归属",
        "· 回合盈亏与交易所已结盈亏相差 90.00U",
        "· 1 笔平仓成交缺少已结盈亏记录",
        "已识别 2 笔，以下纪律结论仅供排查，不计入当日成绩：",
    ]
    assert lines[-1] == "净盈亏未通过对账，不给数字"
    text = "\n".join(lines)
    assert "笔合规" not in text and "62.30" not in text
    html = render_html(report)
    assert "不是已核准日报" in html
    assert "<li>回合盈亏与交易所已结盈亏相差 90.00U</li>" in html
    assert "初步结果 · 未核准" in html and "账本未通过对账，不给数字。" in html
    # 三种输出同一口径：没对上账，HTML 里也不出现任何金额（账本和逐笔的已结盈亏都不显示）
    assert "62.30" not in html and "当日净盈亏" not in html and "本笔已结盈亏" not in html


def test_missing_status_is_never_treated_as_reconciled():
    report = day([tp_short()])
    del report["status"]
    assert telegram_summary(report).startswith("<b>⚠ 2026-11-16 未核准")
    assert reconciliation_reasons(report) == ["报告缺少对账状态"]
    assert reconciliation_reasons(day([tp_short()])) == []


# --------------------------------------------------------------------- day-level flags, limits

def test_day_level_flags_follow_the_trades_and_precede_money():
    flags = [check(R_FLIP, "fail", "SOLUSDT 平空后 3 分钟开多", tier="day", value=D("1")),
             check(R_REENTRY, "fail", "ETHUSDT 止损后 5 分钟同向重进", tier="day", value=D("1")),
             check(R_COUNT, "na", "当日新开 2 笔（未设置每日笔数上限）", tier="day", value=D("2"))]
    report = day([tp_short(), mid_exit_short()], day_checks=flags)
    lines = telegram_summary(report).split("\n")
    assert lines[-3:-1] == ["⚠ 当日：反手（SOLUSDT 平空后 3 分钟开多）",
                            "⚠ 当日：止损后立刻重进（ETHUSDT 止损后 5 分钟同向重进）"]
    assert lines[-1].startswith("当日净盈亏 ")
    html = render_html(report)
    assert ('<li><span>反手<small>SOLUSDT 平空后 3 分钟开多</small></span>'
            '<span class="st c-fail">✗ 违规</span></li>') in html
    assert "<li><span>当日笔数<small>当日新开 2 笔（未设置每日笔数上限）</small></span></li>" in html
    calm = render_html(day([tp_short()], day_checks=[check(R_FLIP, "pass", "没有平仓后 30 分钟内反向开仓",
                                                           tier="day")]))
    assert '<span class="st c-pass">✓ 没有违规</span>' in calm


def test_telegram_never_exceeds_the_line_budget_and_keeps_violations_visible():
    trades = [tp_short() for _ in range(20)] + [mid_exit_short(), no_stop_long()]
    flags = [check(R_FLIP, "fail", "x", tier="day")] * 3
    lines = telegram_summary(day(trades, day_checks=flags)).split("\n")
    assert len(lines) <= TELEGRAM_MAX_LINES
    assert any("<b>中途下车</b>" in line for line in lines)
    assert any("<b>无止损</b>" in line for line in lines)
    assert any(line.startswith("另有 10 笔未列出（其中合规 10 笔）") for line in lines)
    assert lines[-1].startswith("当日净盈亏 ")


def test_report_survives_a_json_round_trip_unchanged():
    report = day([tp_short(), mid_exit_short(), no_stop_long(), inherited_position()])
    restored = json.loads(json.dumps(report, default=str, ensure_ascii=False))
    assert telegram_summary(restored) == telegram_summary(report)
    assert render_html(restored) == render_html(report)


def test_score_is_computed_when_the_report_carries_none():
    report = day([tp_short(), mid_exit_short()])
    expected = telegram_summary(report)
    del report["score"]
    assert telegram_summary(report) == expected


# --------------------------------------------------------------------- legacy shape

def test_legacy_flat_episode_report_still_renders():
    """The current CLI still writes flat episodes until it is rewired to the new shape."""
    report = {
        "date": "2026-08-27", "window_label": "北京时间 2026-08-27 00:00–24:00",
        "status": "reconciled", "net_pnl": 12.34, "closed_pnl": 15.0, "fees": 2.0, "funding": -0.66,
        "episodes": [{
            "episode_id": "ep-1", "symbol": "BTCUSDT", "direction": "short", "verdict": "部分合规",
            "status": "closed", "net_pnl": 12.34, "price_return_pct": 1.2,
            "narrative": {"summary": "方向正确，但入场偏早。", "reasons": ["15m趋势转换：未完成"],
                          "next_action": "等待15m结构确认。", "source": "ai"},
            "evidence": [{"label": "15m趋势转换", "state": "present", "group": "hard"}],
            "fills": [{"exec_id": "entry-1", "action": "entry", "time": 1000, "price": 100, "qty": 2},
                      {"exec_id": "exit-1", "action": "close", "time": 61_000, "price": 98.8, "qty": 2}],
            "k1m": [[0, 99, 101, 98, 100], [60_000, 100, 101, 98, 99]],
            "k15m": [[0, 95, 102, 94, 100], [900_000, 100, 101, 96, 97]],
        }],
        "verdict_counts": {"部分合规": 1}, "tomorrow_action": "等待15m结构确认。",
    }
    text = telegram_summary(report)
    assert text.split("\n")[0] == "<b>2026-08-27 实盘纪律 · 1 笔，0 笔合规</b>"
    assert "<b>部分合规</b>" in text and text.endswith("当日净盈亏 +12.34U（手续费 2.00U，资金费 -0.66U）")
    html = render_html(report)
    assert '<span class="badge unk">部分合规</span>' in html
    assert "方向正确，但入场偏早。" in html and "<b>15m趋势转换</b>" in html
    assert html.count('data-exec-id="entry-1"') == 3            # both charts + the fills table
    assert html.count('data-kind="unknown"') == 2 and html.count('data-exec-id="exit-1"') == 1


# --------------------------------------------------------------------- real contract types

def test_renders_asdict_of_the_real_contract_dataclasses():
    principles = pytest.importorskip("app.review.principles")
    system_match = pytest.importorskip("app.review.system_match")
    from dataclasses import asdict
    Check, State = principles.Check, principles.CheckState
    labels = principles.LABELS
    states = {"sl_at_entry": State.PASS, "sl_not_loosened": State.UNKNOWN,
              "no_averaging_down": State.PASS, "exit_by_plan": State.FAIL}
    checks = {key: Check(key, labels[key], "hard", state, f"{key} 的依据")
              for key, state in states.items()}
    checks["early_exit_cost"] = Check("early_exit_cost", labels["early_exit_cost"], "reference",
                                      State.NA, "之后价格先到原止盈 231", "target_first")
    review = principles.PrincipleReview(
        "ep-sol", "中途下车", checks,
        (principles.ExitLeg(T0 + 28 * MIN, D("1"), D("237.6"), "manual"),),
        D("243"), D("231"), D("3"), D("0.8"), 1680)
    fit = system_match.SystemFit(
        "ep-sol", "down", "range", "顺势", "无", "系统外",
        (Check("alignment", "顺逆势", "reference", State.PASS, "做空; 1h 下跌"),))
    base = mid_exit_short()
    base["episode"]["fills"][1]["time"] = T0 + 28 * MIN
    base["episode"]["fills"][0]["time"] = T0
    report = day([{"episode": base["episode"], "principles": asdict(review), "fit": asdict(fit),
                   "charts": {}}])
    report["score"] = principles.day_score([review])
    report["day_checks"] = [asdict(Check("flip_after_exit", labels["flip_after_exit"], "day",
                                         State.FAIL, "SOLUSDT 平空后 3 分钟开多", D("1")))]
    lines = telegram_summary(report).split("\n")
    assert lines[0] == "<b>2026-11-16 实盘纪律 · 1 笔，0 笔合规 · 当日层面 1 项违规</b>"
    assert lines[1] == f"今日最常破的规矩：{labels['exit_by_plan']}（1 次）"
    assert lines[2] == (f"1. SOLUSDT 空 · <b>中途下车</b> · 手动平仓 · 计划RR 3 · 实际 +0.80R"
                        f" · 未过：{labels['exit_by_plan']}")
    assert lines[3] == "⚠ 当日：反手（SOLUSDT 平空后 3 分钟开多）"
    html = render_html(report)
    assert '<p class="cost"><b>离场之后（事后对照，不改变结论）</b>之后价格先到原止盈 231</p>' in html
    assert '<em class="c-unknown">未知</em>' in html and '<em class="c-fail">未过</em>' in html
    assert '<li class="chip bad">中途下车 1</li>' in html and "合规 0" not in html
    assert '<span class="chip">4h 无序</span>' in html
    assert render_html(json.loads(json.dumps(report, default=str, ensure_ascii=False))) == html


# --------------------------------------------------------------------- escaping

HOSTILE = '<script>alert(1)</script>"\'><img src=x onerror=alert(2)>&'


def hostile_day():
    item = mid_exit_short()
    item["episode"]["symbol"] = HOSTILE
    item["episode"]["direction"] = HOSTILE
    item["episode"]["episode_id"] = HOSTILE
    item["episode"]["fills"][0]["exec_id"] = HOSTILE
    item["episode"]["fills"][0]["action"] = HOSTILE
    item["principles"]["verdict"] = HOSTILE
    item["principles"]["exits"] = (leg(item["principles"]["exits"][0]["time_ms"], 237.6, HOSTILE),)
    for row in item["principles"]["checks"].values():
        row["label"] = HOSTILE + row["label"]
        row["detail"] = HOSTILE
    item["principles"]["planned_sl"] = HOSTILE
    item["fit"].update(trend_1h=HOSTILE, alignment=HOSTILE, trigger=HOSTILE, setup=HOSTILE)
    item["charts"]["1m"]["markers"] = [{"time_ms": item["episode"]["fills"][0]["time"],
                                        "price": "240", "kind": HOSTILE, "exec_id": HOSTILE}]
    report = day([item], day_checks=[check((HOSTILE, HOSTILE), "fail", HOSTILE, tier="day")])
    report["date"] = HOSTILE
    report["window_label"] = HOSTILE
    report["score"]["most_broken"] = {"key": HOSTILE, "label": HOSTILE, "count": 1}
    report["score"]["by_verdict"] = {HOSTILE: 1}
    report["ledger"]["net_pnl"] = HOSTILE
    return report


def test_html_escapes_every_dynamic_string():
    for report in (hostile_day(), {**hostile_day(), "status": HOSTILE}):
        html = render_html(report)
        assert "<script" not in html and "<img" not in html
        assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
        # no attribute can be broken out of: every quote from the payload is an entity
        assert '"\'>' not in html and "onerror=alert(2)>" not in html
        tags = set(re.findall(r"</?([a-zA-Z0-9]+)", html))
        assert tags <= {"html", "head", "meta", "title", "style", "body", "main", "header", "p", "h1",
                        "h2", "h3", "div", "section", "article", "b", "span", "ul", "ol", "li", "i",
                        "small", "em", "dl", "dt", "dd", "time", "figure", "figcaption", "svg",
                        "rect", "path", "text", "line", "circle", "g", "details", "summary",
                        "table", "thead", "tbody", "tr", "th", "td", "footer"}


def test_telegram_escapes_every_dynamic_string():
    for report in (hostile_day(), {**hostile_day(), "status": "mismatch",
                                   "reconciliation_reasons": [HOSTILE]}):
        text = telegram_summary(report)
        assert set(re.findall(r"<[^>]*>", text)) <= {"<b>", "</b>"}
        assert "&lt;script&gt;" in text


# --------------------------------------------------------------------- charts

def test_chart_keeps_working_with_only_bars_and_fills():
    bars = [[0, 99, 101, 98, 100], [60_000, 100, 101, 98, 99]]
    fills = [{"exec_id": "entry-1", "action": "entry", "time": 1000, "price": 100, "qty": 2},
             {"exec_id": "exit-1", "action": "close", "time": 61_000, "price": 98.8, "qty": 2}]
    svg = _chart_svg(bars, fills, "1m")
    assert svg.startswith("<svg") and svg.endswith("</svg>")
    assert 'data-exec-id="entry-1"' in svg and 'data-exec-id="exit-1"' in svg
    assert "K线数据缺失" in _chart_svg([], fills, "1m")
    assert "K线数据缺失" in _chart_svg([["x", None]], fills, "1m")


def test_chart_marks_each_exit_kind_and_pins_far_levels_to_the_edge():
    bars = path_bars(0, MIN, [(0, 100), (60, 100), (60, 99)])
    fills = [{"exec_id": "in", "action": "entry", "side": "Sell", "time": 30 * MIN, "price": 100, "qty": 3}]
    exits = [leg(70 * MIN, 99.6, "take_profit"), leg(80 * MIN, 99.4, "trailing_stop"),
             leg(90 * MIN, 99.2, "stop_loss"), leg(100 * MIN, 99.1, "liquidation")]
    svg = _chart_svg(bars, fills, "1m", exits=exits, planned_sl=100.5, planned_tp=50, direction="short")
    for kind in ("entry", "take_profit", "trailing_stop", "stop_loss", "liquidation"):
        assert f'data-kind="{kind}"' in svg
    assert "计划止损</text>" in svg and "计划止盈↓图外</text>" in svg
    assert 'class="after"' not in svg


def test_long_trades_are_merged_into_fewer_candles_and_say_so():
    bars = path_bars(0, MIN, [(0, 100), (900, 104)])
    fills = [{"exec_id": "in", "action": "entry", "time": 40 * MIN, "price": 100.2, "qty": 1}]
    svg = _chart_svg(bars, fills, "1m", exits=[leg(880 * MIN, 103.9, "take_profit")], direction="long")
    assert "每根合并 6 根" in svg


@pytest.mark.parametrize("direction,stop,target,path,outcome", [
    ("short", 103.0, 95.0, [(0, 100), (5, 100), (10, 94)], "target"),
    ("short", 103.0, 95.0, [(0, 100), (5, 100), (10, 104)], "stop"),
    ("long", 97.0, 105.0, [(0, 100), (5, 100), (10, 106)], "target"),
    ("long", 97.0, 105.0, [(0, 100), (5, 100), (10, 96)], "stop"),
    ("long", 97.0, 105.0, [(0, 100), (5, 100), (10, 101)], "none"),
])
def test_after_manual_exit_scan_stops_at_first_touch(direction, stop, target, path, outcome):
    rows = _norm_bars(path_bars(0, MIN, path))
    result = _aftermath(rows, 5 * MIN + 1, direction, stop, target)
    assert result["outcome"] == outcome
    assert all(row[0] >= 5 * MIN + 1 for row in rows[result["index"]:])


def test_after_manual_exit_edge_cases():
    rows = _norm_bars([[0, 100, 100, 100, 100], [MIN, 100, 106, 96, 100]])
    assert _aftermath(rows, 1, "long", 97.0, 105.0)["outcome"] == "both"
    assert _aftermath(rows, 5 * MIN, "long", 97.0, 105.0) == {"outcome": "no_data"}
    assert _aftermath(rows, 1, "long", None, None) is None
    assert _aftermath(rows, 1, "", 97.0, 105.0) is None


# --------------------------------------------------------------------- image

@pytest.mark.parametrize("build", [
    lambda: day([tp_short(), sl_long()]),
    lambda: day([tp_short(), mid_exit_short(), no_stop_long(), inherited_position()],
                day_checks=[check(R_FLIP, "fail", "SOLUSDT 平空后 3 分钟开多", tier="day")]),
    lambda: day([], net="0", fees="0"),
    unreconciled_day,
    hostile_day,
])
def test_summary_image_is_valid_png(tmp_path, build):
    path = render_summary_png(build(), tmp_path / "summary.png")
    assert path.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")


# --------------------------------------------------------------------- narrator

def test_default_narrative_is_deterministic_and_needs_no_ai_tool(monkeypatch):
    monkeypatch.delenv(narrator.AI_ENV_FLAG, raising=False)

    def forbidden(*args, **kwargs):
        raise AssertionError("AI tool must not be looked up or run by default")

    monkeypatch.setattr(narrator.shutil, "which", forbidden)
    story = generate_narrative(mid_exit_short()["principles"], runner=forbidden)
    assert story == Narrative(
        summary="本笔纪律结论为“中途下车”。",
        reasons=("只有止盈与止损两种离场：16:28 市价手动平仓，止盈 231 与止损 243 都未触发",),
        next_action="对应准则：只有止盈触发或止损触发两种离场方式，持仓期杂波不得作为平仓依据。",
        confidence="high", source="deterministic")


def test_ai_path_falls_back_when_no_tool_is_installed(monkeypatch):
    monkeypatch.setattr(narrator.shutil, "which", lambda name: None)
    story = generate_narrative(no_stop_long()["principles"], use_ai=True)
    assert story.source == "deterministic"
    assert story.reasons[0] == "进场即设止损：整个持仓期没有任何止损单记录"


def test_narrative_ignores_profit_and_loss():
    win, loss = tp_short()["principles"], sl_long()["principles"]
    assert deterministic_narrative(win).summary == deterministic_narrative(loss).summary
    assert deterministic_narrative(win).next_action == deterministic_narrative(loss).next_action
    flipped = {**mid_exit_short()["principles"], "realized_r": D("-5")}
    assert deterministic_narrative(flipped) == deterministic_narrative(mid_exit_short()["principles"])


def ai_reply(**fields):
    base = {"summary": "s", "reasons": ["只有止盈与止损两种离场：手动平仓"], "next_action": "对照第一准则",
            "confidence": "high"}
    return json.dumps({**base, **fields}, ensure_ascii=False)


def test_ai_may_rephrase_reasons_but_never_the_verdict():
    review = mid_exit_short()["principles"]
    ok = parse_or_fallback(ai_reply(summary="其实这笔很好，算合规"), review)
    assert ok.source == "ai" and ok.summary == "本笔纪律结论为“中途下车”。"
    assert ok.reasons == ("只有止盈与止损两种离场：手动平仓",)
    rejected = [
        ai_reply(reasons=["只有止盈与止损两种离场：赚钱了，其实算合规"]),      # names another verdict
        ai_reply(next_action="这笔应判为证据不足"),                        # names another verdict
        ai_reply(reasons=["这笔赚钱了所以没问题"]),                        # does not cite a check
        ai_reply(next_action="预计还会下跌，建议做空"),                    # forecast / advice
        ai_reply(reasons=["只有止盈与止损两种离场：" + "长" * 200]),           # unbounded text
        ai_reply(verdict="合规"),                                          # extra field
        ai_reply(reasons="只有止盈与止损两种离场"),                            # wrong type
        "not json",
    ]
    for raw in rejected:
        assert parse_or_fallback(raw, review).source == "deterministic", raw


def test_ai_runner_output_is_bounded_and_failures_do_not_block():
    review = mid_exit_short()["principles"]

    class Result:
        returncode = 0
        stdout = "```json\n" + ai_reply() + "\n```"

    assert generate_narrative(review, executable="tool", runner=lambda *a, **k: Result()).source == "ai"

    def boom(*args, **kwargs):
        raise TimeoutError

    assert generate_narrative(review, executable="tool", runner=boom).source == "deterministic"


def test_prompt_carries_checks_and_fixed_verdict_but_no_money():
    item = mid_exit_short()
    prompt = narrator.build_prompt(item["principles"])
    assert '"fixed_verdict": "中途下车"' in prompt and "只有止盈与止损两种离场" in prompt
    assert "23.50" not in prompt and "realized_r" not in prompt and "pnl" not in prompt.lower()


# --------------------------------------------------------------------- delivery

class Bot:
    def __init__(self, photo=True, text=True, document=True):
        self.calls, self.captions = [], []
        self.ok = {"photo": photo, "text": text, "document": document}

    async def send_photo(self, path, caption=""):
        self.calls.append("photo"); self.captions.append(caption)
        return {"ok": self.ok["photo"]}

    async def notify(self, text):
        self.calls.append("text"); self.captions.append(text)
        return {"ok": self.ok["text"]}

    async def send_document(self, path, caption=""):
        self.calls.append("document")
        return {"ok": self.ok["document"]}


def deliver(tmp_path, bot, report):
    html_path = tmp_path / "report.html"
    html_path.write_text(render_html(report), encoding="utf-8")
    png_path = render_summary_png(report, tmp_path / "summary.png")
    asyncio.run(deliver_review(bot, report, png_path, html_path))


def test_successful_photo_carries_the_summary_and_still_sends_the_full_html(tmp_path):
    bot, report = Bot(), day([tp_short(), mid_exit_short()])
    deliver(tmp_path, bot, report)
    assert bot.calls == ["photo", "document"]
    assert bot.captions[0] == telegram_summary(report)


def test_photo_failure_falls_back_to_text_and_document(tmp_path):
    bot = Bot(photo=False)
    deliver(tmp_path, bot, day([tp_short()]))
    assert bot.calls == ["photo", "text", "document"]


def test_summary_longer_than_a_caption_is_sent_as_its_own_message(tmp_path):
    report = day([mid_exit_short() for _ in range(14)])
    summary = telegram_summary(report)
    assert len(summary) > 1000
    bot = Bot()
    deliver(tmp_path, bot, report)
    assert bot.calls == ["photo", "text", "document"]
    assert bot.captions == [summary.split("\n")[0], summary]


def test_all_telegram_delivery_paths_failing_is_an_error(tmp_path):
    with pytest.raises(RuntimeError):
        deliver(tmp_path, Bot(photo=False, text=False, document=False), day([tp_short()]))
