# Wyckoff-Gated Chan Second Entry Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the loose macro pullback paper strategy with a strict Wyckoff first-entry then Chan second-entry paper strategy on `5m` and `15m`.

**Architecture:** Keep the existing `macro_pullback` integration point, but replace the detector internals with a state-free scan that finds a confirmed high-volume Spring/UTAD first entry followed by a confirmed second fractal. Backtesting gains balanced quote-volume sampling so 7-day tests cover more symbols without hammering Binance.

**Tech Stack:** Python 3, SQLite, existing Binance REST/WS engine, direct script-style tests.

---

### Task 1: Update Detector Tests

**Files:**
- Modify: `tests/test_macro_pullback.py`

- [ ] Add tests where a short signal requires UTAD first sell, a down leg, then a second top fractal.
- [ ] Add tests where a long signal requires Spring first buy, an up leg, then a second bottom fractal.
- [ ] Add a negative test where a second top exists without prior UTAD and must not signal.
- [ ] Run `python tests/test_macro_pullback.py` and confirm the new tests fail before implementation.

### Task 2: Replace Detector Internals

**Files:**
- Modify: `app/engine/macro_pullback.py`
- Modify: `config.yaml`

- [ ] Add parameters: `timeframes`, `vol_ma`, `vol_mult`, `lookback`, `reclaim_bars`, `reclaim_tolerance_pct`, `min_leg_pct`, `second_tolerance_pct`.
- [ ] Implement Spring detection:
  - sweep below previous low,
  - sweep candle volume at least `vol_mult` times previous `vol_ma` average,
  - reclaim to prior down-leg start within `reclaim_bars`,
  - first bottom fractal near the sweep.
- [ ] Implement UTAD detection as the short-side mirror.
- [ ] Implement second-entry detection:
  - long: first bottom `L1`, up leg, later bottom fractal `L2`, `L2` does not break `L1`.
  - short: first top `H1`, down leg, later top fractal `H2`, `H2` does not break `H1`.
- [ ] Keep `Signal.extra.path = "macro_chan_pullback"` and use `type = "second_buy"` or `type = "second_sell"`.
- [ ] Run `python tests/test_macro_pullback.py` until it passes.

### Task 3: Timeframe Integration

**Files:**
- Modify: `app/engine/signals.py`
- Modify: `app/engine/backtest.py`

- [ ] Update exclusive strategy routing so `5m` and `15m` can both trigger.
- [ ] Pass the current triggering timeframe into the detector.
- [ ] Backtest event walking should evaluate both `5m` and `15m` events.
- [ ] Run `python tests/test_macro_pullback.py` and `PYTHONIOENCODING=utf-8 python tests/test_chan_bi.py`.

### Task 4: Balanced Backtest Sampling

**Files:**
- Modify: `tests/backtest_cli.py`

- [ ] Add balanced symbol sampling from Binance 24h quote volume.
- [ ] Default target sample size is 90, split evenly across high, medium, and low volume thirds.
- [ ] Preserve explicit symbol-list override for small functional runs.
- [ ] Run a 7-day sampled backtest: `python tests/backtest_cli.py 7 5m,15m btc_on sample:90`.

### Task 5: Reset, Deploy, Verify

**Files:**
- Existing project files only.

- [ ] Clear local paper data with `python tools/reset_paper_data.py`.
- [ ] Deploy with `.\deploy\deploy.ps1`.
- [ ] Clear remote paper data with `cd /opt/trade && venv/bin/python tools/reset_paper_data.py`.
- [ ] Restart remote service and verify:
  - service is `active`,
  - web IP returns `200`,
  - `mode = paper`,
  - `macro_pullback.enabled = True`,
  - `macro_pullback.exclusive = True`,
  - paper tables are reset.
