"""每日增量刷新本地样本(用户 2026-06-16:随时间推移每天补数据)。

为每个币种 + 每个级别维护一份滚动缓存(默认保留30天)。
增量:只拉"上次缓存最后一根之后"的新K(每天约几百根),很轻、不易触发封禁。
缓存文件名沿用 {sym}_{tf}_{days}d_{今天日期}.json,看图器/回测当天即可直接用。

手动跑:   .venv/Scripts/python scripts/bt_refresh.py --tfs 5m,15m,1h --days 30 --top 0
计划任务:  见 scripts/setup_schedule.ps1(每天自动跑)
遇 418 封禁立即停,保留已更新部分,次日继续。
"""
import argparse
import asyncio
import glob
import json
import os
import sys
import time

import aiohttp

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE = os.path.join(ROOT, ".btcache")
os.makedirs(CACHE, exist_ok=True)
BASE = "https://fapi.binance.com"
TF_MS = {"1m": 60_000, "5m": 300_000, "15m": 900_000, "1h": 3_600_000}


class Banned(Exception):
    pass


async def _get(session, path, params, tries=6):
    for _ in range(tries):
        async with session.get(BASE + path, params=params) as r:
            if r.status == 418:
                raise Banned(f"IP封禁(418), Retry-After={r.headers.get('Retry-After','?')}s")
            if r.status == 429:
                wait = int(r.headers.get("Retry-After", "10"))
                if wait > 180:
                    raise Banned(f"429长冷却{wait}s")
                print(f"  429 等{wait+2}s", flush=True)
                await asyncio.sleep(wait + 2)
                continue
            r.raise_for_status()
            data = await r.json()
            if int(r.headers.get("X-MBX-USED-WEIGHT-1M", "0")) > 1600:
                await asyncio.sleep(20)
            return data
    raise RuntimeError("rate limited")


async def top_symbols(session, top):
    data = await _get(session, "/fapi/v1/ticker/24hr", {})
    rows = sorted(((d["symbol"], float(d.get("quoteVolume", 0))) for d in data
                   if d["symbol"].endswith("USDT")), key=lambda x: -x[1])
    return [s for s, _ in rows][:top] if top > 0 else [s for s, _ in rows]


def _cache_path(sym, tf, days):
    return os.path.join(CACHE, f"{sym}_{tf}_{days}d.json")


def _load_prev(sym, tf, days):
    """读最近一份该(sym,tf,days)缓存(任意日期),用于增量起点。"""
    p = os.path.join(CACHE, f"{sym}_{tf}_{days}d.json"); files = [p] if os.path.exists(p) else []
    if not files:
        return []
    try:
        return json.load(open(files[-1]))
    except Exception:
        return []


async def refresh_one(session, sym, tf, days):
    tf_ms = TF_MS[tf]
    now = int(time.time() * 1000)
    cutoff = now - days * 86400 * 1000           # 滚动窗口起点
    prev = _load_prev(sym, tf, days)
    by_t = {int(k["open_time"]): k for k in prev}
    cur = int(prev[-1]["open_time"]) if prev else cutoff - 60 * tf_ms   # 增量起点=上次最后一根
    while cur < now:
        batch = await _get(session, "/fapi/v1/klines",
                           {"symbol": sym, "interval": tf, "limit": 1500, "startTime": cur})
        if not batch:
            break
        for k in batch:
            if int(k[6]) < now:
                by_t[int(k[0])] = {"open_time": int(k[0]), "open": float(k[1]), "high": float(k[2]),
                                   "low": float(k[3]), "close": float(k[4]), "volume": float(k[5])}
        nxt = int(batch[-1][0]) + tf_ms
        if nxt <= cur or len(batch) < 1500:
            break
        cur = nxt
    series = [by_t[t] for t in sorted(by_t) if t >= cutoff]   # 滚掉超窗的旧K
    json.dump(series, open(_cache_path(sym, tf, days), "w"))
    return len(series), (len(by_t) - len(prev))


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tfs", default="5m,15m,1h")
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--top", type=int, default=0)
    ap.add_argument("--conc", type=int, default=3)
    a = ap.parse_args()
    tfs = [t for t in a.tfs.split(",") if t]
    t0 = time.time()
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as session:
        syms = await top_symbols(session, a.top)
        print(f"[{time.strftime('%Y-%m-%d %H:%M')}] 刷新 {len(syms)} 币 × {tfs} (滚动{a.days}天, 并发{a.conc})", flush=True)
        sem = asyncio.Semaphore(a.conc)
        done = [0]
        banned = [False]
        total_new = [0]

        async def one(sym):
            if banned[0]:
                return
            async with sem:
                for tf in tfs:
                    if banned[0]:
                        return
                    try:
                        n, added = await refresh_one(session, sym, tf, a.days)
                        total_new[0] += max(0, added)
                    except Banned as e:
                        if not banned[0]:
                            print(f"  ⛔ {e} — 停止, 已刷新部分保留, 次日继续", flush=True)
                        banned[0] = True
                    except Exception:
                        pass
            done[0] += 1
            if done[0] % 50 == 0:
                print(f"  {done[0]}/{len(syms)}", flush=True)

        await asyncio.gather(*(one(s) for s in syms))
    print(f"[完成] 用时{round(time.time()-t0,1)}s, 新增约{total_new[0]}根, {'(中途被封,未全)' if banned[0] else '全量OK'}", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
