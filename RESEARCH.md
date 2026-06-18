# 研究方向与待办（RESEARCH.md）

> 后续要研究/试验的策略因子与思路。来源：用户收集的交易方法论 Tier List（2026-06-14）+ 日常想法。
> 用法：每项标注【现状】【研究问题】【数据/前置】【优先级】。做完一项就把状态改为 ✅ 并在 ROADMAP/REPORT 写回测结论。
> 这不是施工图（那是 ROADMAP.md），这是「想法蓄水池 + 待办清单」。

## 0. 与现有系统的关系（避免重复造轮子）
当前主策略 `chan_bi`（缠论笔 + 底/顶分型 + 停顿K + 背驰），已有模块：
- `volume_profile.py`（成交量分布雏形）、`squeeze.py`（挤压）、MACD（用于背驰）、Wyckoff/spring（`spring.py`，现做"确认型"试验）。
下面的因子，凡能与「流动性买点（sweep & reclaim）+ 大盘第一」的核心理念叠加成**多重确认**的，优先级更高。

---

## 1. Tier List 研究清单（高 → 低优先级，源自方法论排名）

### 🔴 高优先级（与现有理念最契合）

- [ ] **Volume Profile（成交量分布）** — `vol_profile`
  - 现状：已有 `volume_profile.py` 雏形。
  - 研究：POC / VAH / VAL（价值区上下沿）、成交密集区作支撑阻力与止盈目标、naked POC（未回补的高成交价）。
  - 数据：K线+成交量（已有）。可先做日内/区间 Profile。

- [ ] **Order Flow（订单流 / CVD）** — `order_flow`
  - 现状：无。
  - 研究：累计成交量 Delta（CVD）、主动买/卖盘失衡、吸筹/派发的盘口痕迹；与背驰叠加判断力度衰竭。
  - 数据【前置】：需逐笔/aggTrades 数据（币安 `aggTrades` REST/WS），现系统只订阅 kline，要加数据层。

- [ ] **ICT 概念体系（Inner Circle Trader）** — `ict`
  - 现状：核心理念「流动性买点 = 假突破回收(sweep & reclaim)」已是系统灵魂，部分实现。
  - 研究：Order Block（订单块）、Liquidity Sweep（流动性扫荡，已部分）、Displacement（位移）、BOS/CHoCH（结构破坏/转换）。与缠论笔结构互为印证。
  - 关键词学习源：截图里的 "Casper SMC ICT Mastery"。

- [ ] **FVG（Fair Value Gap，公允价值缺口）** — `fvg`
  - 现状：无。属 ICT 子概念。
  - 研究：三K缺口识别（中间K的实体跳空区）、价格回补缺口作入场/止盈、与 Order Block 联用。

- [ ] **Wyckoff（威科夫）** — `wyckoff`
  - 现状：spring 已实现；当前正做「威科夫降为**确认型**（不单独开仓，只在与缠论重叠时双重加强）」的 7/14 天对比回测。
  - 研究：吸筹/派发示意图（accumulation/distribution schematic）、spring/upthrust、阶段划分（Phase A–E）；确认型 vs 独立信号的期望R对比（进行中）。

### 🟡 中优先级（增强现有因子）

- [ ] **VWAP** — `vwap`
  - 研究：Anchored VWAP（从重要高/低点锚定）、VWAP ± 标准差通道作动态支撑阻力。

- [ ] **Bollinger Bands（布林带）** — `bbands`
  - 现状：`squeeze.py` 已有挤压思路。
  - 研究：布林带挤压突破、%B、bandwidth 作波动率过滤器。

- [ ] **MACD 强化** — `macd`
  - 现状：已用于背驰判定。
  - 研究：多周期 MACD 共振、柱状背离量化分级。

### 🟢 低优先级（验证后再决定是否纳入）

- [ ] **Fibonacci（斐波那契）** — `fib`
  - 研究：回撤位（0.5/0.618/0.786）作入场区、扩展位（1.272/1.618）作止盈；与流动性位/缠论笔端点对齐时才采信。

- [ ] **Harmonic Patterns（谐波形态）** — `harmonic`
  - 研究：Gartley / Bat / Butterfly（基于斐波那契比率）。形态稀少、主观性强，列为最后验证项。

---

## 1.5 架构待办：两段式漏斗的「做空 screener」（高优先，见 DOCTRINE 第九节）

- [ ] **做空过度上涨币（见顶反转 screener）** — `short_screener`
  - 思路：Stage1 全市场筛「涨得最凶/见顶迹象」候选池 → Stage2 只在候选上跑卖点引擎，出信号才空。
  - **Stage1 筛选因子**：24h/7d 涨幅 top N；价格离 20/50MA 乖离 z-score 极高(抛物线)；放量滞涨(climax volume)；OI暴增 + funding 极高正值(多头拥挤)；RSI 超买 / 离 VWAP 极远。
  - **Stage2 卖点触发**：chan_bi 一卖/二卖；顶背驰；创新高失败(假突破回落=sweep&reclaim 做空镜像)；跌破启动位/微观结构；放量长上影拒绝。
  - **铁律(必须实现为硬约束)**：① 大盘 gate——BTC 强多时禁用/极小仓；② 必须等 Stage2 确认，不裸空强势；③ 硬止损(顶之上)+小仓+分批，不扛单；④ funding 既是燃料也是持仓成本。
  - 复用：`squeeze.py` 是其镜像(做多)，可参照结构写 `short_screener`；候选注入现有 `universe` / Playbook 流程。
  - 【数据】OI(`open_interest_hist`)、funding 已有；涨幅/乖离用现有 K 线即可。

## 2. 进行中的实验
- [ ] **缠论 × 威科夫 双重确认对比**：缠论信号「与威科夫重叠 vs 不重叠」的胜率/期望R，跑 7 天 + 14 天。
  （2026-06-14 起，威科夫已改为「确认型」不单独开仓 → 见 paper.py / config `confirm_only`。）

## 3. 通用研究纪律
- 每个新因子先做**离线回测**（`tests/backtest_*.py`），用期望R/胜率说话，再决定是否上 paper、再上 live。
- 新因子优先做成**确认/加分项**叠加到现有买点，而非独立开仓，降低过拟合风险。
- 回测数据实时取自币安 REST（`backtest.py: fetch_series`），币种池来自 DB `enabled_symbols()`；**在 VPS 上跑才覆盖完整 44 币池**，笔记本本地 DB 可能只有兜底币。
