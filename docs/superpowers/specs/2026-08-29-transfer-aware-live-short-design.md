# 划转感知熔断与只做空实盘设计

## 目标

恢复线上自动下单，并把 2026-08-29 起的新信号限定为只做空。每笔使用 60 USDT
固定保证金、5 倍杠杆，形成约 300 USDT 名义仓位；最多同时持有 10 个仓位。

## 根因

现有熔断直接比较 `totalWalletBalance` 与固定本金的 98%。期货账户发生 `TRANSFER -400 U`
后，系统把资金划出当成交易亏损，持续阻止所有自动订单。同期交易净额约为正，故这是资金流
口径错误而非策略亏损。

## 设计

保留钱包余额熔断，但将比较值改为“划转校正余额” ：

```text
划转校正余额 = totalWalletBalance - 本金基准设定以来的 USDT TRANSFER 净额
```

划出为负数，因此会加回；划入为正数，因此会扣除。这样外部注资不会掩盖交易亏损，资金划出
也不会制造虚假亏损。基准时刻使用 `settings.risk.account_equity.updated_at`。Binance 收入接口按
`incomeType=TRANSFER` 分页查询；查询失败时停止自动下单，不绕过风控。

当校正余额低于 `risk.account_equity * (1-live.max_loss_pct/100)` 时熔断；恢复到阈值以上后记录
一次恢复事件。线上配置为：`mode=live`、`live.auto_trade=true`、`trade_direction=short`、
`risk.leverage=5`、`live.fixed_margin_u=60`、其他仓位回退值为0、`live.max_positions=10`，保留
`live.max_loss_pct=2`。

不追单、不回放今天已经过期的历史信号。仅部署恢复后的新做空信号会进入实盘；做多信号仍入库
用于研究，但不打开模拟或实盘仓位。

## 验证与回滚

单元测试覆盖划出不触发熔断、真实亏损仍触发、收入接口参数。部署前完整测试；部署后核对精确
REVISION、服务状态、HTTP、运行参数、当前校正余额和无新增错误。若任一关键检查失败，立即关闭
`live.auto_trade`，回滚到部署前提交并重启。
