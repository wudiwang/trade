# Trading Research Platform Blueprint

## Product Goal

Build a personal crypto trading research platform that can:

- turn chart ideas into coded strategies,
- backtest strategies locally,
- review historical hit charts with markers,
- save classic and rejected chart cases,
- combine strategies by intersection and union,
- track macro and BTC context,
- run AI watch agents for important symbols,
- publish verified strategies to VPS paper/live execution through a controlled release process.

## Architecture

One repository, two application entry points:

```text
Local Research App
  - strategy lab
  - backtest studio
  - pattern library
  - watch agent workspace
  - macro research dashboard
  - agent workbench

VPS Live App
  - real-time market data
  - approved strategy runtime
  - paper trading
  - Telegram alerts
  - live order execution
  - TP/SL protection
  - health checks
```

Shared code should live in reusable modules:

```text
strategies/ or app/engine/strat_*
shared market data helpers
shared marker schema
shared backtest settlement logic
shared risk model
```

## Phase 1 Scope

Phase 1 must make the system usable and auditable:

1. Create knowledge base and agent memory structure.
2. Make deployment versioned and verifiable.
3. Upgrade local backtest system with strategy details, metrics, timeframe switching, and case saving.
4. Define a strategy contract shared by local and live systems.
5. Add Strategy Lab pages for strategy introductions, changelogs, metrics, and classic charts.
6. Add Agent Workbench table showing each agent, responsibility, latest work, output, and next task.
7. Add Watch Agent design and storage model, but do not allow uncontrolled auto-trading.
8. Add macro research storage model, with YouTube ingestion scaffold that can be enabled later when credentials or tooling are ready.

## Phase 2 Scope

1. YouTube analyst ingestion and transcript summarization.
2. Macro data feeds: liquidity, rates, central bank policy, risk appetite.
3. 1m high-frequency watch agents for selected symbols.
4. Advanced strategy composer with intersections, unions, filters, and trigger windows.
5. Multi-agent scorecards and automatic research reports.
6. Docker or CI/CD deployment.

## Release Gates

No strategy moves forward without:

1. Local backtest.
2. Historical chart review.
3. Good and bad pattern cases saved.
4. Strategy changelog updated.
5. Risk model reviewed.
6. Paper mode observation.
7. Explicit user approval before live.

## Known Current Risks

- Legacy deployment currently uses archive upload and can obscure exact online version.
- Local data cache can become stale if Windows scheduled task fails.
- Some strategy scanner files and live runtime strategy files are not yet fully unified.
- Current local viewer is useful but too simple for long-term research.

