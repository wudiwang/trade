# 2026-08-29 Live Short Restoration

## Approved Runtime Contract

- Apply only to new signals after deployment; do not chase or replay expired signals.
- Live automatic direction: short only. Long signals remain research records and do not open positions.
- Fixed margin: 60 USDT per entry.
- Leverage: 5x.
- Approximate notional: 300 USDT per entry before exchange quantity rounding.
- Maximum concurrent positions: 10, preserving margin headroom on the observed account balance.
- Drawdown breaker: 2% relative to the configured 1479.41 U equity baseline.
- Breaker equity excludes USDT transfers since the equity baseline was written.

## Root Cause and Change

The prior breaker compared raw futures wallet balance to the baseline. A `TRANSFER -400 U`
therefore halted automation even though realized PnL less commissions and funding was about
`+4.56 U`. The engine now subtracts net USDT transfers from raw wallet balance before applying
the same 2% loss threshold. Binance transfer-history failure is fail-closed. Missing, future, or
older-than-89-days baseline timestamps also fail closed because Binance exposes only the most
recent three months of income history. Risk caches are five seconds, position lookup failure
skips the order, and an async lock serializes position checking through order placement. Mode,
automation, and direction are rechecked inside the lock. Each successful entry reserves a local
position slot for 15 seconds so delayed Binance position visibility cannot exceed the configured
concurrent-position limit.

## Scope

Code paths changed: Binance income query parameters and live automatic risk checking. Strategy
signal generation and historical paper settlement are unchanged. Deployment and final runtime
verification are recorded in the handoff after activation.

## Deployment Result

- Previous VPS revision: `2a96f59a40122dba9c85e3266142d9fe14fba95a`.
- Deployed code revision: `16e61a0604342d9be7694e8e0790c59ef07ac750`.
- Rollback snapshot: `/opt/trade/data/deploy-backup-20260829-16e61a0.json`.
- The first activation health command was incorrectly expanded by local PowerShell. The safety
  handler wrote `live.auto_trade=false` and restarted successfully before the activation was
  retried with separate health commands.
- Final runtime values: `mode=live`, `live.auto_trade=true`, `trade_direction=short`,
  `risk.leverage=5`, `live.fixed_margin_u=60`, fixed percent/notional fallbacks zero,
  `live.max_positions=10`, `live.max_loss_pct=2`, equity baseline unchanged at `1479.41`.
- Final read-only check: raw wallet `1108.81167704 U`, net transfer `-400 U`, adjusted equity
  `1508.81167704 U`, breaker threshold `1449.8218 U`, no open positions, HTTP 200, service active,
  fresh market data, and no warning/error journal entries after the final restart.
- Verification before deployment: 13 focused automatic-risk tests and 81 full tests passed.
