"""逐根回放 macro_pullback(新停顿逻辑)在昨天(07-28 UTC)全市场的触发。
模拟实时: 每根K用 klines[:i+1] 尾窗调 detect_macro_pullback, 完成即记一条(带冷却)。
输出触发条数 + 明细JSON(供出图审核)。用法: .venv/Scripts/python scripts/replay_macro.py
"""
import glob, json, os, sys, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
try: sys.stdout.reconfigure(encoding="utf-8")
except Exception: pass
from app.config import get_config
from app.engine.macro_pullback import detect_macro_pullback

CACHE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".btcache")
OUT = sys.argv[1] if len(sys.argv) > 1 else "."
DAY_START = 1785196800000            # 07-28 00:00 UTC
DAY_END = DAY_START + 86400000       # 07-29 00:00 UTC
W = 400
cfg = get_config()
KEYS = ("enabled","exclusive","vol_ma","vol_mult","lookback","reclaim_bars","reclaim_tolerance_pct",
        "reclaim_body_pct","wyckoff_fractal_window","min_leg_pct","second_tolerance_pct","stop_buffer_pct",
        "cooldown_bars","max_signal_bars_after_second","stall_max_gap_bars","max_entry_leg_ratio",
        "min_effective_bars_between","retrace_min","retrace_max","leg_body_ratio_min","max_leg_pct",
        "min_rr","tp_rr_long","tp_rr_short","tp_lookback","vp_bins")
params = {k: cfg.get(f"macro_pullback.{k}") for k in KEYS}
params = {k: v for k, v in params.items() if v is not None}
params["account_equity"] = cfg.get("risk.account_equity", 1000); params["risk_pct"] = cfg.get("risk.risk_pct", 0.5)
cooldown = int(params.get("cooldown_bars", 12))
print(f"新参数确认: retrace_max={params.get('retrace_max')} leg_body_ratio_min={params.get('leg_body_ratio_min')} stall_max_gap_bars={params.get('stall_max_gap_bars')}", flush=True)

sigs = []
t0 = time.time()
for tf in ("5m", "15m"):
    params["tf"] = tf
    files = [f for f in glob.glob(os.path.join(CACHE, f"*_{tf}_30d.json")) if not os.path.basename(f).startswith("sig_")]
    done = 0
    for f in files:
        sym = os.path.basename(f)[:-len(f"_{tf}_30d.json")]
        try: kl = json.load(open(f))
        except Exception: continue
        if len(kl) < 60: continue
        idxs = [i for i, k in enumerate(kl) if DAY_START <= int(k["open_time"]) < DAY_END]
        last = {"long": -999, "short": -999}
        for i in idxs:
            if i < 50: continue
            win = kl[max(0, i - W):i + 1]
            for d in ("long", "short"):
                if i - last[d] < cooldown: continue
                try: s = detect_macro_pullback(sym, d, win, win, params)
                except Exception: s = None
                if s is not None:
                    last[d] = i
                    ex = s.extra if isinstance(s.extra, dict) else {}
                    sigs.append({"symbol": sym, "tf": tf, "direction": d,
                                 "entry_time": int(kl[i]["open_time"]), "entry": s.entry, "sl": s.sl, "tp": s.tp,
                                 "vol_ratio": s.vol_ratio, "structure": ex.get("structure"), "markers": ex.get("markers")})
        done += 1
        if done % 150 == 0:
            print(f"  {tf} {done}/{len(files)} ({round(time.time()-t0)}s) 累计{len(sigs)}条", flush=True)

from collections import Counter
print(f"\n=== 新策略 昨天(07-28 UTC)触发: {len(sigs)} 条 (用时{round(time.time()-t0)}s) ===")
print("  按级别:", dict(Counter(s["tf"] for s in sigs)))
print("  按方向:", dict(Counter(s["direction"] for s in sigs)))
print("  按级别+方向:", dict(Counter(f"{s['tf']}-{s['direction']}" for s in sigs)))
json.dump(sigs, open(os.path.join(OUT, "replay_0728.json"), "w"))
print(f"→ 明细已存 replay_0728.json ({len(sigs)}条)")
