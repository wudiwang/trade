"""深度记录器 / 热力图构建的单测（全部离线，不碰交易所）。"""
import asyncio
import gzip
import json
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from app.flow import depth as D
from app.flow.depth import (
    ALL_SOURCES, Book, DepthRecorder, DepthStore, RateLimited, RawBook, SourceError,
    bucket_book, bucket_index, build_heatmap, check_mids, choose_grid, make_snapshot,
    merge_snapshots, parse_binance, parse_bybit, parse_okx, parse_okx_ct_val,
)

T0 = 1_800_000_000_000          # ms, 恰是 30s/60s/300s 的整数倍
CONTRACT_KEYS = {"symbol", "bucket", "p0", "rows", "times", "mid", "z", "zmax", "sources",
                 "coverage", "latest", "recorded_minutes", "health"}


# ---------------------------------------------------------------- 造数据
def mk_book(mid=65000.0, levels=((10.0, 1.0), (100.0, 2.0), (600.0, 5.0)), source="binance") -> Book:
    """对称盘口: levels = [(离中间价的美元距离, 数量)], 另加紧贴中间价的买一卖一(各 0.5)。"""
    bids = [(mid - 0.5, 0.5)] + [(mid - d, q) for d, q in levels]
    asks = [(mid + 0.5, 0.5)] + [(mid + d, q) for d, q in levels]
    return Book(source, bids, asks)


def mk_snap(ts, books: dict, bucket=5.0):
    return make_snapshot(ts, {n: bucket_book(b, bucket) for n, b in books.items()}, bucket)


def row_of(payload, price) -> int:
    return int((price - payload["p0"]) // payload["bucket"])


# ================================================================ 解析与单位换算
def test_解析_币安_字符串转数值():
    b = parse_binance({"lastUpdateId": 1, "bids": [["64999.9", "1.250"]], "asks": [["65000.1", "0.003"]]})
    assert b.source == "binance"
    assert b.bids == [(64999.9, 1.25)] and b.asks == [(65000.1, 0.003)]
    assert parse_binance({"bids": [], "asks": []}, "binance_spot").source == "binance_spot"
    with pytest.raises(SourceError):
        parse_binance({"code": -1121, "msg": "Invalid symbol."})


def test_解析_bybit_错误码要报错():
    ok = {"retCode": 0, "result": {"b": [["64901.30", "0.117"]], "a": [["64901.40", "10.159"]]}}
    b = parse_bybit(ok)
    assert b.bids == [(64901.3, 0.117)] and b.asks == [(64901.4, 10.159)]
    with pytest.raises(SourceError):
        parse_bybit({"retCode": 10001, "retMsg": "params error"})


def test_解析_okx_张数换算成BTC():
    """OKX 盘口单位是「张」, 1 张 = ct_val BTC。305.35 张 × 0.01 = 3.0535 BTC。"""
    obj = {"code": "0", "data": [{"bids": [["64903.8", "305.35", "37"]],
                                  "asks": [["64903.9", "281.76", "20"]], "ts": "1"}]}
    b = parse_okx(obj, 0.01)
    assert b.bids[0][0] == 64903.8 and b.bids[0][1] == pytest.approx(3.0535)
    assert b.asks[0][1] == pytest.approx(2.8176)
    assert parse_okx(obj, 0.1).bids[0][1] == pytest.approx(30.535)      # 面值变了, 结果跟着变
    with pytest.raises(SourceError):
        parse_okx(obj, 0.0)                                             # 面值未知不许猜
    with pytest.raises(SourceError):
        parse_okx({"code": "50011", "msg": "Too Many Requests", "data": []}, 0.01)


def test_解析_okx_合约面值():
    inst = {"code": "0", "data": [{"instId": "BTC-USDT-SWAP", "ctVal": "0.01", "ctMult": "1",
                                   "ctValCcy": "BTC", "ctType": "linear"}]}
    assert parse_okx_ct_val(inst, "BTC-USDT-SWAP", "BTC") == pytest.approx(0.01)
    inst["data"][0]["ctMult"] = "10"
    assert parse_okx_ct_val(inst, "BTC-USDT-SWAP", "BTC") == pytest.approx(0.1)
    with pytest.raises(SourceError):          # 反向合约(面值是美元)不能当 BTC 用
        parse_okx_ct_val({"code": "0", "data": [{"instId": "BTC-USD-SWAP", "ctVal": "100",
                                                 "ctValCcy": "USD"}]}, "BTC-USD-SWAP", "BTC")
    with pytest.raises(SourceError):
        parse_okx_ct_val(inst, "ETH-USDT-SWAP", "ETH")
    assert D.okx_inst_id("BTCUSDT") == "BTC-USDT-SWAP"
    with pytest.raises(ValueError):
        D.okx_inst_id("BTCUSD")


# ================================================================ 分桶
def test_分桶_边界归属():
    """第 i 桶覆盖 [i*b, (i+1)*b): 恰在边界上的价格归上面那个桶。"""
    assert bucket_index(64995.0, 5.0) == 12999
    assert bucket_index(64999.99, 5.0) == 12999
    assert bucket_index(65000.0, 5.0) == 13000
    assert bucket_index(65004.9, 5.0) == 13000
    assert bucket_index(0.3, 0.1) == 3            # 0.3/0.1 = 2.9999999999999996, 不能掉到 2
    assert bucket_index(0.7, 0.1) == 7


def test_分桶_买卖分开_同桶累加_中间补零():
    book = Book("binance",
                bids=[(64999.5, 1.0), (64996.0, 2.0), (64995.0, 0.5), (64980.0, 4.0)],
                asks=[(65000.5, 3.0), (65004.9, 1.0), (65005.0, 7.0)])
    d = bucket_book(book, 5.0)
    assert d.mid == 65000.0
    # 买: 64980→桶12996, 64995/64996/64999.5→桶12999; 中间 12997、12998 补 0
    assert d.bid_i0 == 12996 and d.bids == pytest.approx([4.0, 0.0, 0.0, 3.5])
    # 卖: 65000.5 与 65004.9 同在桶 13000; 65005.0 恰在边界 → 桶 13001
    assert d.ask_i0 == 13000 and d.asks == pytest.approx([4.0, 7.0])
    assert d.reach_bid == pytest.approx(20.0 / 65000 * 100)
    assert d.reach_ask == pytest.approx(5.0 / 65000 * 100)


def test_分桶_半幅裁剪与脏数据():
    mid = 65000.0
    book = mk_book(mid, levels=((1600.0, 9.0), (1700.0, 99.0)))      # ±2.5% = ±1625
    d = bucket_book(book, 5.0, half_range_pct=2.5)
    assert sum(d.bids) == pytest.approx(9.5) and sum(d.asks) == pytest.approx(9.5)   # 1700 外的 99 被裁掉
    assert d.reach_bid <= 2.5 and d.reach_ask <= 2.5
    wide = bucket_book(book, 5.0, half_range_pct=5.0)
    assert sum(wide.bids) == pytest.approx(108.5)
    # 数量为 0 的档位忽略
    z = bucket_book(Book("x", [(64999.5, 1.0), (64000.0, 0.0)], [(65000.5, 1.0)]), 5.0)
    assert z.bid_i0 == 12999 and z.bids == [1.0]
    with pytest.raises(SourceError):
        bucket_book(Book("x", [], [(65000.5, 1.0)]), 5.0)
    with pytest.raises(SourceError):          # 买一 ≥ 卖一
        bucket_book(Book("x", [(65001.0, 1.0)], [(65000.5, 1.0)]), 5.0)


def test_中间价互校():
    assert check_mids({"binance": 65000, "bybit": 65010, "okx": 64990}) == {}
    bad = check_mids({"binance": 65000, "bybit": 65010, "okx": 6500.0})     # 少了一位 → 丢弃
    assert list(bad) == ["okx"]
    assert check_mids({"binance": 65000}) == {}


# ================================================================ 快照 / 卷积
def test_快照_量化与读回():
    s = mk_snap(T0, {"binance": mk_book(levels=((10.0, 1.234), (100.0, 2.0)))})
    assert s.n == 1 and s.mid == 65000.0 and s.bucket == 5.0 and s.unit == 100
    i0, qs = s.side_btc("binance", "bids")
    m = s.sources["binance"]
    assert m["b"] == [i0, len(qs)] and m["n"] == 1
    assert sum(qs) == pytest.approx(0.5 + 1.23 + 2.0)        # 1.234 量化到 0.01 BTC
    assert all(isinstance(q, int) for q in s.body()["binance"][0])


def test_卷积_各源只在在场快照上求均值_范围取并集():
    a = mk_snap(T0, {"binance": mk_book(65000, ((10.0, 10.0),)), "bybit": mk_book(65000, ((10.0, 4.0),))})
    b = mk_snap(T0 + 30_000, {"binance": mk_book(65000, ((10.0, 30.0), (50.0, 8.0)))})    # bybit 这轮超时
    m = merge_snapshots([a, b], T0)
    assert m.ts == T0 and m.n == 2 and m.t == T0 + 15_000
    assert m.sources["binance"]["n"] == 2 and m.sources["bybit"]["n"] == 1
    i0, qs = m.side_btc("binance", "bids")
    got = {i0 + j: q for j, q in enumerate(qs) if q}
    assert got[bucket_index(64990.0, 5.0)] == pytest.approx(20.0)         # (10+30)/2
    assert got[bucket_index(64950.0, 5.0)] == pytest.approx(4.0)          # 只在 b 里有 → (0+8)/2
    _, bq = m.side_btc("bybit", "bids")
    assert sum(bq) == pytest.approx(4.5)                                  # bybit 没被缺席的那轮摊薄
    assert merge_snapshots([], T0) is None


# ================================================================ 存储
def test_存储_写入读回与只读不建库(tmp_path):
    path = tmp_path / "sub" / "depth.sqlite"
    st = DepthStore(path)
    # 按需创建: 只读操作不会顺手建库
    assert st.range(0) == [] and st.latest() is None and st.count() == 0 and st.first_ts() is None
    assert st.overview() == (None, None, None) and st.read_window(24) == [] and st.last_ts() is None
    assert st.prune(1) == 0 and st.rollup_pending(T0) == 0
    assert not path.exists()

    snaps = [mk_snap(T0 + i * 30_000, {"binance": mk_book(65000 + i), "okx": mk_book(65001 + i, source="okx")})
             for i in range(5)]
    sizes = [st.write(s) for s in snaps]
    assert path.exists() and all(0 < n < 2000 for n in sizes)
    assert st.count() == 5 and st.first_ts() == T0 and st.last_ts() == T0 + 120_000
    got = st.range(T0 + 30_000, T0 + 90_000)
    assert [g.ts for g in got] == [T0 + 30_000, T0 + 60_000, T0 + 90_000]
    assert got[0].meta == snaps[1].meta and got[0].body() == snaps[1].body()
    assert got[0].mid == snaps[1].mid and got[0].n == 1
    assert st.latest().ts == T0 + 120_000
    last, first, last_ts = st.overview()
    assert (last.ts, first, last_ts) == (T0 + 120_000, T0, T0 + 120_000)
    # 同 ts 覆盖而不是重复
    st.write(mk_snap(T0, {"binance": mk_book(70000)}))
    assert st.count() == 5 and st.range(T0, T0)[0].mid == 70000.0
    c = sqlite3.connect(path)
    assert c.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    assert c.execute("PRAGMA page_size").fetchone()[0] == DepthStore.PAGE_SIZE
    c.close()
    assert st.db_bytes() > 0


def test_存储_清理过期(tmp_path):
    st = DepthStore(tmp_path / "d.sqlite")
    day = 86400_000
    now = T0 + 10 * day
    for age_days in (9.5, 7.5, 3.0, 1.0, 0.1):
        ts = now - int(age_days * day)
        st.write(mk_snap(ts, {"binance": mk_book()}))
        st.write(merge_snapshots([mk_snap(ts, {"binance": mk_book()})], ts), "m5")
    removed = st.prune(keep_days=8, keep_raw_days=2, now_ms=now)
    assert removed == 3 + 1                       # 原始删 9.5/7.5/3.0 天的, 均值只删 9.5 天的
    assert st.count("raw") == 2 and st.count("m5") == 4
    assert st.first_ts() == now - int(7.5 * day)
    assert st.prune(keep_days=8, keep_raw_days=2, now_ms=now) == 0


def test_存储_多线程并发读写(tmp_path):
    """事件循环线程与 FastAPI 线程池会同时访问: 每次操作独立短连接, 不应丢写、不应抛。"""
    st = DepthStore(tmp_path / "d.sqlite")
    snap_books = {"binance": mk_book()}

    def writer(k):
        for i in range(25):
            st.write(mk_snap(T0 + (k * 25 + i) * 1000, snap_books))
        return True

    def reader(_):
        n = 0
        for _i in range(25):
            n = max(n, len(st.range(0)))
            st.latest()
        return n

    with ThreadPoolExecutor(8) as ex:
        res = list(ex.map(writer, range(4))) + list(ex.map(reader, range(4)))
    assert all(res[:4])
    assert st.count() == 100


def test_存储_五分钟卷积与长窗口读取(tmp_path):
    st = DepthStore(tmp_path / "d.sqlite")
    # 两个完整窗口(各 10 张) + 第三个窗口里 3 张
    for i in range(23):
        st.write(mk_snap(T0 + i * 30_000, {"binance": mk_book(65000 + i, ((10.0, float(i + 1)),))}))
    now = T0 + 22 * 30_000 + 1000
    assert st.rollup_pending(now) == 2            # 当前窗口不卷
    assert st.rollup_pending(now) == 0            # 幂等
    m5 = st.range(0, tier="m5")
    assert [r.ts for r in m5] == [T0, T0 + 300_000] and [r.n for r in m5] == [10, 10]
    assert m5[0].t == T0 + 135_000                # 成员平均时间
    assert m5[0].mid == pytest.approx(65004.5)
    # <24h: 原始快照; ≥24h: 均值行 + 尚未卷积的原始尾巴
    assert len(st.read_window(1, now)) == 23
    long = st.read_window(24, now)
    assert [r.n for r in long] == [10, 10, 1, 1, 1]
    assert [r.t for r in long] == sorted(r.t for r in long)
    # 进程重启(新对象)后还能接着卷
    for i in range(23, 31):
        st.write(mk_snap(T0 + i * 30_000, {"binance": mk_book()}))
    assert DepthStore(st.path).rollup_pending(T0 + 31 * 30_000) == 1
    assert st.count("m5") == 3


# ================================================================ 折叠 / 网格
def test_折叠_裁剪与对齐():
    acc = [0.0] * 4
    D._fold(acc, 100, [1, 2, 3, 4, 5, 6, 7, 8], 100, 2, 4, 1.0)
    assert acc == [3, 7, 11, 15]
    acc = [0.0] * 4                                # 起点不在行边界上: 第一行只吃到半行
    D._fold(acc, 101, [2, 3, 4], 100, 2, 4, 1.0)
    assert acc == [2, 7, 0, 0]
    acc = [0.0] * 4                                # 两头超出窗口的部分裁掉
    D._fold(acc, 96, list(range(1, 17)), 100, 2, 4, 0.5)
    assert acc == [(5 + 6) / 2, (7 + 8) / 2, (9 + 10) / 2, (11 + 12) / 2]
    acc = [0.0] * 4                                # 完全在窗口外
    D._fold(acc, 200, [1, 2, 3], 100, 2, 4, 1.0)
    D._fold(acc, 10, [1, 2, 3], 100, 2, 4, 1.0)
    assert acc == [0, 0, 0, 0]
    acc = [0.0] * 3                                # k=1
    D._fold(acc, 99, [9, 1, 0, 2, 9], 100, 1, 3, 2.0)
    assert acc == [2, 0, 4]


def test_网格_行数受限且对齐():
    b, p0, rows, k = choose_grid(63377.0, 66623.0, 5.0, 220)
    assert rows <= 220 and b == 5.0 * k and p0 % b == 0
    assert p0 <= 63377.0 and p0 + rows * b >= 66623.0
    b1, _, rows1, _ = choose_grid(64900.0, 65100.0, 5.0, 220)
    assert b1 == 5.0 and rows1 == 40               # 窗口小就不变粗
    b2, p2, rows2, _ = choose_grid(50000.0, 80000.0, 5.0, 220)
    assert rows2 <= 220 and p2 <= 50000.0 and p2 + rows2 * b2 >= 80000.0


# ================================================================ 热力图
def test_热力图_空载荷():
    p = build_heatmap([], hours=6, sources="all")
    assert set(p) == CONTRACT_KEYS
    assert p["times"] == [] and p["z"] == [] and p["mid"] == [] and p["rows"] == 0
    assert p["latest"] is None and p["recorded_minutes"] == 0 and p["sources"] == []
    assert set(p["coverage"]) == set(ALL_SOURCES)
    assert set(p["health"]) == {"last_ok", "last_error", "snapshots"}
    assert set(p["health"]["last_ok"]) == set(ALL_SOURCES) and p["health"]["snapshots"] == 0
    json.dumps(p)
    # 有数据但选中的源从没出现过 → 同样是空载荷
    s = mk_snap(T0, {"binance": mk_book()})
    assert build_heatmap([s], sources="okx")["rows"] == 0


def test_热力图_形状与不变量():
    snaps = [mk_snap(T0 + i * 30_000, {"binance": mk_book(65000 + 3 * i),
                                       "okx": mk_book(65002 + 3 * i, ((20.0, 3.0), (800.0, 6.0)), "okx")})
             for i in range(40)]
    p = build_heatmap(snaps, hours=1, sources="all", max_cols=360, max_rows=220)
    assert set(p) == CONTRACT_KEYS and p["symbol"] == "BTCUSDT"
    t = len(p["times"])
    assert t == 40 and len(p["mid"]) == t and len(p["z"]) == t
    assert all(len(col) == p["rows"] for col in p["z"])
    assert 0 < p["rows"] <= 220
    assert p["times"] == sorted(set(p["times"])) and p["times"][-1] == snaps[-1].ts
    assert p["p0"] % p["bucket"] == 0
    assert p["sources"] == ["binance", "okx"]
    assert all(v >= 0 for col in p["z"] for v in col)
    assert all(round(v, 1) == v for col in p["z"] for v in col)             # 1 位小数
    assert 0 < p["zmax"] <= max(v for col in p["z"] for v in col)
    assert p["mid"][0] == 65000.0 and p["mid"][-1] == 65000.0 + 3 * 39      # 参考中间价取优先级最高的源
    assert p["recorded_minutes"] == pytest.approx(19.5)
    assert p["coverage"]["binance"] == pytest.approx(600 / 65000 * 100, abs=0.02)
    assert p["coverage"]["okx"] == pytest.approx(800 / 65000 * 100, abs=0.02)
    assert p["coverage"]["bybit"] == 0.0
    la = p["latest"]
    assert la["ts"] == snaps[-1].ts and la["mid"] == p["mid"][-1]
    assert len(la["prices"]) == len(la["bids"]) == len(la["asks"]) == p["rows"]
    assert la["prices"][0] == p["p0"] and la["prices"][1] - la["prices"][0] == p["bucket"]
    json.dumps(p)


def test_热力图_买单在中间价下方_卖单在上方():
    mid = 65000.0
    book = Book("binance",
                bids=[(mid - 0.5, 0.5), (64000.0, 50.0)],
                asks=[(mid + 0.5, 0.5), (66000.0, 70.0)])
    p = build_heatmap([mk_snap(T0, {"binance": book})], hours=1, max_rows=1000)
    assert p["bucket"] == 5.0
    r_bid, r_mid, r_ask = row_of(p, 64000.0), row_of(p, mid), row_of(p, 66000.0)
    assert r_bid < r_mid < r_ask
    col, la = p["z"][0], p["latest"]
    assert col[r_bid] == 50.0 and col[r_ask] == 70.0
    assert la["bids"][r_bid] == 50.0 and la["asks"][r_bid] == 0
    assert la["asks"][r_ask] == 70.0 and la["bids"][r_ask] == 0
    assert sum(la["bids"][r_mid + 1:]) == 0 and sum(la["asks"][:r_mid - 1]) == 0
    assert sum(la["bids"]) == pytest.approx(50.5) and sum(la["asks"]) == pytest.approx(70.5)
    assert sum(col) == pytest.approx(121.0)
    # 窗口贴着数据: 最低一行就是买墙, 最高一行就是卖墙
    assert r_bid == 0 and r_ask == p["rows"] - 1


def test_热力图_变粗保持总量():
    levels = tuple((7.0 * j + 3.0, 0.1 * ((j * 7) % 13 + 1)) for j in range(200))    # 数量都是 0.1 的整数倍
    snaps = [mk_snap(T0 + i * 30_000, {"binance": mk_book(65000 + 11 * i, levels)}) for i in range(6)]
    fine = build_heatmap(snaps, hours=1, max_rows=100_000)
    coarse = build_heatmap(snaps, hours=1, max_rows=40)
    assert fine["bucket"] == 5.0 and coarse["bucket"] > 5.0 and coarse["rows"] <= 40
    assert coarse["bucket"] % 5.0 == 0 and coarse["p0"] % coarse["bucket"] == 0
    for cf, cc in zip(fine["z"], coarse["z"]):
        assert sum(cc) == pytest.approx(sum(cf), abs=1e-6)
        # 逐行: 粗行 = 落在其中的细行之和
        agg = [0.0] * coarse["rows"]
        for i, v in enumerate(cf):
            agg[row_of(coarse, fine["p0"] + i * fine["bucket"])] += v
        assert cc == pytest.approx(agg, abs=1e-6)
    assert sum(coarse["latest"]["bids"]) == pytest.approx(sum(fine["latest"]["bids"]), abs=1e-6)
    assert sum(coarse["latest"]["asks"]) == pytest.approx(sum(fine["latest"]["asks"]), abs=1e-6)


def test_热力图_时间降采样取均值而非最后一张():
    wall = 64900.0

    def snap(i, qty, with_bybit=True):
        books = {"binance": mk_book(65000, ((100.0, qty),))}
        if with_bybit:
            books["bybit"] = mk_book(65000, ((100.0, 6.0),), "bybit")
        return mk_snap(T0 + i * 30_000, books)

    # 每列 60s = 2 张快照。第 1 列: 墙 10 → 30; 第 2 列: bybit 只在其中一张里
    snaps = [snap(0, 10.0), snap(1, 30.0), snap(2, 20.0), snap(3, 20.0, with_bybit=False)]
    p = build_heatmap(snaps, hours=1, max_cols=60, max_rows=1000)
    assert len(p["times"]) == 2
    r = row_of(p, wall)
    assert p["z"][0][r] == 20.0 + 6.0             # binance 均值 (10+30)/2, 加 bybit 6
    assert p["z"][1][r] == 20.0 + 6.0             # bybit 缺席的那张不把它摊薄成 3
    assert p["times"] == [T0, T0 + 60_000]        # times = 各列的起始时刻
    # 只看 bybit
    pb = build_heatmap(snaps, hours=1, sources="bybit", max_cols=60, max_rows=1000)
    assert pb["sources"] == ["bybit"] and pb["z"][0][row_of(pb, wall)] == 6.0
    assert len(pb["times"]) == 2                  # 第 2 列只有一张含 bybit


def test_热力图_列数上限与时间窗():
    snaps = [mk_snap(T0 + i * 30_000, {"binance": mk_book(65000 + i)}) for i in range(600)]    # 5 小时
    p = build_heatmap(snaps, hours=6, max_cols=50)
    assert 0 < len(p["times"]) <= 50 and p["times"] == sorted(set(p["times"]))
    assert len(p["z"]) == len(p["mid"]) == len(p["times"])
    p1 = build_heatmap(snaps, hours=1, max_cols=360)
    assert len(p1["times"]) == 120                # 只取最近 1 小时, 一张一列
    assert p1["times"][0] > snaps[-1].ts - 3600_000 and p1["times"][-1] == snaps[-1].ts
    # 同一批数据再多来一张: 旧列的时间戳不变(列对齐绝对时间, 画面不抖)
    more = snaps + [mk_snap(T0 + 600 * 30_000, {"binance": mk_book()})]
    p2 = build_heatmap(more, hours=6, max_cols=50)
    assert set(p["times"][1:-1]) <= set(p2["times"])


def test_热力图_均值行与原始快照等价(tmp_path):
    """先卷 5 分钟再构图 == 直接拿原始快照构图(同样的 5 分钟列)。"""
    st = DepthStore(tmp_path / "d.sqlite")
    for i in range(30):
        books = {"binance": mk_book(65000 + 2 * i, ((10.0, 1.0 + i % 4), (300.0, 5.0)))}
        if i % 3:
            books["okx"] = mk_book(65001 + 2 * i, ((40.0, 2.0 + i % 5), (900.0, 9.0)), "okx")
        st.write(mk_snap(T0 + i * 30_000, books))
    st.rollup_pending(T0 + 30 * 30_000)
    raw, m5 = st.range(0), st.range(0, tier="m5")
    assert len(m5) == 3
    a = build_heatmap(raw, hours=24, max_cols=288, max_rows=100_000)
    b = build_heatmap(m5, hours=24, max_cols=288, max_rows=100_000)
    assert len(a["times"]) == len(b["times"]) == 3
    assert a["times"] == b["times"] and a["mid"] == pytest.approx(b["mid"], abs=0.011)
    assert (a["p0"], a["bucket"], a["rows"]) == (b["p0"], b["bucket"], b["rows"])
    for ca, cb in zip(a["z"], b["z"]):
        assert ca == pytest.approx(cb, abs=0.11)   # 均值行量化到 0.01 BTC + 输出 1 位小数


def test_热力图_桶宽不一致也不崩():
    """中途改过记录桶宽: 走逐桶兜底路径, 总量仍守恒。"""
    a = mk_snap(T0, {"binance": mk_book(65000, ((10.0, 4.0), (100.0, 8.0)))}, bucket=5.0)
    b = mk_snap(T0 + 30_000, {"binance": mk_book(65000, ((10.0, 4.0), (100.0, 8.0)))}, bucket=10.0)
    p = build_heatmap([a, b], hours=1, max_rows=1000)
    assert p["bucket"] == 10.0 and len(p["z"]) == 2
    assert sum(p["z"][0]) == pytest.approx(25.0) and sum(p["z"][1]) == pytest.approx(25.0)


# ================================================================ 抓取(假会话)
class FakeResp:
    def __init__(self, body=b"{}", status=200, headers=None, delay=0.0):
        self.body = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.status, self.headers, self.delay = status, headers or {}, delay

    async def __aenter__(self):
        if self.delay:
            await asyncio.sleep(self.delay)
        return self

    async def __aexit__(self, *exc):
        return False

    async def read(self):
        return self.body


class FakeSession:
    def __init__(self, routes, headers=None):
        self.routes, self.headers, self.calls = routes, headers or {}, []

    def get(self, url, params=None, timeout=None):
        self.calls.append((url, dict(params or {})))
        r = self.routes[url]
        return r() if callable(r) else r


def exch_routes(mid=65000.0):
    """四个交易所各一份小盘口。OKX 以「张」计: 100 张 × 0.01 = 1 BTC。"""
    s = lambda x: f"{x:.2f}"                                            # noqa: E731
    return {
        D.BINANCE_FUT_URL: FakeResp({"bids": [[s(mid - 1), "1.5"], [s(mid - 50), "3"]],
                                     "asks": [[s(mid + 1), "2.5"]]}, headers={"X-MBX-USED-WEIGHT-1M": "40"}),
        D.BYBIT_URL: FakeResp({"retCode": 0, "result": {"b": [[s(mid - 2), "4"]], "a": [[s(mid + 2), "5"]]}}),
        D.OKX_INSTR_URL: FakeResp({"code": "0", "data": [{"instId": "BTC-USDT-SWAP", "ctVal": "0.01",
                                                          "ctMult": "1", "ctValCcy": "BTC"}]}),
        D.OKX_BOOKS_URL: FakeResp({"code": "0", "data": [{"bids": [[s(mid - 3), "100", "7"]],
                                                          "asks": [[s(mid + 3), "250", "9"]]}]}),
        D.BINANCE_SPOT_URL: FakeResp({"bids": [[s(mid - 30), "6"]], "asks": [[s(mid - 28), "7"]]},
                                     headers={"x-mbx-used-weight-1m": "250"}),
    }


def test_http_限流与错误与超时():
    async def go():
        with pytest.raises(RateLimited) as ei:
            await D._http_get(FakeSession({"u": FakeResp(status=429, headers={"Retry-After": "77"})}), "u", {}, 1.0)
        assert ei.value.status == 429 and ei.value.retry_after == 77.0
        with pytest.raises(RateLimited) as ei:
            await D._http_get(FakeSession({"u": FakeResp(status=418)}), "u", {}, 1.0)
        assert ei.value.status == 418
        with pytest.raises(SourceError, match="HTTP 403"):
            await D._http_get(FakeSession({"u": FakeResp(status=403)}), "u", {}, 1.0)
        t0 = time.perf_counter()
        with pytest.raises(SourceError, match="超时"):
            await D._http_get(FakeSession({"u": FakeResp(delay=5.0)}), "u", {}, 0.1)
        assert time.perf_counter() - t0 < 1.0
        # 带 API key 默认头的会话(实盘那条)不许拿来抓别家的公开行情
        with pytest.raises(SourceError, match="密钥"):
            await D._http_get(FakeSession({"u": FakeResp()}, headers={"X-MBX-APIKEY": "k"}), "u", {}, 1.0)
        body, _ = await D._http_get(FakeSession({"u": FakeResp(b"abc")}), "u", {}, 1.0)
        assert body == b"abc"
    asyncio.run(go())


def test_抓取_四源归一化与各源隔离():
    async def go():
        routes = exch_routes()
        routes[D.BYBIT_URL] = FakeResp(delay=5.0)                          # bybit 卡死
        routes[D.BINANCE_SPOT_URL] = FakeResp(status=451)                  # 现货被地区封锁
        sess = FakeSession(routes)
        t0 = time.perf_counter()
        out = await D.fetch_books(sess, timeout_s=0.2)
        assert time.perf_counter() - t0 < 1.0                              # 一个源卡住不拖累其它源
        assert set(out) == set(ALL_SOURCES)
        assert isinstance(out["bybit"], Exception) and isinstance(out["binance_spot"], Exception)
        assert out["binance"].bids == [(64999.0, 1.5), (64950.0, 3.0)]
        assert out["okx"].bids == [(64997.0, pytest.approx(1.0))]          # 100 张 → 1 BTC
        assert out["okx"].asks == [(65003.0, pytest.approx(2.5))]
        # 参数: 各家要的深度档数
        by_url = {u: p for u, p in sess.calls}
        assert by_url[D.BINANCE_FUT_URL] == {"symbol": "BTCUSDT", "limit": "1000"}
        assert by_url[D.BINANCE_SPOT_URL] == {"symbol": "BTCUSDT", "limit": "5000"}
        assert by_url[D.OKX_BOOKS_URL] == {"instId": "BTC-USDT-SWAP", "sz": "5000"}
        assert by_url[D.BYBIT_URL]["category"] == "linear" and by_url[D.BYBIT_URL]["limit"] == "1000"
    asyncio.run(go())


def test_抓取_okx面值只查一次_查不到不猜():
    async def go():
        sess = FakeSession(exch_routes())
        src = D.OkxSource()
        for _ in range(3):
            raw = await src.fetch_raw(sess, 1.0)
            assert raw.ct_val == pytest.approx(0.01)
        assert sum(1 for u, _ in sess.calls if u == D.OKX_INSTR_URL) == 1
        assert sum(1 for u, _ in sess.calls if u == D.OKX_BOOKS_URL) == 3
        # 合约信息接口挂了且从没拿到过面值 → 本源失败, 不用写死的值顶上
        routes = exch_routes()
        routes[D.OKX_INSTR_URL] = FakeResp(status=500)
        sess2 = FakeSession(routes)
        res = await D.fetch_raw_guarded(D.OkxSource(), sess2, 1.0)
        assert isinstance(res, SourceError)
        assert all(u != D.OKX_BOOKS_URL for u, _ in sess2.calls)
    asyncio.run(go())


# ================================================================ 记录器
class FakeSource:
    def __init__(self, name, book=None, fetch_exc=None, parse_exc=None, delay=0.0,
                 weight=None, weight_guard=None):
        self.name, self.book, self.fetch_exc, self.parse_exc = name, book, fetch_exc, parse_exc
        self.delay, self.weight, self.weight_guard, self.calls = delay, weight, weight_guard, 0

    async def fetch_raw(self, session, timeout_s):
        self.calls += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.fetch_exc:
            raise self.fetch_exc
        return RawBook(self.name, b"{}", weight=self.weight)

    def parse(self, raw):
        if self.parse_exc:
            raise self.parse_exc
        return self.book


def test_记录器_真实源类加假会话_端到端(tmp_path):
    async def go():
        st = DepthStore(tmp_path / "d.sqlite")
        rec = DepthRecorder(st, FakeSession(exch_routes()), interval_s=30, timeout_s=1.0)
        stats = await rec.snapshot_once(T0)
        rec.close()
        return st, rec, stats
    st, rec, stats = asyncio.run(go())
    assert stats["written"] and sorted(stats["ok"]) == sorted(ALL_SOURCES) and stats["errors"] == {}
    assert stats["bytes"] > 0 and stats["compute_ms"] >= 0
    snap = st.latest()
    assert snap.ts == T0 and list(snap.sources) == list(ALL_SOURCES)
    assert snap.mid == 65000.0                                # 参考中间价 = 币安合约
    assert snap.sources["binance_spot"]["m"] == 64971.0       # 现货有基差, 各用各的中间价
    assert sum(snap.side_btc("okx", "bids")[1]) == pytest.approx(1.0)       # 100 张 × 0.01
    assert sum(snap.side_btc("okx", "asks")[1]) == pytest.approx(2.5)
    assert sum(snap.side_btc("binance", "bids")[1]) == pytest.approx(4.5)
    h = rec.health()
    assert set(h) == {"last_ok", "last_error", "snapshots"} and h["snapshots"] == 1
    assert all(h["last_ok"][n] == T0 for n in ALL_SOURCES)
    assert all(h["last_error"][n] is None for n in ALL_SOURCES)
    assert rec.status()["weight"] == {"binance": 40, "binance_spot": 250}
    json.dumps(rec.status())


def test_记录器_单源出错不影响其它源(tmp_path):
    async def go():
        st = DepthStore(tmp_path / "d.sqlite")
        srcs = {
            "binance": FakeSource("binance", mk_book(65000)),
            "bybit": FakeSource("bybit", fetch_exc=RuntimeError("boom")),               # 抓取抛异常
            "okx": FakeSource("okx", parse_exc=ValueError("bad json")),                 # 解析抛异常
            "binance_spot": FakeSource("binance_spot", mk_book(64980), delay=5.0),      # 卡死
        }
        rec = DepthRecorder(st, object(), sources=srcs, interval_s=30, timeout_s=0.1)
        t0 = time.perf_counter()
        stats = await rec.snapshot_once(T0)
        took = time.perf_counter() - t0
        rec.close()
        return st, rec, stats, took
    st, rec, stats, took = asyncio.run(go())
    assert took < 1.0
    assert stats["ok"] == ["binance"] and stats["written"]
    assert set(stats["errors"]) == {"bybit", "okx", "binance_spot"}
    assert list(st.latest().sources) == ["binance"]
    h = rec.health()
    assert h["last_ok"] == {"binance": T0, "bybit": None, "okx": None, "binance_spot": None}
    assert h["last_error"]["binance"] is None
    assert "boom" in h["last_error"]["bybit"] and "bad json" in h["last_error"]["okx"]
    assert "超时" in h["last_error"]["binance_spot"]
    assert h["snapshots"] == 1


def test_记录器_中间价离谱的源被丢弃(tmp_path):
    async def go():
        st = DepthStore(tmp_path / "d.sqlite")
        srcs = {"binance": FakeSource("binance", mk_book(65000)),
                "bybit": FakeSource("bybit", mk_book(65010, source="bybit")),
                "okx": FakeSource("okx", mk_book(6500, ((1.0, 1.0),), "okx"))}      # 单位/交易对搞错
        rec = DepthRecorder(st, object(), sources=srcs, timeout_s=0.5)
        stats = await rec.snapshot_once(T0)
        rec.close()
        return st, rec, stats
    st, rec, stats = asyncio.run(go())
    assert stats["ok"] == ["binance", "bybit"] and "偏离" in stats["errors"]["okx"]
    assert list(st.latest().sources) == ["binance", "bybit"]
    assert rec.health()["last_ok"]["okx"] is None


def test_记录器_全部失败不写库_限流要停手_连续失败退避(tmp_path):
    async def go():
        st = DepthStore(tmp_path / "d.sqlite")
        srcs = {"binance": FakeSource("binance", fetch_exc=RateLimited(429, 120.0)),
                "bybit": FakeSource("bybit", fetch_exc=RuntimeError("down"))}
        rec = DepthRecorder(st, object(), sources=srcs, interval_s=30, timeout_s=0.5)
        out = []
        for i in range(6):
            out.append(await rec.snapshot_once(T0 + i * 30_000))
        rec.close()
        return st, rec, srcs, out
    st, rec, srcs, out = asyncio.run(go())
    assert not any(o["written"] for o in out) and st.count() == 0
    assert not st.path.exists()                   # 没数据就不建库
    # 429 带 Retry-After=120s → 之后 4 轮(120s)都不再碰币安
    assert srcs["binance"].calls == 2 and out[1]["skipped"] == ["binance"]
    # 普通失败: 前 3 次每轮都试, 之后指数退避(60s → 第 5 轮再试)
    assert srcs["bybit"].calls == 4
    assert rec.status()["fails"]["bybit"] == 4 and rec.health()["snapshots"] == 0


def test_记录器_币安权重吃紧时让路(tmp_path):
    async def go():
        st = DepthStore(tmp_path / "d.sqlite")
        srcs = {"binance": FakeSource("binance", mk_book(), weight=1900, weight_guard=1800),
                "bybit": FakeSource("bybit", mk_book(source="bybit"))}
        rec = DepthRecorder(st, object(), sources=srcs, interval_s=30, timeout_s=0.5)
        a = await rec.snapshot_once(T0)
        b = await rec.snapshot_once(T0 + 30_000)
        c = await rec.snapshot_once(T0 + 60_000)
        rec.close()
        return rec, srcs, a, b, c
    rec, srcs, a, b, c = asyncio.run(go())
    assert a["ok"] == ["binance", "bybit"]        # 这一轮的数据照用
    assert b["skipped"] == ["binance"] and b["ok"] == ["bybit"]
    assert c["skipped"] == [] and srcs["binance"].calls == 2


def test_记录器_run循环不死_可停可取消(tmp_path):
    async def go():
        st = DepthStore(tmp_path / "d.sqlite")
        srcs = {"binance": FakeSource("binance", mk_book()),
                "bybit": FakeSource("bybit", fetch_exc=RuntimeError("boom"))}
        rec = DepthRecorder(st, object(), sources=srcs, interval_s=0.05, timeout_s=0.2)
        real_write = st.write
        state = {"broken": True}

        def flaky_write(snap, tier="raw"):
            if state["broken"]:
                raise sqlite3.OperationalError("disk I/O error")
            return real_write(snap, tier)

        async def until(cond, limit=10.0):         # 轮询等条件成立(机器忙时不靠固定 sleep 赌时间)
            t_end = time.monotonic() + limit
            while not cond() and time.monotonic() < t_end:
                await asyncio.sleep(0.02)
            return cond()

        st.write = flaky_write                    # 连库都写不了, 循环也不能死
        task = asyncio.create_task(rec.run())
        assert await until(lambda: srcs["binance"].calls >= 3)       # 出错后还在一轮轮地跑
        assert not task.done() and rec.status()["running"]
        assert "disk I/O error" in rec.status()["loop_error"] and st.count() == 0
        state["broken"] = False
        assert await until(lambda: rec.health()["snapshots"] >= 2)   # 库恢复后自动接着录
        rec.stop()
        await asyncio.wait_for(task, 5.0)
        assert not rec.status()["running"]
        n_mid = st.count()

        # 取消: CancelledError 正常传播, 便于进程关停
        rec2 = DepthRecorder(st, object(), sources=srcs, interval_s=0.05, timeout_s=0.2)
        t2 = asyncio.create_task(rec2.run())
        assert await until(lambda: rec2.status()["iterations"] >= 1)
        t2.cancel()
        with pytest.raises(asyncio.CancelledError):
            await t2
        return st, rec, rec2, n_mid
    st, rec, rec2, n_mid = asyncio.run(go())
    assert n_mid >= 2 and rec.status()["loop_error"] is None
    assert rec.health()["snapshots"] == n_mid
    assert rec.health()["last_ok"]["binance"] is not None and "boom" in rec.health()["last_error"]["bybit"]
    assert not rec2.status()["running"]


# ================================================================ 带缓存的请求入口
def test_参数收敛():
    assert D.normalize_query(6, "all", 360) == (6, ALL_SOURCES, 360)
    assert D.normalize_query("5", "okx, binance ,nope", "9999") == (6, ("binance", "okx"), 360)
    assert D.normalize_query(1000, "", 1) == (168, ALL_SOURCES, 60)
    assert D.normalize_query("abc", None, "x") == (6, ALL_SOURCES, 360)
    assert D.normalize_query(72, ["binance_spot"], 200) == (72, ("binance_spot",), 180)
    assert D.parse_sources("ALL") == ALL_SOURCES


def test_请求入口_载荷完整且走缓存(tmp_path):
    D.heatmap_cache_clear()
    st = DepthStore(tmp_path / "d.sqlite")
    now = T0 + 40 * 30_000
    # 库还不存在: 空载荷, 不建库
    empty = D.heatmap_payload(st, 6, "all", 360, now_ms=now)
    assert set(empty) == CONTRACT_KEYS and empty["rows"] == 0 and empty["latest"] is None
    assert empty["recorded_minutes"] == 0 and empty["health"]["snapshots"] == 0
    assert not st.path.exists()

    D.heatmap_cache_clear()
    for i in range(40):
        st.write(mk_snap(T0 + i * 30_000, {"binance": mk_book(65000 + i), "okx": mk_book(65001 + i, source="okx")}))
    raw = D.heatmap_json(st, 1, "all", 360, now_ms=now)
    assert isinstance(raw, bytes)
    p = json.loads(raw)
    assert set(p) == CONTRACT_KEYS
    assert len(p["times"]) == 40 and len(p["z"]) == 40 and all(len(c) == p["rows"] for c in p["z"])
    assert p["latest"]["ts"] == T0 + 39 * 30_000 and len(p["latest"]["bids"]) == p["rows"]
    assert p["recorded_minutes"] == pytest.approx(19.5) and p["health"]["snapshots"] == 40
    assert p["sources"] == ["binance", "okx"]
    # 与纯函数的结果一致
    direct = build_heatmap(st.read_window(1, now), 1, "all", 360, 220)
    assert p["z"] == direct["z"] and p["times"] == direct["times"] and p["latest"] == direct["latest"]
    # 2 秒内重复请求直接拿缓存的同一份字节, 不重新构图
    builds = D._CACHE.builds
    assert D.heatmap_json(st, 1, "all", 360, now_ms=now) is raw
    # gzip 版: 解开后与明文逐字节相同
    gz = D.heatmap_json(st, 1, "all", 360, now_ms=now, gzip=True)
    assert gz[:2] == bytes([0x1F, 0x8B]) and len(gz) < len(raw) and gzip.decompress(gz) == raw
    assert D.heatmap_json(st, 1, "all", 360, now_ms=now, gzip=True) is gz
    assert D._CACHE.builds == builds
    # 不同的源组合是不同的缓存项
    pb = D.heatmap_payload(st, 1, "okx", 360, now_ms=now)
    assert pb["sources"] == ["okx"] and pb["mid"][0] == 65001.0

    # 有记录器时 health 取记录器的
    class Rec:
        def health(self):
            return {"last_ok": {n: 1 for n in ALL_SOURCES}, "last_error": {n: None for n in ALL_SOURCES},
                    "snapshots": 123}
    D.heatmap_cache_clear()
    assert D.heatmap_payload(st, 1, "all", 360, recorder=Rec(), now_ms=now)["health"]["snapshots"] == 123

    async def via_async():
        return await D.heatmap_json_async(st, 1, "okx", 360)
    assert json.loads(asyncio.run(via_async()))["symbol"] == "BTCUSDT"
    D.heatmap_cache_clear()


def test_请求入口_轻的部分随新快照刷新(tmp_path):
    """矩阵缓存期内来了新快照: latest/recorded_minutes 跟上, 且仍落在缓存的网格上。"""
    D.heatmap_cache_clear()
    st = DepthStore(tmp_path / "d.sqlite")
    for i in range(10):
        st.write(mk_snap(T0 + i * 30_000, {"binance": mk_book(65000)}))
    now = T0 + 10 * 30_000
    cache = D._HeatCache(light_ttl_s=0.0)          # 轻的部分每次都重算
    args = (st, 1, ALL_SOURCES, 360, 1000, None, "BTCUSDT", now)
    p1 = json.loads(cache.get(*args))
    st.write(mk_snap(T0 + 10 * 30_000, {"binance": mk_book(65000, ((10.0, 42.0),))}))
    p2 = json.loads(cache.get(*args))
    assert p2["times"] == p1["times"]              # 矩阵还是缓存的那份
    assert p2["latest"]["ts"] == T0 + 10 * 30_000 and p2["latest"]["ts"] > p1["latest"]["ts"]
    assert len(p2["latest"]["bids"]) == p2["rows"] == p1["rows"]
    assert p2["latest"]["bids"][row_of(p2, 64990.0)] == 42.0
    assert p2["recorded_minutes"] == 5.0
    assert cache.builds == 1
    # gzip 版每次只重压尾巴, 仍是合法的 gzip 流, 且与明文一致
    for _ in range(3):
        st.write(mk_snap(T0 + 11 * 30_000 + _, {"binance": mk_book(65000, ((10.0, 7.0 + _),))}))
        gz = cache.get(*args, gzip=True)
        p3 = json.loads(gzip.decompress(gz))
        assert set(p3) == CONTRACT_KEYS and p3["times"] == p1["times"]
        assert p3["latest"]["bids"][row_of(p3, 64990.0)] == 7.0 + _
    assert cache.builds == 1


def test_请求入口_缓存项数量有上限(tmp_path):
    """公开接口: 陌生人轮番换参数也只能占住有限的内存。"""
    st = DepthStore(tmp_path / "d.sqlite")
    for i in range(4):
        st.write(mk_snap(T0 + i * 30_000, {n: mk_book(65000, source=n) for n in ALL_SOURCES}))
    cache = D._HeatCache(max_entries=3)
    now = T0 + 4 * 30_000
    combos = [("binance",), ("bybit",), ("okx",), ("binance_spot",), ("binance", "okx")]
    for sel in combos:
        for gz in (False, True):
            cache.get(st, 1, sel, 360, 220, None, "BTCUSDT", now, gz)
    assert len(cache._heavy) == 3 and len(cache._full) <= 6
    assert cache.builds == len(combos)
