"""回测CLI自测: python tests/backtest_cli.py [days] [symbols...] """
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import get_config
from app.engine.binance_rest import BinanceRest
from app.engine.backtest import run_backtest


async def main():
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 7
    symbols = sys.argv[2:] or ["BTCUSDT", "ETHUSDT"]
    cfg = get_config()
    # 与线上当前设置一致的放宽参数
    for k, v in {"spring.vol_mult": 2.5, "spring.quiet_bars": 8, "spring.quiet_mult": 1.8,
                 "spring.range_atr_min": 1.2, "spring.newlow_lookback": 20,
                 "spring.btc_filter": False}.items():
        cfg.set_override(k, v)
    rest = BinanceRest(cfg.get("binance.rest_base"))
    res = await run_backtest(cfg, rest, symbols, ["5m", "15m", "1h", "4h"], days,
                             progress=lambda d, t, m: print(f"  {d}/{t} {m}"))
    await rest.close()
    print(json.dumps({k: res[k] for k in ("total", "by_tf", "by_type", "by_direction", "elapsed_s")},
                     ensure_ascii=False, indent=1))
    for s in res["signals"][-8:]:
        print(f"  {s['symbol']} {s['tf']} {s['direction']} {s['type']} score={s['score']} "
              f"entry={s['entry']} {s['result']} pnl_r={s['pnl_r']}")


asyncio.run(main())
