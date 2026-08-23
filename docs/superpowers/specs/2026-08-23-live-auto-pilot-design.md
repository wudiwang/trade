# Live Auto-Trading Small-Pilot Design

Date: 2026-08-23

## Decision

Enable a tightly limited live auto-trading pilot on the currently deployed VPS revision
`2a96f59a40122dba9c85e3266142d9fe14fba95a`. Do not deploy the dirty local worktree or
change strategy logic as part of this activation.

The user approved the small-pilot option after reviewing the current account state and
fee-adjusted paper results.

## Options Considered

1. Keep Telegram confirmation: lowest execution risk, but does not meet the requested
   automation goal.
2. Small automatic pilot: limits order size and concurrent exposure while collecting live
   execution evidence. This is the selected option.
3. Enable automation with the existing full settings: rejected because the existing settings
   permit about 500 USDT notional per order, ten concurrent positions, and an excessively wide
   balance-loss circuit breaker.

## Approved Runtime Settings

- `mode = live` (already active)
- `live.auto_trade = true`
- `risk.account_equity = 1479.41`
- `risk.leverage = 2`
- `live.fixed_margin_u = 10`
- `live.fixed_margin_pct = 0`
- `live.fixed_notional_u = 0`
- `live.max_positions = 1`
- `live.max_loss_pct = 2`
- `trade_direction = both` (unchanged)

Each new position therefore uses approximately 10 USDT margin and 20 USDT notional. The
balance circuit breaker stops new automatic entries when wallet balance is below approximately
1449.82 USDT. It does not close an existing position.

## Activation Procedure

1. Read the live settings, wallet balance, and positions immediately before activation.
2. Abort if there is an open futures position, the service is unhealthy, or the deployed
   revision differs from the audited revision.
3. Write all approved risk settings first while keeping `live.auto_trade = false`.
4. Read the settings back and verify every value.
5. Set `live.auto_trade = true` as the final mutation.
6. Read settings back again, confirm service health, and inspect recent service events for
   configuration or order errors.

## Failure and Rollback

If any verification fails, immediately set `live.auto_trade = false`. Do not change `.env`, API
keys, strategy code, historical data, existing orders, or positions. Disabling automation only
prevents future automatic entries; it does not close an existing position or cancel its TP/SL.

## Verification Evidence

Record the deployed revision, final runtime settings, service state, web HTTP status, open
positions, and relevant log output in the 2026-08-23 journal entry. The pilot affects VPS live
trading and can place real Binance Futures orders after a qualifying strategy signal.

## Known Risks

- Recent paper performance is approximately flat after estimated fees over three and seven
  days, and negative over longer windows; the pilot is evidence collection, not proof of edge.
- The circuit breaker uses wallet balance rather than daily realized loss and only blocks new
  entries.
- Exchange slippage, funding, order rejection, and protection-order failures can make live
  results differ from paper results.
- The system has no automatic time-based end to this pilot; it must be reviewed and disabled
  manually.
