# 把本地看图器顶到公网固定地址: https://srv1587764.hstgr.cloud
#
#   手机(任何网络) → VPS Caddy(自动TLS) → 127.0.0.1:8531 → SSH反向隧道 → 本机 127.0.0.1:8530
#
# 为什么是这个架构:
#   - 数据(.btcache 2.7G)和信号(207万条, 常驻 6.5G 内存)都在本机, VPS 只有 2.2G 可用内存, 搬不过去。
#     所以 VPS 只当中继, 不存数据。
#   - 隧道只绑 VPS 的回环口(8531), 外网碰不到, 只有 Caddy 能连 → 不用改 sshd, 不用开防火墙。
#   - overall.it.com 的 DNS 在 Namecheap 不在 Cloudflare, 而 srv1587764.hstgr.cloud 是 VPS 自带主机名,
#     已解析、已有证书 → 零 DNS 工作。
#
# 用法:
#   $env:BT_USER="peter"; $env:BT_PASS="<你的密码>"
#   .\scripts\bt_remote.ps1
#
# ⚠ 本机关机/断网 = 手机打不开(会看到 502)。这是数据在本地的必然代价。

param(
  [string]$VpsUser    = "root",
  [string]$VpsHost    = "76.13.182.175",
  [string]$PublicUrl  = "https://srv1587764.hstgr.cloud",
  [int]$LocalPort     = 8530,
  [int]$RemotePort    = 8531,
  [int]$Days          = 30
)

$ErrorActionPreference = "Stop"
$repo = Split-Path $PSScriptRoot -Parent

# --- 拒绝无密码裸奔 ------------------------------------------------------
# 看图器有 POST /api/label 写接口, 一旦上公网, 没密码=谁都能改你的打标。
if (-not $env:BT_USER -or -not $env:BT_PASS) {
  Write-Host "拒绝启动: 上公网必须设账号密码。" -ForegroundColor Red
  Write-Host '  $env:BT_USER="peter"; $env:BT_PASS="<自己想一个>"' -ForegroundColor Yellow
  exit 1
}

$py = Join-Path $repo ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) { $py = "python" }

# --- 1. 起看图器(只绑回环, 不对局域网暴露) -------------------------------
Write-Host "启动看图器 127.0.0.1:$LocalPort (读 200万+ 信号进内存, 约20秒)..." -ForegroundColor Green
$viewer = Start-Process -FilePath $py `
  -ArgumentList "$repo\scripts\bt_viewer.py","--port","$LocalPort","--host","127.0.0.1","--days","$Days" `
  -PassThru -NoNewWindow

$auth = @{ Authorization = "Basic " + [Convert]::ToBase64String(
            [Text.Encoding]::UTF8.GetBytes("$($env:BT_USER):$($env:BT_PASS)")) }
$ready = $false
foreach ($i in 1..60) {
  Start-Sleep -Seconds 1
  try {
    $r = Invoke-WebRequest "http://127.0.0.1:$LocalPort/" -TimeoutSec 2 -UseBasicParsing -Headers $auth
    if ($r.StatusCode -eq 200) { $ready = $true; break }
  } catch { }
}
if (-not $ready) {
  Write-Host "看图器没起来, 中止。" -ForegroundColor Red
  if (-not $viewer.HasExited) { Stop-Process -Id $viewer.Id -Force }
  exit 1
}
Write-Host "看图器就绪。" -ForegroundColor DarkGray

# --- 2. SSH 反向隧道 (断线自动重连) --------------------------------------
Write-Host ""
Write-Host "  手机打开: $PublicUrl" -ForegroundColor Cyan
Write-Host "  账号: $($env:BT_USER)  密码: (你设的 BT_PASS)" -ForegroundColor Cyan
Write-Host "  Ctrl+C 停止" -ForegroundColor DarkGray
Write-Host ""

try {
  while ($true) {
    Write-Host "[$(Get-Date -Format HH:mm:ss)] 建立隧道 -> ${VpsHost}:$RemotePort" -ForegroundColor DarkGray
    & ssh -N -R "${RemotePort}:127.0.0.1:$LocalPort" `
        -o ServerAliveInterval=30 -o ServerAliveCountMax=3 `
        -o ExitOnForwardFailure=yes -o StrictHostKeyChecking=accept-new `
        "$VpsUser@$VpsHost"
    # ssh 退出 = 断线(睡眠/换网/VPS重启), 隔几秒重连
    Write-Host "[$(Get-Date -Format HH:mm:ss)] 隧道断开, 5秒后重连..." -ForegroundColor Yellow
    Start-Sleep -Seconds 5
  }
}
finally {
  if (-not $viewer.HasExited) { Stop-Process -Id $viewer.Id -Force }
  Write-Host "已停止看图器与隧道。" -ForegroundColor DarkGray
}
