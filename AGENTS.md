# Trade System Agent Entry

This repository is the single source of truth for a personal crypto trading research and execution system.

All agents must read this file first, then follow the linked knowledge files before changing code.

## Mission

Build a system with two application entry points in one repository:

- Local research app: strategy lab, backtests, chart review, case library, macro research, watch agents.
- VPS live app: stable signal monitoring, paper trading, Telegram alerts, controlled live execution.

The local app is the research lab. The VPS app is the execution room. Never experiment directly on live execution code or settings.

## Current Important Paths

- `scripts/bt_viewer.py`: local backtest chart viewer on port 8530.
- `scripts/bt_refresh.py`: local Binance futures kline cache refresh.
- `scripts/bt_scan.py`: local strategy signal precomputation.
- `scripts/bt_registry.py`: local strategy registry used by scanner and viewer.
- `.btcache/`: local cached market data and generated signal JSON. This is local data, not committed.
- `app/engine/macro_pullback.py`: current macro pullback / reversal strategy logic.
- `app/engine/signals.py`: live signal routing.
- `app/engine/trader.py`: live Binance execution.
- `deploy/deploy.ps1`: current legacy deployment script. Treat as temporary until Git-based deployment is implemented.
- `research/`: 灵感库/研究档案。`research/ideas/<n>-<slug>/` = 可回测的交易策略灵感(原图+假设+研究链路);
  `research/principles/` = 沉淀下来的交易准则。看图器 `/ideas` 页面读取。用户发来的形态截图和想法必须存到这里,
  原话一字不改, 模糊处标"待定"而不是替他假设。每次回测/证伪往对应 `timeline.md` 顶部追加一条。
- `docs/knowledge/`: stable project knowledge base.
- `docs/journal/`: user lifecycle log, ideas, decisions, and todos.
- `docs/agents/`: per-agent readable memory and research notes.
- `.claude/commands/trade-loop.md`: Claude Code loop instruction for task-by-task implementation.

## Mandatory Rules

1. Do not touch `.env`, API keys, or secrets.
2. Do not enable live auto-trading unless the user explicitly asks.
3. Do not deploy without verifying the exact version and service health.
4. Do not make strategy logic changes without updating strategy docs and changelog.
5. Do not delete `.btcache/`, `data/`, or `logs/` unless the user explicitly asks.
6. Backtest locally first, review charts second, paper/live only after approval.
7. If blocked by missing keys, missing credentials, or external accounts, scaffold the code and mark the item as blocked instead of stopping the whole plan.
8. Every implementation task must leave a short note in the relevant journal or agent handoff file.

## Read Order

1. `docs/knowledge/README.md`
2. `docs/knowledge/project_blueprint.md`
3. `docs/knowledge/local_backtest_studio.md`
4. `docs/knowledge/deployment.md`
5. `docs/knowledge/strategy_contract.md`
6. `docs/knowledge/agent_workflow.md`
7. `docs/journal/todos.md`
8. `.claude/commands/trade-loop.md`

## Verification Before Handoff

For any completed task, report:

- Files changed.
- Commands run.
- Test or verification output.
- Remaining risks.
- Whether the change affects local research only, VPS paper, or live trading.

