"""阶段A冒烟测试：配置→REST→DB 全链路。"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import get_config
from app.db import DB
from app.engine.binance_rest import BinanceRest


async def main():
    cfg = get_config()
    db = DB(cfg.db_path)
    rest = BinanceRest(cfg.get("binance.rest_base"))

    symbols = await rest.usdt_perp_symbols()
    print(f"USDT永续合约总数: {len(symbols)}")

    vols = await rest.ticker_24h()
    min_vol = cfg.get("universe.min_quote_volume_24h")
    rows = []
    for s in symbols:
        qv = vols.get(s["symbol"], 0.0)
        rows.append({**s, "quote_volume_24h": qv, "enabled": 1 if qv >= min_vol else 0})
    db.upsert_symbols(rows)
    enabled = db.enabled_symbols()
    print(f"24h成交额>= {min_vol/1e6:.0f}M 的监控币种: {len(enabled)}")
    print(f"样例: {enabled[:8]}")

    # 回填两个币种试水
    for sym in ["BTCUSDT", "ETHUSDT"]:
        for tf in cfg.timeframes:
            ks = await rest.klines(sym, tf, limit=200)
            db.upsert_klines(sym, tf, ks)
            got = db.get_klines(sym, tf, limit=200)
            last = got[-1]
            print(f"{sym} {tf}: 入库{len(got)}根, 最新 open_time={last['open_time']} close={last['close']} closed={last['closed']}")

    await rest.close()
    print("SMOKE OK")


asyncio.run(main())
