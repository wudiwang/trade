"""缠论底座静态验证图(用户 2026-07-27)。渲染一段5m: 蜡烛+笔+中枢框+一买二买一卖二卖。
用法: .venv/Scripts/python scripts/chan_png.py --sym BTCUSDT --tail 420 --out xxx.png
"""
import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle
from app.engine import chan_bi as CB

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE = os.path.join(ROOT, ".btcache")


def render(sym, tail, out, days=30, quality=False):
    kl = json.load(open(os.path.join(CACHE, f"{sym}_5m_{days}d.json")))
    pts, merged, seq = CB.find_buy_points(kl, apply_quality=quality)
    zs = CB.build_zhongshu(seq)
    n = len(kl)
    i0 = max(0, n - tail)
    idx_of = {int(k["open_time"]) // 1000: i for i, k in enumerate(kl)}

    fig, (ax, axv) = plt.subplots(2, 1, figsize=(19, 10), sharex=True,
                                  gridspec_kw={"height_ratios": [4, 1]})
    fig.patch.set_facecolor("#0e1116")
    for a in (ax, axv):
        a.set_facecolor("#0e1116")
        a.tick_params(colors="#8b949e")
        for sp in a.spines.values():
            sp.set_color("#30363d")

    # 蜡烛 + 量
    for i in range(i0, n):
        k = kl[i]; o, h, l, c = float(k["open"]), float(k["high"]), float(k["low"]), float(k["close"])
        up = c >= o; col = "#3fb950" if up else "#f85149"
        x = i - i0
        ax.plot([x, x], [l, h], color=col, linewidth=0.6, zorder=2)
        ax.add_patch(Rectangle((x - 0.3, min(o, c)), 0.6, max(abs(c - o), 1e-9),
                     facecolor=col, edgecolor=col, zorder=3))
        axv.bar(x, float(k["volume"]), color=col, width=0.7, alpha=0.6)

    # 笔 zigzag(仅窗口内)
    bx, by = [], []
    for f in seq:
        i = f.extreme_src_idx
        if i >= i0:
            bx.append(i - i0); by.append(f.extreme_price)
    ax.plot(bx, by, color="#c9a227", linewidth=1.1, zorder=4, label="笔")

    # 中枢框
    for z in zs:
        t0 = idx_of.get(int(kl[seq[z["start_fx"]].extreme_src_idx]["open_time"]) // 1000)
        t1 = idx_of.get(int(kl[seq[z["end_fx"]].extreme_src_idx]["open_time"]) // 1000)
        if t0 is None or t1 is None or t1 < i0:
            continue
        x0 = max(t0, i0) - i0; x1 = t1 - i0
        ax.add_patch(Rectangle((x0, z["ZD"]), max(x1 - x0, 0.5), z["ZG"] - z["ZD"],
                     facecolor="#58a6ff", alpha=0.14, edgecolor="#58a6ff", linewidth=1.0, zorder=1))

    # 买卖点
    style = {"buy1": ("一买", "#3fb950", "^", -1), "buy2": ("二买", "#2ea043", "^", -1),
             "sell1": ("一卖", "#f85149", "v", 1), "sell2": ("二卖", "#da3633", "v", 1)}
    seen = set()
    for p in pts:
        si = p["stall_idx"]
        if si < i0:
            continue
        x = si - i0; nm, col, mk, sgn = style[p["type"]]
        yprice = float(kl[si]["close"])
        pad = (max(float(k["high"]) for k in kl[i0:]) - min(float(k["low"]) for k in kl[i0:])) * 0.03
        y = yprice + sgn * pad
        ax.scatter([x], [y], marker=mk, s=130, c=col, edgecolors="white", linewidths=0.5, zorder=6)
        ax.annotate(nm, (x, y), color=col, fontsize=8, ha="center",
                    va="bottom" if sgn > 0 else "top", zorder=7)
        # 二买: 画一买低点参考线
        if p["type"] == "buy2" and p.get("ref_price") and p["type"] not in seen:
            ax.axhline(p["ref_price"], color="#2ea043", linewidth=0.5, linestyle=":", alpha=0.4, zorder=1)

    # 时间刻度
    ticks = list(range(0, n - i0, max(1, (n - i0) // 10)))
    ax.set_xticks(ticks)
    axv.set_xticklabels([time.strftime("%m-%d\n%H:%M", time.localtime(int(kl[i0 + t]["open_time"]) / 1000)) for t in ticks],
                        fontsize=7, color="#8b949e")
    try:
        plt.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei", "DejaVu Sans"]
        plt.rcParams["axes.unicode_minus"] = False
    except Exception:
        pass
    cnt = {}
    for p in pts:
        if p["stall_idx"] >= i0:
            cnt[p["type"]] = cnt.get(p["type"], 0) + 1
    ax.set_title(f"{sym} 5m 缠论底座验证  |  窗口内: 中枢{sum(1 for z in zs if (idx_of.get(int(kl[seq[z['end_fx']].extreme_src_idx]['open_time'])//1000) or 0)>=i0)} "
                 f"一买{cnt.get('buy1',0)} 二买{cnt.get('buy2',0)} 一卖{cnt.get('sell1',0)} 二卖{cnt.get('sell2',0)}",
                 color="#d6dae0", fontsize=13)
    ax.grid(True, color="#161b22", linewidth=0.5)
    axv.grid(True, color="#161b22", linewidth=0.5)
    plt.tight_layout()
    fig.savefig(out, dpi=110, facecolor="#0e1116")
    print(f"saved {out}  ({n-i0} bars)")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--sym", default="BTCUSDT")
    ap.add_argument("--tail", type=int, default=420)
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--quality", type=int, default=0)
    ap.add_argument("--out", default="chan_verify.png")
    a = ap.parse_args()
    render(a.sym, a.tail, a.out, a.days, bool(a.quality))
