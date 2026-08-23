# Live 500 USDT Notional Sizing Design

Date: 2026-08-23

## Decision

Increase the small live auto-trading pilot from 10 USDT fixed margin to 250 USDT fixed margin at
2x leverage, producing approximately 500 USDT notional per new position. Keep the one-position
limit and existing 2% wallet-balance circuit breaker.

The user selected fixed sizing and defined the goal as approximately 10 USDT net profit when a
trade reaches take profit.

## Expected Economics

Using the currently open LITUSDT signal as the sizing reference:

- Entry: 3.2868
- Take profit: 3.3551, approximately 2.078% above entry
- Stop loss: 3.2522, approximately 1.053% below entry
- Notional: approximately 500 USDT
- Margin at 2x: approximately 250 USDT
- Gross take-profit result: approximately 10.39 USDT
- Estimated round-trip taker fees at 0.045% per side: approximately 0.45 USDT
- Estimated net take-profit result: approximately 9.94 USDT
- Estimated stop result including fees: approximately -5.75 USDT

Fixed sizing does not guarantee 10 USDT profit on every signal. A signal with a 1% take-profit
distance will earn materially less, while a 3% take-profit distance will earn materially more.

## Approved Runtime Settings

- `mode = live`
- `live.auto_trade = true`
- `risk.leverage = 2`
- `live.fixed_margin_u = 250`
- `live.fixed_margin_pct = 0`
- `live.fixed_notional_u = 0`
- `live.max_positions = 1`
- `risk.account_equity = 1479.41`
- `live.max_loss_pct = 2`
- `trade_direction = both`

## Existing Position

Do not increase, reduce, replace, or otherwise modify the currently open LITUSDT position. Do
not cancel or replace its existing stop-loss or take-profit protection. The new sizing applies
only to a future automatic entry after the current position is closed.

## Safe Update Procedure

1. Verify the audited VPS revision, service health, current settings, LITUSDT position, and its
   live stop-loss and take-profit orders.
2. Temporarily set `live.auto_trade = false`; this must not close the existing position.
3. Set `live.fixed_margin_u = 250` while retaining the other approved risk limits.
4. Read every relevant setting back from the running process.
5. Re-enable `live.auto_trade = true` only if all values match and the existing position and
   protection orders remain intact.
6. Independently re-read settings, positions, orders, service health, and recent errors.

## Rollback

If verification fails, set `live.auto_trade = false` and leave it off. Do not close the existing
position automatically. Restoring the previous 10 USDT margin requires an explicit settings
update to `live.fixed_margin_u = 10`.

## Known Risks

- A 250 USDT margin allocation is about 16.9% of the observed 1479.41 USDT wallet balance.
- Exchange minimums and quantity-step rounding can change the exact notional and profit.
- Fees, slippage, and funding can reduce net profit below the estimate.
- The 2% circuit breaker blocks new entries below approximately 1449.82 USDT wallet balance; it
  does not close an existing position.
- The strategy's fee-adjusted paper edge remains unproven over longer windows.
