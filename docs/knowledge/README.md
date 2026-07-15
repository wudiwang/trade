# Project Knowledge Base

This directory is the durable knowledge base for the trading system. It records architecture, operations, strategy rules, data flow, deployment, and agent workflows.

## Index

- `project_blueprint.md`: full product and engineering blueprint.
- `local_backtest_studio.md`: local backtest app, cache, refresh, viewer, and research workflow.
- `deployment.md`: version control, release, VPS deployment, rollback, and verification.
- `strategy_contract.md`: standard interface every strategy must satisfy.
- `strategy_lab_ui.md`: first-phase UI requirements for the local strategy lab.
- `agent_workflow.md`: multi-agent roles, permissions, memory layout, and handoff rules.
- `watch_agent.md`: AI watchlist and 1m tape-reading agent design.
- `macro_research.md`: BTC macro view, YouTube analyst ingestion, liquidity and risk context.
- `data_cache.md`: local cache file meanings, refresh frequency, rate-limit plan.

## Related Directories

- `docs/journal/`: daily lifecycle log, decisions, ideas, todos, and metrics.
- `docs/agents/`: personal memory and research logs for each sub-agent.
- `pattern_cases/`: saved classic chart patterns and user-labeled cases.
- `artifacts/`: generated reports, scorecards, screenshots, and agent outputs.

## Operating Principle

Research can be iterative and experimental locally. Live trading must be stable, versioned, auditable, and protected by explicit risk controls.

