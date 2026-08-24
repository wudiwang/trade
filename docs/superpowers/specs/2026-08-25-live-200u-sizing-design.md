# Live 200 USDT Notional Sizing Design

Date: 2026-08-25

## Decision

Use 100 USDT fixed margin at 2x leverage for approximately 200 USDT notional on each future
automatic entry. This supersedes the unimplemented 500 USDT notional proposal from 2026-08-23.

## Expected Economics

For a representative signal with a 2% take-profit distance:

- Gross take-profit result: approximately 4.00 USDT.
- Estimated round-trip taker fees at 0.045% per side: approximately 0.18 USDT.
- Estimated net take-profit result: approximately 3.82 USDT.

Actual profit varies with each signal's take-profit distance, quantity-step rounding, slippage,
fees, and funding. This design no longer targets 10 USDT net profit per trade.

## Approved Runtime Settings

- `mode = live`
- `live.auto_trade = true`
- `risk.leverage = 2`
- `live.fixed_margin_u = 100`
- `live.fixed_margin_pct = 0`
- `live.fixed_notional_u = 0`
- `live.max_positions = 1`
- `risk.account_equity = 1479.41`
- `live.max_loss_pct = 2`
- `trade_direction = both`

The Binance minimum-notional rule remains in force. A nominal 200 USDT position is comfortably
above the 20 USDT minimum even after quantity-step rounding.

## Existing Position

Do not resize, add to, reduce, close, or replace any position that is already open. Do not cancel
or replace its protective orders. The new size applies only to the next automatic entry.

## Safe Update Procedure

1. Verify the deployed revision, service health, current settings, positions, and protection
   orders.
2. Temporarily set `live.auto_trade = false` to prevent a race with a new signal.
3. Set `live.fixed_margin_u = 100` and retain every other approved risk limit.
4. Read all relevant settings back from the running application.
5. Confirm existing positions and protection orders were not changed.
6. Set `live.auto_trade = true` only after verification passes.
7. Independently verify settings, positions, service health, and recent error logs.

## Failure and Rollback

If any check fails, set `live.auto_trade = false` and leave it off. Restore the prior
`live.fixed_margin_u` value observed during preflight. Do not change any existing position or
protective order.

## Scope and Risk

This is a VPS live-trading hot-configuration change. It does not deploy local code. Each future
entry may reserve about 100 USDT margin, approximately 6.8% of the observed 1479.41 USDT wallet
balance. The one-position limit and 2% balance circuit breaker remain active, but the breaker
only blocks new entries and does not close an existing position.
