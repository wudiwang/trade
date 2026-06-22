# 一键部署到 VPS（增量更新代码 + 重启服务）
# 铁律(用户 2026-06-22): 必须先把当前提交推到 GitHub, 再部署线上。
#   → 部署的版本永远 == GitHub 上的版本, 有备份、可追溯, 防止"线上有、git没有"的分叉。
# 用法: .\deploy\deploy.ps1   （SSH密钥认证: ~\.ssh\trade_vps）
$ErrorActionPreference = "Stop"
Set-Location (Split-Path $PSScriptRoot -Parent)

$VPS = "root@76.13.182.175"
$KEY = "$env:USERPROFILE\.ssh\trade_vps"
$SSHOPTS = @("-i", $KEY, "-o", "StrictHostKeyChecking=accept-new", "-o", "BatchMode=yes")

# ---- 闸门: 必须有提交、且工作区干净、且已推到 GitHub, 否则不部署 ----
$dirty = (git status --porcelain -- app config.yaml requirements.txt deploy 2>$null)
if ($dirty) { Write-Error "工作区有未提交改动(app/config/requirements/deploy), 先 commit 再部署:`n$dirty"; exit 1 }
$BRANCH = (git rev-parse --abbrev-ref HEAD).Trim()
$REV = (git rev-parse HEAD).Trim()
Write-Host "① 先推 GitHub: $BRANCH @ $REV" -ForegroundColor Cyan
git push origin $BRANCH
if ($LASTEXITCODE -ne 0) { Write-Error "推送 GitHub 失败(可能非fast-forward/有分叉), 已中止部署。先理顺分支再来。"; exit 1 }
# 二次确认远端确实有这个提交, 才继续上线
$remoteHas = (git branch -r --contains $REV 2>$null | Select-String "origin/$BRANCH")
if (-not $remoteHas) { Write-Error "远端未包含 $REV, 中止。"; exit 1 }
Write-Host "② 部署线上 (== GitHub 的 $REV)" -ForegroundColor Cyan

git archive --format=tar.gz -o deploy\trade.tar.gz HEAD
scp @SSHOPTS deploy\trade.tar.gz "${VPS}:/tmp/trade.tar.gz"
# 注意: git archive 只含已提交文件; .env / data/ / logs / .btcache 均被 gitignore,
# tar 解包不会覆盖它们 → 自动保留。
ssh @SSHOPTS $VPS @"
set -e
tar xzf /tmp/trade.tar.gz -C /opt/trade
echo $REV > /opt/trade/REVISION
cd /opt/trade && venv/bin/pip install -q -r requirements.txt
systemctl restart trade
sleep 8
systemctl is-active trade
curl -s -o /dev/null -w "web:%{http_code}\n" http://127.0.0.1:8488/
"@
Write-Host "deploy done @ $REV" -ForegroundColor Green
