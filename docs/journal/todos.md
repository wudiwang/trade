# Master Todo List

## In Progress

- Build knowledge base and Claude Code loop instruction.
- Refresh local backtest cache and improve refresh scheduling.
- Plan local Strategy Lab upgrade.
- Plan version-controlled deployment.

## Next

- Implement Git-based VPS deployment with version checks.
- Add local data freshness display.
- Add strategy detail page to local backtest viewer.
- Add timeframe switching to chart review.
- Add save classic pattern case action.
- Add strategy metrics panel.
- Add Agent Workbench table.
- Add Watch Agent data model and page stub.

## Later

- YouTube analyst ingestion for BTC macro view.
- Macro liquidity dashboard.
- Advanced strategy composer.
- Docker or CI/CD deployment.
- Multi-agent scorecard generation.

## Blocked / Requires User

- External YouTube/API access decisions.
- Whether to enable live auto-trade after versioned deployment is stable.
- Final choice of remote Git provider if CI/CD is introduced.


## Blocked detail (2026-06-19, Claude loop)

- YouTube macro ingestion: scaffold ready (scripts/macro_ingest.py accepts a transcript file). AUTO ingestion BLOCKED — need to choose access method (caption API vs 3rd-party transcription vs manual paste). Use transcript-file mode meanwhile.
- VPS deploy verification (Task 2): scripts ready (deploy/verify_remote.ps1 read-only PASS; deploy.ps1 REVISION stamping; remote_deploy.sh scaffold). Actual deploy + REVISION end-to-end BLOCKED on user/Codex approval.
- Task 7 (metrics expansion): touches scripts/bt_registry.py + bt_scan.py which the concurrent codex session is actively rewriting → deferred to avoid clobber; recommend codex owns metrics or coordinate.
