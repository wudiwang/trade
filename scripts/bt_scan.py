"""把策略信号扫描与看图器解耦(用户 2026-06-17:看图器不该在启动时同步扫全市场)。

跑各策略 scan → 每策略写一份 .btcache/sig_<strat>_<days>d.json。
看图器只读这些 JSON(秒级启动)。可单跑某策略,或全部;适合放夜间计划任务。

用法:
  .venv/Scripts/python scripts/bt_scan.py --days 30                 # 全部策略
  .venv/Scripts/python scripts/bt_scan.py --days 30 --strats smallbig,deepbase
"""
import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass
import bt_registry as R

CACHE = R.CACHE
FIELDS = ("strat", "symbol", "direction", "stage", "created_at", "entry", "sl", "tp",
          "result", "pnl_r", "climaxX", "movePct", "anchor", "extra", "vol_ratio")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--strats", default="")
    a = ap.parse_args()
    os.makedirs(CACHE, exist_ok=True)
    names = [x for x in a.strats.split(",") if x] or list(R.SCANS)
    C = R.cache_loader(a.days)
    for n in names:
        t = time.time()
        try:
            rows = R.SCANS[n](C)
        except Exception as e:
            print(f"{n}: ERROR {e}", flush=True)
            continue
        out = []
        for s in rows:
            s.setdefault("strat", n)
            out.append({k: s.get(k) for k in FIELDS})
        path = os.path.join(CACHE, f"sig_{n}_{a.days}d.json")
        json.dump(out, open(path, "w"))
        print(f"{n}: {len(out)} 信号, {round(time.time()-t,1)}s → {os.path.basename(path)}", flush=True)


if __name__ == "__main__":
    main()
