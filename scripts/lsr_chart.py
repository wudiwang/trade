"""LSR 信号审核出图(用户 2026-07-29 定的流程: 先出图人工审, 过了才跑期望)。
把 lsr_strategy 判定的"合格信号"逐个画出来, 标注: 前低L_prior/清扫K/小高点H0/突破/整理区/入场/止损止盈。
用法: .venv/Scripts/python scripts/lsr_chart.py --n 9 --outdir <dir>
"""
import argparse
import glob
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle
import pandas as pd
import lsr_strategy as L

plt.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei", "DejaVu Sans"]
plt.rcParams["axes.unicode_minus"] = False
CACHE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".btcache")


def build_df(kl):
    df = pd.DataFrame(kl)
    df["ts"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
    df = df.set_index("ts").sort_index()
    for c in ("open", "high", "low", "close", "volume"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    return df[["open", "high", "low", "close", "volume"]].dropna()


def render(df, r, sym, p, out):
    o = df["open"].to_numpy(); h = df["high"].to_numpy()
    lo = df["low"].to_numpy(); c = df["close"].to_numpy()
    ip, isw, ih0 = int(r["prior_low_idx"]), int(r["sweep_idx"]), int(r["h0_idx"])
    b, e = int(r["breakout_idx"]), int(r["decision_idx"])
    a = max(0, ip - 6); z = min(len(df) - 1, e + 22)
    entry_i = e + 1
    fig, ax = plt.subplots(figsize=(15, 8)); fig.patch.set_facecolor("#0e1116")
    ax.set_facecolor("#0e1116"); ax.tick_params(colors="#8b949e")
    for sp in ax.spines.values():
        sp.set_color("#30363d")
    for i in range(a, z + 1):
        up = c[i] >= o[i]; col = "#3fb950" if up else "#f85149"; x = i - a
        ax.plot([x, x], [lo[i], h[i]], color=col, lw=0.7, zorder=2)
        ax.add_patch(Rectangle((x - 0.3, min(o[i], c[i])), 0.6, max(abs(c[i] - o[i]), 1e-9),
                     facecolor=col, edgecolor=col, zorder=3))
    # 关键位横线
    ax.axhline(r["prior_low"], color="#f0a500", ls="--", lw=1.1, zorder=1)
    ax.text(0, r["prior_low"], " 前低 L_prior(流动性池)", color="#f0a500", fontsize=9, va="bottom")
    ax.axhline(r["h0"], color="#58a6ff", ls="--", lw=1.1, zorder=1)
    ax.text(0, r["h0"], " 小高点 H0", color="#58a6ff", fontsize=9, va="bottom")
    # 清扫K
    xs = isw - a
    ax.scatter([xs], [lo[isw]], marker="v", s=180, c="#ff5555", edgecolors="white", zorder=6)
    ax.annotate("清扫K(扫穿前低+带下影+放量)", (xs, lo[isw]), color="#ff5555", fontsize=9,
                ha="center", va="top")
    # 突破点
    xb = b - a
    ax.scatter([xb], [c[b]], marker="^", s=140, c="#58a6ff", edgecolors="white", zorder=6)
    ax.annotate("突破H0", (xb, h[b]), color="#58a6ff", fontsize=9, ha="center", va="bottom")
    # 整理区
    ax.add_patch(Rectangle((b - a + 0.5, r["consol_low"]), (e - b), r["consol_high"] - r["consol_low"],
                 facecolor="#c9a227", alpha=0.15, edgecolor="#c9a227", zorder=1))
    # 入场 + 止损止盈
    if entry_i <= z:
        entry = o[entry_i]; buf = p.stop_buffer_atr * r["atr"]
        stop = r["sweep_low"] - buf; risk = entry - stop; tgt = entry + p.tp_r * risk
        xe = entry_i - a
        ax.scatter([xe], [entry], marker="*", s=320, c="#ffd700", edgecolors="black", zorder=7)
        ax.annotate("入场", (xe, entry), color="#ffd700", fontsize=10, ha="center", va="bottom")
        ax.axhline(stop, color="#f85149", ls=":", lw=1, alpha=0.7)
        ax.text(z - a, stop, "止损 ", color="#f85149", fontsize=8, ha="right", va="bottom")
        ax.axhline(tgt, color="#3fb950", ls=":", lw=1, alpha=0.7)
        ax.text(z - a, tgt, f"止盈 {p.tp_r}R ", color="#3fb950", fontsize=8, ha="right", va="bottom")
    flags = [k for k in ("ok_wick", "ok_volume", "ok_reclaim", "ok_breakout", "ok_hold", "ok_pullback") if r.get(k)]
    ax.set_title(f"{sym} 5m LSR · {r['sweep_time']:%m-%d %H:%M}  放量{r['rvol']:.1f}x 回调{r['pullback']:.0%} | 过关条件: {'/'.join(flags)}",
                 color="#d6dae0", fontsize=11)
    ax.grid(True, color="#161b22", lw=0.4)
    plt.tight_layout(); fig.savefig(out, dpi=100, facecolor="#0e1116"); plt.close(fig)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=9)
    ap.add_argument("--outdir", required=True)
    ap.add_argument("--days-report", type=int, default=7)
    a = ap.parse_args()
    os.makedirs(a.outdir, exist_ok=True)
    p = L.Params()
    # 优先几个流动性好的币, 再补 pump 域
    pri = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "SUIUSDT", "DOGEUSDT", "WLDUSDT", "1000PEPEUSDT"]
    files = [os.path.join(CACHE, f"{s}_5m_30d.json") for s in pri]
    files = [f for f in files if os.path.exists(f)]
    got = 0
    for f in files:
        if got >= a.n:
            break
        sym = os.path.basename(f)[:-len("_5m_30d.json")]
        kl = json.load(open(f))
        if len(kl) < 300:
            continue
        df = L.add_indicators(build_df(kl), p)
        cutoff = df.index[-1] - pd.Timedelta(days=a.days_report)
        cands = L.scan_candidates(df, p)
        if cands.empty:
            continue
        ok = cands
        for flag in p.required:
            ok = ok[ok[flag]]
        ok = ok[ok["decision_idx"] > 0]
        ok = ok[[df.index[int(x)] >= cutoff for x in ok["decision_idx"]]]
        for _, r in ok.head(max(1, a.n // len(files) + 1)).iterrows():
            if got >= a.n:
                break
            out = os.path.join(a.outdir, f"lsr_{got+1:02d}_{sym}.png")
            render(df, r, sym, p, out)
            print(f"  出图 {os.path.basename(out)}", flush=True)
            got += 1
    print(f"共 {got} 张 → {a.outdir}")


if __name__ == "__main__":
    main()
