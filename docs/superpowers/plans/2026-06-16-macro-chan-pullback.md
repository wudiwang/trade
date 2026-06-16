# Macro Chan Pullback Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build and deploy a paper-trading strategy that uses the manual BTC macro direction to trigger altcoin second-buy and second-sell setups across `5m`, `15m`, and `1h`.

**Architecture:** Add a focused `app/engine/macro_pullback.py` module for structure detection, trigger confirmation, and signal construction. Integrate it into `SignalEngine.evaluate_all()` behind config, reuse the existing `PaperBroker.open_from_signal()` path, add reset tooling for paper data, and extend backtesting with a deterministic proxy macro mode.

**Tech Stack:** Python 3, SQLite, FastAPI, existing Binance REST/WS engine, direct script-style tests.

---

### Task 1: Strategy Detector Unit Tests

**Files:**
- Create: `tests/test_macro_pullback.py`
- Create: `app/engine/macro_pullback.py`

- [ ] **Step 1: Write failing tests for second-sell and second-buy detection**

Create `tests/test_macro_pullback.py` with synthetic 15m and 5m candles that model the user's screenshots: impulse, first turn, weaker retest, and 5m trigger.

```python
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.engine.macro_pullback import detect_macro_pullback


def k(o, h, l, c, v=100.0, t=0, step_ms=300000):
    return {
        "open_time": t * step_ms,
        "open": o,
        "high": h,
        "low": l,
        "close": c,
        "volume": v,
        "quote_volume": v * c,
        "taker_buy": v / 2,
        "closed": 1,
    }


def cfg(**overrides):
    base = {
        "enabled": True,
        "structure_tf": "15m",
        "trigger_tf": "5m",
        "impulse_window": 18,
        "impulse_min_pct": 3.0,
        "ma_period": 5,
        "ma_extension_pct": 0.5,
        "retest_tolerance_pct": 0.4,
        "volume_decay_ratio": 1.2,
        "stop_buffer_pct": 0.3,
        "min_rr": 1.2,
        "tp_lookback": 30,
        "vp_bins": 12,
    }
    base.update(overrides)
    return base


def sell_structure():
    vals = [
        (70, 70.5, 69.8, 70.2, 100),
        (70.2, 71.2, 70.1, 71.0, 120),
        (71.0, 72.2, 70.9, 72.0, 150),
        (72.0, 73.4, 71.9, 73.2, 170),
        (73.2, 74.7, 73.0, 74.5, 190),
        (74.5, 76.1, 74.2, 75.8, 220),
        (75.8, 76.05, 74.8, 75.0, 180),
        (75.0, 75.2, 73.8, 74.0, 160),
        (74.0, 74.4, 73.2, 73.6, 150),
        (73.6, 74.4, 73.4, 74.1, 90),
        (74.1, 75.35, 73.9, 75.0, 85),
        (75.0, 75.45, 74.6, 74.8, 80),
    ]
    return [k(*row, t=i, step_ms=900000) for i, row in enumerate(vals)]


def sell_trigger():
    vals = [
        (74.6, 75.1, 74.5, 74.9, 60),
        (74.9, 75.35, 74.8, 75.25, 65),
        (75.25, 75.45, 75.0, 75.1, 60),
        (75.1, 75.15, 74.7, 74.8, 70),
        (74.8, 74.9, 74.2, 74.35, 90),
    ]
    return [k(*row, t=100 + i) for i, row in enumerate(vals)]


def test_detect_second_sell_after_failed_retest():
    sig = detect_macro_pullback("SOLUSDT", "short", sell_structure(), sell_trigger(), cfg())
    assert sig is not None
    assert sig.direction == "short"
    assert sig.extra["type"] == "second_sell"
    assert sig.extra["path"] == "macro_chan_pullback"
    assert sig.sl > sig.entry
    assert sig.tp < sig.entry
    assert sig.extra["structure"]["H2"] < sig.extra["structure"]["H1"]


def buy_structure():
    vals = [
        (76, 76.2, 75.5, 75.6, 100),
        (75.6, 75.8, 74.5, 74.7, 130),
        (74.7, 74.9, 73.2, 73.5, 170),
        (73.5, 73.8, 72.1, 72.4, 210),
        (72.4, 72.6, 70.8, 71.2, 240),
        (71.2, 72.0, 70.7, 71.8, 180),
        (71.8, 73.2, 71.6, 72.9, 130),
        (72.9, 73.4, 72.4, 73.1, 110),
        (73.1, 73.2, 71.4, 71.8, 90),
        (71.8, 72.1, 71.3, 71.9, 85),
    ]
    return [k(*row, t=i, step_ms=900000) for i, row in enumerate(vals)]


def buy_trigger():
    vals = [
        (71.8, 72.0, 71.35, 71.5, 60),
        (71.5, 71.7, 71.3, 71.45, 58),
        (71.45, 72.0, 71.4, 71.9, 65),
        (71.9, 72.35, 71.85, 72.25, 90),
    ]
    return [k(*row, t=100 + i) for i, row in enumerate(vals)]


def test_detect_second_buy_after_higher_low():
    sig = detect_macro_pullback("SOLUSDT", "long", buy_structure(), buy_trigger(), cfg())
    assert sig is not None
    assert sig.direction == "long"
    assert sig.extra["type"] == "second_buy"
    assert sig.sl < sig.entry
    assert sig.tp > sig.entry
    assert sig.extra["structure"]["L2"] > sig.extra["structure"]["L1"]


def test_neutral_macro_has_no_signal():
    assert detect_macro_pullback("SOLUSDT", "neutral", sell_structure(), sell_trigger(), cfg()) is None
```

- [ ] **Step 2: Run the tests to verify failure**

Run: `python tests/test_macro_pullback.py`

Expected: fail with `ModuleNotFoundError: No module named 'app.engine.macro_pullback'`.

### Task 2: Strategy Detector Implementation

**Files:**
- Create: `app/engine/macro_pullback.py`
- Test: `tests/test_macro_pullback.py`

- [ ] **Step 1: Implement `detect_macro_pullback()`**

Create `app/engine/macro_pullback.py` with:

```python
import time
from dataclasses import dataclass

from .chan import ema, find_fractals, merge_klines
from .signals import Signal
from .volume_profile import build_profile, nearest_hvn_above, nearest_hvn_below


@dataclass
class Setup:
    direction: str
    entry: float
    sl: float
    tp: float
    rr: float
    trigger: str
    structure: dict
    vol_ratio: float


def detect_macro_pullback(symbol: str, macro_direction: str, struct_klines: list,
                          trigger_klines: list, params: dict) -> Signal | None:
    # Full implementation in the task execution step.
    raise NotImplementedError
```

Replace the placeholder with the final implementation:

- `macro_direction == "short"` searches latest 15m top fractal as `H2`, earlier top as `H1`, low between as `L1`, validates impulse, lower high, volume decay, and 5m bearish trigger.
- `macro_direction == "long"` searches latest 15m bottom fractal as `L2`, earlier bottom as `L1`, high between as `H1`, validates impulse, higher low, volume decay, and 5m bullish trigger.
- Builds `Signal` using the existing `Signal` dataclass.
- Uses volume profile or 2R fallback for take profit.

- [ ] **Step 2: Run detector tests**

Run: `python tests/test_macro_pullback.py`

Expected: all tests pass and print no assertion errors.

### Task 3: Config and SignalEngine Integration

**Files:**
- Modify: `config.yaml`
- Modify: `app/engine/signals.py`
- Test: `tests/test_macro_pullback.py`

- [ ] **Step 1: Add config defaults**

Add this block to `config.yaml`:

```yaml
macro_pullback:
  enabled: true
  structure_tf: 15m
  trigger_tf: 5m
  context_tf: 1h
  impulse_window: 24
  impulse_min_pct: 4.0
  ma_period: 20
  ma_extension_pct: 1.5
  retest_tolerance_pct: 0.4
  volume_decay_ratio: 0.8
  stop_buffer_pct: 0.3
  cooldown_bars: 12
  min_rr: 1.5
  tp_lookback: 100
  vp_bins: 50
```

- [ ] **Step 2: Call the strategy from `SignalEngine.evaluate_all()`**

At the beginning of `evaluate_all()`, when the current closed timeframe is `5m`, call:

```python
mp = self._eval_macro_pullback(symbol, kbt)
if mp:
    out.append(mp)
```

Add `_eval_macro_pullback()` that:

- reads `self.macro_view["direction"]`,
- skips `neutral`,
- passes 15m and 5m klines to `detect_macro_pullback()`,
- applies cooldown by `(symbol, "macro_pullback", direction)`,
- stores cooldown only when a signal is returned.

- [ ] **Step 3: Run focused tests**

Run: `python tests/test_macro_pullback.py`

Expected: all tests pass.

### Task 4: Paper Data Reset Tool

**Files:**
- Create: `tools/reset_paper_data.py`

- [ ] **Step 1: Add reset script**

Create `tools/reset_paper_data.py`:

```python
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import get_config
from app.db import DB


TABLES = ("signals", "paper_trades", "equity_curve")


def main():
    cfg = get_config()
    db = DB(cfg.db_path)
    for table in TABLES:
        db.execute(f"DELETE FROM {table}")
    db.log("info", "reset", "cleared signals, paper_trades, equity_curve for macro_pullback rollout")
    print("cleared: " + ", ".join(TABLES))


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Run reset script only after tests/backtest pass**

Run: `python tools/reset_paper_data.py`

Expected: `cleared: signals, paper_trades, equity_curve`.

### Task 5: Backtest Support

**Files:**
- Modify: `app/engine/backtest.py`
- Modify: `tests/backtest_cli.py`

- [ ] **Step 1: Add proxy manual macro lookup**

In `backtest.py`, add a macro lookup mode that maps BTC trend to manual macro direction:

- BTC trend `1` means macro `long`.
- BTC trend `-1` means macro `short`.
- BTC trend `0` means `neutral`.

Set `eng.macro_view` before `evaluate_all()` in `walk_symbol_mtf()`.

- [ ] **Step 2: Include macro pullback stats in output**

Ensure existing `by_path` and `by_type` include `macro_chan_pullback`, `second_buy`, and `second_sell`.

- [ ] **Step 3: Run a small backtest**

Run: `python tests/backtest_cli.py 3 5m,15m,1h btc_on`

Expected: completes with JSON summary and no exceptions.

### Task 6: Verification and Deployment

**Files:**
- Existing project files only.

- [ ] **Step 1: Run unit tests**

Run:

```powershell
python tests/test_macro_pullback.py
python tests/test_chan_bi.py
```

Expected: both pass.

- [ ] **Step 2: Run local smoke where feasible**

Run:

```powershell
python tests/backtest_cli.py 7 5m,15m,1h btc_on
```

Expected: completes and prints summary.

- [ ] **Step 3: Clear local paper data**

Run:

```powershell
python tools/reset_paper_data.py
```

Expected: old local simulated signals and paper trades are gone.

- [ ] **Step 4: Deploy**

Run:

```powershell
.\deploy\deploy.ps1
```

Expected: deployment completes without error.

- [ ] **Step 5: Verify online service**

Open the online Web console, confirm:

- mode remains `paper`,
- timeframes are `5m/15m/1h`,
- status is healthy,
- paper stats start from zero,
- changing macro direction controls whether the new strategy can produce signals.
