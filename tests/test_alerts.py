"""近实时提醒引擎(app/engine/alerts.py)与 KlineWS 报价通路的单测。全部离线、时间可注入。"""
import asyncio
import json
import math
import random
import re
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db import DB
from app.engine import alerts as A
from app.engine.alerts import (
    AlertEngine, DropTrigger, RollingExtreme, bkk, fmt_price, last_break_from_bars, window_extreme,
)
from app.engine.binance_ws import KlineWS

T0 = 1_763_301_600            # 2025-11-16 14:00:00 UTC = 曼谷 11-16 21:00, 对齐到整点
assert T0 % 3600 == 0
SYM = "BTCUSDT"
TFS = (("5m", 300), ("15m", 900), ("1h", 3600))


class Cfg:
    def __init__(self, **v):
        self.v = {k.replace("__", "."): val for k, val in v.items()}

    def get(self, dotted, default=None):
        return self.v.get(dotted, default)


class Clock:
    def __init__(self, t=float(T0)):
        self.t = float(t)

    def __call__(self):
        return self.t


class Bars:
    """get_bars 桩: {(symbol, tf): [dict行...]}。"""

    def __init__(self):
        self.d = {}

    def __call__(self, symbol, tf):
        return self.d.get((symbol, tf), [])

    def fill(self, symbol, tf, tf_s, end_s, n, price=100.0, lows=None):
        """生成 n 根收盘于 end_s 之前的平盘K线; lows: {从最新往回数的序号: 最低价}。"""
        rows = []
        for i in range(n):
            ot = (end_s - (n - i) * tf_s) * 1000
            low = (lows or {}).get(n - 1 - i, price - 0.5)
            rows.append({"open_time": ot, "open": price, "high": price + 0.5, "low": low,
                         "close": price, "volume": 1.0, "quote_volume": price, "taker_buy": 0.5, "closed": 1})
        self.d[(symbol, tf)] = rows
        return rows


def fresh_db():
    return DB(Path(tempfile.mkdtemp()) / "t.db")


def make(db=None, bars=None, clock=None, **cfg):
    db = db or fresh_db()
    bars = bars if bars is not None else Bars()
    clock = clock or Clock()
    eng = AlertEngine(Cfg(**cfg), db, bars, now=clock)
    return eng, db, bars, clock


def tick(eng, clock, price, dt=1.0, sym=SYM):
    """时间前进 dt 秒后送一笔报价。"""
    clock.t += dt
    return eng.on_tick(sym, price, int(clock.t * 1000))


def bar5(end_s, o, h, l, c):
    return ((end_s - 300) * 1000, o, h, l, c, 10.0, 1000.0, 5.0, 1)


# ============================================================ 小件
def test_曼谷时间与价格格式():
    assert bkk(T0) == "11-16 21:00"
    assert bkk(1_763_251_200 + 6 * 3600 + 5 * 60) == "11-16 13:05"
    assert fmt_price(95000) == "95,000.0"
    assert fmt_price(150.25) == "150.25"
    assert fmt_price(0.00001234) == "0.00001234"


def test_滑动窗口极值_对照暴力解():
    rnd = random.Random(7)
    for sign in (1, -1):
        roll = RollingExtreme(300, sign)
        hist, t, px = [], 0.0, 100.0
        for _ in range(4000):
            t += rnd.choice((0.2, 1.0, 1.0, 3.0, 40.0))
            px *= 1 + rnd.uniform(-0.002, 0.002)
            hist.append((t, px))
            _, ext = roll.update(t, px)
            win = [p for (ts, p) in hist if ts >= t - 300]
            want = max(win) if sign > 0 else min(win)
            # 同一秒内的样本合并 → 允许差在"同一秒"里, 但极值本身必须是窗口里真实出现过的价格
            assert ext in win
            if ext != want:
                t_want = [ts for (ts, p) in hist if p == want][-1]
                assert t_want - (t - 300) < 1.0        # 只可能是恰好压在窗口边缘那一秒的样本
        assert len(roll) <= 302


def test_滑动窗口_内存有界():
    roll = RollingExtreme(300, 1)
    t, px = 0.0, 100000.0
    for _ in range(50000):                              # 一秒 10 笔, 价格单调下跌(最坏情况)
        t += 0.1
        px -= 0.01
        roll.update(t, px)
        assert len(roll) <= 302


def test_触发器_冷却与加深():
    g = DropTrigger(1.0, 1800, 0.5)
    assert g.check(0, 0.99) is None
    assert g.check(1, 1.0) == "new"
    assert g.check(2, 1.4) is None                      # 冷却内, 没加深够一个 step
    assert g.check(3, 1.5) == "deeper"                  # 1.0 + 0.5
    assert g.check(4, 1.9) is None                      # 基准已抬到 1.5
    assert g.check(5, 2.0) == "deeper"
    assert g.check(5 + 1799, 1.2) is None               # 冷却从最近一次响起算
    assert g.check(5 + 1800, 1.2) == "new"
    assert g.count == 4
    g0 = DropTrigger(1.0, 1800, 0)                      # step<=0 → 冷却内不升级
    assert g0.check(0, 1.0) == "new" and g0.check(1, 9.0) is None


# ============================================================ (a) 价位提醒
LV = dict(alerts__drop__enabled=False, alerts__range_break__enabled=False)   # 只测价位, 关掉另外两类


def test_价位_跌破_只响一次():
    eng, db, _, clock = make(**LV)
    assert tick(eng, clock, 100.0) == []
    r = eng.add_level(SYM, 95.0, note="前低")
    assert r["ok"] and r["alert"]["op"] == "below" and r["alert"]["status"] == "active"
    assert set(r["alert"]) == {"id", "symbol", "op", "price", "note", "status",
                               "created_at", "fired_at", "fired_price"}
    assert tick(eng, clock, 95.1) == []
    msgs = tick(eng, clock, 94.9)
    assert len(msgs) == 1
    assert msgs[0].startswith("🎯 <b>价位提醒</b> BTCUSDT 跌破 95")
    assert "现价 94.9" in msgs[0] and "前低" in msgs[0] and f"#{r['alert']['id']}" in msgs[0]
    assert tick(eng, clock, 94.0) == []                 # 不重复
    assert tick(eng, clock, 96.0) == [] and tick(eng, clock, 94.0) == []   # 回去再破也不响
    assert eng.list_levels() == []
    row = eng.list_levels(include_fired=True)[0]
    assert row["status"] == "fired" and row["fired_price"] == 94.9 and row["fired_at"] == int(clock.t) - 3


def test_价位_突破_方向自动推断():
    eng, _, _, clock = make(**LV)
    tick(eng, clock, 100.0)
    r = eng.add_level(SYM, 105.0)
    assert r["ok"] and r["alert"]["op"] == "above"
    assert tick(eng, clock, 104.99) == []
    msgs = tick(eng, clock, 105.0)                      # 触及即算
    assert len(msgs) == 1 and "涨破 105" in msgs[0]
    assert tick(eng, clock, 106.0) == []


def test_价位_创建校验():
    eng, _, bars, clock = make(alerts__levels__max_active=3, **LV)
    assert not eng.add_level(SYM, 95.0)["ok"]                         # 没有任何价格 → 拒绝
    tick(eng, clock, 100.0)
    for bad in (0, -1, "abc", None, float("nan"), float("inf")):
        r = eng.add_level(SYM, bad)
        assert not r["ok"] and "价格" in r["error"]
    r = eng.add_level(SYM, 105.0, op="below")                          # 现价已在该位下方
    assert not r["ok"] and "下方" in r["error"]
    r = eng.add_level(SYM, 95.0, op="above")
    assert not r["ok"] and "上方" in r["error"]
    assert not eng.add_level(SYM, 100.0)["ok"]                         # 与现价相同且没给方向
    assert not eng.add_level(SYM, 95.0, op="sideways")["ok"]
    assert not eng.add_level("<script>", 95.0)["ok"]
    assert not eng.add_level("DOGEUSDT", 0.1)["ok"]                    # 不在监控里(没行情)
    assert eng.add_level(SYM, 95.0, op="跌破")["ok"]                   # 中文方向别名
    r = eng.add_level(SYM, 95.0, op="below")
    assert not r["ok"] and "相同" in r["error"]                        # 重复
    assert eng.add_level(SYM, 94.0)["ok"] and eng.add_level(SYM, 110.0)["ok"]
    r = eng.add_level(SYM, 93.0)
    assert not r["ok"] and "上限" in r["error"]                        # 上限 3 条
    assert len(eng.list_levels()) == 3
    assert eng.status()["counts"]["levels_active"] == 3


def test_价位_没有报价时用收盘价判断_并支持省略USDT():
    eng, _, bars, clock = make(**LV)
    bars.fill("ETHUSDT", "5m", 300, T0, 3, price=3000.0)
    assert eng.last_price("ETHUSDT") == 3000.0
    assert eng.last_price("NOPEUSDT") is None
    r = eng.add_level("eth", 2900)
    assert r["ok"] and r["alert"]["symbol"] == "ETHUSDT" and r["alert"]["op"] == "below"
    assert eng.tick_symbols() == {"BTCUSDT", "ETHUSDT"}                # 有活动价位的币也要报价
    assert tick(eng, clock, 2950.0, sym="ETHUSDT") == []
    assert len(tick(eng, clock, 2899.0, sym="ETHUSDT")) == 1
    assert eng.tick_symbols() == {"BTCUSDT"}                           # 响完就不再需要它的报价


def test_价位_重启后的行为():
    db, clock = fresh_db(), Clock()
    eng1, *_ = make(db=db, clock=clock, **LV)
    tick(eng1, clock, 100.0)
    a = eng1.add_level(SYM, 95.0)["alert"]
    b = eng1.add_level(SYM, 105.0)["alert"]

    # 重启: 首个报价仍在两条线之间 → 不能因为"上一笔价格丢了"就响
    eng2, *_ = make(db=db, clock=clock, **LV)
    assert {r["id"] for r in eng2.list_levels()} == {a["id"], b["id"]}
    assert tick(eng2, clock, 99.0) == []
    assert tick(eng2, clock, 96.0) == []

    # 再重启: 停机期间价格已经跌穿 95 → 依据"库里那条是活动的跌破提醒 + 现价"判定, 响一次并注明
    eng3, *_ = make(db=db, clock=clock, **LV)
    msgs = tick(eng3, clock, 94.0)
    assert len(msgs) == 1 and "跌破 95" in msgs[0] and "重启后首个报价已在该位下方" in msgs[0]
    assert tick(eng3, clock, 93.0) == []

    # 第三次重启: 已触发的不会再响, 另一条仍然有效
    eng4, *_ = make(db=db, clock=clock, **LV)
    assert [r["id"] for r in eng4.list_levels()] == [b["id"]]
    assert tick(eng4, clock, 93.0) == []
    msgs = tick(eng4, clock, 105.5)
    assert len(msgs) == 1 and "涨破 105" in msgs[0] and "重启后" not in msgs[0]


def test_价位_删除():
    eng, db, _, clock = make(**LV)
    tick(eng, clock, 100.0)
    a = eng.add_level(SYM, 95.0)["alert"]
    assert eng.remove_level(a["id"]) is True
    assert eng.remove_level(a["id"]) is False and eng.remove_level(9999) is False
    assert eng.remove_level("x") is False
    assert tick(eng, clock, 90.0) == []
    assert eng.list_levels(include_fired=True) == []
    eng2, *_ = make(db=db, clock=clock, **LV)                                # 删除是持久的
    assert tick(eng2, clock, 90.0) == []


def test_价位_备注转义():
    eng, _, _, clock = make(**LV)
    tick(eng, clock, 100.0)
    eng.add_level(SYM, 95.0, note="<b>x</b> & y")
    msg = tick(eng, clock, 94.0)[0]
    assert "&lt;b&gt;x&lt;/b&gt; &amp; y" in msg and "<b>x</b>" not in msg


def test_价位_on_tick不读库():
    eng, db, _, clock = make(**LV)
    tick(eng, clock, 100.0)
    eng.add_level(SYM, 95.0)

    class Boom:
        def __getattr__(self, name):
            raise AssertionError("on_tick 不应访问数据库")
    eng.db = Boom()
    for px in (99.0, 98.0, 97.0, 96.0):
        assert tick(eng, clock, px) == []
    eng.db = db


def test_价位_写库失败也只响一次_之后补写():
    eng, db, _, clock = make(**LV)
    tick(eng, clock, 100.0)
    a = eng.add_level(SYM, 95.0)["alert"]

    class Broken:
        def execute(self, *a, **k):
            raise RuntimeError("database is locked")
    eng.db = Broken()
    assert len(tick(eng, clock, 94.0)) == 1
    assert tick(eng, clock, 93.0) == []
    eng.db = db
    assert db.one("SELECT status FROM price_alerts WHERE id=?", (a["id"],))["status"] == "active"
    clock.t += 300
    eng.on_closed_bar(SYM, "5m", bar5(clock.t, 93, 93, 93, 93))        # 收盘K回调里补写
    assert db.one("SELECT status FROM price_alerts WHERE id=?", (a["id"],))["status"] == "fired"


# ============================================================ (b) 急跌提醒
DROP5 = dict(alerts__drop__windows_min=[5], alerts__drop__pct_5m=1.0,
             alerts__drop__cooldown_min_5m=30, alerts__drop__step_pct_5m=0.5,
             alerts__range_break__enabled=False)


def test_急跌_窗口_冷却_加深():
    eng, _, _, clock = make(**DROP5)
    assert tick(eng, clock, 100.0) == []
    assert tick(eng, clock, 99.5, 60) == []                            # -0.5%
    msgs = tick(eng, clock, 99.0, 60)                                  # -1.0% → 响
    assert len(msgs) == 1
    m = msgs[0]
    assert m.startswith("🔻 <b>急跌</b> BTCUSDT 5分钟 -1.00%")
    assert "现价 99" in m and "窗口高点 100" in m and "阈值 1%" in m and bkk(clock.t) in m
    assert tick(eng, clock, 98.9, 10) == []                            # 冷却内, 只深了一点
    msgs = tick(eng, clock, 98.5, 10)                                  # -1.5% = 上次 + step → 提前再响
    assert len(msgs) == 1 and msgs[0].startswith("🔻 <b>急跌加深</b>") and "上次 -1.00%" in msgs[0]
    assert tick(eng, clock, 98.4, 10) == []
    # 高点 100 滑出 5 分钟窗口后, 跌幅按窗口内的新高点算 → 不再是 -1.6%
    assert tick(eng, clock, 98.4, 300) == []
    assert tick(eng, clock, 97.5, 60) == []                            # 窗口内 -0.91%, 不到阈值
    assert tick(eng, clock, 98.1, 10) == []
    # 窗口内 -1.02%(98.4 → 97.4): 过了阈值; 没比上次的 -1.5% 再深 0.5, 但现价比上次提醒价 98.5 低了 1.12%
    # ≥ 步长 0.5% —— 这是"第二段"(价格比上次提醒时更低) → 冷却内也再响一条
    msgs = tick(eng, clock, 97.4, 10)
    assert len(msgs) == 1 and msgs[0].startswith("🔻 <b>急跌加深</b> BTCUSDT 5分钟 -1.02%")
    assert "较上次提醒价 98.5 再跌 1.12%" in msgs[0] and "上次 -" not in msgs[0]
    assert tick(eng, clock, 97.3, 10) == []                            # 只比上次提醒价(97.4)低 0.1% → 不响
    # 冷却(30 分钟, 自最近一次响起算)过后, 再有 ≥1% 的回落 → 新的一次
    assert tick(eng, clock, 97.3, 1800) == []
    assert tick(eng, clock, 98.0, 10) == []
    msgs = tick(eng, clock, 97.0, 60)
    assert len(msgs) == 1 and msgs[0].startswith("🔻 <b>急跌</b>")
    assert eng.status()["counts"]["fired"]["drop"] == 4


def test_急跌_冷却内第二段更低也响_窗口没到阈值不响():
    """评审复现: 第一段急跌报过之后反弹, 冷却期内第二段把价格带到更低 —— 以前整段沉默。"""
    eng, _, _, clock = make(**DROP5)                                   # 5分钟 ≥1%, 冷却 30 分钟, 步长 0.5%
    tick(eng, clock, 100.0)
    assert len(tick(eng, clock, 98.9, 120)) == 1                       # 第一段 -1.1% → 响(提醒价 98.9)
    assert tick(eng, clock, 99.6, 400) == []                           # 反弹
    # 缓慢阴跌到更低: 窗口内幅度不到阈值 → 不是"急跌", 不响(这归破位提醒管)
    for px in (99.3, 99.0, 98.7, 98.4, 98.2):
        assert tick(eng, clock, px, 200) == []
    assert tick(eng, clock, 98.6, 200) == []
    # 第二段急跌: 5 分钟内 98.6 → 97.5 = -1.12% ≥ 阈值, 且比上次提醒价 98.9 低 1.42% ≥ 步长 → 响
    msgs = tick(eng, clock, 97.5, 120)
    assert len(msgs) == 1 and "急跌加深" in msgs[0] and "较上次提醒价 98.9 再跌 1.42%" in msgs[0]
    # DropTrigger 不传价格时保持旧口径(校准脚本的旧调用方式不受影响)
    trig = DropTrigger(1.0, 1800.0, 0.5)
    assert trig.check(0.0, 1.1) == "new" and trig.check(10.0, 1.2) is None and trig.check(20.0, 1.6) == "deeper"
    trig = DropTrigger(1.0, 1800.0, 0.5)
    assert trig.check(0.0, 1.1, 100.0) == "new" and trig.check(10.0, 1.2, 99.6) is None
    assert trig.check(20.0, 1.2, 99.5) == "lower" and trig.check(30.0, 1.2, 99.4) is None
    # "更低"这一档还要求该币任何窗口最近 quiet_s 秒内没响过(一路直线暴跌时不刷屏); "加深"不受此限
    trig = DropTrigger(1.0, 1800.0, 0.5)
    assert trig.check(0.0, 1.1, 100.0, 1, None, 300.0) == "new"
    assert trig.check(60.0, 1.2, 99.0, 1, 50.0, 300.0) is None           # 10 秒前别的窗口刚响过
    assert trig.check(70.0, 1.7, 98.9, 1, 50.0, 300.0) == "deeper"
    assert trig.check(400.0, 1.2, 98.0, 1, 70.0, 300.0) == "lower"       # 安静了 330 秒
    up = DropTrigger(1.0, 1800.0, 0.5)                                 # 急涨方向: 更高才算
    assert up.check(0.0, 1.1, 100.0, -1) == "new" and up.check(10.0, 1.2, 99.0, -1) is None
    assert up.check(20.0, 1.2, 100.5, -1) == "lower"


def test_急跌_窗口外的高点不算():
    eng, _, _, clock = make(**DROP5)
    tick(eng, clock, 100.0)
    assert tick(eng, clock, 99.4, 150) == []
    assert tick(eng, clock, 98.9, 151) == []          # 距 100 已 301 秒; 窗口内最高是 99.4 → -0.5%
    assert len(tick(eng, clock, 98.4, 100)) == 1      # 99.4 → 98.4 = -1.006%


def test_急跌_两个窗口同一笔触发只发一条():
    eng, _, _, clock = make(alerts__drop__pct_5m=1.0, alerts__drop__pct_15m=1.0,
                            alerts__range_break__enabled=False)
    tick(eng, clock, 100.0)
    msgs = tick(eng, clock, 98.9, 30)
    assert len(msgs) == 1
    assert "5分钟 -1.10%" in msgs[0] and "15分钟 -1.10%" in msgs[0]
    assert eng.status()["counts"]["fired"]["drop"] == 1


def test_急跌_默认只报下跌_可开上涨():
    eng, _, _, clock = make(**DROP5)
    tick(eng, clock, 100.0)
    assert tick(eng, clock, 103.0, 30) == []                           # 默认不报急涨
    eng2, _, _, clock2 = make(**DROP5, alerts__drop__directions=["down", "up"])
    tick(eng2, clock2, 100.0)
    msgs = tick(eng2, clock2, 101.5, 30)
    assert len(msgs) == 1 and msgs[0].startswith("🔺 <b>急涨</b> BTCUSDT 5分钟 +1.50%")


def test_急跌_只对配置的币种生效():
    eng, _, bars, clock = make(**DROP5)
    bars.fill("ETHUSDT", "5m", 300, T0, 3, price=3000.0)
    assert eng.add_level("ETHUSDT", 2000.0)["ok"]                      # ETH 只有价位提醒
    tick(eng, clock, 3000.0, sym="ETHUSDT")
    assert tick(eng, clock, 2900.0, 10, sym="ETHUSDT") == []           # -3.3% 也不报急跌
    assert tick(eng, clock, 100.0, sym="SOLUSDT") == []                # 完全不关心的币
    assert "SOLUSDT" not in eng.status()["last_tick_age"]


def test_默认阈值_来自校准():
    eng, *_ = make()
    th = eng.status()["thresholds"]
    assert [(w["minutes"], w["pct"]) for w in th["drop"]["windows"]] == \
        [(m, A.DEFAULT_DROP[m]["pct"]) for m in A.DEFAULT_DROP_WINDOWS]
    assert th["drop"]["directions"] == ["down"] and th["range_break"]["directions"] == ["down"]
    assert th["range_break"]["days"] == [2]
    assert eng.tick_symbols() == {"BTCUSDT"}


# ============================================================ (c) 破位提醒
def range_setup(**cfg):
    """3 天 15m + 41.7h 5m 平盘在 100; 30 小时前有个 2 日内最低 90; 60 小时前有个更低的 80(窗口外)。"""
    eng, db, bars, clock = make(alerts__drop__enabled=False, **cfg)
    bars.fill(SYM, "15m", 900, T0, 500, lows={30 * 4: 90.0, 60 * 4: 80.0})
    bars.fill(SYM, "5m", 300, T0, 500, lows={30 * 12: 90.0})
    bars.fill(SYM, "1h", 3600, T0, 500, lows={30: 90.0, 60: 80.0})
    return eng, db, bars, clock


def test_破位_参照低点取两日内最低():
    eng, _, bars, clock = range_setup()
    low, low_t, tf = window_extreme(bars, SYM, TFS, 2, 1, T0)
    assert low == 90.0 and tf == "15m"                                 # 5m 只够 41.7h → 用 15m 覆盖
    assert low_t == T0 - 30 * 3600                                     # 那根K线的收盘时刻
    assert window_extreme(bars, SYM, TFS, 3, 1, T0)[0] == 80.0         # 3 日窗口就看得到 80
    assert window_extreme(bars, SYM, TFS, 2, -1, T0)[0] == 100.5       # 高点
    assert eng.on_closed_bar(SYM, "5m", bar5(T0, 100, 100.5, 99.5, 100)) == []
    ref = eng.status()["ref_lows"][SYM]["2d_low"]
    assert ref["price"] == 90.0 and ref["tf"] == "15m" and ref["at_bkk"] == bkk(T0 - 30 * 3600)


def test_破位_覆盖不到窗口就不给参照价():
    bars = Bars()
    bars.fill(SYM, "5m", 300, T0, 100)                                 # 只有 8 小时
    assert window_extreme(bars, SYM, TFS, 2, 1, T0) == (None, None, "5m")
    assert window_extreme(Bars(), SYM, TFS, 2, 1, T0) == (None, None, None)
    eng, _, _, clock = make(bars=bars, alerts__drop__enabled=False)
    eng.on_closed_bar(SYM, "5m", bar5(T0, 100, 100.5, 99.5, 100))
    assert eng.status()["ref_lows"][SYM]["2d_low"]["price"] is None
    assert tick(eng, clock, 50.0) == []                                # 没参照价 → 不报


def test_破位_响一次_新低不重复_冷却后再破才响():
    eng, _, bars, clock = range_setup()
    eng.on_closed_bar(SYM, "5m", bar5(T0, 100, 100.5, 99.5, 100))
    assert tick(eng, clock, 90.0) == []                                # 触及不算, 要跌破
    msgs = tick(eng, clock, 89.9)
    assert len(msgs) == 1
    assert msgs[0].startswith("📉 <b>跌破2日低点</b> BTCUSDT")
    assert "现价 89.9 · 近2日最低 90" in msgs[0] and bkk(T0 - 30 * 3600) in msgs[0]
    for px in (89.8, 89.5, 89.0, 88.0):                                # 一路新低 → 不再响
        assert tick(eng, clock, px, 30) == []
    # 5m 收盘: 参照低点更新成这根的最低 88
    clock.t = T0 + 300
    assert eng.on_closed_bar(SYM, "5m", bar5(T0 + 300, 100, 100, 88.0, 88.5)) == []
    assert eng.status()["ref_lows"][SYM]["2d_low"]["price"] == 88.0
    assert tick(eng, clock, 87.9, 3600) == []                          # 1 小时后再创新低: 仍在冷却(4h)内
    assert tick(eng, clock, 87.8, 3 * 3600) == []                      # 冷却从"上一次创新低"顺延
    msgs = tick(eng, clock, 87.7, 4 * 3600)                            # 横了 4 小时后再破 → 新的一次
    assert len(msgs) == 1 and "跌破2日低点" in msgs[0]
    assert eng.status()["counts"]["fired"]["range"] == 2


def test_破位_重启在下跌途中不重复报():
    # 1 小时前那根 15m 创了 2 日新低(=当时已经破位) → 重启后再创新低不应再报一遍
    bars = Bars()
    bars.fill(SYM, "15m", 900, T0, 500, lows={30 * 4: 90.0, 4: 89.0})
    bars.fill(SYM, "5m", 300, T0, 500, lows={30 * 12: 90.0, 12: 89.0})
    assert last_break_from_bars(bars, SYM, TFS, 2, 1, T0, 4 * 3600) == T0 - 3600
    eng, _, _, clock = make(bars=bars, alerts__drop__enabled=False)
    assert tick(eng, clock, 95.0) == []                                # 首笔报价: 顺带算出参照价
    assert eng.status()["ref_lows"][SYM]["2d_low"]["price"] == 89.0
    assert tick(eng, clock, 88.5) == []                                # 同一次破位的延续
    # 对照: 低点是 10 小时前创的(早过了冷却) → 现在跌破要报
    bars2 = Bars()
    bars2.fill(SYM, "15m", 900, T0, 500, lows={30 * 4: 90.0, 40: 89.0})
    bars2.fill(SYM, "5m", 300, T0, 500, lows={30 * 12: 90.0, 120: 89.0})
    assert last_break_from_bars(bars2, SYM, TFS, 2, 1, T0, 4 * 3600) is None
    eng2, _, _, clock2 = make(bars=bars2, alerts__drop__enabled=False)
    assert tick(eng2, clock2, 95.0) == []
    assert len(tick(eng2, clock2, 88.5)) == 1


def test_破位_刚收盘的K线即使缓存还没更新也算进参照价():
    eng, _, bars, clock = range_setup()
    clock.t = T0 + 300
    tick(eng, clock, 100.0, 0)                                         # 报价是活的 → 不走兜底
    eng.on_closed_bar(SYM, "5m", bar5(T0 + 300, 100, 100, 85.0, 99))   # 缓存里还没有这根
    assert eng.status()["ref_lows"][SYM]["2d_low"]["price"] == 85.0


# ============================================================ 兜底 / 健壮性
def test_报价缺席时用收盘K兜底():
    eng, _, bars, clock = make(**DROP5)
    bars.fill(SYM, "5m", 300, T0, 3, price=100.0)
    assert eng.add_level(SYM, 95.0)["ok"]                              # 用收盘价 100 校验
    clock.t = T0 + 300
    assert eng.on_closed_bar(SYM, "5m", bar5(T0 + 300, 100, 100.2, 99.8, 100)) == []
    clock.t = T0 + 600                                                 # 这根下影刺穿 95, 且 5 分钟跌 ≥1%
    msgs = eng.on_closed_bar(SYM, "5m", bar5(T0 + 600, 100, 100.1, 94.0, 96.0))
    kinds = sorted(m[0] for m in msgs)
    assert kinds == ["🎯", "🔻"]
    # 看得出不是实时报价触发的, 且写明K线内的先后顺序是推定的(阴线按 开→高→低→收)
    assert all(m.endswith("无实时报价, 由5m收盘K按 开→高→低→收 估算(K内先后为推定)") for m in msgs)
    assert eng.on_closed_bar(SYM, "15m", bar5(T0 + 600, 100, 100.1, 90.0, 96.0)) == []   # 非基准级别不处理


def test_报价正常时收盘K不重复判定():
    eng, _, bars, clock = make(**DROP5)
    tick(eng, clock, 100.0)
    assert eng.add_level(SYM, 95.0)["ok"]
    clock.t = T0 + 299
    tick(eng, clock, 100.0, 0)                                         # 收盘前 1 秒还有报价
    clock.t = T0 + 300
    assert eng.on_closed_bar(SYM, "5m", bar5(T0 + 300, 100, 100, 94.0, 100)) == []
    assert len(eng.list_levels()) == 1


def test_报价正常时_不足一秒的插针由收盘K补上():
    eng, _, bars, clock = make(alerts__drop__enabled=False, alerts__range_break__enabled=False)
    tick(eng, clock, 100.0)
    assert eng.add_level(SYM, 95.0, note="前低")["ok"] and eng.add_level(SYM, 104.0)["ok"]
    clock.t = T0 + 600 - 1
    tick(eng, clock, 100.0, 0)                                         # 报价一直是活的, 但没采到 94.8 那一下
    clock.t = T0 + 600
    msgs = eng.on_closed_bar(SYM, "5m", bar5(T0 + 600, 100, 100.5, 94.8, 99.9))   # 这根K在提醒建立之后才开盘
    assert len(msgs) == 1 and "跌破 95" in msgs[0] and "5m最低 94.8" in msgs[0] and "前低" in msgs[0]
    assert "补判" not in msgs[0]
    assert [r["op"] for r in eng.list_levels()] == ["above"]
    row = [r for r in eng.list_levels(True) if r["status"] == "fired"][0]
    assert row["fired_price"] == 94.8
    clock.t = T0 + 900
    tick(eng, clock, 100.0, -1)
    assert eng.on_closed_bar(SYM, "5m", bar5(T0 + 900, 100, 100.5, 94.0, 99.9)) == []   # 不重复


def test_兜底_本根K线内新建的价位只看收盘价():
    eng, _, bars, clock = make(alerts__drop__enabled=False, alerts__range_break__enabled=False)
    bars.fill(SYM, "5m", 300, T0, 3, price=100.0)
    clock.t = T0 + 120                                                 # K线开盘 2 分钟后才建的提醒
    assert eng.add_level(SYM, 95.0)["ok"]
    clock.t = T0 + 300
    assert eng.on_closed_bar(SYM, "5m", bar5(T0 + 300, 100, 100, 94.0, 99.0)) == []   # 94 的下影可能在建之前
    clock.t = T0 + 600
    assert len(eng.on_closed_bar(SYM, "5m", bar5(T0 + 600, 99, 99, 94.5, 98.0))) == 1


def test_脏输入永不抛异常():
    def bad_bars(symbol, tf):
        raise RuntimeError("cache gone")
    eng, _, _, clock = make(bars=bad_bars)
    for args in ((None, "abc", None), (SYM, float("nan"), -1), (["x"], 1, 2), (SYM, 100, "zzz"),
                 (SYM, -5, T0 * 1000), (SYM, float("inf"), T0 * 1000), (SYM, "100", 1e30),
                 (SYM, None, None), (object(), object(), object()), (SYM, 100, float("nan"))):
        assert eng.on_tick(*args) == []
    for args in ((SYM, "5m", ()), (None, None, None), (SYM, "5m", ("a", "b", "c", "d", "e")),
                 (SYM, "5m", None), (SYM, "5m", (T0 * 1000, 1, float("nan"), -1, 0, 0, 0, 0, 1)),
                 (SYM, 5, (1, 2, 3, 4, 5)), ({}, [], 3)):
        assert eng.on_closed_bar(*args) == []
    assert eng.add_level(None, None)["ok"] is False
    assert eng.add_level("ETHUSDT", 95.0)["ok"] is False               # 取不到行情
    assert eng.add_level(SYM, 95.0, op=object())["ok"] is False
    assert eng.remove_level(None) is False
    assert eng.last_price(None) is None and eng.last_price(SYM) is not None    # 已有一笔合法报价("100")
    assert isinstance(eng.status(), dict) and isinstance(eng.list_levels(True), list)
    assert isinstance(eng.tick_symbols(), set)


def test_时间戳离谱时改用本机时间():
    eng, _, _, clock = make(**DROP5)
    assert eng.on_tick(SYM, 100.0, 10 ** 18) == []                     # 脏时间戳不该把窗口时间推到未来
    clock.t += 60
    assert eng.on_tick(SYM, 99.5, 0) == []
    clock.t += 60
    msgs = eng.on_tick(SYM, 98.9, int(clock.t * 1000))
    assert len(msgs) == 1 and "5分钟 -1.10%" in msgs[0]


def test_配置是脏值时退回默认():
    eng, *_ = make(alerts__drop__pct_5m="abc", alerts__drop__windows_min="x,y",
                   alerts__range_break__days=None, alerts__symbols="btcusdt, ethusdt",
                   alerts__drop__cooldown_min_15m=-5, alerts__levels__max_active="zz",
                   alerts__drop__pct_15m=0, timeframes="bogus")
    th = eng.status()["thresholds"]
    assert [w["pct"] for w in th["drop"]["windows"]] == [A.DEFAULT_DROP[5]["pct"], A.DEFAULT_DROP[15]["pct"]]
    assert th["drop"]["windows"][1]["cooldown_min"] == 0
    assert th["range_break"]["days"] == [2] and th["levels"]["max_active"] == 50
    assert eng.tick_symbols() == {"BTCUSDT", "ETHUSDT"} and eng.status()["base_tf"] == "5m"

    class NoCfg:
        def get(self, *a, **k):
            raise RuntimeError("no config")
    eng2 = AlertEngine(NoCfg(), fresh_db(), Bars(), now=Clock())
    assert eng2.tick_symbols() == {"BTCUSDT"}


def test_总开关与热更新():
    cfg = Cfg(**DROP5)
    clock = Clock()
    eng = AlertEngine(cfg, fresh_db(), Bars(), now=clock)
    tick(eng, clock, 100.0)
    cfg.v["alerts.drop.pct_5m"] = 3.0                                  # 运行中改阈值
    clock.t += 61
    eng.on_closed_bar(SYM, "15m", bar5(clock.t, 100, 100, 100, 100))   # 任意收盘K回调里重读(每分钟至多一次)
    assert eng.status()["thresholds"]["drop"]["windows"][0]["pct"] == 3.0
    assert tick(eng, clock, 98.0, 30) == []                            # -2% < 3%
    assert len(tick(eng, clock, 96.9, 30)) == 1
    cfg.v["alerts.enabled"] = False
    clock.t += 61
    eng.on_closed_bar(SYM, "5m", bar5(clock.t, 100, 100, 100, 100))
    assert eng.tick_symbols() == set() and eng.status()["enabled"] is False
    assert tick(eng, clock, 50.0) == []
    assert eng.add_level(SYM, 40.0)["ok"] is False


def _tg_html_ok(msg: str) -> bool:
    """Telegram HTML: 除了 <b></b> 标签, 不能有裸的 < > &。"""
    body = msg.replace("<b>", "").replace("</b>", "")
    body = re.sub(r"&(lt|gt|amp|quot|#x27|#\d+);", "", body)
    return not any(ch in body for ch in "<>&")


def test_所有文案都是合法的TelegramHTML_且不超过四行():
    eng, _, bars, clock = make(alerts__drop__directions=["down", "up"], alerts__drop__pct_5m=1.0,
                               alerts__drop__pct_15m=1.0, alerts__range_break__directions="both")
    bars.fill(SYM, "15m", 900, T0, 500, lows={30 * 4: 90.0})
    bars.fill(SYM, "5m", 300, T0, 500, lows={30 * 12: 90.0})
    tick(eng, clock, 100.0)
    assert eng.add_level(SYM, 99.5, note='a<b & c>d "q"')["ok"]
    assert eng.add_level(SYM, 100.4, note="上沿")["ok"]
    out = []
    for px, dt in ((100.45, 5), (99.4, 5), (97.8, 5), (89.0, 5), (91.0, 5), (101.0, 5), (102.5, 5)):
        out += tick(eng, clock, px, dt)
    titles = {m.split("</b>")[0] for m in out}
    assert titles == {"🎯 <b>价位提醒", "🔻 <b>急跌", "🔻 <b>急跌加深", "📉 <b>跌破2日低点",
                      "🔺 <b>急涨", "🔺 <b>急涨加速", "📈 <b>突破2日高点"}
    for m in out:
        assert _tg_html_ok(m), m
        assert 2 <= len(m.split("\n")) <= 4, m
        assert bkk(clock.t)[:5] in m                                    # 都带曼谷日期


def test_status_结构():
    eng, _, _, clock = make()
    tick(eng, clock, 100.0)
    clock.t += 5
    s = eng.status()
    assert s["enabled"] is True and s["symbols"] == ["BTCUSDT"] and s["tick_symbols"] == ["BTCUSDT"]
    assert s["last_tick_age"] == {"BTCUSDT": 5.0} and s["last_price"] == {"BTCUSDT": 100.0}
    assert set(s["thresholds"]) == {"drop", "range_break", "levels"}
    assert s["counts"]["ticks"] == 1 and s["counts"]["errors"] == 0
    assert set(s["counts"]["fired"]) == {"level", "drop", "range"}
    json.dumps(s)                                                      # 可直接给 web 接口


# ============================================================ KlineWS 报价通路
def ws_msg(sym="BTCUSDT", tf="5m", c="100.5", ev=1_000_000, closed=False):
    return json.dumps({"stream": f"{sym.lower()}@kline_{tf}", "data": {
        "e": "kline", "E": ev, "s": sym,
        "k": {"t": 600000, "T": 899999, "s": sym, "i": tf, "o": "100", "c": c, "h": "101", "l": "99",
              "v": "12.5", "q": "1250.5", "V": "6.5", "Q": "650", "n": 10, "x": closed, "B": "0"}}})


def make_ws(**kw):
    closed, ticks = [], []

    async def on_closed(symbol, tf, bar):
        closed.append((symbol, tf, bar))

    def on_tick(symbol, price, ts_ms):
        ticks.append((symbol, price, ts_ms))

    kw.setdefault("on_tick", on_tick)
    ws = KlineWS("wss://example.invalid/", on_closed, **kw)
    return ws, closed, ticks


def feed(ws, *raws):
    async def run():
        for r in raws:
            await ws._handle(r)
    asyncio.run(run())


def test_ws_收盘通路与原来一致():
    expect = ("BTCUSDT", "5m", (600000, 100.0, 101.0, 99.0, 100.5, 12.5, 1250.5, 6.5, 1))
    # 不带报价通路(旧用法)
    ws0, closed0, _ = make_ws(on_tick=None)
    feed(ws0, ws_msg(closed=False), ws_msg(closed=True), "not json", json.dumps({"result": None, "id": 1}))
    assert closed0 == [expect]
    # 带报价通路: 收盘K照样回调、元组一模一样, 且收盘那条不当报价发
    ws1, closed1, ticks1 = make_ws(tick_symbols=lambda: {"BTCUSDT"}, tick_tf="5m")
    feed(ws1, ws_msg(closed=True), "not json", json.dumps({"data": {}}))
    assert closed1 == [expect] and ticks1 == []


def test_ws_报价节流_每币每秒一笔():
    ws, closed, ticks = make_ws(tick_symbols=lambda: {"BTCUSDT"}, tick_tf="5m")
    raws = [ws_msg(c=str(100 + i), ev=1_000_000 + i * 250) for i in range(9)]   # 250ms 一条, 共 2 秒
    feed(ws, *raws)
    assert ticks == [("BTCUSDT", 100.0, 1_000_000), ("BTCUSDT", 104.0, 1_001_000),
                     ("BTCUSDT", 108.0, 1_002_000)]
    assert closed == [] and ws.tick_count == 3
    feed(ws, ws_msg(c="50", ev=999_000))                               # 事件时间倒退(重连) → 放行并重置
    assert ticks[-1] == ("BTCUSDT", 50.0, 999_000)


def test_ws_只取指定级别与指定币种():
    ws, _, ticks = make_ws(tick_symbols=lambda: {"BTCUSDT"}, tick_tf="5m")
    feed(ws, ws_msg(tf="15m"), ws_msg(tf="1h"), ws_msg(sym="ETHUSDT"), ws_msg(sym="ETHUSDT", tf="15m"))
    assert ticks == []
    feed(ws, ws_msg())
    assert len(ticks) == 1


def test_ws_订阅集合会重读():
    want = {"BTCUSDT"}
    ws, _, ticks = make_ws(tick_symbols=lambda: want, tick_tf="5m")
    feed(ws, ws_msg(sym="ETHUSDT"))
    assert ticks == []
    want = {"BTCUSDT", "ETHUSDT"}
    ws.tick_symbols = lambda: want
    ws._tick_set_next = 0.0                                            # 相当于过了 1 秒
    feed(ws, ws_msg(sym="ETHUSDT", ev=2_000_000))
    assert ticks == [("ETHUSDT", 100.5, 2_000_000)]
    # tick_symbols=None → 全部币种; tick_tf=None → start() 时取第一个级别
    ws2, _, ticks2 = make_ws()
    feed(ws2, ws_msg(sym="SOLUSDT"))
    assert len(ticks2) == 1


def test_ws_报价回调或集合函数出错不影响收盘通路():
    closed = []

    async def on_closed(symbol, tf, bar):
        closed.append(symbol)

    def boom(symbol, price, ts_ms):
        raise RuntimeError("tick consumer bug")

    def bad_set():
        raise RuntimeError("no set")

    for kw in (dict(on_tick=boom, tick_symbols=lambda: {"BTCUSDT"}),
               dict(on_tick=boom, tick_symbols=bad_set),
               dict(on_tick=boom, tick_symbols=lambda: None)):
        ws = KlineWS("wss://example.invalid", on_closed, tick_tf="5m", **kw)
        feed(ws, ws_msg(), ws_msg(ev=1_005_000), ws_msg(closed=True))
    assert closed == ["BTCUSDT"] * 3
    ws = KlineWS("wss://example.invalid", on_closed, on_tick=boom, tick_symbols=bad_set, tick_tf="5m")
    feed(ws, ws_msg())
    assert ws._tick_set == frozenset()                                 # 集合取不到 → 不放开全部币种


def test_ws_接上提醒引擎_端到端():
    eng, _, _, clock = make(**DROP5)
    out = []
    ws, _, _ = make_ws(on_tick=lambda s, p, t: out.extend(eng.on_tick(s, p, t)),
                       tick_symbols=eng.tick_symbols, tick_tf="5m")
    ev = int(clock.t * 1000)
    feed(ws, ws_msg(c="100", ev=ev), ws_msg(c="99.5", ev=ev + 500), ws_msg(c="98.9", ev=ev + 2000),
         ws_msg(sym="ETHUSDT", c="1", ev=ev + 2000))
    assert len(out) == 1 and out[0].startswith("🔻 <b>急跌</b> BTCUSDT 5分钟 -1.10%")
    assert math.isclose(eng.last_price(SYM), 98.9)


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok", name)
