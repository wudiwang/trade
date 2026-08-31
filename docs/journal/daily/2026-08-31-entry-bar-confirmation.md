# 2026-08-31 Entry Bar Direction Confirmation

Task completed: 收紧 macro_pullback 二买/二卖的实际入场 K 线方向确认，并发布到实盘策略。

Files changed:
- `app/engine/macro_pullback.py`
- `scripts/bt_registry.py`
- `tests/test_macro_pullback.py`
- `docs/strategies/macro_pullback/logic.md`
- `docs/strategies/macro_pullback/changelog.md`
- 本任务的设计与实施计划文档

Verification commands:
- `python -m pytest tests/test_macro_pullback.py -q`
- `python -m pytest -q`
- ZEC 精确 K 线方向确认复核
- `deploy/verify_remote.ps1`

Results:
- 基线 18 项策略测试通过。
- 新增两项测试先失败，证明旧逻辑会接受反向入场 K；实现后 20 项通过。
- ZEC 做空方向确认结果为 `False`。
- 完整回归与部署后验证结果见本次任务最终交接。

Risks:
- 新门槛会减少信号，尤其会过滤停顿后第一根发生反向收回的入场。
- 它只确认一根 K 的收盘方向，不等于更大级别已经反转。
- 不自动处理部署前已经存在的持仓。

Scope: VPS live strategy signal generation and matching local backtest registry; order sizing and risk controls unchanged.
