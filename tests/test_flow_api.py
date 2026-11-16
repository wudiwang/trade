"""订单流集成层测试：/api/flow/* 五个接口的契约、空载荷、参数收敛、访问控制、缓存与后台任务。

运行: PYTHONIOENCODING=utf-8 .venv/Scripts/python -m pytest tests/test_flow_api.py -q
全程不触网：取数层全部换成假的（假 rest、假 aiohttp 会话、假深度采集源），
应用用真实的 create_app(config.yaml + 临时 sqlite, engine=None)。
"""
import ast
import asyncio
import gzip
import json
import math
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import app.flow.api as flow_api
import app.web.server as server
from app.config import Config
from app.db import DB
from app.flow import cvd as cvd_mod
from app.flow import depth as depth_mod
from app.flow import liqmap as liq_mod
from app.flow.api import Busy, DepthResponseCache, FlowRuntime, FlowSettings

SRC4 = ["binance", "bybit", "okx", "binance_spot"]
FUT3 = ["binance", "bybit", "okx"]
M15, H1 = 900_000, 3_600_000
SUMMARY_KEYS = {"price", "chg_24h", "chg_7d", "divs", "cvd_24h", "cvd_ratio_24h", "oi", "oi_24h", "oi_7d",
                "quadrant", "quadrant_text", "funding", "funding_days", "summary", "ts"}
DEPTH_KEYS = {"symbol", "bucket", "p0", "rows", "times", "mid", "z", "zmax", "sources", "coverage", "latest",
              "recorded_minutes", "health"}
LIQ_KEYS = {"symbol", "tf", "bucket", "p0", "rows", "times", "close", "z", "zmax", "profile", "price",
            "clusters", "assumptions", "ts"}
CVD_KEYS = {"tf", "times", "price", "series", "div", "ts"}


def now_ms() -> int:
    return int(time.time() * 1000)


def num(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool)


# ================================================================ 假数据源
def P(t: int) -> float:
    """合成价格：只由时间决定，各周期、各接口看到的是同一条曲线。"""
    return 65000.0 + 900.0 * math.sin(t / 2.0e7) + 250.0 * math.sin(t / 2.7e6)


def OI(t: int) -> float:
    return 80000.0 + 2500.0 * math.sin(t / 3.0e8) + 400.0 * math.sin(t / 1.3e7)


def raw_kline(t: int, step: int, spot: bool = False) -> list:
    o, c = P(t), P(t + step)
    vol = (100.0 + 40.0 * math.sin(t / 5.0e6) ** 2) / (8.0 if spot else 1.0)
    ratio = 0.5 + (-0.05 if spot else 0.06) * math.sin(t / 9.0e6)
    return [t, f"{o:.1f}", f"{max(o, c) + 15:.1f}", f"{min(o, c) - 15:.1f}", f"{c:.1f}", f"{vol:.3f}",
            t + step - 1, f"{vol * c:.1f}", 100, f"{vol * ratio:.3f}", f"{vol * ratio * c:.1f}", "0"]


class _Resp:
    def __init__(self, data, status=200, headers=None):
        self._data, self.status, self.headers = data, status, headers or {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def json(self, content_type=None):
        return self._data

    async def text(self):
        return json.dumps(self._data)

    async def read(self):
        return json.dumps(self._data).encode()


class FakeUpstream:
    """假 aiohttp 会话：币安合约/现货 K 线、OI 历史、实时 OI；OKX 一律 500（okx_swap 是可选项）。"""
    headers: dict = {}

    def __init__(self):
        self.calls: list[str] = []
        self.fail = False            # True = 所有请求抛网络错误
        self.closed = False

    async def close(self):
        self.closed = True

    def get(self, url, params=None, timeout=None):
        p = dict(params or {})
        self.calls.append(url.split("//", 1)[-1].split("?")[0])
        if self.fail:
            raise OSError("网络断了")
        now = now_ms()
        if "/fapi/v1/klines" in url or "/api/v3/klines" in url:
            spot = "/api/v3/" in url
            step = cvd_mod.TF_MS[p["interval"]]
            limit = int(p["limit"])
            if "startTime" in p:             # 清算图：从 startTime 往后
                t0 = -(-int(p["startTime"]) // step) * step
                ts = list(range(t0, now + 1, step))[:limit]
            else:                            # CVD：最近 limit 根（含未收盘那根）
                cur = now // step * step
                ts = [cur - (limit - 1 - j) * step for j in range(limit)]
            return _Resp([raw_kline(t, step, spot) for t in ts], headers={"X-MBX-USED-WEIGHT-1M": "12"})
        if "/futures/data/openInterestHist" in url:
            end = min(int(p.get("endTime", now)), now)
            if end < now - 31 * 86400_000:
                return _Resp({"code": -1130}, 400)
            t1 = end // M15 * M15
            ts = [t1 - j * M15 for j in range(int(p["limit"]))][::-1]
            ts = [t for t in ts if t >= now - 31 * 86400_000]
            return _Resp([{"symbol": "BTCUSDT", "sumOpenInterest": f"{OI(t):.3f}", "timestamp": t} for t in ts])
        if "/fapi/v1/openInterest" in url:
            return _Resp({"symbol": "BTCUSDT", "openInterest": f"{OI(now):.3f}", "time": now})
        if "okx.com" in url:
            return _Resp({}, 500)
        raise AssertionError(f"意外的上游请求 {url}")


class FakeRest:
    """假 BinanceRest：只有 brief_payload 用到的三个方法。"""

    def __init__(self):
        self.calls: list[str] = []
        self.fail_oi = self.fail_funding = self.hang = False

    async def klines(self, symbol, interval, limit=500, start_time=None):
        self.calls.append("klines")
        if self.hang:
            await asyncio.sleep(30)
        cur = now_ms() // H1 * H1
        out = []
        for j in range(limit):
            t = cur - (limit - 1 - j) * H1
            k = raw_kline(t, H1)
            out.append((t, float(k[1]), float(k[2]), float(k[3]), float(k[4]), float(k[5]),
                        float(k[7]), float(k[9]), 1 if t < cur else 0))
        return out

    async def open_interest_hist(self, symbol, period="15m", limit=12):
        self.calls.append("oi")
        cur = now_ms() // H1 * H1
        return [] if self.fail_oi else [OI(cur - (limit - 1 - j) * H1) for j in range(limit)]

    async def funding_rate_hist(self, symbol, limit=100):
        self.calls.append("funding")
        cur = now_ms() // (8 * H1) * (8 * H1)
        return [] if self.fail_funding else [(cur - (limit - 1 - j) * 8 * H1, 0.00004 + 0.00001 * (j % 3))
                                             for j in range(limit)]


def mk_book(source: str, mid: float, reach_usd: float) -> depth_mod.Book:
    """对称盘口：每 10 美元一档直到 reach_usd，量随距离递增。"""
    bids = [(mid - 0.5, 0.5)] + [(mid - d, 1.0 + d / 100.0) for d in range(10, int(reach_usd) + 1, 10)]
    asks = [(mid + 0.5, 0.5)] + [(mid + d, 1.0 + d / 100.0) for d in range(10, int(reach_usd) + 1, 10)]
    return depth_mod.Book(source, bids, asks)


REACH = {"binance": 140.0, "bybit": 320.0, "okx": 870.0, "binance_spot": 1400.0}   # ≈ 实测的 ±0.22/0.5/1.35/2.2%


def write_snapshots(store, n=8, step_ms=30_000, sources=SRC4, end_ms=None):
    end = (end_ms or now_ms()) // step_ms * step_ms
    for i in range(n):
        ts = end - (n - 1 - i) * step_ms
        mid = 65000.0 + 5.0 * i
        books = {s: depth_mod.bucket_book(mk_book(s, mid + (30.0 if s == "binance_spot" else 0.0), REACH[s]))
                 for s in sources}
        store.write(depth_mod.make_snapshot(ts, books))
    return end


class FakeSource:
    """假深度采集源（DepthRecorder 的 sources 注入点）。"""
    weight_guard = None

    def __init__(self, name):
        self.name, self.calls = name, 0

    async def fetch_raw(self, session, timeout_s):
        self.calls += 1
        return depth_mod.RawBook(self.name, b"{}", weight=40 if self.name == "binance" else None)

    def parse(self, raw):
        return mk_book(self.name, 65000.0 + (30.0 if self.name == "binance_spot" else 0.0), REACH[self.name])


# ================================================================ 夹具
@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    cvd_mod._reset_cache()
    liq_mod._reset_cache()
    depth_mod.heatmap_cache_clear()
    monkeypatch.setattr(server, "LOGIN_FAIL_DELAY_S", 0)
    monkeypatch.setattr(flow_api, "CVD_FIRST_GAP_S", 0.0)
    monkeypatch.setattr(flow_api, "CVD_GAP_S", 0.0)
    monkeypatch.setattr(cvd_mod, "OKX_MIN_GAP_S", 0.0)
    yield
    rt = flow_api.get_runtime()
    if rt is not None:
        rt.depth_cache.close()
    cvd_mod._reset_cache()
    liq_mod._reset_cache()
    depth_mod.heatmap_cache_clear()


class Env:
    """真实 create_app + 临时库。flow 的深度库也指到临时目录。"""

    def __init__(self, tmp_path, enabled=True):
        self.tmp = tmp_path
        self.cfg = Config()
        self.cfg.set_override("flow.enabled", enabled)
        self.cfg.set_override("flow.db_path", str(tmp_path / "depth.sqlite"))
        self.db = DB(tmp_path / "t.db")
        self.app = server.create_app(self.cfg, self.db, engine=None, bot=None)
        self.rt: FlowRuntime = flow_api.get_runtime()
        self.rest, self.up = FakeRest(), FakeUpstream()
        self.rt.rest = self.rest
        self.rt.session_factory = lambda: self.up

    def client(self, ip="10.1.0.1") -> TestClient:
        return TestClient(self.app, client=(ip, 50000))

    def login(self) -> TestClient:
        c = self.client("10.1.0.9")
        r = c.post("/api/login", json={"username": self.cfg.get("web.username", "admin"),
                                       "password": self.cfg.get("web.initial_password")})
        assert r.status_code == 200, r.text
        return c

    def populate(self, snapshots=8):
        """跑一遍三个刷新（假数据）并往深度库写几张快照。"""
        async def go():
            a = await self.rt.refresh_summary()
            b = await self.rt.refresh_cvd()
            c = await self.rt.refresh_liqmap()
            return a, b, c
        res = asyncio.run(go())
        write_snapshots(self.rt.store, snapshots)
        return res


@pytest.fixture
def env(tmp_path):
    return Env(tmp_path)


def get_all(c: TestClient) -> dict:
    out = {}
    for name, path in (("summary", "/api/flow/summary"), ("depth", "/api/flow/depth"),
                       ("liqmap", "/api/flow/liqmap"), ("cvd", "/api/flow/cvd"), ("health", "/api/flow/health")):
        r = c.get(path)
        assert r.status_code == 200, (path, r.status_code, r.text[:200])
        assert r.headers["content-type"].startswith("application/json")
        out[name] = r.json()
    return out


# ================================================================ 空载荷
def assert_empty_shapes(d: dict, enabled: bool):
    s = d["summary"]
    assert set(s) == SUMMARY_KEYS and s["ts"] == 0 and s["price"] is None and s["divs"] == []
    assert set(s["funding"]) == {"state", "label", "latest", "avg", "neg", "look"} and s["funding_days"] == []
    p = d["depth"]
    assert set(p) == DEPTH_KEYS
    assert p["times"] == [] and p["z"] == [] and p["mid"] == [] and p["rows"] == 0 and p["latest"] is None
    assert p["recorded_minutes"] == 0 and p["sources"] == [] and p["symbol"] == "BTCUSDT"
    assert set(p["coverage"]) == set(SRC4) and set(p["health"]) == {"last_ok", "last_error", "snapshots"}
    q = d["liqmap"]
    assert set(q) == LIQ_KEYS and q["rows"] == 0 and q["times"] == [] and q["z"] == [] and q["close"] == []
    assert q["profile"] == {"long": [], "short": []} and q["clusters"] == {"above": [], "below": []}
    assert set(q["assumptions"]) == {"tiers", "mmr", "note"} and q["tf"] == "15m"
    v = d["cvd"]
    assert set(v) == CVD_KEYS and v["tf"] == "1h" and v["times"] == [] and v["price"] == [] and v["ts"] == 0
    assert v["series"] == {"binance_fut": [], "binance_spot": []}
    assert set(v["div"]) == {"fut_vs_price", "spot_vs_price", "fut_vs_spot"}
    assert all(set(x) == {"state", "text"} for x in v["div"].values())
    h = d["health"]
    assert h["enabled"] is enabled and h["caches"] == {"summary": 0, "liqmap": 0, "cvd": 0}
    assert h["recorder"]["running"] is False and h["recorder"]["snapshots"] == 0
    assert set(h["recorder"]["last_ok"]) == set(SRC4)


def test_开启但还没数据_各接口回合法空载荷(env):
    assert_empty_shapes(get_all(env.client()), enabled=True)
    assert not (env.tmp / "depth.sqlite").exists()          # 只读请求不建库


def test_关闭时_空载荷_不起任务_不建库(tmp_path):
    env = Env(tmp_path, enabled=False)
    assert env.rt.enabled is False and env.rt.store is None
    # 即使模块级缓存里有数据（比如之前开过），关闭后也只回空载荷
    asyncio.run(cvd_mod.refresh(env.up, "1h"))
    asyncio.run(liq_mod.refresh(env.up))
    assert cvd_mod.get_cached("1h")["times"] and liq_mod.get_cached(7)["times"]
    assert_empty_shapes(get_all(env.client()), enabled=False)
    for q in ("?tf=4h", "?tf=5m"):
        assert env.client().get("/api/flow/cvd" + q).json()["times"] == []
    assert env.client().get("/api/flow/liqmap?days=30").json()["rows"] == 0

    async def go():
        n_before = len(asyncio.all_tasks())
        tasks = flow_api.start_flow_tasks(env.cfg, type("E", (), {"rest": env.rest})())
        return tasks, len(asyncio.all_tasks()) - n_before
    tasks, extra = asyncio.run(go())
    assert tasks == [] and extra == 0 and env.rt.tasks == []
    assert env.rest.calls == [] and not (tmp_path / "depth.sqlite").exists()


# ================================================================ 有数据：逐项对契约
def test_有数据时_五个接口符合契约_且请求路径不触网(env):
    assert env.populate() == (True, True, True)
    n_up, n_rest = len(env.up.calls), len(env.rest.calls)
    assert n_up > 0 and n_rest == 3
    c = env.client()
    d = get_all(c)
    now = now_ms()

    # ---- summary ----
    s = d["summary"]
    assert set(s) == SUMMARY_KEYS and now - s["ts"] < 60_000
    for k in ("price", "chg_24h", "chg_7d", "cvd_24h", "cvd_ratio_24h", "oi", "oi_24h", "oi_7d"):
        assert num(s[k]), k
    assert [x["bars"] for x in s["divs"]] == [6, 24, 72]
    assert all(set(x) == {"bars", "price_pct", "cvd_chg", "cvd_pct_of_vol", "state", "text"} for x in s["divs"])
    assert set(s["funding"]) == {"state", "label", "latest", "avg", "neg", "look"}
    assert s["funding"]["state"] == "crowded_long" and num(s["funding"]["avg"])
    assert s["funding_days"] and all(isinstance(r, list) and len(r) == 3 for r in s["funding_days"])
    assert s["quadrant"] in ("new_longs", "short_cover", "new_shorts", "long_squeeze", "flat")
    assert s["quadrant_text"] and s["summary"]

    # ---- depth（默认 6h / all / 360）----
    p = d["depth"]
    assert set(p) == DEPTH_KEYS and p["symbol"] == "BTCUSDT"
    T, R = len(p["times"]), p["rows"]
    assert 2 <= T <= 360 and 0 < R <= 220 and p["bucket"] > 0
    assert p["times"] == sorted(p["times"]) and len(p["mid"]) == T
    assert len(p["z"]) == T and all(len(col) == R for col in p["z"])
    assert all(num(v) and v >= 0 for col in p["z"] for v in col) and p["zmax"] > 0
    assert p["sources"] == FUT3                              # all = 三个合约源，不含现货
    assert set(p["coverage"]) == set(SRC4)
    assert 0 < p["coverage"]["binance"] < p["coverage"]["bybit"] < p["coverage"]["okx"] < p["coverage"]["binance_spot"]
    lt = p["latest"]
    assert set(lt) == {"ts", "mid", "prices", "bids", "asks"}
    assert len(lt["prices"]) == len(lt["bids"]) == len(lt["asks"]) == R
    assert lt["prices"][0] == p["p0"] and lt["prices"][1] - lt["prices"][0] == p["bucket"]
    mid_row = int((lt["mid"] - p["p0"]) // p["bucket"])
    assert sum(lt["bids"][mid_row + 1:]) == 0 and sum(lt["asks"][:mid_row]) == 0    # 买在下、卖在上
    assert sum(lt["bids"]) > 0 and sum(lt["asks"]) > 0
    assert p["recorded_minutes"] == pytest.approx(3.5) and p["health"]["snapshots"] == 8
    # 三个合约源各自的总量加起来 = 合并后的总量（合并是求和）
    col_sum = sum(p["z"][-1])
    parts = [sum(c.get(f"/api/flow/depth?src={s}").json()["z"][-1]) for s in FUT3]
    assert col_sum == pytest.approx(sum(parts), rel=0.01)

    # ---- liqmap（默认 7 天）----
    q = d["liqmap"]
    assert set(q) == LIQ_KEYS and q["symbol"] == "BTCUSDT" and q["tf"] == "15m"
    T, R = len(q["times"]), q["rows"]
    assert 2 <= T <= 360 and 0 < R <= 220 and len(q["close"]) == T
    assert len(q["z"]) == T and all(len(col) == R for col in q["z"]) and q["zmax"] > 0
    assert len(q["profile"]["long"]) == R and len(q["profile"]["short"]) == R
    assert q["price"] == pytest.approx(P(now), rel=0.01) and now - q["ts"] < 60_000
    assert set(q["clusters"]) == {"above", "below"}
    for side in ("above", "below"):
        assert len(q["clusters"][side]) <= 3
        for cl in q["clusters"][side]:
            assert set(cl) == {"price", "size", "dist_pct"}
            assert (cl["price"] > q["price"]) == (side == "above") and abs(cl["dist_pct"]) <= 8.01
    a = q["assumptions"]
    assert sum(w for _, w in a["tiers"]) == pytest.approx(1.0) and a["mmr"] == 0.004 and "估算" in a["note"]
    for days in (3, 14, 30):
        qq = c.get(f"/api/flow/liqmap?days={days}").json()
        assert set(qq) == LIQ_KEYS and len(qq["times"]) >= 2
        assert (qq["times"][-1] - qq["times"][0]) / 86400_000 == pytest.approx(days, rel=0.1)

    # ---- cvd（默认 1h，以及其余周期）----
    for tf in ("1h", "5m", "15m", "4h"):
        v = d["cvd"] if tf == "1h" else c.get(f"/api/flow/cvd?tf={tf}").json()
        assert set(v) == CVD_KEYS and v["tf"] == tf
        T = len(v["times"])
        assert 150 <= T <= 200 and len(v["price"]) == T
        assert all(b - a == cvd_mod.TF_MS[tf] for a, b in zip(v["times"], v["times"][1:]))
        assert {"binance_fut", "binance_spot"} <= set(v["series"]) <= {"binance_fut", "binance_spot", "okx_swap"}
        assert all(len(x) == T and x[0] == 0 for x in v["series"].values())
        assert set(v["div"]) == {"fut_vs_price", "spot_vs_price", "fut_vs_spot"}
        assert all(x["state"] and x["text"] for x in v["div"].values())
        assert now - v["ts"] < 60_000

    # ---- health ----
    h = d["health"]
    assert h["enabled"] is True and set(h["caches"]) == {"summary", "liqmap", "cvd"}
    assert all(now - h["caches"][k] < 60_000 for k in h["caches"])
    assert "detail" not in h                                  # 匿名不给内部细节

    # ---- 以上所有请求都没有碰任何上游 ----
    assert len(env.up.calls) == n_up and len(env.rest.calls) == n_rest


def test_src选择_all只含合约_现货需点名(env):
    write_snapshots(env.rt.store, 4)
    c = env.client()
    g = lambda q: c.get("/api/flow/depth" + q).json()["sources"]          # noqa: E731
    assert g("") == FUT3 and g("?src=all") == FUT3 and g("?src=ALL,binance_spot") == FUT3
    assert g("?src=binance_spot") == ["binance_spot"]
    assert g("?src=okx,binance") == ["binance", "okx"]                    # 顺序规范化
    assert g("?src=binance,bybit,okx,binance_spot") == SRC4
    assert g("?src=nope") == FUT3 and g("?src=") == FUT3 and g("?src=,,;drop") == FUT3
    # 只录了合约源时选现货：合法的空图，而不是报错
    env2_store = depth_mod.DepthStore(env.tmp / "fut_only.sqlite")
    write_snapshots(env2_store, 3, sources=FUT3)
    p = depth_mod.heatmap_payload(env2_store, 6, "binance_spot", 360)
    assert p["times"] == [] and p["rows"] == 0 and p["latest"] is None


def test_刚开录时_已录时长不被5分钟窗口起点夸大(env):
    store, w = env.rt.store, 300_000
    w0 = now_ms() // w * w - w                                # 上一个 5 分钟窗口
    t1, t2 = w0 + 287_000, w0 + w + 64                        # 窗口快结束时才开录；13 秒后第二张
    for ts in (t1, t2):
        mid = 65000.0
        store.write(depth_mod.make_snapshot(ts, {s: depth_mod.bucket_book(mk_book(s, mid, REACH[s])) for s in FUT3}))
    assert store.rollup_pending(t2) == 1                      # 卷出一行 ts=w0 的均值行（窗口起点）
    assert store.first_ts() == t1
    p = env.client().get("/api/flow/depth?hours=24").json()   # ≥24h 读均值档
    assert p["recorded_minutes"] == pytest.approx(0.2, abs=0.05) and p["latest"]["ts"] == t2
    # 原始快照被清理之后，只能以均值行的窗口起点为准
    store.prune(keep_days=8, keep_raw_days=0, now_ms=t2 + 1)
    assert store.count("raw") == 0 and store.first_ts() == w0


def test_乱参数一律回默认_不报错(env):
    env.populate(snapshots=4)
    c = env.client()
    base = c.get("/api/flow/depth").json()
    for q in ("?hours=999", "?hours=abc", "?hours=-1", "?hours=6.5", "?hours=", "?cols=abc", "?cols=99999",
              "?hours=1e9&src=%00&cols=-5", "?hours=6&hours=7&cols=360&x=1"):
        r = c.get("/api/flow/depth" + q)
        assert r.status_code == 200, q
        assert r.json()["sources"] == FUT3 and len(r.json()["times"]) <= 360, q
    assert c.get("/api/flow/depth?hours=abc&cols=zzz").json()["times"] == base["times"]
    d7 = c.get("/api/flow/liqmap").json()
    for q in ("?days=5", "?days=abc", "?days=-7", "?days=", "?days=7.0", "?days=1000000"):
        r = c.get("/api/flow/liqmap" + q)
        assert r.status_code == 200 and r.json()["times"] == d7["times"], q
    for q in ("?tf=7m", "?tf=", "?tf=1H", "?tf=../x", "?tf=1d"):
        r = c.get("/api/flow/cvd" + q)
        assert r.status_code == 200 and r.json()["tf"] == "1h", q


def test_参数收敛函数():
    assert [flow_api.norm_hours(v) for v in (1, "6", 24, "72", 168, 2, "x", None, 6.0)] == [1, 6, 24, 72, 168, 6, 6, 6, 6]
    assert [flow_api.norm_cols(v) for v in (360, 240, 359, 100, 59, 0, -1, 10**9, "abc", None)] == \
        [360, 240, 240, 60, 60, 60, 60, 360, 360, 360]
    assert flow_api.norm_src("all") == tuple(FUT3)
    assert flow_api.norm_src("binance_spot, OKX") == ("okx", "binance_spot")
    assert flow_api.norm_src("all", ("binance",)) == ("binance",)
    assert flow_api.norm_src("x" * 10_000) == tuple(FUT3)
    assert [flow_api.norm_days(v) for v in (3, "7", 14, 30, 5, "x")] == [3, 7, 14, 30, 7, 7]
    assert [flow_api.norm_tf(v) for v in ("5m", "15m", "1h", "4h", "1H", "2h", None)] == \
        ["5m", "15m", "1h", "4h", "1h", "1h", "1h"]
    # 深度图的缓存键空间有界
    n_keys = len(flow_api.DEPTH_HOURS) * (2 ** 4 - 1) * len(flow_api.DEPTH_COLS)
    assert n_keys == 300


def test_配置解析与夹取():
    cfg = Config()
    s = FlowSettings.from_cfg(cfg)                            # config.yaml 里的 flow 段
    assert s.enabled is True and s.interval_s == 30 and s.keep_days == 8 and s.half_range_pct == 2.5
    assert s.db_path == ROOT / "data" / "flow_depth.sqlite" and s.sources == tuple(SRC4)
    assert s.all_sources == tuple(FUT3) and s.keep_raw_days == 2
    for k, v in (("flow.enabled", "false"), ("flow.interval_s", 0.1), ("flow.keep_days", "x"),
                 ("flow.half_range_pct", 99), ("flow.sources", ["okx", "nope", "binance_spot"]),
                 ("flow.db_path", "")):
        cfg.set_override(k, v)
    s = FlowSettings.from_cfg(cfg)
    assert s.enabled is False and s.interval_s == 10 and s.keep_days == 8 and s.half_range_pct == 5.0
    assert s.sources == ("okx", "binance_spot") and s.all_sources == ("okx",)
    assert s.db_path == ROOT / "data" / "flow_depth.sqlite"
    cfg.set_override("flow.sources", ["binance_spot"])
    assert FlowSettings.from_cfg(cfg).all_sources == ("binance_spot",)      # 没有合约源可合并时退到已录的源
    cfg.set_override("flow.sources", [])
    assert FlowSettings.from_cfg(cfg).sources == tuple(SRC4)


def test_稳态上游权重预算():
    b = flow_api.upstream_budget(FlowSettings.from_cfg(Config()))
    fapi, spot = b["binance_fapi_weight_per_min"], b["binance_spot_weight_per_min"]
    assert fapi["depth"] == 40 and fapi["summary"] == 2 and fapi["liqmap"] == 0.4
    assert fapi["total"] == pytest.approx(42.98, abs=0.01) and fapi["total"] < 0.02 * fapi["limit"]
    assert spot["depth"] == 500 and spot["total"] == pytest.approx(500.58, abs=0.01)


# ================================================================ 访问控制
def test_匿名可读_匿名写被拒_登录后才有细节(env):
    env.populate(snapshots=3)
    anon = env.client()
    for path in ("/api/flow/summary", "/api/flow/depth", "/api/flow/liqmap", "/api/flow/cvd", "/api/flow/health"):
        r = anon.get(path)
        assert r.status_code == 200 and r.headers["cache-control"] == "no-store", path
        for method in ("post", "put", "patch", "delete"):
            assert getattr(anon, method)(path).status_code == 401, (method, path)
    assert anon.get("/api/flow/nope").status_code == 404
    assert anon.get("/api/flow/depth/../../settings").status_code in (401, 404)
    assert anon.post("/api/settings", json={"mode": "live"}).status_code == 401
    assert "detail" not in anon.get("/api/flow/health").json()

    owner = env.login()
    assert owner.post("/api/flow/summary", json={}).status_code == 405      # 登录了也没有写接口
    h = owner.get("/api/flow/health").json()
    det = h["detail"]
    assert set(det["refreshers"]) == {"summary", "cvd", "liqmap"}
    assert det["upstream_budget"]["binance_fapi_weight_per_min"]["depth"] == 40
    assert det["settings"]["all_sources"] == FUT3 and "depth_cache" in det
    json.dumps(h)


def test_匿名请求计入单IP限流(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "ANON_API_PER_MIN", 5)      # create_app 时读取
    c = Env(tmp_path).client("10.9.9.9")
    codes = [c.get("/api/flow/health").status_code for _ in range(7)]
    assert codes[:5] == [200] * 5 and codes[5:] == [429, 429]


def test_gzip协商(env):
    env.populate(snapshots=3)
    c = env.client()
    for path in ("/api/flow/depth", "/api/flow/liqmap", "/api/flow/cvd", "/api/flow/summary"):
        plain = c.get(path, headers={"Accept-Encoding": "identity"})
        assert "content-encoding" not in plain.headers, path
        zipped = c.get(path, headers={"Accept-Encoding": "gzip"})
        assert zipped.headers.get("content-encoding") == "gzip", path
        a, b = plain.json(), zipped.json()                    # httpx 自动解压
        for d in (a, b):
            d.pop("health", None)
        assert a == b, path
    # 体积：清算图明文几百 KB，gzip 后应小得多
    raw = liq_mod.get_cached_json(7)
    assert len(liq_mod.get_cached_json(7, gz=True)) < len(raw) / 2
    assert json.loads(gzip.decompress(env.rt.liqmap_body(7, True))) == json.loads(raw)


# ================================================================ 深度图响应缓存
def test_深度缓存_同键并发只构建一次_且在线程里():
    async def go():
        c = DepthResponseCache(ttl_s=5)
        seen = []

        def build():
            seen.append(threading.current_thread().name)
            time.sleep(0.05)
            return b"x"
        out = await asyncio.gather(*(c.get("k", build) for _ in range(20)))
        again = await c.get("k", build)
        st = c.stats()
        c.close()
        return out, again, seen, st
    out, again, seen, st = asyncio.run(go())
    assert out == [b"x"] * 20 and again == b"x"
    assert len(seen) == 1 and seen[0].startswith("flow-heat") and seen[0] != threading.main_thread().name
    assert st["builds"] == 1 and st["hits"] == 1 and st["pending"] == 0 and st["entries"] == 1


def test_深度缓存_TTL过期才重建_并有条目上限():
    async def go():
        t = [100.0]
        c = DepthResponseCache(ttl_s=5, max_entries=3, clock=lambda: t[0])
        n = {"k": 0}

        def build():
            n["k"] += 1
            return b"v%d" % n["k"]
        a = await c.get("k", build)
        t[0] += 4.9
        b = await c.get("k", build)
        t[0] += 0.2
        d = await c.get("k", build)
        for key in ("a", "b", "c"):
            await c.get(key, build)
        st = c.stats()
        c.close()
        return a, b, d, st, "k" in c._d
    a, b, d, st, k_kept = asyncio.run(go())
    assert (a, b, d) == (b"v1", b"v1", b"v2")
    assert st["entries"] == 3 and not k_kept                  # 最旧的被挤掉


def test_深度缓存_构图占空比有上限():
    async def go():
        c = DepthResponseCache(ttl_s=5, duty=0.25)

        def slow():
            time.sleep(0.12)
            return b"a"
        await c.get("A", slow)                                # 用时 ~0.12s → 之后至少歇 ~0.36s
        t0 = time.perf_counter()
        await c.get("B", lambda: b"b")                        # 另一个键：没有旧图，只能等冷却
        waited = time.perf_counter() - t0
        c.close()
        return waited
    assert asyncio.run(go()) >= 0.25


def test_深度缓存_轮不到构建时先回旧图_后台照常重建():
    async def go():
        c = DepthResponseCache(ttl_s=0.01, duty=0.25)
        n = {"k": 0}

        def slow():
            n["k"] += 1
            time.sleep(0.15)
            return b"v%d" % n["k"]
        first = await c.get("A", slow)                        # 之后冷却 ~0.45s
        await asyncio.sleep(0.03)                             # 已过 TTL
        t0 = time.perf_counter()
        second = await c.get("A", slow)                       # 冷却中 + 有旧图 → 立刻回旧图
        fast = time.perf_counter() - t0
        stale_served = c.stats()["stale_served"]
        await asyncio.sleep(1.0)                              # 后台重建完成
        st = c.stats()
        third = c._d["A"][1]
        c.close()
        return first, second, fast, stale_served, st, third
    first, second, fast, stale_served, st, third = asyncio.run(go())
    assert first == second == b"v1" and fast < 0.1 and stale_served == 1
    assert st["builds"] == 2 and third == b"v2" and st["pending"] == 0


def test_深度缓存_排队有上限_最近请求的先做_被挤掉的报繁忙():
    async def go():
        c = DepthResponseCache(ttl_s=5, max_pending=2, max_wait_s=5.0, duty=1.0)
        order = []

        def mk(name, dur=0.0):
            def build():
                order.append(name)
                time.sleep(dur)
                return name.encode()
            return build
        a = asyncio.ensure_future(c.get("A", mk("A", 0.2)))
        await asyncio.sleep(0.05)                             # A 已经在线程里构建
        waiters = []
        for name in ("B", "C", "D"):                          # 用户连点三个窗口：排队 B,C,D 超过 2 → 丢掉最久的 B
            waiters.append(asyncio.ensure_future(c.get(name, mk(name))))
            await asyncio.sleep(0)
        res = await asyncio.gather(a, *waiters, return_exceptions=True)
        st = c.stats()
        c.close()
        return res, order, st
    res, order, st = asyncio.run(go())
    assert res[0] == b"A" and isinstance(res[1], Busy) and res[2:] == [b"C", b"D"]
    assert order == ["A", "D", "C"]                           # 最后点的那张最先出来
    assert st["dropped"] == 1 and st["busy"] == 1 and st["builds"] == 3 and st["pending"] == 0


def test_深度缓存_等太久且无旧图报繁忙_构建仍在后台完成():
    async def go():
        c = DepthResponseCache(ttl_s=5, max_wait_s=0.05)

        def slow():
            time.sleep(0.3)
            return b"a"
        with pytest.raises(Busy):
            await c.get("A", slow)
        await asyncio.sleep(0.5)
        done = await c.get("A", slow)                         # 刚才那次其实算完了，现在直接命中
        st = c.stats()
        c.close()
        return done, st
    done, st = asyncio.run(go())
    assert done == b"a" and st["builds"] == 1 and st["busy"] == 1 and st["hits"] == 1


def test_深度缓存_构建抛异常_有旧图回旧图_没有则上抛():
    async def go():
        t = [0.0]
        c = DepthResponseCache(ttl_s=5, error_cooldown_s=0.0, clock=lambda: t[0])

        def boom():
            raise RuntimeError("库坏了")
        with pytest.raises(RuntimeError):
            await c.get("A", boom)
        ok = await c.get("A", lambda: b"good")
        t[0] += 6
        stale = await c.get("A", boom)
        st = c.stats()
        c.close()
        return ok, stale, st
    ok, stale, st = asyncio.run(go())
    assert ok == stale == b"good" and st["errors"] == 2 and st["pending"] == 0


def test_深度接口_多人同看只构建一次(env, monkeypatch):
    write_snapshots(env.rt.store, 6)
    real, calls = depth_mod.heatmap_json, []

    def counting(*a, **kw):
        calls.append((a[1:], kw.get("gzip")))
        return real(*a, **kw)
    monkeypatch.setattr(depth_mod, "heatmap_json", counting)
    c1, c2 = env.client("10.2.0.1"), env.client("10.2.0.2")
    bodies = [c.get("/api/flow/depth?hours=6&src=all&cols=360").json() for c in (c1, c2, c1, c2, c1)]
    assert len(calls) == 1 and all(b == bodies[0] for b in bodies)
    # 等价写法（乱参数回默认）也落在同一个缓存键上
    c1.get("/api/flow/depth?hours=6.5&src=nope&cols=abc")
    c1.get("/api/flow/depth")
    assert len(calls) == 1
    c1.get("/api/flow/depth?hours=1")                          # 另一个窗口 = 另一个键
    assert len(calls) == 2 and calls[1][0][0] == 1
    assert env.rt.depth_cache.stats()["hits"] >= 6


def test_深度接口_构图失败回503_恢复后正常(env, monkeypatch):
    write_snapshots(env.rt.store, 3)
    env.rt.depth_cache.error_cooldown_s = 0.0
    real = depth_mod.heatmap_json

    def boom(*a, **kw):
        raise RuntimeError("database disk image is malformed")
    monkeypatch.setattr(depth_mod, "heatmap_json", boom)
    c = env.client()
    r = c.get("/api/flow/depth")
    assert r.status_code == 503 and "error" in r.json() and "malformed" not in r.text   # 不把内部错误原文给出去
    monkeypatch.setattr(depth_mod, "heatmap_json", real)
    assert c.get("/api/flow/depth").status_code == 200


def test_深度接口_繁忙回503带RetryAfter(env, monkeypatch):
    async def busy(*a, **kw):
        raise Busy(3)
    monkeypatch.setattr(env.rt.depth_cache, "get", busy)
    r = env.client().get("/api/flow/depth")
    assert r.status_code == 503 and r.headers["retry-after"] == "3"


# ================================================================ 状态卡刷新
def test_状态卡_缺OI或资金费时不拿缺数据下结论(env):
    env.rest.fail_oi = True
    note = asyncio.run(env.rt.refresh_summary())
    assert isinstance(note, str) and "不完整" in note          # 已发布，但标注不完整（不算失败、不退避）
    s = env.client().get("/api/flow/summary").json()
    assert set(s) == SUMMARY_KEYS and num(s["price"]) and s["ts"] > 0
    assert s["oi"] is None and s["oi_24h"] is None and s["quadrant"] == "" and "不可用" in s["quadrant_text"]
    assert s["summary"] == ""                                 # 那句总结会把缺失当成"减仓"，去掉
    assert s["funding"]["state"] == "crowded_long"

    env.rest.fail_oi, env.rest.fail_funding = False, True
    asyncio.run(env.rt.refresh_summary())
    s = env.client("10.1.0.2").get("/api/flow/summary").json()
    assert num(s["oi"]) and s["quadrant"] and s["funding"]["state"] == "unknown"
    assert s["funding"]["avg"] is None and s["funding_days"] == [] and s["summary"] == ""


def test_状态卡_已有完整结果时_不完整的新结果不覆盖(env):
    assert asyncio.run(env.rt.refresh_summary()) is True
    good = env.rt.summary_json
    env.rest.fail_oi = True
    with pytest.raises(RuntimeError, match="沿用"):
        asyncio.run(env.rt.refresh_summary())
    assert env.rt.summary_json is good
    env.rt.summary_ts -= flow_api.SUMMARY_KEEP_GOOD_MS + 1     # 旧结果太老了：宁可给不完整的新数据
    assert isinstance(asyncio.run(env.rt.refresh_summary()), str)
    assert env.rt.summary_json is not good


def test_状态卡_取数超时或没有rest_保留旧值(env, monkeypatch):
    asyncio.run(env.rt.refresh_summary())
    good = env.rt.summary_json
    monkeypatch.setattr(flow_api, "SUMMARY_TIMEOUT_S", 0.05)
    env.rest.hang = True
    t0 = time.perf_counter()
    with pytest.raises(asyncio.TimeoutError):
        asyncio.run(env.rt.refresh_summary())
    assert time.perf_counter() - t0 < 2 and env.rt.summary_json is good
    env.rt.rest = None
    with pytest.raises(RuntimeError):
        asyncio.run(env.rt.refresh_summary())
    assert env.rt.summary_json is good


def test_cvd与清算图_上游失败时抛出原因_旧缓存保留(env):
    env.populate(snapshots=0)
    liq_before, cvd_before = liq_mod.get_cached_json(7), cvd_mod.get_cached_json("1h")
    env.up.fail = True
    liq_mod._status["last_attempt_ms"] = 0                    # 允许立刻再取一次
    with pytest.raises(RuntimeError):
        asyncio.run(env.rt.refresh_liqmap())
    for st in cvd_mod._state.values():                        # 假装又收了一根棒：需要重新取数
        st["last_bar"] -= 10 * cvd_mod.TF_MS["4h"]
        st["attempt_ms"] = 0
    with pytest.raises(RuntimeError):
        asyncio.run(env.rt.refresh_cvd())
    assert liq_mod.get_cached_json(7) is liq_before and cvd_mod.get_cached_json("1h") is cvd_before


def test_health的缓存时间戳_取最近一次确认最新的时刻(env):
    rt = env.rt
    # 从没发布过：即使刷新循环"成功"过（比如只是没有新棒），也不能报成有数据
    rt.loops["cvd"]["last_ok_ms"] = now_ms()
    assert rt.health()["caches"] == {"summary": 0, "liqmap": 0, "cvd": 0}
    env.populate(snapshots=0)
    published = cvd_mod.last_updated_ms()
    rt.loops["cvd"]["last_ok_ms"] = 0
    assert rt.health()["caches"]["cvd"] == published
    # CVD 五分钟才重新发布一次，其间循环每分钟确认「仍是最新」→ 状态点不该误报过期
    rt.loops["cvd"]["last_ok_ms"] = published + 240_000
    rt.loops["liqmap"]["last_ok_ms"] = liq_mod.last_updated_ms() + 1000
    c = rt.health()["caches"]
    assert c["cvd"] == published + 240_000 and c["liqmap"] == liq_mod.last_updated_ms() + 1000
    assert c["summary"] == rt.summary_ts


# ================================================================ 后台循环
def test_退避算式():
    f = flow_api.backoff_delay
    assert [f(60, n, 900) for n in range(6)] == [60, 120, 240, 480, 900, 900]
    assert f(300, 1, 1800) == 600 and f(300, 3, 1800) == 1800 and f(60, 50, 10**9) == 60 * 64


def test_刷新循环_失败只记状态并退避_永不外抛_恢复后清零(env, monkeypatch):
    monkeypatch.setattr(flow_api, "FIRST_DELAY_S", {"summary": 0.0, "cvd": 0.0, "liqmap": 0.0})
    monkeypatch.setattr(flow_api, "MAX_BACKOFF_S", {"summary": 0.03, "cvd": 0.03, "liqmap": 0.03})
    rt = env.rt
    rt.loops["summary"]["interval_s"] = 0.01
    mode = {"v": "boom"}

    async def fn():
        if mode["v"] == "boom":
            raise ValueError("坏了")
        if mode["v"] == "timeout":
            await asyncio.wait_for(asyncio.sleep(5), 0.01)
        return True

    async def go():
        task = asyncio.ensure_future(rt._loop("summary", fn))
        await asyncio.sleep(0.25)
        s1 = dict(rt.loops["summary"])
        mode["v"] = "timeout"
        await asyncio.sleep(0.2)
        s2 = dict(rt.loops["summary"])
        mode["v"] = "ok"
        await asyncio.sleep(0.2)
        s3 = dict(rt.loops["summary"])
        alive = not task.done()
        task.cancel()
        res = await asyncio.gather(task, return_exceptions=True)
        return s1, s2, s3, alive, res
    s1, s2, s3, alive, res = asyncio.run(go())
    assert s1["fails"] >= 3 and "ValueError: 坏了" in s1["last_error"] and s1["last_ok_ms"] == 0
    assert s2["last_error"] == "超时" and s2["fails"] > s1["fails"]
    assert s3["fails"] == 0 and s3["last_error"] is None and s3["last_ok_ms"] > 0
    assert alive and isinstance(res[0], asyncio.CancelledError)


def test_刷新循环_币安权重吃紧时让路(env, monkeypatch):
    monkeypatch.setattr(flow_api, "FIRST_DELAY_S", {"summary": 0.0, "cvd": 0.0, "liqmap": 0.0})
    rt = env.rt
    rt.loops["cvd"]["interval_s"] = 0.01
    weight = {"v": flow_api.WEIGHT_YIELD + 300}

    class Rec:
        def status(self):
            return {"weight": {"binance": weight["v"]}, "last_ok": {"binance": now_ms()}}
    rt.recorder = Rec()
    called = []

    async def fn():
        called.append(1)
        return True

    async def go():
        task = asyncio.ensure_future(rt._loop("cvd", fn))
        await asyncio.sleep(0.1)
        n1, err1 = len(called), rt.loops["cvd"]["last_error"]
        weight["v"] = 100
        await asyncio.sleep(1.0)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        return n1, err1
    n1, err1 = asyncio.run(go())
    assert n1 == 0 and "让路" in err1                          # 权重高：一次都没去取数
    assert len(called) >= 1 and rt.loops["cvd"]["last_error"] is None
    # 记录器的币安合约源因 429/418 在冷却：同样让路（状态卡走的 BinanceRest 自己不会停手）
    def rec_with(status: dict):
        return type("R", (), {"status": lambda self: status})()

    rt.recorder = rec_with({"weight": {"binance": 50}, "last_ok": {"binance": now_ms()},
                            "cooldown_s": {"binance": 540}, "last_error": {"binance": "被限流 HTTP 418"}})
    assert "冷却" in rt.yield_reason()
    rt.recorder = rec_with({"weight": {}, "last_ok": {}, "cooldown_s": {"binance": 60},
                            "last_error": {"binance": "超时(>8s)"}})          # 普通网络错误的退避不算
    assert rt.yield_reason() is None
    # 读数太旧（记录器停了）就不再据此让路
    rt.recorder = rec_with({"weight": {"binance": 9999}, "last_ok": {"binance": now_ms() - 10 * 60_000}})
    assert rt.yield_reason() is None
    rt.recorder = rec_with({})                                                 # 状态里什么都没有也不出错
    assert rt.yield_reason() is None


def test_后台任务_用假数据源启动_填满缓存_并能干净收尾(tmp_path, monkeypatch):
    monkeypatch.setattr(flow_api, "FIRST_DELAY_S", {"summary": 0.0, "cvd": 0.0, "liqmap": 0.0})
    env = Env(tmp_path)
    env.rt.recorder_sources = {n: FakeSource(n) for n in SRC4}
    engine = type("E", (), {"rest": env.rest})()

    async def go():
        tasks = flow_api.start_flow_tasks(env.cfg, engine, session_factory=lambda: env.up)
        again = flow_api.start_flow_tasks(env.cfg, engine)             # 重复调用不重复起任务
        names = sorted(t.get_name() for t in tasks)
        rt = flow_api.get_runtime()
        for _ in range(200):
            rec = rt.recorder.status()
            if (rt.summary_ts and cvd_mod.last_updated_ms() and liq_mod.last_updated_ms()
                    and rec["snapshots"] >= 1 and all(cvd_mod.get_cached(tf)["times"] for tf in cvd_mod.SUPPORTED_TFS)):
                break
            await asyncio.sleep(0.05)
        health = rt.health(full=True)
        body = json.loads(await rt.depth_body(6, tuple(FUT3), 360, False))
        await flow_api.stop_flow_tasks()
        return tasks, again, names, health, body, rt
    tasks, again, names, health, body, rt = asyncio.run(go())
    assert names == ["flow-cvd", "flow-depth", "flow-liqmap", "flow-summary"]
    assert [id(t) for t in again] == [id(t) for t in tasks]
    assert rt is env.rt and all(t.done() for t in tasks) and rt.tasks == [] and env.up.closed
    assert health["recorder"]["running"] is True and health["recorder"]["snapshots"] >= 1
    assert all(health["recorder"]["last_ok"][n] for n in SRC4)
    assert all(v > 0 for v in health["caches"].values())
    assert all(v["last_error"] is None and v["fails"] == 0 for v in health["detail"]["refreshers"].values())
    assert body["sources"] == FUT3 and body["latest"] is not None and body["health"]["snapshots"] >= 1
    assert (tmp_path / "depth.sqlite").exists()
    assert set(env.rest.calls) == {"klines", "oi", "funding"}


def test_后台任务_没有rest时其余照常_状态卡标明未启动(tmp_path, monkeypatch):
    monkeypatch.setattr(flow_api, "FIRST_DELAY_S", {"summary": 0.0, "cvd": 0.0, "liqmap": 0.0})
    env = Env(tmp_path)
    env.rt.rest = None
    env.rt.recorder_sources = {"binance": FakeSource("binance")}

    async def go():
        tasks = flow_api.start_flow_tasks(env.cfg, None, session_factory=lambda: env.up)
        names = sorted(t.get_name() for t in tasks)
        await asyncio.sleep(0.05)
        await flow_api.stop_flow_tasks()
        return names
    assert asyncio.run(go()) == ["flow-cvd", "flow-depth", "flow-liqmap"]
    assert "rest" in env.rt.loops["summary"]["last_error"]


def test_记录器看门人_意外退出后重启_取消时干净退出(env, monkeypatch):
    rt = env.rt
    runs = []
    real_sleep = asyncio.sleep

    class Rec:
        async def run(self):
            runs.append(1)
            if len(runs) == 1:
                raise RuntimeError("意外")
            if len(runs) == 2:
                return                                     # 没被要求停却自己返回了：也算意外
            await real_sleep(30)

    rt.recorder = Rec()

    async def fast_sleep(d):
        await real_sleep(0 if d >= 5 else d)                   # 看门人的退避不真等
    monkeypatch.setattr(flow_api.asyncio, "sleep", fast_sleep)

    async def go():
        task = asyncio.ensure_future(rt._run_recorder())
        await real_sleep(0.1)
        alive = not task.done()
        task.cancel()
        res = await asyncio.gather(task, return_exceptions=True)
        return alive, res
    alive, res = asyncio.run(go())
    assert len(runs) == 3 and rt.recorder_restarts == 2 and alive
    assert isinstance(res[0], asyncio.CancelledError)


# ================================================================ 接线
def test_路由注册失败不连累控制台(tmp_path, monkeypatch):
    def boom(*a, **kw):
        raise RuntimeError("flow 坏了")
    monkeypatch.setattr(flow_api, "register_flow", boom)
    cfg = Config()
    app = server.create_app(cfg, DB(tmp_path / "t.db"), engine=None, bot=None)
    c = TestClient(app, client=("10.3.0.1", 50000))
    assert c.get("/api/me").json() == {"logged_in": False, "user": None}
    assert c.get("/").status_code == 200 and c.get("/flow.html").status_code == 200
    assert c.get("/api/flow/summary").status_code == 404


def test_静态页与控制台入口(env):
    c = env.client()
    assert "订单流" in c.get("/flow.html").text
    assert "/api/flow/depth" in c.get("/flow.js").text
    assert c.get("/flow.css").status_code == 200 and c.get("/lib/lwc.js").status_code == 200
    assert 'href="/flow.html"' in c.get("/").text


def test_main里_任务在引擎启动之后起_且启停都被隔离():
    src = (ROOT / "app" / "main.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    assert src.index("await engine.start()") < src.index("start_flow_tasks(cfg, engine)")

    def guarded(name: str) -> bool:
        """对 name 的调用是否位于某个捕获 Exception 的 try 体内。"""
        for node in ast.walk(tree):
            if isinstance(node, ast.Try) and any(
                    isinstance(h.type, ast.Name) and h.type.id == "Exception" for h in node.handlers):
                for sub in node.body:
                    for n in ast.walk(sub):
                        if isinstance(n, ast.Call) and getattr(n.func, "id", "") == name:
                            return True
        return False
    assert guarded("start_flow_tasks") and guarded("stop_flow_tasks")
    # 收尾：不管订单流停得怎样，bot / 引擎的 stop 都在 finally 里
    fin = [n for n in ast.walk(tree) if isinstance(n, ast.Try) and n.finalbody
           and "engine.stop()" in ast.unparse(n.finalbody) and "stop_flow_tasks" in ast.unparse(n.body)]
    assert fin


def test_预览服务不导入引擎_机器人_下单器():
    code = (
        "import sys, importlib.util as u\n"
        f"spec = u.spec_from_file_location('flow_dev_server', r'{ROOT / 'scripts' / 'flow_dev_server.py'}')\n"
        "m = u.module_from_spec(spec); spec.loader.exec_module(m)\n"
        "bad = sorted(n for n in sys.modules if n in ('app.main', 'app.engine.core', 'app.engine.trader',"
        " 'app.engine.paper', 'app.bot.telegram', 'app.engine.binance_ws'))\n"
        "print('BAD=' + ','.join(bad))\n"
        "print('PORT=' + str(m.main.__module__))\n"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, encoding="utf-8",
                         cwd=str(ROOT), env={**os.environ, "PYTHONIOENCODING": "utf-8"}, timeout=120)
    assert out.returncode == 0, out.stderr[-800:]
    assert "BAD=\n" in out.stdout.replace("\r", ""), out.stdout
    src = (ROOT / "scripts" / "flow_dev_server.py").read_text(encoding="utf-8")
    assert "default=8532" in src and "engine=None" in src
    assert "cfg.db_path" not in src and 'DB(tmp / "console.db")' in src      # 主库是临时文件，不碰线上那份
