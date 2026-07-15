"""把 VPS 上【线上实盘系统真实打出的信号】拉到本地，供看图器/灵感页展示。

这不是本地回测扫出来的模拟信号 —— 是 /opt/trade 上那套 live 系统真实推送过的单子
(signals 表) + 它的 paper 结算结果 (paper_trades 表)。

用法:
    python scripts/live_sync.py --n 50
    → 写入 .btcache/live_signals.json

看图器的 /api/live 读这个文件; /api/live/refresh 在后台重跑本脚本。
"""
import argparse
import json
import os
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, ".btcache", "live_signals.json")

VPS = "root@76.13.182.175"
APP = "/opt/trade"

# 在 VPS 上跑这段, 把最近 N 条信号(含 paper 结算)吐成 JSON。
REMOTE = r'''
import json, sqlite3, sys, time
n = int(sys.argv[1]) if len(sys.argv) > 1 else 50
days = int(sys.argv[2]) if len(sys.argv) > 2 else 0     # >0: 按最近N天拉全量(忽略 n 上限)
c = sqlite3.connect("data/trade.db"); c.row_factory = sqlite3.Row
cols = """s.id, s.created_at, s.symbol, s.tf, s.direction, s.kind,
         s.entry, s.sl, s.tp, s.rr, s.vol_ratio, s.status, s.state, s.reason,
         p.track, p.result, p.pnl_r, p.exit_price, p.closed_at"""
if days > 0:
    cut = int(time.time()) - days * 86400
    rows = c.execute(f"""SELECT {cols} FROM signals s
      LEFT JOIN paper_trades p ON p.signal_id = s.id
      WHERE s.created_at >= ? ORDER BY s.created_at DESC""", (cut,)).fetchall()
else:
    rows = c.execute(f"""SELECT {cols} FROM signals s
      LEFT JOIN paper_trades p ON p.signal_id = s.id
      ORDER BY s.created_at DESC LIMIT ?""", (n,)).fetchall()
tot = c.execute("SELECT COUNT(*) FROM signals").fetchone()[0]
closed = c.execute("SELECT COUNT(*), SUM(pnl_r) FROM paper_trades WHERE result IN ('tp','sl')").fetchone()
wins = c.execute("SELECT COUNT(*) FROM paper_trades WHERE result='tp'").fetchone()[0]
print(json.dumps({
  "rows": [dict(r) for r in rows],
  "total": tot,
  "n_closed": closed[0] or 0,
  "sum_r": closed[1] or 0.0,
  "wins": wins,
}, ensure_ascii=False))
'''


def pull(n=50, days=0, timeout=60):
    """SSH 到 VPS 取真实信号 → 写本地 JSON。days>0 时按最近N天拉全量, 否则拉最近 n 条。"""
    try:
        p = subprocess.run(
            ["ssh", "-o", "ConnectTimeout=10", "-o", "BatchMode=yes", VPS,
             f"cd {APP} && python3 - {n} {days}"],
            input=REMOTE, capture_output=True, text=True, timeout=timeout, encoding="utf-8")
    except subprocess.TimeoutExpired:
        return False, "SSH 超时 —— VPS 连不上?"
    if p.returncode != 0:
        return False, f"SSH 失败: {(p.stderr or '').strip()[:200]}"
    try:
        d = json.loads((p.stdout or "").strip().splitlines()[-1])
    except Exception as e:
        return False, f"解析失败: {e}"

    d["synced_at"] = int(time.time())
    n_closed = d.get("n_closed") or 0
    d["exp_r"] = round((d.get("sum_r") or 0.0) / n_closed, 3) if n_closed else None
    d["win_rate"] = round(d["wins"] / n_closed * 100, 1) if n_closed else None
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    json.dump(d, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    return True, f"已同步 {len(d['rows'])} 条(线上共 {d['total']} 条)"


def load():
    if not os.path.exists(OUT):
        return None
    try:
        return json.load(open(OUT, encoding="utf-8"))
    except Exception:
        return None


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=50)
    ap.add_argument("--days", type=int, default=0, help=">0: 拉最近N天全部触发")
    a = ap.parse_args()
    ok, msg = pull(a.n, a.days)
    print(("[live_sync] " + msg))
    sys.exit(0 if ok else 1)
