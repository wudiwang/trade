"""线上策略回归(用户 2026-07-12): 把线上 macro_pullback 最近那波"翻红"的盈利单原样导出来看形态。

背景: 07-06~07-10 线上纸面从 -60R 拉回 -24R, 全靠这批单。它们不是新策略, 就是线上策略自己打出来的单,
所以叫"回归"——回头看这批盈利单到底长什么样, 有没有可复制的形态。

数据源: VPS /opt/trade/data/trade.db (只读) 的 paper_trades + signals(含 extra 结构标注)。
输出: .btcache/sig_online_regress_30d.json → 本地看图器(bt_viewer)直接可看, 手机同局域网也能开。

用法:
  .venv/Scripts/python scripts/online_regress.py                # 从VPS拉最新
  .venv/Scripts/python scripts/online_regress.py --dump x.json  # 用已拉好的dump
"""
import argparse
import json
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass
import bt_registry as R

VPS = "root@76.13.182.175"
KEY = os.path.expanduser("~/.ssh/trade_vps")
WIN_FROM, WIN_TO = "2026-07-06", "2026-07-11"      # 那波翻红的窗口
STRAT = "online_regress"

REMOTE_PY = """
import sqlite3, json
c = sqlite3.connect('file:data/trade.db?mode=ro', uri=True); c.row_factory = sqlite3.Row
out = {t: [dict(r) for r in c.execute('select * from ' + t)] for t in ('paper_trades', 'signals')}
print(json.dumps(out))
"""


def pull_vps():
    cmd = ["ssh", "-i", KEY, "-o", "StrictHostKeyChecking=no", VPS,
           f"cd /opt/trade && python3 -c {json.dumps(REMOTE_PY)}"]
    return json.loads(subprocess.run(cmd, capture_output=True, text=True, check=True).stdout)


def build(dump, win_from=WIN_FROM, win_to=WIN_TO, only_wins=True):
    sg = {r["id"]: r for r in dump["signals"]}
    lo = time.mktime(time.strptime(win_from, "%Y-%m-%d"))
    hi = time.mktime(time.strptime(win_to, "%Y-%m-%d"))
    out = []
    for t in dump["paper_trades"]:
        if not t.get("closed_at") or not (lo <= t["closed_at"] < hi):
            continue
        if only_wins and t["result"] != "tp":
            continue
        if t["result"] not in ("tp", "sl"):
            continue
        s = sg.get(t["signal_id"]) or {}
        ex = s.get("extra") or "{}"
        ex = json.loads(ex) if isinstance(ex, str) else ex
        st = ex.get("structure") or {}
        risk = abs(t["entry"] - t["sl"]) or 1e-9
        fee_r = 2 * 0.00045 * t["entry"] / risk          # 往返taker手续费(折R)
        out.append({
            "strat": STRAT, "symbol": t["symbol"], "tf": s.get("tf") or t["tf"],
            "direction": t["direction"], "stage": t["track"],
            "created_at": int(t["opened_at"]), "entry_time": int(t["opened_at"]) * 1000,
            "closed_at": int(t["closed_at"]),
            "entry": t["entry"], "sl": t["sl"], "tp": t["tp"],
            "result": t["result"], "pnl_r": t["pnl_r"],
            "net_r": round((t["pnl_r"] or 0) - fee_r, 3), "fee_r": round(fee_r, 3),
            "bars_held": None,
            "vol_ratio": (ex.get("wyckoff") or {}).get("vol_ratio"),
            "climaxX": (ex.get("wyckoff") or {}).get("vol_ratio"),
            "anchor": st.get("H2_time") or st.get("L2_time"),
            "extra": {"path": "online_regress", "wyckoff": ex.get("wyckoff"),
                      "structure": st, "markers": ex.get("markers"),
                      "macro": ex.get("macro"), "reason": s.get("reason")},
        })
    out.sort(key=lambda r: r["created_at"])
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dump", default="")
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--all", action="store_true", help="连亏损单一起导(默认只导盈利单)")
    a = ap.parse_args()
    dump = json.load(open(a.dump)) if a.dump else pull_vps()
    rows = build(dump, only_wins=not a.all)
    p = os.path.join(R.CACHE, f"sig_{STRAT}_{a.days}d.json")
    json.dump(rows, open(p, "w"))
    ns = sum(1 for r in rows if r["direction"] == "short")
    print(f"{STRAT}: {len(rows)} 单 (做空{ns} / 做多{len(rows)-ns})  {WIN_FROM}~{WIN_TO}")
    print(f"  毛R {sum(r['pnl_r'] or 0 for r in rows):+.1f}  扣费后 {sum(r['net_r'] for r in rows):+.1f}"
          f"  (手续费平均吃掉 {sum(r['fee_r'] for r in rows)/max(len(rows),1):.3f}R/单)")
    print(f"  → {p}")


if __name__ == "__main__":
    main()
