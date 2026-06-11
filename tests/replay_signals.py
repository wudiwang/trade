"""C6 回放自测：拉真实历史K线，逐根喂给 SignalEngine，统计信号产出。
用途：1) 验证引擎在真实数据上无异常 2) 看 RR>=5 vs RR>=2.5 的触发频率。
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

N_SYMBOLS = 30
BARS = 500


async def main():
    cfg = get_config()
    db = DB(cfg.db_path)
    rest = BinanceRest(cfg.get("binance.rest_base"))
    engine = SignalEngine(cfg, db)

    symbols = db.enabled_symbols()[:N_SYMBOLS]
    if not symbols:
        print("先跑 tests/smoke_data_layer.py 生成币种表")
        return

    t0 = time.time()
    stats = {"primary": 0, "secondary": 0, "long": 0, "short": 0}
    samples = []
    errors = 0

    for sym in symbols:
        try:
            k15 = await rest.klines(sym, "15m", limit=BARS)
            k5 = await rest.klines(sym, "5m", limit=BARS)
        except Exception as e:
            print(f"{sym} 拉取失败: {e}")
            continue
        COLS = ("open_time", "open", "high", "low", "close", "volume", "quote_volume", "taker_buy", "closed")
        k15 = [dict(zip(COLS, r)) for r in k15 if r[8] == 1]
        k5 = [dict(zip(COLS, r)) for r in k5 if r[8] == 1]

        for tf, ks in (("15m", k15), ("5m", k5)):
            for i in range(60, len(ks)):
                window = ks[: i + 1]
                cut = window[-1]["open_time"]
                k15_cut = [x for x in k15 if x["open_time"] <= cut]
                try:
                    sig = engine.evaluate(sym, tf, window, k15_cut)
                except Exception as e:
                    errors += 1
                    print(f"ERROR {sym} {tf} @{i}: {type(e).__name__}: {e}")
                    continue
                if sig:
                    stats[sig.kind] += 1
                    stats[sig.direction] += 1
                    if len(samples) < 10:
                        samples.append(sig)

    dt = time.time() - t0
    days = BARS * 15 / 60 / 24  # 15m 数据覆盖天数
    print(f"\n回放完成: {len(symbols)}币种 x 2级别 x {BARS}根, 耗时{dt:.0f}s, 异常{errors}个")
    print(f"主信号(RR>=5): {stats['primary']}   次级(RR>=2.5): {stats['secondary']}")
    print(f"做多: {stats['long']}   做空: {stats['short']}")
    print(f"(15m数据约覆盖{days:.1f}天)")
    for s in samples:
        print(f"  {s.symbol} {s.tf} {s.direction} {s.kind} rr={s.rr} vol={s.vol_ratio}x "
              f"score={s.extra.get('factor_score')} {s.extra.get('factors')}")
    print("REPLAY OK" if errors == 0 else f"REPLAY HAD {errors} ERRORS")
    await rest.close()


asyncio.run(main())
