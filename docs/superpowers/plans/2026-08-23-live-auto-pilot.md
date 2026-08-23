# Live Auto-Trading Small-Pilot Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Enable a tightly limited live auto-trading pilot on the audited VPS revision and leave a verified rollback path and audit record.

**Architecture:** Use the running application's authenticated localhost settings API so SQLite overrides and in-memory configuration change together without deploying code or restarting the service. Apply all risk limits while automation remains off, verify them, then enable `live.auto_trade` as the final mutation. Any failed verification triggers an immediate API rollback to `live.auto_trade = false`.

**Tech Stack:** PowerShell, SSH, Python 3, FastAPI localhost API, SQLite settings overrides, Binance Futures read-only account endpoints, systemd.

---

### Task 1: Preflight the Exact Live Runtime

**Files:**
- Read: `deploy/verify_remote.ps1`
- Read: `docs/superpowers/specs/2026-08-23-live-auto-pilot-design.md`
- No files modified

- [x] **Step 1: Verify version and service health**

Run:

```powershell
.\deploy\verify_remote.ps1
```

Expected: remote revision is exactly `2a96f59a40122dba9c85e3266142d9fe14fba95a`, service is `active`, web HTTP is `200`, strategy function is present, and result is `HEALTHY`. The local revision may differ because no deployment is part of this task.

- [x] **Step 2: Query current settings, balance, and positions without printing secrets**

Run a Python audit over SSH that loads `Config`, `DB`, and setting overrides, then calls only `account_info()` and `position_risk()` through `BinanceRest`. Print only the approved setting keys, total wallet balance, available balance, and non-zero positions.

Expected: `mode=live`, `live.auto_trade=false`, wallet balance is positive, and positions is an empty list.

- [x] **Step 3: Enforce the abort gate**

Do not proceed if the remote revision changed, service health failed, `live.auto_trade` is already true, or any futures position is open. Report the observed condition without changing VPS state.

### Task 2: Apply Risk Limits While Automation Is Off

**Files:**
- Modify externally: `/opt/trade/data/trade.db` through the running localhost API
- No repository code modified

- [x] **Step 1: Create an authenticated localhost session token in memory**

Inside a remote Python process in `/opt/trade`, instantiate `DB(get_config().db_path)`, load the existing username, and call `app.web.server.make_token(db, username, 1)`. Keep the token in process memory and never print it.

- [x] **Step 2: POST the complete safe setting set with automation disabled**

Send this JSON to `http://127.0.0.1:8488/api/settings` with the in-memory token in the `session` cookie:

```json
{
  "mode": "live",
  "live.auto_trade": "false",
  "risk.account_equity": "1479.41",
  "risk.leverage": "2",
  "live.fixed_margin_u": "10",
  "live.fixed_margin_pct": "0",
  "live.fixed_notional_u": "0",
  "live.max_positions": "1",
  "live.max_loss_pct": "2",
  "trade_direction": "both"
}
```

Expected: HTTP 200 with `ok=true`; `applied` contains every submitted key and `live.auto_trade` is false.

- [x] **Step 3: Read settings back from the running process**

GET `http://127.0.0.1:8488/api/settings` with the same in-memory session cookie.

Expected: every returned value exactly matches the approved setting set. In particular, leverage is 2, fixed margin is 10, fixed margin percent is 0, maximum positions is 1, loss limit is 2, and auto trading is false.

- [ ] **Step 4: Roll back on mismatch**

If the POST or read-back fails, POST only `{"live.auto_trade":"false"}`, verify it reads back as false, and stop. Do not enable automation.

### Task 3: Enable Automation as the Final Mutation

**Files:**
- Modify externally: `/opt/trade/data/trade.db` through the running localhost API
- No repository code modified

- [x] **Step 1: Recheck positions immediately before enablement**

Call the Binance `position_risk()` endpoint again from the VPS.

Expected: no position has a non-zero `positionAmt`. Abort and leave automation false otherwise.

- [x] **Step 2: Enable only the automation flag**

POST this JSON to the localhost settings API:

```json
{"live.auto_trade":"true"}
```

Expected: HTTP 200 with `ok=true` and `applied.live.auto_trade=true`.

- [x] **Step 3: Verify effective settings and service health**

GET `/api/settings` and `/api/status` using the authenticated localhost session, then run `systemctl is-active trade` and a localhost HTTP status check.

Expected: all safe settings remain unchanged, `live.auto_trade=true`, application status reports `mode=live`, service is `active`, and web HTTP is `200`.

- [x] **Step 4: Inspect post-change events**

Read the latest 20 `event_log` rows and the last 50 service journal lines. Do not print secrets or full order payloads.

Expected: a settings update event is present and there are no new configuration, authentication, engine, or order errors.

- [ ] **Step 5: Disable immediately if verification fails**

POST `{"live.auto_trade":"false"}` through the same API, verify false via GET, and report the failed check. Do not close positions or cancel protective orders automatically.

### Task 4: Record and Verify the Handoff

**Files:**
- Create: `docs/journal/daily/2026-08-23.md`
- Modify: `docs/superpowers/plans/2026-08-23-live-auto-pilot.md`

- [x] **Step 1: Mark only completed plan checkboxes**

Change each successfully executed `- [ ]` step to `- [x]`. Leave aborted or skipped steps unchecked.

- [x] **Step 2: Write the required journal checkpoint**

Record:

```text
Task completed: Small live auto-trading pilot activation
Files changed: docs/superpowers/plans/2026-08-23-live-auto-pilot.md; docs/journal/daily/2026-08-23.md
Commands run: read-only VPS audit; authenticated localhost settings updates; health and log checks
Results: deployed revision; final effective settings; service/web status; wallet balance; open-position count
Risks: real Binance Futures orders are now possible; fee-adjusted paper edge is unproven; circuit breaker blocks new entries but does not close positions
Scope: VPS live trading
Next recommended step: review after the first live fill or within 24 hours, whichever occurs first
```

- [x] **Step 3: Verify repository hygiene**

Run:

```powershell
git diff --check -- docs/superpowers/plans/2026-08-23-live-auto-pilot.md docs/journal/daily/2026-08-23.md
git status --short
```

Expected: no whitespace errors; `.env` is unchanged; unrelated pre-existing working-tree changes remain untouched.

- [x] **Step 4: Commit only the plan and journal**

Run:

```powershell
git add -- docs/superpowers/plans/2026-08-23-live-auto-pilot.md docs/journal/daily/2026-08-23.md
git commit -m "ops: record small live auto-trading pilot"
```

Expected: one commit containing only the plan and 2026-08-23 journal entry.
