"""每日实盘复盘总装（app/review/daily.py）的端到端测试。

全部走真实的 BybitReadClient（GET + 路径白名单 + 只读 Key 校验），只把它的传输层换成
回放示例应答的 FixtureExchange：不联网、不需要任何密钥。
运行: PYTHONIOENCODING=utf-8 .venv/Scripts/python -m pytest tests/test_bybit_daily_review_live.py -q
"""
import asyncio
import copy
import importlib.util
import json
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest

from app.review import daily
from app.review.bybit_client import READ_PATHS, BybitReadClient
from app.review.fixture_exchange import FixtureExchange
from app.review.principles import VERDICTS

FIXTURES = Path(__file__).parent / "fixtures" / "bybit_review"
DAY = "2026-11-16"

_spec = importlib.util.spec_from_file_location("make_day_full", FIXTURES / "make_day_full.py")
kit = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(kit)


def settings(tmp_path, **changes):
    base = daily.ReviewSettings(db_path=tmp_path / "review.db", output_dir=tmp_path / "out",
                                kline_pause_seconds=0.0)
    return replace(base, **changes)


def build(payload, cfg, day=DAY, now_ms=None, stored=None):
    exchange = FixtureExchange(payload)
    client = BybitReadClient("fixture", "fixture", requester=exchange.requester)
    now_ms = exchange.now_ms if now_ms is None else now_ms
    request = daily.make_request(day, cfg, now_ms)
    report, _ = asyncio.run(daily.build_report(client, request, cfg, now_ms=now_ms,
                                               stored_orders=stored, source="fixture"))
    return report, exchange


def by_symbol(report):
    return {(row["episode"]["symbol"], row["episode"]["direction"]): row for row in report["trades"]}


def one_trade(*, sl="239.40", tp="247.00", exit_price="239.38", stop_hit=True, entry_at=None,
              exit_at=None):
    """SOLUSDT 多单一笔：进场单带(或不带)止损止盈；止损触发离场，或手动市价平仓。"""
    entry_at = entry_at or kit.at(14, 46, 20)
    exit_at = exit_at or kit.at(15, 31, 44)
    orders = [kit.entry_order("d-entry", "SOLUSDT", "Buy", "12", entry_at, "241.90", sl=sl, tp=tp)]
    entry = kit.execution("d-entry-1", "d-entry", "SOLUSDT", "Buy", "12", "241.90", entry_at + 4)
    if stop_hit:
        orders.append(kit.tpsl("d-sl", "SOLUSDT", "Sell", "12", entry_at + 6, exit_at + 5,
                               "StopLoss", sl, 2, avg=exit_price))
        if tp:
            orders.append(kit.tpsl("d-tp", "SOLUSDT", "Sell", "12", entry_at + 6, exit_at + 9,
                                   "TakeProfit", tp, 1))
        close = kit.execution("d-sl-1", "d-sl", "SOLUSDT", "Sell", "12", exit_price, exit_at,
                              closedSize="12", stopOrderType="StopLoss",
                              createType="CreateByStopLoss")
    else:
        if sl:
            orders.append(kit.tpsl("d-sl", "SOLUSDT", "Sell", "12", entry_at + 6, exit_at + 5,
                                   "StopLoss", sl, 2))
        if tp:
            orders.append(kit.tpsl("d-tp", "SOLUSDT", "Sell", "12", entry_at + 6, exit_at + 9,
                                   "TakeProfit", tp, 1))
        orders.append(kit.close_order("d-close", "SOLUSDT", "Sell", "12", exit_at - 50, exit_price))
        close = kit.execution("d-close-1", "d-close", "SOLUSDT", "Sell", "12", exit_price, exit_at,
                              closedSize="12", createType="CreateByClosing")
    return orders, [("SOLUSDT", entry, close)]


# --------------------------------------------------------------------- 示例日：全链路

@pytest.fixture(scope="module")
def full_day(tmp_path_factory):
    payload = json.loads((FIXTURES / "day_full.json").read_text(encoding="utf-8"))
    return build(payload, settings(tmp_path_factory.mktemp("full")))


def test_fixture_day_gives_one_of_each_verdict_the_owner_asked_for(full_day):
    report, _ = full_day
    verdicts = [(row["episode"]["symbol"], row["episode"]["direction"], row["principles"]["verdict"])
                for row in report["trades"]]
    assert verdicts == [("BTCUSDT", "short", "合规"), ("ETHUSDT", "long", "中途下车"),
                        ("SOLUSDT", "short", "无止损"), ("SOLUSDT", "long", "合规")]
    assert report["score"]["total"] == 4 and report["score"]["compliant"] == 2
    assert report["score"]["judged"] == 4
    assert report["score"]["most_broken"] == {"key": "exit_by_plan", "count": 2,
                                              "label": "只有止盈与止损两种离场"}
    assert set(report["score"]["by_verdict"]) == set(VERDICTS)
    assert report["status"] == "reconciled" and report["partial"] is False
    assert report["source"] == "fixture" and "示例数据" in report["window_label"]


def test_report_dict_follows_the_shared_contract(full_day):
    report, _ = full_day
    for row in report["trades"]:
        assert set(row) == {"episode", "principles", "fit", "charts"}
        assert {"episode_id", "symbol", "direction", "status", "fills", "net_pnl"} <= set(row["episode"])
        assert {"episode_id", "verdict", "checks", "exits", "planned_sl", "planned_tp",
                "planned_rr", "realized_r", "hold_seconds"} == set(row["principles"])
        assert {"episode_id", "trend_1h", "trend_4h", "alignment", "trigger", "setup",
                "evidence"} == set(row["fit"])
        assert row["principles"]["verdict"] in VERDICTS
        assert set(row["charts"]) == {"15m", "1m"}
        for chart in row["charts"].values():
            assert len(chart["bars"]) > 60 and len(chart["bars"][0]) == 5
            assert chart["markers"] == []
        assert all(check["tier"] == "reference" for check in row["fit"]["evidence"])
    assert {check["key"] for check in report["day_checks"]} == {
        "flip_after_exit", "revenge_reentry", "trade_count"}
    assert all(check["tier"] == "day" for check in report["day_checks"])
    assert {"net_pnl", "closed_pnl", "fees", "funding", "status"} <= set(report["ledger"])
    # 已经是纯 JSON：存库再读回来一字不差；金额与价格是十进制字符串，不是浮点数
    assert json.loads(json.dumps(report, ensure_ascii=False)) == report
    assert report["ledger"]["fees"] == "10.1862079000"
    assert report["trades"][0]["principles"]["planned_sl"] == "95900.00"


def test_mid_trade_exit_has_a_computed_cost_and_the_flip_is_flagged(full_day):
    report, _ = full_day
    eth = by_symbol(report)[("ETHUSDT", "long")]["principles"]
    cost = eth["checks"]["early_exit_cost"]
    assert cost["value"] == "target_first" and cost["state"] == "na"
    assert "先到原止盈 3280" in cost["detail"] and "+2.5R" in cost["detail"]
    assert "+0.7R" in cost["detail"]
    assert eth["planned_rr"] == "2.50" and eth["realized_r"] == "0.70"
    assert [leg["kind"] for leg in eth["exits"]] == ["manual"]
    flip = next(check for check in report["day_checks"] if check["key"] == "flip_after_exit")
    assert flip["state"] == "fail" and "SOLUSDT" in flip["detail"] and "14:46 开多" in flip["detail"]
    # 反手后的那笔本身是带止损进场、止损触发离场：亏损，但合规
    sol_long = by_symbol(report)[("SOLUSDT", "long")]
    assert sol_long["principles"]["verdict"] == "合规"
    assert Decimal(sol_long["episode"]["net_pnl"]) < 0
    assert [leg["kind"] for leg in sol_long["principles"]["exits"]] == ["stop_loss"]


def test_system_fit_is_filled_from_klines_fetched_before_entry(full_day):
    report, _ = full_day
    trades = by_symbol(report)
    btc = trades[("BTCUSDT", "short")]["fit"]
    assert (btc["trend_4h"], btc["alignment"], btc["setup"]) == ("down", "顺势", "顺势回调(关键位未核对)")
    assert trades[("ETHUSDT", "long")]["fit"]["alignment"] == "顺势"
    assert trades[("SOLUSDT", "short")]["fit"]["alignment"] == "无序"
    assert trades[("SOLUSDT", "short")]["fit"]["setup"] == "系统外"


def test_every_request_is_a_documented_read_path_and_klines_are_bounded(full_day):
    _, exchange = full_day
    paths = [path for path, _ in exchange.calls]
    assert set(paths) <= READ_PATHS
    assert paths[0] == "/v5/user/query-api"                 # 先验只读，再读别的
    assert paths.count("/v5/user/query-api") == 1
    klines = [params for path, params in exchange.calls if path == "/v5/market/kline"]
    assert 0 < len(klines) <= 40
    assert all(int(params["limit"]) <= 1000 for params in klines)
    assert {params["interval"] for params in klines} == {"1", "5", "15", "60", "240"}
    # 同一品种同一级别的同一段只取一次
    keys = [(p["symbol"], p["interval"], p["start"], p["end"]) for p in klines]
    assert len(keys) == len(set(keys))


def test_times_in_details_follow_the_configured_timezone(tmp_path):
    payload = json.loads((FIXTURES / "day_full.json").read_text(encoding="utf-8"))
    zone, hours, label, name = daily.resolve_timezone("Asia/Bangkok")
    cfg = settings(tmp_path, tz=zone, tz_offset_hours=hours, tz_label=label, timezone_name=name)
    report, _ = build(payload, cfg, day="2026-11-16", now_ms=kit.DAY0 + kit.DAY + 2 * kit.HOUR)
    assert report["tz_offset_hours"] == 7 and report["window_label"].startswith("曼谷时间")
    # 曼谷的 11-16 比北京的晚 1 小时开始：BTC 09:30(北京) = 08:30(曼谷)
    btc = by_symbol(report)[("BTCUSDT", "short")]
    assert btc["principles"]["checks"]["sl_at_entry"]["detail"].startswith("08:30 进场")
    assert "11-16 08:30(UTC+7)" in btc["fit"]["evidence"][0]["detail"]


# --------------------------------------------------------------------- 盈亏不影响结论

@pytest.mark.parametrize("stop_hit", [True, False])
def test_profit_or_loss_never_changes_the_verdict(tmp_path, stop_hit):
    """同一组订单，只把离场成交价换到进场价的另一侧：结论、每条硬检查都必须一样。"""
    results = []
    for exit_price in ("239.38", "244.42"):
        orders, trips = one_trade(exit_price=exit_price, stop_hit=stop_hit)
        report, _ = build(kit.payload(orders, trips), settings(tmp_path))
        row = report["trades"][0]
        hard = {key: (check["state"], check["value"])
                for key, check in row["principles"]["checks"].items() if check["tier"] == "hard"}
        results.append((row["principles"]["verdict"], hard,
                        [leg["kind"] for leg in row["principles"]["exits"]],
                        Decimal(row["episode"]["net_pnl"]) > 0))
    loss, win = results
    assert loss[3] is False and win[3] is True              # 结果确实一亏一赚
    assert loss[:3] == win[:3]
    assert loss[0] == ("合规" if stop_hit else "中途下车")


def test_long_and_short_trades_are_both_judged(full_day):
    report, _ = full_day
    assert {row["episode"]["direction"] for row in report["trades"]} == {"long", "short"}
    assert all(row["principles"]["verdict"] in ("合规", "中途下车", "无止损")
               for row in report["trades"])


# --------------------------------------------------------------------- 对账闸门（沿用）

def test_tampered_closed_pnl_fails_the_three_source_reconciliation(tmp_path):
    payload = json.loads((FIXTURES / "day_full.json").read_text(encoding="utf-8"))
    row = payload["responses"]["/v5/position/closed-pnl"][0]["result"]["list"][0]
    row["closedPnl"] = format(Decimal(row["closedPnl"]) + 90, "f")
    report, _ = build(payload, settings(tmp_path))
    assert report["status"] == "mismatch"
    assert Decimal(report["ledger"]["closed_pnl_difference"]) == 90
    assert any("回合盈亏与交易所已结盈亏相差 90.00U" in text
               for text in report["reconciliation_reasons"])
    # 纪律结论仍然给出（只看订单），但整份报告标为未核准
    assert [row["principles"]["verdict"] for row in report["trades"]][0] == "合规"


def test_funding_in_the_window_still_blocks_approval(tmp_path):
    payload = json.loads((FIXTURES / "day_full.json").read_text(encoding="utf-8"))
    rows = payload["responses"]["/v5/account/transaction-log"][0]["result"]["list"]
    rows.append({**rows[0], "id": "tx-funding", "type": "SETTLEMENT", "funding": "-0.31",
                 "fee": "0", "cashFlow": "0", "change": "-0.31",
                 "transactionTime": str(kit.at(16, 0))})
    report, _ = build(payload, settings(tmp_path))
    assert report["status"] == "mismatch" and report["ledger"]["funding_present"] is True
    assert "窗口内有资金费，暂不能逐笔归属" in report["reconciliation_reasons"]


def test_today_so_far_is_marked_partial_and_never_approved(tmp_path):
    payload = json.loads((FIXTURES / "day_full.json").read_text(encoding="utf-8"))
    # 12:00 这一刻交易所的样子：BTC 空单还开着（止盈 13:42 才到）
    payload["responses"]["/v5/position/list"] = kit.envelope([kit.position(
        "BTCUSDT", "Sell", "0.020", "95012.40", kit.at(9, 30), kit.at(9, 30),
        stopLoss="95900.00", takeProfit="92600.00")])
    report, _ = build(payload, settings(tmp_path), now_ms=kit.at(12, 0))
    assert report["partial"] is True and report["status"] == "mismatch"
    assert report["window_label"] == "北京时间 2026-11-16 00:00–截至 12:00（当日未结束） · 示例数据"
    assert report["window_end_ms"] == kit.at(12, 0)
    verdicts = {row["episode"]["symbol"]: row["principles"]["verdict"] for row in report["trades"]}
    # 12:00 时 BTC 还没到止盈：持仓中，不提前下结论；之后的成交一律不出现
    assert verdicts == {"BTCUSDT": "持仓中", "ETHUSDT": "中途下车"}
    assert "窗口还没结束或结束不足 10 分钟，流水可能未到齐" in report["reconciliation_reasons"]


def test_a_day_without_any_trade_is_a_clean_empty_report(tmp_path):
    report, exchange = build(kit.payload([], []), settings(tmp_path))
    assert report["trades"] == [] and report["status"] == "reconciled"
    assert report["ledger"]["empty_day"] is True and report["score"]["total"] == 0
    assert not [path for path, _ in exchange.calls if path == "/v5/market/kline"]


# --------------------------------------------------------------------- 隔夜仓 / 继承仓

def test_overnight_trade_is_judged_on_the_day_it_closes(tmp_path):
    """昨天带止损进场、今天止损触发：今天的复盘要能看到昨天的进场，而不是「继承仓位不评价」。"""
    orders, trips = one_trade(entry_at=kit.at(14, 46, 20) - kit.DAY, exit_at=kit.at(9, 5, 0))
    payload = kit.payload(orders, trips)
    today, _ = build(payload, settings(tmp_path))
    row = today["trades"][0]
    assert row["episode"]["inherited_position"] is False
    assert row["principles"]["verdict"] == "合规"
    assert row["principles"]["checks"]["sl_at_entry"]["state"] == "pass"
    assert row["principles"]["hold_seconds"] > 18 * 3600
    count = next(check for check in today["day_checks"] if check["key"] == "trade_count")
    assert "当日新开 0 笔" in count["detail"] and "1 笔是之前留下的仓位" in count["detail"]
    # 对账口径不变：窗口内只看到平仓，无法逐笔核对 → 未核准
    assert today["status"] == "mismatch"
    # 前一天的复盘里，这笔还在持仓中
    yesterday, _ = build(payload, settings(tmp_path), day="2026-11-15")
    assert [r["principles"]["verdict"] for r in yesterday["trades"]] == ["持仓中"]


def test_position_older_than_the_lookback_is_not_judged(tmp_path):
    """进场早于回看范围：窗口里只有加仓和平仓，原始进场看不到 → 继承仓位不评价。"""
    add = kit.execution("add-1", "o-add", "BTCUSDT", "Buy", "1", "100", kit.at(10, 0))
    close = kit.execution("close-1", "o-close", "BTCUSDT", "Sell", "3", "105", kit.at(11, 0),
                          closedSize="3", createType="CreateByClosing")
    orders = [kit.entry_order("o-add", "BTCUSDT", "Buy", "1", kit.at(10, 0), "100"),
              kit.close_order("o-close", "BTCUSDT", "Sell", "3", kit.at(11, 0), "105")]
    payload = kit.payload(orders, [], extra_executions=[add, close])
    report, _ = build(payload, settings(tmp_path))
    row = report["trades"][0]
    assert row["episode"]["inherited_position"] is True and row["episode"]["direction"] == "long"
    assert row["principles"]["verdict"] == "继承仓位不评价"
    assert row["fit"]["setup"] == "未知"
    assert report["status"] == "mismatch" and report["score"]["judged"] == 0


# --------------------------------------------------------------------- 止损快照

def test_stored_order_snapshots_reveal_a_stop_that_was_loosened(tmp_path):
    """止损是在仓位面板上设的（进场单不带）。交易所只留最后的触发价 238.00；
    只有靠之前留存的那次快照（239.40）才能看出它被放宽过。"""
    cfg = settings(tmp_path)
    entry_at, exit_at = kit.at(14, 46, 20), kit.at(15, 31, 44)

    def scenario(trigger, final):
        orders = [kit.entry_order("d-entry", "SOLUSDT", "Buy", "12", entry_at, "241.90")]
        stop = kit.tpsl("d-sl", "SOLUSDT", "Sell", "12", entry_at + 30_000,
                        (exit_at + 5) if final else entry_at + 30_000, "StopLoss", trigger, 2,
                        avg="237.98" if final else None)
        if not final:
            stop.update(orderStatus="Untriggered", cancelType="UNKNOWN")
        entry = kit.execution("d-entry-1", "d-entry", "SOLUSDT", "Buy", "12", "241.90", entry_at + 4)
        close = kit.execution("d-sl-1", "d-sl", "SOLUSDT", "Sell", "12", "237.98", exit_at,
                              closedSize="12", stopOrderType="StopLoss",
                              createType="CreateByStopLoss")
        if final:
            return kit.payload([*orders, stop], [("SOLUSDT", entry, close)])
        return kit.payload(orders, [], open_orders=[stop], extra_executions=[entry],
                           now_ms=kit.at(14, 50))

    # 没有快照：只看得到最后的 238.00 → 无法判断，证据不足
    alone, _ = build(scenario("238.00", True), cfg)
    assert alone["trades"][0]["principles"]["verdict"] == "证据不足"
    assert alone["trades"][0]["principles"]["checks"]["sl_not_loosened"]["state"] == "unknown"

    # 14:50 的定时快照记下了当时的 239.40
    early = FixtureExchange(scenario("239.40", False))
    client = BybitReadClient("fixture", "fixture", requester=early.requester)
    added = asyncio.run(daily.sync_orders(cfg, client=client, now_ms=early.now_ms))
    assert added == 2 and set(path for path, _ in early.calls) <= READ_PATHS
    assert asyncio.run(daily.sync_orders(cfg, client=client, now_ms=early.now_ms + 60_000)) == 0

    stored = daily._load_stored_orders(cfg.db_path, 0)
    report, _ = build(scenario("238.00", True), cfg, stored=stored)
    principles = report["trades"][0]["principles"]
    assert principles["verdict"] == "止损被放宽"
    check = principles["checks"]["sl_not_loosened"]
    assert check["state"] == "fail" and "239.4" in check["detail"] and "238" in check["detail"]


# --------------------------------------------------------------------- 失败方式

def test_a_key_that_is_not_read_only_is_refused_before_anything_else_is_read(tmp_path):
    orders, trips = one_trade()
    exchange = FixtureExchange(kit.payload(orders, trips, read_only=0))
    client = BybitReadClient("fixture", "fixture", requester=exchange.requester)
    cfg = settings(tmp_path)
    request = daily.make_request(DAY, cfg, exchange.now_ms)
    with pytest.raises(daily.ReviewUnavailable) as caught:
        asyncio.run(daily.run_review(cfg, request, client=client, now_ms=exchange.now_ms))
    assert caught.value.code == "not_read_only" and "只读" in caught.value.message
    assert [path for path, _ in exchange.calls] == ["/v5/user/query-api"]
    assert not (tmp_path / "out").exists()


def test_kline_outage_does_not_stop_the_discipline_review(tmp_path):
    orders, trips = one_trade(stop_hit=False)
    exchange = FixtureExchange(kit.payload(orders, trips))
    inner = exchange.requester

    async def flaky(path, params, private):
        if path == "/v5/market/kline":
            raise OSError("network down")
        return await inner(path, params, private)

    client = BybitReadClient("fixture", "fixture", requester=flaky)
    cfg = settings(tmp_path)
    request = daily.make_request(DAY, cfg, exchange.now_ms)
    report, _ = asyncio.run(daily.build_report(client, request, cfg, now_ms=exchange.now_ms))
    row = report["trades"][0]
    assert row["principles"]["verdict"] == "中途下车"            # 硬规矩不依赖 K 线
    assert row["principles"]["checks"]["early_exit_cost"]["value"] == "no_data"
    assert row["fit"]["alignment"] == "未知"
    assert any("部分K线没有取到" in note for note in report["data_notes"])


def test_unresolved_fill_on_a_hedge_mode_symbol_blocks_approval(tmp_path):
    orders, trips = one_trade()
    hedge = copy.deepcopy(orders[0])
    hedge.update(orderId="h-short", side="Sell", positionIdx=2, stopLoss="0.00", takeProfit="0.00")
    orphan = kit.execution("orphan-1", "missing-order", "SOLUSDT", "Sell", "1", "240.00", kit.at(8, 0))
    back = kit.execution("orphan-2", "missing-order-2", "SOLUSDT", "Buy", "1", "240.00", kit.at(8, 5))
    payload = kit.payload([*orders, hedge], trips, extra_executions=[orphan, back])
    report, _ = build(payload, settings(tmp_path))
    assert report["status"] == "mismatch"
    assert any("双向持仓" in text and "2 笔成交找不到对应订单" in text
               for text in report["reconciliation_reasons"])
