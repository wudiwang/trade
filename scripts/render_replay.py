"""把 replay_0728.json 里每条信号渲染成图(供逐个审核)。标: 爆量K/L1H1L2(或H1L1H2)/入场/止损止盈。
用法: .venv/Scripts/python scripts/render_replay.py <scratchpad_dir> [max_n]
"""
import glob, json, os, sys, time
import matplotlib; matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle
plt.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei", "DejaVu Sans"]; plt.rcParams["axes.unicode_minus"] = False
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE = os.path.join(ROOT, ".btcache")
SP = sys.argv[1]
MAXN = int(sys.argv[2]) if len(sys.argv) > 2 else 999
OUTDIR = os.path.join(SP, "replay_charts"); os.makedirs(OUTDIR, exist_ok=True)
sigs = json.load(open(os.path.join(SP, "replay_0728.json")))
_cache = {}
def load(sym, tf):
    key = (sym, tf)
    if key not in _cache:
        f = os.path.join(CACHE, f"{sym}_{tf}_30d.json")
        _cache[key] = json.load(open(f)) if os.path.exists(f) else []
    return _cache[key]
def render(s, n):
    kl = load(s["symbol"], s["tf"])
    if not kl: return False
    mks = s.get("markers") or []
    times = [int(k["open_time"]) for k in kl]
    def xi(ms): return min(range(len(times)), key=lambda i: abs(times[i]-ms))
    ei = xi(s["entry_time"])
    mt = [int(m["time"]) for m in mks if m.get("time")]
    a = max(0, (xi(min(mt)) if mt else ei) - 5); z = min(len(kl)-1, ei + 12)
    fig, ax = plt.subplots(figsize=(14, 7)); fig.patch.set_facecolor("#0e1116"); ax.set_facecolor("#0e1116")
    ax.tick_params(colors="#8b949e", labelsize=7)
    for sp in ax.spines.values(): sp.set_color("#30363d")
    for i in range(a, z+1):
        k = kl[i]; o,h,l,c = float(k["open"]),float(k["high"]),float(k["low"]),float(k["close"])
        col = "#3fb950" if c>=o else "#f85149"; x=i-a
        ax.plot([x,x],[l,h],color=col,lw=0.8); ax.add_patch(Rectangle((x-0.3,min(o,c)),0.6,max(abs(c-o),1e-9),facecolor=col,edgecolor=col))
    long = s["direction"]=="long"
    for m in mks:
        if not m.get("time"): continue
        x = xi(int(m["time"]))-a; price=m.get("price"); lb=m.get("label","")
        col = "#ff7043" if "爆量" in lb else "#4f8ef7"
        below = m.get("position")=="belowBar"
        ax.scatter([x],[price],marker=("^" if below else "v"),s=90,c=col,edgecolors="white",zorder=6)
        ax.annotate(lb+(f" {m.get('vol_ratio')}x" if m.get("vol_ratio") else ""),(x,price),color=col,fontsize=7,ha="center",va=("bottom" if below else "top"))
    xe = ei-a
    ax.scatter([xe],[s["entry"]],marker="*",s=300,c="#ffd700",edgecolors="black",zorder=7)
    ax.annotate(("二买" if long else "二卖")+f" {s['entry']}",(xe,s["entry"]),color="#ffd700",fontsize=9,ha="center",va=("bottom" if long else "top"))
    ax.axhline(s["sl"],color="#f85149",ls=":",lw=1); ax.text(z-a,s["sl"],"止损 ",color="#f85149",ha="right",fontsize=7,va="top")
    ax.axhline(s["tp"],color="#3fb950",ls=":",lw=1); ax.text(z-a,s["tp"],"止盈 ",color="#3fb950",ha="right",fontsize=7,va="bottom")
    tks=list(range(0,z-a+1,max(1,(z-a)//8)))
    ax.set_xticks(tks); ax.set_xticklabels([time.strftime("%m-%d %H:%M",time.gmtime(kl[a+t]["open_time"]/1000)) for t in tks],rotation=25)
    ax.set_title(f"#{n} {s['symbol']} {s['tf']} {'二买(多)' if long else '二卖(空)'} 入场{s['entry']} 放量{s.get('vol_ratio')}x | {time.strftime('%m-%d %H:%M',time.gmtime(s['entry_time']/1000))}UTC",color="#d6dae0",fontsize=10)
    ax.grid(True,color="#161b22",lw=0.4); plt.tight_layout()
    out=os.path.join(OUTDIR,f"sig_{n:02d}_{s['symbol']}_{s['tf']}_{s['direction']}.png")
    fig.savefig(out,dpi=95,facecolor="#0e1116"); plt.close(fig); return True
sigs.sort(key=lambda s: s["entry_time"])
n=0
for s in sigs:
    if n>=MAXN: break
    if render(s, n+1): n+=1
print(f"渲染 {n} 张 → {OUTDIR}")
