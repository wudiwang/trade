"""每日实盘复盘的「接线」测试：配置、调度时间逻辑（注入时钟）、无密钥软失败、Telegram 指令、
登录后才可见的网页接口、模拟日报开关、留存与幂等。

不联网、不需要任何密钥。
运行: PYTHONIOENCODING=utf-8 .venv/Scripts/python -m pytest tests/test_review_daily.py -q
"""
import asyncio
import importlib.util
import inspect
import json
import re
from dataclasses import replace
from datetime import date, datetime, time as dtime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app.web.server as server
from app.bot.telegram import TgBot
from app.config import Config
from app.db import DB
from app.review import daily
from app.review.bybit_client import READ_PATHS, BybitReadClient
from app.review.fixture_exchange import FixtureExchange
from app.review.models import OrderRecord
from app.review.narrator import build_prompt, parse_or_fallback
from app.review.store import ReviewStore, order_from_payload, read_page_html, read_pages

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).parent / "fixtures" / "bybit_review"
KEYS = {"BYBIT_API_KEY": "k", "BYBIT_API_SECRET": "s"}
NO_KEYS: dict = {}

_spec = importlib.util.spec_from_file_location("make_day_full", FIXTURES / "make_day_full.py")
kit = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(kit)


class Cfg:
    """最小配置替身：cfg.get('a.b') + Telegram 凭据属性。"""

    def __init__(self, tmp_path=None, **values):
        self.values = {"telegram.enabled": True}
        if tmp_path is not None:
            self.values.update({"review.db_path": str(tmp_path / "review.db"),
                                "review.output_dir": str(tmp_path / "out")})
        self.values.update({key.replace("__", "."): value for key, value in values.items()})
        self.tg_token, self.tg_chat_id = "token", "42"

    def get(self, key, default=None):
        return self.values.get(key, default)


def on(tmp_path, **more):
    return Cfg(tmp_path, review__enabled=True, review__telegram_enabled=True, **more)


class Bot:
    def __init__(self, enabled=True):
        self.enabled, self.texts, self.photos, self.documents = enabled, [], [], []

    async def notify(self, text):
        self.texts.append(text)
        return {"ok": True}

    async def send_photo(self, path, caption=""):
        self.photos.append(caption)
        return {"ok": True}

    async def send_document(self, path, caption=""):
        self.documents.append(path)
        return {"ok": True}


def at(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=daily.BEIJING)


@pytest.fixture(scope="module")
def sample(tmp_path_factory):
    """示例日跑一遍全链路，得到一份真实结构的 ReviewResult。"""
    root = tmp_path_factory.mktemp("sample")
    cfg = replace(daily.ReviewSettings(), db_path=root / "review.db", output_dir=root / "out",
                  kline_pause_seconds=0.0)
    exchange = FixtureExchange.load(FIXTURES / "day_full.json")
    client = BybitReadClient("fixture", "fixture", requester=exchange.requester)
    request = daily.make_request("2026-11-16", cfg, exchange.now_ms)
    result = asyncio.run(daily.run_review(cfg, request, client=client, now_ms=exchange.now_ms,
                                          source="fixture"))
    return cfg, result, exchange


def as_live(result):
    """同一份报告，去掉「示例数据」标记（示例数据本身永远不允许投递）。"""
    return replace(result, report={**result.report, "source": "bybit"})


# --------------------------------------------------------------------- 配置

def test_shipped_config_keeps_everything_off_until_the_owner_adds_his_key():
    cfg = Config()
    settings = daily.settings_from_config(cfg)
    assert settings.enabled is False and settings.telegram_enabled is False
    assert settings.timezone_name == "Asia/Shanghai" and settings.tz_offset_hours == 8
    assert settings.send_time == dtime(0, 10)
    assert cfg.get("reports.paper_daily") is False
    assert settings.principles == daily.PrincipleParams(tz_offset_hours=8)
    assert settings.match == daily.MatchParams(tz_offset_hours=8)
    assert settings.lookback_days == 7 and settings.order_sync_minutes == 30
    text = (ROOT / "config.yaml").read_text(encoding="utf-8-sig")
    section = text[text.index("\nreview:"):]
    for key in ("enabled", "timezone", "send_time", "telegram_enabled", "sl_grace_seconds",
                "min_rr", "horizon_hours", "flip_minutes", "reentry_minutes", "max_trades_per_day",
                "min_trend_bars", "trigger_lookback_bars", "imbalance_body_mult"):
        line = next(line for line in section.splitlines() if line.strip().startswith(key + ":"))
        assert "#" in line and re.search(r"[一-鿿]", line.split("#", 1)[1]), key
    assert "BYBIT_API_SECRET" in text and not re.search(r"BYBIT_API_(KEY|SECRET)\s*[:=]\s*\S", text)


def test_thresholds_come_from_config_and_bad_values_fall_back(tmp_path):
    cfg = Cfg(tmp_path, review__timezone="Asia/Bangkok", review__send_time=10,
              review__principles__sl_grace_seconds=300, review__principles__min_rr="2.5",
              review__principles__max_trades_per_day=3, review__principles__horizon_hours="oops",
              review__match__trigger_lookback_bars=8, review__match__imbalance_body_mult=4,
              review__lookback_days=0)
    settings = daily.settings_from_config(cfg)
    assert settings.tz_offset_hours == 7 and settings.tz_label == "曼谷时间"
    assert settings.send_time == dtime(0, 10)              # YAML 把不带引号的 00:10 读成整数 10
    assert settings.principles.sl_grace_seconds == 300
    assert settings.principles.min_rr == Decimal("2.5")
    assert settings.principles.max_trades_per_day == 3
    assert settings.principles.horizon_hours == 72         # 认不出的值保留默认
    assert settings.principles.tz_offset_hours == 7 and settings.match.tz_offset_hours == 7
    assert settings.match.trigger_lookback_bars == 8
    assert settings.match.imbalance_body_mult == Decimal("4")
    assert settings.lookback_days == 1
    assert settings.db_path == tmp_path / "review.db"


@pytest.mark.parametrize("value,expected", [
    ("00:10", dtime(0, 10)), ("23:55", dtime(23, 55)), (" 7:05 ", dtime(7, 5)), (10, dtime(0, 10)),
    (485, dtime(8, 5)), ("25:00", dtime(0, 10)), ("soon", dtime(0, 10)), (None, dtime(0, 10)),
])
def test_send_time_parsing(value, expected):
    assert daily.parse_send_time(value) == expected


@pytest.mark.parametrize("name,hours,label", [
    ("Asia/Shanghai", 8, "北京时间"), ("Asia/Bangkok", 7, "曼谷时间"), ("UTC+7", 7, "曼谷时间"),
    ("+05:30", 5.5, "UTC+5.5"), ("Europe/Moscow", 3, "UTC+3"), ("Mars/Olympus", 8, "北京时间"),
])
def test_timezone_resolution(name, hours, label):
    zone, offset, text, _ = daily.resolve_timezone(name)
    assert (offset, text) == (hours, label)
    assert zone.utcoffset(None) == timedelta(hours=hours)


def test_day_window_and_request_labels():
    settings = daily.ReviewSettings()
    start, end = daily.day_window("2026-11-16", settings.tz)
    assert (start, end) == (kit.DAY0, kit.DAY0 + kit.DAY)
    full = daily.make_request("2026-11-16", settings, end + 600_000)
    assert (full.partial, full.label) == (False, "北京时间 2026-11-16 00:00–24:00")
    part = daily.make_request("2026-11-16", settings, kit.at(15, 32))
    assert part.partial and part.end_ms == kit.at(15, 32)
    assert part.label == "北京时间 2026-11-16 00:00–截至 15:32（当日未结束）"
    for bad, code in (("2026-11-17", "future_day"), ("2026-13-01", "bad_day"), ("昨天", "bad_day")):
        with pytest.raises(daily.ReviewUnavailable) as caught:
            daily.make_request(bad, settings, kit.at(15, 32))
        assert caught.value.code == code


# --------------------------------------------------------------------- 指令解析

TODAY = date(2026, 11, 17)


@pytest.mark.parametrize("text,day", [
    ("复盘", "2026-11-17"), ("/review", "2026-11-17"), ("review", "2026-11-17"),
    ("/review@daxian_bot", "2026-11-17"), ("复盘 今天", "2026-11-17"), ("复盘 today", "2026-11-17"),
    ("复盘 昨天", "2026-11-16"), ("复盘昨天", "2026-11-16"), ("/review yesterday", "2026-11-16"),
    ("复盘 前天", "2026-11-15"), ("复盘 2026-11-10", "2026-11-10"), ("/review 2026/11/10", "2026-11-10"),
    ("复盘 20261110", "2026-11-10"), ("复盘 11-10", "2026-11-10"), ("复盘 11月10日", "2026-11-10"),
    ("  复盘   昨天  ", "2026-11-16"), ("复盘 12-30", "2025-12-30"),
])
def test_review_command_is_recognised(text, day):
    assert daily.parse_review_command(text, TODAY) == daily.ReviewCommand(day)


@pytest.mark.parametrize("text", ["", "状态", "/status", "复 盘", "reviewer", "今天复盘", "/start",
                                  "复盘一下最近为什么老是拿不住单子吧我想听听"])
def test_other_messages_are_not_review_commands(text):
    assert daily.parse_review_command(text, TODAY) is None


@pytest.mark.parametrize("text,needle", [
    ("复盘 2026-11-18", "还没到"), ("复盘 明天", "没看懂"), ("复盘 2026-02-30", "没看懂"),
    ("/review 13-40", "没看懂"),
])
def test_review_command_with_a_bad_day_explains_itself(text, needle):
    command = daily.parse_review_command(text, TODAY)
    assert command.day is None and needle in command.error


# --------------------------------------------------------------------- 调度：时间逻辑

@pytest.mark.parametrize("now,expected", [
    ("2026-11-17 00:09:59", None), ("2026-11-17 00:10:00", "2026-11-16"),
    ("2026-11-17 03:00:00", "2026-11-16"), ("2026-11-17 06:09:59", "2026-11-16"),
    ("2026-11-17 06:10:00", None), ("2026-11-17 23:59:00", None),
    ("2026-01-01 00:10:00", "2025-12-31"),
])
def test_due_day_with_default_send_time(now, expected):
    assert daily.due_review_day(at(now), dtime(0, 10), 6) == expected


def test_due_day_with_a_late_evening_send_time_still_means_the_day_that_ended():
    assert daily.due_review_day(at("2026-11-17 23:30:00"), dtime(23, 30), 6) == "2026-11-16"
    assert daily.due_review_day(at("2026-11-18 02:00:00"), dtime(23, 30), 6) == "2026-11-16"
    assert daily.due_review_day(at("2026-11-17 23:29:00"), dtime(23, 30), 6) is None


class Runner:
    def __init__(self, result=None, fail=None):
        self.result, self.fail, self.requests = result, fail, []

    async def __call__(self, settings, request, **kwargs):
        self.requests.append(request)
        if self.fail:
            raise self.fail
        return self.result


class Sender:
    def __init__(self):
        self.sent = []

    async def __call__(self, bot, result, settings, daily=False):
        self.sent.append((result, daily))


def tick(cfg, bot, state, now, env=KEYS, runner=None, sender=None):
    return asyncio.run(daily.review_tick(cfg, bot, state, now=at(now), env=env,
                                         runner=runner, sender=sender))


def test_scheduler_sends_once_at_send_time_for_the_day_that_just_ended(tmp_path):
    cfg, bot, state = on(tmp_path), Bot(), daily.SchedulerState()
    runner, sender = Runner(result="RESULT"), Sender()
    assert tick(cfg, bot, state, "2026-11-17 00:09:00", runner=runner, sender=sender) == "idle"
    assert runner.requests == []
    assert tick(cfg, bot, state, "2026-11-17 00:10:30", runner=runner, sender=sender) == "sent"
    request = runner.requests[0]
    assert (request.day, request.partial) == ("2026-11-16", False)
    assert (request.start_ms, request.end_ms) == (kit.DAY0, kit.DAY0 + kit.DAY)
    assert sender.sent == [("RESULT", True)]
    for later in ("2026-11-17 00:11:30", "2026-11-17 05:00:00", "2026-11-17 12:00:00"):
        assert tick(cfg, bot, state, later, runner=runner, sender=sender) == "idle"
    assert len(runner.requests) == 1 and bot.texts == []
    assert tick(cfg, bot, state, "2026-11-18 00:10:00", runner=runner, sender=sender) == "sent"
    assert runner.requests[1].day == "2026-11-17"


@pytest.mark.parametrize("flags", [
    {}, {"review__enabled": True}, {"review__telegram_enabled": True},
])
def test_scheduler_does_nothing_unless_both_switches_are_on(tmp_path, flags):
    runner, bot = Runner(result="R"), Bot()
    state = daily.SchedulerState()
    assert tick(Cfg(tmp_path, **flags), bot, state, "2026-11-17 00:10:30", runner=runner) == "off"
    assert runner.requests == [] and bot.texts == []


def test_scheduler_without_credentials_stays_silent(tmp_path):
    """没有密钥：不生成、不推送，也绝不发「已复盘」之类的消息。"""
    cfg, bot, state = on(tmp_path), Bot(), daily.SchedulerState()
    runner, sender = Runner(result="R"), Sender()
    for now in ("2026-11-17 00:10:30", "2026-11-17 00:11:30"):
        assert tick(cfg, bot, state, now, env=NO_KEYS, runner=runner, sender=sender) == "no_credentials"
    assert runner.requests == [] and sender.sent == []
    assert bot.texts == [] and bot.photos == [] and bot.documents == []
    assert not (tmp_path / "review.db").exists()
    partial = {"BYBIT_API_KEY": "k"}                         # 只填了一半也算没有
    assert tick(cfg, bot, state, "2026-11-17 00:12:30", env=partial, runner=runner) == "no_credentials"


def test_scheduler_respects_a_disabled_telegram_bot(tmp_path):
    runner = Runner(result="R")
    assert tick(on(tmp_path), Bot(enabled=False), daily.SchedulerState(), "2026-11-17 00:10:30",
                runner=runner) == "bot_off"
    assert runner.requests == []


def test_scheduler_retries_with_spacing_then_gives_up_with_one_honest_message(tmp_path):
    cfg, bot, state = on(tmp_path), Bot(), daily.SchedulerState()
    runner, sender = Runner(fail=OSError("boom")), Sender()
    assert tick(cfg, bot, state, "2026-11-17 00:10:00", runner=runner, sender=sender) == "failed"
    assert tick(cfg, bot, state, "2026-11-17 00:11:00", runner=runner, sender=sender) == "waiting"
    assert tick(cfg, bot, state, "2026-11-17 00:15:00", runner=runner, sender=sender) == "failed"
    assert bot.texts == []                                   # 还会再试，先不打扰
    assert tick(cfg, bot, state, "2026-11-17 00:20:00", runner=runner, sender=sender) == "failed"
    assert tick(cfg, bot, state, "2026-11-17 00:30:00", runner=runner, sender=sender) == "gave_up"
    assert len(runner.requests) == 3 and sender.sent == []
    assert len(bot.texts) == 1 and "没有生成" in bot.texts[0] and "2026-11-16" in bot.texts[0]
    assert "boom" not in bot.texts[0] or "连不上" in bot.texts[0]


def test_scheduler_recovers_on_a_retry(tmp_path):
    cfg, bot, state = on(tmp_path), Bot(), daily.SchedulerState()
    sender = Sender()
    assert tick(cfg, bot, state, "2026-11-17 00:10:00", runner=Runner(fail=TimeoutError()),
                sender=sender) == "failed"
    assert tick(cfg, bot, state, "2026-11-17 00:16:00", runner=Runner(result="R"),
                sender=sender) == "sent"
    assert sender.sent == [("R", True)] and bot.texts == []


def test_scheduler_reports_a_wrong_kind_of_key_once_and_stops(tmp_path):
    cfg, bot, state = on(tmp_path), Bot(), daily.SchedulerState()
    runner = Runner(fail=daily.ReviewUnavailable(daily.NOT_READ_ONLY_TEXT, "not_read_only"))
    assert tick(cfg, bot, state, "2026-11-17 00:10:00", runner=runner) == "unavailable"
    assert tick(cfg, bot, state, "2026-11-17 00:20:00", runner=runner) == "idle"
    assert len(runner.requests) == 1 and len(bot.texts) == 1
    assert "没有生成" in bot.texts[0] and "只读" in bot.texts[0]


def test_scheduler_does_not_resend_after_a_restart(tmp_path, sample):
    _, result, _ = sample
    cfg, bot = on(tmp_path), Bot()
    settings = daily.settings_from_config(cfg)
    first = daily.SchedulerState()
    runner = Runner(result=as_live(result))
    assert asyncio.run(daily.review_tick(cfg, bot, first, now=at("2026-11-17 00:10:00"), env=KEYS,
                                         runner=runner)) == "sent"
    assert len(bot.photos) == 1 and len(bot.documents) == 1
    store = ReviewStore(settings.db_path)
    assert store.has_delivery("2026-11-16", daily.DAILY_PREFIX) is True
    assert store.has_delivery("2026-11-17", daily.DAILY_PREFIX) is False
    store.close()
    # 进程重启：内存状态没了，但库里记着已经推过
    restarted = daily.SchedulerState()
    again = Runner(result=as_live(result))
    assert asyncio.run(daily.review_tick(cfg, bot, restarted, now=at("2026-11-17 00:40:00"),
                                         env=KEYS, runner=again)) == "already_sent"
    assert again.requests == [] and len(bot.photos) == 1


def test_order_snapshots_run_on_their_own_interval_and_need_the_key(tmp_path):
    calls = []

    async def syncer(settings, env=None):
        calls.append(settings.order_sync_minutes)
        return 3

    def run(cfg, state, now, env=KEYS):
        return asyncio.run(daily.order_sync_tick(cfg, state, now=at(now), env=env, syncer=syncer))

    state = daily.SchedulerState()
    cfg = Cfg(tmp_path, review__enabled=True)               # 不需要打开 Telegram 推送
    assert run(cfg, state, "2026-11-17 10:00:00", env=NO_KEYS) == "no_credentials"
    assert run(cfg, state, "2026-11-17 10:00:00") == "synced:3"
    assert run(cfg, state, "2026-11-17 10:29:00") == "idle"
    assert run(cfg, state, "2026-11-17 10:30:00") == "synced:3"
    assert calls == [30, 30]
    assert run(Cfg(tmp_path), daily.SchedulerState(), "2026-11-17 10:00:00") == "off"
    off = Cfg(tmp_path, review__enabled=True, review__order_sync_minutes=0)
    assert run(off, daily.SchedulerState(), "2026-11-17 10:00:00") == "off"


def test_scheduler_tick_never_raises(tmp_path, monkeypatch):
    async def explode(*args, **kwargs):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(daily, "review_tick", explode)
    monkeypatch.setattr(daily, "order_sync_tick", explode)
    outcome = asyncio.run(daily.scheduler_tick(on(tmp_path), Bot(), daily.SchedulerState()))
    assert outcome == {"orders": "error", "review": "error"}


def test_main_wires_the_review_task_like_the_other_isolated_tasks():
    import app.main as main
    source = inspect.getsource(main.amain)
    block = source[source.index("async def live_review"):source.index("asyncio.create_task(live_review())")]
    assert "scheduler_tick(cfg, bot, state)" in block and "await asyncio.sleep(60)" in block
    assert block.count("except Exception") == 2            # 加载失败、单次失败都只记日志
    assert "trader" not in block and "engine" not in block.replace("交易引擎", "")


# --------------------------------------------------------------------- 模拟信号日报开关

def test_paper_daily_report_is_off_by_default_and_can_be_switched_back_on():
    from app.main import paper_daily_due
    cfg = Config()
    assert paper_daily_due(cfg, True, 8, (2026, 321), None) is False
    cfg.set_override("reports.paper_daily", True)
    assert paper_daily_due(cfg, True, 8, (2026, 321), None) is True
    assert paper_daily_due(cfg, True, 8, (2026, 321), (2026, 321)) is False     # 当天发过
    assert paper_daily_due(cfg, True, 9, (2026, 321), None) is False            # 不是 8 点
    assert paper_daily_due(cfg, False, 8, (2026, 321), None) is False           # 机器人没开
    import app.main as main
    source = inspect.getsource(main.amain)
    assert "if paper_daily_due(cfg, bot.enabled, t.tm_hour, day, last_day):" in source
    assert "FROM paper_trades" in source                    # 原来的统计代码还在，只是加了开关


# --------------------------------------------------------------------- 投递

def test_delivery_of_a_finished_day(sample):
    cfg, result, _ = sample
    bot = Bot()
    asyncio.run(daily.deliver(bot, as_live(result), cfg))
    assert len(bot.photos) == 1 and len(bot.documents) == 1 and bot.texts == []
    assert bot.photos[0].startswith("<b>2026-11-16 实盘纪律 · 4 笔，2 笔合规 · 当日层面 1 项违规</b>")
    assert "未结束" not in bot.photos[0]


def test_delivery_of_an_unfinished_day_says_so_first(sample):
    cfg, result, _ = sample
    report = {**result.report, "source": "bybit", "partial": True, "window_end_ms": kit.at(15, 32)}
    bot = Bot()
    asyncio.run(daily.deliver(bot, replace(result, report=report), cfg))
    first = bot.photos[0].split("\n")[0]
    assert first == "⏳ <b>2026-11-16 未结束</b> · 截至 15:32（北京时间），以下只是到目前为止"


def test_delivery_of_a_day_without_trades_is_one_short_text(sample):
    cfg, result, _ = sample
    report = {**result.report, "source": "bybit", "trades": [],
              "score": {"total": 0, "judged": 0, "compliant": 0, "by_verdict": {}, "most_broken": None}}
    bot = Bot()
    asyncio.run(daily.deliver(bot, replace(result, report=report), cfg))
    assert bot.photos == [] and bot.documents == []
    assert len(bot.texts) == 1 and "今天没有成交" in bot.texts[0]


def test_fixture_reports_are_never_delivered(sample):
    cfg, result, _ = sample
    bot = Bot()
    with pytest.raises(daily.ReviewUnavailable):
        asyncio.run(daily.deliver(bot, result, cfg))
    assert bot.texts == [] and bot.photos == [] and bot.documents == []


# --------------------------------------------------------------------- 无密钥：每个入口

def test_run_review_without_credentials_is_a_soft_failure_that_touches_nothing(tmp_path):
    settings = daily.settings_from_config(on(tmp_path))
    request = daily.make_request("2026-11-16", settings, kit.DAY0 + 2 * kit.DAY)
    with pytest.raises(daily.ReviewUnavailable) as caught:
        asyncio.run(daily.run_review(settings, request, env=NO_KEYS))
    message = caught.value.message
    assert caught.value.code == "no_credentials" and message == daily.NO_CREDENTIALS_TEXT
    for needle in ("只读", "BYBIT_API_KEY", "BYBIT_API_SECRET", ".env", "没有生成"):
        assert needle in message
    assert "\n" not in message and message.count("。") == 1   # 就一句话
    assert not (tmp_path / "review.db").exists() and not (tmp_path / "out").exists()
    with pytest.raises(daily.ReviewUnavailable):
        asyncio.run(daily.sync_orders(settings, env=NO_KEYS))


def test_precheck_names_the_one_thing_to_do(tmp_path):
    enabled, disabled = daily.settings_from_config(on(tmp_path)), daily.settings_from_config(Cfg(tmp_path))
    assert daily.precheck(enabled, KEYS) is None
    assert daily.precheck(enabled, NO_KEYS) == daily.NO_CREDENTIALS_TEXT
    assert daily.precheck(disabled, NO_KEYS) == daily.NO_CREDENTIALS_TEXT
    assert daily.precheck(disabled, KEYS) == daily.DISABLED_TEXT
    assert "review.enabled" in daily.DISABLED_TEXT


def test_failures_are_described_without_leaking_internals():
    assert daily.describe_failure(daily.ReadOnlyKeyRequired("x")) == daily.NOT_READ_ONLY_TEXT
    assert "超时" in daily.describe_failure(asyncio.TimeoutError())
    assert "连不上" in daily.describe_failure(OSError("getaddrinfo failed"))
    assert daily.describe_failure(daily.BybitError("/v5/x: 10003 invalid")).startswith("Bybit 返回错误")
    assert len(daily.describe_failure(ValueError("z" * 1000))) < 200


# --------------------------------------------------------------------- Telegram 指令

class BotHarness:
    def __init__(self, cfg, monkeypatch, env):
        for name in ("BYBIT_API_KEY", "BYBIT_API_SECRET"):
            monkeypatch.delenv(name, raising=False)
        for name, value in env.items():
            monkeypatch.setenv(name, value)
        self.bot = TgBot(cfg, db=None)
        self.sent, self.runs, self.delivered = [], [], []

        async def api(method, **params):
            self.sent.append((method, params.get("text")))
            return {"ok": True, "result": {}}

        async def run_review(settings, request, **kwargs):
            self.runs.append(request)
            return "RESULT"

        async def deliver(bot, result, settings, **kwargs):
            self.delivered.append(result)

        self.bot.api = api
        monkeypatch.setattr(daily, "run_review", run_review)
        monkeypatch.setattr(daily, "deliver", deliver)

    def message(self, text, chat="42"):
        async def go():
            await self.bot._on_message({"chat": {"id": chat}, "text": text})
            task = self.bot._review_task
            if task is not None:
                await task
        asyncio.run(go())

    @property
    def texts(self):
        return [text for method, text in self.sent if method == "sendMessage"]


def test_review_command_replies_at_once_then_sends_the_result(tmp_path, monkeypatch):
    h = BotHarness(on(tmp_path), monkeypatch, KEYS)
    h.message("复盘")
    today = datetime.now(daily.BEIJING).date().isoformat()
    assert len(h.texts) == 1 and "生成中…" in h.texts[0] and today in h.texts[0]
    assert "今天还没结束" in h.texts[0]
    assert len(h.runs) == 1 and h.runs[0].day == today and h.runs[0].partial is True
    assert "未结束" in h.runs[0].label
    assert h.delivered == ["RESULT"]


def test_review_command_for_yesterday_is_a_full_day(tmp_path, monkeypatch):
    h = BotHarness(on(tmp_path), monkeypatch, KEYS)
    h.message("/Review 昨天")
    yesterday = (datetime.now(daily.BEIJING).date() - timedelta(days=1)).isoformat()
    assert h.runs[0].day == yesterday and h.runs[0].partial is False
    assert "今天还没结束" not in h.texts[0]


def test_review_command_from_another_chat_is_ignored(tmp_path, monkeypatch):
    h = BotHarness(on(tmp_path), monkeypatch, KEYS)
    h.message("复盘", chat="999")
    assert h.sent == [] and h.runs == [] and h.bot._review_task is None


def test_review_command_without_credentials_gets_one_sentence_and_no_review(tmp_path, monkeypatch):
    h = BotHarness(on(tmp_path), monkeypatch, NO_KEYS)
    h.message("复盘 昨天")
    assert h.texts == [daily.NO_CREDENTIALS_TEXT]
    assert h.runs == [] and h.delivered == [] and h.bot._review_task is None
    assert "生成中" not in h.texts[0]


def test_review_command_while_disabled_says_how_to_enable(tmp_path, monkeypatch):
    h = BotHarness(Cfg(tmp_path), monkeypatch, KEYS)
    h.message("复盘")
    assert h.texts == [daily.DISABLED_TEXT] and h.runs == []


def test_review_command_with_a_bad_day_or_failure_is_one_line(tmp_path, monkeypatch):
    h = BotHarness(on(tmp_path), monkeypatch, KEYS)
    h.message("复盘 明天")
    assert len(h.texts) == 1 and "没看懂" in h.texts[0] and h.runs == []

    async def broken(settings, request, **kwargs):
        raise daily.BybitError("/v5/execution/list: 10006 too many visits")

    monkeypatch.setattr(daily, "run_review", broken)
    h.sent.clear()
    h.message("复盘 昨天")
    assert len(h.texts) == 2 and "生成中…" in h.texts[0]
    assert "没有生成" in h.texts[1] and "Bybit 返回错误" in h.texts[1]
    assert h.delivered == []


def test_only_one_review_is_generated_at_a_time(tmp_path, monkeypatch):
    h = BotHarness(on(tmp_path), monkeypatch, KEYS)

    async def go():
        gate = asyncio.Event()

        async def slow(settings, request, **kwargs):
            h.runs.append(request)
            await gate.wait()
            return "RESULT"

        monkeypatch.setattr(daily, "run_review", slow)
        await h.bot._on_message({"chat": {"id": "42"}, "text": "复盘"})
        await asyncio.sleep(0)
        await h.bot._on_message({"chat": {"id": "42"}, "text": "复盘 昨天"})
        gate.set()
        await h.bot._review_task

    asyncio.run(go())
    assert len(h.runs) == 1 and h.texts[1] == daily.BUSY_TEXT


def test_other_messages_do_not_touch_the_review_module(tmp_path, monkeypatch):
    h = BotHarness(on(tmp_path), monkeypatch, KEYS)
    monkeypatch.setattr(daily, "settings_from_config", lambda cfg: 1 / 0)
    h.message("hello")
    h.message("今天复盘了吗")
    assert h.sent == [] and h.runs == []


# --------------------------------------------------------------------- 网页：只给登录用户

class Web:
    def __init__(self, tmp_path):
        self.cfg = Config()
        self.review_db = tmp_path / "review.db"
        self.cfg.set_override("review.db_path", str(self.review_db))
        self.app = server.create_app(self.cfg, DB(tmp_path / "t.db"), engine=None, bot=None)

    def anonymous(self) -> TestClient:
        return TestClient(self.app, client=("10.0.0.1", 50000))

    def logged_in(self) -> TestClient:
        client = TestClient(self.app, client=("10.0.0.9", 50000))
        reply = client.post("/api/login", json={"username": self.cfg.get("web.username", "admin"),
                                                "password": self.cfg.get("web.initial_password")})
        assert reply.status_code == 200, reply.text
        return client


@pytest.fixture
def web(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "LOGIN_FAIL_DELAY_S", 0)
    return Web(tmp_path)


def store_page(web, sample, day="2026-11-16"):
    _, result, _ = sample
    store = ReviewStore(web.review_db)
    store.replace_page(day, result.report, result.html_path.read_text(encoding="utf-8"))
    store.close()


def test_review_routes_are_not_public():
    assert not any("review" in path for path in server.PUBLIC_GET)
    for path in ("/api/reviews", "/api/reviews/2026-11-16", "/api/reviews/"):
        assert server.is_public_get(path) is False
        assert server.anonymous_allowed("GET", path) is False
        assert server.anonymous_allowed("POST", path) is False


@pytest.mark.parametrize("path", ["/api/reviews", "/api/reviews?limit=5", "/api/reviews/2026-11-16",
                                  "/api/reviews/latest", "/api/reviews/2026-11-16/raw",
                                  "/API/reviews", "/api//reviews/2026-11-16"])
def test_anonymous_cannot_read_reviews(web, sample, path):
    store_page(web, sample)
    reply = web.anonymous().get(path)
    assert reply.status_code == 401
    assert "实盘" not in reply.text and "BTCUSDT" not in reply.text


def test_anonymous_cannot_write_or_probe_reviews(web):
    client = web.anonymous()
    for method in ("post", "put", "delete", "patch"):
        assert getattr(client, method)("/api/reviews/2026-11-16").status_code == 401


def test_logged_in_user_lists_and_opens_reviews(web, sample):
    client = web.logged_in()
    assert client.get("/api/reviews").json() == {"reviews": []}      # 还没有库：空列表，不建库
    assert not web.review_db.exists()
    assert client.get("/api/reviews/2026-11-16").status_code == 404

    store_page(web, sample)
    store_page(web, sample, day="2026-11-15")
    listing = client.get("/api/reviews").json()["reviews"]
    assert [row["day"] for row in listing] == ["2026-11-16", "2026-11-15"]
    assert listing[0]["total"] == 4 and listing[0]["compliant"] == 2
    assert listing[0]["status"] == "reconciled" and listing[0]["partial"] is False
    assert listing[0]["most_broken"] == "只有止盈与止损两种离场"
    assert "html" not in listing[0]
    assert len(client.get("/api/reviews?limit=1").json()["reviews"]) == 1
    assert len(client.get("/api/reviews?limit=-5").json()["reviews"]) == 1

    page = client.get("/api/reviews/2026-11-16")
    assert page.status_code == 200 and page.headers["content-type"].startswith("text/html")
    assert "今天我有没有按规矩做？" in page.text and "离场之后（事后对照，不改变结论）" in page.text
    assert page.headers["cache-control"] == "no-store"
    assert "default-src 'none'" in page.headers["content-security-policy"]
    assert page.headers["x-frame-options"] == "DENY"


@pytest.mark.parametrize("day", ["2026-13-01", "2026-02-30", "20261116", "2026-11-16x", "latest",
                                 "..", "2026-11-1６", "%2e%2e", "2026-11-16%00", "0000-00-00"])
def test_day_parameter_is_validated_strictly(web, sample, day):
    store_page(web, sample)
    reply = web.logged_in().get(f"/api/reviews/{day}")
    assert reply.status_code in (400, 404) and "今天我有没有按规矩做" not in reply.text
    if reply.status_code == 400:
        assert reply.json()["error"] == "日期格式应为 YYYY-MM-DD"


def test_reading_reviews_never_calls_the_exchange_or_creates_files(web, tmp_path):
    source = inspect.getsource(server.create_app)
    block = source[source.index('@app.get("/api/reviews"'):source.index('@app.websocket("/api/ws")')]
    assert "dependencies=LOGIN" in block and block.count("dependencies=LOGIN") == 2
    assert "bybit_client" not in block and "daily" not in block
    assert read_pages(tmp_path / "missing.db") == [] and read_page_html(tmp_path / "missing.db", "x") is None
    assert not (tmp_path / "missing.db").exists()


# --------------------------------------------------------------------- 留存与幂等

def order_row(trigger="239.40", status="untriggered", updated=2000):
    return OrderRecord(
        exchange="bybit", account_id="default", order_id="sl-1", symbol="SOLUSDT", side="Sell",
        position_idx=0, order_type="Market", qty=Decimal("12"), filled_qty=Decimal("0"), price=None,
        avg_fill_price=None, trigger_price=Decimal(trigger), reduce_only=True, close_position=True,
        status=status, origin="stop_loss", attached_sl=None, attached_tp=None, created_ms=1000,
        updated_ms=updated)


def test_order_snapshots_keep_every_distinct_state_and_nothing_twice(tmp_path):
    store = ReviewStore(tmp_path / "review.db")
    assert store.upsert_orders([order_row()], seen_ms=10) == 1
    assert store.upsert_orders([order_row()], seen_ms=20) == 0            # 没变化：不重复记
    assert store.upsert_orders([order_row("238.00", updated=3000)], seen_ms=30) == 1
    assert store.upsert_orders([order_row("238.00", "filled", 4000)], seen_ms=40) == 1
    assert store.count("bybit_orders") == 1 and store.count("bybit_order_observations") == 3
    seen = store.order_observations(0)
    assert [(row.trigger_price, row.status) for row in seen] == [
        (Decimal("239.40"), "untriggered"), (Decimal("238.00"), "untriggered"),
        (Decimal("238.00"), "filled")]
    assert seen[0] == order_row() and isinstance(seen[0].qty, Decimal) and seen[0].price is None
    assert [row.updated_ms for row in store.order_observations(3000)] == [3000, 4000]
    assert order_from_payload(store._payload(order_row())) == order_row()
    store.close()


def test_review_for_the_same_day_replaces_itself(sample):
    cfg, result, exchange = sample
    client = BybitReadClient("fixture", "fixture", requester=exchange.requester)
    request = daily.make_request("2026-11-16", cfg, exchange.now_ms)
    again = asyncio.run(daily.run_review(cfg, request, client=client, now_ms=exchange.now_ms,
                                         source="fixture"))
    assert again.report == result.report
    store = ReviewStore(cfg.db_path)
    try:
        for table in ("daily_trade_reviews", "daily_review_pages"):
            assert store.count(table) == 1
        assert store.count("bybit_executions") == 8 and store.count("bybit_transactions") == 8
        assert store.count("bybit_closed_pnl") == 4
        assert store.count("bybit_order_observations") == 12
        assert [row["day"] for row in store.list_pages()] == ["2026-11-16"]
        assert store.page_html("2026-11-16") == result.html_path.read_text(encoding="utf-8")
    finally:
        store.close()


def test_delivery_marks_are_scoped_by_prefix(tmp_path):
    store = ReviewStore(tmp_path / "review.db")
    store.mark_delivered("2026-11-16", "abc")                      # 手动 / 命令行
    assert store.has_delivery("2026-11-16", daily.DAILY_PREFIX) is False
    store.mark_delivered("2026-11-16", daily.DAILY_PREFIX + "abc")
    assert store.has_delivery("2026-11-16", daily.DAILY_PREFIX) is True
    assert store.has_delivery("2026-11-16") is True
    store.close()


# --------------------------------------------------------------------- K 线：有界、不重复

class FakeKlines:
    def __init__(self, fail=False):
        self.calls, self.fail = [], fail

    async def klines(self, category, symbol, interval, start_ms, end_ms):
        self.calls.append((symbol, interval, start_ms, end_ms))
        if self.fail:
            raise OSError("down")
        step = int(interval) * 60_000
        return [daily.Kline(stamp, *[Decimal("1")] * 6) for stamp in range(start_ms, end_ms, step)]


def test_kline_book_fetches_each_range_once_in_requests_of_at_most_1000_bars():
    client = FakeKlines()
    book = daily.KlineBook(client, now_ms=10_000 * 60_000, pause=0, budget=10)
    assert asyncio.run(book.ensure("BTCUSDT", 1, 0, 2500 * 60_000)) is True
    assert [(end - start) // 60_000 for _, _, start, end in client.calls] == [1000, 1000, 500]
    assert asyncio.run(book.ensure("BTCUSDT", 1, 100 * 60_000, 2000 * 60_000)) is True
    assert len(client.calls) == 3                                  # 已经取过的不再取
    assert asyncio.run(book.ensure("BTCUSDT", 1, 2400 * 60_000, 2600 * 60_000)) is True
    assert client.calls[-1][2:] == (2500 * 60_000, 2600 * 60_000)    # 只补缺的那一段
    assert len(book.get("BTCUSDT", 1)) == 2600
    assert len(book.get("BTCUSDT", 1, 60_000, 180_000)) == 2
    assert asyncio.run(book.ensure("BTCUSDT", 1, 9_990 * 60_000, 20_000 * 60_000)) is True
    assert client.calls[-1][3] == 10_000 * 60_000                  # 不取「未来」的 K 线


def test_kline_book_stops_at_its_budget_and_survives_errors():
    client = FakeKlines()
    book = daily.KlineBook(client, now_ms=10**12, pause=0, budget=2)
    assert asyncio.run(book.ensure("ETHUSDT", 5, 0, 5000 * 300_000)) is False
    assert len(client.calls) == 2 and book.exhausted is True
    broken = daily.KlineBook(FakeKlines(fail=True), now_ms=10**12, pause=0, budget=5)
    assert asyncio.run(broken.ensure("ETHUSDT", 5, 0, 300_000)) is False
    assert broken.get("ETHUSDT", 5) == [] and "ETHUSDT 5m" in broken.errors[0]


def test_fixture_exchange_answers_klines_in_the_documented_shape():
    exchange = FixtureExchange.load(FIXTURES / "day_full.json")
    reply = asyncio.run(exchange.requester("/v5/market/kline", {
        "category": "linear", "symbol": "BTCUSDT", "interval": "15",
        "start": kit.at(9, 0), "end": kit.at(11, 0) - 1, "limit": 1000}, False))
    rows = reply["result"]["list"]
    assert reply["retCode"] == 0 and len(rows) == 8
    assert all(len(row) == 7 and all(isinstance(cell, str) for cell in row) for row in rows)
    assert [int(row[0]) for row in rows] == sorted((int(row[0]) for row in rows), reverse=True)
    for _, open_, high, low, close, volume, turnover in rows:
        assert Decimal(low) <= min(Decimal(open_), Decimal(close))
        assert Decimal(high) >= max(Decimal(open_), Decimal(close))
        assert Decimal(volume) > 0 and Decimal(turnover) > 0
    future = asyncio.run(exchange.requester("/v5/market/kline", {
        "symbol": "BTCUSDT", "interval": "60", "start": exchange.now_ms + 3_600_000,
        "end": exchange.now_ms + 7_200_000, "limit": 10}, False))
    assert future["result"]["list"] == []
    unknown = asyncio.run(exchange.requester("/v5/market/kline", {
        "symbol": "DOGEUSDT", "interval": "1", "start": kit.at(9, 0), "end": kit.at(9, 5)}, False))
    assert unknown["result"]["list"] == []


# --------------------------------------------------------------------- 只读保证 / 旧纪律层已退役

def test_review_code_contains_no_way_to_trade():
    forbidden = ("session.post", ".post(", "/v5/order/create", "/v5/order/cancel", "/v5/order/amend",
                 "/v5/position/trading-stop", "/v5/position/set-leverage", "/v5/asset/transfer",
                 "withdraw")
    for name in ("app/review/daily.py", "app/review/fixture_exchange.py", "app/review/store.py",
                 "scripts/bybit_daily_review.py"):
        text = (ROOT / name).read_text(encoding="utf-8").lower()
        for needle in forbidden:
            assert needle not in text, (name, needle)
    assert "verify_read_only()" in inspect.getsource(daily.fetch_account).split("await client.")[1]
    assert "verify_read_only()" in inspect.getsource(daily.sync_orders)


def test_fixture_cannot_be_used_to_reach_a_non_read_endpoint():
    exchange = FixtureExchange({"responses": {}})
    client = BybitReadClient("fixture", "fixture", requester=exchange.requester)
    with pytest.raises(daily.BybitError):
        asyncio.run(client._get("/v5/order/create", {}, True))
    assert exchange.calls == []
    assert {"/v5/order/history", "/v5/order/realtime", "/v5/execution/list"} <= READ_PATHS


def test_old_discipline_layer_is_gone():
    assert not (ROOT / "app" / "review" / "discipline.py").exists()
    assert importlib.util.find_spec("app.review.discipline") is None
    for path in list((ROOT / "app").rglob("*.py")) + list((ROOT / "scripts").glob("*.py")):
        text = path.read_text(encoding="utf-8", errors="ignore")
        assert "review.discipline" not in text and "evaluate_episode" not in text, path
    retired = ("部分合规", "冲动交易", "严重变形", "暂不适用", "数据待核对", "入场不在窗口")
    for name in ("app/review/daily.py", "scripts/bybit_daily_review.py", "app/review/principles.py"):
        text = (ROOT / name).read_text(encoding="utf-8")
        assert not any(word in text for word in retired), name


# --------------------------------------------------------------------- 文字整理（自旧测试迁移）

def test_prompt_built_from_a_real_review_has_checks_but_no_account_data(sample):
    _, result, _ = sample
    for row in result.report["trades"]:
        prompt = build_prompt(row["principles"])
        assert row["principles"]["verdict"] in prompt and "只有止盈与止损两种离场" in prompt
        lowered = prompt.lower()
        assert "api" not in lowered and "account" not in lowered and "secret" not in lowered
        assert "net_pnl" not in prompt and "realized_r" not in prompt


def test_ai_wording_cannot_override_the_verdict_or_add_money(sample):
    _, result, _ = sample
    review = result.report["trades"][1]["principles"]              # 中途下车
    raw = json.dumps({"summary": "其实做得很好", "reasons": ["只有止盈与止损两种离场：合规"],
                      "next_action": "继续", "confidence": "high", "verdict": "合规", "pnl": 999999},
                     ensure_ascii=False)
    narrative = parse_or_fallback(raw, review)
    assert narrative.source == "deterministic" and "中途下车" in narrative.summary
    assert not hasattr(narrative, "verdict") and not hasattr(narrative, "pnl")
    assert parse_or_fallback("not json", review).source == "deterministic"
