# Decisions

## 2026-06-19: One Repository, Two Entry Points

Decision: keep local research app and VPS live app in one repository, but run them as separate application entry points.

Reason: strategy logic should be shared, while research experimentation must not destabilize live trading.

## 2026-06-19: Local Is Research Lab, VPS Is Execution Room

Decision: local system handles backtesting, chart review, pattern cases, strategy design, and agent research. VPS handles stable monitoring, alerts, paper, and live execution.

Reason: live trading requires stability, traceability, and risk controls.

## 2026-06-19: Deployment Must Become Versioned

Decision: replace archive-style deployment with Git-based, commit-verifiable deployment.

Reason: prior partial deployment created uncertainty about which strategy version was online.


## 2026-07-11: FVG二买二卖(macrofvg) — 存档研究, 不上线

决定: 新建 scripts/strat_macrofvg.py 存档策略(线上 macro_pullback 结构 + FVG门槛 + 止损放一买 + 固定1:3), 只做本地回测, 不动线上代码。

30天全市场(663币/5m)回测:
- macrofvg(正式版): 969信号, 胜率25.0%, 扣费后 -0.058R (做多 +0.054R / 做空 -0.173R)
- 对照·无FVG: 4237信号, 扣费后 -0.072R
- 对照·止损放二买: 946信号, 扣费后 -0.173R
- 对照·线上原版(止损L2+RR2): 4111信号, 扣费后 -0.188R

关键发现: 真正改善来自"止损放一买"而非FVG。止损距离中位数 0.52% → 1.86%, 往返手续费占比从 0.17R 降到 0.05R (宽止损=同等风险下仓位小=手续费吃掉的R少)。线上原版毛期望 +0.084R 本来是正的, 是被手续费吃成负的。
FVG门槛砍掉77%信号, 做多端边际 +0.003R → +0.054R, 做空端反而更差。

结论: 做多端 +0.054R 但 t=0.66, 统计上与0无异, 不能算边际; 做空端 -0.173R (t=-2.22) 显著为负, 应关闭。下一步: 一年样本外复验(只做多), 未通过前不上线。
