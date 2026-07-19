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

## 2026-07-19 线上二买/二卖: 由人工触发反馈加三条"反弹质量"门槛

**来源**：Peter 在盲测页对 176 条线上触发标了 15 条反馈（5 ok / 10 bad），
备注集中在两类抱怨：①"反弹没力度/太粘稠/包含关系太多" ②"根本没回调过" / "反弹太高"。

**量化后发现**：ok 组与 bad 组在「回调深度 = |peak-second|/|peak-base|」上几乎完全分离——
ok 组 5 条全部落在 0.28~0.44，bad 组 10 条散在 0.19/0.26/0.27/0.45/0.50/0.54/0.55/0.59/0.71/0.76。
**注意**：Peter 口述策略时说"回调到 0.618"，但他实际认可的买点中位数是 **0.39（接近 0.382 黄金分割）**，
嘴上说的和眼睛认的不是一个数。

**改动**（`macro_pullback._leg_quality_ok`，long/short 对称）：
- `retrace_min=0.27` / `retrace_max=0.50` —— 回调深度带
- `leg_body_ratio_min=0.42` —— 反弹段 K 线平均实体/振幅，治"粘稠"
- `max_leg_pct=15.0` —— 反弹幅度上限，治"追高"

**重测结果**（176 条线上触发，基线可复现 123 条）：
信号量 123 → 38（降到 31%）；标过的样本里 **满意保留 3、满意误杀 0、问题已消 7、问题仍在 0**。
各门槛贡献：retrace_deep×47、leg_sticky×19、retrace_shallow×15、leg_too_far×4。

**⚠️ 过拟合警告**：15 个样本 / 4 个参数，且区间边界贴着 ok 组的极值。Peter 明确选择三条一起上
（我的建议是先只上回调深度）。因此代码里加了 `_rejects` 拒因记录，三条同时上线仍可事后拆分各自贡献；
每条门槛阈值设为 None/0 即可单独关闭做边际测试。**上线 VPS 前必须先攒更多样本外反馈验证。**
