"""本地回测器 + 磁盘缓存(用户 2026-06-16:本地比VPS快、绕开ssh/限速)。

特点:
- 直连币安公开 klines(无需密钥);独立IP,不抢VPS实盘带宽。
- K线拉一次存盘(.btcache/),改参数重跑秒出。
- 用 importlib 直接加载 strat_*.py(零包副作用)。

用法:
  python scripts/bt_local.py smallbig --days 30 --top 0          # 全市场USDT永续
  python scripts/bt_local.py smallbig --days 30 --top 200        # 只跑成交额前200
  python scripts/bt_local.py smallbig --days 30 --refresh        # 忽略缓存重拉
"""
import argparse
import asyncio
import importlib.util
import json
import os
import time

import sys

import aiohttp

try:
    sys.stdout.reconfigure(encoding="utf-8")     # Windows 控制台中文输出
except Exception:
    pass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE = os.path.join(ROOT, ".btcache")
os.makedirs(CACHE, exist_ok=True)
BASE = "https://fapi.binance.com"
WEIGHT_COOLDOWN = 20      # 接近权重上限时主动歇秒数


def load_strat(name):
    path = os.path.join(ROOT, "app", "engine", f"strat_{name}.py")
    spec = importlib.util.spec_from_file_location(f"strat_{name}", path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


class Banned(Exception):
    pass


async def _get(session, path, params, tries=6):
    """权重感知限速: 读 X-MBX-USED-WEIGHT-1M, 接近上限主动歇; 418=封禁直接中止。"""
    for _ in range(tries):
        async with session.get(BASE + path, params=params) as r:
            if r.status == 418:                       # IP 已封禁, 别再打
                ra = r.headers.get("Retry-After", "?")
                raise Banned(f"IP 被币安封禁(418), 需等约 {ra}s 冷却")
            if r.status == 429:
                wait = int(r.headers.get("Retry-After", "10"))
                if wait > 180:                         # 长冷却=接近封禁, 中止而非死等
                    raise Banned(f"429 长冷却 {wait}s, 中止以免升级为封禁")
                print(f"  429 限速, 等{wait+2}s", flush=True)
                await asyncio.sleep(wait + 2)
                continue
            r.raise_for_status()
            data = await r.json()
            used = int(r.headers.get("X-MBX-USED-WEIGHT-1M", "0"))
            if used > 1600:                            # 上限2400, 留足余量主动降速
                print(f"  权重已用 {used}/2400, 歇 {WEIGHT_COOLDOWN}s", flush=True)
                await asyncio.sleep(WEIGHT_COOLDOWN)
            return data
    raise RuntimeError("rate limited too many times")


async def top_symbols(session, top=0):
    data = await _get(session, "/fapi/v1/ticker/24hr", {})
    rows = [(d["symbol"], float(d.get("quoteVolume", 0))) for d in data
            if d["symbol"].endswith("USDT")]
    rows.sort(key=lambda x: -x[1])
    syms = [s for s, _ in rows]
    return syms[:top] if top > 0 else syms


async def fetch_5m(session, symbol, days, refresh=False):
    cache = os.path.join(CACHE, f"{symbol}_5m_{days}d_{time.strftime('%Y%m%d')}.json")
    if os.path.exists(cache) and not refresh:
        return json.load(open(cache))
    tf_ms = 300 * 1000
    now = int(time.time() * 1000)
    cur = now - days * 86400 * 1000 - 60 * tf_ms
    out = {}
    while cur < now:
        batch = await _get(session, "/fapi/v1/klines",
                           {"symbol": symbol, "interval": "5m", "limit": 1500, "startTime": cur})
        if not batch:
            break
        for k in batch:
            if int(k[6]) < now:   # close_time < now → 已收盘
                out[int(k[0])] = {"open_time": int(k[0]), "open": float(k[1]), "high": float(k[2]),
                                  "low": float(k[3]), "close": float(k[4]), "volume": float(k[5])}
        nxt = int(batch[-1][0]) + tf_ms
        if nxt <= cur or len(batch) < 1500:
            break
        cur = nxt
    series = [out[t] for t in sorted(out)]
    json.dump(series, open(cache, "w"))
    return series


def run_smallbig(sb, series_by_sym, days, rr_target, spans=(7, 14, 30)):
    P = dict(decline_bars=10, vol_ma=20, climax_min=3.0, climax_max=12.0, drop_pct=6.0,
             dryup_window=10, dryup_ratio=0.6, sl_buf_pct=0.3, rr_target=rr_target)
    alls = []
    ref = 0
    for sym, k5 in series_by_sym.items():
        if len(k5) < 80:
            continue
        ref = max(ref, int(k5[-1]["open_time"]) // 1000)
        for d in ("long", "short"):
            for s in sb.detect_small_to_big(k5, d, P):
                s["symbol"] = sym
                sb._settle(s, k5)
                alls.append(s)
    out = {"n_all": len(alls), "by_span": {}}
    for sp in spans:
        cut = ref - sp * 86400
        rows = [s for s in alls if s["created_at"] >= cut]
        out["by_span"][f"{sp}d"] = {
            "n": len(rows),
            "total": sb._bucket(rows, lambda s: "all").get("all", {}),
            "by_dir": sb._bucket(rows, lambda s: s["direction"]),
        }
    return out


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("strat")
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--top", type=int, default=0)
    ap.add_argument("--rr", type=float, default=2.0)
    ap.add_argument("--refresh", action="store_true")
    ap.add_argument("--conc", type=int, default=3)              # 温柔: 默认并发3
    ap.add_argument("--cached-only", action="store_true")        # 只用已缓存, 零网络
    a = ap.parse_args()

    t0 = time.time()
    series_by_sym = {}
    if a.cached_only:
        import glob
        tag = f"_5m_{a.days}d_{time.strftime('%Y%m%d')}.json"
        for f in glob.glob(os.path.join(CACHE, f"*{tag}")):
            sym = os.path.basename(f)[: -len(tag)]
            series_by_sym[sym] = json.load(open(f))
        print(f"[仅缓存] {len(series_by_sym)} 个币 (零网络)", flush=True)
    else:
        timeout = aiohttp.ClientTimeout(total=30)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            syms = await top_symbols(session, a.top)
            print(f"币种 {len(syms)} 个, 拉{a.days}天5m (并发{a.conc}, 缓存 .btcache/)", flush=True)
            sem = asyncio.Semaphore(a.conc)
            done = [0]
            banned = [False]

            async def one(sym):
                if banned[0]:
                    return
                async with sem:
                    try:
                        series_by_sym[sym] = await fetch_5m(session, sym, a.days, a.refresh)
                    except Banned as e:
                        if not banned[0]:
                            print(f"  ⛔ {e} — 停止拉取, 已缓存的可继续用", flush=True)
                        banned[0] = True
                        series_by_sym.pop(sym, None)
                    except Exception:
                        series_by_sym[sym] = []
                done[0] += 1
                if done[0] % 25 == 0:
                    print(f"  拉取 {done[0]}/{len(syms)}", flush=True)

            await asyncio.gather(*(one(s) for s in syms))
    series_by_sym = {k: v for k, v in series_by_sym.items() if v}
    fetch_s = round(time.time() - t0, 1)

    sb = load_strat(a.strat)
    tc = time.time()
    res = run_smallbig(sb, series_by_sym, a.days, a.rr)
    print(f"\n拉取{fetch_s}s 计算{round(time.time()-tc,1)}s 总命中{res['n_all']}")
    nm = {"long": "做多(抄底)", "short": "做空(顶部)"}
    print(f"==== 小转大 · 多空 · RR{a.rr} ====")
    for sp in ("7d", "14d", "30d"):
        b = res["by_span"][sp]
        t = b["total"]
        print(f"-- 近{sp} (命中{b['n']}) 合计: 已结{t.get('closed',0)} 胜率{t.get('win_rate',0)}% "
              f"期望{t.get('expectancy_r',0)}R 总R{t.get('total_r',0)}")
        for dr, x in b["by_dir"].items():
            print(f"     {nm.get(dr,dr)}: 信号{x['signals']} 已结{x['closed']} 胜率{x['win_rate']}% "
                  f"期望{x['expectancy_r']}R 总R{x['total_r']}")


if __name__ == "__main__":
    asyncio.run(main())
