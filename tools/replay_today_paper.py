"""Replay today's macro pullback signals and write them into paper trades."""
import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import get_config
from app.db import DB
from app.engine.backtest import fetch_series, walk_symbol_mtf
from app.engine.binance_rest import BinanceRest
from app.engine.paper import PaperBroker
from app.engine.signals import TF_MS


async def main() -> None:
    cfg = get_config()
    db = DB(cfg.db_path)
    settings = db.get_settings()
    macro_direction = settings.get("macro_view_direction", "neutral")
    if macro_direction not in ("long", "short"):
        raise SystemExit("macro_view_direction must be long or short before replaying paper trades")

    rest = BinanceRest(cfg.get("binance.rest_base"))
    syms = await rest.usdt_perp_symbols()
    exclude = set(cfg.get("universe.exclude", []) or [])
    symbols = sorted(s["symbol"] for s in syms if s["symbol"] not in exclude)
    tfs = cfg.get("macro_pullback.timeframes", ["5m", "15m"]) or ["5m", "15m"]
    fetch_tfs = sorted(set(tfs + ["5m", "15m"]))
    paper = PaperBroker(cfg, db)

    start_s = int(time.time()) - 24 * 60 * 60
    inserted = 0
    seen = 0
    try:
        for n, symbol in enumerate(symbols, 1):
            series_by_tf = {}
            for tf in fetch_tfs:
                series_by_tf[tf] = await fetch_series(rest, symbol, tf, 1)
            btc_lookup = lambda _t: 1 if macro_direction == "long" else -1
            sigs = walk_symbol_mtf(cfg, symbol, series_by_tf, btc_lookup, fetch_tfs)
            for sig in sigs:
                if sig["created_at"] < start_s:
                    continue
                seen += 1
                extra = sig.get("extra") or {}
                struct = extra.get("structure") if isinstance(extra, dict) else {}
                anchor = (struct or {}).get("L2_time") or (struct or {}).get("H2_time")
                if anchor and not db.claim_signal_anchor("macro_pullback", sig["symbol"], sig["tf"], sig["direction"], int(anchor)):
                    continue
                sid = db.insert_signal(sig)
                from app.engine.signals import Signal
                s = Signal(**{k: sig[k] for k in (
                    "symbol", "tf", "direction", "kind", "entry", "sl", "tp", "rr", "vol_ratio",
                    "strength", "suggested_qty", "risk_usdt", "reason", "created_at", "extra",
                )})
                paper.open_from_signal(sid, s)
                inserted += 1
            if n % 25 == 0:
                print(f"{n}/{len(symbols)} symbols, seen={seen}, inserted={inserted}")
    finally:
        await rest.close()

    print(json.dumps({
        "macro_direction": macro_direction,
        "symbols": len(symbols),
        "timeframes": tfs,
        "seen_today": seen,
        "inserted_paper_trades": inserted,
    }, ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(main())
