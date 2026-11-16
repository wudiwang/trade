# Transfer-aware Live Short Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Exclude futures-account transfers from the live drawdown breaker and deploy future short signals at 60 USDT margin, 5x leverage.

**Architecture:** Extend the Binance income client with official pagination/filter parameters. In `Engine`, derive transfer-adjusted wallet equity from the configured equity baseline timestamp and use it for the existing 2% breaker. Keep direction and sizing as runtime settings so strategy detection code is unchanged.

**Tech Stack:** Python 3, asyncio, aiohttp, SQLite, pytest, systemd, PowerShell/SSH deployment.

---

### Task 1: Write failing breaker and REST tests

**Files:**
- Create: `tests/test_auto_risk.py`
- Modify: `app/engine/binance_rest.py`
- Modify: `app/engine/core.py`

- [ ] Add an async test proving a wallet of 1108.81 U plus `TRANSFER=-400 U` produces adjusted equity 1508.81 U and reaches `LiveTrader` instead of halting.
- [ ] Add an async test proving an adjusted balance below 98% still halts and never calls the trader.
- [ ] Add a REST-client test requiring `incomeType`, `page`, `startTime`, and `limit` to be forwarded.
- [ ] Run `python -m pytest tests/test_auto_risk.py -q`; expect failures because the transfer-aware method and REST parameters do not exist.

### Task 2: Implement transfer-aware breaker

**Files:**
- Modify: `app/engine/binance_rest.py`
- Modify: `app/engine/core.py`

- [ ] Extend `BinanceRest.income` with optional `end_ms`, `page`, and `income_type` parameters while preserving existing callers.
- [ ] Add a 60-second cached Engine helper that reads the equity setting timestamp, pages `incomeType=TRANSFER`, sums USDT transfers, and returns raw/transfer/adjusted balances.
- [ ] Replace the raw-wallet comparison in `_auto_execute` with adjusted equity. Fail closed if account or transfer history cannot be read, and emit one halt/recovery event transition.
- [ ] Run `python -m pytest tests/test_auto_risk.py -q`; expect all new tests to pass.

### Task 3: Document and verify

**Files:**
- Modify: `docs/knowledge/deployment.md`
- Create: `docs/journal/daily/2026-08-29-live-short.md`

- [ ] Document the transfer-adjusted breaker invariant and the exact approved runtime settings.
- [ ] Run `python -m pytest -q`; expect all tests to pass.
- [ ] Run `python -m py_compile app/engine/core.py app/engine/binance_rest.py` and `git diff --check`; expect exit 0.
- [ ] Commit only the spec, plan, source, tests, and journal files; push the feature branch.

### Task 4: Deploy and activate

**Files:**
- Runtime database settings only; do not modify `.env`, keys, positions, data, or logs.

- [ ] Record the pre-deploy revision and settings for rollback.
- [ ] Archive the exact pushed commit, upload it, extract to `/opt/trade`, write `REVISION`, install requirements, and restart `trade`.
- [ ] While automation remains false during the restart, atomically write: live mode, short-only, leverage 5, fixed margin 60, percentage/notional fallbacks 0, max positions 10, max loss 2.
- [ ] Verify adjusted equity is above the breaker threshold, then set `live.auto_trade=true` and restart once so the in-memory configuration exactly matches SQLite.
- [ ] Verify exact revision, active service, HTTP 200, websocket freshness, all runtime values, zero unexpected open positions, and no post-start errors. On any failure, set `live.auto_trade=false` before rollback.
