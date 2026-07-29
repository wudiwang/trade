"""渲染被新逻辑毙掉的信号: 原入场点 + 新逻辑选中的"第二极值"(往往是远古的), 让用户判断毙得对不对。
用法: .venv/Scripts/python scripts/render_killed.py <scratchpad> [only_dir]
"""
import json, os, sys, time
sys.path.insert(0, r'C:/Users/ADMIN/trade')
import matplotlib; matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle
plt.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei", "DejaVu Sans"]; plt.rcParams["axes.unicode_minus"] = False
from app.config import get_config
from app.engine import macro_pullback as M

CACHE = r'C:/Users/ADMIN/trade/.btcache'
SP = sys.argv[1]
ONLY = sys.argv[2] if len(sys.argv) > 2 else None
OUT = os.path.join(SP, "killed_charts"); os.makedirs(OUT, exist_ok=True)
cfg = get_config()
KEYS = ("enabled","vol_ma","vol_mult","lookback","reclaim_bars","reclaim_body_pct","wyckoff_fractal_window",
        "min_leg_pct","second_tolerance_pct","stop_buffer_pct","max_signal_bars_after_second","stall_max_gap_bars",
        "min_effective_bars_between","retrace_min","retrace_max","leg_body_ratio_min","max_leg_pct")
P = {k: cfg.get(f"macro_pullback.{k}") for k in KEYS}; P = {k: v for k, v in P.items() if v is not None}
P["account_equity"] = 1000; P["risk_pct"] = 0.5
killed = json.load(open(os.path.join(SP, "killed.json")))
if ONLY:
    killed = [k for k in killed if k["direction"] == ONLY]
_c = {}
def load(sym, tf):
    k = (sym, tf)
    if k not in _c:
        f = os.path.join(CACHE, f"{sym}_{tf}_30d.json"); _c[k] = json.load(open(f)) if os.path.exists(f) else []
    return _c[k]
def T(ms): return time.strftime("%m-%d %H:%M", time.gmtime(ms/1000))

n = 0
for s in sorted(killed, key=lambda x: x["entry_time"]):
    sym, tf, d = s["symbol"], s["tf"], s["direction"]
    kl = load(sym, tf)
    if not kl: continue
    ei = min(range(len(kl)), key=lambda x: abs(int(kl[x]["open_time"]) - s["entry_time"]))
    win = kl[max(0, ei-400):ei+1]; off = max(0, ei-400)
    P["tf"] = tf
    first = M._find_spring(win, P) if d == "long" else M._find_utad(win, P)
    pick_i = None; pick_p = None
    if first:
        idx = int(first["idx"]); tol = float(P.get("second_tolerance_pct", 0.2))/100
        if d == "long":
            l1 = M._f(win[idx], "low")
            cands = [j for j in M._chan_fractal_extremes_after(win, idx+3, "bottom") if M._f(win[j], "low") >= l1*(1-tol)]
            if cands:
                j = min(cands, key=lambda x: M._f(win[x], "low")); pick_i = j+off; pick_p = M._f(win[j], "low")
        else:
            h1 = M._f(win[idx], "high")
            cands = [j for j in M._chan_fractal_extremes_after(win, idx+3, "top") if M._f(win[j], "high") <= h1*(1+tol)]
            if cands:
                j = max(cands, key=lambda x: M._f(win[x], "high")); pick_i = j+off; pick_p = M._f(win[j], "high")
    st = s.get("structure") or {}
    mts = [int(m["time"]) for m in (s.get("markers") or []) if m.get("time")]
    times = [int(k["open_time"]) for k in kl]
    def xi(ms): return min(range(len(times)), key=lambda i: abs(times[i]-ms))
    lo_i = min([xi(min(mts))] if mts else [ei]) if mts else ei
    a = max(0, min(lo_i, pick_i if pick_i is not None else lo_i) - 6); z = min(len(kl)-1, ei+10)
    if z-a > 260: a = z-260
    fig, ax = plt.subplots(figsize=(15, 7)); fig.patch.set_facecolor("#0e1116"); ax.set_facecolor("#0e1116")
    ax.tick_params(colors="#8b949e", labelsize=7)
    for sp in ax.spines.values(): sp.set_color("#30363d")
    for i in range(a, z+1):
        k = kl[i]; o,h,l,c = float(k["open"]),float(k["high"]),float(k["low"]),float(k["close"])
        col = "#3fb950" if c>=o else "#f85149"; x=i-a
        ax.plot([x,x],[l,h],color=col,lw=0.7); ax.add_patch(Rectangle((x-0.3,min(o,c)),0.6,max(abs(c-o),1e-9),facecolor=col,edgecolor=col))
    for m in (s.get("markers") or []):
        if not m.get("time"): continue
        x = xi(int(m["time"]))-a
        if x < 0 or x > z-a: continue
        lb = m.get("label",""); col = "#ff7043" if "爆量" in lb else "#4f8ef7"
        below = m.get("position") == "belowBar"
        ax.scatter([x],[m.get("price")],marker=("^" if below else "v"),s=80,c=col,edgecolors="white",zorder=6)
        ax.annotate(lb,(x,m.get("price")),color=col,fontsize=7,ha="center",va=("bottom" if below else "top"))
    long = d == "long"
    xe = ei-a
    ax.scatter([xe],[s["entry"]],marker="*",s=300,c="#ffd700",edgecolors="black",zorder=8)
    ax.annotate(f"原入场 {s['entry']}",(xe,s["entry"]),color="#ffd700",fontsize=9,ha="center",va=("bottom" if long else "top"))
    if pick_i is not None and a <= pick_i <= z:
        xp = pick_i-a
        ax.scatter([xp],[pick_p],marker="X",s=220,c="#e3b341",edgecolors="red",linewidths=1.5,zorder=9)
        ax.annotate(f"新逻辑选中的{'第二低点' if long else '第二高点'}\n{T(kl[pick_i]['open_time'])[6:]} (不合格→整条作废)",
                    (xp,pick_p),color="#e3b341",fontsize=8,ha="center",va=("top" if long else "bottom"))
        ax.axvline(xp,color="#e3b341",ls=":",lw=0.8,alpha=0.5)
    tks = list(range(0,z-a+1,max(1,(z-a)//9)))
    ax.set_xticks(tks); ax.set_xticklabels([T(kl[a+t]["open_time"]) for t in tks],rotation=25)
    ax.set_title(f"✂被毙 {sym} {tf} {'二买(多)' if long else '二卖(空)'} 原入场{T(s['entry_time'])[6:]}UTC | {s.get('why','')}",color="#d6dae0",fontsize=11)
    ax.grid(True,color="#161b22",lw=0.4); plt.tight_layout()
    n += 1
    fig.savefig(os.path.join(OUT,f"kill_{n:02d}_{sym}_{tf}_{d}.png"),dpi=95,facecolor="#0e1116"); plt.close(fig)
print(f"渲染 {n} 张 → {OUT}")
