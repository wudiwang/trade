"""评审问题的回归测试(全离线: 临时 sqlite、假配置、假行情、假 Telegram)。

每个测试对应一条被确认的评审发现, 测的是「修之前会错的那个具体场景」:
  联动规则  断线缺口后与到达顺序无关 / 丢掉的K补回后重算(含更正) / 停机期间出结果的补报 / 引擎后台补拉
  通知通路  发送失败重试、Telegram ok=false 也算失败、健康计数能在列表里看到
  第七条    未审核规则的模拟结果不进日报合计与统计接口, 只报笔数
  下单闸    模拟/提示行不能走「确认买入」回调; 模拟行的 state 不被止损改写
  提醒引擎  重启不重报 / 阶梯下跌续跌 / 收盘K补判破位 / 停机期间触及的价位 / 过期行情与离谱价位
不计算、不断言任何胜率/期望(CLAUDE.md 第七条)。

python -m pytest tests/test_review_fixes.py -q
"""
import asyncio
import json
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import test_alerts as ta  # noqa: E402
import test_alerts_integration as ti  # noqa: E402
from test_alerts_integration import (  # noqa: E402
    BASE, BTC, BTC_BARS, PUMP, PUMP_BARS, RULE, SIGNAL_I, T, TF, Cfg, Env, Tripwire,
    btc_series, pump_series, rows, run, tup)

import app.bot.telegram as tg  # noqa: E402
import app.web.server as server  # noqa: E402
from app.config import Config  # noqa: E402
from app.db import DB  # noqa: E402
from app.engine import core as core_mod  # noqa: E402
from app.engine import linked_rules as lr  # noqa: E402
from app.engine.linked_rules import LinkedRuleRunner, unaudited_tracks  # noqa: E402
from app.engine.paper import PaperBroker  # noqa: E402
from app.engine.strat_btcbreak import replay  # noqa: E402
from app.main import daily_close_lines  # noqa: E402

COLS = ("open_time", "open", "high", "low", "close", "volume", "quote_volume", "taker_buy", "closed")


@pytest.fixture
def db(tmp_path):
    return DB(tmp_path / "t.db")


@pytest.fixture
def clock():
    return ti.Clock()


def backfill(env, sym, bars):
    """模拟 Engine.backfill_all 的效果: K线进库 + 进内存缓存, 但【不】走收盘回调。"""
    env.db.upsert_klines(sym, "5m", list(bars))
    dq = env.engine.cache.setdefault((sym, "5m"), __import__("collections").deque(maxlen=550))
    have = {b["open_time"] for b in dq}
    for b in bars:
        if b[0] not in have:
            dq.append(dict(zip(COLS, b)))


def params():
    return lr.params_from_entry(RULE)


# =====================================================================================
# 联动规则: 断线缺口 —— 结果不能取决于哪个品种的K先到(评审: high)
# =====================================================================================

@pytest.mark.parametrize("first", [BTC, PUMP])
def test_gap_covering_trigger_is_rebuilt_in_time_order_regardless_of_arrival(db, clock, first):
    """缺口盖住了 BTC 破位K和 PUMP 信号K(看门狗回填把它们补进了库和缓存, 但没走收盘回调)。
    缺口后第一根实时K无论是 BTC 的还是 PUMP 的先到: 都报出同一个(过期的)入场, 不开模拟单, 且与整段回放一致。
    修之前: PUMP 先到时, 它缺口里的K在「还没看到触发K」的状态下被喂掉 → 真正的入场丢失, 最后报「到期」。"""
    async def go():
        env = Env(db, clock)
        await env.feed(BTC_BARS, PUMP_BARS, 0, T)                       # 破位之前一切正常
        gap = range(T, SIGNAL_I + 2)                                    # T .. SIGNAL_I+1 断线
        backfill(env, BTC, [BTC_BARS[i] for i in gap])
        backfill(env, PUMP, [PUMP_BARS[i] for i in gap])
        nxt = SIGNAL_I + 2
        order = [(BTC, BTC_BARS), (PUMP, PUMP_BARS)]
        if first == PUMP:
            order.reverse()
        for sym, series in order:
            await env.bar(sym, series[nxt])
        await env.drain()

        assert len(env.notices) == 2, env.notices
        assert "联动规则待命" in env.notices[0] and "补报" in env.notices[0]
        assert "模拟入场·补报" in env.notices[1] and "信号K已过, 未开模拟单" in env.notices[1]
        assert not any("到期" in m for m in env.notices)
        sig = rows(db, "SELECT extra FROM signals")
        assert len(sig) == 1
        ev = json.loads(sig[0]["extra"])
        assert ev["late"] is True
        want = [e for e in replay(BTC_BARS[:nxt + 1], PUMP_BARS[:nxt + 1], params())["events"]
                if e["type"] == "entry"]
        assert len(want) == 1
        assert ev["event"]["entry"]["signal_time"] == want[0]["entry"]["signal_time"] == BASE + SIGNAL_I * TF
        assert ev["event"]["entry"]["fx_low_time"] == want[0]["entry"]["fx_low_time"]
        assert rows(db, "SELECT id FROM paper_trades") == []            # 过期信号不开模拟单
        st = env.engine.linked.status()
        assert st["phase"] == "idle" and st["counts"]["gap_missing"] == 0 and st["counts"]["resync"] >= 1
        # 之后的实时K不再为这一轮发任何东西
        await env.feed(BTC_BARS, PUMP_BARS, nxt + 1, nxt + 6)
        assert len(env.notices) == 2 and len(rows(db, "SELECT id FROM signals")) == 1
        env.untouched()
    run(go())


def test_any_gap_any_arrival_order_matches_replay(db, clock, tmp_path):
    """缺口放在破位前后的各个位置、长度 1~6、两种到达顺序: 入库的入场集合都与整段回放相同。"""
    want = {(e["entry"]["fx_low_time"], e["entry"]["signal_time"])
            for e in replay(BTC_BARS, PUMP_BARS, params())["events"] if e["type"] == "entry"}
    assert len(want) == 1
    n = 0

    async def one(g0, glen, first):
        d = DB(tmp_path / f"g{g0}_{glen}_{first}.db")
        env = Env(d, clock)
        await env.feed(BTC_BARS, PUMP_BARS, 0, g0)
        gap = range(g0, g0 + glen)
        backfill(env, BTC, [BTC_BARS[i] for i in gap])
        backfill(env, PUMP, [PUMP_BARS[i] for i in gap])
        for i in range(g0 + glen, SIGNAL_I + 10):
            pair = [(BTC, BTC_BARS[i]), (PUMP, PUMP_BARS[i])]
            if first == PUMP:
                pair.reverse()
            for sym, b in pair:
                await env.bar(sym, b)
        await env.drain()
        got = {(json.loads(r["extra"])["event"]["entry"]["fx_low_time"],
                json.loads(r["extra"])["event"]["entry"]["signal_time"]) for r in rows(d, "SELECT extra FROM signals")}
        assert got == want, (g0, glen, first, got)
        assert sum("联动规则待命" in m for m in env.notices) == 1, (g0, glen, first, env.notices)
        assert not any("更正" in m or "到期" in m for m in env.notices), (g0, glen, first, env.notices)

    async def go():
        nonlocal n
        for g0 in range(T - 2, SIGNAL_I + 1):
            for glen in (1, 2, 4, 6):
                for first in (BTC, PUMP):
                    await one(g0, glen, first)
                    n += 1
    run(go())
    assert n == 48


def test_watchdog_backfill_reports_gap_events_without_waiting_for_next_bar(db, clock):
    """看门狗回填之后引擎立刻让规则按补回的K线重建: 缺口里的待命/入场马上补报, 之后的实时K不重复。"""
    async def go():
        env = Env(db, clock)
        await env.feed(BTC_BARS, PUMP_BARS, 0, T)
        gap = range(T, SIGNAL_I + 2)
        backfill(env, BTC, [BTC_BARS[i] for i in gap])
        backfill(env, PUMP, [PUMP_BARS[i] for i in gap])
        clock.t = (BASE + (SIGNAL_I + 2) * TF) / 1000.0 + 30
        env.engine._after_backfill()
        await env.drain()
        assert len(env.notices) == 2 and "联动规则待命" in env.notices[0] and "模拟入场·补报" in env.notices[1]
        await env.feed(BTC_BARS, PUMP_BARS, SIGNAL_I + 2, SIGNAL_I + 6)
        assert len(env.notices) == 2 and rows(db, "SELECT id FROM paper_trades") == []
        env.untouched()
    run(go())


# =====================================================================================
# 联动规则: 短暂重连丢掉的那根K(缓存和库里都没有) —— 以前永远补不回来、也没人知道(评审: medium)
# =====================================================================================

def _feed_runner(r, db, clock, btc, pump, start, stop, skip=()):
    out = []
    for i in range(start, stop):
        clock.t = (BASE + (i + 1) * TF) / 1000.0
        for sym, series in ((BTC, btc), (PUMP, pump)):
            if (sym, i) in skip:
                continue
            db.upsert_klines(sym, "5m", [series[i]])              # 引擎在调规则之前已把这根K入库
            out += r.on_closed_bar(sym, "5m", series[i])
    return out


def test_lost_trigger_bar_is_visible_then_refetched_and_announced_late(db, clock):
    cfg = Cfg()
    r = LinkedRuleRunner(cfg, db, PaperBroker(cfg, db), lambda s, tf: [], now=clock)
    for rt in r._rules:
        rt.seeded = True
    out = _feed_runner(r, db, clock, BTC_BARS, PUMP_BARS, 0, T + 2, skip={(BTC, T)})   # BTC 破位K丢了
    assert out == [] and r.status()["phase"] == "idle"            # 没看到破位K → 没待命
    st = r.status()
    assert st["counts"]["gap_missing"] == 1 and "缺K 1 根(补拉中)" in st["summary"]     # 但这个洞是看得见的
    assert r.wants_refetch() is True
    reqs = r.refetch_requests()                                   # 引擎后台任务据此去补拉(这里手工补)
    assert [(s, tf) for s, tf, _ in reqs] == [(BTC, "5m")] and reqs[0][2] >= 3
    assert r.refetch_requests() == []                             # 30 秒内不重复要
    db.upsert_klines(BTC, "5m", [BTC_BARS[T]])
    msgs = r.resync()
    assert len(msgs) == 1 and "联动规则待命" in msgs[0] and "补报" in msgs[0]
    st = r.status()
    assert st["phase"] == "armed" and st["counts"]["gap_missing"] == 0 and st["counts"]["gap_filled"] == 1
    assert "缺K" not in st["summary"] and r.wants_refetch() is False
    assert r.resync() == []                                       # 再重建一次: 什么都不重复
    # 之后信号K是实时到的 → 正常开模拟单
    out = _feed_runner(r, db, clock, BTC_BARS, PUMP_BARS, T + 2, SIGNAL_I + 1)
    assert len(out) == 1 and "模拟入场" in out[0] and "补报" not in out[0] and "模拟单 #" in out[0]
    assert len(rows(db, "SELECT id FROM paper_trades")) == 1


def test_lost_wick_bar_false_arm_is_corrected_after_refetch(db, clock):
    """丢的那根K才是真正的窗口低点(下影 95): 没有它, 下一根放量收 99.2 看起来「跌破了 99.5」→ 误报待命。
    补回之后重算: 待命不成立 → 发一条更正, 状态回到空闲。
    (窗口取 5 小时 = 60 根: 覆盖率门槛 98% 容得下缺一根; 实盘的 48 小时窗口容得下缺 11 根。)"""
    rule = {**RULE, "lookback_hours": 5.0}
    X = 70
    btc = []
    for i in range(X + 6):
        if i == X:
            btc.append(tup(i, 100, 100.5, 95.0, 99.8))            # 长下影, 收回去了; 量普通 → 本身不是破位
        elif i == X + 1:
            btc.append(tup(i, 99.8, 99.9, 99.1, 99.2, 400.0))     # 放量, 收 99.2: 低于 99.5(旧低)但高于 95(真低)
        else:
            btc.append(tup(i, 100, 100.5, 99.5, 100))
    pump = pump_series("d" * (X + 6))
    # 对照: 一根不丢 → 规则始终空闲
    assert replay(btc, pump, lr.params_from_entry(rule))["events"] == []

    cfg = Cfg(linked_rules=[rule])
    r = LinkedRuleRunner(cfg, db, PaperBroker(cfg, db), lambda s, tf: [], now=clock)
    for rt in r._rules:
        rt.seeded = True
    out = _feed_runner(r, db, clock, btc, pump, 0, X + 2, skip={(BTC, X)})
    assert len(out) == 1 and "联动规则待命" in out[0] and "跌破5h低点 99.5" in out[0]   # 按残缺数据的误报
    assert r.status()["phase"] == "armed" and r.wants_refetch()
    r.refetch_requests()
    db.upsert_klines(BTC, "5m", [btc[X]])
    msgs = r.resync()
    assert len(msgs) == 1 and "联动规则更正" in msgs[0] and "待命(破位K" in msgs[0] and "不成立" in msgs[0]
    st = r.status()
    assert st["phase"] == "idle" and st["counts"]["retracted"] == 1 and st["counts"]["gap_missing"] == 0
    assert r.resync() == []


def test_unfillable_hole_is_given_up_and_stays_visible(db, clock):
    cfg = Cfg()
    r = LinkedRuleRunner(cfg, db, PaperBroker(cfg, db), lambda s, tf: [], now=clock)
    for rt in r._rules:
        rt.seeded = True
    _feed_runner(r, db, clock, BTC_BARS, PUMP_BARS, 0, T + 2, skip={(PUMP, T)})
    assert r.wants_refetch()
    for k in range(lr.REFETCH_MAX_ATTEMPTS):
        clock.t += lr.REFETCH_MIN_INTERVAL_S + 1
        assert r.refetch_requests() != []
        assert r.resync() == []                                   # 补拉没拿到那根K
    clock.t += lr.REFETCH_MIN_INTERVAL_S + 1
    assert r.refetch_requests() == [] and r.wants_refetch() is False      # 次数用完 → 放弃, 不再请求
    st = r.status()
    assert st["counts"]["gap_lost"] == 1 and st["counts"]["gap_missing"] == 0
    assert "缺K 1 根未补回" in st["summary"]


class _KlineRest:
    """只有公开K线接口的假 REST(别的属性一概没有 → 碰了就 AttributeError)。"""

    def __init__(self, bars_by_symbol):
        self.bars, self.calls = bars_by_symbol, []

    async def klines(self, symbol, interval, limit=500, start_time=None):
        self.calls.append((symbol, interval, limit))
        return list(self.bars[symbol][-limit:])


def test_engine_refetches_lost_bar_in_background_and_notifies(db, clock, monkeypatch):
    async def go():
        monkeypatch.setattr(core_mod, "LINKED_REFETCH_DELAY_S", 0.0)
        env = Env(db, clock)
        e = env.engine
        await env.feed(BTC_BARS, PUMP_BARS, 0, T)
        await env.bar(PUMP, PUMP_BARS[T])                         # BTC 的破位K T 在重连间隙里丢了
        e.rest = _KlineRest({BTC: BTC_BARS[:T + 2], PUMP: PUMP_BARS[:T + 2]})
        await env.bar(BTC, BTC_BARS[T + 1])                       # 下一根到了才发现有洞 → 起后台任务
        assert e._linked_refetch_task is not None and env.notices == []
        await env.bar(PUMP, PUMP_BARS[T + 1])                     # 收盘回调没有等网络
        await e._linked_refetch_task
        await env.drain()
        assert [c[0] for c in e.rest.calls] == [BTC]              # 只拉缺K的那个品种, 一次
        assert len(env.notices) == 1 and "联动规则待命" in env.notices[0] and "补报" in env.notices[0]
        assert e.linked.status()["phase"] == "armed" and e.linked.wants_refetch() is False
        assert len(e.cache[(BTC, "5m")]) == T + 1                 # 内存缓存(别的策略读的那份)没被动过
        assert env.trader.hits == [] and env.auto_calls == [] and env.sub_calls == []
        # REST 整个不可用: 后台任务自己吞掉, 引擎照常
        e.rest = Tripwire("rest")
        e.linked._refetch_wanted = True
        e._linked_refetch_kick()
        await e._linked_refetch_task
        await env.bar(BTC, BTC_BARS[T + 2])
    run(go())


# =====================================================================================
# 联动规则: 停机期间出了结果 —— 说过「待命, 有效至…」就得有下文(评审: medium)
# =====================================================================================

def test_entry_completed_during_downtime_is_reported_late_once(db, clock):
    async def go():
        env1 = Env(db, clock)
        await env1.feed(BTC_BARS, PUMP_BARS, 0, T + 2)
        assert len(env1.notices) == 1 and "联动规则待命" in env1.notices[0]
    run(go())
    for i in range(T + 2, SIGNAL_I + 3):                          # 停机: 这几根由启动回填补进库
        db.upsert_klines(BTC, "5m", [BTC_BARS[i]])
        db.upsert_klines(PUMP, "5m", [PUMP_BARS[i]])
    clock.t = (BASE + (SIGNAL_I + 3) * TF) / 1000.0 + 20
    cfg = Cfg()
    r = LinkedRuleRunner(cfg, db, PaperBroker(cfg, db), lambda s, tf: [], now=clock)
    msgs = r.seed()
    assert len(msgs) == 1 and "模拟入场·补报" in msgs[0] and "信号K已过, 未开模拟单" in msgs[0]
    sig = rows(db, "SELECT * FROM signals")
    assert len(sig) == 1 and sig[0]["kind"] == "alert" and sig[0]["status"] == "sim"
    assert json.loads(sig[0]["extra"])["late"] is True
    assert rows(db, "SELECT id FROM paper_trades") == []
    le = r.status()["last_entry"]
    assert le["signal_id"] == sig[0]["id"] and le["recorded"] is True and le["late"] is True
    # 再重启: 不重复
    r2 = LinkedRuleRunner(cfg, db, PaperBroker(cfg, db), lambda s, tf: [], now=clock)
    assert r2.seed() == [] and len(rows(db, "SELECT id FROM signals")) == 1
    assert r2.status()["counts"]["dup_skipped"] == 0


def test_expiry_during_downtime_is_reported_once(db, clock):
    btc, pump = btc_series(T + 20, {T}), pump_series("d" * (T + 20))

    async def go():
        env1 = Env(db, clock)
        await env1.feed(btc, pump, 0, T + 2)
        assert len(env1.notices) == 1
    run(go())
    for i in range(T + 2, T + 20):
        db.upsert_klines(BTC, "5m", [btc[i]])
        db.upsert_klines(PUMP, "5m", [pump[i]])
    clock.t = (BASE + (T + 20) * TF) / 1000.0 + 20
    cfg = Cfg()
    r = LinkedRuleRunner(cfg, db, PaperBroker(cfg, db), lambda s, tf: [], now=clock)
    msgs = r.seed()
    assert len(msgs) == 1 and "联动规则到期(补报)" in msgs[0]
    r2 = LinkedRuleRunner(cfg, db, PaperBroker(cfg, db), lambda s, tf: [], now=clock)
    assert r2.seed() == [] and rows(db, "SELECT id FROM signals") == []


# =====================================================================================
# 通知通路: 一次性的提醒不能因为一次发送失败就丢(评审: medium ×2)
# =====================================================================================

def test_notice_is_retried_until_delivered_and_false_counts_as_failure(db, clock, monkeypatch):
    async def go():
        sleeps = []
        real_sleep = asyncio.sleep

        async def fake_sleep(s):
            sleeps.append(s)
            await real_sleep(0)

        monkeypatch.setattr(core_mod.asyncio, "sleep", fake_sleep)
        env = Env(db, clock)
        e = env.engine
        got, calls = [], []

        class Bot:
            notify_retry_after = None

            async def notify(self, text):
                calls.append(text)
                if len(calls) == 1:
                    raise TimeoutError("telegram timeout")        # 第一次: 网络异常
                if len(calls) == 2:
                    self.notify_retry_after = 7.0                 # 第二次: Telegram 回 ok=false + 429
                    return False
                got.append(text)
                return True

        e.notice_subscribers[:] = [Bot().notify]
        e._notify_bg(["价位到了"])
        await env.drain()
        assert got == ["价位到了"] and len(calls) == 3            # 最终送达, 且只送达一次
        assert sleeps == [core_mod.NOTICE_RETRY_DELAYS_S[0], 7.0]  # 第二次等待照 Telegram 说的 7 秒
        h = e.notice_health()
        assert h["notice_failed"] == 0 and h["notice_retried"] == 2 and h["hook_errors"] == {"notice_send": 2}
        assert server.health_summary(h) == "重试 2 次"

        # Telegram 断了一阵(重试表试完还不通): 记一条发送失败, 列表里看得见; 这条留着等补发
        state = {"up": False}

        async def flaky(text):
            if not state["up"]:
                return False
            got.append(text)
            return True

        e.notice_subscribers[:] = [flaky]
        e._notify_bg(["断线时的那条"])
        await env.drain()
        h = e.notice_health()
        assert h["notice_failed"] == 1 and h["notice_last_fail_at"] > 0 and h["notice_backlog"] == 1
        assert "通知发送失败 1 条(待补发 1)" in server.health_summary(h)
        view = server.alerts_view(e)
        assert "通知发送失败 1 条" in view["health_summary"] and view["health"]["notice_failed"] == 1
        listing = tg.alert_reply({"action": "list"}, e.alerts, e.linked, h)
        assert "⚠️ 通知通路: " in listing and "通知发送失败 1 条" in listing
        # 恢复之后: 后台补发一次, 标明是延迟送达
        state["up"] = True
        for _ in range(20):
            await real_sleep(0)
        assert len(got) == 2 and got[1].splitlines()[0] == "断线时的那条"
        assert got[1].splitlines()[1].startswith("(延迟送达: 当时未能发出, 原定 ")
        h = e.notice_health()
        assert h["notice_recovered"] == 1 and h["notice_backlog"] == 0 and h["notice_lost"] == 0
        assert "已补发 1" in server.health_summary(h)
        assert core_mod.NOTICE_BACKLOG_RETRY_S in sleeps
        assert "通知通路" not in tg.alert_reply({"action": "list"}, e.alerts, e.linked, {"notice_failed": 0})
        assert set(e.status()) == {"started_at", "uptime_s", "symbols", "ws_conns", "ws_last_msg_age_s",
                                   "last_eval_ms", "mode", "tracks", "macro", "squeeze", "funnel"}   # 公开接口没多东西
    run(go())


def test_fired_level_survives_a_telegram_refusal(db, clock, monkeypatch):
    """评审复现: 价位触发的那一刻 Telegram 回 ok=false(429)。修之前: 价位已标 fired, 消息没了, 之后再也不发。"""
    async def go():
        async def fake_sleep(s):
            return None

        monkeypatch.setattr(core_mod.asyncio, "sleep", fake_sleep)
        env = Env(db, clock, **{"alerts.enabled": True, "alerts.drop.enabled": False,
                                "alerts.range_break.enabled": False})
        e = env.engine
        bot = tg.TgBot(ta_cfg(), db=None, trader=Tripwire("trader"))
        replies = [{"ok": False, "error_code": 429, "parameters": {"retry_after": 3}}, {"ok": True, "result": {}}]
        sent = []

        async def fake_api(method, **params):
            sent.append(params["text"])
            return replies.pop(0) if replies else {"ok": True, "result": {}}

        bot.api = fake_api
        e.notice_subscribers[:] = [bot.notify]
        await env.bar(BTC, tup(0, 100, 100.5, 99.5, 100))
        assert e.alerts.add_level(BTC, 99.7, "below", "前低")["ok"] is True
        clock.t += 10
        e._on_tick(BTC, 99.6, int(clock.t * 1000))
        await env.drain()
        assert db.one("SELECT status FROM price_alerts")["status"] == "fired"
        assert len(sent) == 2 and sent[0] == sent[1] and "价位提醒" in sent[1]      # 被拒一次, 重试送达
        assert bot.notify_retry_after is None and e.notice_health()["notice_failed"] == 0
        assert env.trader.hits == []
    run(go())


def ta_cfg():
    class C:
        tg_token = "TEST-TOKEN"
        tg_chat_id = "42"
        mode = "live"

        def get(self, key, default=None):
            return default
    return C()


def test_notify_reports_delivery_result_without_new_exceptions():
    async def go():
        bot = tg.TgBot(ta_cfg(), db=None, trader=Tripwire("trader"))
        out = [{"ok": True}, {"ok": False, "error_code": 400, "description": "can't parse entities"},
               {"ok": False, "error_code": 429, "parameters": {"retry_after": 12}}, {}]

        async def fake_api(method, **params):
            return out.pop(0)

        bot.api = fake_api
        assert await bot.notify("a") is True
        assert await bot.notify("b") is False and bot.notify_retry_after is None
        assert await bot.notify("c") is False and bot.notify_retry_after == 12.0
        assert await bot.notify("d") is False
        bot.enabled = False
        assert await bot.notify("e") is None                      # 未启用: 不算失败, 不重试
    run(go())


# =====================================================================================
# 第七条: 未审核规则的模拟结果不出统计(评审: medium ×2)
# =====================================================================================

def test_unaudited_tracks_from_config():
    assert unaudited_tracks(Cfg()) == {"btcbreak_pump"}                      # 没写 audited = 未审核
    assert unaudited_tracks(Cfg(linked_rules=[{**RULE, "audited": True}])) == set()
    assert unaudited_tracks(Cfg(linked_rules=[{**RULE, "audited": "true"}])) == set()
    assert unaudited_tracks(Cfg(linked_rules=[{**RULE, "audited": "no"}, {**RULE, "name": "x2", "audited": 1}])) \
        == {"btcbreak_pump"}
    # 停用的规则以前留下的模拟单同样不进统计
    assert unaudited_tracks(Cfg(linked_rules=[{**RULE, "enabled": False}])) == {"btcbreak_pump"}
    assert unaudited_tracks(Cfg(linked_rules=None)) == set() and unaudited_tracks(object()) == set()
    assert unaudited_tracks(Config()) == {"btcbreak_pump"}                   # 仓库里的 config.yaml: 现在是未审核


def test_daily_report_keeps_unaudited_rule_out_of_totals():
    closed = [{"track": "second_buy", "pnl": 2.0}, {"track": "second_buy", "pnl": -0.5},
              {"track": "second_sell", "pnl": -1.0},
              {"track": "btcbreak_pump", "pnl": 30.0}, {"track": "btcbreak_pump", "pnl": -9.0}]
    hidden = frozenset({"btcbreak_pump"})
    lines = daily_close_lines(closed, hidden)
    assert lines == ["已平仓: <b>3</b> 笔 — 赚 1 / 亏 2（胜率 33%）",
                     "当日盈亏: <b>+0.5U</b>",
                     "扣手续费后约: <b>-0.9U</b>（费≈1.4U）",
                     "　二买(多): 2笔 赚1/亏1 +1.5U",
                     "　二卖(空): 1笔 赚0/亏1 -1.0U",
                     "　BTC破位→PUMP(模拟): 平仓 2 笔（未审核，不计胜率与盈亏）"]
    assert lines[:5] == daily_close_lines(closed[:3], hidden)                # 别的策略的数字完全不受它影响
    only = daily_close_lines(closed[3:], hidden)
    assert only == ["已平仓: 0 笔", "　BTC破位→PUMP(模拟): 平仓 2 笔（未审核，不计胜率与盈亏）"]
    assert not any(w in "".join(only) for w in ("赚", "亏 ", "胜率 ", "+", "-", "盈亏:"))
    assert daily_close_lines([], hidden) == ["已平仓: 0 笔"]
    # 审核通过(不再隐藏)后才进合计与明细
    assert any("BTC破位→PUMP(模拟): 2笔 赚1/亏1" in x for x in daily_close_lines(closed))


class _CfgAudited:
    """真实配置, 只把联动规则标成已审核。"""

    def __init__(self):
        object.__setattr__(self, "_c", Config())

    def get(self, key, default=None):
        if key == "linked_rules":
            return [{**RULE, "audited": True}]
        return self._c.get(key, default)

    def __getattr__(self, name):
        return getattr(self._c, name)


def _seed_trades(db):
    for track, res, pnl, r in (("second_buy", "tp", 5.0, 2.0), ("second_buy", "sl", -2.5, -1.0),
                               ("btcbreak_pump", "tp", 9.0, 2.0), ("btcbreak_pump", "sl", -4.0, -1.0),
                               ("btcbreak_pump", "open", None, None)):
        db.execute("INSERT INTO paper_trades (signal_id, symbol, tf, direction, track, entry, sl, tp, qty, "
                   "opened_at, result, pnl, pnl_r, closed_at) VALUES (0,'PUMPUSDT','5m','long',?,1,0.9,1.2,1,0,?,?,?,1)",
                   (track, res, pnl, r))
        db.execute("INSERT OR REPLACE INTO equity_curve (ts, track, equity) VALUES (?,?,?)",
                   (len(track) + (1 if res == "tp" else 2), track, 1000.0))


def test_stats_endpoints_exclude_unaudited_rule(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "LOGIN_FAIL_DELAY_S", 0)
    d = DB(tmp_path / "s.db")
    _seed_trades(d)
    app = server.create_app(Config(), d, engine=None, bot=None)
    c = TestClient(app, client=("10.0.0.1", 50000))                         # 匿名(这些接口是公开的)
    by = c.get("/api/stats_by_tf").json()
    assert [r["track"] for r in by["by_tf_track"]] == ["second_buy"]
    assert len(by["by_tf"]) == 1 and by["by_tf"][0]["n"] == 2 and by["by_tf"][0]["wins"] == 1
    assert by["by_tf"][0]["open_cnt"] == 0                                  # 合计里连它的持仓数也不混
    assert by["unaudited"] == [{"track": "btcbreak_pump", "n": 3, "closed": 2, "open": 1,
                                "status": "未审核, 不计胜率与盈亏"}]
    for key in ("wins", "pnl", "avg_r", "win_rate", "expectancy_r"):
        assert key not in by["unaudited"][0]
    ss = c.get("/api/strategy_stats").json()
    assert "btcbreak_pump" not in json.dumps(ss, ensure_ascii=False) and len(ss) == 1 and ss[0]["closed"] == 2
    assert c.get("/api/equity?track=btcbreak_pump").json() == []
    assert len(c.get("/api/equity?track=second_buy").json()) == 2
    # 逐笔明细仍然可见(不是统计)
    assert sum(t["track"] == "btcbreak_pump" for t in c.get("/api/trades").json()) == 3

    # 审核通过之后才出现在统计里
    d2 = DB(tmp_path / "s2.db")
    _seed_trades(d2)
    c2 = TestClient(server.create_app(_CfgAudited(), d2, engine=None, bot=None), client=("10.0.0.2", 50000))
    by2 = c2.get("/api/stats_by_tf").json()
    assert sorted(r["track"] for r in by2["by_tf_track"]) == ["btcbreak_pump", "second_buy"]
    assert "unaudited" not in by2 and len(c2.get("/api/equity?track=btcbreak_pump").json()) == 2


# =====================================================================================
# 下单闸 / 模拟行状态(评审: low ×2)
# =====================================================================================

def test_sim_signal_row_keeps_state_sim_after_stop_out(db, clock):
    pump = pump_series("d" * (T + 2) + "u" + "S" + "d" * 12)        # 入场后一路走低 → 打到止损
    btc = btc_series(T + 16, {T})

    async def go():
        env = Env(db, clock)
        await env.feed(btc, pump)
        t = rows(db, "SELECT * FROM paper_trades")
        assert len(t) == 1 and t[0]["result"] == "sl"
        s = rows(db, "SELECT * FROM signals")
        assert len(s) == 1 and (s[0]["kind"], s[0]["status"], s[0]["state"]) == ("alert", "sim", "sim")
        env.untouched()
    run(go())
    # 别的策略的行为不变: 普通信号止损后仍被标成 fail
    sid = db.insert_signal(ti._real_signal("long", 1.0, 0.9, 1.2, clock.t).to_db())
    db.execute("INSERT INTO paper_trades (signal_id, symbol, tf, direction, track, entry, sl, tp, qty, opened_at) "
               "VALUES (?,?,?,?,?,?,?,?,?,?)", (sid, "ETHUSDT", "5m", "long", "second_buy", 1.0, 0.9, 1.2, 1.0, 0))
    PaperBroker(Cfg(), db).on_closed_bar("ETHUSDT", "5m", (0, 1.0, 1.0, 0.8, 0.85, 1.0, 1.0, 0.0, 1))
    assert db.one("SELECT state FROM signals WHERE id=?", (sid,))["state"] == "fail"


def test_confirm_callback_refuses_sim_and_alert_rows(db, clock):
    """「确认买入」的回调只认带按钮发出去的主信号。联动规则的模拟行、提示型(kind=alert)行一律拒绝,
    连状态都不改; 下单器是绊线 —— 碰一下测试就失败。"""
    async def go():
        env = Env(db, clock)
        await env.feed(BTC_BARS, PUMP_BARS, 0, SIGNAL_I + 1)
        sim = rows(db, "SELECT * FROM signals")[0]
        hint = ti._real_signal("long", 1.0, 0.9, 1.2, clock.t)
        hint.kind = "alert"
        hint_id = db.insert_signal(hint.to_db())
        real_id = db.insert_signal(ti._real_signal("long", 1.0, 0.9, 1.2, 4_000_000_000).to_db())
        db.update_signal(real_id, status="notified")

        calls = []
        bot = tg.TgBot(ta_cfg(), db=db, trader=Tripwire("trader"))           # mode=live + 绊线下单器

        async def fake_api(method, **params):
            calls.append((method, params))
            return {"ok": True, "result": {}}

        bot.api = fake_api

        def cq(data):
            return {"id": "q", "data": data, "message": {"chat": {"id": 42}, "message_id": 7}}

        for sid in (sim["id"], hint_id):
            for action in ("c", "c2"):
                await bot._on_callback(cq(f"{action}:{sid}"))
        assert bot.trader.hits == []                                         # 一次都没碰到下单器
        assert db.one("SELECT status FROM signals WHERE id=?", (sim["id"],))["status"] == "sim"
        assert db.one("SELECT status FROM signals WHERE id=?", (hint_id,))["status"] == "new"
        assert sum(1 for m, p in calls if m == "answerCallbackQuery" and p.get("text") == "该信号不可下单") == 4
        assert tg.signal_confirmable(sim) is False
        assert tg.signal_confirmable({"kind": "primary", "status": "notified", "extra": "{}"}) is True
        assert tg.signal_confirmable({"kind": "primary", "status": "notified",
                                      "extra": json.dumps({"paper_only": True})}) is False
        assert tg.signal_confirmable({"kind": "secondary", "status": "new", "extra": None}) is False
        assert tg.signal_confirmable({}) is False

        # 正常的主信号: 第一步「确认」照旧进入二次确认(这里不走到下单那一步)
        calls.clear()
        await bot._on_callback(cq(f"c:{real_id}"))
        assert [m for m, _ in calls] == ["editMessageReplyMarkup", "answerCallbackQuery"]
        assert calls[1][1]["text"] == "请二次确认" and bot.trader.hits == []
    run(go())


# =====================================================================================
# 提醒引擎(评审: medium ×2, low ×6)
# =====================================================================================
T0, SYM = ta.T0, ta.SYM


def test_range_break_is_not_repeated_after_restart_inside_the_forming_bar():
    eng, db_, bars, clock = ta.range_setup()
    eng.on_closed_bar(SYM, "5m", ta.bar5(T0, 100, 100.5, 99.5, 100))
    assert len(ta.tick(eng, clock, 89.9)) == 1                       # 在未收盘K里破位, 已报
    clock.t += 120                                                   # 两分钟后重启(同一个库、同样的已收盘K)
    eng2, *_ = ta.make(db=db_, bars=bars, clock=clock, alerts__drop__enabled=False)
    assert ta.tick(eng2, clock, 89.95) == []                         # 首笔: 顺带算出参照价
    assert ta.tick(eng2, clock, 89.8) == []                          # 修之前: 这里把同一次破位又报一遍
    assert eng2.status()["counts"]["fired"]["range"] == 0
    # 冷却过后再破才是新的一次(冷却状态接上了, 不是被永久压住)
    assert len(ta.tick(eng2, clock, 89.7, 4 * 3600 + 1)) == 1


def test_drop_cooldown_survives_restart():
    eng, db_, bars, clock = ta.make(**ta.DROP5)
    ta.tick(eng, clock, 100.0)
    assert len(ta.tick(eng, clock, 99.0, 60)) == 1                   # 急跌已报(提醒价 99)
    clock.t += 30
    eng2, *_ = ta.make(db=db_, bars=bars, clock=clock, **ta.DROP5)   # 重启: 滑动窗口是空的
    assert ta.tick(eng2, clock, 99.65) == []
    assert ta.tick(eng2, clock, 98.6, 20) == []                      # 窗口内又 -1.05%: 仍在冷却内 → 不当成新的一次
    assert ta.tick(eng2, clock, 98.4, 20) == []                      # 更低了, 但离上一条才一分多钟 → 不刷屏
    assert ta.tick(eng2, clock, 99.5, 200) == []
    msgs = ta.tick(eng2, clock, 98.4, 100)                           # 过了一个窗口时长, 比上次提醒价低 0.61% → 再响
    assert len(msgs) == 1 and "急跌加深" in msgs[0] and "较上次提醒价 99 再跌 0.61%" in msgs[0]


def test_staircase_decline_re_alerts_every_step_below_last_alert():
    eng, _, bars, clock = ta.range_setup()
    eng.on_closed_bar(SYM, "5m", ta.bar5(T0, 100, 100.5, 99.5, 100))
    assert len(ta.tick(eng, clock, 89.9)) == 1                       # 破位(提醒价 89.9)
    for px in (89.5, 89.0, 88.5, 88.0, 87.5, 87.3):                  # 每 3 小时创一次新低: 冷却一直顺延
        assert ta.tick(eng, clock, px, 3 * 3600) == []
    msgs = ta.tick(eng, clock, 87.2, 3 * 3600)                       # 比 89.9 低 3.00% → 续跌
    assert len(msgs) == 1 and msgs[0].startswith("📉 <b>跌破2日低点后续跌</b> BTCUSDT")
    assert "现价 87.2 · 较上次提醒价 89.9 再跌 3.00%" in msgs[0]
    assert ta.tick(eng, clock, 87.0, 60) == []                       # 基准换成 87.2
    assert len(ta.tick(eng, clock, 84.5, 3 * 3600)) == 1             # 再低 3.1% → 又一条
    assert eng.status()["thresholds"]["range_break"]["step_pct"] == 3.0
    # step_pct=0 → 关闭, 行为同旧版
    eng0, _, _, clock0 = ta.range_setup(alerts__range_break__step_pct=0)
    eng0.on_closed_bar(SYM, "5m", ta.bar5(T0, 100, 100.5, 99.5, 100))
    assert len(ta.tick(eng0, clock0, 89.9)) == 1
    for px in (88.0, 86.0, 80.0):
        assert ta.tick(eng0, clock0, px, 3 * 3600) == []


def test_break_in_the_last_second_of_a_bar_is_caught_at_bar_close():
    """评审复现: 收盘前最后一秒的几笔被每秒一笔的节流丢掉, 收盘K的最低已跌破 2 日低点。
    修之前: 收盘回调直接把这根K的低点并进参照价 → 这次破位永远不报。"""
    eng, _, bars, clock = ta.range_setup()
    eng.on_closed_bar(SYM, "5m", ta.bar5(T0, 100, 100.5, 99.5, 100))
    clock.t = T0 + 299.1
    assert ta.tick(eng, clock, 90.4, 0) == []                        # 最后一笔被接受的报价: 还在 90 上方
    clock.t = T0 + 300
    msgs = eng.on_closed_bar(SYM, "5m", ta.bar5(T0 + 300, 90.5, 90.6, 89.5, 89.6))
    assert len(msgs) == 1 and msgs[0].startswith("📉 <b>跌破2日低点</b>")
    assert "5m最低 89.5 · 近2日最低 90" in msgs[0]
    assert eng.status()["ref_lows"][SYM]["2d_low"]["price"] == 89.5
    assert ta.tick(eng, clock, 89.4, 5) == []                        # 同一次破位, 不重复
    # 报价已经报过的那种情况: 收盘时不再报第二遍
    eng2, _, _, clock2 = ta.range_setup()
    eng2.on_closed_bar(SYM, "5m", ta.bar5(T0, 100, 100.5, 99.5, 100))
    clock2.t = T0 + 290
    assert len(ta.tick(eng2, clock2, 89.8, 0)) == 1
    clock2.t = T0 + 300
    assert eng2.on_closed_bar(SYM, "5m", ta.bar5(T0 + 300, 90.5, 90.6, 89.5, 89.6)) == []


def test_level_touched_during_downtime_is_found_by_rescan():
    eng, db_, bars, clock = ta.make(**ta.LV)
    bars.fill(SYM, "5m", 300, T0, 3, price=100.0)
    assert eng.add_level(SYM, 95.0, None, "前低")["ok"]               # T0 时建的
    assert eng.add_level(SYM, 110.0)["ok"]
    # 停机 50 分钟: 期间一根K下影打到 94 又收回 100
    bars.fill(SYM, "5m", 300, T0 + 3000, 10, price=100.0, lows={2: 94.0})
    clock.t = T0 + 3000 + 5
    eng2, *_ = ta.make(db=db_, bars=bars, clock=clock, **ta.LV)
    msgs = eng2.rescan_levels()
    assert len(msgs) == 1 and "价位提醒" in msgs[0] and "跌破 95" in msgs[0] and "5m最低 94" in msgs[0]
    assert "行情中断期间触及" in msgs[0] and "前低" in msgs[0]
    got = {r["id"]: r["status"] for r in eng2.list_levels(include_fired=True)}
    assert sorted(got.values()) == ["active", "fired"]
    assert eng2.rescan_levels() == []                                # 只报一次
    assert ta.tick(eng2, clock, 100.0) == []                         # 之后的报价不再为它响
    # 创建之前的K不算(那时它还不存在)
    eng3, db3, bars3, clock3 = ta.make(**ta.LV)
    bars3.fill(SYM, "5m", 300, T0, 10, price=100.0, lows={4: 94.0})
    assert eng3.add_level(SYM, 95.0)["ok"] and eng3.rescan_levels() == []


def test_add_level_rejects_stale_price_and_implausible_distance():
    eng, _, bars, clock = ta.make(**ta.LV)
    bars.fill("XYZUSDT", "5m", 300, T0 - 6 * 3600, 20, price=1.0)    # 6 小时前就没行情了(被移出监控, 缓存还在)
    assert eng.last_price("XYZUSDT") is None                         # 过期的价格不当现价
    r = eng.add_level("XYZUSDT", 1.10)
    assert r["ok"] is False and "行情已过期" in r["error"] and "360 分钟前" in r["error"]
    assert "XYZUSDT" not in eng.tick_symbols()
    r = eng.add_level("NOPEUSDT", 1.0)
    assert r["ok"] is False and "没有 NOPEUSDT 的行情" in r["error"]

    bars.fill(SYM, "5m", 300, T0, 3, price=64000.0)
    assert eng.last_price(SYM) == 64000.0
    r = eng.add_level(SYM, 63.0)                                     # 「提醒 btc 63 000」被拆成 63
    assert r["ok"] is False and "相差过大" in r["error"] and "-99.9%" in r["error"]
    assert eng.add_level(SYM, 1e12, "above")["ok"] is False
    assert eng.add_level(SYM, 1e-10)["ok"] is False
    assert eng.list_levels() == []                                   # 一个名额都没占
    assert eng.add_level(SYM, 63000.0)["ok"] is True                 # 正常的照旧
    assert eng.add_level(SYM, 20000.0)["ok"] is True                 # -69%: 仍在允许范围内
    # 一根K之内没有新报价也算新鲜(收盘价最多落后一根多一点)
    clock.t = T0 + 500
    assert eng.last_price(SYM) == 64000.0
    clock.t = T0 + 2 * 300 + 61
    assert eng.last_price(SYM) is None


def test_non_ascii_contract_symbol_is_accepted_like_web_and_telegram():
    eng, _, bars, clock = ta.make(**ta.LV)
    sym = server.norm_symbol("币安人生")
    assert sym == "币安人生USDT"
    bars.fill(sym, "5m", 300, T0, 3, price=0.1)
    r = eng.add_level(sym, 0.12)
    assert r["ok"] is True and r["alert"]["symbol"] == sym and r["alert"]["op"] == "above"
    msgs = ta.tick(eng, clock, 0.121, sym=sym)
    assert len(msgs) == 1 and "涨破 0.12" in msgs[0]                  # 触发文案与列表/指令同一个词
    assert eng.add_level("BTC-USDT", 1.0)["ok"] is False             # 带符号的仍然不收


def test_rebuild_does_not_report_events_older_than_the_late_cap(db, clock):
    """目标币断流 8 个多小时后才补回: 重建时发现的入场已是 8 小时前的事 → 不补报、不入库(不刷屏); 之后照常工作。"""
    n = T + 104
    btc = btc_series(n, {T})
    pump = pump_series("d" * (T + 2) + "u" + "S" + "u" * (n - T - 4))
    cfg = Cfg()
    r = LinkedRuleRunner(cfg, db, PaperBroker(cfg, db), lambda s, tf: [], now=clock)
    for rt in r._rules:
        rt.seeded = True
    out = _feed_runner(r, db, clock, btc, pump, 0, T)
    out += _feed_runner(r, db, clock, btc, pump, T, T + 100, skip={(PUMP, i) for i in range(T, T + 100)})
    assert len(out) == 2 and "联动规则待命" in out[0] and "联动规则到期" in out[1]     # 没有 PUMP 的K: 只能到期
    for i in range(T, T + 100):
        db.upsert_klines(PUMP, "5m", [pump[i]])
    assert r.resync() == []                                       # 8 小时前的入场: 不补报
    assert rows(db, "SELECT id FROM signals") == [] and r.status()["phase"] == "idle"
    assert r.status()["counts"]["gap_filled"] == 100
    assert _feed_runner(r, db, clock, btc, pump, T + 100, n) == []
    assert lr.LATE_REPORT_MAX_S == 6 * 3600.0
