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
    tfs = (sys.argv[2].split(",") if len(sys.argv) > 2 else ["5m", "15m", "1h", "4h"])
    btc_filter = len(sys.argv) > 3 and sys.argv[3] == "btc_on"
    cfg = get_config()
    cfg.set_override("spring.btc_filter", btc_filter)
    rest = BinanceRest(cfg.get("binance.rest_base"))
    if len(sys.argv) > 4:
        symbols = [s.upper() for s in sys.argv[4].split(",") if s.strip()]
    else:
        syms = await rest.usdt_perp_symbols()
        exclude = set(cfg.get("universe.exclude", []) or [])
        symbols = sorted(s["symbol"] for s in syms if s["symbol"] not in exclude)
    res = await run_backtest(cfg, rest, symbols, tfs, days,
                             progress=lambda d, t, m: print(f"  {d}/{t} {m}"))
    await rest.close()
    print(json.dumps({k: res[k] for k in ("total", "by_tf", "by_type", "by_direction", "elapsed_s")},
                     ensure_ascii=False, indent=1))
    for s in res["signals"][-8:]:
        print(f"  {s['symbol']} {s['tf']} {s['direction']} {s['type']} score={s['score']} "
              f"entry={s['entry']} {s['result']} pnl_r={s['pnl_r']}")


asyncio.run(main())
