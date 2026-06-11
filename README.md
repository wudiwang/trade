# 缠论分型交易系统

监控币安全部 USDT 本位永续合约（按 24h 成交额过滤），在 5m/15m 级别识别**缠论顶/底分型 + 量能放大 + 跌破收回**信号，推送 Telegram 确认后（live 模式）自动下单并挂止盈止损。Paper 模式双轨统计（RR≥5 / RR≥2.5）验证策略期望。

## 访问

| 入口 | 地址 |
|---|---|
| Web 控制台 | https://trade.overall.it.com （DNS 生效后）/ 备用 http://76.13.182.175:8488 |
| 初始账号 | admin / trade@2026 （⚠️ 登录后立即改密码） |
| Telegram | @trade_flyyy_bot（信号卡片+确认按钮；发 /status 查运行状态） |

## 信号规则（V1）

1. **底分型**（K线包含处理后）中间K低点/高点均低于两侧 → 潜在买点；顶分型对称做空
2. **量能**：分型极值K成交量 ≥ 1.5× 前20根均量（≥2.0× 标记强信号）
3. **跌破收回**：极值K跌破前低、确认K收盘收回前低上方（空头为冲高回落）
4. **趋势过滤**：1h EMA50 方向（15m聚合），只做顺势
5. **止损** = 分型极值 ∓0.3%；**止盈** = Volume Profile 最近密集成交区(HVN)
6. **盈亏比**：RR≥5 推送 TG（主信号）；RR≥2.5 仅入库统计（对照轨）
7. **仓位**：单笔风险 = 账户 × 0.5%，最多 5 仓，冷却 10 根K

所有参数可在 Web「参数配置」在线改，热生效。

## 架构

```
app/main.py            总装入口 (python -m app.main)
app/engine/core.py     主循环: 币种刷新→回填→WS→收盘评估→信号→paper结算
app/engine/binance_ws.py   合约行情流 (注意: 2026-04起必须 /market 前缀)
app/engine/chan.py     缠论: 包含处理/分型/趋势
app/engine/signals.py  信号组装与过滤链
app/engine/paper.py    模拟盘双轨结算
app/engine/trader.py   实盘下单 (市价入场+TP/SL条件单)
app/bot/telegram.py    信号卡片/两步确认/下单触发
app/web/server.py      FastAPI: 登录/信号/统计/配置/WS推送
data/trade.db          SQLite (WAL)
```

## 运维

- **VPS**: root@76.13.182.175 (吉隆坡)，`systemctl status trade`，日志 `journalctl -u trade -f`
- **部署/更新**: 本机执行 `.\deploy\deploy.ps1`（git 提交后）
- **本机调试**: `.\run.ps1`（⚠️ 与 VPS 同时跑会 TG 轮询冲突，先 `systemctl stop trade`）
- **测试**: `python tests/test_chan.py`（单元）、`tests/smoke_*.py`（链路）、`tests/replay_signals.py`（回放）

## 切换实盘 (live)

1. 币安创建 API Key：只开「合约交易」权限，**禁止提现**，IP 白名单填 `76.13.182.175`
2. 写入 VPS `/opt/trade/.env` 的 `BINANCE_API_KEY/SECRET`，`systemctl restart trade`
3. Web 配置页把 mode 改为 `live`
4. 之后 TG 卡片上点「确认买入→二次确认」即真实下单（市价入场+自动挂TP/SL）
