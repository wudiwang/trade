"""渲染XRP: 修复后的新入场(1.0607, 圈定低位) vs 旧入场(1.08, 追H1)对比。"""
import json, os, sys, time, urllib.request
import matplotlib; matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle
plt.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei", "DejaVu Sans"]; plt.rcParams["axes.unicode_minus"] = False
SP = sys.argv[1]
L1_t = 1785246300000; entry_t = 1785300300000
url = f"https://fapi.binance.com/fapi/v1/klines?symbol=XRPUSDT&interval=15m&startTime={L1_t-6*900000}&endTime={entry_t+8*900000}&limit=200"
raw = json.load(urllib.request.urlopen(url, timeout=20))
kl = [{"t": int(k[0]), "o": float(k[1]), "h": float(k[2]), "l": float(k[3]), "c": float(k[4])} for k in raw]
tms = [k["t"] for k in kl]
def xi(ms): return min(range(len(tms)), key=lambda i: abs(tms[i]-ms))
fig, ax = plt.subplots(figsize=(16, 8)); fig.patch.set_facecolor("#0e1116"); ax.set_facecolor("#0e1116")
ax.tick_params(colors="#8b949e")
for sp in ax.spines.values(): sp.set_color("#30363d")
for i, k in enumerate(kl):
    up = k["c"] >= k["o"]; col = "#3fb950" if up else "#f85149"
    ax.plot([i, i], [k["l"], k["h"]], color=col, lw=0.8)
    ax.add_patch(Rectangle((i-0.3, min(k["o"], k["c"])), 0.6, max(abs(k["c"]-k["o"]), 1e-9), facecolor=col, edgecolor=col))
def M(label, ms, price, color, mk="^", s=150, va="top"):
    i = xi(ms); ax.scatter([i], [price], marker=mk, s=s, c=color, edgecolors="white", zorder=6)
    ax.annotate(label, (i, price), color=color, fontsize=10, ha="center", va=va, zorder=7)
ax.axhline(1.0821, color="#f0a500", ls="--", lw=1.1); ax.text(0, 1.0821, " H1前高 1.0821", color="#f0a500", va="bottom", fontsize=9)
M("爆量K/L1 1.0445", 1785246300000, 1.0445, "#ff7043", "v", 150, "top")
M("你圈的L2低 1.0549", 1785259500000+900000*3, 1.0549, "#4f8ef7", "^", 150, "bottom")
M("✅新入场 1.0607(修复后·买在低点)", 1785263100000+900000, 1.0607, "#2ea043", "*", 380, "bottom")
M("❌旧入场 1.08(追H1前高)", entry_t, 1.08, "#f85149", "x", 220, "bottom")
tks = list(range(0, len(kl), max(1, len(kl)//10)))
ax.set_xticks(tks); ax.set_xticklabels([time.strftime("%m-%d %H:%M", time.gmtime(kl[t]["t"]/1000)) for t in tks], fontsize=7, color="#8b949e", rotation=30)
ax.set_title("XRPUSDT 15m 停顿逻辑修复对比 | 新: 合并K右K+放宽死线 → 入场1.0607(低位) vs 旧: 1.08(追H1) | 时间UTC", color="#d6dae0", fontsize=12)
ax.grid(True, color="#161b22", lw=0.4); plt.tight_layout()
out = os.path.join(SP, "xrp_stall_fix.png"); fig.savefig(out, dpi=110, facecolor="#0e1116"); print("saved", out)
