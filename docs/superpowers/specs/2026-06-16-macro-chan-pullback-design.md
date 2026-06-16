# Macro Chan Pullback Strategy Design

## Goal

Add a new paper-trading strategy that uses the user's manually confirmed BTC trend as the directional gate, then searches all USDT perpetual symbols for Chan-style second-sell or second-buy pullback entries.

This strategy should open paper positions directly. Historical paper signals and paper positions will be cleared before the new strategy is used as the active data source, so future statistics start from this strategy.

## Operating Scope

- Timeframes: `5m`, `15m`, and `1h`.
- Universe: all available USDT perpetual contract symbols returned by the existing Binance futures universe loader.
- Execution mode: paper trading first. Live trading is not part of this change.
- Direction gate: existing manual macro view.
  - `macro_view_direction = short`: enable short second-sell strategy only.
  - `macro_view_direction = long`: enable long second-buy strategy only.
  - `macro_view_direction = neutral`: disable this strategy.

## Strategy A: Macro-Gated Second Sell

Purpose: when BTC has manually been judged to be at or near a larger resistance/reversal area, find altcoin short entries after a sharp rise fails to continue.

Structure timeframe: `15m`.
Trigger timeframe: `5m`.
Context timeframe: `1h`.

Rules:

1. Manual macro direction must be `short`.
2. The symbol must have a sharp 15m upward impulse before the setup.
3. The impulse creates a first high `H1`.
4. Price must pull back from `H1` to form an intermediate low `L1`.
5. Price then rebounds to a lower high `H2`.
6. `H2` must not break `H1`, allowing a configurable tolerance.
7. The rebound should show weaker volume or weaker momentum than the first impulse.
8. The 5m chart must confirm failure with one of:
   - 5m top fractal confirmation,
   - 5m mini-range breakdown,
   - 5m failed retest of MA20/EMA21 followed by a bearish close.
9. Open a paper short when the 5m trigger confirms.
10. Initial stop is above `H2` with a configurable buffer.
11. Take profit targets use the existing framework: nearby prior low, volume profile dense area, or risk/reward fallback.

## Strategy B: Macro-Gated Second Buy

Purpose: when BTC has manually been judged to be at or near a larger support/reversal area, find altcoin long entries after a sharp selloff fails to continue.

Structure timeframe: `15m`.
Trigger timeframe: `5m`.
Context timeframe: `1h`.

Rules:

1. Manual macro direction must be `long`.
2. The symbol must have a sharp 15m downward impulse before the setup.
3. The impulse creates a first low `L1`.
4. Price must rebound from `L1` to form an intermediate high `H1`.
5. Price then pulls back to a higher low `L2`.
6. `L2` must not break `L1`, allowing a configurable tolerance.
7. The pullback should show weaker volume or weaker momentum than the first selloff.
8. The 5m chart must confirm support with one of:
   - 5m bottom fractal confirmation,
   - 5m mini-range breakout,
   - 5m successful MA20/EMA21 retest followed by a bullish close.
9. Open a paper long when the 5m trigger confirms.
10. Initial stop is below `L2` with a configurable buffer.
11. Take profit targets use the existing framework: nearby prior high, volume profile dense area, or risk/reward fallback.

## Initial Parameters

- `macro_pullback.enabled`: `true`.
- `macro_pullback.structure_tf`: `15m`.
- `macro_pullback.trigger_tf`: `5m`.
- `macro_pullback.context_tf`: `1h`.
- `macro_pullback.impulse_window`: 24 bars.
- `macro_pullback.impulse_min_pct`: 4.0.
- `macro_pullback.ma_period`: 20.
- `macro_pullback.ma_extension_pct`: 1.5.
- `macro_pullback.retest_tolerance_pct`: 0.4.
- `macro_pullback.volume_decay_ratio`: 0.8.
- `macro_pullback.stop_buffer_pct`: 0.3.
- `macro_pullback.cooldown_bars`: 12.
- `macro_pullback.min_rr`: 1.5.

## Data Reset

Before starting this strategy as the new paper data source, clear historical simulated strategy data:

- `signals`
- `paper_trades`
- `equity_curve`
- strategy-related `event_log` rows may be retained unless they pollute UI statistics.

Do not clear:

- `klines`
- `symbols`
- `settings`
- `users`
- `watchlist`
- `playbooks`
- `orders`

## Integration

Add the strategy as an independent path inside `SignalEngine.evaluate_all()` so existing Chan and Wyckoff logic can remain available behind configuration.

Generated signals should:

- use `kind = primary`,
- include `extra.path = "macro_chan_pullback"`,
- include `extra.type = "second_sell"` or `extra.type = "second_buy"`,
- include structure points such as `H1`, `L1`, `H2`, or `L2`,
- open paper positions through the existing `PaperBroker.open_from_signal()` path.

## Backtesting

Backtest in two modes:

1. Deterministic proxy macro mode, where BTC trend is inferred from historical BTC structure for repeatable testing.
2. Manual macro replay mode later, if manual macro annotations are added to the database.

Report:

- total signals,
- closed trades,
- open trades,
- win rate,
- total R,
- average R,
- results by direction,
- results by trigger type,
- results by timeframe.

## Deployment

After local verification, deploy with the existing deployment script and keep the remote service in paper mode. Confirm the online Web console loads, status is healthy, and new paper statistics start from zero.
