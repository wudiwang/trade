"""复核发现的问题的回归测试:总装、报告、投递、系统匹配。

全部走真实的 BybitReadClient(GET + 路径白名单 + 只读 Key 校验)+ FixtureExchange 回放,
不联网、不需要任何密钥。示例数据都是按 Bybit V5 文档的字段形状编的,不来自真实账户。
"""
import asyncio
import json
import re
from dataclasses import replace
from datetime import datetime
from decimal import Decimal

import pytest

from app.review import daily, image, narrator
from app.review.bybit_client import BybitReadClient
from app.review.fixture_exchange import FixtureExchange
from app.review.report import (DeliveryIncomplete, deliver_review, headline, day_view,
                               render_html, telegram_summary)
from app.review.store import ReviewStore
from app.review.system_match import MatchParams, match_episode
from tests.test_bybit_daily_review_live import FIXTURES, kit, settings
from tests.test_review_daily import KEYS, Bot, Cfg, on
from tests.test_review_report import (R_FLIP, article, check, day, mid_exit_short, sl_long,
                                      tp_short)
from tests.test_review_system_match import (consolidation_15m, episode, evidence, mirror,
                                            shift_short_15m, uptrend)

DAY, NEXT = "2026-11-16", "2026-11-17"
MIN = 60_000


def build(payload, cfg, day_text=DAY, now_ms=None, stored=None, sync_times=None):
    exchange = FixtureExchange(payload)
    client = BybitReadClient("fixture", "fixture", requester=exchange.requester)
    now_ms = exchange.now_ms if now_ms is None else now_ms
    request = daily.make_request(day_text, cfg, now_ms)
    report, _ = asyncio.run(daily.build_report(client, request, cfg, now_ms=now_ms,
                                               stored_orders=stored, source="fixture",
                                               sync_times=sync_times))
    return report


def bracket_trade(tag, symbol, side, qty, entry_at, entry_px, sl, tp, exit_at, exit_px, exit_by):
    """一笔带止损止盈的交易(随进场单设置),由止损或止盈触发离场,或手动市价平仓。"""
    close_side = "Sell" if side == "Buy" else "Buy"
    orders = [kit.entry_order(f"{tag}-entry", symbol, side, qty, entry_at, entry_px, sl=sl, tp=tp)]
    entry = kit.execution(f"{tag}-entry-1", f"{tag}-entry", symbol, side, qty, entry_px, entry_at + 4)
    loss_dir, gain_dir = (2, 1) if side == "Buy" else (1, 2)
    hit = {"stop": "sl", "target": "tp"}.get(exit_by)
    orders.append(kit.tpsl(f"{tag}-sl", symbol, close_side, qty, entry_at + 6, exit_at + 5, "StopLoss",
                           sl, loss_dir, avg=exit_px if hit == "sl" else None))
    orders.append(kit.tpsl(f"{tag}-tp", symbol, close_side, qty, entry_at + 6, exit_at + 5,
                           "TakeProfit", tp, gain_dir, avg=exit_px if hit == "tp" else None))
    if hit:
        kind = "StopLoss" if hit == "sl" else "TakeProfit"
        close = kit.execution(f"{tag}-x", f"{tag}-{hit}", symbol, close_side, qty, exit_px, exit_at,
                              closedSize=qty, stopOrderType=kind, createType=f"CreateBy{kind}")
    else:
        orders.append(kit.close_order(f"{tag}-close", symbol, close_side, qty, exit_at - 50, exit_px))
        close = kit.execution(f"{tag}-x", f"{tag}-close", symbol, close_side, qty, exit_px, exit_at,
                              closedSize=qty, createType="CreateByClosing")
    return orders, (symbol, entry, close)


def hard_states(row):
    return {key: item["state"] for key, item in row["principles"]["checks"].items()
            if item["tier"] == "hard"}


# ----------------------------------------------------------------------------- 隔夜仓(critical)

@pytest.mark.parametrize("exit_by", ["target", "manual", "stop"])
def test_overnight_trade_closed_after_midnight_is_not_accused_in_the_report_of_the_day_before(
        tmp_path, exit_by):
    """空单 22:00 进场(止损止盈随单),次日 00:05 离场,00:15 生成前一天的复盘。
    前一天的报告里它是「持仓中」,不能出现「未过:止损没有被放宽」(止损单是离场那一刻才被撤的)。"""
    orders, trip = bracket_trade("o", "BTCUSDT", "Sell", "0.05", kit.at(22, 0), "92700", "95900",
                                 "92600", kit.at(24, 5), "92600", exit_by)
    payload = kit.payload(orders, [trip], market=kit.market(), now_ms=kit.at(24, 15))
    cfg = settings(tmp_path)
    report = build(payload, cfg)
    row = report["trades"][0]
    assert row["principles"]["verdict"] == "持仓中"
    assert "fail" not in hard_states(row).values(), hard_states(row)
    assert report["score"]["most_broken"] is None
    text = telegram_summary(report)
    assert "未过" not in text and "破" not in text.split("\n")[1]
    # 第二天的报告里,同一笔交易按它真实的离场方式评价
    later = build(payload, cfg, NEXT, now_ms=kit.at(36, 0))
    verdict = later["trades"][0]["principles"]["verdict"]
    assert verdict == {"target": "合规", "manual": "中途下车", "stop": "合规"}[exit_by]
    assert hard_states(later["trades"][0])["sl_not_loosened"] == "pass"


def test_yesterdays_review_generated_hours_later_still_judges_as_of_that_days_end(tmp_path):
    orders, trip = bracket_trade("o", "BTCUSDT", "Sell", "0.05", kit.at(22, 0), "92700", "95900",
                                 "92600", kit.at(33, 0), "92650", "manual")      # 次日 09:00 手动平
    payload = kit.payload(orders, [trip], market=kit.market(), now_ms=kit.at(39, 0))
    row = build(payload, settings(tmp_path))["trades"][0]
    assert row["principles"]["verdict"] == "持仓中"
    assert hard_states(row)["sl_not_loosened"] != "fail"
    assert "没有止损" not in row["principles"]["checks"]["sl_not_loosened"]["detail"]


# ----------------------------------------------------------------------------- 跨零点的反手

def test_flip_across_the_day_boundary_is_reported_on_the_day_of_the_second_trade(tmp_path):
    first = bracket_trade("a", "SOLUSDT", "Sell", "10", kit.at(23, 20), "239.60", "241.80", "233.00",
                          kit.at(23, 50), "241.82", "stop")
    second = bracket_trade("b", "SOLUSDT", "Buy", "10", kit.at(24, 5), "239.70", "237.50", "245.00",
                           kit.at(24, 40), "237.48", "stop")
    payload = kit.payload(first[0] + second[0], [first[1], second[1]], market=kit.market(),
                          now_ms=kit.at(25, 0))
    cfg = settings(tmp_path)
    day_one = build(payload, cfg)
    checks = {item["key"]: item for item in day_one["day_checks"]}
    assert len(day_one["trades"]) == 1 and checks["flip_after_exit"]["state"] == "pass"
    day_two = build(payload, cfg, NEXT, now_ms=kit.at(25, 0))
    checks = {item["key"]: item for item in day_two["day_checks"]}
    assert len(day_two["trades"]) == 1 and day_two["score"]["total"] == 1
    assert checks["flip_after_exit"]["state"] == "fail"
    assert "开多" in checks["flip_after_exit"]["detail"] and "15 分钟" in checks["flip_after_exit"]["detail"]
    assert Decimal(checks["trade_count"]["value"]) == 1
    assert "⚠ 当日：反手" in telegram_summary(day_two)


# ----------------------------------------------------------------------------- 同品种多笔不互相影响

def test_system_fit_of_a_trade_does_not_depend_on_other_trades_of_the_same_symbol(tmp_path, monkeypatch):
    """同品种多了一笔三天前的交易,后一笔的趋势判定用的 K 线必须一模一样。"""
    late = bracket_trade("b", "SOLUSDT", "Buy", "10", kit.at(14, 46), "241.90", "239.40", "247.00",
                         kit.at(15, 31), "239.38", "stop")
    early = bracket_trade("a", "SOLUSDT", "Buy", "10", kit.at(-70, 0), "240.00", "237.00", "246.00",
                          kit.at(-69, 0), "236.98", "stop")
    seen = {}
    real = daily.match_episode

    def spy(episode, klines, params=None):
        fit = real(episode, klines, params)
        entry = min(fill.execution.timestamp_ms for fill in episode.fills if fill.action == "entry")
        seen.setdefault(entry, []).append(
            ({tf: (len(rows), rows[0].start_ms if rows else None) for tf, rows in klines.items()}, fit))
        return fit

    monkeypatch.setattr(daily, "match_episode", spy)
    cfg = settings(tmp_path)
    build(kit.payload(late[0], [late[1]], market=kit.market()), cfg)
    build(kit.payload(early[0] + late[0], [early[1], late[1]], market=kit.market()), cfg)
    alone, together = seen[kit.at(14, 46) + 4]
    assert alone[0] == together[0]
    assert alone[1] == together[1]
    assert alone[0]["4h"][0] <= cfg.trend_bars + 8             # 只有这一笔自己取的那一段


# ----------------------------------------------------------------------------- 取数

class FlakyClient:
    """只实现 fetch_account 用到的只读方法;记录调用,仓位在第一次读之后变化一次。"""

    def __init__(self, positions):
        self._positions = list(positions)
        self.calls = []

    async def verify_read_only(self):
        self.calls.append(("verify",))

    async def positions(self, category):
        self.calls.append(("positions",))
        return self._positions.pop(0) if len(self._positions) > 1 else self._positions[0]

    async def executions(self, category, start, end):
        self.calls.append(("executions", end))
        return []

    async def transactions(self, category, start, end):
        return []

    async def closed_pnl(self, category, start, end):
        return []

    async def order_history(self, category, start, end):
        self.calls.append(("orders", end))
        return []

    async def open_orders(self, category):
        return []


def test_fetch_account_refetches_once_when_the_position_changed_while_reading():
    cfg = daily.ReviewSettings()
    key = ("linear", "SOLUSDT", 0)
    request = daily.make_request(DAY, cfg, kit.at(24, 15))
    moving = FlakyClient([{key: Decimal("10")}, {}, {}])           # 读成交期间仓位被平掉
    inputs = asyncio.run(daily.fetch_account(moving, request, cfg, kit.at(24, 15)))
    assert [call[0] for call in moving.calls].count("executions") == 2
    assert inputs.positions_before == inputs.positions_after == {}
    still = FlakyClient([{key: Decimal("10")}])
    asyncio.run(daily.fetch_account(still, request, cfg, kit.at(24, 15)))
    assert [call[0] for call in still.calls].count("executions") == 1
    assert still.calls[0] == ("verify",)                           # 只读校验仍然最先


def test_live_runs_take_the_fill_cutoff_after_reading_positions():
    cfg = daily.ReviewSettings()
    request = daily.make_request(DAY, cfg, kit.at(24, 15))
    client = FlakyClient([{}])
    later = kit.at(24, 15) + 2_500
    inputs = asyncio.run(daily.fetch_account(client, request, cfg, kit.at(24, 15), clock=lambda: later))
    assert inputs.now_ms == later
    assert ("executions", later) in client.calls and ("orders", later) in client.calls
    order = [call[0] for call in client.calls]
    assert order.index("positions") < order.index("executions")


def test_fills_that_do_not_add_up_to_the_position_are_explained_not_silently_merged(tmp_path):
    """成交净额 +10,仓位接口却说空仓(例如最后一笔平仓成交还没出现在成交列表里)。"""
    orders, trip = bracket_trade("a", "SOLUSDT", "Buy", "10", kit.at(10, 0), "239.00", "237.00",
                                 "245.00", kit.at(11, 0), "236.98", "stop")
    orphan_order = kit.entry_order("b-entry", "SOLUSDT", "Buy", "10", kit.at(14, 0), "240.00",
                                   sl="238.00", tp="246.00")
    orphan = kit.execution("b-entry-1", "b-entry", "SOLUSDT", "Buy", "10", "240.00", kit.at(14, 0) + 4)
    payload = kit.payload(orders + [orphan_order], [trip], market=kit.market(),
                          extra_executions=[orphan])
    report = build(payload, settings(tmp_path))
    assert any("SOLUSDT" in note and "对不上" in note for note in report["data_notes"])
    assert "对不上" in render_html(report) and "ℹ 数据" in telegram_summary(report)


def test_orders_complete_since_follows_an_unbroken_chain_of_syncs():
    hour = 3_600_000
    now = 1_000 * hour
    keep = 24 * hour - 10 * MIN
    assert daily.orders_complete_since([], now, 24) == now - keep
    chain = [now - 20 * hour, now - 40 * hour, now - 60 * hour]
    assert daily.orders_complete_since(chain, now, 24) == now - 60 * hour - keep
    broken = [now - 20 * hour, now - 50 * hour]                    # 中间断了 30 小时
    assert daily.orders_complete_since(broken, now, 24) == now - 20 * hour - keep
    assert daily.orders_complete_since([now + hour], now, 24) == now - keep


def test_every_stored_order_sync_is_logged(tmp_path):
    store = ReviewStore(tmp_path / "r.db")
    store.upsert_orders([], seen_ms=1_000)
    store.upsert_orders([], seen_ms=5_000)
    store.upsert_orders([], seen_ms=5_000)
    assert store.order_sync_times() == [1_000, 5_000] and store.order_sync_times(2_000) == [5_000]
    store.close()


# ----------------------------------------------------------------------------- K 线不拖垮硬规矩

class Klines:
    def __init__(self, error=None, delay=0.0):
        self.error, self.delay, self.calls = error, delay, 0

    async def klines(self, category, symbol, interval, start_ms, end_ms):
        self.calls += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error:
            raise self.error
        return []


def test_kline_book_gives_up_after_a_timeout_instead_of_waiting_for_every_request():
    client = Klines(error=asyncio.TimeoutError())
    book = daily.KlineBook(client, now_ms=10**12, pause=0, budget=50)
    assert asyncio.run(book.ensure("BTCUSDT", 1, 0, 60_000)) is False
    assert book.dead is True
    for minutes in (5, 15, 60, 240):
        assert asyncio.run(book.ensure("BTCUSDT", minutes, 0, minutes * 60_000)) is False
    assert client.calls == 1
    flaky = Klines(error=OSError("down"))
    book = daily.KlineBook(flaky, now_ms=10**12, pause=0, budget=50)
    for index in range(6):
        asyncio.run(book.ensure("ETHUSDT", 1, index * 60_000, (index + 1) * 60_000))
    assert flaky.calls == 3 and book.dead is True               # 连续三次出错后不再请求


def test_hanging_kline_endpoint_still_delivers_the_order_based_verdicts(tmp_path):
    payload = json.loads((FIXTURES / "day_full.json").read_text(encoding="utf-8"))
    exchange = FixtureExchange(payload)
    client = BybitReadClient("fixture", "fixture", requester=exchange.requester)

    async def hang(*args, **kwargs):
        await asyncio.sleep(30)

    client.klines = hang
    cfg = replace(settings(tmp_path), kline_timeout_seconds=0.2)
    request = daily.make_request(DAY, cfg, exchange.now_ms)
    report, _ = asyncio.run(daily.build_report(client, request, cfg, now_ms=exchange.now_ms,
                                               source="fixture"))
    assert [row["principles"]["verdict"] for row in report["trades"]] == [
        "合规", "中途下车", "无止损", "合规"]
    assert any("取K线超过" in note and "硬规矩不受影响" in note for note in report["data_notes"])
    assert "取K线超过" in render_html(report)


# ----------------------------------------------------------------------------- 失败信息不带密钥

def test_failure_text_never_carries_the_telegram_bot_token():
    aiohttp = pytest.importorskip("aiohttp")
    from yarl import URL
    token = "123456789:FAKE-TOKEN-FOR-TEST-ONLY-abcdefghij"
    info = aiohttp.RequestInfo(URL(f"https://api.telegram.org/bot{token}/sendDocument"), "POST", {},
                               URL(f"https://api.telegram.org/bot{token}/sendDocument"))
    error = aiohttp.ContentTypeError(info, (), status=502, message="unexpected mimetype: text/html")
    assert token in str(error)                                   # 原始异常文本确实带着 token
    text = daily.describe_failure(error)
    assert "FAKE-TOKEN" not in text and "123456789" not in text and "连不上" in text
    generic = daily.describe_failure(ValueError(f"bad reply from https://api.telegram.org/bot{token}/x"))
    assert "FAKE-TOKEN" not in generic and "123456789" not in generic
    bare = daily.describe_failure(RuntimeError(f"bot{token} rejected"))
    assert "FAKE-TOKEN" not in bare


# ----------------------------------------------------------------------------- 投递

class PartBot:
    enabled = True

    def __init__(self, photo=True, text=True, document=True):
        self.ok = {"photo": photo, "text": text, "document": document}
        self.calls, self.texts = [], []

    async def send_photo(self, path, caption=""):
        self.calls.append("photo")
        return {"ok": self.ok["photo"]}

    async def notify(self, text):
        self.calls.append("text")
        self.texts.append(text)
        return {"ok": self.ok["text"]}

    async def send_document(self, path, caption=""):
        self.calls.append("document")
        return {"ok": self.ok["document"]}


@pytest.fixture(scope="module")
def sample(tmp_path_factory):
    root = tmp_path_factory.mktemp("sample")
    cfg = replace(daily.ReviewSettings(), db_path=root / "review.db", output_dir=root / "out",
                  kline_pause_seconds=0.0)
    exchange = FixtureExchange.load(FIXTURES / "day_full.json")
    client = BybitReadClient("fixture", "fixture", requester=exchange.requester)
    request = daily.make_request(DAY, cfg, exchange.now_ms)
    result = asyncio.run(daily.run_review(cfg, request, client=client, now_ms=exchange.now_ms,
                                          source="fixture"))
    return replace(result, report={**result.report, "source": "bybit"})


class CountingRunner:
    def __init__(self, result):
        self.result, self.calls = result, 0

    async def __call__(self, settings_, request, **kwargs):
        self.calls += 1
        return self.result


def ticks(cfg, bot, state, runner, minutes):
    out = []
    for minute in minutes:
        now = datetime(2026, 11, 17, 0, minute, tzinfo=daily.BEIJING)
        out.append(asyncio.run(daily.review_tick(cfg, bot, state, now=now, env=KEYS, runner=runner)))
    return out


def test_failed_document_upload_does_not_resend_the_summary_or_rebuild_the_review(tmp_path, sample):
    cfg, bot, state, runner = on(tmp_path), PartBot(document=False), daily.SchedulerState(), \
        CountingRunner(sample)
    assert ticks(cfg, bot, state, runner, (10, 15, 20, 30)) == ["failed", "failed", "failed", "gave_up"]
    assert bot.calls.count("photo") == 1                         # 摘要只发了一次
    assert bot.calls.count("document") == 3 and runner.calls == 1
    assert len(bot.texts) == 1 and "摘要已发出" in bot.texts[0] and "没有生成" not in bot.texts[0]
    # 重启后(内存状态没了)也不会重发摘要:库里记着已送达的部分
    again = PartBot()
    assert ticks(cfg, again, daily.SchedulerState(), runner, (40,)) == ["sent"]
    assert again.calls == ["document"]
    assert ticks(cfg, again, daily.SchedulerState(), runner, (50,)) == ["already_sent"]


def test_document_alone_does_not_count_as_a_delivered_review(tmp_path, sample):
    cfg, bot, state, runner = on(tmp_path), PartBot(photo=False, text=False), \
        daily.SchedulerState(), CountingRunner(sample)
    assert ticks(cfg, bot, state, runner, (10,)) == ["failed"]
    later = PartBot()
    assert ticks(cfg, later, state, runner, (16,)) == ["sent"]
    assert later.calls == ["photo"]                              # 只补摘要,附件不重发


def test_deliver_review_reports_which_part_arrived(tmp_path, sample):
    with pytest.raises(DeliveryIncomplete) as caught:
        asyncio.run(deliver_review(PartBot(document=False), sample.report, sample.png_path,
                                   sample.html_path))
    assert caught.value.sent == {"summary"}
    with pytest.raises(DeliveryIncomplete) as caught:
        asyncio.run(deliver_review(PartBot(photo=False, text=False), sample.report, sample.png_path,
                                   sample.html_path))
    assert caught.value.sent == {"document"}
    bot = PartBot()
    asyncio.run(deliver_review(bot, sample.report, sample.png_path, sample.html_path,
                               skip={"summary"}))
    assert bot.calls == ["document"]


# ----------------------------------------------------------------------------- 摘要图的字体

def test_latin_only_fonts_are_rejected_for_the_summary_image():
    from PIL import ImageFont
    assert image.cjk_font_path() is not None, "这台机器应当有中文字体(开发机 / 已装 fonts-noto-cjk)"
    assert image.has_cjk_glyphs(ImageFont.truetype(image.cjk_font_path(), 40)) is True
    assert image.has_cjk_glyphs(ImageFont.load_default()) is False
    for latin in ("C:/Windows/Fonts/arial.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"):
        try:
            font = ImageFont.truetype(latin, 40)
        except OSError:
            continue
        assert image.has_cjk_glyphs(font) is False


def test_without_a_chinese_font_no_image_is_made_and_the_text_summary_is_sent(tmp_path, sample,
                                                                             monkeypatch):
    monkeypatch.setattr(image, "cjk_font_path", lambda bold=False: None)
    image._load.cache_clear()
    with pytest.raises(image.NoCjkFont):
        image.render_summary_png(sample.report, tmp_path / "x" / "summary.png")
    assert not (tmp_path / "x").exists()
    result = daily.write_report(dict(sample.report), tmp_path / "out")
    assert result.png_path is None and not (tmp_path / "out" / DAY / "summary.png").exists()
    assert result.html_path.is_file() and result.text_path.is_file()
    bot = PartBot()
    asyncio.run(daily.deliver(bot, result, daily.ReviewSettings(db_path=tmp_path / "r.db")))
    assert bot.calls == ["text", "document"] and "实盘纪律" in bot.texts[0]


# ----------------------------------------------------------------------------- 报告措辞

def flip_check():
    return check(R_FLIP, "fail", "SOLUSDT 14:40 平空(止损)→ 14:46 开多,间隔 6 分钟", tier="day",
                 value="1")


def test_green_headline_is_not_shown_on_a_day_with_a_day_level_violation(tmp_path):
    report = day([tp_short(), sl_long()], day_checks=[flip_check()])
    text, tone = headline(day_view(report))
    assert tone == "warn" and "当日层面有 1 项违规：反手" in text
    lines = telegram_summary(report).split("\n")
    assert lines[0] == "<b>2026-11-16 实盘纪律 · 2 笔，2 笔合规 · 当日层面 1 项违规</b>"
    assert "今日没有破规矩的交易" not in "\n".join(lines)
    html = render_html(report)
    assert "今日没有破规矩的交易" not in html and '<p class="broken warn">' in html
    image.render_summary_png(report, tmp_path / "s.png")          # 图也用同一句话(不抛即可)
    clean = day([tp_short(), sl_long()])
    assert headline(day_view(clean)) == ("今日没有破规矩的交易", "ok")


def test_open_position_with_a_failed_rule_is_named_in_the_headline():
    item = tp_short()
    item["principles"]["verdict"] = "持仓中"
    item["episode"]["status"] = "open"
    item["principles"]["checks"]["sl_not_loosened"]["state"] = "fail"
    report = day([item])
    del report["score"]                               # 由报告层按逐笔结果自己统计
    text, tone = headline(day_view(report))
    assert tone == "warn" and "持仓中的 1 笔有未过的规矩：止损没有被放宽" in text
    assert day_view(report)["score"]["most_broken"] is None


def test_cost_box_has_a_neutral_heading():
    card = article(render_html(day([tp_short(), mid_exit_short()])), 2)
    assert "<b>离场之后（事后对照，不改变结论）</b>" in card and "中途下车的代价" not in card


def test_price_return_is_labelled_as_direction_adjusted():
    item = tp_short()
    item["episode"]["price_return_pct"] = "2.54"
    card = article(render_html(day([item])), 1)
    assert "按方向计的价格收益 +2.54%" in card and "价格变动" not in card


def test_data_notes_and_scope_are_shown_in_every_output():
    report = day([tp_short()], data_notes=["K线请求数达到上限 300 次，部分交易缺图或缺趋势判定"],
                 scope_label="Bybit USDT 永续")
    html = render_html(report)
    assert "数据说明" in html and "K线请求数达到上限 300 次" in html
    assert "范围：Bybit USDT 永续" in html
    assert "ℹ 数据：K线请求数达到上限 300 次" in telegram_summary(report)
    empty = day([], scope_label="Bybit USDT 永续")
    assert "没有交易需要评价（只统计 Bybit USDT 永续）" in telegram_summary(empty)
    assert "只统计 Bybit USDT 永续" in render_html(empty)


def test_chart_labels_stay_readable_on_a_phone():
    """图在 375px 手机上约 294px 宽(viewBox 360):字号至少 11 个单位才有 9px。"""
    html = render_html(day([tp_short(), mid_exit_short()]))
    sizes = [float(value) for value in re.findall(r'<text[^>]*font-size="([\d.]+)"', html)]
    assert sizes and min(sizes) >= 11
    assert 'viewBox="0 0 360 ' in html


# ----------------------------------------------------------------------------- 系统匹配

def test_forbidden_setup_and_plain_descriptions_are_never_rendered_as_conforming():
    fit = match_episode(episode(price="106"),
                        {"4h": uptrend("4h"), "1h": uptrend("1h"), "15m": consolidation_15m()})
    assert fit.setup == "盘整区预判(禁入)"
    assert evidence(fit, "setup").state.value == "fail"           # 禁入不是"符合"
    assert evidence(fit, "consolidation").state.value == "pass"   # 有 = 在盘整区内
    assert evidence(fit, "trend_4h").state.value == "na" and evidence(fit, "entry").state.value == "na"
    item = tp_short()
    from dataclasses import asdict
    item["fit"] = json.loads(json.dumps(asdict(fit), default=str, ensure_ascii=False))
    card = article(render_html(day([item])), 1)
    block = card[card.index("系统匹配依据"):]
    block = block[:block.index("</details>")]
    assert "符合" not in block and "✓" not in block
    assert '<em class="c-soft">在盘整区内</em>' in block
    assert '<em class="c-soft">盘整区预判(禁入)</em>' in block
    assert '<em class="c-soft">无</em>' not in block             # "没有触发"只是描述,不上提示色


def test_retest_must_lie_between_the_broken_level_and_the_lower_high():
    """顶1 120.4 / 底1 109.6 / 顶2 116.4:做空价要在 109.6~116.4 之间才算回抽到位。"""
    inside = match_episode(episode("short", "112"), {"15m": shift_short_15m()})
    assert inside.setup == "趋势转换反转" and evidence(inside, "shift_retest").state.value == "pass"
    for price in ("117", "121", "135"):
        fit = match_episode(episode("short", price), {"15m": shift_short_15m()})
        retest = evidence(fit, "shift_retest")
        assert retest.state.value == "fail" and "已高于顶2" in retest.detail, price
        assert fit.setup != "趋势转换反转", price
    long_side = match_episode(episode("long", "110"), {"15m": mirror(shift_short_15m())})
    assert evidence(long_side, "shift_retest").state.value == "fail"
    assert "已低于底2" in evidence(long_side, "shift_retest").detail


def test_alignment_decided_by_1h_says_so_on_the_chip():
    item = sl_long()                                  # 1h 上涨、4h 无序、顺势
    card = article(render_html(day([item])), 1)
    assert "顺势（按 1h；4h 无序）" in card
    both = tp_short()
    both["fit"] = {"episode_id": "ep-btc", "trend_1h": "down", "trend_4h": "down",
                   "alignment": "顺势", "trigger": "无", "setup": "系统外", "evidence": ()}
    assert '<span class="chip">顺势</span>' in article(render_html(day([both])), 1)


# ----------------------------------------------------------------------------- AI 润色的总时限

def test_ai_polish_shares_one_deadline_for_the_whole_report(tmp_path, monkeypatch):
    payload = json.loads((FIXTURES / "day_full.json").read_text(encoding="utf-8"))
    clock = {"now": 1_000.0}
    timeouts = []

    def slow_ai(review, *, timeout_seconds=120, **kwargs):
        timeouts.append(timeout_seconds)
        clock["now"] += 70                                   # 每次"用掉" 70 秒
        return narrator.deterministic_narrative(review)

    monkeypatch.setenv(narrator.AI_ENV_FLAG, "1")
    monkeypatch.setattr(narrator, "generate_narrative", slow_ai)
    monkeypatch.setattr(daily.time, "monotonic", lambda: clock["now"])
    report = build(payload, replace(settings(tmp_path), ai_timeout_seconds=120))
    assert len(report["trades"]) == 4 and report["status"] == "reconciled"
    assert timeouts == [120, 50]                             # 第三笔起时限已用完,不再调用
