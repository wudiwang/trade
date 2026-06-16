@echo off
REM 每日:增量刷数据 → 重算各策略信号JSON。计划任务 TradeBacktestRefresh 调用本文件。
cd /d "%~dp0.."
".venv\Scripts\python.exe" "scripts\bt_refresh.py" --tfs 5m,15m,1h --days 30 --top 0 >> ".btcache\refresh.log" 2>&1
".venv\Scripts\python.exe" "scripts\bt_scan.py" --days 30 >> ".btcache\refresh.log" 2>&1
