# 一键部署到 VPS（增量更新代码 + 重启服务）
# 用法: .\deploy\deploy.ps1   （SSH密钥认证: ~\.ssh\trade_vps）
$ErrorActionPreference = "Stop"
Set-Location (Split-Path $PSScriptRoot -Parent)

$VPS = "root@76.13.182.175"
$KEY = "$env:USERPROFILE\.ssh\trade_vps"
$SSHOPTS = @("-i", $KEY, "-o", "StrictHostKeyChecking=accept-new", "-o", "BatchMode=yes")

git archive --format=tar.gz -o deploy\trade.tar.gz HEAD
scp @SSHOPTS deploy\trade.tar.gz "${VPS}:/tmp/trade.tar.gz"
ssh @SSHOPTS $VPS @'
set -e
tar xzf /tmp/trade.tar.gz -C /opt/trade
cd /opt/trade && venv/bin/pip install -q -r requirements.txt
systemctl restart trade
sleep 8
systemctl is-active trade
curl -s -o /dev/null -w "web:%{http_code}\n" http://127.0.0.1:8488/
'@
Write-Host "deploy done" -ForegroundColor Green
