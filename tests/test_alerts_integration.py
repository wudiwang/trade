"""提醒引擎 + 联动规则 接入实盘引擎的集成测试(全离线: 临时 sqlite、假配置、假行情)。

重点验证的是「边界」而不是形态本身(形态见 test_strat_btcbreak.py):
  - 一整轮(破位 → 待命 → 底分型+停顿)只发两条通知、只入库一行、只开一笔模拟单;
  - 下单相关的一切(trader / _auto_execute / signal_subscribers / REST)在这条路径上碰都不碰;
  - 重启在一轮中间不重复; 历史不足时保持空闲; 提醒或规则内部抛异常不影响正常的信号评估。
不计算、不断言任何胜率/期望(CLAUDE.md 第七条)。

python -m pytest tests/test_alerts_integration.py -q
"""
import ast
import asyncio
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.db import DB  # noqa: E402
from app.engine import core as core_mod  # noqa: E402
from app.engine.alerts import AlertEngine  # noqa: E402
from app.engine.core import Engine  # noqa: E402
from app.engine.linked_rules import (  # noqa: E402
    PAPER_KIND, LinkedRuleRunner, params_from_entry, rule_entries, rule_symbols)
from app.engine.paper import PaperBroker  # noqa: E402
from app.engine.signals import Signal  # noqa: E402

TF = 300_000
BASE = 1_794_000_000_000                      # 5m 网格上的一个时刻(ms)
BTC, PUMP, ETH = "BTCUSDT", "PUMPUSDT", "ETHUSDT"
T = 30                                        # 破位K下标(2h 窗口此时已铺满)
RULE = {"name": "btcbreak_pump", "enabled": True, "paper": True,
        "trigger_symbol": BTC, "target_symbol": PUMP, "tf": "5m",
        "lookback_hours": 2.0, "vol_ma": 5, "vol_mult": 2.0, "ttl_hours": 1.0,
        "require_stall": True, "stall_max_gap": 3, "sl_buffer_pct": 0.3, "rr": 2.0}


# ------------------------------------------------------------------ 假件
class Cfg:
    """最坏情况的配置: live + auto_trade 开着。联动规则在这种配置下也不许碰到下单。"""
    mode = "live"
    timeframes = ["5m", "15m", "1h"]
    binance_key = ""
    binance_secret = ""

    def __init__(self, **over):
        self.values = {
            "binance.rest_base": "http://127.0.0.1:9", "binance.ws_base": "ws://127.0.0.1:9",
            "live.auto_trade": True, "risk.account_equity": 1000, "risk.risk_pct": 0.5,
            "alerts.enabled": False, "linked_rules": [dict(RULE)],
        }
        self.values.update(over)

    def get(self, key, default=None):
        return self.values.get(key, default)


class Tripwire:
    """任何属性访问/调用都记一笔; 测试最后断言一笔都没有。"""

    def __init__(self, name):
        object.__setattr__(self, "_name", name)
        object.__setattr__(self, "hits", [])

    def __getattr__(self, item):
        self.hits.append(item)
        raise AssertionError(f"{self._name}.{item} 被访问了: 这条路径不允许碰下单/账户")

    def __call__(self, *a, **k):
        self.hits.append("__call__")
        raise AssertionError(f"{self._name} 被调用了")


class Clock:
    def __init__(self):
        self.t = BASE / 1000.0

    def __call__(self):
        return self.t


class Env:
    """一台没联网的引擎: 真 DB、真 PaperBroker、真 AlertEngine/LinkedRuleRunner, 下单相关全是绊线。"""

    def __init__(self, db, clock, **cfg_over):
        self.db, self.clock = db, clock
        self.cfg = Cfg(**cfg_over)
        e = Engine(self.cfg, db)
        self.engine = e
        self.trader = e.trader = Tripwire("trader")
        self.rest = e.rest = Tripwire("rest")
        self.auto_calls, self.sub_calls, self.evals = [], [], []
        self.notices, self.closes = [], []

        async def auto(sid, sig):
            self.auto_calls.append((sid, sig))
            return True

        async def on_signal(sid, sig):
            self.sub_calls.append((sid, sig))

        async def notify(text):
            self.notices.append(text)

        async def on_close(trade):
            self.closes.append(trade)

        e._auto_execute = auto
        e._auto_execute_locked = auto
        e.signal_subscribers.append(on_signal)
        e.notice_subscribers.append(notify)
        e.trade_close_subscribers.append(on_close)
        self.next_signals = []                    # evaluate_all 下一次要返回的信号

        def evaluate_all(symbol, tf, kbt):
            self.evals.append((symbol, tf))
            out, self.next_signals = self.next_signals, []
            return out

        e.signal_engine.evaluate_all = evaluate_all
        if e.linked is not None:
            e.linked._now = clock
        if e.alerts is not None:
            e.alerts._now = clock

    async def bar(self, symbol, b, tf="5m"):
        self.clock.t = (b[0] + TF) / 1000.0
        await self.engine.on_closed_bar(symbol, tf, b)

    async def feed(self, btc, pump, start=0, stop=None):
        stop = max(len(btc), len(pump)) if stop is None else stop
        for i in range(start, stop):
            if i < len(btc):
                await self.bar(BTC, btc[i])
            if i < len(pump):
                await self.bar(PUMP, pump[i])
        await self.drain()

    async def drain(self):
        for _ in range(5):
            tasks = list(self.engine._bg_notices)
            if not tasks:
                break
            await asyncio.gather(*tasks)

    def untouched(self):
        assert self.trader.hits == [] and self.rest.hits == []
        assert self.auto_calls == [] and self.sub_calls == []


def tup(i, o, h, l, c, v=100.0):
    return (BASE + i * TF, float(o), float(h), float(l), float(c), float(v), float(v) * float(c), 0.0, 1)


def btc_series(n, breaks=(), level=100.0, break_vol=300.0):
    """平盘, 在 breaks 下标处放量收破之前所有低点, 之后在新台阶平盘。"""
    out, cur = [], level
    for i in range(n):
        if i in breaks:
            new = cur - 2.0
            out.append(tup(i, cur, cur + 0.2, new - 0.5, new, break_vol))
            cur = new
        else:
            out.append(tup(i, cur, cur + 0.5, cur - 0.5, cur))
    return out


def pump_series(spec, h0=2000):
    """每个字符一根K: d=阶梯下行 u=阶梯上行(弱收) S=强阳(收盘 > 前高)。"""
    out, h, l = [], h0, h0 - 20
    for i, ch in enumerate(spec):
        if ch == "d":
            h, l = h - 10, l - 10
            hi, lo, c = h, l, l + 2
        elif ch == "u":
            h, l = h + 10, l + 10
            hi, lo, c = h, l, l + 2
        elif ch == "S":
            h, l = h + 30, l + 10
            hi, lo, c = h, l, h - 2
        else:
            raise ValueError(ch)
        out.append(tup(i, c / 100, hi / 100, lo / 100, c / 100))
    return out


# 破位在 T; PUMP 低点在 T+1, 右K T+2, 强阳(停顿K=信号K)在 T+3, 之后一路阶梯上行到止盈
BTC_BARS = btc_series(T + 30, {T})
PUMP_BARS = pump_series("d" * (T + 2) + "u" + "S" + "u" * 26)
SIGNAL_I = T + 3


@pytest.fixture
def db(tmp_path):
    return DB(tmp_path / "t.db")


@pytest.fixture
def clock():
    return Clock()


def rows(db, sql, args=()):
    return [dict(r) for r in db.query(sql, args)]


def run(coro):
    return asyncio.run(coro)


# ============================ 一整轮 ============================

def test_full_episode_two_notices_one_signal_one_paper_trade(db, clock):
    async def go():
        env = Env(db, clock)
        e = env.engine
        assert e.linked is not None and e.alerts is None
        await env.feed(BTC_BARS, PUMP_BARS, 0, SIGNAL_I + 1)

        # ---- 恰好两条通知: 待命 + 模拟入场 ----
        assert len(env.notices) == 2, env.notices
        armed, entry = env.notices
        assert "联动规则待命" in armed and "BTCUSDT" in armed and "跌破2h低点" in armed and "放量" in armed
        assert "等 PUMPUSDT 第一个底分型" in armed
        assert "模拟入场" in entry and "无实盘下单" in entry and "PUMPUSDT" in entry
        for word in ("入场 ", "止损 ", "止盈 ", "风险 ", "模拟单 #"):
            assert word in entry
        for m in env.notices:                              # Telegram HTML: 只有 <b> 标签, 没有裸尖括号
            assert m.replace("<b>", "").replace("</b>", "").count("<") == 0
            assert "\n" in m and m.splitlines()[-1][2] == "-"      # 末行是曼谷时间 "MM-DD HH:MM"

        # ---- signals 表恰好一行: kind=alert, 只模拟 ----
        sigs = rows(db, "SELECT * FROM signals")
        assert len(sigs) == 1
        s = sigs[0]
        assert (s["symbol"], s["tf"], s["direction"], s["kind"]) == (PUMP, "5m", "long", "alert")
        assert s["status"] == "sim" and s["state"] == "sim"
        ex = json.loads(s["extra"])
        assert ex["type"] == "btcbreak_pump" and ex["paper_only"] is True and ex["late"] is False
        ev = ex["event"]
        assert ev["type"] == "entry" and ev["trigger"]["time"] == BASE + T * TF
        ent = ev["entry"]
        assert ent["signal_time"] == BASE + SIGNAL_I * TF and ent["fx_low_time"] == BASE + (T + 1) * TF
        assert s["entry"] == ent["entry"] and s["sl"] == ent["sl"] and s["tp"] == ent["tp"]
        assert s["sl"] < s["entry"] < s["tp"]              # 入场即带止损与止盈(第一准则)
        assert s["sl"] == pytest.approx(ent["fx_low"] * 0.997)
        assert s["tp"] == pytest.approx(s["entry"] + 2.0 * (s["entry"] - s["sl"]))

        # ---- 模拟单恰好一笔, track = 规则名 ----
        trades = rows(db, "SELECT * FROM paper_trades")
        assert len(trades) == 1
        t = trades[0]
        assert t["track"] == "btcbreak_pump" and t["signal_id"] == s["id"] and t["result"] == "open"
        assert (t["symbol"], t["tf"], t["direction"]) == (PUMP, "5m", "long")
        assert (t["entry"], t["sl"], t["tp"]) == (s["entry"], s["sl"], s["tp"]) and t["qty"] > 0
        assert f"模拟单 #{t['id']}" in entry and f"信号 #{s['id']}" in entry

        st = e.linked.status()
        assert st["phase"] == "idle" and st["history_ok"] is True
        assert st["counts"]["armed"] == 1 and st["counts"]["entry"] == 1 and st["counts"]["paper_opened"] == 1
        assert st["last_entry"]["paper_trade_id"] == t["id"] and st["last_trigger"]["time"] == BASE + T * TF
        assert isinstance(st["summary"], str) and "btcbreak_pump" in st["summary"]

        # ---- 之后跟踪到止盈: 走 paper 结算 + trade_close_subscribers, 仍然不碰下单 ----
        await env.feed(BTC_BARS, PUMP_BARS, SIGNAL_I + 1)
        t2 = rows(db, "SELECT * FROM paper_trades")
        assert len(t2) == 1 and t2[0]["result"] == "tp" and t2[0]["exit_price"] == s["tp"]
        assert [c["track"] for c in env.closes] == ["btcbreak_pump"]
        assert len(env.notices) == 2 and len(rows(db, "SELECT id FROM signals")) == 1
        # 正常的信号评估一根没少(BTC/PUMP 都在正常监控里时照常评估)
        assert len(env.evals) == len(BTC_BARS) + len(PUMP_BARS)
        env.untouched()
    run(go())


def test_expired_episode_notifies_once_and_records_nothing(db, clock):
    async def go():
        env = Env(db, clock)
        await env.feed(btc_series(T + 20, {T}), pump_series("d" * (T + 20)))
        assert len(env.notices) == 2
        assert "联动规则待命" in env.notices[0] and "联动规则到期" in env.notices[1]
        assert rows(db, "SELECT id FROM signals") == [] and rows(db, "SELECT id FROM paper_trades") == []
        st = env.engine.linked.status()
        assert st["phase"] == "idle" and st["counts"]["expired"] == 1 and st["counts"]["entry"] == 0
        env.untouched()
    run(go())


def test_paper_false_only_notifies_and_stores(db, clock):
    async def go():
        env = Env(db, clock, linked_rules=[{**RULE, "paper": False}])
        await env.feed(BTC_BARS, PUMP_BARS, 0, SIGNAL_I + 1)
        assert len(env.notices) == 2 and "未开模拟单" in env.notices[1]
        assert len(rows(db, "SELECT id FROM signals")) == 1
        assert rows(db, "SELECT id FROM paper_trades") == []
        env.untouched()
    run(go())


# ============================ 重启 / 幂等 ============================

def test_restart_mid_episode_does_not_duplicate(db, clock):
    async def go():
        env1 = Env(db, clock)
        await env1.feed(BTC_BARS, PUMP_BARS, 0, T + 2)       # 已待命, 还没入场
        assert len(env1.notices) == 1 and "联动规则待命" in env1.notices[0]
        assert env1.engine.linked.status()["phase"] == "armed"

        # ---- 重启: 新引擎从 klines 表预热, 状态是「待命」, 不重发待命通知 ----
        env2 = Env(db, clock)
        assert env2.engine.linked.seed() == []
        st = env2.engine.linked.status()
        assert st["phase"] == "armed" and st["armed_at"] == BASE + (T + 1) * TF
        assert st["expires_at"] == BASE + (T + 1) * TF + 3_600_000
        await env2.feed(BTC_BARS, PUMP_BARS, T + 2, SIGNAL_I + 1)
        assert len(env2.notices) == 1 and "模拟入场" in env2.notices[0]
        assert len(rows(db, "SELECT id FROM signals")) == 1
        assert len(rows(db, "SELECT id FROM paper_trades")) == 1

        # ---- 入场后再重启, 并把最后几根K重复推一遍: 什么都不多 ----
        env3 = Env(db, clock)
        assert env3.engine.linked.seed() == []
        assert env3.engine.linked.status()["phase"] == "idle"
        await env3.feed(BTC_BARS, PUMP_BARS, T, SIGNAL_I + 1)
        assert env3.notices == []
        assert len(rows(db, "SELECT id FROM signals")) == 1
        assert len(rows(db, "SELECT id FROM paper_trades")) == 1

        # ---- 最坏情况: 一个完全没预热的运行器把整段K从头再喂一遍(等于事件全部重放)。
        #      待命/入场都已在库里占过位 → 不再通知、不再入库、不再开模拟单 ----
        cache = {(BTC, "5m"): [], (PUMP, "5m"): []}
        r4 = LinkedRuleRunner(env3.cfg, db, PaperBroker(env3.cfg, db), lambda s, tf: cache[(s, tf)], now=clock)
        for rt in r4._rules:
            rt.seeded = True
        out = []
        for i in range(SIGNAL_I + 1):
            clock.t = (BASE + (i + 1) * TF) / 1000.0
            out += r4.on_closed_bar(BTC, "5m", BTC_BARS[i])
            out += r4.on_closed_bar(PUMP, "5m", PUMP_BARS[i])
        assert out == []
        c = r4.status()["counts"]
        assert c["armed"] == 1 and c["entry"] == 1 and c["dup_skipped"] == 1 and c["paper_opened"] == 0
        assert len(rows(db, "SELECT id FROM signals")) == 1
        assert len(rows(db, "SELECT id FROM paper_trades")) == 1
        for env in (env1, env2, env3):
            env.untouched()
    run(go())


def test_restart_while_armed_but_never_announced_sends_one_catch_up(db, clock):
    """破位发生在停机期间(K线由启动回填写进了库): 启动预热后补报一次, 再重启不再报。"""
    for i in range(T + 2):
        db.upsert_klines(BTC, "5m", [BTC_BARS[i]])
        db.upsert_klines(PUMP, "5m", [PUMP_BARS[i]])
    clock.t = (BASE + (T + 2) * TF) / 1000.0
    cfg = Cfg()
    r1 = LinkedRuleRunner(cfg, db, PaperBroker(cfg, db), lambda s, tf: [], now=clock)
    msgs = r1.seed()
    assert len(msgs) == 1 and "联动规则待命" in msgs[0] and "启动时补报" in msgs[0]
    assert r1.status()["phase"] == "armed"
    r2 = LinkedRuleRunner(cfg, db, PaperBroker(cfg, db), lambda s, tf: [], now=clock)
    assert r2.seed() == [] and r2.status()["phase"] == "armed"
    assert rows(db, "SELECT id FROM signals") == []


# ============================ 历史不足 ============================

def test_short_history_keeps_runner_idle_until_window_fills(db, clock):
    cfg = Cfg()
    # 库里只有 10 根 BTC(窗口要 24 根), 其中最后一根就是「放量收破前低」
    btc = btc_series(40, {9, 34})
    pump = pump_series("d" * 40)
    for i in range(10):
        db.upsert_klines(BTC, "5m", [btc[i]])
        db.upsert_klines(PUMP, "5m", [pump[i]])
    clock.t = (BASE + 10 * TF) / 1000.0
    r = LinkedRuleRunner(cfg, db, PaperBroker(cfg, db), lambda s, tf: [], now=clock)
    assert r.seed() == []
    st = r.status()
    assert st["phase"] == "idle" and st["history_ok"] is False
    assert "历史不足" in st["history_note"] and "历史不足" in st["summary"]
    assert st["window_bars"] < st["window_need"] and st["seed"]["trigger_bars"] == 10

    out = []
    for i in range(10, 34):                                # 实时K一根根进来: 窗口没铺满前不触发
        clock.t = (BASE + (i + 1) * TF) / 1000.0
        out += r.on_closed_bar(BTC, "5m", btc[i])
        out += r.on_closed_bar(PUMP, "5m", pump[i])
    assert out == [] and r.status()["phase"] == "idle"
    assert r.status()["history_ok"] is True                # 铺满后自动生效, 不需要重启
    clock.t = (BASE + 35 * TF) / 1000.0
    out = r.on_closed_bar(BTC, "5m", btc[34])
    assert len(out) == 1 and "联动规则待命" in out[0]
    assert r.status()["phase"] == "armed"


def test_truncated_window_never_triggers(db, clock):
    """只给到破位K为止不足两小时的历史: 哪怕那根K放量创了「可见范围内」的新低, 也不触发。"""
    cfg = Cfg()
    btc = btc_series(20, {15})
    r = LinkedRuleRunner(cfg, db, PaperBroker(cfg, db), lambda s, tf: [], now=clock)
    for rt in r._rules:
        rt.seeded = True
    out = []
    for i in range(20):
        clock.t = (BASE + (i + 1) * TF) / 1000.0
        out += r.on_closed_bar(BTC, "5m", btc[i])
    assert out == []
    st = r.status()
    assert st["phase"] == "idle" and st["history_ok"] is False and st["counts"]["armed"] == 0


# ============================ 断线缺K ============================

def test_gap_fill_from_cache_and_late_entry_opens_no_paper_trade(db, clock):
    cfg = Cfg()
    cache = {(BTC, "5m"): [], (PUMP, "5m"): []}
    r = LinkedRuleRunner(cfg, db, PaperBroker(cfg, db), lambda s, tf: cache[(s, tf)], now=clock)
    for rt in r._rules:
        rt.seeded = True
    cols = ("open_time", "open", "high", "low", "close", "volume", "quote_volume", "taker_buy", "closed")
    out = []
    for i in range(SIGNAL_I + 4):
        clock.t = (BASE + (i + 1) * TF) / 1000.0
        cache[(BTC, "5m")].append(dict(zip(cols, BTC_BARS[i])))
        cache[(PUMP, "5m")].append(dict(zip(cols, PUMP_BARS[i])))
        out += r.on_closed_bar(BTC, "5m", BTC_BARS[i])
        if i in (SIGNAL_I, SIGNAL_I + 1, SIGNAL_I + 2):    # PUMP 的信号K及其后两根没有实时推到(断线)
            continue
        out += r.on_closed_bar(PUMP, "5m", PUMP_BARS[i])
    assert len(out) == 2 and "联动规则待命" in out[0]
    assert "模拟入场" in out[1] and "信号K已过, 未开模拟单" in out[1]
    c = r.status()["counts"]
    assert c["gap_filled"] == 3 and c["gap_missing"] == 0 and c["late_entries"] == 1 and c["paper_opened"] == 0
    sig = rows(db, "SELECT extra FROM signals")
    assert len(sig) == 1 and json.loads(sig[0]["extra"])["late"] is True
    assert rows(db, "SELECT id FROM paper_trades") == []


# ============================ 异常隔离 ============================

def test_exceptions_in_alerts_or_rule_do_not_reach_engine(db, clock, monkeypatch):
    async def go():
        env = Env(db, clock, **{"alerts.enabled": True})
        e = env.engine
        assert e.alerts is not None and e.linked is not None

        def boom(*a, **k):
            raise RuntimeError("boom")

        # ---- 第一层: 整个对象的方法直接抛 ----
        e.alerts.on_closed_bar = boom
        e.alerts.on_tick = boom
        e.linked.on_closed_bar = boom
        assert e._on_tick(BTC, 100.0, int(clock.t * 1000)) is None
        await env.bar(BTC, BTC_BARS[0])
        await env.bar(PUMP, PUMP_BARS[0])
        assert env.evals == [(BTC, "5m"), (PUMP, "5m")]          # 正常的信号评估照常跑
        assert e.hook_errors == {"tick": 1, "alerts": 2, "linked": 2}
        assert len(e.cache[(BTC, "5m")]) == 1 and len(rows(db, "SELECT open_time FROM klines")) == 2

        # ---- 第二层: 对象自己的内部抛(它们自己就该吞掉, 返回空列表) ----
        del e.alerts.on_closed_bar, e.alerts.on_tick, e.linked.on_closed_bar
        e.alerts._process = boom
        e.alerts._refresh_refs = boom
        e.linked.seed()                                          # 先预热(否则第一根K只是触发预热)
        for rt in e.linked._rules:
            rt.rule.on_bar = boom
        assert e.alerts.on_tick(BTC, 100.0, int(clock.t * 1000)) == []
        await env.bar(BTC, BTC_BARS[1])
        await env.bar(PUMP, PUMP_BARS[1])
        assert env.evals[2:] == [(BTC, "5m"), (PUMP, "5m")]
        assert e.linked.status()["errors"] >= 2 and e.alerts.status()["counts"]["errors"] >= 1

        # ---- 第三层: 发通知的订阅者抛(Telegram 挂了) ----
        async def bad_notify(text):
            raise RuntimeError("telegram down")

        e.notice_subscribers.insert(0, bad_notify)
        e._notify_bg(["hello"])
        await env.drain()
        # 好的订阅者只收到一次; 坏的那个按重试表重试(这里间隔调成 0), 试完计一条发送失败
        assert env.notices == ["hello"] and e.hook_errors["notice_send"] == 3
        assert e.notice_failed == 1 and e.notice_retried == 2
        env.untouched()
    monkeypatch.setattr(core_mod, "NOTICE_RETRY_DELAYS_S", (0.0, 0.0))
    run(go())


def test_signal_evaluation_exception_does_not_block_linked_rule(db, clock):
    """反过来也成立: 正常评估炸了, 联动规则照样走完一轮。"""
    async def go():
        env = Env(db, clock)

        def bad_eval(symbol, tf, kbt):
            raise RuntimeError("eval boom")

        env.engine.signal_engine.evaluate_all = bad_eval
        await env.feed(BTC_BARS, PUMP_BARS, 0, SIGNAL_I + 1)
        assert len(env.notices) == 2 and len(rows(db, "SELECT id FROM paper_trades")) == 1
        env.untouched()
    run(go())


# ============================ 与正常交易信号互不串线 ============================

def _real_signal(direction, entry, sl, tp, t):
    return Signal(symbol=PUMP, tf="5m", direction=direction, kind="primary", entry=entry, sl=sl, tp=tp,
                  rr=2.0, vol_ratio=1.0, strength="normal", suggested_qty=10.0, risk_usdt=5.0,
                  reason="test", created_at=int(t), extra={"type": "second_sell"})


def test_opposite_real_signal_does_not_close_linked_sim_trade(db, clock):
    async def go():
        env = Env(db, clock)
        await env.feed(BTC_BARS, PUMP_BARS, 0, SIGNAL_I + 1)
        sim = rows(db, "SELECT * FROM paper_trades")
        assert len(sim) == 1 and sim[0]["result"] == "open"
        # 同币同级别先有一笔别的策略的多单, 再来一个真实的反向(做空)信号
        db.execute("INSERT INTO paper_trades (signal_id, symbol, tf, direction, track, entry, sl, tp, qty, "
                   "opened_at) VALUES (0,?,?,?,?,?,?,?,?,?)", (PUMP, "5m", "long", "second_buy", 17.0, 16.0, 19.0, 1.0, 0))
        px = PUMP_BARS[SIGNAL_I + 1][4]
        env.next_signals = [_real_signal("short", px, px * 1.02, px * 0.96, clock.t)]
        await env.bar(PUMP, PUMP_BARS[SIGNAL_I + 1])
        await env.drain()
        by = {r["track"]: r for r in rows(db, "SELECT * FROM paper_trades")}
        assert by["second_buy"]["result"] == "rev"          # 原有行为不变: 别的策略的多单被反向信号平掉
        assert by["btcbreak_pump"]["result"] == "open"      # 联动规则的模拟单只认止盈/止损
        assert by["second_sell"]["result"] == "open"
        # 真实信号照常走到自动下单那一步(这里是记录用的替身); 联动规则那一行从没到过那里
        linked_sid = sim[0]["signal_id"]
        assert len(env.auto_calls) == 1 and env.auto_calls[0][0] != linked_sid
        assert env.auto_calls[0][1].kind == "primary"
        assert env.trader.hits == [] and env.rest.hits == [] and env.sub_calls == []
    run(go())


def test_linked_row_is_never_paired_as_dual_signal(db, clock):
    async def go():
        env = Env(db, clock)
        await env.feed(BTC_BARS, PUMP_BARS, 0, SIGNAL_I + 1)
        n = len(env.notices)
        px = PUMP_BARS[SIGNAL_I + 1][4]
        wy = _real_signal("long", px, px * 0.98, px * 1.04, clock.t)
        wy.extra = {"type": "buy1", "path": "威科夫"}
        env.next_signals = [wy]
        await env.bar(PUMP, PUMP_BARS[SIGNAL_I + 1])
        await env.drain()
        assert len(env.notices) == n                        # 没有「双信号重叠」提醒
        assert all("dual" not in json.loads(r["extra"]) for r in rows(db, "SELECT extra FROM signals"))
    run(go())


# ============================ 币种强制纳入 / 仅行情 ============================

class FakeRest:
    def __init__(self, vols):
        self.vols = vols

    async def usdt_perp_symbols(self):
        return [{"symbol": s, "status": "TRADING", "price_precision": 2, "qty_precision": 3,
                 "tick_size": 0.01, "step_size": 0.001} for s in self.vols]

    async def ticker_24h(self):
        return dict(self.vols)


def test_rule_and_alert_symbols_are_always_monitored_but_data_only(db, clock):
    async def go():
        env = Env(db, clock, **{"alerts.enabled": True, "alerts.symbols": ["BTCUSDT", "SOLUSDT", "NOPEUSDT"],
                                "universe.min_quote_volume_24h": 1e9, "universe.exclude": ["PUMPUSDT"]})
        e = env.engine
        e.rest = FakeRest({BTC: 5e9, ETH: 2e9, PUMP: 3e9, "SOLUSDT": 1e6, "DOGEUSDT": 1e6})
        await e.refresh_universe()
        assert e.symbols == [BTC, ETH, PUMP, "SOLUSDT"]      # DOGE 被成交额过滤; NOPE 交易所没有
        assert e.data_only == {PUMP, "SOLUSDT"}              # PUMP 在 exclude 里、SOL 成交额不够 → 仅行情
        assert e.ws.on_tick is not None and e.ws.tick_tf == "5m"
        assert e.ws.tick_symbols() == {BTC, "SOLUSDT", "NOPEUSDT"}
        # 仅行情的币: K线照常进缓存/入库/喂规则, 但不评估信号
        await env.bar(PUMP, PUMP_BARS[0])
        await env.bar(ETH, tup(0, 10, 11, 9, 10))
        await env.bar(BTC, BTC_BARS[0])
        assert env.evals == [(ETH, "5m"), (BTC, "5m")]
        assert len(e.cache[(PUMP, "5m")]) == 1
        assert e.linked.status()["counts"]["bars"] >= 1
        assert env.trader.hits == [] and env.auto_calls == [] and env.sub_calls == []
    run(go())


def test_engine_without_sections_is_unchanged(db, clock):
    env = Env(db, clock, linked_rules=[])
    e = env.engine
    assert e.alerts is None and e.linked is None
    assert e.ws.on_tick is None and e.ws.tick_symbols is None
    assert e._forced_symbols() == set() and e._protected_tracks() == ()
    assert set(e.status()) == {"started_at", "uptime_s", "symbols", "ws_conns", "ws_last_msg_age_s",
                               "last_eval_ms", "mode", "tracks", "macro", "squeeze", "funnel"}
    assert set(e.status()["tracks"]) == {"buy1", "buy2", "second_buy", "second_sell"}

    async def go():
        await env.bar(BTC, BTC_BARS[0])
        assert env.evals == [(BTC, "5m")] and e.hook_errors == {}
    run(go())


def test_bad_rule_config_is_skipped_not_fatal(db, clock):
    env = Env(db, clock, linked_rules=[{**RULE, "name": "bad", "vol_ma": "abc"},
                                       {**RULE, "name": "same", "target_symbol": BTC},
                                       {**RULE, "name": "off", "enabled": False}, dict(RULE)])
    r = env.engine.linked
    assert [rt.name for rt in r._rules] == ["btcbreak_pump"]
    assert len(r.status()["config_errors"]) == 2
    assert rule_symbols(env.cfg) == {BTC, PUMP}
    assert [x["name"] for x in rule_entries(env.cfg)] == ["bad", "same", "btcbreak_pump"]
    p = params_from_entry({**RULE, "lookback_hours": 48, "vol_ma": 20.0, "require_stall": "false"})
    assert p.lookback_hours == 48.0 and p.vol_ma == 20 and p.require_stall is False


# ============================ 实时报价通路 ============================

def _ws_msg(symbol, tf, open_ms, price, event_ms, closed=False):
    return json.dumps({"stream": f"{symbol.lower()}@kline_{tf}", "data": {
        "e": "kline", "E": event_ms, "s": symbol,
        "k": {"t": open_ms, "s": symbol, "i": tf, "o": str(price), "c": str(price), "h": str(price), "l": str(price),
              "v": "1", "q": "1", "V": "0", "x": closed}}})


def test_tick_path_is_sync_and_sends_in_background(db, clock):
    async def go():
        env = Env(db, clock, **{"alerts.enabled": True})
        e = env.engine
        await env.bar(BTC, tup(0, 100, 100.5, 99.5, 100))
        assert e.alerts.add_level(BTC, 99.7, "below", "前低")["ok"] is True
        clock.t += 10
        ms = int(clock.t * 1000)
        # 非基准级别的流、别的币: 不产生报价
        await e.ws._handle(_ws_msg(BTC, "15m", BASE, 98.0, ms))
        await e.ws._handle(_ws_msg(ETH, "5m", BASE + TF, 98.0, ms))
        assert e.ws.tick_count == 0 and env.notices == []
        # 5m 流里未收盘K的推送 → 一笔报价 → 价位提醒触发; 处理函数返回时消息还没发(只是排了后台任务)
        await e.ws._handle(_ws_msg(BTC, "5m", BASE + TF, 99.6, ms))
        assert e.ws.tick_count == 1 and e.ws.tick_errors == 0
        assert env.notices == [] and len(e._bg_notices) == 1
        await env.drain()
        assert len(env.notices) == 1 and "价位提醒" in env.notices[0] and "前低" in env.notices[0]
        assert e._bg_notices == set()
        # 同一秒内的后续推送被节流; 价位只响一次
        await e.ws._handle(_ws_msg(BTC, "5m", BASE + TF, 99.5, ms + 200))
        clock.t += 2
        await e.ws._handle(_ws_msg(BTC, "5m", BASE + TF, 99.4, ms + 2000))
        await env.drain()
        assert e.ws.tick_count == 2 and len(env.notices) == 1
        assert env.evals == [(BTC, "5m")]                    # 报价不触发信号评估
        env.untouched()
    run(go())


def test_notice_backlog_is_bounded(db, clock):
    async def go():
        env = Env(db, clock)
        e = env.engine
        gate = asyncio.Event()

        async def stuck(text):
            await gate.wait()

        e.notice_subscribers[:] = [stuck]
        for i in range(core_mod.MAX_PENDING_NOTICES + 10):
            e._notify_bg([f"m{i}"])
        assert len(e._bg_notices) == core_mod.MAX_PENDING_NOTICES and e.notice_dropped == 10
        gate.set()
        await env.drain()
        assert e._bg_notices == set()
    run(go())


# ============================ 日报 ============================

def test_daily_report_lists_every_track():
    from app.main import daily_track_lines
    rows_ = [{"track": "btcbreak_pump", "pnl": 3.0}, {"track": "second_sell", "pnl": -1.0},
             {"track": "second_buy", "pnl": 2.0}, {"track": "second_buy", "pnl": -0.5},
             {"track": "buy<1>", "pnl": 0.0}, {"track": None, "pnl": None}]
    lines = daily_track_lines(rows_)
    assert lines == ["　二买(多): 2笔 赚1/亏1 +1.5U",
                     "　二卖(空): 1笔 赚0/亏1 -1.0U",
                     "　BTC破位→PUMP(模拟): 1笔 赚1/亏0 +3.0U",
                     "　buy&lt;1&gt;: 1笔 赚0/亏1 +0.0U",
                     "　other: 1笔 赚0/亏1 +0.0U"]
    assert daily_track_lines([]) == []
    # 原来只有二买/二卖时, 输出与旧版逐字相同
    assert daily_track_lines(rows_[1:4]) == ["　二买(多): 2笔 赚1/亏1 +1.5U", "　二卖(空): 1笔 赚0/亏1 -1.0U"]


# ============================ 静态边界: 这两个模块里不存在通往下单的名字 ============================

FORBIDDEN = {"trader", "LiveTrader", "execute_signal", "_auto_execute", "_auto_execute_locked",
             "signal_subscribers", "place_order", "place_algo_order", "cancel_order", "_signed",
             "binance_rest", "BinanceRest", "set_leverage", "account_info", "position_risk"}


@pytest.mark.parametrize("name", ["linked_rules.py", "alerts.py", "strat_btcbreak.py"])
def test_modules_cannot_reach_order_path(name):
    src = (ROOT / "app" / "engine" / name).read_text(encoding="utf-8")
    tree = ast.parse(src)
    seen = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            seen |= {a.name.split(".")[-1] for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            seen.add((node.module or "").split(".")[-1])
            seen |= {a.name for a in node.names}
        elif isinstance(node, ast.Attribute):
            seen.add(node.attr)
        elif isinstance(node, ast.Name):
            seen.add(node.id)
    assert seen & FORBIDDEN == set()


def test_engine_hooks_only_use_plain_notices():
    """core.py 里新加的挂钩函数: 只允许用 notice_subscribers 发文字。"""
    src = (ROOT / "app" / "engine" / "core.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    hooks = {"_build_alerts", "_alert_tick_tf", "_hook_error", "_forced_symbols", "_protected_tracks",
             "_on_tick", "_notify_bg", "_deliver_notices", "_prepare_linked",
             "notice_health", "_after_backfill", "_linked_refetch_kick", "_linked_refetch",
             "_stash_notice", "_backlog_loop"}
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in hooks:
            found.add(node.name)
            attrs = {n.attr for n in ast.walk(node) if isinstance(n, ast.Attribute)}
            assert attrs & (FORBIDDEN - {"binance_rest"}) == set(), node.name
    assert found == hooks


def test_paper_kind_copy_never_reaches_db(db, clock):
    async def go():
        env = Env(db, clock)
        await env.feed(BTC_BARS, PUMP_BARS, 0, SIGNAL_I + 1)
        assert [r["kind"] for r in rows(db, "SELECT kind FROM signals")] == ["alert"]
        assert PAPER_KIND != "alert"
    run(go())


# ============================ 启动预热: 历史补齐(公开K线) ============================

class KlineRest:
    """只有公开K线接口的假 REST; 别的属性一概没有(碰了就 AttributeError → 测试失败)。"""

    def __init__(self, bars_by_symbol, hang=False):
        self.bars, self.hang, self.calls = bars_by_symbol, hang, []

    async def klines(self, symbol, interval, limit=500, start_time=None):
        self.calls.append((symbol, interval, limit))
        if self.hang:
            await asyncio.sleep(3600)
        rows = list(self.bars[symbol][-limit:])
        last = rows[-1]
        return rows + [(last[0] + TF,) + tuple(last[1:8]) + (0,)]     # 末尾带一根未收盘K, 不应入库


def _recent_bars(n, mk):
    """生成 n 根贴着「现在」结束的已收盘 5m K(用真实时钟, 因为引擎按本机时间判断历史够不够)。"""
    import time as _t
    last_open = int(_t.time() * 1000) // TF * TF - TF
    shift = last_open - (BASE + (n - 1) * TF)
    return [(b[0] + shift,) + tuple(b[1:]) for b in mk(n)]


def test_startup_tops_up_history_then_seeds(db, clock):
    import time as _t

    async def go():
        env = Env(db, clock)
        e = env.engine
        btc = _recent_bars(120, lambda n: btc_series(n))
        pump = _recent_bars(120, lambda n: pump_series("d" * n))
        e.symbols = [BTC, PUMP]
        e.rest = KlineRest({BTC: btc, PUMP: pump})
        clock.t = _t.time()
        await e._prepare_linked()
        assert sorted(c[0] for c in e.rest.calls) == [BTC, PUMP]          # 每个品种恰好一次请求
        n_btc = db.one("SELECT COUNT(*) c, MAX(closed) m FROM klines WHERE symbol=?", (BTC,))
        assert 50 <= n_btc["c"] <= 60 and db.one("SELECT COUNT(*) c FROM klines WHERE closed=0")["c"] == 0
        st = e.linked.status()
        assert st["history_ok"] is True and st["phase"] == "idle" and st["window_bars"] == 24
        assert st["phase_certain"] is True and st["stale_symbols"] == []
        # 库里已经够了 → 再启动一次不再请求
        e.rest.calls.clear()
        await e._prepare_linked()
        assert e.rest.calls == []
        assert env.trader.hits == []
    run(go())


def test_startup_survives_rest_hang_or_failure(db, clock, monkeypatch):
    import time as _t

    async def go():
        monkeypatch.setattr(core_mod, "LINKED_TOPUP_TIMEOUT_S", 0.05)
        env = Env(db, clock)
        e = env.engine
        e.symbols = [BTC, PUMP]
        e.rest = KlineRest({}, hang=True)                                  # 被限流: 请求一直不返回
        clock.t = _t.time()
        t0 = _t.perf_counter()
        await e._prepare_linked()                                          # 不抛、不长时间卡住
        assert _t.perf_counter() - t0 < 2.0 and len(e.rest.calls) == 2
        st = e.linked.status()
        assert st["phase"] == "idle" and st["history_ok"] is False and "历史不足" in st["summary"]
        e.rest = Tripwire("rest")                                          # REST 整个不可用
        await e._prepare_linked()
        assert e.linked.status()["phase"] == "idle"
    run(go())


def test_seed_with_stale_history_expires_silently(db, clock):
    """库里的K线停在「刚待命」那一刻, 但本机时间已是几小时后(回填失败/长时间停机):
    预热后不是待命、不补报、不发到期通知; 之后实时K进来也不会为那一轮发任何东西。"""
    for i in range(T + 2):
        db.upsert_klines(BTC, "5m", [BTC_BARS[i]])
        db.upsert_klines(PUMP, "5m", [PUMP_BARS[i]])
    clock.t = (BASE + (T + 2) * TF) / 1000.0 + 5 * 3600
    cfg = Cfg()
    r = LinkedRuleRunner(cfg, db, PaperBroker(cfg, db), lambda s, tf: [], now=clock)
    assert r.seed() == []
    st = r.status()
    assert st["phase"] == "idle" and st["stale_symbols"] == [BTC, PUMP] and "无最新K线" in st["summary"]
    late_i = T + 2 + 60
    out = r.on_closed_bar(BTC, "5m", tup(late_i, 98, 98.5, 97.5, 98))
    out += r.on_closed_bar(PUMP, "5m", tup(late_i, 16.9, 17.0, 16.8, 16.9))
    assert out == [] and r.status()["counts"]["gap_missing"] > 0
    assert rows(db, "SELECT id FROM signals") == [] and rows(db, "SELECT id FROM paper_trades") == []


def test_seed_after_recorded_entry_links_back_to_signal(db, clock):
    async def go():
        env = Env(db, clock)
        await env.feed(BTC_BARS, PUMP_BARS, 0, SIGNAL_I + 1)
        sid = rows(db, "SELECT id FROM signals")[0]["id"]
        env2 = Env(db, clock)
        assert env2.engine.linked.seed() == []
        le = env2.engine.linked.status()["last_entry"]
        assert le["signal_id"] == sid and le["recorded"] is True
        assert le["signal_time"] == BASE + SIGNAL_I * TF
    run(go())


def test_entry_during_downtime_is_not_backfilled(db, clock):
    """入场信号出现在停机期间(K线由启动回填补进库): 预热不重放 → 不入库、不开模拟单, status 里标明未记录。"""
    for i in range(SIGNAL_I + 3):
        db.upsert_klines(BTC, "5m", [BTC_BARS[i]])
        db.upsert_klines(PUMP, "5m", [PUMP_BARS[i]])
    clock.t = (BASE + (SIGNAL_I + 3) * TF) / 1000.0
    cfg = Cfg()
    r = LinkedRuleRunner(cfg, db, PaperBroker(cfg, db), lambda s, tf: [], now=clock)
    assert r.seed() == []
    st = r.status()
    assert st["phase"] == "idle" and st["last_entry"]["recorded"] is False and st["last_entry"]["signal_id"] is None
    assert rows(db, "SELECT id FROM signals") == [] and rows(db, "SELECT id FROM paper_trades") == []


# ============================ 其它边角 ============================

def test_two_rules_status_and_tracks(db, clock):
    cfg = Cfg(linked_rules=[dict(RULE), {**RULE, "name": "btcbreak_eth", "target_symbol": ETH}])
    r = LinkedRuleRunner(cfg, db, PaperBroker(cfg, db), lambda s, tf: [], now=clock)
    assert r.symbols() == {BTC, PUMP, ETH} and r.paper_tracks() == ("btcbreak_pump", "btcbreak_eth")
    st = r.status()
    assert [x["rule"] for x in st["rules"]] == ["btcbreak_pump", "btcbreak_eth"]
    assert "btcbreak_pump" in st["summary"] and "btcbreak_eth" in st["summary"] and st["errors"] == 0
    assert sorted(n[0] for n in r.history_needs()) == [BTC, BTC, ETH, PUMP]
    # 一根 BTC K 同时喂给两条规则; 不相干的(币种, 级别)直接返回
    for rt in r._rules:
        rt.seeded = True
    assert r.on_closed_bar(BTC, "5m", BTC_BARS[0]) == [] and r.on_closed_bar(BTC, "15m", BTC_BARS[0]) == []
    assert r.on_closed_bar("SOLUSDT", "5m", BTC_BARS[0]) == []
    assert [x["counts"]["bars"] for x in r.status()["rules"]] == [1, 1]


def test_seed_failure_leaves_rule_idle_and_retries_are_bounded(db, clock):
    class BadDB:
        def __init__(self, real):
            self.real, self.calls = real, 0

        def get_klines(self, *a, **k):
            self.calls += 1
            raise RuntimeError("db locked")

        def __getattr__(self, name):
            return getattr(self.real, name)

    cfg, bad = Cfg(), BadDB(db)
    r = LinkedRuleRunner(cfg, bad, PaperBroker(cfg, db), lambda s, tf: [], now=clock)
    assert r.seed() == []
    st = r.status()
    assert st["phase"] == "idle" and st["history_ok"] is False and "预热失败" in st["history_note"]
    out = []
    for i in range(8):                                     # 收盘K回调里最多再试两次, 之后不再碰库
        clock.t = (BASE + (i + 1) * TF) / 1000.0
        out += r.on_closed_bar(BTC, "5m", BTC_BARS[i])
    assert out == [] and bad.calls == 3 and r.status()["counts"]["bars"] == 8


def test_notify_outside_event_loop_does_not_raise(db, clock):
    env = Env(db, clock)
    env.engine._notify_bg(["x"])                           # 没有运行中的事件循环: 只记一次错, 不抛
    assert env.engine.hook_errors == {"notify": 1} and env.notices == []
    env.engine._notify_bg([])
    env.engine._notify_bg([None, ""])
    assert env.engine.hook_errors == {"notify": 1}


def test_db_failure_on_entry_still_notifies_but_opens_nothing(db, clock):
    class FlakyDB:
        """入场那一刻写库失败(占位或插入), 其余照常。"""

        def __init__(self, real, fail):
            self.real, self.fail = real, fail

        def claim_signal_anchor(self, strategy, *a):
            if self.fail == "claim" and ":" not in strategy:
                raise RuntimeError("db locked")
            return self.real.claim_signal_anchor(strategy, *a)

        def insert_signal(self, row):
            if self.fail == "insert":
                raise RuntimeError("disk full")
            return self.real.insert_signal(row)

        def __getattr__(self, name):
            return getattr(self.real, name)

    for fail in ("claim", "insert"):
        cfg = Cfg(linked_rules=[{**RULE, "name": f"r_{fail}"}])
        fdb = FlakyDB(db, fail)
        r = LinkedRuleRunner(cfg, fdb, PaperBroker(cfg, db), lambda s, tf: [], now=clock)
        for rt in r._rules:
            rt.seeded = True
        out = []
        for i in range(SIGNAL_I + 1):
            clock.t = (BASE + (i + 1) * TF) / 1000.0
            out += r.on_closed_bar(BTC, "5m", BTC_BARS[i])
            out += r.on_closed_bar(PUMP, "5m", PUMP_BARS[i])
        assert len(out) == 2 and "模拟入场" in out[1] and "入库失败, 未开模拟单" in out[1], fail
        assert r.status()["errors"] == 1 and r.status()["last_entry"]["signal_id"] is None
    assert rows(db, "SELECT id FROM signals") == [] and rows(db, "SELECT id FROM paper_trades") == []
