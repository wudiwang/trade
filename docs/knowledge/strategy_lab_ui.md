# Strategy Lab UI Plan

## Purpose

Upgrade the local backtest viewer into a practical strategy lab.

## Pages

### Strategy Overview

Show one card or table row per strategy:

- name,
- status,
- directions,
- timeframes,
- sample count,
- win rate,
- expected R,
- profit factor,
- latest update,
- owner agent,
- allowed for paper/live.

### Strategy Detail

Show:

- original idea,
- classic seed charts,
- current logic,
- parameters,
- changelog,
- code file paths,
- metrics by span, direction, and timeframe,
- saved good/bad cases.

### Signal Chart Review

Requirements:

- switch timeframe,
- show K lines and volume,
- show entry, SL, TP,
- show strategy markers,
- save current chart as a case,
- add user label and note,
- show post-entry MFE/MAE.

### Strategy Composer

First version supports:

- union,
- intersection,
- same symbol,
- time window in bars or minutes,
- same-direction toggle,
- filter strategy plus trigger strategy.

Example:

```text
Funding Extreme Filter AND Macro Pullback Trigger within 12 bars, same direction.
```

### Agent Workbench

Show a table:

- agent,
- responsibility,
- current strategy,
- latest action,
- latest artifact,
- next task,
- blocked items,
- handoff link.

## Phase 1 Priority

Build for practical research first. Avoid decorative UI. The user should be able to decide whether a strategy is valuable and whether its chart hits match the intended pattern.

