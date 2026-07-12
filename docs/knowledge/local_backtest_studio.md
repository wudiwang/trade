# Local Backtest Studio

## Purpose

The local backtest system is the research lab. It should let the user quickly test strategy ideas, inspect historical hit charts, save classic patterns, and decide whether a strategy has practical value.

## Current Commands

Start local viewer:

```powershell
.\.venv\Scripts\python.exe scripts\bt_viewer.py --days 30 --host 127.0.0.1 --port 8530
```

Open:

```text
http://127.0.0.1:8530
```

Refresh local Binance futures kline cache:

```powershell
.\.venv\Scripts\python.exe scripts\bt_refresh.py --tfs 5m,15m,1h --days 30 --top 0
```

Precompute all strategy signals:

```powershell
.\.venv\Scripts\python.exe scripts\bt_scan.py --days 30
```

## Phone Access (2026-07-12)

Viewer works on phones: below 820px the signal list becomes a pull-down drawer (auto-closes
after picking a signal) and the strategy-logic panel collapses, so the chart owns ~75-85% of
the screen. On LAN: `http://<PC-LAN-IP>:8530`.

Fixed public URL — **https://srv1587764.hstgr.cloud** via `.\scripts\bt_remote.ps1`:

```powershell
$env:BT_USER="peter"; $env:BT_PASS="<password>"
.\scripts\bt_remote.ps1
```

```text
phone (any network) -> VPS Caddy (auto-TLS) -> 127.0.0.1:8531 -> SSH reverse tunnel -> local 127.0.0.1:8530
```

Why this shape, so nobody "simplifies" it later:

- The viewer holds all klines + 2.07M signals in RAM: **6.5GB RSS**. The VPS has 3.8GB total /
  2.2GB available and is running the **live trading service**. Hosting the viewer there as-is
  would OOM-kill the trader. So the VPS is only a relay — no data moves.
- The tunnel binds the VPS **loopback** (8531), so it is unreachable from the internet and only
  Caddy can proxy to it. No `sshd` change (`GatewayPorts` stays off), no firewall change.
- `overall.it.com` DNS is at Namecheap, **not Cloudflare** (so `cloudflared tunnel route dns`
  cannot work), and `trade.overall.it.com` does not resolve at all — the Caddyfile block for it
  has never had a cert. `srv1587764.hstgr.cloud` is the VPS's built-in Hostinger hostname:
  already resolving, already certified, zero DNS work.
- Caddyfile on the VPS gained one block (`srv1587764.hstgr.cloud -> 127.0.0.1:8531`);
  backup at `/etc/caddy/Caddyfile.bak.20260712120047`.

PC off / asleep = phone sees **502**. Unavoidable while the data lives locally; the fix is the
memory-lean rewrite (klines fetched on demand from Binance, signals in SQLite) that would let the
viewer actually run on the VPS.

`bt_viewer.py` gained optional Basic Auth via `BT_USER`/`BT_PASS` (unset = no auth, unchanged
local behaviour). `bt_remote.ps1` refuses to start without them — the viewer exposes a write
endpoint (`POST /api/label`).

## `/api/signals` is paginated — do not un-paginate it

30d x ~2.2k symbols x 18 strategies = **2.07M signals (~549MB of JSON)**. The endpoint used to
serialize all of it on every page load, which killed the browser (`Unexpected end of JSON input`)
while the frontend only ever rendered the first 1500 rows. It now filters server-side and returns
the newest `limit` rows plus a total count:

```text
GET /api/signals?strat=&dir=&result=&limit=800   ->  {"total": 2070427, "rows": [...]}   # ~213KB
```

Any new consumer must pass filters and a limit rather than pulling the whole set.

Run the current daily batch:

```powershell
cmd /c scripts\daily_job.cmd
```

## Current Data Files

Local cache is stored in:

```text
.btcache/
```

Examples:

```text
BTCUSDT_5m_30d.json
BTCUSDT_15m_30d.json
BTCUSDT_1h_30d.json
sig_reversal_30d.json
sig_smallbig_30d.json
sig_macro_pullback_30d.json
refresh.log
```

Kline files are source data. `sig_*` files are derived signal outputs and can be regenerated.

## Required Phase 1 Upgrades

1. Strategy detail page:
   - strategy introduction,
   - original idea,
   - classic charts,
   - current logic,
   - current parameters,
   - changelog,
   - code file references,
   - metrics.

2. Chart review:
   - switch timeframe at least between `5m`, `15m`, and `1h`,
   - preserve entry/SL/TP markers,
   - show strategy-specific markers such as L1/H1, L2/H2, volume sweep, stall K, entry K,
   - show MFE and MAE after entry.

3. Save classic case:
   - save current chart context,
   - include symbol, timeframe, strategy, signal id, timestamp, entry, SL, TP, markers,
   - allow labels: good, bad, borderline, invalid, classic,
   - save to `pattern_cases/<strategy>/...`,
   - optionally save screenshot into `artifacts/pattern_cases/...`.

4. Metrics:
   - sample count,
   - win rate,
   - expected R,
   - average R,
   - total R,
   - profit factor,
   - max drawdown,
   - max consecutive losses,
   - buy-point upward probability after 5/10/20 bars,
   - MFE and MAE distribution,
   - long/short split,
   - timeframe split.

5. Strategy composer:
   - union,
   - intersection,
   - same-symbol time window,
   - same-direction requirement,
   - filter strategy plus trigger strategy.

## Refresh Plan

Conservative plan to avoid Binance rate limits:

- Top 200 symbols: refresh every 30 minutes.
- Full market: refresh twice daily, for example 08:30 and 20:30.
- Full strategy recomputation: once daily.
- Frequently used strategy recomputation: every 30-60 minutes or manual trigger.
- Manual refresh button in local UI should display estimated runtime and latest data timestamp.

