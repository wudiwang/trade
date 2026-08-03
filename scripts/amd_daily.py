"""日线 AMD(Accumulation-Manipulation-Distribution) 检测 + 前瞻验证(用户 2026-08-01)。

做空方向(判定"已进入日线下跌趋势"):
  A 积累: 连续 range_days 天在一个窄区间内 —— 区间高度 (H-L)/L <= range_max_pct
  M 操纵: 某天【最高价突破区间上沿】但【收盘拉回区间内】= 向上假突破扫掉空头止损/突破追多单
  D 派发: M 之后 dist_within 天内, 某天【收盘跌破区间下沿】→ 下跌趋势确立(D确认日)

严格无未来函数: 区间只用 D 之前的数据构建; 判定当天收盘后成立, 前瞻收益从次日开盘算。

验证: D确认后 5/10/20 天的前瞻收益, 与【同币全样本随机日】基线对比。
用法: .venv/Scripts/python scripts/amd_daily.py
"""
import glob
import json
import os
import statistics
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

CACHE = os.path.join(ROOT, ".btcache")

P = dict(
    range_days=10,          # A: 区间回看天数
    range_max_pct=0.25,     # A: 区间高度上限(占区间低点)
    pierce_tol=0.0,         # M: 突破区间上沿的最小幅度
    dist_within=10,         # D: M之后N天内需完成跌破
    reclaim_frac=1.0,       # M: 收盘须回到区间上沿以下(1.0=就是上沿)
)


def to_daily(k5: list) -> list:
    """5m → 日线(288根/天)。尾部不足一天丢弃。"""
    out = []
    n = (len(k5) // 288) * 288
    for i in range(0, n, 288):
        c = k5[i:i + 288]
        out.append({"open_time": int(c[0]["open_time"]), "open": float(c[0]["open"]),
                    "high": max(float(x["high"]) for x in c),
                    "low": min(float(x["low"]) for x in c),
                    "close": float(c[-1]["close"]),
                    "volume": sum(float(x["volume"]) for x in c)})
    return out


def detect_amd_sell(d: list, P: dict = P) -> list:
    """返回 [{a0,a1,rng_hi,rng_lo,m_idx,d_idx,...}]。d_idx = D确认日(收盘跌破)。"""
    out = []
    R = int(P["range_days"])
    used_to = -1
    for m in range(R, len(d) - 1):
        if m <= used_to:
            continue
        seg = d[m - R:m]                       # A: M之前的R天构成区间(不含M自己)
        hi = max(x["high"] for x in seg)
        lo = min(x["low"] for x in seg)
        if lo <= 0 or (hi - lo) / lo > float(P["range_max_pct"]):
            continue                            # 区间太宽 = 不是横盘积累
        # M: 最高价破上沿, 收盘拉回上沿之下
        if not (d[m]["high"] > hi * (1 + float(P["pierce_tol"])) and
                d[m]["close"] < hi * float(P["reclaim_frac"])):
            continue
        # D: 之后N天内收盘跌破下沿
        di = None
        for j in range(m + 1, min(len(d), m + 1 + int(P["dist_within"]))):
            if d[j]["close"] < lo:
                di = j
                break
        if di is None:
            continue
        out.append({"a0": m - R, "a1": m - 1, "rng_hi": hi, "rng_lo": lo,
                    "m_idx": m, "d_idx": di,
                    "m_time": d[m]["open_time"], "d_time": d[di]["open_time"],
                    "rng_pct": round((hi - lo) / lo * 100, 2),
                    "pierce_pct": round((d[m]["high"] / hi - 1) * 100, 2)})
        used_to = di                            # 同一段结构只取一次
    return out


def fwd(d: list, i: int, days: int):
    """从 i 日【次日开盘】起持有 days 天的收益%(前瞻, 无未来函数)。"""
    e = i + 1
    x = e + days
    if x >= len(d):
        return None
    op = d[e]["open"]
    return (d[x]["close"] - op) / op * 100 if op > 0 else None


def main():
    files = [f for f in glob.glob(os.path.join(CACHE, "*_5m_365d.json"))
             if not os.path.basename(f).startswith("sig_")]
    hits, base = {5: [], 10: [], 20: []}, {5: [], 10: [], 20: []}
    n_sig = 0
    per_sym = []
    for f in files:
        sym = os.path.basename(f)[:-len("_5m_365d.json")]
        try:
            k5 = json.load(open(f))
        except Exception:
            continue
        d = to_daily(k5)
        if len(d) < 60:
            continue
        evs = detect_amd_sell(d)
        n_sig += len(evs)
        if evs:
            per_sym.append((sym, len(evs), len(d)))
        for e in evs:
            for h in (5, 10, 20):
                r = fwd(d, e["d_idx"], h)
                if r is not None:
                    hits[h].append(r)
        for i in range(int(P["range_days"]), len(d) - 21):     # 基线: 同币所有可比日
            for h in (5, 10, 20):
                r = fwd(d, i, h)
                if r is not None:
                    base[h].append(r)
    print(f"扫描 {len(files)} 币(日线一年) → AMD做空形态 {n_sig} 次\n")
    print(f"{'前瞻':<8}{'AMD后':>22}{'基线(全样本)':>24}{'差值':>10}")
    print("-" * 66)
    for h in (5, 10, 20):
        a, b = hits[h], base[h]
        if not a:
            print(f"{h}天    无样本")
            continue
        am, bm = statistics.mean(a), statistics.mean(b)
        adn = sum(1 for x in a if x < 0) / len(a) * 100
        bdn = sum(1 for x in b if x < 0) / len(b) * 100
        print(f"{h}天    均值{am:>7.2f}% 下跌率{adn:>5.1f}% (n={len(a):<4})"
              f"均值{bm:>7.2f}% 下跌率{bdn:>5.1f}% (n={len(b):<6})"
              f"{am-bm:>+8.2f}%")
    print(f"\n出现次数最多的币:")
    for s, n, nd in sorted(per_sym, key=lambda x: -x[1])[:8]:
        print(f"   {s:<14}{n} 次 / {nd} 天")


if __name__ == "__main__":
    main()
