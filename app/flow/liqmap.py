"""清算热力图(估算)：从【持仓量增量 × 假设的杠杆分布】推算强平位堆在哪（2026-11-16）。

注意：这是模型估算，不是交易所数据。交易所不公开每个仓位的强平价，CoinGlass 的清算
热力图也是同一类推算。每条假设都写进 payload 的 assumptions.note，前端必须把
"估算"挂在图上，别让人当成真实挂单。

模型（逐根 15m K 线，严格只用当根及之前的数据，无前视）：
  1. 扫单：本根最低价触及的多头强平位、最高价触及的空头强平位 → 视为已清算，移除。
     只扫【之前的 K 线】留下的档位；当根新开的仓从下一根起才可能被扫（根内先后未知）。
  2. OI 上升 dOI：每张合约必有一多一空，所以是【dOI 的新多】加【dOI 的新空】，
     不是各一半。入场价取当根典型价 (高+低+收)/3，按 LEVERAGE_TIERS 权重分到各杠杆档：
         多头强平价 = 入场 × (1 - 1/杠杆 + MMR)
         空头强平价 = 入场 × (1 + 1/杠杆 - MMR)
  3. OI 下降 D：有 D 的多和 D 的空平掉了。
     - 本根已被第 1 步扫掉的量先抵扣（被清算本身就是平仓，再衰减一次就重复计算了）；
     - 剩余降幅 = max(0, D - 本侧被扫量)，按"每张存量合约被平掉的概率相同"处理：
           本侧整池 × (1 - 剩余降幅 / (上一根的OI - 本侧被扫量))
       分母用【全市场 OI】而不是池子自身总量：窗口开始前就存在的老仓我们不知道入场价
       （不在池里），但它们同样在平仓。若拿池子总量当分母，等于假定平掉的全是最近新开
       的仓，一次去杠杆就把整张图清空，明显失真。
       由此得到一个可检验的不变量：任一侧池子总量永远 ≤ 当时的真实 OI。

结构：
  纯计算（不碰网络，可离线单测）：LiqPool / simulate / find_clusters / merge_bars / build_payload
  取数（注入 aiohttp session）：fetch_klines / fetch_oi_hist / fetch_oi_now
  缓存：refresh()（后台每 5 分钟调）＋ get_cached()/get_cached_json()（请求路径，永不触网）
"""
from __future__ import annotations

import asyncio
import gzip
import heapq
import json
import logging
import math
import time
from bisect import bisect_left
from typing import Iterable, NamedTuple, Sequence

import aiohttp

log = logging.getLogger("flow.liqmap")

# ───────────────────────── 常量（都会原样回传到 assumptions） ─────────────────────────
SYMBOL = "BTCUSDT"
TF = "15m"
TF_MS = 15 * 60 * 1000
DAY_MS = 86_400_000

# (杠杆, 权重)。权重和为 1：假设【每一张】新开合约都落在这五档里。
# 这是主观设定，交易所不公开真实杠杆分布；低杠杆档(5x/10x)的强平位离价格 10%~20%，
# 天然落在近端视野之外，相当于"低杠杆/对冲仓不构成近端清算压力"。
LEVERAGE_TIERS: tuple[tuple[int, float], ...] = (
    (5, 0.15), (10, 0.30), (25, 0.30), (50, 0.15), (100, 0.10),
)

# 维持保证金率：币安 BTCUSDT 永续第一档（名义 0~30 万 USDT）= 0.40%。
# 2026-11-16 用币安官网杠杆分层接口核对过（bracketMaintenanceMarginRate=0.004）。
# 大仓位档位更高（0.5%~5%+），这里统一按第一档算。
MMR = 0.004

BASE_BUCKET = 50.0            # 价格行的基础高度(USD)；显示时按整数倍放粗
MAX_ROWS = 220
MAX_COLS = 360
SUPPORTED_DAYS = (3, 7, 14, 30)
DEFAULT_DAYS = 7
HISTORY_DAYS = 30             # 币安 openInterestHist 只留最近约 30 天
DISPLAY_MARGIN_PCT = 10.0     # 图的价格范围 = 窗口内最高/最低价再各外扩这么多
CLUSTER_RANGE_PCT = 8.0       # 只在现价 ±8% 内找簇
CLUSTER_WIDTH_PCT = 1.0       # 一个"簇" = 这么宽的一条价带（占现价百分比）
CLUSTER_MIN_SEP_PCT = 2.0     # 簇与簇至少相隔这么远，免得一大片连续的强平位被切成三段占满榜单
CLUSTER_TOP = 3
MIN_CLUSTER_SIZE = 0.05       # 小于这个量(BTC)的不算簇
ZMAX_QUANTILE = 0.99


def assumption_note(mmr: float = MMR) -> str:
    """把模型的每条假设用大白话写出来，随 payload 下发（用户不能把它当成交易所数据）。"""
    m = f"{mmr * 100:g}%"
    return "\n".join((
        "【估算值，不是交易所数据】交易所不公开每个仓位的强平价，这张图是按下面的假设推算的：",
        "1. 数据：只用币安 BTCUSDT 永续的 15 分钟 K 线和持仓量(OI)，最多回看 30 天；"
        "更早建的仓、其他交易所的仓都不在图里。",
        "2. 开仓：OI 每净增 1 BTC，记作新开 1 BTC 多单 + 1 BTC 空单，"
        "入场价取这根 K 线的 (高+低+收)/3。",
        "3. 杠杆：按 tiers 里的比例分到各档，这个比例是主观设定，真实分布未知。"
        f"强平价：多单 = 入场×(1 - 1/杠杆 + {m})，空单 = 入场×(1 + 1/杠杆 - {m})，"
        f"{m} 是币安 BTCUSDT 第一档维持保证金率。",
        "4. 清算：之后任何一根 K 线的最低价碰到多单强平位、或最高价碰到空单强平位，"
        "就当作已经爆仓，从图上移除。",
        "5. 平仓：OI 下降时，先抵扣这一根已被扫掉的量，"
        "其余降幅按「每张持仓被平掉的概率相同」等比例缩小全部档位。",
        "6. 全仓/逐仓、追加保证金、对冲仓都无从得知，所以只能看相对强弱和大致位置，"
        "不能当成精确价位或真实挂单。",
        "7. 切换天数只改变图的时间范围；当前强平位始终用全部 30 天数据累积。",
    ))


ASSUMPTION_NOTE = assumption_note()

FAPI_BASE = "https://fapi.binance.com"
HTTP_TIMEOUT_S = 15
MAX_PAGES = 10                # 单次分页上限（30 天 OI = 6 页，留余量）
WEIGHT_GUARD = 1200           # 币安 1 分钟已用权重超过它就停手，把额度让给实盘引擎(上限 2400)
MIN_FETCH_INTERVAL_MS = 60_000


# ───────────────────────── 纯计算：价格网格 ─────────────────────────
class Grid(NamedTuple):
    """价格网格：第 i 行覆盖 [p0 + i*bucket, p0 + (i+1)*bucket)。"""
    p0: float
    bucket: float
    rows: int

    def row(self, price: float) -> int:
        """价格落在第几行；出界返回 -1（出界档位仍留在池里参与扫单，只是不画）。"""
        r = int((price - self.p0) // self.bucket)
        return r if 0 <= r < self.rows else -1


def choose_grid(lo: float, hi: float, base_bucket: float = BASE_BUCKET,
                max_rows: int = MAX_ROWS) -> Grid:
    """选一个覆盖 [lo, hi] 的网格：bucket 取 base_bucket 的最小整数倍使行数 ≤ max_rows，
    p0 对齐到 bucket 的整数倍（这样两次刷新之间网格稳定，不会抖）。"""
    if not (hi > lo):
        hi = lo + base_bucket
    k = max(1, math.ceil((hi - lo) / (base_bucket * max_rows)))
    while True:
        bucket = base_bucket * k
        p0 = math.floor(lo / bucket) * bucket
        rows = max(1, math.ceil((hi - p0) / bucket))
        if rows <= max_rows:
            return Grid(float(p0), float(bucket), int(rows))
        k += 1


# ───────────────────────── 纯计算：强平位池 ─────────────────────────
def liq_price_long(entry: float, leverage: float, mmr: float = MMR) -> float:
    """多单强平价（逐仓近似）。"""
    return entry * (1.0 - 1.0 / leverage + mmr)


def liq_price_short(entry: float, leverage: float, mmr: float = MMR) -> float:
    """空单强平价（逐仓近似）。"""
    return entry * (1.0 + 1.0 / leverage - mmr)


class _Side:
    """一侧（多头或空头）的未清算强平位。

    - 堆：多头按价格从高到低出堆（离现价最近的先被扫），空头从低到高。
    - 衰减用"全局倍率"惰性实现：真实数量 = 存储量 × scale，整池衰减只改 scale，O(1)。
    - 带网格时同步维护每行的存储量，出一列热力图只要 O(行数)。
    """
    __slots__ = ("is_long", "heap", "scale", "stored", "grid", "rows", "cnt")

    def __init__(self, is_long: bool, grid: Grid | None):
        self.is_long = is_long
        self.heap: list[tuple[float, float]] = []     # (key, 存储量)；key = -价格(多) / 价格(空)
        self.scale = 1.0
        self.stored = 0.0
        self.grid = grid
        self.rows = [0.0] * grid.rows if grid else None
        self.cnt = [0] * grid.rows if grid else None

    def add(self, price: float, size: float) -> None:
        if size <= 0 or price <= 0:
            return
        st = size / self.scale
        heapq.heappush(self.heap, (-price if self.is_long else price, st))
        self.stored += st
        if self.rows is not None:
            r = self.grid.row(price)
            if r >= 0:
                self.rows[r] += st
                self.cnt[r] += 1

    def sweep(self, bound: float) -> float:
        """多头侧 bound=本根最低价：移除所有强平价 ≥ bound 的档位；
        空头侧 bound=本根最高价：移除所有强平价 ≤ bound 的档位。返回被扫掉的真实数量。"""
        key_bound = -bound if self.is_long else bound      # 两侧统一成 key ≤ key_bound
        h = self.heap
        if not h or h[0][0] > key_bound:
            return 0.0
        swept = 0.0
        rows, cnt, grid = self.rows, self.cnt, self.grid
        while h and h[0][0] <= key_bound:
            key, st = heapq.heappop(h)
            swept += st
            if rows is not None:
                r = grid.row(-key if self.is_long else key)
                if r >= 0:
                    cnt[r] -= 1
                    rows[r] = rows[r] - st if cnt[r] > 0 else 0.0   # 归零时硬清，杜绝浮点残渣
        self.stored = self.stored - swept if h else 0.0
        return swept * self.scale

    def decay(self, factor: float) -> None:
        """整池等比例缩小到 factor 倍。"""
        if factor >= 1.0 or not self.heap:
            return
        if factor <= 0.0:
            self.heap.clear()
            self.stored = 0.0
            self.scale = 1.0
            if self.rows is not None:
                self.rows = [0.0] * self.grid.rows
                self.cnt = [0] * self.grid.rows
            return
        self.scale *= factor
        if self.scale < 1e-9:                # 倍率太小就摊回存储量，避免之后 add 时除出天文数字
            s = self.scale
            self.heap = [(k, st * s) for k, st in self.heap]   # 同乘正数不改变堆序
            self.stored *= s
            if self.rows is not None:
                self.rows = [v * s for v in self.rows]
            self.scale = 1.0

    def total(self) -> float:
        return self.stored * self.scale

    def levels(self) -> list[tuple[float, float]]:
        """[(强平价, 真实数量)]，按价格升序。"""
        s = self.scale
        if self.is_long:
            return sorted((-k, st * s) for k, st in self.heap)
        return sorted((k, st * s) for k, st in self.heap)

    def row_values(self) -> list[float]:
        s = self.scale
        return [v * s if v > 0.0 else 0.0 for v in self.rows]


def _decay_factor(drop: float, swept: float, oi_prev: float) -> float:
    """OI 下降 drop 时，本侧【扫单之后剩下的】池子应保留的比例。

    swept 是本根本侧已被清算的量：它本身就解释了 drop 的一部分，先抵扣，避免重复计算。
    """
    resid = drop - swept
    if resid <= 0.0:
        return 1.0
    base = oi_prev - swept
    if base <= 0.0:
        return 0.0
    return max(0.0, 1.0 - resid / base)


class LiqPool:
    """强平位池：long = 多单的强平位（在价格下方），short = 空单的强平位（在上方）。"""

    def __init__(self, tiers: Sequence[tuple[float, float]] = LEVERAGE_TIERS,
                 mmr: float = MMR, grid: Grid | None = None):
        self.mmr = float(mmr)
        self.tiers = tuple((float(lev), float(w)) for lev, w in tiers
                           if w > 0 and lev > 0 and 1.0 / lev > mmr)   # 1/杠杆 ≤ MMR 的档开仓即爆，无意义
        self._k_long = [(1.0 - 1.0 / lev + self.mmr, w) for lev, w in self.tiers]
        self._k_short = [(1.0 + 1.0 / lev - self.mmr, w) for lev, w in self.tiers]
        self.grid = grid
        self.long = _Side(True, grid)
        self.short = _Side(False, grid)

    def step(self, high: float, low: float, entry: float,
             oi_prev: float | None = None, oi_now: float | None = None) -> dict:
        """推进一根 K 线。oi_prev/oi_now = 这根开盘/收盘时的 OI(BTC)，任一未知则只扫单。
        返回本根发生了什么（调试/单测用）。"""
        swept_long = self.long.sweep(low)
        swept_short = self.short.sweep(high)
        opened = 0.0
        f_long = f_short = 1.0
        if oi_prev is not None and oi_now is not None:
            d = oi_now - oi_prev
            if d > 0.0:
                opened = d
                for kf, w in self._k_long:
                    self.long.add(entry * kf, d * w)
                for kf, w in self._k_short:
                    self.short.add(entry * kf, d * w)
            elif d < 0.0:
                f_long = _decay_factor(-d, swept_long, oi_prev)
                f_short = _decay_factor(-d, swept_short, oi_prev)
                self.long.decay(f_long)
                self.short.decay(f_short)
        return {"swept_long": swept_long, "swept_short": swept_short, "opened": opened,
                "decay_long": f_long, "decay_short": f_short}

    def levels(self, side: str) -> list[tuple[float, float]]:
        return (self.long if side == "long" else self.short).levels()

    def total(self, side: str) -> float:
        return (self.long if side == "long" else self.short).total()

    def column(self) -> list[float]:
        """当前这一刻的热力图一列：每行 多+空 的未清算量（需要带 grid 构造）。"""
        sl, ss = self.long.scale, self.short.scale
        return [(a * sl if a > 0.0 else 0.0) + (b * ss if b > 0.0 else 0.0)
                for a, b in zip(self.long.rows, self.short.rows)]

    def profile(self) -> tuple[list[float], list[float]]:
        """(多头每行, 空头每行)。"""
        return self.long.row_values(), self.short.row_values()


def typical_price(high: float, low: float, close: float) -> float:
    return (high + low + close) / 3.0


def simulate(bars: Sequence[tuple], *, grid: Grid | None = None,
             snap_at: Iterable[int] = (),
             tiers: Sequence[tuple[float, float]] = LEVERAGE_TIERS,
             mmr: float = MMR) -> tuple[LiqPool, list[list[float]]]:
    """按时间顺序跑完所有 K 线。

    bars: [(开盘时间ms, 开, 高, 低, 收, 量, 收盘时的OI_BTC 或 None), ...] 升序。
          成交量目前不参与模型，只是留位。OI 缺失的那根只扫单，其变化并入下一根有 OI 的 K 线。
    snap_at: 处理完哪些下标的 K 线后记一列（需要 grid）。列只依赖该下标及之前的数据。
    返回 (最终的池, 列的列表)。
    """
    pool = LiqPool(tiers, mmr, grid)
    snaps = set(snap_at) if grid is not None else set()
    cols: list[list[float]] = []
    prev_oi: float | None = None        # 第一根没有"开盘时的 OI"，只当基准
    step = pool.step
    for i, b in enumerate(bars):
        high, low, close, oi = b[2], b[3], b[4], b[6]
        step(high, low, typical_price(high, low, close), prev_oi, oi)
        if oi is not None:
            prev_oi = oi
        if i in snaps:
            cols.append(pool.column())
    return pool, cols


# ───────────────────────── 纯计算：簇 ─────────────────────────
def find_clusters(levels: Iterable[tuple[float, float]], price: float, side: str, *,
                  fine_bucket: float = BASE_BUCKET, range_pct: float = CLUSTER_RANGE_PCT,
                  width_pct: float = CLUSTER_WIDTH_PCT, min_sep_pct: float = CLUSTER_MIN_SEP_PCT,
                  top: int = CLUSTER_TOP) -> list[dict]:
    """在现价一侧 range_pct 以内找最大的几堆强平位。

    side="below" 只看 [price×(1-range), price)；"above" 只看 (price, price×(1+range)]。
    做法（贪心）：按 fine_bucket 落桶 → 找总量最大的一条宽 ≈ price×width_pct 的价带，记为一簇
    （簇价 = 带内按数量加权的平均强平价，size = 带内总量）→ 把簇价上下 min_sep_pct 以内的量
    全部划掉，再找下一簇。这样簇与簇至少相隔 min_sep_pct，一大片连续的强平位只报最强的那条，
    把名额留给别处。
    返回按 size 从大到小；dist_pct 上方为正、下方为负。
    """
    if price <= 0 or fine_bucket <= 0:
        return []
    below = side == "below"
    lo = price * (1.0 - range_pct / 100.0)
    hi = price * (1.0 + range_pct / 100.0)
    size: dict[int, float] = {}
    pv: dict[int, float] = {}
    for p, s in levels:
        if s <= 0.0:
            continue
        if below:
            if not (lo <= p < price):
                continue
        elif not (price < p <= hi):
            continue
        b = int(p // fine_bucket)
        size[b] = size.get(b, 0.0) + s
        pv[b] = pv.get(b, 0.0) + p * s
    if not size:
        return []
    b0 = min(size)
    n = max(size) - b0 + 1
    arr = [0.0] * n                 # 每桶数量
    pva = [0.0] * n                 # 每桶 价格×数量
    for b, s in size.items():
        arr[b - b0] = s
        pva[b - b0] = pv[b]
    w = min(n, max(1, int(round(price * width_pct / 100.0 / fine_bucket))))
    sep_usd = price * min_sep_pct / 100.0
    out: list[dict] = []
    for _ in range(top):
        cur = sum(arr[:w])
        best, bi = cur, 0
        for i in range(1, n - w + 1):
            cur += arr[i + w - 1] - arr[i - 1]
            if cur > best + 1e-9:
                best, bi = cur, i
        s = sum(arr[bi:bi + w])     # 重新精确求和，不用滑窗的累积值
        if s < MIN_CLUSTER_SIZE:
            break
        cp = sum(pva[bi:bi + w]) / s
        out.append({"price": round(cp, 1), "size": round(s, 1),
                    "dist_pct": round((cp - price) / price * 100.0, 2)})
        for i in range(n):          # 划掉这一簇及其周围 min_sep 以内的量（按桶中心算距离）
            if bi <= i < bi + w or abs((b0 + i + 0.5) * fine_bucket - cp) < sep_usd:
                arr[i] = 0.0
                pva[i] = 0.0
    return out


# ───────────────────────── 纯计算：拼数据、出 payload ─────────────────────────
def merge_bars(klines: Sequence[tuple], oi_by_ts: dict[int, float],
               oi_now: float | None = None, tf_ms: int = TF_MS) -> list[tuple]:
    """把 K 线和 OI 历史拼成 simulate() 要的 bars。

    币安 openInterestHist 的 timestamp 是【该时刻的快照】（实测 15m 与 5m 序列在同一时刻
    数值相同），所以开盘时间为 t 的 K 线，收盘时的 OI = oi_by_ts[t + tf_ms]。
    最后一根（未收盘）没有收盘 OI，用实时 OI(oi_now) 顶上；都没有就是 None。
    """
    out: list[tuple] = []
    n = len(klines)
    for i, k in enumerate(klines):
        t = int(k[0])
        oi = oi_by_ts.get(t + tf_ms)
        if oi is None and i == n - 1:
            oi = oi_now
        out.append((t, float(k[1]), float(k[2]), float(k[3]), float(k[4]), float(k[5]), oi))
    return out


def assumptions(tiers: Sequence[tuple[float, float]] = LEVERAGE_TIERS, mmr: float = MMR) -> dict:
    return {"tiers": [[lev, w] for lev, w in tiers], "mmr": mmr,
            "note": assumption_note(mmr)}


def empty_payload() -> dict:
    """还没有数据时的合法空 payload（HTTP 200 用）。rows=0 / times=[] 表示"暂无数据"。"""
    return {
        "symbol": SYMBOL, "tf": TF, "bucket": BASE_BUCKET, "p0": 0.0, "rows": 0,
        "times": [], "close": [], "z": [], "zmax": 0.0,
        "profile": {"long": [], "short": []},
        "price": 0.0,
        "clusters": {"above": [], "below": []},
        "assumptions": assumptions(),
        "ts": 0,
    }


def _r(v: float):
    """热力图单元格取整：<0.05 记 0；<100 留 1 位小数；更大取整数（省一半流量，色阶看不出差别）。"""
    if v < 0.05:
        return 0
    if v < 100.0:
        return round(v, 1)
    return int(v + 0.5)


def robust_max(z: Sequence[Sequence[float]], q: float = ZMAX_QUANTILE) -> float:
    """色阶上限：非零单元格的 q 分位数（少数极亮的格子不至于把整张图压暗）。"""
    vals = sorted(v for col in z for v in col if v > 0)
    if not vals:
        return 0.0
    return float(vals[min(len(vals) - 1, int(q * (len(vals) - 1) + 0.5))])


def _plan_columns(times: Sequence[int], days: float, max_cols: int,
                  tf_ms: int = TF_MS) -> tuple[int, list[int], int]:
    """决定显示窗口从哪根开始、在哪些下标后记一列。返回 (起始下标, 记列下标, 每列合并几根)。

    每列合并 step 根 K 线，按【绝对时间边界】分组（如 step=8 → 整 2 小时一列），这样历史列在
    两次刷新之间不会错位；开头不满一组的零头丢掉，最后一组可以不满（正在形成的那列）。
    """
    n_all = len(times)
    t_start = times[-1] - int(days * DAY_MS) + tf_ms
    i0 = bisect_left(times, t_start)
    step = max(1, math.ceil((n_all - i0) / max_cols))
    while True:
        span = step * tf_ms
        j0 = i0
        if step > 1:
            j0 = bisect_left(times, math.ceil(times[i0] / span) * span)
            if j0 >= n_all:                  # 极端情况：窗口比一组还短
                j0 = i0
        snaps = [i for i in range(j0, n_all)
                 if i == n_all - 1 or times[i + 1] // span != times[i] // span]
        if len(snaps) <= max_cols:
            return j0, snaps, step
        step += 1


def build_payload(bars: Sequence[tuple], days: float = DEFAULT_DAYS, *,
                  tiers: Sequence[tuple[float, float]] = LEVERAGE_TIERS, mmr: float = MMR,
                  base_bucket: float = BASE_BUCKET, max_rows: int = MAX_ROWS,
                  max_cols: int = MAX_COLS, margin_pct: float = DISPLAY_MARGIN_PCT,
                  now_ms: int | None = None) -> dict:
    """纯函数：bars → /api/flow/liqmap 的 payload。

    bars 可以比 days 长：【全部】bars 都参与累积（当作预热），days 只决定画出来的时间范围。
    所以切换天数不会改变"当前强平位在哪"，只改变往回看多远。
    每列的时间 = 该列包含的最后一根 15m K 线的开盘时间；该列只用到这根及之前的数据。
    """
    clean = [b for b in bars
             if len(b) >= 7 and b[3] > 0 and b[2] >= b[3] and b[4] > 0]
    clean.sort(key=lambda b: b[0])
    if len(clean) < 2:
        return empty_payload()

    times_all = [int(b[0]) for b in clean]
    i0, snaps, _ = _plan_columns(times_all, days, max_cols)
    lo = min(b[3] for b in clean[i0:]) * (1.0 - margin_pct / 100.0)
    hi = max(b[2] for b in clean[i0:]) * (1.0 + margin_pct / 100.0)
    grid = choose_grid(lo, hi, base_bucket, max_rows)

    pool, cols = simulate(clean, grid=grid, snap_at=snaps, tiers=tiers, mmr=mmr)

    z = [[_r(v) for v in col] for col in cols]
    prof_long, prof_short = pool.profile()
    price = float(clean[-1][4])
    return {
        "symbol": SYMBOL, "tf": TF,
        "bucket": grid.bucket, "p0": grid.p0, "rows": grid.rows,
        "times": [times_all[i] for i in snaps],
        "close": [float(clean[i][4]) for i in snaps],
        "z": z,
        "zmax": robust_max(z),
        "profile": {"long": [_r(v) for v in prof_long], "short": [_r(v) for v in prof_short]},
        "price": price,
        "clusters": {
            # 上方 = 空单的强平位（价格涨上去会触发），下方 = 多单的强平位
            "above": find_clusters(pool.levels("short"), price, "above", fine_bucket=base_bucket),
            "below": find_clusters(pool.levels("long"), price, "below", fine_bucket=base_bucket),
        },
        "assumptions": assumptions(tiers, mmr),
        "ts": int(now_ms if now_ms is not None else time.time() * 1000),
    }


# ───────────────────────── 取数（只在后台刷新里调用，绝不在请求路径上） ─────────────────────────
class RateLimited(RuntimeError):
    """币安限流/封禁(429/418)，或我们自己的权重保护触发。retry_after 单位秒。"""

    def __init__(self, status: int, retry_after: float):
        super().__init__(f"币安限流 status={status} retry_after={retry_after:.0f}s")
        self.status = status
        self.retry_after = retry_after


_used_weight = {"value": 0, "at": 0.0}     # 最近一次响应头里的 1 分钟已用权重


async def _get_json(session: aiohttp.ClientSession, path: str, params: dict,
                    base: str = FAPI_BASE):
    # 权重保护：这台机器的 IP 额度首先属于实盘引擎，已用过半就不再添乱
    if _used_weight["value"] > WEIGHT_GUARD and time.time() - _used_weight["at"] < 60:
        raise RateLimited(0, 60)
    async with session.get(base + path, params=params,
                           timeout=aiohttp.ClientTimeout(total=HTTP_TIMEOUT_S)) as resp:
        used = resp.headers.get("X-MBX-USED-WEIGHT-1M")
        if used is not None:
            try:
                _used_weight["value"], _used_weight["at"] = int(used), time.time()
            except ValueError:
                pass
        if resp.status in (418, 429):
            try:
                ra = float(resp.headers.get("Retry-After", "60"))
            except ValueError:
                ra = 60.0
            raise RateLimited(resp.status, ra)
        if resp.status != 200:
            body = (await resp.text())[:200]
            raise RuntimeError(f"币安 {path} HTTP {resp.status}: {body}")
        return await resp.json()


async def fetch_klines(session: aiohttp.ClientSession, start_ms: int, now_ms: int,
                       symbol: str = SYMBOL, base: str = FAPI_BASE) -> list[tuple]:
    """从 start_ms 到现在的 15m K 线（含未收盘的最后一根）。返回 [(t, o, h, l, c, v)] 升序。
    权重：≤99 根=1，≤499=2，≤1000=5；30 天全量 3 次请求共 15，增量刷新每次 1。"""
    out: dict[int, tuple] = {}
    cursor = int(start_ms)
    for _ in range(MAX_PAGES):
        need = (now_ms - cursor) // TF_MS + 2
        limit = int(max(2, min(1000, need)))
        raw = await _get_json(session, "/fapi/v1/klines",
                              {"symbol": symbol, "interval": TF,
                               "startTime": cursor, "limit": limit}, base)
        if not raw:
            break
        for k in raw:
            t = int(k[0])
            out[t] = (t, float(k[1]), float(k[2]), float(k[3]), float(k[4]), float(k[5]))
        last = int(raw[-1][0])
        if len(raw) < limit or last + TF_MS > now_ms:
            break
        cursor = last + TF_MS
    return [out[t] for t in sorted(out)]


async def fetch_oi_hist(session: aiohttp.ClientSession, start_ms: int, now_ms: int,
                        symbol: str = SYMBOL, base: str = FAPI_BASE) -> dict[int, float]:
    """从 start_ms 到现在的 15m OI 快照 {时间戳: OI_BTC}。

    实测这个接口只传 startTime 时返回的仍是【最新】500 条，所以用 endTime 从新往旧翻页。
    endTime 早于约 31 天会返回 400(-1130)：已经拿到数据时当作"到头了"，不算失败。
    """
    out: dict[int, float] = {}
    end: int | None = None
    for _ in range(MAX_PAGES):
        need = ((end if end is not None else now_ms) - start_ms) // TF_MS + 2
        limit = int(max(1, min(500, need)))
        params: dict = {"symbol": symbol, "period": TF, "limit": limit}
        if end is not None:
            params["endTime"] = end
        try:
            raw = await _get_json(session, "/futures/data/openInterestHist", params, base)
        except RateLimited:
            raise
        except RuntimeError:
            if out:
                break
            raise
        if not isinstance(raw, list) or not raw:
            break
        first = None
        for d in raw:
            try:
                ts, oi = int(d["timestamp"]), float(d["sumOpenInterest"])
            except (KeyError, TypeError, ValueError):
                continue
            if oi > 0:
                out[ts] = oi
            first = ts if first is None else min(first, ts)
        if first is None or first <= start_ms or len(raw) < limit:
            break
        end = first - 1
    return out


async def fetch_oi_now(session: aiohttp.ClientSession, symbol: str = SYMBOL,
                       base: str = FAPI_BASE) -> tuple[int, float] | None:
    """实时 OI（给未收盘那根 K 线用）。返回 (时间ms, OI_BTC)；拿不到返回 None，不影响主流程。"""
    try:
        d = await _get_json(session, "/fapi/v1/openInterest", {"symbol": symbol}, base)
        return int(d["time"]), float(d["openInterest"])
    except RateLimited:
        raise
    except Exception:
        return None


# ───────────────────────── 缓存：后台刷新 + 同步读取 ─────────────────────────
_store: dict = {"klines": {}, "oi": {}, "oi_now": None, "version": 0}
_cache: dict[int, dict] = {}            # days -> payload
_cache_json: dict[int, bytes] = {}      # days -> 已序列化的 JSON
_cache_gz: dict[int, bytes] = {}        # days -> gzip 后的 JSON
_cache_ver: dict[int, int] = {}         # days -> 算这份 payload 时的数据版本
_status: dict = {"last_ok_ms": 0, "last_attempt_ms": 0, "last_error": None,
                 "backoff_until_ms": 0, "bars": 0, "compute_ms": {}, "bytes": {}}
_lock: asyncio.Lock | None = None
_EMPTY_JSON = json.dumps(empty_payload(), ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _norm_days(days) -> int:
    try:
        d = int(days)
    except (TypeError, ValueError):
        return DEFAULT_DAYS
    return d if d in SUPPORTED_DAYS else DEFAULT_DAYS


async def _update_store(session: aiohttp.ClientSession, now_ms: int, base: str = FAPI_BASE) -> None:
    """把内存里的 K 线/OI 补到最新。首次全量拉 30 天（K 线 3 次 + OI 6 次请求），
    之后每次只补尾巴（各 1 次小请求 + 1 次实时 OI）。

    K 线拿到就算数据有更新（version+1）：即使随后 OI 接口失败，新 K 线的扫单也要反映到图上，
    缺的 OI 变化会在下次补齐时并入（merge_bars 每次都从全量重拼，不会永久记错账）。
    """
    hist_start = (now_ms - HISTORY_DAYS * DAY_MS) // TF_MS * TF_MS
    kl: dict[int, tuple] = _store["klines"]
    oi: dict[int, float] = _store["oi"]

    # 回补最近 2 根：未收盘那根一直在变，刚收盘那根也要定稿
    k_start = max(hist_start, max(kl) - 2 * TF_MS) if kl else hist_start
    new_kl = await fetch_klines(session, k_start, now_ms, base=base)
    for k in new_kl:
        kl[k[0]] = k
    cutoff = hist_start - TF_MS
    for t in [t for t in kl if t < cutoff]:
        del kl[t]
    _store["oi_now"] = None
    if new_kl:
        _store["version"] += 1

    o_start = max(hist_start, max(oi) - 2 * TF_MS) if oi else hist_start
    oi.update(await fetch_oi_hist(session, o_start, now_ms, base=base))
    for t in [t for t in oi if t < cutoff]:
        del oi[t]

    live = await fetch_oi_now(session, base=base)
    last_open = max(kl) if kl else 0
    # 实时 OI 只在它确实属于最后一根 K 线的时段内才用
    if live and live[0] >= last_open and live[1] > 0:
        _store["oi_now"] = live[1]


def _compute_many(bars: list[tuple], days_list: Sequence[int], now_ms: int) -> dict[int, tuple]:
    """算 payload 并预先序列化（在工作线程里跑，不占事件循环）。"""
    out: dict[int, tuple] = {}
    for d in days_list:
        t0 = time.perf_counter()
        payload = build_payload(bars, d, now_ms=now_ms)
        ms = (time.perf_counter() - t0) * 1000.0
        js = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        out[d] = (payload, js, gzip.compress(js, compresslevel=6), ms)
    return out


async def refresh(session: aiohttp.ClientSession, days=None, *, now_ms: int | None = None,
                  force: bool = False, in_thread: bool = True, base: str = FAPI_BASE) -> bool:
    """后台刷新：补数据 → 重算 payload → 更新缓存。集成方每 5 分钟调一次即可。

    days=None 一次算完所有支持的天数；也可以逐个传 3/7/14/30 —— 60 秒内重复调用不会重复取数，
    数据没变的天数也不会重算。
    永不抛异常：失败时保留旧缓存、把原因记到 status()，返回 False。
    被币安限流(429/418)后会按 Retry-After 静默，期间不再发请求。
    in_thread=True 时把计算放到工作线程（30 天一次约 0.1~0.3 秒纯 Python 计算，
    别让它卡住同进程的实盘引擎事件循环）。
    """
    global _lock
    if _lock is None:
        _lock = asyncio.Lock()
    want = list(SUPPORTED_DAYS) if days is None else [_norm_days(days)]
    async with _lock:
        now = int(now_ms if now_ms is not None else time.time() * 1000)
        ok = _status["last_error"] is None      # 这次若不取数，就沿用上次取数的成败
        if force or now - _status["last_attempt_ms"] >= MIN_FETCH_INTERVAL_MS:
            if now < _status["backoff_until_ms"] and not force:
                ok = False
            else:
                ok = True
                _status["last_attempt_ms"] = now
                try:
                    await _update_store(session, now, base)
                    _status["last_error"] = None
                except RateLimited as e:
                    _status["backoff_until_ms"] = now + int(max(60.0, e.retry_after) * 1000)
                    _status["last_error"] = str(e)
                    log.warning("清算热力图取数被限流，静默 %.0fs", max(60.0, e.retry_after))
                    ok = False
                except Exception as e:      # 网络抖动等：保留旧数据，下次再试
                    _status["last_error"] = f"{type(e).__name__}: {e}"[:200]
                    log.warning("清算热力图取数失败: %s", _status["last_error"])
                    ok = False

        ver = _store["version"]
        todo = [d for d in want if _cache_ver.get(d) != ver]
        if ver and todo and _store["klines"]:
            kl = _store["klines"]
            bars = merge_bars([kl[t] for t in sorted(kl)], _store["oi"], _store["oi_now"])
            try:
                if in_thread:
                    res = await asyncio.to_thread(_compute_many, bars, todo, now)
                else:
                    res = _compute_many(bars, todo, now)
            except Exception as e:
                _status["last_error"] = f"计算失败 {type(e).__name__}: {e}"[:200]
                log.exception("清算热力图计算失败")
                return False
            for d, (payload, js, gz, ms) in res.items():
                _cache[d], _cache_json[d], _cache_gz[d], _cache_ver[d] = payload, js, gz, ver
                _status["compute_ms"][d] = round(ms, 1)
                _status["bytes"][d] = {"json": len(js), "gzip": len(gz)}
            _status["bars"] = len(bars)
            _status["last_ok_ms"] = now
        return ok


def get_cached(days=DEFAULT_DAYS) -> dict:
    """同步读取最近一次算好的 payload；还没算出来就返回合法的空 payload。绝不触网。
    days 不在 {3,7,14,30} 时按 7 处理。返回的是缓存里的同一个对象，调用方只读、别改。"""
    return _cache.get(_norm_days(days)) or empty_payload()


def get_cached_json(days=DEFAULT_DAYS, gz: bool = False) -> bytes:
    """同上，但直接给已经序列化好的 JSON 字节（gz=True 给 gzip 后的）。
    公开接口建议用这个：30 天的 payload 有几百 KB，每个请求都重新序列化/压缩等于让陌生人
    白烧这台 1 核机器的 CPU。"""
    d = _norm_days(days)
    if gz:
        b = _cache_gz.get(d)
        return b if b is not None else gzip.compress(_EMPTY_JSON)
    return _cache_json.get(d, _EMPTY_JSON)


def last_updated_ms() -> int:
    """最近一次成功重算的时间(ms)，从未成功为 0。给 /api/flow/health 的 caches.liqmap 用。"""
    return int(_status["last_ok_ms"])


def status() -> dict:
    """刷新状态（健康检查用）。"""
    return {
        "last_ok_ms": _status["last_ok_ms"], "last_attempt_ms": _status["last_attempt_ms"],
        "last_error": _status["last_error"], "backoff_until_ms": _status["backoff_until_ms"],
        "bars": _status["bars"], "oi_points": len(_store["oi"]),
        "compute_ms": dict(_status["compute_ms"]), "bytes": dict(_status["bytes"]),
        "cached_days": sorted(_cache),
    }


def _reset_cache() -> None:
    """清空模块内状态（单测用）。"""
    global _lock
    _store.update({"klines": {}, "oi": {}, "oi_now": None, "version": 0})
    for d in (_cache, _cache_json, _cache_gz, _cache_ver):
        d.clear()
    _status.update({"last_ok_ms": 0, "last_attempt_ms": 0, "last_error": None,
                    "backoff_until_ms": 0, "bars": 0, "compute_ms": {}, "bytes": {}})
    _used_weight.update({"value": 0, "at": 0.0})
    _lock = None


# ───────────────────────── 冒烟：python -m app.flow.liqmap ─────────────────────────
async def _smoke() -> None:
    """拉真实数据跑一遍，打印形状/体积/耗时/当前最大的簇。只读公开行情，不启动应用。"""
    async with aiohttp.ClientSession() as s:
        t0 = time.perf_counter()
        ok = await refresh(s, in_thread=False)
        st = status()
        print(f"取数+计算 {time.perf_counter() - t0:.2f}s ok={ok} K线={st['bars']} "
              f"OI点={st['oi_points']} 错误={st['last_error']} 已用权重={_used_weight['value']}")
    for d in SUPPORTED_DAYS:
        p = get_cached(d)
        b = st["bytes"].get(d, {})
        nz = sum(1 for col in p["z"] for v in col if v)
        print()
        print(f"[{d}天] 列×行={len(p['times'])}×{p['rows']} bucket={p['bucket']:g} "
              f"价格范围={p['p0']:g}~{p['p0'] + p['rows'] * p['bucket']:g} 非零格={nz} "
              f"zmax={p['zmax']:g} 计算={st['compute_ms'].get(d)}ms "
              f"JSON={b.get('json')}B gzip={b.get('gzip')}B")
        if not p["rows"]:
            continue
        pr = int((p["price"] - p["p0"]) // p["bucket"])
        lg, sh = p["profile"]["long"], p["profile"]["short"]
        print(f"   现价={p['price']:g}  多头档合计(图内)={sum(lg):.0f} BTC 其中在现价上方={sum(lg[pr + 1:]):.0f}"
              f"  空头档合计(图内)={sum(sh):.0f} BTC 其中在现价下方={sum(sh[:pr]):.0f}")
        for side in ("above", "below"):
            for c in p["clusters"][side]:
                print(f"   {'上方(空单爆)' if side == 'above' else '下方(多单爆)'} "
                      f"{c['price']:>9.1f}  {c['size']:>8.1f} BTC  {c['dist_pct']:+.2f}%")


if __name__ == "__main__":
    asyncio.run(_smoke())
