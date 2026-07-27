"""strong_pump 选币域: 最近2周内曾进过"滚动24h涨幅前20"的币集合(用户 2026-07-27)。
用5m缓存算, 输出币列表(供补拉1m)。选域只按涨幅取前20(单边/回调过滤留到回测时点判)。
"""
import glob
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE = os.path.join(ROOT, ".btcache")
BAR_24H = 288          # 5m×288 = 24h
DAYS = 14
GRID = 12              # 每12根(1h)采样一次排名


def main():
    tag = "_5m_30d.json"
    series = {}
    for f in glob.glob(os.path.join(CACHE, f"*{tag}")):
        sym = os.path.basename(f)[:-len(tag)]
        if sym.startswith("sig_"):
            continue
        try:
            kl = json.load(open(f))
        except Exception:
            continue
        if len(kl) < BAR_24H + 10:
            continue
        series[sym] = [(int(k["open_time"]), float(k["close"])) for k in kl]
    if not series:
        print("无5m缓存"); return
    # 公共时间轴用 BTC
    base = series.get("BTCUSDT") or max(series.values(), key=len)
    now = base[-1][0]
    cutoff = now - DAYS * 86400 * 1000
    grid = [t for t, _ in base if t >= cutoff][::GRID]
    # 每币: time->close 便于查
    idx = {s: {t: c for t, c in v} for s, v in series.items()}
    from collections import Counter
    hits = Counter()                       # 每币在榜采样次数(每次=1h)
    for gt in grid:
        chgs = []
        for s, v in series.items():
            c_now = idx[s].get(gt)
            c_prev = idx[s].get(gt - BAR_24H * 300000)
            if c_now and c_prev and c_prev > 0:
                chgs.append((s, c_now / c_prev - 1))
        chgs.sort(key=lambda x: -x[1])
        for s, _ in chgs[:20]:
            hits[s] += 1
    SUSTAIN = 6                            # 累计在榜 ≥6h 才算"持续强庄"
    ever = sorted(hits)
    sustained = sorted([s for s, n in hits.items() if n >= SUSTAIN])
    print(f"最近{DAYS}天: 曾进前20 = {len(ever)} 个; 累计在榜≥{SUSTAIN}h(持续强庄) = {len(sustained)} 个")
    print("持续强庄:", ",".join(sustained))
    with open(os.path.join(CACHE, "pump_universe.txt"), "w", encoding="utf-8") as f:
        f.write("\n".join(sustained))
    print(f"→ 已写 .btcache/pump_universe.txt ({len(sustained)}币, 供补拉1m)")


if __name__ == "__main__":
    main()
