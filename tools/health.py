"""一键健康报告：python tools/health.py（VPS上跑 /opt/trade/venv/bin/python tools/health.py）"""
import json
import sqlite3
import time
from pathlib import Path

db = sqlite3.connect(Path(__file__).resolve().parent.parent / "data" / "trade.db")
now = int(time.time())

total = db.execute("SELECT COUNT(*) FROM signals").fetchone()[0]
last24 = db.execute("SELECT COUNT(*) FROM signals WHERE created_at>?", (now - 86400,)).fetchone()[0]
print(f"signals: total={total} last24h={last24}")
for r in db.execute("SELECT id,symbol,tf,direction,kind,rr,status,extra,reason FROM signals ORDER BY id DESC LIMIT 6"):
    e = json.loads(r[7] or "{}")
    print(f"  #{r[0]} {r[1]} {r[2]} {r[3]} type={e.get('type')} score={e.get('score')} rr={r[5]} {r[6]}")
    if r[8]:
        print(f"      {r[8][:90]}")

print("paper:", dict(db.execute("SELECT result, COUNT(*) FROM paper_trades GROUP BY result").fetchall()))
kl = db.execute("SELECT COUNT(*), MAX(open_time) FROM klines").fetchone()
print(f"klines: rows={kl[0]} latest_age={(now - (kl[1] or 0)//1000)}s")
ev = db.execute("SELECT ts, level, source, message FROM event_log WHERE level!='info' ORDER BY id DESC LIMIT 3").fetchall()
print("warn/err events:", ev if ev else "none")
