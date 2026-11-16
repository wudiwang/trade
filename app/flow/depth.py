"""多交易所深度记录器 + 深度热力图构建（订单流页「深度热力图」模块，2026-11-16）。

数据流（每 ~30s 一轮）:
    抓取四个源的盘口(网络, 各源互相隔离)
      → 解析成 (价格, BTC 数量)                      纯计算
      → 按固定的绝对美元价格桶折叠, 买卖分开          纯计算
      → zlib 压缩写进独立的 sqlite 文件(DepthStore)
      → 每满 5 分钟把原始快照卷成一行均值(m5 档)
    页面请求只读本地库: build_heatmap() 把一段快照拼成 /api/flow/depth 的载荷。

为什么要两档(原始 30s + 5 分钟均值):
    7 天窗口若直接读原始快照是 2 万行 × ~1100 个数, 纯 Python 解码+折叠要几秒 CPU,
    而本进程里同时跑着实盘引擎。≥24h 的窗口改读 m5 档(7 天 = 2016 行), 构建 < 0.5s, 且带缓存。

各源能看到的深度不同(实测, BTC≈65000): 币安合约 ±0.22% / Bybit ±0.5% / OKX ±1.35% / 币安现货 ±2.2%。
所以合并后的热力图离现价越远、参与求和的源越少 —— 载荷里的 coverage 就是给前端画这条边界用的。

公开接口绝不在请求路径上打交易所: 本模块里只有 DepthRecorder(后台任务)会发网络请求。

接入示例(路由层)::

    store = DepthStore(ROOT / "data" / "flow_depth.sqlite")
    recorder = DepthRecorder(store)                 # 自建干净会话; 别传实盘那条带 API key 头的
    asyncio.create_task(recorder.run())             # 永不抛; 关停时 cancel 即可

    @app.get("/api/flow/depth")
    def flow_depth(request: Request, hours: int = 6, src: str = "all", cols: int = 360):
        gz = "gzip" in request.headers.get("accept-encoding", "")
        body = heatmap_json(store, hours, src, cols, recorder=recorder, gzip=gz)   # 参数会被收敛, 带缓存
        headers = {"Content-Encoding": "gzip", "Vary": "Accept-Encoding"} if gz else {}
        return Response(body, media_type="application/json", headers=headers)

    # /api/flow/health 的 recorder 字段: recorder.status()

实测(2026-11-16, 开发机, 详见提交说明): 一行快照 ≈ 2.0KB(磁盘 ≈ 6.7MB/天, 原始只留 2 天);
一轮「解析+分桶+压缩」四个源合计 14~28ms CPU(在专用线程里); 6 小时全源图构建 0.14~0.28s、
7 天图 0.3~0.6s(都在请求线程里且带缓存, 命中时 ~30µs)。
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import sqlite3
import threading
import time
import zlib
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from statistics import median
from typing import Any, Iterable, Mapping

import aiohttp

log = logging.getLogger("flow.depth")

# ---------------------------------------------------------------- 常量
ALL_SOURCES = ("binance", "bybit", "okx", "binance_spot")   # 也是参考中间价的优先顺序
ALLOWED_HOURS = (1, 6, 24, 72, 168)

BUCKET_USD = 5.0          # 记录时的价格桶(美元)。显示时只会按整数倍变粗, 所以记录要细
HALF_RANGE_PCT = 2.5      # 只记录各源自身中间价 ±2.5% 以内的挂单
UNIT = 100                # 数量以 1/100 BTC 的整数存(输出只保留 1 位小数, 0.01 足够)
ROLLUP_S = 300            # 均值卷积窗口(秒)
ROLLUP_MIN_HOURS = 24     # 窗口 ≥ 该小时数时读 m5 档
MAX_MID_DEV_PCT = 2.0     # 某源中间价偏离各源中位数超过该幅度 → 当作脏数据丢弃

BINANCE_FUT_URL = "https://fapi.binance.com/fapi/v1/depth"
BINANCE_SPOT_URL = "https://api.binance.com/api/v3/depth"
BYBIT_URL = "https://api.bybit.com/v5/market/orderbook"
OKX_BOOKS_URL = "https://www.okx.com/api/v5/market/books-full"
OKX_INSTR_URL = "https://www.okx.com/api/v5/public/instruments"

# 实测(2026-11-16): OKX books-full 的可见深度与 sz 成正比 ——
#   sz=1000 → ±0.24% / 46KB;  sz=2000 → ±0.50% / 92KB;  sz=5000 → ±1.33% / 228KB(线上 gzip 后 53KB, json.loads 1.5ms)
# 5000 是该接口上限, 解析成本可以忽略, 所以直接取满。
OKX_SZ = 5000

_EPS = 1e-9               # 抵消 0.3/0.1=2.9999… 这类浮点误差, 远小于一个最小报价单位
_KEY_HEADERS = {"X-MBX-APIKEY", "AUTHORIZATION", "OK-ACCESS-KEY", "X-BAPI-API-KEY"}


# ================================================================ 纯计算: 解析
@dataclass
class Book:
    """归一化后的单源盘口。bids/asks 均为 (价格, BTC 数量)。"""
    source: str
    bids: list[tuple[float, float]]
    asks: list[tuple[float, float]]


class SourceError(Exception):
    """某个源本轮采集失败(文本会原样出现在 health.last_error 里)。"""


class RateLimited(SourceError):
    """交易所返回 429/418。必须停手, 否则会连累同 IP 的实盘引擎。"""

    def __init__(self, status: int, retry_after: float = 0.0):
        super().__init__(f"被限流 HTTP {status}")
        self.status = status
        self.retry_after = retry_after


def _levels(raw: Iterable, mult: float = 1.0) -> list[tuple[float, float]]:
    if mult == 1.0:
        return [(float(x[0]), float(x[1])) for x in raw]
    return [(float(x[0]), float(x[1]) * mult) for x in raw]


def parse_binance(obj: Mapping, source: str = "binance") -> Book:
    """币安合约 /fapi/v1/depth 与现货 /api/v3/depth 同构: {"bids":[["价","量"],…],"asks":[…]}, 量即 BTC。"""
    if "bids" not in obj or "asks" not in obj:
        raise SourceError(f"币安返回异常: {str(obj)[:80]}")
    return Book(source, _levels(obj["bids"]), _levels(obj["asks"]))


def parse_bybit(obj: Mapping) -> Book:
    """Bybit v5 orderbook(linear): {"retCode":0,"result":{"b":[["价","量"]],"a":[…]}}, 量即 BTC。"""
    if obj.get("retCode") != 0:
        raise SourceError(f"Bybit retCode={obj.get('retCode')} {str(obj.get('retMsg'))[:60]}")
    res = obj.get("result") or {}
    return Book("bybit", _levels(res.get("b") or []), _levels(res.get("a") or []))


def parse_okx(obj: Mapping, ct_val: float) -> Book:
    """OKX books-full: data[0].bids/asks = [["价","张数","订单数"]]; BTC 数量 = 张数 × 合约面值 ct_val。"""
    if str(obj.get("code")) != "0" or not obj.get("data"):
        raise SourceError(f"OKX code={obj.get('code')} {str(obj.get('msg'))[:60]}")
    if not ct_val or ct_val <= 0:
        raise SourceError("OKX 合约面值未知")
    d = obj["data"][0]
    return Book("okx", _levels(d.get("bids") or [], ct_val), _levels(d.get("asks") or [], ct_val))


def parse_okx_ct_val(obj: Mapping, inst_id: str, base_ccy: str) -> float:
    """从 /api/v5/public/instruments 取一张合约等于多少 BTC(ctVal × ctMult)。

    面值币种必须是标的币(U 本位线性合约); 否则张数→BTC 的换算不成立, 宁可报错不要猜。
    """
    if str(obj.get("code")) != "0" or not obj.get("data"):
        raise SourceError(f"OKX 合约信息 code={obj.get('code')} {str(obj.get('msg'))[:60]}")
    for d in obj["data"]:
        if d.get("instId") != inst_id:
            continue
        if d.get("ctValCcy") != base_ccy:
            raise SourceError(f"OKX {inst_id} 面值币种是 {d.get('ctValCcy')} 而非 {base_ccy}")
        val = float(d.get("ctVal") or 0) * float(d.get("ctMult") or 1)
        if val <= 0:
            raise SourceError(f"OKX {inst_id} 面值无效")
        return val
    raise SourceError(f"OKX 合约信息里没有 {inst_id}")


def split_symbol(symbol: str) -> tuple[str, str]:
    """'BTCUSDT' → ('BTC', 'USDT')。只支持 USDT 计价。"""
    if not symbol.endswith("USDT") or len(symbol) <= 4:
        raise ValueError(f"只支持 USDT 计价的交易对: {symbol}")
    return symbol[:-4], "USDT"


def okx_inst_id(symbol: str) -> str:
    base, quote = split_symbol(symbol)
    return f"{base}-{quote}-SWAP"


# ================================================================ 纯计算: 分桶
@dataclass
class Bucketed:
    """单源盘口折叠进价格桶之后的结果。第 i 个桶覆盖 [i*bucket, (i+1)*bucket)。"""
    mid: float
    bid_i0: int               # bids[0] 对应的桶索引(最低的买单桶)
    bids: list[float]         # 稠密, BTC
    ask_i0: int               # asks[0] 对应的桶索引(最低的卖单桶)
    asks: list[float]
    reach_bid: float          # 实际记录到的最远买单距中间价的百分比
    reach_ask: float


def bucket_index(price: float, bucket_usd: float) -> int:
    """价格所在桶 = floor(price / bucket)。恰在边界上的价格归上面那个桶。"""
    return math.floor(price / bucket_usd + _EPS)


def _bucket_side(levels: list[tuple[float, float]], bucket_usd: float,
                 lo: float, hi: float) -> tuple[int, list[float], float | None, float | None]:
    acc: dict[int, float] = {}
    p_min = p_max = None
    for p, q in levels:
        if q <= 0 or p < lo or p > hi:
            continue
        i = int(p / bucket_usd + _EPS)          # p>0, 截断即 floor
        acc[i] = acc.get(i, 0.0) + q
        if p_min is None or p < p_min:
            p_min = p
        if p_max is None or p > p_max:
            p_max = p
    if not acc:
        return 0, [], None, None
    i0, i1 = min(acc), max(acc)
    return i0, [acc.get(i, 0.0) for i in range(i0, i1 + 1)], p_min, p_max


def bucket_book(book: Book, bucket_usd: float = BUCKET_USD,
                half_range_pct: float = HALF_RANGE_PCT, mid: float | None = None) -> Bucketed:
    """把一个源的盘口折进固定的绝对美元价格桶, 只保留 mid ± half_range_pct 以内, 买卖分开。

    mid 缺省取 (最优买 + 最优卖) / 2。空盘口或买卖倒挂视为脏数据, 抛 SourceError。
    """
    if not book.bids or not book.asks:
        raise SourceError("盘口为空")
    best_bid = max(p for p, _ in book.bids)
    best_ask = min(p for p, _ in book.asks)
    if best_bid <= 0 or best_ask <= 0 or best_bid >= best_ask:
        raise SourceError(f"盘口倒挂 买一{best_bid} 卖一{best_ask}")
    if mid is None:
        mid = (best_bid + best_ask) / 2.0
    lo, hi = mid * (1 - half_range_pct / 100.0), mid * (1 + half_range_pct / 100.0)
    b_i0, bids, b_min, _ = _bucket_side(book.bids, bucket_usd, lo, hi)
    a_i0, asks, _, a_max = _bucket_side(book.asks, bucket_usd, lo, hi)
    return Bucketed(
        mid=mid, bid_i0=b_i0, bids=bids, ask_i0=a_i0, asks=asks,
        reach_bid=(mid - b_min) / mid * 100.0 if b_min is not None else 0.0,
        reach_ask=(a_max - mid) / mid * 100.0 if a_max is not None else 0.0,
    )


def check_mids(mids: Mapping[str, float], max_dev_pct: float = MAX_MID_DEV_PCT) -> dict[str, float]:
    """各源中间价互相校验: 返回偏离中位数超限的源 → 偏离百分比(单位/交易对搞错时能拦住)。"""
    if len(mids) < 2:
        return {}
    ref = median(mids.values())
    return {n: abs(m - ref) / ref * 100.0 for n, m in mids.items()
            if ref > 0 and abs(m - ref) / ref * 100.0 > max_dev_pct}


# ================================================================ 快照(存储单元)
class Snapshot:
    """一行深度记录: 原始快照(n=1)或一段时间的均值(n>1)。

    meta(小, 不压缩, 构图时先扫它定价格窗口)::

        {"v":1, "u":100, "bk":5.0, "tm":均值行的平均时间(可无),
         "s": {"binance": {"m":中间价, "n":参与均值的快照数, "r":[买侧%, 卖侧%],
                           "b":[起始桶索引, 桶数], "a":[起始桶索引, 桶数]}, …}}

    body(大, zlib+json, 用到才解): {"binance": [[买侧各桶数量…], [卖侧各桶数量…]], …}, 数量单位 1/u BTC。
    """
    __slots__ = ("ts", "mid", "n", "meta", "_blob", "_body")

    def __init__(self, ts: int, mid: float, n: int, meta: dict,
                 blob: bytes | None = None, body: dict | None = None):
        self.ts = int(ts)
        self.mid = float(mid)
        self.n = int(n)
        self.meta = meta
        self._blob = blob
        self._body = body

    @property
    def t(self) -> int:
        """代表时间: 均值行用成员的平均时间, 原始快照就是 ts。"""
        return int(self.meta.get("tm", self.ts))

    @property
    def bucket(self) -> float:
        return float(self.meta["bk"])

    @property
    def unit(self) -> int:
        return int(self.meta["u"])

    @property
    def sources(self) -> dict:
        return self.meta["s"]

    def body(self) -> dict:
        if self._body is not None:
            return self._body
        return json.loads(zlib.decompress(self._blob))      # 不缓存: 长窗口时省内存

    def blob(self) -> bytes:
        if self._blob is None:
            self._blob = zlib.compress(json.dumps(self._body, separators=(",", ":")).encode(), 6)
        return self._blob

    def meta_json(self) -> str:
        return json.dumps(self.meta, separators=(",", ":"))

    def side_btc(self, source: str, side: str) -> tuple[int, list[float]]:
        """(起始桶索引, 各桶 BTC 数量)。side = 'bids' / 'asks'。测试与排查用。"""
        m = self.sources[source]
        qs = self.body()[source][0 if side == "bids" else 1]
        return m["b" if side == "bids" else "a"][0], [q / self.unit for q in qs]


def ref_mid(src_meta: Mapping, order: Iterable[str] = ALL_SOURCES) -> float | None:
    """按优先顺序取第一个在场源的中间价。"""
    for name in order:
        m = src_meta.get(name)
        if m:
            return m["m"]
    return None


def make_snapshot(ts: int, depths: Mapping[str, Bucketed],
                  bucket_usd: float = BUCKET_USD, unit: int = UNIT) -> Snapshot:
    """把各源的分桶结果打包成一行快照(数量量化成 1/unit BTC 的整数)。"""
    if not depths:
        raise ValueError("没有任何源的数据")
    smeta, body = {}, {}
    for name in sorted(depths, key=lambda n: ALL_SOURCES.index(n) if n in ALL_SOURCES else 99):
        d = depths[name]
        smeta[name] = {"m": round(d.mid, 2), "n": 1,
                       "r": [round(d.reach_bid, 3), round(d.reach_ask, 3)],
                       "b": [d.bid_i0, len(d.bids)], "a": [d.ask_i0, len(d.asks)]}
        body[name] = [[round(q * unit) for q in d.bids], [round(q * unit) for q in d.asks]]
    meta = {"v": 1, "u": unit, "bk": bucket_usd, "s": smeta}
    return Snapshot(ts, ref_mid(smeta), 1, meta, body=body)


def _mean_side(parts: list[tuple[int, list, int]], total_n: int) -> tuple[int, list[int]]:
    """parts: [(起始桶, 数量列表, 权重)] → 并集范围上的加权均值(缺的桶按 0 计)。"""
    parts = [p for p in parts if p[1]]
    if not parts or total_n <= 0:
        return 0, []
    i0 = min(p[0] for p in parts)
    i1 = max(p[0] + len(p[1]) for p in parts)
    acc = [0] * (i1 - i0)
    for start, qs, w in parts:
        off = start - i0
        if w == 1:
            for j, q in enumerate(qs, off):
                acc[j] += q
        else:
            for j, q in enumerate(qs, off):
                acc[j] += q * w
    return i0, [round(v / total_n) for v in acc]


def merge_snapshots(snaps: list[Snapshot], key_ts: int) -> Snapshot | None:
    """把一段快照卷成一行均值。

    每个源只在它【在场】的快照上求均值(某源偶尔超时不该把它的深度摊薄);
    桶范围取并集, 某张快照没覆盖到的桶按 0 计 —— 与 build_heatmap 对原始快照的处理完全一致,
    所以「先卷 5 分钟再按列求均值」与「直接对原始快照按列求均值」结果相同。
    桶宽/单位与最后一张不一致的快照(中途改过配置)直接跳过。
    """
    if not snaps:
        return None
    bk, unit = snaps[-1].bucket, snaps[-1].unit
    snaps = [s for s in snaps if s.bucket == bk and s.unit == unit]
    bodies = [s.body() for s in snaps]
    names = [n for n in ALL_SOURCES if any(n in s.sources for s in snaps)]
    names += sorted({n for s in snaps for n in s.sources} - set(names))
    smeta, body = {}, {}
    for name in names:
        n_tot, mid_sum, rb_sum, ra_sum = 0, 0.0, 0.0, 0.0
        b_parts, a_parts = [], []
        for s, bd in zip(snaps, bodies):
            m = s.sources.get(name)
            if not m:
                continue
            w = int(m.get("n", 1))
            n_tot += w
            mid_sum += m["m"] * w
            rb_sum += m["r"][0] * w
            ra_sum += m["r"][1] * w
            b_parts.append((m["b"][0], bd[name][0], w))
            a_parts.append((m["a"][0], bd[name][1], w))
        b_i0, bq = _mean_side(b_parts, n_tot)
        a_i0, aq = _mean_side(a_parts, n_tot)
        smeta[name] = {"m": round(mid_sum / n_tot, 2), "n": n_tot,
                       "r": [round(rb_sum / n_tot, 3), round(ra_sum / n_tot, 3)],
                       "b": [b_i0, len(bq)], "a": [a_i0, len(aq)]}
        body[name] = [bq, aq]
    n_all = sum(s.n for s in snaps)
    meta = {"v": 1, "u": unit, "bk": bk, "s": smeta,
            "tm": round(sum(s.t * s.n for s in snaps) / n_all)}
    mid = sum(s.mid * s.n for s in snaps) / n_all
    return Snapshot(key_ts, mid, n_all, meta, body=body)


# ================================================================ 网络: 抓取
@dataclass
class RawBook:
    """抓回来还没解析的响应体(解析放到线程里做, 不占事件循环)。"""
    source: str
    body: bytes
    weight: int | None = None      # 币安响应头里的本 IP 近 1 分钟已用权重
    ct_val: float | None = None    # 仅 OKX: 一张合约 = 多少 BTC


def _refuse_keyed_session(session: Any) -> None:
    """公开行情不需要密钥。若传进来的是实盘那条带 API key 默认头的会话, 会把 key 发给别家交易所 —— 直接拒绝。"""
    for k in (getattr(session, "headers", None) or {}):
        if str(k).upper() in _KEY_HEADERS:
            raise SourceError("拒绝使用带 API 密钥头的会话抓公开行情")


async def _http_get(session: aiohttp.ClientSession, url: str, params: dict,
                    timeout_s: float) -> tuple[bytes, Mapping]:
    """GET 并返回 (响应体, 响应头)。严格超时; 429/418 抛 RateLimited; 其它非 200 抛 SourceError。"""
    _refuse_keyed_session(session)

    async def _do():
        async with session.get(url, params=params,
                               timeout=aiohttp.ClientTimeout(total=timeout_s)) as resp:
            if resp.status in (418, 429):
                try:
                    ra = float(resp.headers.get("Retry-After") or 0)
                except (TypeError, ValueError):
                    ra = 0.0
                raise RateLimited(resp.status, ra)
            if resp.status != 200:
                raise SourceError(f"HTTP {resp.status}")
            return await resp.read(), resp.headers

    try:
        return await asyncio.wait_for(_do(), timeout_s * 1.1 + 0.05)
    except asyncio.TimeoutError:
        raise SourceError(f"超时(>{timeout_s:g}s)") from None


def _weight(headers: Mapping) -> int | None:
    """币安响应头 X-MBX-USED-WEIGHT-1M(现货返回的是小写), 大小写不敏感。"""
    try:
        for k, v in headers.items():
            if str(k).upper() == "X-MBX-USED-WEIGHT-1M":
                return int(v)
    except (TypeError, ValueError, AttributeError):
        pass
    return None


class BinanceSource:
    """币安合约(limit=1000, 权重 20)或现货(limit=5000, 权重 250)。两者的 IP 权重池互相独立。"""

    def __init__(self, name: str = "binance", symbol: str = "BTCUSDT", url: str = BINANCE_FUT_URL,
                 limit: int = 1000, weight_guard: int | None = 1800):
        self.name, self.symbol, self.url, self.limit = name, symbol, url, limit
        # 响应头显示本 IP 已用权重 ≥ weight_guard 时, 记录器主动停一分钟, 把额度让给实盘引擎
        self.weight_guard = weight_guard

    async def fetch_raw(self, session: aiohttp.ClientSession, timeout_s: float) -> RawBook:
        body, headers = await _http_get(session, self.url,
                                        {"symbol": self.symbol, "limit": str(self.limit)}, timeout_s)
        return RawBook(self.name, body, weight=_weight(headers))

    def parse(self, raw: RawBook) -> Book:
        return parse_binance(json.loads(raw.body), self.name)


class BybitSource:
    name = "bybit"
    weight_guard = None

    def __init__(self, symbol: str = "BTCUSDT", url: str = BYBIT_URL, limit: int = 1000):
        self.symbol, self.url, self.limit = symbol, url, limit

    async def fetch_raw(self, session: aiohttp.ClientSession, timeout_s: float) -> RawBook:
        body, _ = await _http_get(session, self.url, {"category": "linear", "symbol": self.symbol,
                                                      "limit": str(self.limit)}, timeout_s)
        return RawBook(self.name, body)

    def parse(self, raw: RawBook) -> Book:
        return parse_bybit(json.loads(raw.body))


class OkxSource:
    """OKX 永续。盘口单位是「张」, 面值从合约信息接口取一次并缓存(每天刷新), 不写死。"""
    name = "okx"
    weight_guard = None

    def __init__(self, symbol: str = "BTCUSDT", sz: int = OKX_SZ, books_url: str = OKX_BOOKS_URL,
                 instr_url: str = OKX_INSTR_URL, ct_ttl_s: float = 86400.0):
        self.inst_id = okx_inst_id(symbol)
        self.base = split_symbol(symbol)[0]
        self.sz, self.books_url, self.instr_url, self.ct_ttl_s = sz, books_url, instr_url, ct_ttl_s
        self._ct_val: float | None = None
        self._ct_at = 0.0

    async def ct_val(self, session: aiohttp.ClientSession, timeout_s: float) -> float:
        now = time.monotonic()
        if self._ct_val is None or now - self._ct_at > self.ct_ttl_s:
            try:
                body, _ = await _http_get(session, self.instr_url,
                                          {"instType": "SWAP", "instId": self.inst_id}, timeout_s)
                self._ct_val = parse_okx_ct_val(json.loads(body), self.inst_id, self.base)
                self._ct_at = now
            except Exception:
                if self._ct_val is None:
                    raise                       # 从没拿到过面值: 本源本轮失败, 不猜
                self._ct_at = now - self.ct_ttl_s + 600.0    # 沿用上次取到的值, 10 分钟后再刷
        return self._ct_val

    async def fetch_raw(self, session: aiohttp.ClientSession, timeout_s: float) -> RawBook:
        ct = await self.ct_val(session, timeout_s)
        body, _ = await _http_get(session, self.books_url,
                                  {"instId": self.inst_id, "sz": str(self.sz)}, timeout_s)
        return RawBook(self.name, body, ct_val=ct)

    def parse(self, raw: RawBook) -> Book:
        return parse_okx(json.loads(raw.body), raw.ct_val or 0.0)


def make_sources(names: Iterable[str] = ALL_SOURCES, symbol: str = "BTCUSDT") -> dict[str, Any]:
    """按名字造采集源对象。名字不认识直接报错(配置写错应当立刻暴露)。"""
    out: dict[str, Any] = {}
    for n in names:
        if n == "binance":
            out[n] = BinanceSource("binance", symbol, BINANCE_FUT_URL, 1000, weight_guard=1800)
        elif n == "binance_spot":
            out[n] = BinanceSource("binance_spot", symbol, BINANCE_SPOT_URL, 5000, weight_guard=4500)
        elif n == "bybit":
            out[n] = BybitSource(symbol)
        elif n == "okx":
            out[n] = OkxSource(symbol)
        else:
            raise ValueError(f"未知深度源: {n}")
    return out


def _err_text(e: BaseException) -> str:
    if isinstance(e, SourceError):
        return str(e)[:160]
    if isinstance(e, asyncio.TimeoutError):
        return "超时"
    return f"{type(e).__name__}: {e}"[:160]


async def fetch_raw_guarded(src: Any, session: aiohttp.ClientSession,
                            timeout_s: float) -> RawBook | Exception:
    """抓一个源; 永不抛异常(取消除外)。

    整个源(OKX 可能连发两个请求)的耗时硬上限 ≈ 1.25 × timeout_s, 到点即弃, 不拖累其它源。
    """
    try:
        return await asyncio.wait_for(src.fetch_raw(session, timeout_s), timeout_s * 1.25 + 0.1)
    except asyncio.TimeoutError:
        return SourceError(f"超时(>{timeout_s:g}s)")
    except Exception as e:                      # noqa: BLE001 - 各源必须隔离
        return e


async def fetch_books(session: aiohttp.ClientSession, sources: Mapping[str, Any] | Iterable[str] | None = None,
                      timeout_s: float = 8.0, symbol: str = "BTCUSDT") -> dict[str, Book | Exception]:
    """并发抓全部源并解析。返回 {源名: Book 或 异常}; 一个源失败/超时不影响其它源, 也不会抛。"""
    if sources is None:
        sources = make_sources(ALL_SOURCES, symbol)
    elif not isinstance(sources, Mapping):
        sources = make_sources(sources, symbol)
    names = list(sources)
    raws = await asyncio.gather(*(fetch_raw_guarded(sources[n], session, timeout_s) for n in names))
    out: dict[str, Book | Exception] = {}
    for n, raw in zip(names, raws):
        if isinstance(raw, Exception):
            out[n] = raw
            continue
        try:
            out[n] = sources[n].parse(raw)
        except Exception as e:                  # noqa: BLE001
            out[n] = e
    return out


async def _fetch_one(src: Any, session: aiohttp.ClientSession, timeout_s: float) -> Book:
    return src.parse(await src.fetch_raw(session, timeout_s))


async def fetch_binance_futures(session: aiohttp.ClientSession, symbol: str = "BTCUSDT",
                                limit: int = 1000, timeout_s: float = 8.0) -> Book:
    return await _fetch_one(BinanceSource("binance", symbol, BINANCE_FUT_URL, limit), session, timeout_s)


async def fetch_binance_spot(session: aiohttp.ClientSession, symbol: str = "BTCUSDT",
                             limit: int = 5000, timeout_s: float = 8.0) -> Book:
    return await _fetch_one(BinanceSource("binance_spot", symbol, BINANCE_SPOT_URL, limit), session, timeout_s)


async def fetch_bybit(session: aiohttp.ClientSession, symbol: str = "BTCUSDT",
                      limit: int = 1000, timeout_s: float = 8.0) -> Book:
    return await _fetch_one(BybitSource(symbol, limit=limit), session, timeout_s)


async def fetch_okx(session: aiohttp.ClientSession, symbol: str = "BTCUSDT",
                    sz: int = OKX_SZ, timeout_s: float = 8.0) -> Book:
    """含一次合约面值查询(张 → BTC)。长期轮询请复用 OkxSource 实例, 面值只查一次。"""
    return await _fetch_one(OkxSource(symbol, sz), session, timeout_s)


# ================================================================ 存储
_TABLES = {"raw": "depth_raw", "m5": "depth_m5"}
_SCHEMA = "".join(
    f"CREATE TABLE IF NOT EXISTS {t} ("
    "ts INTEGER PRIMARY KEY, mid REAL NOT NULL, n INTEGER NOT NULL, "
    "meta TEXT NOT NULL, payload BLOB NOT NULL);"
    for t in _TABLES.values()
)


class DepthStore:
    """深度快照的独立 sqlite 文件(WAL)。

    两张同构的表: depth_raw(原始快照, ts=采集时刻) 与 depth_m5(5 分钟均值, ts=窗口起点)。
    每次操作都开一条短连接、用完即关, 所以事件循环线程和 FastAPI 线程池可以随便并发调用,
    不共享任何连接状态; 写写冲突交给 sqlite 自己的 busy timeout。
    文件按需创建: 只读操作遇到文件不存在直接返回空, 不会顺手建库。
    """

    # 一行 ≈ 2KB。sqlite 默认 4KB 页一页只放得下一行(实测磁盘占用是内容的 1.9 倍);
    # 16KB 页一页放 7 行, 浪费降到 ~10%。只对新建的库生效。
    PAGE_SIZE = 16384

    def __init__(self, path: str | os.PathLike, rollup_s: int = ROLLUP_S):
        self.path = Path(path)
        self.rollup_ms = int(rollup_s) * 1000
        self._rolled_cur = -1                    # 已确认「此前的窗口都卷完了」的当前窗口起点
        self._ready = False
        self._init_lock = threading.Lock()

    # ---------- 连接 ----------
    def _ensure(self) -> None:
        if self._ready and self.path.exists():
            return
        with self._init_lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fresh = not self.path.exists() or self.path.stat().st_size == 0
            c = sqlite3.connect(self.path, timeout=15)
            try:
                if fresh:
                    c.execute(f"PRAGMA page_size={int(self.PAGE_SIZE)}")
                c.execute("PRAGMA journal_mode=WAL")
                c.executescript(_SCHEMA)
                c.commit()
            finally:
                c.close()
            self._ready = True

    @contextmanager
    def _conn(self, write: bool = False):
        if write:
            self._ensure()
        c = sqlite3.connect(self.path, timeout=15)
        try:
            if write:
                c.execute("PRAGMA synchronous=NORMAL")
            yield c
            if write:
                c.commit()
        finally:
            c.close()

    def _readable(self) -> bool:
        if not self.path.exists():
            return False
        if not self._ready:
            self._ensure()                       # 旧库缺表时补上(幂等)
        return True

    @staticmethod
    def _snap(row: tuple) -> Snapshot:
        ts, mid, n, meta, payload = row
        return Snapshot(ts, mid, n, json.loads(meta), blob=bytes(payload))

    # ---------- 写 ----------
    def write(self, snap: Snapshot, tier: str = "raw") -> int:
        """写入一行(同 ts 覆盖)。返回落盘字节数(meta + 压缩体)。"""
        meta, blob = snap.meta_json(), snap.blob()
        with self._conn(write=True) as c:
            c.execute(f"INSERT OR REPLACE INTO {_TABLES[tier]} (ts, mid, n, meta, payload) VALUES (?,?,?,?,?)",
                      (snap.ts, snap.mid, snap.n, meta, blob))
        return len(meta) + len(blob)

    def prune(self, keep_days: float, keep_raw_days: float | None = None, now_ms: int | None = None) -> int:
        """删掉 keep_days 天以前的行; 原始快照可以用 keep_raw_days 设更短的保留期。返回删除行数。"""
        if not self._readable():
            return 0
        now = int(time.time() * 1000) if now_ms is None else now_ms
        raw_days = keep_days if keep_raw_days is None else min(keep_days, keep_raw_days)
        with self._conn(write=True) as c:
            n = c.execute("DELETE FROM depth_raw WHERE ts < ?", (now - int(raw_days * 86400_000),)).rowcount
            n += c.execute("DELETE FROM depth_m5 WHERE ts < ?", (now - int(keep_days * 86400_000),)).rowcount
        return n

    def rollup_pending(self, now_ms: int | None = None, max_windows: int = 24) -> int:
        """把已经走完的 5 分钟窗口卷成均值行(当前窗口不动)。每次最多处理 max_windows 个, 返回处理数。

        从库里现算而不是在内存里攒, 进程重启后会自动把落下的窗口补上。
        """
        if not self._readable():
            return 0
        now = int(time.time() * 1000) if now_ms is None else now_ms
        w_ms = self.rollup_ms
        cur = now // w_ms * w_ms
        if cur == self._rolled_cur:               # 本窗口内已经追平过, 不必每 30 秒再查一遍库
            return 0
        done = 0
        with self._conn() as c:
            last = c.execute("SELECT MAX(ts) FROM depth_m5").fetchone()[0]
        lo = 0 if last is None else last + w_ms
        while done < max_windows:
            with self._conn() as c:
                nxt = c.execute("SELECT MIN(ts) FROM depth_raw WHERE ts >= ? AND ts < ?", (lo, cur)).fetchone()[0]
                if nxt is None:
                    break
                w0 = nxt // w_ms * w_ms
                rows = c.execute("SELECT ts, mid, n, meta, payload FROM depth_raw WHERE ts >= ? AND ts < ? "
                                 "ORDER BY ts", (w0, w0 + w_ms)).fetchall()
            merged = merge_snapshots([self._snap(r) for r in rows], w0)
            if merged is not None:
                self.write(merged, "m5")
            lo = w0 + w_ms
            done += 1
        if done < max_windows:
            self._rolled_cur = cur
        return done

    # ---------- 读 ----------
    def range(self, since_ms: int, until_ms: int | None = None, tier: str = "raw") -> list[Snapshot]:
        """[since_ms, until_ms] 内的行, 按时间升序。压缩体不解, 用到时才解。"""
        if not self._readable():
            return []
        hi = until_ms if until_ms is not None else 1 << 62
        with self._conn() as c:
            rows = c.execute(f"SELECT ts, mid, n, meta, payload FROM {_TABLES[tier]} "
                             "WHERE ts >= ? AND ts <= ? ORDER BY ts", (int(since_ms), int(hi))).fetchall()
        return [self._snap(r) for r in rows]

    def read_window(self, hours: float, now_ms: int | None = None) -> list[Snapshot]:
        """取最近 hours 小时构图用的行: <24h 读原始快照; ≥24h 读 5 分钟均值 + 尚未卷积的原始尾巴。"""
        now = int(time.time() * 1000) if now_ms is None else now_ms
        since = now - int(hours * 3600_000)
        if hours < ROLLUP_MIN_HOURS:
            return self.range(since, now, "raw")
        rows = self.range(since, now, "m5")
        tail_from = rows[-1].ts + self.rollup_ms if rows else since
        return rows + self.range(max(tail_from, since), now, "raw")

    def latest(self) -> Snapshot | None:
        """最新一张原始快照。"""
        if not self._readable():
            return None
        with self._conn() as c:
            row = c.execute("SELECT ts, mid, n, meta, payload FROM depth_raw ORDER BY ts DESC LIMIT 1").fetchone()
        return self._snap(row) if row else None

    def _first_of(self, raw_min: int | None, m5_min: int | None) -> int | None:
        """库里最早一条数据的时间。

        m5 行的 ts 是 5 分钟窗口的【起点】, 会比窗口里第一张快照早最多 5 分钟(刚开录十几秒就显示
        「已录 5 分钟」)。最早的原始快照就落在最早那个窗口里时(原始行还没被清理), 以原始快照为准。
        """
        if raw_min is None or m5_min is None:
            return raw_min if m5_min is None else m5_min
        if m5_min <= raw_min < m5_min + self.rollup_ms:
            return raw_min
        return min(raw_min, m5_min)

    def overview(self) -> tuple[Snapshot | None, int | None, int | None]:
        """(最新原始快照, 库里最早一条数据的时间, 最新原始快照的时间), 一条连接取完。"""
        if not self._readable():
            return None, None, None
        with self._conn() as c:
            row = c.execute("SELECT ts, mid, n, meta, payload FROM depth_raw ORDER BY ts DESC LIMIT 1").fetchone()
            a = c.execute("SELECT MIN(ts) FROM depth_raw").fetchone()[0]
            b = c.execute("SELECT MIN(ts) FROM depth_m5").fetchone()[0]
        return (self._snap(row) if row else None, self._first_of(a, b), row[0] if row else None)

    def count(self, tier: str = "raw") -> int:
        if not self._readable():
            return 0
        with self._conn() as c:
            return c.execute(f"SELECT COUNT(*) FROM {_TABLES[tier]}").fetchone()[0]

    def first_ts(self) -> int | None:
        """库里最早一行的时间(两档取更早者)。"""
        if not self._readable():
            return None
        with self._conn() as c:
            a = c.execute("SELECT MIN(ts) FROM depth_raw").fetchone()[0]
            b = c.execute("SELECT MIN(ts) FROM depth_m5").fetchone()[0]
        return self._first_of(a, b)

    def last_ts(self) -> int | None:
        if not self._readable():
            return None
        with self._conn() as c:
            return c.execute("SELECT MAX(ts) FROM depth_raw").fetchone()[0]

    def db_bytes(self) -> int:
        """库文件 + WAL 的磁盘占用。"""
        total = 0
        for p in (self.path, Path(str(self.path) + "-wal")):
            try:
                total += p.stat().st_size
            except OSError:
                pass
        return total


# ================================================================ 记录器
def _new_session() -> aiohttp.ClientSession:
    """记录器自用的干净会话(不带任何密钥头)。DNS 结果缓存 5 分钟, 省得每 30 秒解析四个域名。"""
    return aiohttp.ClientSession(connector=aiohttp.TCPConnector(limit=8, ttl_dns_cache=300))


class _SrcState:
    __slots__ = ("last_ok", "last_error", "last_error_ts", "fails", "errors", "cooldown_until", "weight")

    def __init__(self):
        self.last_ok: int | None = None
        self.last_error: str | None = None       # 最近一次成功后清空 → 非空即「此刻有问题」
        self.last_error_ts: int | None = None
        self.fails = 0                           # 连续失败次数
        self.errors = 0                          # 累计失败次数
        self.cooldown_until = 0                  # ms; 在此之前跳过该源
        self.weight: int | None = None


class DepthRecorder:
    """后台深度记录器: 每 interval_s 抓一轮四个源的盘口, 分桶压缩写库。

    - run() 永远循环、永不抛异常(任务被取消时照常传播 CancelledError 以便关停)。
    - 各源隔离: 单源失败/超时只影响它自己; 全部失败则本轮不写。
    - 解析+分桶+写库默认丢到专用单线程里跑(offload=True), 事件循环只做网络 I/O。
    - 对交易所克制: 429/418 至少停 60s/600s; 币安响应头显示本 IP 权重吃紧时主动停 60s;
      连续失败按指数退避(最长 10 分钟), 被地区封锁的源不会被每 30 秒敲一次。
    - 不要把实盘引擎那条带 API key 头的会话传进来(会被拒绝); 缺省自建一条干净会话。
    """

    def __init__(self, store: DepthStore, session: aiohttp.ClientSession | None = None, *,
                 symbol: str = "BTCUSDT", sources: Mapping[str, Any] | Iterable[str] | None = None,
                 interval_s: float = 30.0, timeout_s: float = 8.0,
                 bucket_usd: float = BUCKET_USD, half_range_pct: float = HALF_RANGE_PCT,
                 keep_days: float = 8.0, keep_raw_days: float = 2.0, offload: bool = True):
        self.store = store
        self.symbol = symbol
        if sources is None:
            sources = make_sources(ALL_SOURCES, symbol)
        elif not isinstance(sources, Mapping):
            sources = make_sources(sources, symbol)
        self.sources: dict[str, Any] = dict(sources)
        self.interval_s = float(interval_s)
        self.timeout_s = float(timeout_s)
        self.bucket_usd = float(bucket_usd)
        self.half_range_pct = float(half_range_pct)
        self.keep_days = keep_days
        self.keep_raw_days = keep_raw_days
        self.offload = offload

        self._session = session
        self._state = {n: _SrcState() for n in self.sources}
        self._pool: ThreadPoolExecutor | None = None
        self._stop: asyncio.Event | None = None
        self._stopping = False
        self._running = False
        self._started_ms: int | None = None
        self._count: int | None = None           # 库内原始快照数(内存计数, 避免每次请求扫表)
        self._iterations = 0
        self._last_snapshot_ms: int | None = None
        self._last_prune = 0.0
        self._last_stats: dict = {}
        self._max_compute_ms = 0.0
        self._loop_error: str | None = None

    # ---------- 对外状态(纯内存, 任何线程可调) ----------
    def health(self) -> dict:
        """/api/flow/depth 里 health 字段的原样内容。四个源的键永远都在。"""
        return {
            "last_ok": {n: (self._state[n].last_ok if n in self._state else None) for n in ALL_SOURCES},
            "last_error": {n: (self._state[n].last_error if n in self._state else None) for n in ALL_SOURCES},
            "snapshots": int(self._count or 0),
        }

    def status(self) -> dict:
        """/api/flow/health 的 recorder 字段: health() 加运行细节。"""
        now = int(time.time() * 1000)
        out = self.health()
        out.update({
            "running": self._running,
            "symbol": self.symbol,
            "sources": list(self.sources),
            "interval_s": self.interval_s,
            "started": self._started_ms,
            "iterations": self._iterations,
            "last_snapshot": self._last_snapshot_ms,
            "last_cpu_ms": self._last_stats.get("cpu_ms"),          # 线程 CPU 时间(整轮处理)
            "last_compute_ms": self._last_stats.get("compute_ms"),  # 解析+分桶+压缩 的墙钟
            "last_write_ms": self._last_stats.get("write_ms"),      # sqlite 写入+卷积 的墙钟
            "max_compute_ms": round(self._max_compute_ms, 1),
            "last_bytes": self._last_stats.get("bytes"),
            "fails": {n: s.fails for n, s in self._state.items()},
            "errors": {n: s.errors for n, s in self._state.items()},
            "cooldown_s": {n: max(0, round((s.cooldown_until - now) / 1000)) for n, s in self._state.items()},
            "weight": {n: s.weight for n, s in self._state.items() if s.weight is not None},
            "loop_error": self._loop_error,
            "db_bytes": self.store.db_bytes(),
        })
        return out

    # ---------- 线程里跑的重活 ----------
    def _process(self, ts: int, raws: dict[str, RawBook]) -> dict:
        """解析 + 分桶 + 互校 + 编码 + 写库 + 卷积。同步函数, 不碰网络。"""
        c0, t0 = time.thread_time(), time.perf_counter()
        depths: dict[str, Bucketed] = {}
        errors: dict[str, str] = {}
        for name, raw in raws.items():
            try:
                book = self.sources[name].parse(raw)
                depths[name] = bucket_book(book, self.bucket_usd, self.half_range_pct)
            except Exception as e:              # noqa: BLE001
                errors[name] = _err_text(e)
        for name, dev in check_mids({n: d.mid for n, d in depths.items()}).items():
            errors[name] = f"中间价偏离其它源 {dev:.1f}%, 已丢弃"
            del depths[name]
        nbytes = 0
        snap = None
        if depths:
            snap = make_snapshot(ts, depths, self.bucket_usd)
            snap.blob()
        t1 = time.perf_counter()
        if snap is not None:
            nbytes = self.store.write(snap)
            self.store.rollup_pending(ts)
        t2 = time.perf_counter()
        return {"ok": list(depths), "errors": errors, "bytes": nbytes, "written": snap is not None,
                "compute_ms": round((t1 - t0) * 1000, 2), "write_ms": round((t2 - t1) * 1000, 2),
                "cpu_ms": round((time.thread_time() - c0) * 1000, 2)}

    def _housekeeping(self) -> int:
        self.store.prune(self.keep_days, self.keep_raw_days)
        return self.store.count()

    async def _off(self, fn, *args):
        if not self.offload:
            return fn(*args)
        if self._pool is None:
            self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="flow-depth-rec")
        return await asyncio.get_running_loop().run_in_executor(self._pool, fn, *args)

    # ---------- 一轮 ----------
    def _fail(self, name: str, text: str, now_ms: int, exc: BaseException | None = None) -> None:
        st = self._state[name]
        st.fails += 1
        st.errors += 1
        if st.last_error != text:
            log.warning("深度源 %s 失败: %s", name, text)
        st.last_error, st.last_error_ts = text, now_ms
        if isinstance(exc, RateLimited):
            wait = max(exc.retry_after, 600.0 if exc.status == 418 else 60.0)
        elif st.fails >= 3:
            wait = min(600.0, self.interval_s * 2 ** (st.fails - 2))
        else:
            return
        st.cooldown_until = now_ms + int(wait * 1000)

    async def snapshot_once(self, now_ms: int | None = None) -> dict:
        """抓一轮并写库。返回本轮统计(ok 源、错误、字节数、CPU 毫秒…)。单源问题不会抛。"""
        ts = int(time.time() * 1000) if now_ms is None else int(now_ms)
        own = None
        if self._session is None:
            own = self._session = _new_session()
        try:
            if self._count is None:
                self._count = await self._off(self.store.count)
            active = [n for n, st in self._state.items() if st.cooldown_until <= ts]
            got = await asyncio.gather(*(fetch_raw_guarded(self.sources[n], self._session, self.timeout_s)
                                         for n in active))
            raws = {n: r for n, r in zip(active, got) if isinstance(r, RawBook)}
            fetch_errors: dict[str, str] = {}
            for n, r in zip(active, got):
                if isinstance(r, Exception):
                    fetch_errors[n] = _err_text(r)
                    self._fail(n, fetch_errors[n], ts, r)
            stats = {"ok": [], "errors": {}, "bytes": 0, "written": False,
                     "compute_ms": 0.0, "write_ms": 0.0, "cpu_ms": 0.0}
            if raws:
                stats = await self._off(self._process, ts, raws)
            for n, text in stats["errors"].items():
                self._fail(n, text, ts)
            for n in stats["ok"]:
                st = self._state[n]
                if st.fails:
                    log.info("深度源 %s 恢复", n)
                st.last_ok, st.last_error, st.fails = ts, None, 0
                st.weight = raws[n].weight
                guard = getattr(self.sources[n], "weight_guard", None)
                if guard and st.weight is not None and st.weight >= guard:
                    st.cooldown_until = ts + 60_000
                    st.last_error = f"币安 IP 权重 {st.weight}≥{guard}, 让路 60s"
                    log.warning("深度源 %s: %s", n, st.last_error)
            if stats["written"]:
                self._last_snapshot_ms = ts
                self._count = (self._count or 0) + 1
            self._iterations += 1
            self._last_stats = stats
            self._max_compute_ms = max(self._max_compute_ms, stats["compute_ms"])
            return dict(stats, ts=ts, errors={**fetch_errors, **stats["errors"]},
                        skipped=[n for n in self._state if n not in active])
        finally:
            if own is not None:
                await own.close()
                self._session = None

    # ---------- 主循环 ----------
    def stop(self) -> None:
        """请求 run() 在当前这一轮结束后退出。"""
        self._stopping = True
        if self._stop is not None:
            self._stop.set()

    async def _sleep(self) -> None:
        # 对齐到墙上时钟的 interval 整数倍(略过一点, 保证时间戳落在边界之后) → 列分箱稳定、不漂移
        now = time.time()
        delay = self.interval_s - (now % self.interval_s) + min(0.05, self.interval_s / 10)
        try:
            await asyncio.wait_for(self._stop.wait(), delay)
        except asyncio.TimeoutError:
            pass

    async def run(self) -> None:
        self._stop = asyncio.Event()
        if self._stopping:
            self._stop.set()
        self._running = True
        self._started_ms = int(time.time() * 1000)
        self._last_prune = time.monotonic()
        own = None
        try:
            if self._session is None:
                own = self._session = _new_session()
            while not self._stop.is_set():
                try:
                    if own is not None and own.closed:      # 自建的会话意外被关: 重建, 不要从此全灭
                        own = self._session = _new_session()
                    await self.snapshot_once()
                    # 每小时清理一次过期行(启动满一小时才第一次清理: 先让卷积把落下的窗口补完)
                    if time.monotonic() - self._last_prune >= 3600.0:
                        self._count = await self._off(self._housekeeping)
                        self._last_prune = time.monotonic()
                    self._loop_error = None
                except asyncio.CancelledError:
                    raise
                except Exception as e:              # noqa: BLE001 - 记录器绝不能把异常带出去
                    text = _err_text(e)
                    if text != self._loop_error:        # 同一个错(比如磁盘满)不要每 30 秒刷一屏堆栈
                        log.exception("深度记录器本轮异常")
                    self._loop_error = text
                await self._sleep()
        finally:
            self._running = False
            if own is not None:
                self._session = None
                try:
                    await own.close()
                except Exception:                   # noqa: BLE001
                    pass
            self.close()

    def close(self) -> None:
        """回收处理线程(run() 退出时自动调用; 单独用 snapshot_once 的脚本收尾时调一下)。"""
        if self._pool is not None:
            self._pool.shutdown(wait=False)
            self._pool = None


# ================================================================ 热力图
# 行高只取记录桶的这些整数倍 → 行边界永远落在记录桶边界上, 变粗时总量不变, 刻度也是整数
_LADDER = (1, 2, 3, 4, 5, 6, 8, 10, 12, 15, 20, 25, 30, 40, 50, 60, 80, 100,
           120, 150, 200, 250, 300, 400, 500, 600, 800, 1000)


def parse_sources(src: str | Iterable[str] | None) -> tuple[str, ...]:
    """'all' / None / 逗号串 / 列表 → 规范顺序的源名元组。不认识的名字忽略; 一个都不认识则当作 all。"""
    if src is None:
        return ALL_SOURCES
    names = src.split(",") if isinstance(src, str) else list(src)
    want = {str(n).strip().lower() for n in names}
    if "all" in want:
        return ALL_SOURCES
    sel = tuple(n for n in ALL_SOURCES if n in want)
    return sel or ALL_SOURCES


def choose_grid(lo: float, hi: float, base: float, max_rows: int) -> tuple[float, float, int, int]:
    """给价格窗口 [lo, hi) 选行高: 记录桶 base 的最小整数倍 k, 使行数 ≤ max_rows。

    返回 (行高, p0=第 0 行下沿, 行数, k)。p0 是行高的整数倍。
    """
    max_rows = max(1, int(max_rows))
    ks = list(_LADDER)
    k_exact = max(1, math.ceil((hi - lo) / (base * max_rows)))
    ks += [k for k in (k_exact, k_exact + 1, k_exact + 2) if k > ks[-1]]
    for k in ks:
        out = base * k
        i_lo = math.floor(lo / out + _EPS)
        i_hi = max(i_lo + 1, math.ceil(hi / out - _EPS))
        if i_hi - i_lo <= max_rows:
            return out, i_lo * out, i_hi - i_lo, k
    out = base * ks[-1]
    i_lo = math.floor(lo / out + _EPS)
    return out, i_lo * out, max_rows, ks[-1]


def _fold(acc: list, i0: int, q: list, g0: int, k: int, rows: int, w: float) -> None:
    """把一段稠密数量 q(记录桶, 起始索引 i0)每 k 个桶并成一行, 乘权重 w 累加进 acc。

    acc 长 rows, 第 0 行下沿对应记录桶索引 g0。窗口外的部分裁掉。
    """
    off = i0 - g0
    if off < 0:
        q = q[-off:]
        off = 0
    over = off + len(q) - rows * k
    if over > 0:
        if over >= len(q):
            return
        q = q[:len(q) - over]
    if not q:
        return
    r = off // k
    if k == 1:
        for v in q:
            if v:
                acc[r] += v * w
            r += 1
        return
    lead = off % k
    if lead:
        head = k - lead
        acc[r] += sum(q[:head]) * w
        r += 1
        q = q[head:]
    for j in range(0, len(q), k):
        acc[r] += sum(q[j:j + k]) * w
        r += 1


def _fold_generic(acc: list, i0: int, q: list, bk: float, p0: float, out: float, rows: int, w: float) -> None:
    """桶宽与网格不成整数倍时(中途改过记录桶宽)的兜底: 逐桶按下沿价格归行。"""
    for j, v in enumerate(q):
        if v:
            r = math.floor(((i0 + j) * bk - p0) / out + _EPS)
            if 0 <= r < rows:
                acc[r] += v * w


def _fold_snapshot_side(acc: list, snap: Snapshot, name: str, side: int, qs: list,
                        base: float, p0: float, out: float, k: int, rows: int, w: float) -> None:
    i0 = snap.sources[name]["b" if side == 0 else "a"][0]
    if snap.bucket == base:
        _fold(acc, i0, qs, round(p0 / base), k, rows, w)
    else:
        _fold_generic(acc, i0, qs, snap.bucket, p0, out, rows, w)


def _profile(snap: Snapshot, sel: tuple[str, ...], base: float, p0: float, out: float,
             k: int, rows: int) -> tuple[list[float], list[float]]:
    """一张快照在给定网格上的买/卖深度(各选中源相加)。"""
    bids, asks = [0.0] * rows, [0.0] * rows
    body = snap.body()
    w = 1.0 / snap.unit
    for name in sel:
        if name in snap.sources and name in body:
            _fold_snapshot_side(bids, snap, name, 0, body[name][0], base, p0, out, k, rows, w)
            _fold_snapshot_side(asks, snap, name, 1, body[name][1], base, p0, out, k, rows, w)
    return bids, asks


def _latest_block(snap: Snapshot, sel: tuple[str, ...], base: float, p0: float, out: float,
                  k: int, rows: int) -> dict:
    bids, asks = _profile(snap, sel, base, p0, out, k, rows)
    mid = ref_mid(snap.sources, sel)
    return {"ts": snap.t, "mid": round(mid if mid is not None else snap.mid, 2),
            "prices": [round(p0 + i * out, 2) for i in range(rows)],
            "bids": [round(v, 2) or 0 for v in bids],
            "asks": [round(v, 2) or 0 for v in asks]}


def _default_health(snapshots: int = 0) -> dict:
    return {"last_ok": {n: None for n in ALL_SOURCES},
            "last_error": {n: None for n in ALL_SOURCES},
            "snapshots": int(snapshots)}


def empty_heatmap(symbol: str = "BTCUSDT", health: dict | None = None,
                  recorded_minutes: float = 0.0, bucket_usd: float = BUCKET_USD) -> dict:
    """还没有数据时的载荷(HTTP 200, 不是错误)。"""
    return {"symbol": symbol, "bucket": float(bucket_usd), "p0": 0.0, "rows": 0,
            "times": [], "mid": [], "z": [], "zmax": 0.0, "sources": [],
            "coverage": {n: 0.0 for n in ALL_SOURCES}, "latest": None,
            "recorded_minutes": round(float(recorded_minutes), 1),
            "health": health if health is not None else _default_health()}


def _typical_step(ts: list[int]) -> int:
    """相邻行时间差的中位数, 取整到秒(ms)。不足两行返回 0。"""
    if len(ts) < 2:
        return 0
    return int(round(median(b - a for a, b in zip(ts, ts[1:])) / 1000.0)) * 1000


def build_heatmap(rows: Iterable[Snapshot], hours: float = 6, sources: str | Iterable[str] | None = None,
                  max_cols: int = 360, max_rows: int = 220, *, symbol: str = "BTCUSDT",
                  half_range_pct: float | None = None, health: dict | None = None,
                  recorded_minutes: float | None = None) -> dict:
    """把一段深度记录拼成 /api/flow/depth 的载荷。纯计算, 不碰库也不碰网络。

    rows: DepthStore.range()/read_window() 的结果(原始快照、均值行可以混用)。
    时间: 以最后一行为右端, 只取最近 hours 小时; 列宽 = 快照间隔的整数倍, 且对齐到绝对时间
          (每次刷新同一批快照总落在同一列, 画面不抖); times[i] 是该列的起始时刻;
          没有数据的列不输出(记录器停过的时段在 times 里表现为跳变)。
    列内: 每个源在它在场的快照上取【均值】再跨源相加。取均值而不是取最后一张, 是因为一列可能压了
          几十张快照 —— 取最后一张会把恰好落在采样点上的闪单画成一整列的墙, 也会把持续大半列的墙漏掉。
    价格: 窗口 = 选中源实际记录到的价格范围(可用 half_range_pct 按中间价再收窄);
          行高取记录桶的整数倍使行数 ≤ max_rows。
    """
    sel = parse_sources(sources)
    max_cols, max_rows = max(1, int(max_cols)), max(1, int(max_rows))
    snaps = sorted(rows, key=lambda s: s.t)
    if health is None:
        health = _default_health(sum(1 for s in snaps if s.n == 1))
    if recorded_minutes is None:
        recorded_minutes = (snaps[-1].t - snaps[0].t) / 60000.0 if snaps else 0.0
    coverage = {n: 0.0 for n in ALL_SOURCES}
    usable = [s for s in snaps if any(n in s.sources for n in sel)]
    if not usable:
        return empty_heatmap(symbol, health, recorded_minutes)

    span_ms = max(1, int(float(hours) * 3600_000))
    t_end = usable[-1].t
    usable = [s for s in usable if s.t > t_end - span_ms]

    # ---- 时间分箱 ----
    step = _typical_step([s.t for s in usable])
    col_ms = max(1000, math.ceil(span_ms / max_cols))
    if step > 0:
        col_ms = max(step, math.ceil(col_ms / step - _EPS) * step)
    n_bins = min(max_cols, math.ceil(span_ms / col_ms))
    first_bin = t_end // col_ms - n_bins + 1
    usable = [s for s in usable if s.t // col_ms >= first_bin]

    # ---- 第一遍(只看 meta): 价格窗口 / 覆盖范围 / 记录桶 ----
    lo, hi = math.inf, -math.inf
    mids: list[float] = []
    reach: dict[str, list[float]] = {n: [] for n in ALL_SOURCES}
    base = max(s.bucket for s in usable)
    for s in usable:
        bk = s.bucket
        for name, m in s.sources.items():
            if name in reach:
                reach[name].append((m["r"][0] + m["r"][1]) / 2.0)
            if name not in sel:
                continue
            (b_i0, b_len), (a_i0, a_len) = m["b"], m["a"]
            if b_len:
                lo = min(lo, b_i0 * bk)
                hi = max(hi, (b_i0 + b_len) * bk)
            if a_len:
                lo = min(lo, a_i0 * bk)
                hi = max(hi, (a_i0 + a_len) * bk)
        mids.append(ref_mid(s.sources, sel))
    for name, vals in reach.items():
        if vals:
            coverage[name] = round(median(vals), 2)
    if half_range_pct is not None and mids:
        lo = max(lo, min(mids) * (1 - half_range_pct / 100.0))
        hi = min(hi, max(mids) * (1 + half_range_pct / 100.0))
    if not (lo < hi):                              # 选中源在场但一个桶都没有(极端脏数据)
        return dict(empty_heatmap(symbol, health, recorded_minutes), coverage=coverage)
    out_bucket, p0, n_rows, k = choose_grid(lo, hi, base, max_rows)

    # ---- 第二遍: 逐行解压, 折进当前列; 换列时结算 ----
    times: list[int] = []
    mid_out: list[float] = []
    z: list[list] = []
    included: set[str] = set()
    cur_bin = None
    acc: dict[str, list] = {}
    cnt: dict[str, int] = {}
    mid_sum = 0.0
    n_sum = 0

    def finish() -> None:
        col = [0.0] * n_rows
        for name, a in acc.items():
            inv = 1.0 / cnt[name]
            col = [c + v * inv for c, v in zip(col, a)]
        z.append([round(v, 1) or 0 for v in col])
        times.append(int(cur_bin * col_ms))
        mid_out.append(round(mid_sum / n_sum, 2))

    for s, mid in zip(usable, mids):
        b = s.t // col_ms
        if b != cur_bin:
            if cur_bin is not None:
                finish()
            cur_bin, acc, cnt = b, {}, {}
            mid_sum = 0.0
            n_sum = 0
        body = s.body()
        unit = s.unit
        for name in sel:
            m = s.sources.get(name)
            if not m or name not in body:
                continue
            n_s = int(m.get("n", 1))
            if name not in acc:
                acc[name], cnt[name] = [0.0] * n_rows, 0
                included.add(name)
            w = n_s / unit
            _fold_snapshot_side(acc[name], s, name, 0, body[name][0], base, p0, out_bucket, k, n_rows, w)
            _fold_snapshot_side(acc[name], s, name, 1, body[name][1], base, p0, out_bucket, k, n_rows, w)
            cnt[name] += n_s
        mid_sum += mid * s.n
        n_sum += s.n
    if cur_bin is not None:
        finish()

    nz = sorted(v for col in z for v in col if v)
    zmax = float(nz[max(0, math.ceil(len(nz) * 0.99) - 1)]) if nz else 0.0

    return {
        "symbol": symbol,
        "bucket": float(out_bucket),
        "p0": float(p0),
        "rows": n_rows,
        "times": times,
        "mid": mid_out,
        "z": z,
        "zmax": zmax,
        "sources": [n for n in ALL_SOURCES if n in included],
        "coverage": coverage,
        "latest": _latest_block(usable[-1], sel, base, p0, out_bucket, k, n_rows),
        "recorded_minutes": round(float(recorded_minutes), 1),
        "health": health,
    }


# ================================================================ 带缓存的请求入口
def normalize_query(hours: Any = 6, src: Any = "all", cols: Any = 360) -> tuple[int, tuple[str, ...], int]:
    """把公开接口的参数收敛到有限集合(缓存键空间有界, 陌生人没法用怪参数逼着进程反复重算)。

    hours → {1,6,24,72,168} 里最接近的; cols → 60 的整数倍并夹在 [60, 360]; src → 规范源元组。
    """
    try:
        h = float(hours)
    except (TypeError, ValueError):
        h = 6.0
    h = min(ALLOWED_HOURS, key=lambda a: abs(a - h))
    try:
        c = int(cols)
    except (TypeError, ValueError):
        c = 360
    c = max(60, min(360, c // 60 * 60))
    return h, parse_sources(src), c


def _heavy_ttl_s(hours: int, cols: int) -> float:
    """矩阵部分的缓存时长: 约为列宽的 1/5, 夹在 [20s, 300s]。7 天图一列半小时, 没必要每 20 秒重算。"""
    return max(20.0, min(300.0, hours * 3600.0 / cols / 5.0))


class _Heavy:
    """缓存的矩阵部分。gz 在第一次有人要 gzip 时才生成。"""
    __slots__ = ("expires", "blob", "grid", "count", "gz", "light")

    def __init__(self, expires: float, blob: bytes, grid: tuple, count: int):
        self.expires = expires
        self.blob = blob            # 序列化好的 JSON, 去掉了结尾的 '}', 等着接上轻的部分
        self.grid = grid            # (行高, p0, 行数)
        self.count = count          # 库内快照数(没有记录器时 health.snapshots 用)
        self.gz = None              # (已压缩的头部字节, 停在同步点上的压缩器)
        self.light = (0.0, b"")     # (过期时刻, 轻的部分的 JSON 尾巴)


class _HeatCache:
    """/api/flow/depth 的两级缓存(线程安全, 同一时刻只有一个线程在构图)。

    重的部分(times/mid/z…)按参数缓存 20s~300s, 存序列化好的 JSON 字节(一份 ≤ ~0.4MB);
    轻的部分(latest/health/recorded_minutes)每 2 秒按最新快照重算, 接在后面拼成完整载荷。
    gzip 版: 重的部分只压一次, 压缩器停在同步点; 每次只需复制压缩器把那一小段尾巴压进去,
    得到的仍是一条合法的单成员 gzip 流 —— 不必为了换几 KB 的尾巴重压几百 KB。
    """

    def __init__(self, max_entries: int = 12, light_ttl_s: float = 2.0, gzip_level: int = 5):
        self.max_entries = max_entries
        self.light_ttl_s = light_ttl_s
        self.gzip_level = gzip_level
        self.builds = 0                              # 实际构图次数(测试/排查用)
        self._lock = threading.Lock()
        self._heavy: OrderedDict = OrderedDict()     # key -> _Heavy
        self._full: dict = {}                        # (key, 是否 gzip) -> (过期时刻, 完整响应体)

    def clear(self) -> None:
        with self._lock:
            self._heavy.clear()
            self._full.clear()

    def _light_tail(self, heavy: _Heavy, store: DepthStore, sel: tuple[str, ...],
                    recorder: DepthRecorder | None) -> bytes:
        out_bucket, p0, n_rows = heavy.grid
        latest = None
        last, first, last_ts = store.overview()
        if last is not None and n_rows:
            base = last.bucket
            k = max(1, round(out_bucket / base))
            if abs(k * base - out_bucket) > 1e-6:      # 网格不是该快照桶宽的整数倍 → 走逐桶兜底
                base = out_bucket
            latest = _latest_block(last, sel, base, p0, out_bucket, k, n_rows)
        minutes = (last_ts - first) / 60000.0 if first is not None and last_ts is not None else 0.0
        health = recorder.health() if recorder is not None else _default_health(heavy.count)
        light = {"latest": latest, "recorded_minutes": round(max(0.0, minutes), 1), "health": health}
        return b"," + json.dumps(light, separators=(",", ":")).encode()[1:]

    def get(self, store: DepthStore, hours: int, sel: tuple[str, ...], cols: int, max_rows: int,
            recorder: DepthRecorder | None, symbol: str, now_ms: int | None, gzip: bool = False) -> bytes:
        key = (str(store.path), hours, sel, cols, max_rows)
        fkey = (key, bool(gzip))
        hit = self._full.get(fkey)
        if hit and hit[0] > time.monotonic():
            return hit[1]
        with self._lock:
            mono = time.monotonic()
            hit = self._full.get(fkey)
            if hit and hit[0] > mono:
                return hit[1]
            heavy = self._heavy.get(key)
            if heavy is None or heavy.expires <= mono:
                p = build_heatmap(store.read_window(hours, now_ms), hours, sel, cols, max_rows, symbol=symbol)
                self.builds += 1
                grid = (p["bucket"], p["p0"], p["rows"])
                for name in ("latest", "health", "recorded_minutes"):
                    p.pop(name)
                blob = json.dumps(p, separators=(",", ":")).encode()[:-1]
                heavy = _Heavy(mono + _heavy_ttl_s(hours, cols), blob, grid,
                               store.count() if recorder is None else 0)
                self._heavy[key] = heavy
                while len(self._heavy) > self.max_entries:
                    old, _ = self._heavy.popitem(last=False)
                    self._full.pop((old, False), None)
                    self._full.pop((old, True), None)
            self._heavy.move_to_end(key)
            if heavy.light[0] <= mono:
                heavy.light = (mono + self.light_ttl_s, self._light_tail(heavy, store, sel, recorder))
            expires, tail = heavy.light
            if gzip:
                if heavy.gz is None:
                    co = zlib.compressobj(self.gzip_level, zlib.DEFLATED, 31)      # 31 = gzip 容器
                    heavy.gz = (co.compress(heavy.blob) + co.flush(zlib.Z_SYNC_FLUSH), co)
                gz_head, co = heavy.gz
                c2 = co.copy()
                full = gz_head + c2.compress(tail) + c2.flush()
            else:
                full = heavy.blob + tail
            self._full[fkey] = (expires, full)
            if len(self._full) > self.max_entries * 4:
                for kk in [kk for kk, v in self._full.items() if v[0] <= mono]:
                    del self._full[kk]
            return full


_CACHE = _HeatCache()
_HEAT_POOL: ThreadPoolExecutor | None = None


def heatmap_cache_clear() -> None:
    _CACHE.clear()


def heatmap_json(store: DepthStore, hours: Any = 6, src: Any = "all", cols: Any = 360, *,
                 recorder: DepthRecorder | None = None, gzip: bool = False, max_rows: int = 220,
                 symbol: str = "BTCUSDT", now_ms: int | None = None) -> bytes:
    """/api/flow/depth 的完整响应体(已序列化的 JSON 字节, 带缓存)。只读本地库, 不发任何网络请求。

    gzip=True 返回 gzip 压缩后的字节(实测 6 小时全源图 339KB → ~105KB), 路由需自己加响应头
    Content-Encoding: gzip, 并只在请求的 Accept-Encoding 含 gzip 时这样做。

    同步函数, 缓存未命中时要几十到几百毫秒 CPU: 请在线程里调用
    (FastAPI 的普通 def 路由本来就在线程池里; async 路由用 heatmap_json_async)。
    路由里直接 Response(content=..., media_type="application/json") 返回,
    别把 8 万个数的字典再交给 FastAPI 逐个编码。
    """
    h, sel, c = normalize_query(hours, src, cols)
    return _CACHE.get(store, h, sel, c, int(max_rows), recorder, symbol, now_ms, gzip)


def heatmap_payload(store: DepthStore, hours: Any = 6, src: Any = "all", cols: Any = 360, **kw) -> dict:
    """同 heatmap_json, 返回字典(每次调用多一次 json.loads, 约几毫秒)。"""
    kw.pop("gzip", None)
    return json.loads(heatmap_json(store, hours, src, cols, **kw))


async def heatmap_json_async(store: DepthStore, hours: Any = 6, src: Any = "all", cols: Any = 360, *,
                             recorder: DepthRecorder | None = None, gzip: bool = False,
                             max_rows: int = 220, symbol: str = "BTCUSDT") -> bytes:
    """给 async 路由用: 在专用单线程里构图, 不占事件循环, 也不挤占默认线程池(DNS 解析在那里)。"""
    global _HEAT_POOL
    if _HEAT_POOL is None:
        _HEAT_POOL = ThreadPoolExecutor(max_workers=1, thread_name_prefix="flow-depth-heat")
    return await asyncio.get_running_loop().run_in_executor(
        _HEAT_POOL, lambda: heatmap_json(store, hours, src, cols, recorder=recorder, gzip=gzip,
                                         max_rows=max_rows, symbol=symbol))
