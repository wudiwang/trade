"""灵感001 · 检验"只做当日涨幅榜前几的票"(用户 2026-07-12)。

关键: 排名必须用【入场那一刻】已知的信息算 —— 该币当日(UTC 00:00起)涨幅在全市场的名次。
不能用当天收盘后的涨幅榜(那是未来函数, 会把结果做假)。

币种池: .btcache 里所有有 1m 数据的币(已含30天内当过当日涨幅榜前10的168个币)。

用法: .venv/Scripts/python scripts/fvg1m_gainers.py --leg 8 --gap 0.3 --stop A --tp rr2
"""
import argparse
import bisect
import json
import math
import os
import sys
import time
from collections import Counter
from concurrent.futures import ProcessPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass
import bt_registry as R
import strat_fvg1m as S

def _job(a):
    sym, p5, p1, P = a          # P 随任务传(Windows 是 spawn, 全局变量不会带过去)
    return S.walk(sym, json.load(open(p5)), json.load(open(p1)), P)


def load_market():
    """全市场5m: 用于算'入场时该币当日涨幅'及其名次。"""
    mk = {}
    for f in os.listdir(R.CACHE):
        if not f.endswith("_5m_30d.json") or f.startswith("sig_"):
            continue
        s = f[: -len("_5m_30d.json")]
        try:
            k = json.load(open(os.path.join(R.CACHE, f)))
        except Exception:
            continue
        mk[s] = ([int(x["open_time"]) // 1000 for x in k],
                 [float(x["close"]) for x in k], [float(x["open"]) for x in k])
    return mk


def gain_at(mk, sym, ts):
    """入场时点该币的'当日涨幅%'(UTC当日开盘 → 入场前最后一根5m收盘)。"""
    v = mk.get(sym)
    if not v:
        return None
    T, C, O = v
    j = bisect.bisect_left(T, ts) - 1
    if j < 1:
        return None
    d = time.gmtime(ts)
    day0 = int(time.mktime((d.tm_year, d.tm_mon, d.tm_mday, 0, 0, 0, 0, 0, 0)) - time.timezone)
    i0 = bisect.bisect_left(T, day0)
    if i0 >= j or i0 >= len(O):
        return None
    return (C[j] - O[i0]) / O[i0] * 100


def stat(rs):
    if len(rs) < 10:
        return f"n={len(rs):<4} 样本太少"
    n = len(rs)
    m = sum(r["net_r"] for r in rs) / n
    sd = math.sqrt(sum((r["net_r"] - m) ** 2 for r in rs) / max(n - 1, 1))
    t = m / (sd / math.sqrt(n)) if sd else 0.0
    w = sum(1 for r in rs if r["pnl_r"] > 0) / n * 100
    g = sum(r["pnl_r"] for r in rs) / n
    return f"n={n:>4} 胜率{w:>5.1f}% 毛R={g:+.3f} 净R={m:+.3f} t={t:+.2f} 累计{sum(r['net_r'] for r in rs):+7.1f}R"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--leg", type=float, default=8.0)
    ap.add_argument("--gap", type=float, default=0.3)
    ap.add_argument("--stop", default="A")
    ap.add_argument("--tp", default="rr2")
    a = ap.parse_args()
    P = dict(S.BASE, min_leg_pct=a.leg, min_gap_pct=a.gap, stop=a.stop, tp=a.tp)

    files = []
    for f in sorted(os.listdir(R.CACHE)):
        if f.endswith("_1m_30d.json") and not f.startswith("sig_"):
            sym = f[: -len("_1m_30d.json")]
            p5 = os.path.join(R.CACHE, sym + "_5m_30d.json")
            if os.path.exists(p5):
                files.append((sym, p5, os.path.join(R.CACHE, f), P))
    print(f"[bt] 币种池 {len(files)} 个(有1m数据) | 上涨≥{a.leg}% 缺口≥{a.gap}% 止损{a.stop} 止盈{a.tp}\n", flush=True)

    rows = []
    with ProcessPoolExecutor() as ex:
        for v in ex.map(_job, files, chunksize=2):
            rows.extend(v)
    rows = [r for r in rows if r["result"] in ("tp", "sl", "timeout")]
    print(f"[bt] {len(rows)} 个信号\n", flush=True)

    mk = load_market()
    for r in rows:
        r["gain"] = gain_at(mk, r["symbol"], r["created_at"])
    ok = [r for r in rows if r["gain"] is not None]
    # 名次: 入场时点全市场按当日涨幅排序
    cache = {}
    for r in ok:
        ts = r["created_at"]
        key = ts // 900                      # 15分钟粒度缓存, 够用且快很多
        if key not in cache:
            cache[key] = sorted((gain_at(mk, s, ts) or -999) for s in mk)
        arr = cache[key]
        r["rank"] = len(arr) - bisect.bisect_left(arr, r["gain"])

    print("全部信号:        ", stat(ok))
    print()
    print("按【入场那一刻·该币当日涨幅在全市场的名次】分档:")
    for nm, f in (("涨幅榜 前3", lambda r: r["rank"] <= 3),
                  ("前4~10", lambda r: 3 < r["rank"] <= 10),
                  ("前11~30", lambda r: 10 < r["rank"] <= 30),
                  ("前31~100", lambda r: 30 < r["rank"] <= 100),
                  ("100名以外", lambda r: r["rank"] > 100)):
        print(f"  {nm:<10}", stat([r for r in ok if f(r)]))
    print()
    print("按【入场那一刻·该币当日涨幅%】分档:")
    for nm, f in (("+20%以上", lambda r: r["gain"] >= 20), ("+10~20%", lambda r: 10 <= r["gain"] < 20),
                  ("+5~10%", lambda r: 5 <= r["gain"] < 10), ("0~5%", lambda r: 0 <= r["gain"] < 5),
                  ("当日下跌", lambda r: r["gain"] < 0)):
        print(f"  {nm:<10}", stat([r for r in ok if f(r)]))

    top10 = [r for r in ok if r["rank"] <= 10]
    if len(top10) >= 20:
        print()
        print("涨幅榜前10 · 稳健性:")
        top10.sort(key=lambda r: r["created_at"])
        cut = top10[len(top10) // 2]["created_at"]
        print("  前半月    ", stat([r for r in top10 if r["created_at"] < cut]))
        print("  后半月    ", stat([r for r in top10 if r["created_at"] >= cut]))
        bysym = {}
        for r in top10:
            bysym.setdefault(r["symbol"], []).append(r)
        top = sorted(bysym.items(), key=lambda kv: -sum(x["net_r"] for x in kv[1]))[:3]
        tot = sum(r["net_r"] for r in top10)
        drop = sum(sum(x["net_r"] for x in v) for _, v in top)
        print(f"  贡献最大3币: " + ", ".join(f"{s}({sum(x['net_r'] for x in v):+.1f}R/{len(v)}单)" for s, v in top))
        print(f"  去掉这3币后: {tot-drop:+.1f}R", stat([r for r in top10 if r["symbol"] not in dict(top)]))
        hours = Counter(time.strftime("%m-%d %H", time.localtime(r["created_at"])) for r in top10)
        print(f"  时间扎堆: {len(hours)}个不同小时/{len(top10)}单, 最多一小时 {max(hours.values())} 单")
        json.dump(top10, open(os.path.join(R.CACHE, "sig_fvg1m_top10_30d.json"), "w"))


if __name__ == "__main__":
    main()
