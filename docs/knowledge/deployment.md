# Deployment and Version Control

## Desired State

Deployment must be versioned, repeatable, verifiable, and rollbackable.

The VPS should run a known Git commit, and the web UI/API should show that commit.

## Current Legacy State

Current deployment script:

```text
deploy/deploy.ps1
```

It archives local `HEAD`, uploads `trade.tar.gz`, extracts into `/opt/trade`, installs dependencies, and restarts `trade.service`.

Risk: `/opt/trade` has not been a Git working tree, so exact deployed version can be hard to prove.

## Target Flow

```text
local development
→ git commit
→ push to remote
→ VPS git fetch
→ checkout exact commit
→ install dependencies
→ write REVISION
→ restart service
→ verify health
→ record deployment
```

## Required Deployment Checks

After deployment, verify:

1. `git rev-parse HEAD` matches intended commit.
2. `REVISION` contains intended commit and timestamp.
3. `trade.service` is active.
4. Web console returns HTTP 200.
5. Binance websocket connected.
6. Strategy route contains expected function names, such as `_eval_macro_pullbacks` for macro pullback.
7. Runtime settings match expected mode and auto-trade state.
8. No new errors in journal after restart.

## Live Drawdown Breaker

The live breaker compares the configured equity baseline with transfer-adjusted futures wallet
equity, not the raw wallet balance:

```text
adjusted equity = totalWalletBalance - net USDT TRANSFER since risk.account_equity.updated_at
```

This keeps deposits from masking trading losses and keeps withdrawals from being mistaken for
losses. Transfer history is read from Binance `GET /fapi/v1/income` with
`incomeType=TRANSFER` and pagination. If wallet or transfer history cannot be verified, live
automatic entry fails closed. Updating `risk.account_equity` starts a new transfer-adjustment
baseline because the settings row receives a new `updated_at` value.

Because Binance retains income history for only three months, the engine uses an 89-day safety
window and fails closed when the baseline is missing, in the future, or older than that window.
Wallet and adjusted-equity caches last five seconds. A process-wide async lock covers position
count verification through order placement, and position-query failure also fails closed. The
engine rechecks mode, automation, and direction after acquiring that lock, so queued signals obey
hot shutdowns. Successful entries reserve a local position slot for 15 seconds while Binance
position visibility catches up.

## Protected Files

Never overwrite or delete without explicit user approval:

- `/opt/trade/.env`
- `/opt/trade/data/trade.db`
- `/opt/trade/logs/`
- any exchange keys or Telegram tokens

## Rollback

Rollback should be:

```bash
cd /opt/trade
git checkout <previous_commit>
systemctl restart trade
systemctl is-active trade
```

Then verify the same health checks.

