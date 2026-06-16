# 注册每日增量刷新本地样本的 Windows 计划任务(用户 2026-06-16)。
# 运行一次即可: powershell -ExecutionPolicy Bypass -File scripts\setup_schedule.ps1
# 删除:  schtasks /delete /tn "TradeBacktestRefresh" /f

$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot
$py   = Join-Path $repo ".venv\Scripts\python.exe"
$script = Join-Path $repo "scripts\bt_refresh.py"
$log  = Join-Path $repo ".btcache\refresh.log"

# 每天 08:30 跑(可改 /st);拉 5m/15m/1h 滚动30天全市场
$action = "`"$py`" `"$script`" --tfs 5m,15m,1h --days 30 --top 0"
$cmd = "cmd /c `"$action >> `"$log`" 2>&1`""

schtasks /create /tn "TradeBacktestRefresh" /tr $cmd /sc DAILY /st 08:30 /f
Write-Output "已注册计划任务 TradeBacktestRefresh(每天 08:30 增量刷新 5m/15m/1h, 日志 .btcache\refresh.log)"
Write-Output "立即测试一次:  schtasks /run /tn TradeBacktestRefresh"
Write-Output "查看:          schtasks /query /tn TradeBacktestRefresh"
Write-Output "删除:          schtasks /delete /tn TradeBacktestRefresh /f"
