"""审计：回放最近N小时窗口，对比线上同期信号产出（排查live路径与回放差异）。"""
import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import get_config
from app.db import DB
from app.engine.binance_rest import BinanceRest
from app.engine.signals import SignalEngine

WINDOW_H = 6
N_SYMBOLS = 55

COLS = ("open_time", "open", "high", "low", "close", "volume", "quote_volume", "taker_buy", "closed")


async def main():
    cfg = get_config()
    cfg.set_override("factors.min_score", 0)  # 与线上一致
    db = DB(cfg.db_path)
    engine = SignalEngine(cfg, db)
    rest = BinanceRest(cfg.get("binance.rest_base"))

    symbols = db.enabled_symbols()[:N_SYMBOLS]
    cutoff_ms = (int(time.time()) - WINDOW_H * 3600) * 1000
    found = []

    for sym in symbols:
        try:
            k15r = await rest.klines(sym, "15m", limit=500)
            k5r = await rest.klines(sym, "5m", limit=500)
        except Exception as e:
            print(f"{sym} fetch fail: {e}")
            continue
        k15 = [dict(zip(COLS, r)) for r in k15r if r[8] == 1]
        k5 = [dict(zip(COLS, r)) for r in k5r if r[8] == 1]
        for tf, ks in (("15m", k15), ("5m", k5)):
            for i in range(60, len(ks)):
                if ks[i]["open_time"] < cutoff_ms:
                    continue  # 只评估最近窗口内收盘的K
                cut = ks[i]["open_time"]
                k15_cut = [x for x in k15 if x["open_time"] <= cut]
                try:
                    sig = engine.evaluate(sym, tf, ks[: i + 1], k15_cut)
                except Exception as e:
                    print(f"ERROR {sym} {tf}@{i}: {e}")
                    continue
                if sig:
                    ts = time.strftime("%H:%M", time.gmtime(cut / 1000 + 8 * 3600))
                    found.append((sym, tf, sig.direction, sig.rr, sig.extra.get("factor_score"), ts))

    print(f"\n最近{WINDOW_H}小时回放({len(symbols)}币种):")
    for f in found:
        print(" ", f)
    print(f"共 {len(found)} 个信号（线上同期: 0）")
    await rest.close()


asyncio.run(main())
