# Wyckoff-Gated Chan Second Entry Design

## Goal

Replace the loose macro pullback paper strategy with a stricter strategy:

1. The user manually sets BTC macro direction.
2. The symbol first produces a high-volume Wyckoff Spring or UTAD first-entry signal.
3. Only after that first-entry signal, the strategy waits for a Chan-style second buy or second sell.
4. The confirmed second entry opens a paper position.

## Scope

- Execution mode: paper only.
- Trigger timeframes: `5m` and `15m`.
- Each timeframe runs independently:
  - `5m` Spring or UTAD leads to a `5m` second entry.
  - `15m` Spring or UTAD leads to a `15m` second entry.
- Universe for live monitoring: all USDT perpetual contracts already supported by the project.
- Backtest universe: a balanced sample of high, medium, and low 24h quote-volume contracts to avoid Binance rate limits.

## Manual BTC Direction Gate

- `macro_view_direction = long`: only seek Spring first-buy setups and later second-buy entries.
- `macro_view_direction = short`: only seek UTAD first-sell setups and later second-sell entries.
- `macro_view_direction = neutral`: strategy produces no signal.

## Long Setup

1. BTC manual direction must be `long`.
2. A 5m or 15m candle sweeps below a prior low.
3. The sweep candle volume must be at least `vol_mult` times the previous `vol_ma` average volume.
4. Within `reclaim_bars`, price must reclaim the start area of the previous down leg.
5. The Spring area must form or align with a confirmed bottom fractal, treated as first buy `L1`.
6. After `L1`, price must rally enough to form an upward leg.
7. Price then pulls back and confirms a second bottom fractal `L2`.
8. `L2` must not break `L1`, allowing a small tolerance.
9. When `L2` confirms, open a paper long.
10. Stop loss sits below `L2` with a configurable buffer.
11. Take profit uses the nearest upper volume-profile HVN or a risk/reward fallback.

## Short Setup

1. BTC manual direction must be `short`.
2. A 5m or 15m candle sweeps above a prior high.
3. The sweep candle volume must be at least `vol_mult` times the previous `vol_ma` average volume.
4. Within `reclaim_bars`, price must fall back to the start area of the previous up leg.
5. The UTAD area must form or align with a confirmed top fractal, treated as first sell `H1`.
6. After `H1`, price must decline enough to form a downward leg.
7. Price then rebounds and confirms a second top fractal `H2`.
8. `H2` must not break `H1`, allowing a small tolerance.
9. When `H2` confirms, open a paper short.
10. Stop loss sits above `H2` with a configurable buffer.
11. Take profit uses the nearest lower volume-profile HVN or a risk/reward fallback.

## Initial Parameters

- `macro_pullback.enabled`: `true`.
- `macro_pullback.exclusive`: `true`.
- `macro_pullback.timeframes`: `["5m", "15m"]`.
- `macro_pullback.vol_ma`: `20`.
- `macro_pullback.vol_mult`: `3.0`.
- `macro_pullback.lookback`: `20`.
- `macro_pullback.reclaim_bars`: `4`.
- `macro_pullback.reclaim_tolerance_pct`: `0.5`.
- `macro_pullback.min_leg_pct`: `0.8`.
- `macro_pullback.second_tolerance_pct`: `0.2`.
- `macro_pullback.stop_buffer_pct`: `0.3`.
- `macro_pullback.cooldown_bars`: `12`.
- `macro_pullback.min_rr`: `1.5`.
- `macro_pullback.tp_lookback`: `100`.
- `macro_pullback.vp_bins`: `50`.

## Backtest Sampling

The 7-day backtest should avoid Binance rate limits by sampling a balanced set of symbols:

- Fetch all USDT perpetual symbols and 24h quote volume.
- Sort symbols by quote volume.
- Split into high, medium, and low thirds.
- Evenly sample up to 30 symbols from each third.
- Total target: about 90 symbols.

Report:

- total signals,
- closed trades,
- win rate,
- total R,
- average R,
- open trades,
- results by timeframe,
- results by direction,
- results by type,
- symbols that produced signals.

## Data Reset

Before evaluating this strategy as the new paper data source, clear:

- `signals`
- `paper_trades`
- `equity_curve`

Do not clear:

- `klines`
- `symbols`
- `settings`
- `users`
- `watchlist`
- `playbooks`
- `orders`

## Deployment

After local tests and sampled 7-day backtest, deploy to the existing VPS and restart the service in paper mode. Confirm the strategy remains exclusive and the paper tables have been reset.
