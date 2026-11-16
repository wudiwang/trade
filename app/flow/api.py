"""订单流页（/flow.html）集成层：公开只读路由 + 后台任务（2026-11-16）。

    register_flow(app, cfg, db, engine)   create_app 里调用：注册 /api/flow/* 五个 GET
    start_flow_tasks(cfg, engine)         engine.start() 之后调用：深度记录器 + 三个刷新循环
    stop_flow_tasks()                     关停时调用（可不调，进程退出即止）

三条铁律（本进程里同时跑着真钱引擎，单核 VPS）：
  1. 请求路径永不触网。五个处理函数只读内存里已经序列化好的字节，或（深度图）读本地 sqlite。
     陌生人刷接口烧不到交易所的 IP 额度。
  2. 重计算一律进线程；深度图另有「短 TTL 响应缓存 + 同键合并 + 构图占空比上限」，
     N 个人同时看同一张图只算一次，有人轮着换参数刷也最多占一条线程的 1/4。
  3. 后台循环每一轮都有 try/except、每个触网的 await 都有超时、连续失败指数退避，永不向外抛。
     flow.enabled=false：路由照常在，但只回合法的空载荷；不起任务、不建库、不发任何请求。

稳态下新增的上游请求量见 upstream_budget()（大头是深度记录器：币安合约 40 权重/分钟）。
"""
from __future__ import annotations

import asyncio
import gzip as _gzip
import json
import logging
import math
import random
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import aiohttp
from fastapi import Request
from fastapi.responses import JSONResponse, Response

from app.config import ROOT
from app.engine import orderflow
from app.flow import cvd as cvd_mod
from app.flow import depth as depth_mod
from app.flow import liqmap as liq_mod

log = logging.getLogger("flow.api")

SYMBOL = "BTCUSDT"                       # cvd / liqmap 两个模块都只做 BTCUSDT
FUTURES_SOURCES = ("binance", "bybit", "okx")   # src=all 的含义：三个合约盘口（现货有基差，要手动点选）
DEPTH_HOURS = depth_mod.ALLOWED_HOURS    # (1, 6, 24, 72, 168)
DEPTH_COLS = (60, 120, 240, 360)         # 列数只认这几档 → 深度图的缓存键空间有界
DEFAULT_HOURS, DEFAULT_COLS = 6, 360

SUMMARY_INTERVAL_S = 60.0
CVD_INTERVAL_S = 60.0                    # cvd.refresh 自己判断要不要取数：实际每根棒收盘才发请求
LIQMAP_INTERVAL_S = 300.0
SUMMARY_TIMEOUT_S = 40.0                 # 三个顺序请求；BinanceRest 自带重试，这里再包一层硬上限
CVD_TIMEOUT_S = 45.0                     # 每个周期一次（币安 2 路并发 + OKX 3 个顺序请求）
LIQMAP_TIMEOUT_S = 150.0                 # 首次要顺序拉 10 个请求
MAX_BACKOFF_S = {"summary": 900.0, "cvd": 300.0, "liqmap": 1800.0}
FIRST_DELAY_S = {"summary": 1.0, "cvd": 4.0, "liqmap": 12.0}     # 启动错峰
CVD_FIRST_GAP_S, CVD_GAP_S = 1.5, 0.3    # CVD 各周期之间的间隔（启动那一轮拉开些）
SUMMARY_KEEP_GOOD_MS = 15 * 60_000       # 新结果缺 OI/资金费时，这么久以内沿用上一份完整的

# 币安合约 IP 权重(默认上限 2400/分钟)先属于实盘引擎：深度记录器每 30 秒都会带回一次
# 「本 IP 近 1 分钟已用权重」，超过这个数三个刷新循环本轮让路（记录器自己在 1800 让路）。
WEIGHT_YIELD = 1200
WEIGHT_FRESH_MS = 90_000


def _now_ms() -> int:
    return int(time.time() * 1000)


def _dumps(obj: Any) -> bytes:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")


def backoff_delay(interval_s: float, fails: int, max_backoff_s: float) -> float:
    """连续失败 fails 次后的下一次间隔：interval × 2^fails，封顶 max_backoff_s。fails=0 即正常间隔。"""
    if fails <= 0:
        return float(interval_s)
    return float(min(max_backoff_s, interval_s * 2 ** min(int(fails), 6)))


# ---------------------------------------------------------------- 配置
def _num(cfg, key: str, default: float, lo: float, hi: float) -> float:
    try:
        v = float(cfg.get(key, default))
    except (TypeError, ValueError):
        v = default
    if not math.isfinite(v):
        v = default
    return max(lo, min(hi, v))


def _flag(v: Any) -> bool:
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "on")
    return bool(v)


@dataclass(frozen=True)
class FlowSettings:
    """config.yaml 的 flow: 段（缺省值即线上默认；启动时读一次，不热更新）。"""
    enabled: bool
    db_path: Path
    interval_s: float          # 盘口快照间隔
    keep_days: float           # 5 分钟均值档保留天数（7 天窗口需要 ≥8）
    keep_raw_days: float       # 原始 30s 快照保留天数（≥24h 的窗口读均值档，原始留 2 天够了）
    half_range_pct: float      # 只记录各源中间价 ± 这么多以内的挂单
    sources: tuple             # 记录哪些源
    all_sources: tuple         # src=all 合并哪些源

    @classmethod
    def from_cfg(cls, cfg) -> "FlowSettings":
        raw = str(cfg.get("flow.db_path", "") or "data/flow_depth.sqlite")
        path = Path(raw)
        if not path.is_absolute():
            path = ROOT / path
        names = cfg.get("flow.sources", None)
        want = {str(n).strip().lower() for n in names} if isinstance(names, (list, tuple)) else set()
        sources = tuple(n for n in depth_mod.ALL_SOURCES if n in want) or depth_mod.ALL_SOURCES
        keep_days = _num(cfg, "flow.keep_days", 8.0, 1.0, 60.0)
        return cls(
            enabled=_flag(cfg.get("flow.enabled", False)),
            db_path=path,
            interval_s=_num(cfg, "flow.interval_s", 30.0, 10.0, 600.0),
            keep_days=keep_days,
            keep_raw_days=min(keep_days, _num(cfg, "flow.keep_raw_days", 2.0, 0.5, 30.0)),
            half_range_pct=_num(cfg, "flow.half_range_pct", depth_mod.HALF_RANGE_PCT, 0.2, 5.0),
            sources=sources,
            all_sources=tuple(n for n in FUTURES_SOURCES if n in sources) or sources,
        )


def upstream_budget(settings: FlowSettings | None = None) -> dict:
    """稳态下本模块每分钟新增的上游请求（币安按权重计；其余按请求数计）。

    币安权重表：合约 depth limit=1000 → 20；现货 depth limit=5000 → 250；klines limit 100~499 → 2、<100 → 1；
    openInterest → 1；openInterestHist / fundingRate 不计权重（各有独立的次数上限 1000/5min、500/5min）。
    CVD 每个周期只在棒收盘时取一次：每小时 12+4+1+0.25 次。
    """
    srcs = settings.sources if settings else depth_mod.ALL_SOURCES
    per_min = 60.0 / (settings.interval_s if settings else 30.0)
    cvd_fetch_per_min = (12 + 4 + 1 + 0.25) / 60.0
    fapi = {
        "depth": 20.0 * per_min if "binance" in srcs else 0.0,
        "summary": 2.0 * 60.0 / SUMMARY_INTERVAL_S,
        "cvd": round(2.0 * cvd_fetch_per_min, 3),
        "liqmap": 2.0 * 60.0 / LIQMAP_INTERVAL_S,
    }
    spot = {
        "depth": 250.0 * per_min if "binance_spot" in srcs else 0.0,
        "cvd": round(2.0 * cvd_fetch_per_min, 3),
    }
    return {
        "binance_fapi_weight_per_min": dict(fapi, total=round(sum(fapi.values()), 2), limit=2400),
        "binance_spot_weight_per_min": dict(spot, total=round(sum(spot.values()), 2), limit=6000),
        "binance_unweighted_req_per_min": {"openInterestHist": 1.0 + 0.2, "fundingRate": 1.0},
        "bybit_req_per_min": per_min if "bybit" in srcs else 0.0,
        "okx_req_per_min": round((per_min if "okx" in srcs else 0.0) + 3 * 1.25 / 60.0, 3),
    }


# ---------------------------------------------------------------- 空载荷（flow 关闭 / 还没算出来）
def empty_summary() -> dict:
    """状态卡的空载荷：键与 brief_payload 一致，数值为 null（前端显示成「–」），ts=0 表示从未算出。"""
    return {
        "price": None, "chg_24h": None, "chg_7d": None, "divs": [],
        "cvd_24h": None, "cvd_ratio_24h": None, "oi": None, "oi_24h": None, "oi_7d": None,
        "quadrant": "", "quadrant_text": "",
        "funding": {"state": "unknown", "label": "", "latest": None, "avg": None, "neg": 0, "look": 0},
        "funding_days": [], "summary": "", "ts": 0,
    }


def normalize_summary(payload: dict, ts: int) -> tuple[dict, bool]:
    """把 brief_payload 的结果整理成契约里的形状。返回 (载荷, 是否完整)。

    BinanceRest 的 OI / 资金费接口失败时返回空列表而不是抛异常，brief_payload 会照算：
    OI=0 会被四象限当成「减仓」，资金费缺失会被一句话总结当成「转负」—— 都是拿缺数据下结论。
    这里把缺的那块明确标成「不可用」，并去掉那句总结。
    """
    p = dict(payload)
    complete = True
    f = dict(p.get("funding") or {})
    if f.get("state") in (None, "unknown"):
        complete = False
        f = {"state": "unknown", "label": "资金费数据暂不可用", "latest": None, "avg": None,
             "neg": 0, "look": 0}
        p["funding_days"] = []
    p["funding"] = {k: f.get(k) for k in ("state", "label", "latest", "avg", "neg", "look")}
    if not p.get("oi"):
        complete = False
        p.update(oi=None, oi_24h=None, oi_7d=None, quadrant="", quadrant_text="持仓量数据暂不可用")
    if not complete:
        p["summary"] = ""
    p["divs"] = [{k: d.get(k) for k in ("bars", "price_pct", "cvd_chg", "cvd_pct_of_vol", "state", "text")}
                 for d in (p.get("divs") or []) if isinstance(d, dict)]
    p["funding_days"] = [list(r) for r in (p.get("funding_days") or [])]
    p["ts"] = int(ts)
    return p, complete


_EMPTY_SUMMARY = _dumps(empty_summary())
_EMPTY_DEPTH = _dumps(depth_mod.empty_heatmap(SYMBOL))
_EMPTY_LIQMAP = _dumps(liq_mod.empty_payload())
_EMPTY_CVD = {tf: _dumps(cvd_mod.empty_payload(tf)) for tf in cvd_mod.SUPPORTED_TFS}


# ---------------------------------------------------------------- 查询参数收敛（不认识的值一律回默认，不报错）
def norm_hours(v: Any) -> int:
    try:
        h = int(str(v).strip())
    except (TypeError, ValueError):
        return DEFAULT_HOURS
    return h if h in DEPTH_HOURS else DEFAULT_HOURS


def norm_cols(v: Any) -> int:
    """向下取到 DEPTH_COLS 里的一档（保证返回的列数 ≤ 请求的 cols），不足最小档按最小档。"""
    try:
        c = int(str(v).strip())
    except (TypeError, ValueError):
        return DEFAULT_COLS
    ok = [x for x in DEPTH_COLS if x <= c]
    return ok[-1] if ok else DEPTH_COLS[0]


def norm_src(v: Any, all_sources: tuple = FUTURES_SOURCES) -> tuple:
    """'all' / 空 / 全不认识 → all_sources；否则按规范顺序取认识的那些源。"""
    names = {x.strip().lower() for x in str(v or "")[:200].split(",")}
    if "all" in names:
        return tuple(all_sources)
    sel = tuple(n for n in depth_mod.ALL_SOURCES if n in names)
    return sel or tuple(all_sources)


def norm_days(v: Any) -> int:
    try:
        d = int(str(v).strip())
    except (TypeError, ValueError):
        return liq_mod.DEFAULT_DAYS
    return d if d in liq_mod.SUPPORTED_DAYS else liq_mod.DEFAULT_DAYS


def norm_tf(v: Any) -> str:
    tf = str(v or "").strip().lower()
    return tf if tf in cvd_mod.SUPPORTED_TFS else cvd_mod.DEFAULT_TF


# ---------------------------------------------------------------- 深度图响应缓存
class Busy(Exception):
    """深度图构建排队过长/过满，且没有旧图可给：路由回 503 + Retry-After。"""

    def __init__(self, retry_after: float = 3.0):
        super().__init__("深度图计算繁忙")
        self.retry_after = retry_after


class DepthResponseCache:
    """/api/flow/depth 的响应缓存。只在事件循环线程里用；构图函数在专用单线程里跑。

    - 命中（ttl_s 内）：直接回字节，不进线程。N 个人同时看同一张图 = 一次构建。
    - 同一个键同时只排一次队，后到的请求挂在同一个 future 上（构建与请求解耦：某个请求断开不会取消构建）。
    - 全局只有一个构建协程，一次只构一张，并且两次构建之间强制歇够时间，使构图线程的占空比 ≤ duty：
      有人轮着换参数刷（键空间 5×15×4=300）也顶多吃掉一条线程的 duty，实盘引擎的事件循环不受影响。
    - 排队按「最近请求的先做」：用户连点几个时间窗口时，最后点的那张最先出来；
      排队超过 max_pending 个就把最久没人要的那张丢掉（等它的请求收到 Busy）。
    - 轮不到马上构建、而手里有旧图（stale_s 内）→ 先回旧图，后台照常重建；
      没有旧图 → 最多等 max_wait_s，再不行抛 Busy（路由回 503，前端保留旧图稍后重试）。
    """

    def __init__(self, ttl_s: float = 5.0, max_entries: int = 16, duty: float = 0.25,
                 max_wait_s: float = 8.0, max_pending: int = 6, stale_s: float = 600.0,
                 error_cooldown_s: float = 2.0, clock: Callable[[], float] = time.monotonic):
        self.ttl_s, self.max_entries, self.duty = ttl_s, max_entries, max(0.01, min(1.0, duty))
        self.max_wait_s, self.max_pending, self.stale_s = max_wait_s, max(1, max_pending), stale_s
        self.error_cooldown_s = error_cooldown_s
        self._clock = clock
        self._d: OrderedDict = OrderedDict()        # key -> (构建完成时刻, 响应体)
        self._pending: OrderedDict = OrderedDict()  # key -> (build, future)；最后一个 = 最近被请求的
        self._building: tuple | None = None         # (key, future)：正在线程里构建的那张
        self._worker: asyncio.Task | None = None
        self._loop = None
        self._cool_until = 0.0
        self._pool: ThreadPoolExecutor | None = None
        self.hits = self.builds = self.stale = self.busy = self.errors = self.dropped = 0
        self.last_build_ms = self.max_build_ms = 0.0

    def stats(self) -> dict:
        return {"entries": len(self._d), "bytes": sum(len(v[1]) for v in self._d.values()),
                "hits": self.hits, "builds": self.builds, "stale_served": self.stale,
                "busy": self.busy, "dropped": self.dropped, "errors": self.errors,
                "pending": len(self._pending) + (1 if self._building else 0),
                "last_build_ms": round(self.last_build_ms, 1), "max_build_ms": round(self.max_build_ms, 1)}

    def clear(self) -> None:
        self._d.clear()

    def close(self) -> None:
        if self._pool is not None:
            self._pool.shutdown(wait=False)
            self._pool = None

    def _bind_loop(self):
        loop = asyncio.get_running_loop()
        if self._loop is not loop:                  # 换了事件循环（只在测试里发生）：旧循环上的排队作废
            self._loop, self._worker, self._building = loop, None, None
            self._pending.clear()
        return loop

    @staticmethod
    def _fail(fut: asyncio.Future, exc: BaseException) -> None:
        if not fut.done():
            fut.set_exception(exc)
            fut.exception()                         # 标记为已取走：没人等时不刷"异常未被取走"的日志

    async def _work(self) -> None:
        """唯一的构建协程：冷却 → 取最近被请求的键 → 进线程构建 → 交付。队列空了就退出。"""
        loop = asyncio.get_running_loop()
        try:
            while self._pending:
                wait = self._cool_until - self._clock()
                if wait > 0:
                    await asyncio.sleep(wait)
                    continue                        # 歇完再挑：这期间可能来了更新的请求
                key, (build, fut) = self._pending.popitem(last=True)
                self._building = (key, fut)
                if self._pool is None:
                    self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="flow-heat")
                t0 = self._clock()
                try:
                    body = await loop.run_in_executor(self._pool, build)
                except Exception as e:              # noqa: BLE001 - 交给等它的请求
                    self.errors += 1
                    self._cool_until = self._clock() + self.error_cooldown_s   # 库坏了之类：别每个请求都撞一次
                    self._fail(fut, e)
                else:
                    took = self._clock() - t0
                    self.builds += 1
                    self.last_build_ms = took * 1000.0
                    self.max_build_ms = max(self.max_build_ms, self.last_build_ms)
                    self._cool_until = self._clock() + took * (1.0 / self.duty - 1.0)
                    self._d[key] = (self._clock(), body)
                    self._d.move_to_end(key)
                    while len(self._d) > self.max_entries:
                        self._d.popitem(last=False)
                    if not fut.done():
                        fut.set_result(body)
                finally:
                    self._building = None
        finally:
            if self._worker is asyncio.current_task():
                self._worker = None

    async def get(self, key, build: Callable[[], bytes]) -> bytes:
        """取 key 对应的响应体；必要时排队在线程里调用 build() 构建。"""
        loop = self._bind_loop()
        now = self._clock()
        ent = self._d.get(key)
        if ent is not None and now - ent[0] < self.ttl_s:
            self.hits += 1
            return ent[1]
        stale = ent[1] if ent is not None and now - ent[0] < self.stale_s else None
        if self._building is not None and self._building[0] == key:
            fut = self._building[1]
        elif key in self._pending:
            fut = self._pending[key][1]
            self._pending.move_to_end(key)          # 又有人要：往前排
        else:
            contended = self._building is not None or bool(self._pending) or self._cool_until - now > 0.3
            fut = loop.create_future()
            self._pending[key] = (build, fut)
            while len(self._pending) > self.max_pending:
                _, (_, old) = self._pending.popitem(last=False)
                self.dropped += 1
                self._fail(old, Busy())
            if self._worker is None or self._worker.done():
                self._worker = loop.create_task(self._work())
            if contended and stale is not None:     # 轮不到马上算：先给旧图，新图在后台排着
                self.stale += 1
                return stale
        try:
            return await asyncio.wait_for(asyncio.shield(fut), self.max_wait_s)
        except asyncio.TimeoutError:
            self.busy += 1
            if stale is not None:
                self.stale += 1
                return stale
            raise Busy() from None
        except Busy:
            self.busy += 1
            if stale is not None:
                self.stale += 1
                return stale
            raise
        except Exception:
            if stale is not None:
                self.stale += 1
                return stale
            raise


# ---------------------------------------------------------------- 运行时
class FlowRuntime:
    """一个应用实例的订单流状态：配置、深度库/记录器、状态卡缓存、后台任务。"""

    def __init__(self, cfg, engine=None, *, session_factory: Callable[[], Any] | None = None,
                 recorder_sources: Any = None):
        self.cfg = cfg
        self.settings = FlowSettings.from_cfg(cfg)
        self.enabled = self.settings.enabled
        self.rest = getattr(engine, "rest", None)          # 状态卡取数用（引擎的行情客户端）
        self.session_factory = session_factory             # 预览/测试用：自定义 aiohttp 会话
        self.recorder_sources = recorder_sources           # 测试用：注入假采集源
        self.store: depth_mod.DepthStore | None = None
        self.recorder: depth_mod.DepthRecorder | None = None
        if self.enabled:
            self.store = depth_mod.DepthStore(self.settings.db_path)    # 文件按需创建：这里不落盘
        self.depth_cache = DepthResponseCache()
        self.summary_json: bytes = _EMPTY_SUMMARY
        self.summary_ts = 0
        self.summary_complete = False
        self._gz: dict = {}                                 # 槽位 -> (原字节对象, gzip 后字节)
        self.tasks: list[asyncio.Task] = []
        self.loops: dict[str, dict] = {
            n: {"interval_s": i, "runs": 0, "fails": 0, "last_ok_ms": 0, "last_run_ms": 0,
                "last_error": None, "next_delay_s": None}
            for n, i in (("summary", SUMMARY_INTERVAL_S), ("cvd", CVD_INTERVAL_S), ("liqmap", LIQMAP_INTERVAL_S))}
        self.recorder_restarts = 0
        self._session: Any = None
        self._gate: asyncio.Semaphore | None = None         # 三个刷新循环同一时刻只许一个在触网
        self._cvd_first = True
        self._depth_err_at = 0.0
        self._stopping = False
        self._rec_session: Any = None                       # 注入给记录器的会话（由我们负责关）

    # ---------- 请求路径（纯内存 / 本地库） ----------
    def gz(self, slot: str, body: bytes) -> bytes:
        """小载荷的 gzip 版，按「原字节对象是否换过」缓存。"""
        ent = self._gz.get(slot)
        if ent is None or ent[0] is not body:
            ent = (body, _gzip.compress(body, compresslevel=5))
            self._gz[slot] = ent
        return ent[1]

    def summary_body(self) -> bytes:
        return self.summary_json if self.enabled else _EMPTY_SUMMARY

    def cvd_body(self, tf: str) -> bytes:
        return cvd_mod.get_cached_json(tf) if self.enabled else _EMPTY_CVD[tf]

    def liqmap_body(self, days: int, gz: bool) -> bytes:
        if not self.enabled:
            return self.gz("liq-empty", _EMPTY_LIQMAP) if gz else _EMPTY_LIQMAP
        return liq_mod.get_cached_json(days, gz=gz)

    async def depth_body(self, hours: int, sel: tuple, cols: int, gz: bool) -> bytes:
        if not self.enabled or self.store is None:
            return self.gz("depth-empty", _EMPTY_DEPTH) if gz else _EMPTY_DEPTH
        store, rec = self.store, (self.recorder if self.started else None)

        def build() -> bytes:
            return depth_mod.heatmap_json(store, hours, sel, cols, recorder=rec, gzip=gz, symbol=SYMBOL)

        return await self.depth_cache.get((hours, sel, cols, gz), build)

    @property
    def started(self) -> bool:
        return any(not t.done() for t in self.tasks)

    def _confirmed_ms(self, name: str, published_ms: int) -> int:
        """缓存「最近一次被确认是最新的」时刻。CVD 只在棒收盘时才重新发布（5 分钟一次甚至更久），
        其间刷新循环每分钟都确认过「没有新棒、缓存仍是最新」—— 页面状态点要看的是这个时刻，
        否则每个周期末尾都会误报「过期」。从没发布过则为 0。"""
        if not published_ms:
            return 0
        return max(int(published_ms), int(self.loops[name]["last_ok_ms"]))

    def health(self, full: bool = False) -> dict:
        """/api/flow/health。匿名只给页面状态点用得着的几项；full=True（已登录）附带排查细节。"""
        rec = self.recorder
        if self.enabled and rec is not None:
            st = rec.status()
            recorder = {k: st.get(k) for k in ("running", "sources", "interval_s", "started",
                                                "last_snapshot", "last_ok", "last_error", "snapshots")}
        else:
            st = None
            recorder = {"running": False, "sources": list(self.settings.sources) if self.enabled else [],
                        "interval_s": self.settings.interval_s, "started": None, "last_snapshot": None,
                        "last_ok": {n: None for n in depth_mod.ALL_SOURCES},
                        "last_error": {n: None for n in depth_mod.ALL_SOURCES}, "snapshots": 0}
        if self.enabled:
            caches = {"summary": int(self.summary_ts),
                      "liqmap": self._confirmed_ms("liqmap", liq_mod.last_updated_ms()),
                      "cvd": self._confirmed_ms("cvd", cvd_mod.last_updated_ms())}
        else:
            caches = {"summary": 0, "liqmap": 0, "cvd": 0}
        out = {"enabled": self.enabled, "recorder": recorder, "caches": caches, "ts": _now_ms()}
        if full:
            out["detail"] = {
                "recorder": st, "recorder_restarts": self.recorder_restarts,
                "refreshers": {n: dict(v) for n, v in self.loops.items()},
                "summary_complete": self.summary_complete,
                "cvd": cvd_mod.status() if self.enabled else None,
                "liqmap": liq_mod.status() if self.enabled else None,
                "depth_cache": self.depth_cache.stats(),
                "upstream_budget": upstream_budget(self.settings),
                "settings": {"interval_s": self.settings.interval_s, "keep_days": self.settings.keep_days,
                             "keep_raw_days": self.settings.keep_raw_days,
                             "half_range_pct": self.settings.half_range_pct,
                             "sources": list(self.settings.sources),
                             "all_sources": list(self.settings.all_sources),
                             "db": self.settings.db_path.name},
            }
        return out

    # ---------- 后台：单次刷新（每个都自带超时；失败抛异常或返回 False，由 _loop 统一处理） ----------
    async def _get_session(self):
        """刷新循环共用的会话：不带任何密钥头（绝不能拿引擎那条带 X-MBX-APIKEY 的会话去访问 OKX）。"""
        s = self._session
        if s is None or getattr(s, "closed", False):
            if self.session_factory is not None:
                s = self.session_factory()
            else:
                s = aiohttp.ClientSession(connector=aiohttp.TCPConnector(limit=4, ttl_dns_cache=300))
            self._session = s
        return s

    def yield_reason(self) -> str | None:
        """币安合约那边吃紧时返回原因（三个刷新循环本轮让路）。读的是记录器的内存状态，不触网。

        两种情况：① 记录器最近一次带回的「本 IP 近 1 分钟已用权重」≥ WEIGHT_YIELD；
        ② 记录器的币安合约源正因 429/418 或权重保护处于冷却期 —— 被限流/封禁期间继续发请求会延长封禁，
        而状态卡走的是引擎那条 BinanceRest（它遇到 429 只会睡一下再重试），所以在这里统一拦住。
        """
        rec = self.recorder
        if rec is None:
            return None
        try:
            st = rec.status()
            used, at = (st.get("weight") or {}).get("binance"), (st.get("last_ok") or {}).get("binance")
            cooling = (st.get("cooldown_s") or {}).get("binance") or 0
            err = str((st.get("last_error") or {}).get("binance") or "")
        except Exception:                               # noqa: BLE001
            return None
        if cooling > 0 and ("限流" in err or "权重" in err):
            return f"币安合约源冷却中({err[:40]})，再等 {cooling:.0f}s"
        if used is not None and at and _now_ms() - at < WEIGHT_FRESH_MS and used >= WEIGHT_YIELD:
            return f"币安合约 IP 权重 {used}≥{WEIGHT_YIELD}，让路给实盘引擎"
        return None

    async def refresh_summary(self):
        """返回 True=完整；返回字符串=已发布但不完整（说明原因，不算失败）；取数失败/超时抛异常。"""
        if self.rest is None:
            raise RuntimeError("没有行情客户端(rest)，状态卡无法刷新")
        raw = await asyncio.wait_for(orderflow.brief_payload(self.rest, SYMBOL), SUMMARY_TIMEOUT_S)
        ts = _now_ms()
        payload, complete = normalize_summary(raw, ts)
        if not complete and self.summary_complete and ts - self.summary_ts < SUMMARY_KEEP_GOOD_MS:
            raise RuntimeError("OI/资金费取数不完整，沿用上一份完整结果")
        self.summary_json = _dumps(payload)             # 含 NaN/Inf 会抛 ValueError → 保留旧值
        self.summary_ts, self.summary_complete = ts, complete
        return True if complete else "OI/资金费取数不完整，状态卡只有部分数据"

    async def refresh_cvd(self) -> bool:
        session = await self._get_session()
        gap = CVD_FIRST_GAP_S if self._cvd_first else CVD_GAP_S   # 各周期错开：启动时不一口气打 8 个币安 + 6 个 OKX 请求
        self._cvd_first = False
        ok = True
        for i, tf in enumerate(cvd_mod.SUPPORTED_TFS):
            if i:
                await asyncio.sleep(gap)
            ok = bool(await asyncio.wait_for(cvd_mod.refresh(session, tf), CVD_TIMEOUT_S)) and ok
        if not ok:
            errs = [f"{tf}: {v['last_error']}" for tf, v in cvd_mod.status()["tfs"].items() if v["last_error"]]
            raise RuntimeError("; ".join(errs)[:200] or "CVD 刷新未完成")
        return True

    async def refresh_liqmap(self) -> bool:
        session = await self._get_session()
        ok = bool(await asyncio.wait_for(liq_mod.refresh(session), LIQMAP_TIMEOUT_S))   # 一次算完 3/7/14/30 天
        if not ok:
            raise RuntimeError(str(liq_mod.status().get("last_error") or "清算图刷新未完成")[:200])
        return True

    # ---------- 后台：循环 ----------
    async def _loop(self, name: str, fn: Callable[[], Any]) -> None:
        """周期刷新。任何异常都只记到状态里并拉长下一次间隔；只有取消会让它退出。"""
        st = self.loops[name]
        interval, max_backoff = st["interval_s"], MAX_BACKOFF_S[name]
        if self._gate is None:
            self._gate = asyncio.Semaphore(1)
        try:
            await asyncio.sleep(FIRST_DELAY_S[name])
            while True:
                delay = interval
                st["last_run_ms"] = _now_ms()
                try:
                    why = self.yield_reason()
                    if why:
                        raise RuntimeError(why)
                    async with self._gate:
                        note = await fn()
                    if st["fails"]:
                        log.info("订单流刷新 %s 恢复", name)
                    # fn 返回字符串 = 结果已发布但有保留（如状态卡缺 OI）：记下说明，不退避
                    st.update(fails=0, last_ok_ms=_now_ms(), last_error=note if isinstance(note, str) else None)
                except asyncio.CancelledError:
                    raise
                except Exception as e:                  # noqa: BLE001 - 后台循环绝不能把异常带出去
                    st["fails"] += 1
                    text = "超时" if isinstance(e, asyncio.TimeoutError) else f"{type(e).__name__}: {e}"[:200]
                    if text != st["last_error"]:        # 同一个错不要每轮刷一行
                        log.warning("订单流刷新 %s 失败: %s", name, text)
                    st["last_error"] = text
                    delay = backoff_delay(interval, st["fails"], max_backoff)
                st["runs"] += 1
                st["next_delay_s"] = round(delay, 1)
                await asyncio.sleep(delay * random.uniform(0.97, 1.03))
        except Exception:                               # noqa: BLE001 - 理论上到不了；到了也只记日志，不外抛
            log.exception("订单流刷新循环 %s 意外退出", name)

    async def _run_recorder(self) -> None:
        """深度记录器的看门人。recorder.run() 自己就永不抛；万一退出了，退避后重启。"""
        fails = 0
        try:
            while True:
                t0 = time.monotonic()
                try:
                    await self.recorder.run()
                    if self._stopping:
                        return
                    err = "记录器意外退出"
                except asyncio.CancelledError:
                    raise
                except Exception as e:                  # noqa: BLE001
                    err = f"{type(e).__name__}: {e}"[:200]
                fails = 1 if time.monotonic() - t0 > 600 else fails + 1
                self.recorder_restarts += 1
                delay = min(600.0, 5.0 * 2 ** min(fails, 8))
                log.error("深度记录器停了(%s)，%.0fs 后重启", err, delay)
                await asyncio.sleep(delay)
        except Exception:                               # noqa: BLE001 - 同上：只记日志，不外抛
            log.exception("深度记录器看门循环意外退出")

    def _task_done(self, task: asyncio.Task) -> None:
        """任务结束时把结果收掉：正常只会因取消而结束；别的情况记一条日志（不影响其它任务和引擎）。"""
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            log.error("订单流后台任务 %s 异常结束: %r", task.get_name(), exc)
        elif not self._stopping:
            log.error("订单流后台任务 %s 提前结束", task.get_name())

    def start(self) -> list:
        """在正在运行的事件循环里起后台任务。重复调用返回同一批任务；flow 关闭时返回 []。"""
        if not self.enabled:
            return []
        if self.started:
            return list(self.tasks)
        loop = asyncio.get_running_loop()
        s = self.settings
        self._stopping = False
        # 缺省不注入会话：记录器自建一条干净会话，并且自己负责重建/关闭
        self._rec_session = self.session_factory() if self.session_factory is not None else None
        self.recorder = depth_mod.DepthRecorder(
            self.store, self._rec_session, symbol=SYMBOL,
            sources=self.recorder_sources if self.recorder_sources is not None else s.sources,
            interval_s=s.interval_s, half_range_pct=s.half_range_pct,
            keep_days=s.keep_days, keep_raw_days=s.keep_raw_days)
        self._gate = asyncio.Semaphore(1)
        self._cvd_first = True
        specs = [("depth", self._run_recorder())]
        if self.rest is not None:
            specs.append(("summary", self._loop("summary", self.refresh_summary)))
        else:
            self.loops["summary"]["last_error"] = "没有行情客户端(rest)，状态卡未启动"
            log.warning("订单流: 没有行情客户端(rest)，状态卡不会刷新")
        specs.append(("cvd", self._loop("cvd", self.refresh_cvd)))
        specs.append(("liqmap", self._loop("liqmap", self.refresh_liqmap)))
        self.tasks = [loop.create_task(coro, name=f"flow-{name}") for name, coro in specs]
        for t in self.tasks:
            t.add_done_callback(self._task_done)
        log.info("订单流后台任务已启动: %s (深度库 %s, 每 %gs 一张快照)",
                 ", ".join(n for n, _ in specs), s.db_path, s.interval_s)
        return list(self.tasks)

    async def stop(self) -> None:
        """取消全部后台任务并收尾。可重复调用。"""
        self._stopping = True
        if self.recorder is not None:
            self.recorder.stop()
        tasks, self.tasks = self.tasks, []
        for t in tasks:
            t.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        sessions = [self._session, self._rec_session]
        self._session = self._rec_session = None
        for s in sessions:
            if s is not None and hasattr(s, "close") and not getattr(s, "closed", False):
                try:
                    await s.close()
                except Exception:                       # noqa: BLE001
                    pass
        if self.recorder is not None:
            self.recorder.close()
        self.depth_cache.close()


_RUNTIME: FlowRuntime | None = None


def get_runtime() -> FlowRuntime | None:
    return _RUNTIME


# ---------------------------------------------------------------- 对外入口
def _wants_gzip(request: Request) -> bool:
    return "gzip" in (request.headers.get("accept-encoding") or "").lower()


def _json(body: bytes, gz: bool = False) -> Response:
    headers = {"Content-Encoding": "gzip", "Vary": "Accept-Encoding"} if gz else None
    return Response(content=body, media_type="application/json", headers=headers)


def register_flow(app, cfg, db=None, engine=None, *, runtime: FlowRuntime | None = None) -> FlowRuntime:
    """注册 /api/flow/* 五个只读 GET。必须在 create_app 的静态挂载之前调用。

    db 目前用不到（深度快照在独立的 sqlite 文件里，不进 trade.db），留着是为了签名稳定。
    访问控制由 server.py 的中间件负责：/api/flow/ 前缀匿名只放 GET，并计入单 IP 每分钟请求上限。
    """
    global _RUNTIME
    rt = runtime if runtime is not None else FlowRuntime(cfg, engine)
    _RUNTIME = rt
    app.state.flow = rt

    @app.get("/api/flow/summary")
    async def flow_summary(request: Request):
        body = rt.summary_body()
        if _wants_gzip(request) and len(body) > 1024:
            return _json(rt.gz("summary", body), True)
        return _json(body)

    @app.get("/api/flow/depth")
    async def flow_depth(request: Request, hours: str = "6", src: str = "all", cols: str = "360"):
        gz = _wants_gzip(request)
        h, sel, c = norm_hours(hours), norm_src(src, rt.settings.all_sources), norm_cols(cols)
        try:
            return _json(await rt.depth_body(h, sel, c, gz), gz)
        except Busy as e:
            resp = JSONResponse({"error": "深度图计算繁忙，请稍后再试"}, status_code=503)
            resp.headers["Retry-After"] = str(int(math.ceil(e.retry_after)))
            return resp
        except Exception:                               # noqa: BLE001 - 本地库读失败等
            now = time.monotonic()
            if now - rt._depth_err_at > 60:             # 同类错误一分钟记一次堆栈
                rt._depth_err_at = now
                log.exception("深度热力图构建失败")
            return JSONResponse({"error": "深度图暂时不可用"}, status_code=503)

    @app.get("/api/flow/liqmap")
    async def flow_liqmap(request: Request, days: str = "7"):
        gz = _wants_gzip(request)
        return _json(rt.liqmap_body(norm_days(days), gz), gz)

    @app.get("/api/flow/cvd")
    async def flow_cvd(request: Request, tf: str = "1h"):
        t = norm_tf(tf)
        body = rt.cvd_body(t)
        if _wants_gzip(request) and len(body) > 1024:
            return _json(rt.gz("cvd-" + t, body), True)
        return _json(body)

    @app.get("/api/flow/health")
    async def flow_health(request: Request):
        # 中间件已把登录用户放进 request.state；登录后才给内部排查细节（权重、耗时、各循环报错）
        return JSONResponse(rt.health(full=bool(getattr(request.state, "auth_user", None))))

    return rt


def start_flow_tasks(cfg, engine=None, *, runtime: FlowRuntime | None = None,
                     session_factory: Callable[[], Any] | None = None) -> list:
    """起后台任务（深度记录器 + 状态卡/CVD/清算图刷新），返回 asyncio 任务列表。

    必须在事件循环里调用。flow.enabled=false 返回 []。任务与引擎互不等待：
    它们只在各自的协程里 sleep / 触网 / 把计算丢给线程，任何失败都留在任务内部。
    engine 只用到 engine.rest（BinanceRest，给状态卡取数）；传 None 则状态卡不刷新。
    """
    global _RUNTIME
    rt = runtime if runtime is not None else _RUNTIME
    if rt is None or (runtime is None and rt.cfg is not cfg):
        rt = _RUNTIME = FlowRuntime(cfg, engine)
    if engine is not None and getattr(engine, "rest", None) is not None:
        rt.rest = engine.rest
    if session_factory is not None:
        rt.session_factory = session_factory
    return rt.start()


async def stop_flow_tasks(runtime: FlowRuntime | None = None) -> None:
    rt = runtime if runtime is not None else _RUNTIME
    if rt is not None:
        await rt.stop()
