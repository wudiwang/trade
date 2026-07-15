# Local Data Cache

## Cache Directory

```text
.btcache/
```

This directory stores local market data and generated backtest signal files. It is intentionally ignored by Git because it can be large and frequently updated.

## File Types

```text
<SYMBOL>_5m_30d.json
<SYMBOL>_15m_30d.json
<SYMBOL>_1h_30d.json
```

These are Binance futures kline caches for one symbol and timeframe.

```text
sig_<strategy>_30d.json
```

These are generated strategy signal files from `scripts/bt_scan.py`. They can be regenerated from kline cache.

```text
refresh.log
```

Refresh and scan log.

## Current Refresh Script

```powershell
.\.venv\Scripts\python.exe scripts\bt_refresh.py --tfs 5m,15m,1h --days 30 --top 0
```

## Rate-Limit Plan

Avoid aggressive full-market refreshes.

Recommended:

- top 200 symbols every 30 minutes,
- full market twice daily,
- full strategy scan once daily,
- common strategy scan every 30-60 minutes,
- manual refresh button for urgent research.

## Runtime Estimate

Observed full-market refresh for roughly 627 symbols across `5m/15m/1h` with concurrency 3 took about 11 minutes. Full strategy recomputation took about another 11 minutes, with `pullback` being the slowest.

