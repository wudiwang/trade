"""清算热力图(估算)的单测：全部离线，不碰网络（取数部分用假 session，行为照实测到的币安接口造）。"""
import asyncio
import gzip
import json
import random

import pytest

from app.flow import liqmap as m
from app.flow.liqmap import DAY_MS, TF_MS, Grid, LiqPool

# 手算用的两档杠杆：10x 和 50x 各一半，MMR 0.4%
T2 = ((10, 0.5), (50, 0.5))
T0 = 1_794_000_000_000 // (8 * TF_MS) * (8 * TF_MS)      # 对齐到整 2 小时的起点

KEYS = {"symbol", "tf", "bucket", "p0", "rows", "times", "close", "z", "zmax",
        "profile", "price", "clusters", "assumptions", "ts"}


def B(i, o, h, l, c, oi, v=100.0):
    """第 i 根 15m K 线：(开盘时间ms, 开, 高, 低, 收, 量, 收盘时的OI)。"""
    return (T0 + i * TF_MS, o, h, l, c, v, oi)


def synth(n, seed=3, price=60000.0, oi=80000.0):
    """确定性的随机游走 K 线 + OI。"""
    rnd = random.Random(seed)
    bars = []
    for i in range(n):
        o = price
        c = o * (1 + rnd.uniform(-0.004, 0.004))
        h = max(o, c) * (1 + rnd.uniform(0, 0.002))
        l = min(o, c) * (1 - rnd.uniform(0, 0.002))
        oi = max(1000.0, oi + rnd.uniform(-300, 300))
        bars.append((T0 + i * TF_MS, o, h, l, c, 100.0, oi))
        price = c
    return bars


def approx_levels(levels, expected):
    assert len(levels) == len(expected), levels
    for (p, s), (ep, es) in zip(levels, expected):
        assert p == pytest.approx(ep, rel=1e-12)
        assert s == pytest.approx(es, rel=1e-12)


# ───────────────────────── 开仓：位置与数量 ─────────────────────────
def test_强平价公式():
    assert m.liq_price_long(100, 10, 0.004) == pytest.approx(90.4)
    assert m.liq_price_long(100, 100, 0.004) == pytest.approx(99.4)
    assert m.liq_price_short(100, 10, 0.004) == pytest.approx(109.6)
    assert m.liq_price_short(100, 100, 0.004) == pytest.approx(100.6)


def test_单次OI上升_强平位与数量精确():
    """OI 1000→1100：新多 100 + 新空 100（不是各 50），按 10x/50x 各半落在算得出的价位。"""
    pool = LiqPool(T2, 0.004)
    r = pool.step(high=101, low=99, entry=100, oi_prev=1000, oi_now=1100)
    assert r["opened"] == 100
    approx_levels(pool.levels("long"), [(90.4, 50), (98.4, 50)])
    approx_levels(pool.levels("short"), [(101.6, 50), (109.6, 50)])
    assert pool.total("long") == pytest.approx(100)      # 每一侧都是完整的 dOI
    assert pool.total("short") == pytest.approx(100)


def test_默认五档_入场价取典型价():
    """经 simulate 走一遍：入场价 = (高+低+收)/3 = 60000，五档默认权重。"""
    bars = [B(0, 60000, 60000, 60000, 60000, 10000),
            B(1, 60000, 60300, 59700, 60000, 11000)]
    pool, _ = m.simulate(bars)
    approx_levels(pool.levels("long"), [
        (48240, 150), (54240, 300), (57840, 300), (59040, 150), (59640, 100)])
    approx_levels(pool.levels("short"), [
        (60360, 100), (60960, 150), (62160, 300), (65760, 300), (71760, 150)])


def test_第一根只做基准_OI不变不开仓():
    pool, _ = m.simulate([B(0, 100, 101, 99, 100, 1000), B(1, 100, 101, 99, 100, 1000)])
    assert pool.levels("long") == [] and pool.levels("short") == []


# ───────────────────────── 扫单 ─────────────────────────
def _opened_60k():
    pool = LiqPool()
    pool.step(60300, 59700, 60000, 10000, 11000)
    return pool


def test_扫单_只扫最低价触及的多头档():
    """最低 59000：只有 59640(100x) 和 59040(50x) 被扫；57840 及更低的、所有空头档都不动。"""
    pool = _opened_60k()
    r = pool.step(high=60100, low=59000, entry=59500, oi_prev=11000, oi_now=11000)
    assert r["swept_long"] == pytest.approx(250)
    assert r["swept_short"] == 0
    approx_levels(pool.levels("long"), [(48240, 150), (54240, 300), (57840, 300)])
    assert len(pool.levels("short")) == 5
    assert pool.total("short") == pytest.approx(1000)


def test_扫单_最高价扫空头档():
    pool = _opened_60k()
    r = pool.step(high=61000, low=59900, entry=60500, oi_prev=11000, oi_now=11000)
    assert r["swept_short"] == pytest.approx(250)        # 60360(100) + 60960(150)
    assert r["swept_long"] == 0
    approx_levels(pool.levels("short"), [(62160, 300), (65760, 300), (71760, 150)])
    assert len(pool.levels("long")) == 5


def test_扫单_恰好触及算_差一点不算():
    pool = LiqPool(((100, 1.0),), 0.004)
    pool.step(100, 100, 100, 1000, 1100)                 # 多头强平位 99.4，空头 100.6
    lvl = pool.levels("long")[0][0]
    pool.step(high=100, low=lvl + 0.01, entry=100)       # 差 0.01 没碰到
    assert pool.total("long") == pytest.approx(100)
    pool.step(high=100, low=lvl, entry=100)              # 恰好碰到
    assert pool.total("long") == 0
    assert pool.total("short") == pytest.approx(100)     # 另一侧不受影响


def test_当根新开的仓_从下一根起才可能被扫():
    """大阴线自己的最低价(98)低于它新开的 100x 多单强平位(99.4)，但根内先后未知，不在当根扫。"""
    bars = [B(0, 100, 100, 100, 100, 1000),
            B(1, 101, 101, 98, 101, 1100)]               # 典型价 (101+98+101)/3 = 100
    pool, _ = m.simulate(bars, tiers=((100, 1.0),))
    approx_levels(pool.levels("long"), [(99.4, 100)])
    bars.append(B(2, 101, 101.2, 100, 101, 1100))        # 最低 100 > 99.4，仍在
    pool, _ = m.simulate(bars, tiers=((100, 1.0),))
    assert pool.total("long") == pytest.approx(100)
    bars.append(B(3, 101, 101, 99.3, 100, 1100))         # 跌破 99.4，扫掉
    pool, _ = m.simulate(bars, tiers=((100, 1.0),))
    assert pool.total("long") == 0


def test_多头档永远在之后所有最低价的下方():
    """扫单不变量：留在池里的多头档 < 它之后出现过的每一个最低价（空头镜像）。"""
    bars = synth(600)
    pool, _ = m.simulate(bars[:-1])
    last_low, last_high = bars[-1][3], bars[-1][2]
    pool.step(last_high, last_low, 0.0)                  # 最后一根只扫单
    assert all(p < last_low for p, _ in pool.levels("long"))
    assert all(p > last_high for p, _ in pool.levels("short"))


# ───────────────────────── OI 下降：缩池与防重复计算 ─────────────────────────
def test_OI下降_整池等比例缩小():
    """OI 11000→9900 降了 10%（没有扫单）→ 每个档位都剩 90%。"""
    pool = _opened_60k()
    r = pool.step(high=60100, low=59900, entry=60000, oi_prev=11000, oi_now=9900)
    assert r["decay_long"] == pytest.approx(0.9) and r["decay_short"] == pytest.approx(0.9)
    approx_levels(pool.levels("long"), [
        (48240, 135), (54240, 270), (57840, 270), (59040, 135), (59640, 90)])
    assert pool.total("short") == pytest.approx(900)


def test_OI下降_分母是全市场OI_不是池子总量():
    """池子只有 1000，OI 降 1100：若拿池子当分母会清零；按全市场 OI 只缩 10%。"""
    pool = _opened_60k()
    pool.step(60100, 59900, 60000, 11000, 9900)
    assert pool.total("long") == pytest.approx(900)


def test_OI下降_本根已扫掉的量先抵扣():
    """同一根里：多头被扫 250，OI 降 550。
    多头侧只剩 550-250=300 需要解释 → 剩余 750 × (1 - 300/(11000-250))
    空头侧没被扫 → 1000 × (1 - 550/11000)。"""
    pool = _opened_60k()
    r = pool.step(high=60100, low=59000, entry=59500, oi_prev=11000, oi_now=10450)
    assert r["swept_long"] == pytest.approx(250)
    assert pool.total("long") == pytest.approx(750 * (1 - 300 / 10750))
    assert pool.total("short") == pytest.approx(950)


def test_OI下降_扫掉的比降幅多_本侧不再衰减():
    """多头被扫 250，但 OI 只降 200：这 200 已经被清算解释完了，多头侧剩余不再缩。"""
    pool = _opened_60k()
    r = pool.step(high=60100, low=59000, entry=59500, oi_prev=11000, oi_now=10800)
    assert r["decay_long"] == 1.0
    assert pool.total("long") == pytest.approx(750)
    assert pool.total("short") == pytest.approx(1000 * (1 - 200 / 11000))


def test_OI归零_清空():
    pool = _opened_60k()
    pool.step(60100, 59900, 60000, 11000, 0.0)
    assert pool.total("long") == 0 and pool.total("short") == 0
    assert pool.levels("long") == []
    pool.step(60100, 59900, 60000, 5000, 5100)           # 清空后还能正常再开
    assert pool.total("long") == pytest.approx(100)


def test_连续大幅衰减_倍率重整后数值仍正确():
    """连降 10 次 90%：内部倍率会被摊回存储量，总量、每行、后续新增都不能错。"""
    g = Grid(0.0, 10.0, 20)
    pool = LiqPool(((10, 1.0),), 0.004, grid=g)
    pool.step(100, 100, 100, 1000, 1100)                 # 多 90.4 / 空 109.6 各 100
    oi = 1100.0
    for _ in range(10):
        pool.step(100, 100, 100, oi, oi * 0.1)
        oi *= 0.1
    assert pool.total("long") == pytest.approx(100 * 0.1 ** 10, rel=1e-9)
    assert pool.long.scale == 1.0                        # 已重整
    pool.step(100, 100, 100, oi, oi + 50)
    assert pool.total("long") == pytest.approx(50 + 1e-8, rel=1e-9)
    assert pool.column()[g.row(90.4)] == pytest.approx(50 + 1e-8, rel=1e-9)
    assert pool.column()[g.row(109.6)] == pytest.approx(50 + 1e-8, rel=1e-9)


def test_池子总量永远不超过真实OI():
    """由"分母用全市场 OI"推出的不变量，随机数据上逐根检查。"""
    bars = synth(1500, seed=11)
    pool = LiqPool()
    prev = None
    for b in bars:
        pool.step(b[2], b[3], (b[2] + b[3] + b[4]) / 3, prev, b[6])
        prev = b[6]
        assert pool.total("long") <= b[6] + 1e-6
        assert pool.total("short") <= b[6] + 1e-6
    assert pool.total("long") > 0 and pool.total("short") > 0


def test_OI缺失_只扫单_变化并入下一根():
    bars = [B(0, 100, 100, 100, 100, 1000),
            B(1, 100, 100, 100, 100, None),              # 这根没有 OI
            B(2, 100, 103, 97, 100, 1200)]               # 典型价 100，两根的 +200 记在这里
    pool, cols = m.simulate(bars[:2], tiers=T2)
    assert pool.levels("long") == []
    pool, _ = m.simulate(bars, tiers=T2)
    approx_levels(pool.levels("long"), [(90.4, 100), (98.4, 100)])
    approx_levels(pool.levels("short"), [(101.6, 100), (109.6, 100)])


# ───────────────────────── 无前视 ─────────────────────────
def test_无前视_后面加K线不改变前面的列():
    bars = synth(400, seed=5)
    g = m.choose_grid(50000, 70000)
    k = 250
    _, full = m.simulate(bars, grid=g, snap_at=range(len(bars)))
    _, part = m.simulate(bars[:k], grid=g, snap_at=range(k))
    assert full[:k] == part                              # 逐格完全相等


def test_无前视_改最后一根只影响最后一列():
    bars = synth(300, seed=6)
    p1 = m.build_payload(bars, 30, max_cols=1000, now_ms=1)
    assert len(p1["times"]) == 300                       # 每根一列

    last = list(bars[-1])
    last[6] = last[6] + 5000                             # 只改未来的 OI：网格不变
    p2 = m.build_payload(bars[:-1] + [tuple(last)], 30, max_cols=1000, now_ms=1)
    assert (p2["p0"], p2["bucket"], p2["rows"]) == (p1["p0"], p1["bucket"], p1["rows"])
    assert p2["z"][:-1] == p1["z"][:-1]
    assert p2["z"][-1] != p1["z"][-1]

    last = list(bars[-1])
    last[3] = min(b[3] for b in bars)                    # 最后一根砸到窗口最低价：扫掉一批多头档
    p3 = m.build_payload(bars[:-1] + [tuple(last)], 30, max_cols=1000, now_ms=1)
    assert (p3["p0"], p3["bucket"], p3["rows"]) == (p1["p0"], p1["bucket"], p1["rows"])
    assert p3["z"][:-1] == p1["z"][:-1]
    assert sum(p3["z"][-1]) < sum(p1["z"][-1])


# ───────────────────────── 网格、放粗、列规划 ─────────────────────────
def test_网格_行归属与出界():
    g = Grid(100.0, 10.0, 5)
    assert [g.row(p) for p in (100, 109.99, 110, 149.9)] == [0, 0, 1, 4]
    assert g.row(150) == -1 and g.row(99.9) == -1


def test_选网格_行数受限且对齐():
    g = m.choose_grid(55123.4, 93077.7)
    assert g.rows <= m.MAX_ROWS
    assert g.bucket % m.BASE_BUCKET == 0 and g.p0 % g.bucket == 0
    assert g.p0 <= 55123.4 and g.p0 + g.rows * g.bucket >= 93077.7
    assert g.bucket == 200.0                             # 37954/50/220 → 需要放粗 4 倍
    small = m.choose_grid(64000, 66000)
    assert small.bucket == 50.0 and small.rows == 40
    assert m.choose_grid(100, 100).rows >= 1             # 退化输入不炸


def test_放粗网格_总量守恒():
    """同一范围，50 美元一行 vs 200 美元一行：每列总量相同，且粗格 = 对应 4 个细格之和。"""
    bars = synth(500, seed=8)
    fine = Grid(40000.0, 50.0, 800)
    coarse = Grid(40000.0, 200.0, 200)
    snaps = list(range(0, 500, 25)) + [499]
    pool_f, cf = m.simulate(bars, grid=fine, snap_at=snaps)
    pool_c, cc = m.simulate(bars, grid=coarse, snap_at=snaps)
    assert len(cf) == len(cc) == 21
    assert sum(cf[-1]) > 0
    for a, b in zip(cf, cc):
        assert sum(a) == pytest.approx(sum(b), rel=1e-9)
        for i in range(coarse.rows):
            assert b[i] == pytest.approx(sum(a[4 * i:4 * i + 4]), rel=1e-9, abs=1e-9)
    # 范围够大时，图上的总量 = 池子里的总量（一点没丢）
    assert sum(cf[-1]) == pytest.approx(pool_f.total("long") + pool_f.total("short"), rel=1e-9)


def test_逐行累计与从档位重算一致():
    """热力图的行是增量维护的（加/扫/衰减），必须和"把池里所有档位重新落一遍格"相等。"""
    bars = synth(800, seed=9)
    g = m.choose_grid(52000, 68000)
    pool, cols = m.simulate(bars, grid=g, snap_at=[len(bars) - 1])
    for side, rows in zip(("long", "short"), pool.profile()):
        want = [0.0] * g.rows
        for p, s in pool.levels(side):
            if g.row(p) >= 0:
                want[g.row(p)] += s
        assert rows == pytest.approx(want, rel=1e-9, abs=1e-9)
    assert cols[-1] == pytest.approx([a + b for a, b in zip(*pool.profile())], rel=1e-12)


def test_列规划_合并根数与绝对时间对齐():
    times = [T0 + 3 * TF_MS + i * TF_MS for i in range(30 * 96)]      # 故意不从整 2 小时开始
    i0, snaps, step = m._plan_columns(times, 30, 360)
    assert step == 8 and len(snaps) <= 360
    assert times[i0] % (8 * TF_MS) == 0                  # 开头零头丢掉，从整 2 小时起
    for i in snaps[:-1]:                                 # 每列收在整 2 小时边界前的最后一根
        assert (times[i] + TF_MS) % (8 * TF_MS) == 0
    assert snaps[-1] == len(times) - 1                   # 最新一根一定是最后一列
    for days, want in ((3, 1), (7, 2), (14, 4)):
        _, s, st = m._plan_columns(times, days, 360)
        assert st == want and len(s) <= 360


# ───────────────────────── 簇 ─────────────────────────
def test_簇_排序_方向_范围_间隔():
    price = 10000.0                                      # 1% = 100，间隔 2% = 200，范围 ±8% = 9200~10800
    longs = [(9500, 30), (9530, 10),                     # 同一条价带 → 一簇 40，均价 9507.5
             (9650, 35),                                 # 离上一簇不到 2% → 被划掉，不单独成簇
             (9280, 20),                                 # 够远 → 第二簇
             (9950, 5),                                  # 第三簇
             (9100, 999)]                                # 超出 -8% → 不看
    shorts = [(10100, 7), (10400, 20), (10410, 3),
              (10900, 500),                              # 超出 +8%
              (10000, 50),                               # 正好在现价上，不算"上方"
              (9990, 60)]                                # 在现价下方的空头档（马上会被扫），不算
    below = m.find_clusters(longs, price, "below", fine_bucket=10)
    above = m.find_clusters(shorts, price, "above", fine_bucket=10)

    assert [(c["price"], c["size"]) for c in below] == [(9507.5, 40.0), (9280.0, 20.0), (9950.0, 5.0)]
    assert [c["dist_pct"] for c in below] == pytest.approx([-4.925, -7.2, -0.5], abs=0.006)
    assert [(c["price"], c["size"]) for c in above] == [(10401.3, 23.0), (10100.0, 7.0)]
    assert [c["dist_pct"] for c in above] == pytest.approx([4.01, 1.0], abs=0.006)

    for side in (below, above):                          # 按数量从大到小
        assert [c["size"] for c in side] == sorted((c["size"] for c in side), reverse=True)
    assert all(c["dist_pct"] < 0 for c in below) and all(c["dist_pct"] > 0 for c in above)


def test_簇_最多三个_空输入():
    lv = [(9950 - 220 * i, 10 + i) for i in range(6)]    # 6 堆，彼此相隔 2.2%
    out = m.find_clusters(lv, 10000.0, "below", fine_bucket=10)
    assert len(out) == 3                                 # 9070、8850 出界，范围内 4 堆取最大的三个
    assert [(c["price"], c["size"]) for c in out] == [(9290.0, 13.0), (9510.0, 12.0), (9730.0, 11.0)]
    assert m.find_clusters([], 10000.0, "below") == []
    assert m.find_clusters(lv, 0.0, "below") == []
    assert m.find_clusters([(9900, 0.01)], 10000.0, "below") == []   # 小到可以忽略


# ───────────────────────── payload ─────────────────────────
def test_payload_小例子逐格核对():
    bars = [B(0, 100, 100, 100, 100, 1000),
            B(1, 100, 101, 99, 100, 1100),               # 开仓：多 90.4/98.4，空 101.6/109.6，各 50
            B(2, 100, 101, 98, 100, 1100)]               # 最低 98 扫掉 98.4；最高 101 没碰到 101.6
    p = m.build_payload(bars, 7, tiers=T2, base_bucket=1.0, margin_pct=12.0, now_ms=123)
    assert set(p) == KEYS
    assert (p["symbol"], p["tf"], p["bucket"], p["ts"], p["price"]) == ("BTCUSDT", "15m", 1.0, 123, 100.0)
    assert p["times"] == [b[0] for b in bars] and p["close"] == [100.0, 100.0, 100.0]
    assert p["p0"] == 86.0 and p["rows"] == 28           # 98×0.88=86.24 → 86；101×1.12=113.12 → 28 行

    def row(price):
        return int((price - p["p0"]) // p["bucket"])

    def cells(col):
        return {i: v for i, v in enumerate(col) if v}

    assert cells(p["z"][0]) == {}
    assert cells(p["z"][1]) == {row(90.4): 50, row(98.4): 50, row(101.6): 50, row(109.6): 50}
    assert cells(p["z"][2]) == {row(90.4): 50, row(101.6): 50, row(109.6): 50}
    assert cells(p["profile"]["long"]) == {row(90.4): 50}
    assert cells(p["profile"]["short"]) == {row(101.6): 50, row(109.6): 50}
    assert p["zmax"] == 50.0
    assert p["clusters"]["below"] == []                  # 90.4 在 -8% 之外
    assert p["clusters"]["above"] == [{"price": 101.6, "size": 50.0, "dist_pct": 1.6}]
    assert p["assumptions"]["tiers"] == [[10, 0.5], [50, 0.5]] and p["assumptions"]["mmr"] == 0.004
    assert "估算" in p["assumptions"]["note"] and "不是交易所数据" in p["assumptions"]["note"]


def test_payload_30天形状与自洽():
    bars = synth(30 * 96 + 1, seed=21)
    p = m.build_payload(bars, 30)
    assert set(p) == KEYS
    T = len(p["times"])
    assert 0 < T <= m.MAX_COLS and 0 < p["rows"] <= m.MAX_ROWS
    assert len(p["close"]) == T and len(p["z"]) == T
    assert all(len(col) == p["rows"] for col in p["z"])
    assert p["times"] == sorted(p["times"]) and p["times"][-1] == bars[-1][0]
    assert p["close"][-1] == p["price"] == bars[-1][4]
    assert len(p["profile"]["long"]) == len(p["profile"]["short"]) == p["rows"]
    assert all(v >= 0 for col in p["z"] for v in col)
    assert 0 < p["zmax"] <= max(v for col in p["z"] for v in col)
    # 最后一列 = 当前多头档 + 空头档（各自取整过，允许每格 0.6 的误差）
    for z, a, b in zip(p["z"][-1], p["profile"]["long"], p["profile"]["short"]):
        assert z == pytest.approx(a + b, abs=0.6)
    # 多头档在现价下方、空头档在上方
    price_row = int((p["price"] - p["p0"]) // p["bucket"])
    assert sum(p["profile"]["long"][price_row + 1:]) == 0
    assert sum(p["profile"]["short"][:price_row]) == 0
    for c in p["clusters"]["above"]:
        assert 0 < c["dist_pct"] <= 8.0 and c["price"] > p["price"]
    for c in p["clusters"]["below"]:
        assert -8.0 <= c["dist_pct"] < 0 and c["price"] < p["price"]
    assert len(p["clusters"]["above"]) <= 3 and len(p["clusters"]["below"]) <= 3
    json.dumps(p)                                        # 可序列化


def test_payload_天数只改时间范围_不改当前强平位():
    bars = synth(10 * 96, seed=22)
    p3, p7 = m.build_payload(bars, 3), m.build_payload(bars, 7)
    assert len(p3["times"]) == 3 * 96 and p3["times"][0] == bars[-3 * 96][0]
    assert len(p7["times"]) == 336
    assert p3["clusters"] == p7["clusters"] and p3["price"] == p7["price"]
    # 只给最后 3 天的数据（没有预热）结果就不同了：说明前面的 7 天确实参与了累积
    assert m.build_payload(bars[-3 * 96:], 3)["clusters"] != p3["clusters"]


def test_payload_空与过短输入():
    for bars in ([], [B(0, 100, 101, 99, 100, 1000)], [(1, 2, 3)]):
        p = m.build_payload(bars, 7)
        assert set(p) == KEYS
        assert p["times"] == [] and p["close"] == [] and p["z"] == [] and p["rows"] == 0
        assert p["profile"] == {"long": [], "short": []}
        assert p["clusters"] == {"above": [], "below": []}
        assert p["zmax"] == 0.0 and p["assumptions"]["tiers"]
        json.dumps(p)


def test_payload_完全没有OI_也合法():
    bars = [B(i, 100, 101, 99, 100, None) for i in range(50)]
    p = m.build_payload(bars, 7)
    assert len(p["times"]) == 50 and p["rows"] > 0 and p["zmax"] == 0.0
    assert all(v == 0 for col in p["z"] for v in col)
    assert p["clusters"] == {"above": [], "below": []}


def test_payload_乱序和坏K线被清理():
    good = synth(100, seed=23)
    messy = list(reversed(good)) + [(T0 + 999 * TF_MS, 0, 0, 0, 0, 0, None)]
    assert m.build_payload(messy, 7, now_ms=5) == m.build_payload(good, 7, now_ms=5)


# ───────────────────────── K线与OI对齐 ─────────────────────────
def test_merge_bars_OI取K线收盘时刻的快照():
    kl = [(T0, 1, 2, 0.5, 1.5, 10), (T0 + TF_MS, 1.5, 3, 1, 2, 20), (T0 + 2 * TF_MS, 2, 2.5, 1.5, 2.2, 5)]
    oi = {T0: 900.0, T0 + TF_MS: 1000.0}                 # T0+2*TF 的快照缺失；T0+3*TF 还没到
    bars = m.merge_bars(kl, oi, oi_now=1234.0)
    assert bars[0] == (T0, 1.0, 2.0, 0.5, 1.5, 10.0, 1000.0)     # 收盘 = T0+15m 的快照
    assert bars[1][6] is None                            # 缺失就是 None，不拿别的顶
    assert bars[2][6] == 1234.0                          # 最后一根(未收盘)用实时 OI
    assert m.merge_bars(kl, oi)[2][6] is None
    assert m.merge_bars([], {}) == []


# ───────────────────────── 取数与缓存（假 session，不触网） ─────────────────────────
NOW = (T0 // TF_MS + 5000) * TF_MS + 7 * 60 * 1000      # 某根 K 线开盘后 7 分钟


class _Resp:
    def __init__(self, status, data, headers=None):
        self.status, self._data, self.headers = status, data, headers or {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def json(self):
        return self._data

    async def text(self):
        return json.dumps(self._data)


class FakeBinance:
    """照 2026-11-16 实测到的行为造的假币安：
    - K 线：从 startTime 起升序，最多 limit 根，含未收盘的最后一根；
    - OI 历史：【无视 startTime】，返回 endTime(缺省=现在) 之前最新的 limit 条；
      只保留 31 天，endTime 更早直接 400(-1130)；
    - 实时 OI：一个数。"""

    def __init__(self, now_ms, seed=1):
        self.now = now_ms
        self.calls = []
        self.fail = None            # None / 429 / "boom"
        self.weight = 5
        self.keep_ms = 31 * DAY_MS  # OI 历史的保留期
        self.oi_down = False        # True = 只有 OI 历史接口挂了
        rnd = random.Random(seed)
        t_end = now_ms // TF_MS * TF_MS + 96 * TF_MS     # 多造一天，方便把时间往后拨
        self.kl, self.oi = {}, {}
        price, oi = 60000.0, 80000.0
        for t in range(t_end - 41 * 96 * TF_MS, t_end + 1, TF_MS):
            self.oi[t] = round(oi, 3)
            o = price
            c = round(o * (1 + rnd.uniform(-0.004, 0.004)), 1)
            h = round(max(o, c) * (1 + rnd.uniform(0, 0.002)), 1)
            l = round(min(o, c) * (1 - rnd.uniform(0, 0.002)), 1)
            self.kl[t] = [t, f"{o:.1f}", f"{h:.1f}", f"{l:.1f}", f"{c:.1f}", "100.000",
                          t + TF_MS - 1, "6000000.0", 100, "50.000", "3000000.0", "0"]
            price = c
            oi = max(1000.0, oi + rnd.uniform(-300, 300))

    @property
    def live_oi(self):
        return self.oi[self.now // TF_MS * TF_MS] + 12.5

    def get(self, url, params=None, timeout=None):
        path = url.split("binance.com", 1)[-1]
        p = dict(params or {})
        self.calls.append((path, p))
        if self.fail == "boom":
            raise OSError("网络断了")
        if self.fail == 429:
            return _Resp(429, {"code": -1003}, {"Retry-After": "120"})
        if path == "/fapi/v1/klines":
            ts = [t for t in sorted(self.kl) if int(p["startTime"]) <= t <= self.now]
            return _Resp(200, [self.kl[t] for t in ts[:int(p["limit"])]],
                         {"X-MBX-USED-WEIGHT-1M": str(self.weight)})
        if path == "/futures/data/openInterestHist":
            if self.oi_down:
                return _Resp(503, {"msg": "service unavailable"})
            end = int(p.get("endTime", self.now))
            keep_from = self.now - self.keep_ms
            if end < keep_from:
                return _Resp(400, {"code": -1130, "msg": "parameter 'endTime' is invalid."})
            ts = [t for t in sorted(self.oi) if keep_from <= t <= min(end, self.now)]
            return _Resp(200, [{"symbol": "BTCUSDT", "sumOpenInterest": f"{self.oi[t]:.8f}",
                                "sumOpenInterestValue": "0", "timestamp": t}
                               for t in ts[-int(p["limit"]):]])
        if path == "/fapi/v1/openInterest":
            return _Resp(200, {"symbol": "BTCUSDT", "openInterest": f"{self.live_oi:.3f}",
                               "time": self.now})
        return _Resp(404, {})

    def expected_bars(self, days=m.HISTORY_DAYS):
        """绕过取数层，直接从假数据拼出应得的 bars。"""
        start = (self.now - days * DAY_MS) // TF_MS * TF_MS
        kl = [(t, *(float(x) for x in self.kl[t][1:6]))
              for t in sorted(self.kl) if start <= t <= self.now]
        oi = {t: v for t, v in self.oi.items() if t <= self.now}
        return m.merge_bars(kl, oi, self.live_oi)


@pytest.fixture
def fresh():
    m._reset_cache()
    yield
    m._reset_cache()


def run(coro):
    return asyncio.run(coro)


def test_未刷新时_getter返回合法空payload(fresh):
    p = m.get_cached(7)
    assert set(p) == KEYS and p["rows"] == 0 and p["times"] == [] and p["z"] == []
    assert json.loads(m.get_cached_json(7)) == p
    assert json.loads(gzip.decompress(m.get_cached_json(7, gz=True))) == p
    assert m.get_cached(99) == p and m.get_cached("abc") == p     # 不支持的天数按默认处理
    assert m.last_updated_ms() == 0


def test_分页_K线与OI都取满30天(fresh):
    fake = FakeBinance(NOW)
    start = (NOW - 30 * DAY_MS) // TF_MS * TF_MS
    kl = run(m.fetch_klines(fake, start, NOW))
    assert len(kl) == 30 * 96 + 1 and kl[0][0] == start and kl[-1][0] == NOW // TF_MS * TF_MS
    assert [k[0] for k in kl] == sorted(k[0] for k in kl)
    assert len(fake.calls) == 3                          # 1000 + 1000 + 881

    fake.calls.clear()
    oi = run(m.fetch_oi_hist(fake, start, NOW))
    assert min(oi) <= start and max(oi) == NOW // TF_MS * TF_MS
    assert all(t in oi for t in range(start, NOW, TF_MS))        # 中间一条不缺
    assert len(fake.calls) == 6                          # 500 条一页，从新往旧翻
    assert all(c[1]["limit"] <= 500 for c in fake.calls)


def test_分页_OI翻到保留期尽头不算失败(fresh):
    fake = FakeBinance(NOW)
    oi = run(m.fetch_oi_hist(fake, NOW - 40 * DAY_MS, NOW))       # 想要 40 天，只有 31 天
    assert len(oi) == 31 * 96 and len(fake.calls) == 6           # 最后一页不满 → 自然结束

    # 保留期里恰好 3000 条 = 6 页全满 → 第 7 页的 endTime 早于保留期，币安回 400
    fake = FakeBinance(NOW)
    fake.keep_ms = 2999 * TF_MS + 7 * 60 * 1000          # 最老的一条正好压在保留期边界上
    oi = run(m.fetch_oi_hist(fake, NOW - 40 * DAY_MS, NOW))
    assert len(oi) == 3000 and len(fake.calls) == 7              # 400 被当作"到头了"，不抛异常
    assert fake.calls[-1][1]["endTime"] < NOW - fake.keep_ms

    # 但第一页就 400（手里什么都没有）必须报错，不能装作成功
    fake = FakeBinance(NOW)
    fake.keep_ms = -1
    with pytest.raises(RuntimeError):
        run(m.fetch_oi_hist(fake, NOW - DAY_MS, NOW))


def test_refresh_全量加载_结果与直接计算一致(fresh):
    fake = FakeBinance(NOW)
    assert run(m.refresh(fake, now_ms=NOW, in_thread=False)) is True
    assert len(fake.calls) == 10                         # K线3 + OI6 + 实时OI1
    st = m.status()
    assert st["bars"] == 30 * 96 + 1 and st["last_error"] is None
    assert st["cached_days"] == [3, 7, 14, 30] and m.last_updated_ms() == NOW

    bars = fake.expected_bars()
    assert bars[-1][6] == pytest.approx(fake.live_oi)    # 未收盘那根用的是实时 OI
    for d in m.SUPPORTED_DAYS:
        p = m.get_cached(d)
        assert p == m.build_payload(bars, d, now_ms=NOW)
        assert p["rows"] > 0 and p["zmax"] > 0 and p["ts"] == NOW
        assert json.loads(m.get_cached_json(d)) == p
        assert gzip.decompress(m.get_cached_json(d, gz=True)) == m.get_cached_json(d)
        assert st["bytes"][d]["gzip"] < st["bytes"][d]["json"]


def test_refresh_在线程里算也一样(fresh):
    fake = FakeBinance(NOW)
    assert run(m.refresh(fake, 7, now_ms=NOW)) is True   # 默认 in_thread=True
    assert m.status()["cached_days"] == [7]              # 只算了要的那个
    assert m.get_cached(7) == m.build_payload(fake.expected_bars(), 7, now_ms=NOW)
    assert m.get_cached(30)["rows"] == 0                 # 没算过的仍是空 payload


def test_refresh_逐个天数调用只取一次数(fresh):
    fake = FakeBinance(NOW)

    async def go():
        for i, d in enumerate(m.SUPPORTED_DAYS):
            assert await m.refresh(fake, d, now_ms=NOW + i * 1000, in_thread=False) is True
    run(go())
    assert len(fake.calls) == 10                         # 后三次调用没有再发任何请求
    assert m.status()["cached_days"] == [3, 7, 14, 30]


def test_refresh_增量只补尾巴(fresh):
    fake = FakeBinance(NOW)
    run(m.refresh(fake, now_ms=NOW, in_thread=False))
    old_last = m.get_cached(3)["times"][-1]
    fake.calls.clear()
    fake.now = NOW + 25 * 60 * 1000                      # 25 分钟后：跨过两个 15 分钟边界
    assert run(m.refresh(fake, now_ms=fake.now, in_thread=False)) is True
    assert [c[0] for c in fake.calls] == ["/fapi/v1/klines", "/futures/data/openInterestHist",
                                          "/fapi/v1/openInterest"]
    assert all(c[1]["limit"] <= 99 for c in fake.calls[:2])       # 小请求，K线权重 1
    p = m.get_cached(3)
    assert p["times"][-1] == fake.now // TF_MS * TF_MS == old_last + 2 * TF_MS
    assert p["ts"] == fake.now
    # 增量拼出来的数据 = 全量重拉的数据（多保留的最老那一根除外）
    kl = m._store["klines"]
    got = m.merge_bars([kl[t] for t in sorted(kl)], m._store["oi"], m._store["oi_now"])
    want = fake.expected_bars()
    assert got[-len(want):] == want


def test_refresh_失败时保留旧缓存且不抛异常(fresh):
    fake = FakeBinance(NOW)
    run(m.refresh(fake, now_ms=NOW, in_thread=False))
    old = m.get_cached(7)
    fake.fail = "boom"
    later = NOW + 5 * 60 * 1000
    assert run(m.refresh(fake, now_ms=later, in_thread=False)) is False
    assert m.get_cached(7) is old and old["ts"] == NOW
    assert "网络断了" in m.status()["last_error"]
    fake.fail = None                                     # 恢复后下一轮自动好
    assert run(m.refresh(fake, now_ms=later + 61_000, in_thread=False)) is True
    assert m.status()["last_error"] is None and m.get_cached(7)["ts"] == later + 61_000


def test_refresh_只有OI接口挂了_新K线的扫单照样更新(fresh):
    fake = FakeBinance(NOW)
    run(m.refresh(fake, now_ms=NOW, in_thread=False))
    old = m.get_cached(3)
    fake.oi_down = True
    fake.now = NOW + 16 * 60 * 1000                      # 新开了一根 K 线
    assert run(m.refresh(fake, now_ms=fake.now, in_thread=False)) is False
    assert "503" in m.status()["last_error"]
    p = m.get_cached(3)
    assert p["ts"] == fake.now and p["times"][-1] == old["times"][-1] + TF_MS
    # 缺 OI 的那几根只扫单；OI 恢复后补齐，结果与全量重拉一致
    fake.oi_down = False
    fake.now += 61_000
    assert run(m.refresh(fake, now_ms=fake.now, in_thread=False)) is True
    kl = m._store["klines"]
    got = m.merge_bars([kl[t] for t in sorted(kl)], m._store["oi"], m._store["oi_now"])
    want = fake.expected_bars()
    assert got[-len(want):] == want


def test_refresh_首次就失败_仍给空payload(fresh):
    fake = FakeBinance(NOW)
    fake.fail = "boom"
    assert run(m.refresh(fake, now_ms=NOW, in_thread=False)) is False
    assert m.get_cached(7)["rows"] == 0 and m.status()["cached_days"] == []


def test_refresh_被限流后静默到期才再请求(fresh):
    fake = FakeBinance(NOW)
    fake.fail = 429
    assert run(m.refresh(fake, now_ms=NOW, in_thread=False)) is False
    assert len(fake.calls) == 1                          # 第一个请求挨了 429 就停
    assert m.status()["backoff_until_ms"] == NOW + 120_000       # 按 Retry-After
    fake.fail = None
    fake.calls.clear()
    assert run(m.refresh(fake, now_ms=NOW + 70_000, in_thread=False)) is False
    assert fake.calls == []                              # 静默期内一个请求都不发
    assert run(m.refresh(fake, now_ms=NOW + 121_000, in_thread=False)) is True
    assert len(fake.calls) == 10


def test_权重保护_已用权重过高就停手(fresh):
    fake = FakeBinance(NOW)
    fake.weight = m.WEIGHT_GUARD + 1                     # 实盘引擎那边已经用了很多额度
    assert run(m.refresh(fake, now_ms=NOW, in_thread=False)) is False
    assert len(fake.calls) == 1                          # 看到响应头后不再发第二个请求
    assert "限流" in m.status()["last_error"]
    assert m.status()["backoff_until_ms"] >= NOW + 60_000
