"""回测CLI自测: python tests/backtest_cli.py [days] [symbols...] """
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import get_config
from app.engine.binance_rest import BinanceRest
from app.engine.backtest import run_backtest
import app.engine.macro_pullback as macro_pullback


def balanced_sample(symbols: list[str], volumes: dict[str, float], target: int) -> list[str]:
    ranked = sorted(symbols, key=lambda s: volumes.get(s, 0.0), reverse=True)
    if target <= 0 or target >= len(ranked):
        return ranked
    thirds = [ranked[:len(ranked) // 3], ranked[len(ranked) // 3: 2 * len(ranked) // 3], ranked[2 * len(ranked) // 3:]]
    per = max(1, target // 3)
    out: list[str] = []
    for bucket in thirds:
        if not bucket:
            continue
        if len(bucket) <= per:
            out.extend(bucket)
            continue
        step = (len(bucket) - 1) / max(per - 1, 1)
        picks = [bucket[round(i * step)] for i in range(per)]
        out.extend(picks)
    return sorted(dict.fromkeys(out))


async def main():
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 7
    tfs = (sys.argv[2].split(",") if len(sys.argv) > 2 else ["5m", "15m", "1h", "4h"])
    btc_filter = len(sys.argv) > 3 and sys.argv[3] == "btc_on"
    cfg = get_config()
    cfg.set_override("spring.btc_filter", btc_filter)
    fixed_rr = None
    if len(sys.argv) > 5 and sys.argv[5].startswith("rr:"):
        fixed_rr = float(sys.argv[5].split(":", 1)[1])
        def fixed_tp_for(_klines, direction, entry, sl, _params):
            risk = abs(entry - sl)
            if direction == "long":
                return entry + fixed_rr * risk, fixed_rr
            return entry - fixed_rr * risk, fixed_rr
        macro_pullback._tp_for = fixed_tp_for
        print(f"fixed_rr={fixed_rr}")
    rest = BinanceRest(cfg.get("binance.rest_base"))
    syms = await rest.usdt_perp_symbols()
    exclude = set(cfg.get("universe.exclude", []) or [])
    all_symbols = sorted(s["symbol"] for s in syms if s["symbol"] not in exclude)
    if len(sys.argv) > 4 and sys.argv[4].startswith("sample:"):
        target = int(sys.argv[4].split(":", 1)[1])
        volumes = await rest.ticker_24h()
        symbols = balanced_sample(all_symbols, volumes, target)
        print(f"sampled {len(symbols)} symbols from {len(all_symbols)} contracts")
    elif len(sys.argv) > 4:
        symbols = [s.upper() for s in sys.argv[4].split(",") if s.strip()]
    else:
        symbols = all_symbols
    res = await run_backtest(cfg, rest, symbols, tfs, days,
                             progress=lambda d, t, m: print(f"  {d}/{t} {m}"))
    await rest.close()
    print(json.dumps({k: res[k] for k in ("total", "by_tf", "by_type", "by_direction", "elapsed_s")},
                     ensure_ascii=False, indent=1))
    for s in res["signals"][-8:]:
        print(f"  {s['symbol']} {s['tf']} {s['direction']} {s['type']} score={s['score']} "
              f"entry={s['entry']} {s['result']} pnl_r={s['pnl_r']}")


if __name__ == "__main__":
    asyncio.run(main())
