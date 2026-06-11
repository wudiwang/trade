# 缠论分型交易提醒系统 — 施工计划 (PLAN.md)

> 本文件是 loop 模式的施工图纸。每轮循环：读本文件 → 找到第一个未完成任务 → 完成并自测 → 更新状态打勾 → 在「施工日志」追加一行。所有阶段完成后进入「打磨循环」：跑测试、修 bug、完善文档。

## 0. 总体架构

```
币安 USDT-M 合约 WebSocket (公开行情，无需密钥)
        │  !markPrice / kline_5m / kline_15m (全币种)
        ▼
┌─────────────────────────────────────────────┐
│  signal-engine (Python, asyncio)             │
│  - K线聚合与缓存 (内存 + SQLite 持久化)        │
│  - 缠论分型识别 (顶/底分型，含K线包含处理)      │
│  - 量能放大检测 (vs MA20成交量)               │
│  - 跌破收回买点判定                           │
│  - 止盈(密集成交区/Volume Profile) 止损(前低)  │
│  - 盈亏比过滤                                 │
└──────┬──────────────────────┬───────────────┘
       │ 信号入库               │ 推送
       ▼                      ▼
   SQLite (signals,      Telegram Bot (小飞机)
   orders, positions,    - 信号卡片 + [确认买入]/[忽略] 按钮
   kline cache)          - 确认后 → 币安下单 + 挂TP/SL
       │
       ▼
  Web 控制台 (FastAPI + 简洁前端)
  - 登录鉴权 (用户名密码 + session)
  - 实时信号流 / 持仓 / 盈亏统计 / 参数配置
  - 部署: 本机先跑 → VPS + Caddy 自动 HTTPS + 域名
```

技术栈：Python 3.11+ / FastAPI / websockets / aiogram(或python-telegram-bot) / SQLite / 原生JS+轻量图表(lightweight-charts) 前端。单进程 asyncio，全币种 5m+15m 两个级别。

## 1. 交易规则 (V1，可配置参数全部进 config)

- **底分型**：经包含处理后，三K组合中间K最低，且左右K低点抬高。顶分型对称。
- **量能条件**：分型最低那根K的成交量 ≥ `vol_multiplier` × 前20根K成交量均值。默认 **1.5**（参考值；≥2.0 标记为强信号）。
- **买点（跌破收回）**：价格跌破近端支撑/前分型低点后，收盘收回到该位之上，且伴随放量 → 底分型确认点即买入参考价。
- **止损**：前低（分型最低点）下方 `sl_buffer`（默认 0.3%）。
- **止盈**：上方最近密集成交区（用近 N 根K的 Volume Profile 高量节点 HVN）。
- **盈亏比过滤**：`min_rr` 可配，默认 5（用户要求），但同时计算并展示 RR≥2.5 的次级信号供统计对比。
- **辅助过滤**（V1 就加，开关可配）：
  - 1h EMA50 方向过滤（只做顺大级别方向的信号）
  - 流动性过滤：24h 成交额 < 5000万 USDT 的币种不监控
  - 同币种冷却：触发后 N 根K内不重复提醒
- **分仓**：单笔风险 = 账户的 `risk_pct`（默认 0.5%），由止损距离反推仓位；最大同时持仓 `max_positions`（默认 5）；总保证金占用上限 50%。
- **模式**：`paper`（默认，模拟盘记录虚拟成交验证胜率/期望）/ `live`（真实下单）。先 paper 验证模式是否赚钱，这是用户的核心目标。

## 2. 施工任务清单

### 阶段A：项目骨架与数据层
- [x] A1. git init + 项目结构 (app/, app/engine/, app/web/, app/bot/, tests/, config.yaml, requirements.txt, .gitignore, README.md)
- [x] A2. 配置模块：config.yaml 加载 + 参数校验（所有规则参数、密钥占位）
- [x] A3. SQLite 数据层：klines、symbols、signals、paper_trades、orders、positions、equity_curve 表 + DAO
- [x] A4. 币安公开 REST：拉取全部 USDT 本位永续币种列表 + 24h成交额过滤 + 历史K线回填（5m/15m 各500根）

### 阶段B：实时行情
- [x] B1. WebSocket 多路订阅管理器（分片+自动重连+心跳）⚠️ 重要：币安2026-04-23起合约行情流必须用 /market 前缀路径，旧 /stream /ws 静默无数据
- [x] B2. K线聚合器：只在K线收盘(x=true)时触发分析；实测收盘延迟0.5-1.9s ✓
- [x] B3. 数据完整性：watchdog 3分钟无消息→重启ws+REST补缺口（core.py）；整机冒烟通过(smoke_engine.py): 55币110流回填2s

### 阶段C：信号引擎
- [x] C1. K线包含关系处理（缠论预处理）
- [x] C2. 顶/底分型识别 + 单元测试（构造已知K线序列验证）
- [x] C3. 量能放大检测 + 跌破收回判定 + 单元测试
- [x] C4. Volume Profile 密集成交区计算（止盈位）+ 单元测试
- [x] C5. 信号组装：入场价/止损/止盈/RR/建议仓位，过滤器链，信号入库（入库在引擎主循环里做，见B阶段集成）
- [x] C6. 历史回放自测：30币种x2级别x500根真实K线，0异常；RR>=5主信号0个、RR>=2.5次级9个 → 印证RR5门槛极严，双轨统计很必要

### 阶段D：Telegram 提醒与确认下单
- [x] D1. Bot 框架：信号卡片消息（实测已发送到用户TG，含完整字段）
- [x] D2. inline 按钮 [确认买入] [忽略] + 二次确认 + 30分钟TTL + /status指令
- [x] D3. 币安下单模块（trader.py：市价入场+STOP_MARKET止损+TAKE_PROFIT_MARKET止盈，closePosition，精度取整）——代码就绪，实盘联调待用户提供API密钥
- [x] D4. paper 模式：信号自动开虚拟仓（双轨），引擎按收盘K结算TP/SL（同K双触按SL保守计），权益曲线落库

### 阶段E：Web 控制台
- [x] E1. FastAPI + 登录（pbkdf2密码 + HMAC session cookie），API未登录401、页面跳登录页；实测鉴权拦截生效
- [x] E2. 信号流页面：实时信号列表 + WebSocket推送toast
- [x] E3. 持仓与统计页：双轨统计卡（pnl/胜率/期望R）、交易表、权益曲线
- [x] E4. 参数配置页：13个参数在线修改并热生效（settings表覆盖，重启不丢）
- [x] E5. K线详情弹窗：蜡烛+成交量+信号箭头标注 (lightweight-charts CDN)；浏览器实测0 console错误
- 注：初始账号 admin / trade@2026（登录后可改密码 POST /api/password）；web端口 8488

### 阶段F：部署与交付
- [x] F1. 本机一键启动脚本 run.ps1
- [x] F2. 部署脚本 deploy/deploy.ps1（git archive → SFTP → 远端 pip+restart+健康检查）
- [x] F3. VPS 上线：systemd 服务 trade 已 active（/opt/trade，开机自启，Restart=always）；Caddy 已追加 trade.overall.it.com 块（validate通过，原站点308正常）；外网 http://76.13.182.175:8488 登录验证通过；⏳ HTTPS 等用户加 DNS A记录后自动签发；SSH密钥登录留到打磨轮
- [x] F4. README：架构/规则/运维/切live指引
- [x] F5. 交付自检：VPS上 引擎55币种+WS连接+TG轮询+Web外网登录 全链路通过（journalctl确认）

### 打磨循环（F 完成后每轮做）
- 跑全部测试；修发现的 bug；检查 engine 连续运行稳定性（内存/重连）；优化信号质量；完善统计页。

## 3. 等待用户提供（缺什么用占位符，不阻塞施工）
- [ ] 币安 API Key/Secret（只开"合约交易"权限，禁止提现，绑VPS IP白名单）→ config.yaml
- [x] Telegram chat_id = 1072583174 (@wudipeter)
- [x] Telegram Bot Token (@trade_flyyy_bot) → 已存 .env，sendMessage 实测通过 (2026-06-12)
- [x] VPS：root@76.13.182.175（Hostinger，吉隆坡，Ubuntu 24.04，1核/3.8G/38G 可用），密码在 .env。币安 fapi 实测连通(200)。已有服务：Caddy(80/443，服务 overall.it.com 静态站 /var/www/51-prototypes + 3100端口Next.js)、hermes agent(无端口冲突)。Python 3.12 可用，无 docker → 用 systemd + venv 部署，不用 Docker
- [x] 域名 overall.it.com → 76.13.182.175 已解析。根域名已被原型站占用，交易系统用子域名 **trade.overall.it.com**（等用户加 A 记录；没加好之前可先用 IP:端口 访问）。部署时在现有 /etc/caddy/Caddyfile 追加站点块，不得改动已有站点配置
- [ ] 账户规模 / risk_pct / 杠杆上限确认

## 4. 施工日志
（每轮循环追加：日期 | 完成项 | 备注/遗留问题）
- 2026-06-12 R1 | A1-A4 完成并冒烟通过 | 实测527个USDT永续，50M过滤后55个；Windows控制台需 PYTHONIOENCODING=utf-8（run.ps1 里要加）；smoke: tests/smoke_data_layer.py
- 2026-06-12 R1 | C1-C6 完成 | 8个单元测试全过(tests/test_chan.py)；回放(tests/replay_signals.py)0异常；关键数据：5天30币2级别 RR>=5信号0个/RR>=2.5信号9个，明早需向用户汇报门槛建议。下一轮：阶段B WebSocket实时行情
- 2026-06-12 R2 | B1-B3 完成 + paper.py + core.py | 排障：fstream无数据→币安2026-04-23端点迁移，行情流需 /market 前缀（本地和VPS都验证过）。整机冒烟OK。下一轮：D Telegram → E Web
- 2026-06-12 R3 | D+E 完成 | TG测试卡片已发用户；整机本地运行中(预览面板托管,端口8488)；web登录/鉴权/信号表/统计/设置/K线弹窗浏览器实测通过。⚠️部署VPS后必须停本地实例（TG getUpdates同token双实例会409）。下一轮：F 部署VPS
- 2026-06-12 R4 | F 全部完成，系统上线VPS | 本地实例已停；trade.service active；引擎+WS+TG+Web全链路外网验证通过。进入打磨循环：监控VPS稳定性/信号产出/修bug。待用户：DNS A记录(trade→76.13.182.175)、币安API密钥、改web密码
- 2026-06-12 P1 | 巡检：VPS健康(92MB内存,28min无错误,K线5.5万行)；30min无信号属正常频率 | 新增每小时维护循环(K线修剪+过期信号标记)并已部署；修复deploy.ps1密码bug(.env里VPS_PASSWORD缺结尾三个点)
- 2026-06-12 P2 | 巡检：ws实时流0s延迟,评估0.7ms,内存稳定92MB,0错误,信号0个(正常,~7h/个频率) | 新增每天08:00(UTC+8) TG统计日报并部署；DNS仍待用户配置
- 2026-06-12 P3 | 应用户要求新增因子打分系统 | 7个因子：RSI超卖/超买+1、RSI背离+2、资金费率极值+1、确认K主动盘占比+1、极值K影线拒绝+1、5m/15m共振+1、BTC大盘共振+1；ATR止损合理性硬过滤；min_score>=1才出信号；因子明细全部入库供后续胜率归因。18个单元测试全过；回放0异常。修复严重bug：K线元组加taker_buy后backfill仍用r[7]判收盘导致回填为空。注意：config里 telegram.enabled 当前为 false，TG提醒处于关闭状态
- 2026-06-12 P4 | 因子系统部署VPS完成 | web设置页新增8个因子参数(共21项)并浏览器验证0错误；真实回放：7信号带评分1-4分含1个RR5.06主信号(带RSI背离)。修复事故：预览验证时临时关闭的 telegram.enabled 被卷进提交并部署，已恢复 true 重新部署，TG轮询确认恢复。教训：临时配置改动要用环境变量而非改config文件
- 2026-06-12 P5 | 巡检全绿(94MB/0错误)；deploy.ps1已切SSH密钥认证(并行会话) | 验证期调参：factors.min_score 1→0(settings热生效,只记录不拦截)，加快paper样本积累和因子归因；TG推送仍只限RR≥5主信号不受影响
