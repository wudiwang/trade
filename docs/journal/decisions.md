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

## 2026-07-19 期望值回测: 三门槛在盈亏上是中性偏负, 真正的杀手是手续费

两条独立证据链都指向同一结论。

**A. 线上真实单(VPS paper, 7天169笔)**
| | 笔数 | 胜率 | 期望(扣费前) | 期望(扣费后) |
|---|---|---|---|---|
| 原策略全部 | 169 | 36.1% | +0.083R | **-0.161R** |
| 三门槛保留 | 36 | 30.6% | -0.083R | **-0.320R** |
| 被门槛滤掉 | 82 | 35.4% | +0.061R | -0.179R |

**B. 30天全市场 A/B 回测(scripts/bt_gate_ab.py, 635币)**
| | 信号 | 次/天 | 胜率 | 期望(扣费前) | 期望(扣费后) |
|---|---|---|---|---|---|
| 基准(无门槛) | 3847 | 128 | 34.4% | +0.0311R | -0.229R |
| 新版(三门槛) | 1169 | 39 | 33.8% | +0.0138R | -0.243R |

边际贡献 **-0.0141R**。门槛砍掉 70% 信号, 但期望没有改善。

**根因: 止损太近, 手续费占比失控。**
止损距离中位仅 **0.543%** 价格 → 每笔手续费吃掉 **0.257R**(平均)。
RR2 下打平只需 33.3% 胜率, 实际 33.8%(刚够), 但**扣费后打平需要 41.9%** —— 差 8 个百分点。
要把费用拖累压到 0.05R, 止损需放宽到约 1.80%(现在的 3.3 倍)。

**结论**: 这个策略的毛边缘(+0.03R)真实但极其微弱, 在当前止损宽度下必然被成本吞掉。
Peter 的人工审美筛选提高了图形质量, 但与盈利能力不相关 —— 印证蓝图第零铁律第2条
"用期望值验收, 不用审美验收"。下一步不应继续调入场条件, 而应攻**出场与止损宽度**
(蓝图 Phase 4, 也是 Peter 负偏度老问题的所在)。

**已知口径差异(待查)**: 本地回测 128 次/天 vs 线上 25 次/天。本地跑 635 币、
线上只监控 55 个高流动性币(24h成交额≥5000万U), 且线上有 cooldown 去重。
两者的 scan 实现也是分叉的(bt_registry.scan_macro_pullback 复刻了一份二买逻辑,
不调用 app/engine 的 _long_second) —— 这次已同步补上三门槛, 但两份实现长期分叉是隐患。

## 2026-07-19 「本地不复现」是假象: 重测工具两处差一根的bug

Peter 问"为什么有三种状态", 追查后发现 **53/176「本地不复现」全是工具bug造成的**,
与 VPS/本地环境差异无关。修复后 **176/176 全部复现**。

两处根因(同属一类: created_at 有 0~3 秒延迟, 53 条未对齐 bar 边界):
1. **窗口按开盘时间筛**: `open_time < created_at` 会把"恰在此刻开盘、触发之后才收盘"
   的那根也喂进去 → 入场K不再是窗口最后一根 → `_stall_entry_idx` 直接判不成立。
   改为按收盘时间筛: `open_time + dur <= created_at`。
2. **入场时刻比较少算一个周期**: `entry_time` 是入场K【开盘】时间, `created_at` 是它
   【收盘】的评估时刻, 天生差 dur, 全被判成 near。改为 `abs((ent+dur)-created_at) <= dur`。

**影响**: 此前所有基于 base_per 的统计都被 30% 的假"不复现"污染。修正后统计:

| | 修复前(基线123) | 修复后(基线176) |
|---|---|---|
| 仍触发 | 38 | **54** |
| 满意保留 | 3 | **10** |
| 满意误杀 | 0 | **3** |
| 问题已消 | 7 | **12** |
| 问题仍在 | 0 | **4** |

三门槛的真实表现比之前认为的差: 不是"零误杀零漏网", 而是**误杀3条、漏网4条**。
拒因分布: retrace_deep×72 retrace_shallow×26 leg_sticky×20 leg_too_far×4。

教训: 与 2026-07-19 盲测泄露、FVG假阳性同属一类 —— **凡是拿"事件时刻"去切K线序列的
地方, 都必须按【收盘时间】而非开盘时间比较**, 且要考虑评估时刻的秒级延迟。
