"""一致性验证：对线上同一时间窗（最近N小时）做回放，对比 live 信号数。
如果回放=0且live=0 → 路径一致，仅行情清淡；回放>0而live=0 → live路径有bug。
"""
import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import get_config
from app.db import DB
from app.engine.binance_rest import BinanceRest
from app.engine.signals import SignalEngine

HOURS = 6
COLS = ("open_time", "open", "high", "low", "close", "volume", "quote_volume", "taker_buy", "closed")


async def main():
    cfg = get_config()
    cfg.set_override("factors.min_score", 0)   # 与线上当前设置一致
    db = DB(cfg.db_path)
    rest = BinanceRest(cfg.get("binance.rest_base"))
    engine = SignalEngine(cfg, db)

    symbols = db.enabled_symbols()
    cutoff_ms = (int(time.time()) - HOURS * 3600) * 1000
    found = []
    for sym in symbols:
        try:
            k15 = [dict(zip(COLS, r)) for r in await rest.klines(sym, "15m", limit=500) if r[8] == 1]
            k5 = [dict(zip(COLS, r)) for r in await rest.klines(sym, "5m", limit=500) if r[8] == 1]
        except Exception as e:
            print(f"{sym} fetch fail: {e}")
            continue
        for tf, ks in (("15m", k15), ("5m", k5)):
            for i in range(60, len(ks)):
                if ks[i]["open_time"] < cutoff_ms:
                    continue
                window = ks[: i + 1]
                cut = window[-1]["open_time"]
                k15_cut = [x for x in k15 if x["open_time"] <= cut]
                sig = engine.evaluate(sym, tf, window, k15_cut)
                if sig:
                    found.append(sig)
                    print(f"  REPLAY-SIGNAL {sym} {tf} {sig.direction} rr={sig.rr} score={sig.extra.get('factor_score')}")
    print(f"\n窗口={HOURS}h, 回放信号数={len(found)} (live同窗口=0)")
    print("CONSISTENT (行情清淡)" if not found else "MISMATCH! live路径需排查")
    await rest.close()


asyncio.run(main())
