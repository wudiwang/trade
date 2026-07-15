# Multi-Agent Workflow

## Principle

Agents can research, summarize, code limited modules, and propose changes. They cannot bypass backtesting, review, risk checks, or user approval.

## Roles

### Orchestrator Agent

Owns task routing, prioritization, and final recommendations.

### Strategy Research Agent

Owns one strategy. Converts user chart ideas into semantic rules, code changes, and backtest hypotheses.

### Backtest Agent

Runs backtests, generates scorecards, checks sample quality, and selects representative charts.

### Pattern Review Agent

Reviews good, bad, and borderline chart cases. Converts user feedback into rule refinements.

### Macro Agent

Tracks BTC market regime, YouTube analyst summaries, liquidity, macro background, and market risk.

### Watch Agent

Tracks selected symbols on 1m/5m and updates watch playbooks. It can alert but cannot freely live-trade.

### Risk Agent

Reviews position sizing, SL/TP, RR, max loss, leverage, and daily risk.

### Release Agent

Handles deployment, version checks, health checks, and rollback plan.

## Agent Memory Layout

Each agent should have:

```text
docs/agents/<agent_name>/memory.md
docs/agents/<agent_name>/research_log.md
docs/agents/<agent_name>/decisions.md
docs/agents/<agent_name>/handoff.md
```

Generated artifacts go under:

```text
artifacts/agents/<agent_name>/
```

## Handoff Requirement

Every completed agent task must update:

- `research_log.md` for work performed,
- `handoff.md` for current status and next step,
- strategy docs if strategy semantics changed,
- `docs/journal/todos.md` if new user-facing work remains.

## Review by Codex

Claude Code or any sub-agent must not be considered done until Codex reviews:

- `git diff`,
- tests or command output,
- docs updated,
- no secrets touched,
- no live auto-trade enabled unexpectedly.

