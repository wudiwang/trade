# Entry Bar Direction Confirmation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reject macro-pullback entries when the actual entry candle reverses against the second-buy or second-sell direction.

**Architecture:** Add one pure confirmation predicate in `app/engine/macro_pullback.py` and call it from both live detection and the local registry scanner. Preserve all structure, exit, sizing, and risk behavior.

**Tech Stack:** Python 3, pytest, SQLite replay data, PowerShell deployment, systemd.

---

### Task 1: Reproduce the bad entry

**Files:**
- Modify: `tests/test_macro_pullback.py`

- [ ] Add a second-sell fixture whose entry candle closes above both its open and the stall close; assert no signal.
- [ ] Add the mirrored second-buy fixture whose entry candle closes below both its open and the stall close; assert no signal.
- [ ] Run `python -m pytest tests/test_macro_pullback.py -q` and verify both new assertions fail because signals are still returned.

### Task 2: Add the shared confirmation rule

**Files:**
- Modify: `app/engine/macro_pullback.py`
- Modify: `scripts/bt_registry.py`

- [ ] Add `_entry_bar_confirms(direction, klines, stall_idx, entry_idx)`.
- [ ] For long require `entry.close >= entry.open` and `entry.close >= stall.close`; for short require the mirrored comparisons.
- [ ] Reject the live signal immediately after `_stall_entry_idx` when the predicate fails.
- [ ] Import and apply the same predicate in the registry scanner before emitting a backtest signal.
- [ ] Run the focused tests and verify they pass.

### Task 3: Verify, document, and release

**Files:**
- Create: `docs/strategies/macro_pullback/logic.md`
- Create: `docs/strategies/macro_pullback/changelog.md`
- Create: `docs/journal/daily/2026-08-31-entry-bar-confirmation.md`

- [ ] Run the complete test suite.
- [ ] Replay the captured ZEC window and verify the prior short is rejected.
- [ ] Record the semantic change and residual risk.
- [ ] Commit only this isolated branch.
- [ ] Deploy the exact commit and verify service health and runtime settings.
