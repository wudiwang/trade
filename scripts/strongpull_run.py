"""强势币回踩 · 选币 → 4h底分型买点 → 出图(供人工审核)。
用法: .venv/Scripts/python scripts/strongpull_run.py <outdir>
"""
import glob
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle
plt.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei", "DejaVu Sans"]
plt.rcParams["axes.unicode_minus"] = False
import strat_strongpull as S

CACHE = os.path.join(ROOT, ".btcache")
OUT = sys.argv[1]
CH = os.path.join(OUT, "sp_charts")
os.makedirs(CH, exist_ok=True)


def T(ms, fmt="%m-%d %H:%M"):
    return time.strftime(fmt, time.gmtime(ms / 1000))


def candles(ax, kl, a, z, w=0.3):
    for i in range(a, z + 1):
        k = kl[i]
        o, h, l, c = float(k["open"]), float(k["high"]), float(k["low"]), float(k["close"])
        col = "#3fb950" if c >= o else "#f85149"
        x = i - a
        ax.plot([x, x], [l, h], color=col, lw=0.8)
        ax.add_patch(Rectangle((x - w, min(o, c)), w * 2, max(abs(c - o), 1e-12),
                               facecolor=col, edgecolor=col))


def dark(ax):
    ax.set_facecolor("#0e1116")
    ax.tick_params(colors="#8b949e", labelsize=7)
    for sp in ax.spines.values():
        sp.set_color("#30363d")
    ax.grid(True, color="#161b22", lw=0.4)


files = [f for f in glob.glob(os.path.join(CACHE, "*_1h_30d.json"))
         if not os.path.basename(f).startswith("sig_")]
data = {}
for f in files:
    sym = os.path.basename(f)[:-len("_1h_30d.json")]
    try:
        data[sym] = json.load(open(f))
    except Exception:
        pass

# 1) 逐日筛选 → 观察池(记录该币首次入池那天)
pool = {}
for back in range(14, -1, -1):
    for sym, k in data.items():
        kk = k[:len(k) - back * 24] if back > 0 else k
        if len(kk) < 24 * 13:
            continue
        p = S.screen(sym, kk)
        if p and sym not in pool:
            pool[sym] = (back, p)
print(f"观察池: {len(pool)} 个币\n")

# 2) 在池中币上找 4h 底分型买点(仅取入池当天及之后)
sigs = []
for sym, (back, p) in pool.items():
    k1h = data[sym]
    pool_ms = int(k1h[max(0, len(k1h) - back * 24 - 1)]["open_time"])
    for b in S.find_buys(k1h):
        if b["entry_time"] >= pool_ms:
            sigs.append({**b, "symbol": sym, "pick": p, "pool_ms": pool_ms})
sigs.sort(key=lambda s: s["entry_time"])
print(f"4h底分型买点: {len(sigs)} 条\n")
print(f"{'币种':<15}{'入池涨幅':>9}{'回撤':>7}{'横盘':>6}  买点时间(UTC)      入场      止损")
for s in sigs:
    p = s["pick"]
    print(f"{s['symbol']:<15}{p.gain_x:>8}x{p.dd*100:>6.0f}%{p.days_since_high:>5}天  "
          f"{T(s['entry_time'])}   {s['entry']:<10.6g}{s['sl']:<10.6g}")

# 3) 出图: 上=日线全景(拉升+回撤+横盘), 下=4h买点
for n, s in enumerate(sigs, 1):
    sym = s["symbol"]; k1h = data[sym]; p = s["pick"]
    d = S.aggregate(k1h, 24)
    k4 = S.aggregate(k1h, 4)
    ei = min(range(len(k4)), key=lambda i: abs(int(k4[i]["open_time"]) - s["entry_time"]))
    fi = min(range(len(k4)), key=lambda i: abs(int(k4[i]["open_time"]) - s["fx_time"]))
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(14, 9),
                                   gridspec_kw={"height_ratios": [1, 1.3]})
    fig.patch.set_facecolor("#0e1116")
    dark(ax1); dark(ax2)
    # 日线
    candles(ax1, d, 0, len(d) - 1, 0.32)
    ax1.axhline(p.run_high, color="#f0a500", ls="--", lw=1)
    ax1.text(0, p.run_high, f" 高点 {p.run_high:.6g}", color="#f0a500", fontsize=8, va="bottom")
    floor = p.run_high * (1 - S.DEFAULT["max_dd"])
    ax1.axhline(floor, color="#8b949e", ls=":", lw=1)
    ax1.text(0, floor, f" 回撤{S.DEFAULT['max_dd']*100:.0f}%线 {floor:.6g}", color="#8b949e", fontsize=8, va="top")
    ax1.axhline(p.run_low, color="#4f8ef7", ls=":", lw=1)
    ax1.text(0, p.run_low, f" 起涨 {p.run_low:.6g}", color="#4f8ef7", fontsize=8, va="bottom")
    tks = list(range(0, len(d), max(1, len(d) // 8)))
    ax1.set_xticks(tks)
    ax1.set_xticklabels([T(d[t]["open_time"], "%m-%d") for t in tks], rotation=0)
    ax1.set_title(f"#{n} {sym} 日线 | 涨{p.gain_x}x 回撤{p.dd*100:.0f}% 高点后横盘{p.days_since_high}天 "
                  f"振幅收窄至{p.flat_ratio} 评分{p.score}", color="#d6dae0", fontsize=11)
    # 4h
    a = max(0, fi - 30); z = min(len(k4) - 1, ei + 18)
    candles(ax2, k4, a, z, 0.3)
    ax2.scatter([fi - a], [s["fx_low"]], marker="^", s=170, c="#4f8ef7",
                edgecolors="white", zorder=6)
    ax2.annotate("4h底分型", (fi - a, s["fx_low"]), color="#4f8ef7", fontsize=9,
                 ha="center", va="top")
    ax2.scatter([ei - a], [s["entry"]], marker="*", s=330, c="#ffd700",
                edgecolors="black", zorder=7)
    ax2.annotate(f"买入 {s['entry']:.6g}", (ei - a, s["entry"]), color="#ffd700",
                 fontsize=9, ha="center", va="bottom")
    ax2.axhline(s["sl"], color="#f85149", ls=":", lw=1)
    ax2.text(z - a, s["sl"], "止损 ", color="#f85149", ha="right", va="top", fontsize=8)
    tks2 = list(range(0, z - a + 1, max(1, (z - a) // 8)))
    ax2.set_xticks(tks2)
    ax2.set_xticklabels([T(k4[a + t]["open_time"]) for t in tks2], rotation=25)
    ax2.set_title(f"4h 买点 @ {T(s['entry_time'])} UTC", color="#d6dae0", fontsize=10)
    plt.tight_layout()
    fig.savefig(os.path.join(CH, f"sp_{n:02d}_{sym}.png"), dpi=95, facecolor="#0e1116")
    plt.close(fig)
json.dump([{k: v for k, v in s.items() if k != "pick"} for s in sigs],
          open(os.path.join(OUT, "strongpull_sigs.json"), "w"))
print(f"\n出图 {len(sigs)} 张 → {CH}")
