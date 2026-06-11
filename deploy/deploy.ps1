# 一键部署到 VPS（增量更新代码 + 重启服务）
# 用法: .\deploy\deploy.ps1   （需要 Posh-SSH 模块，密码读取 .env 的 VPS_PASSWORD）
$ErrorActionPreference = "Stop"
Set-Location (Split-Path $PSScriptRoot -Parent)

$envmap = @{}
Get-Content .env | Where-Object { $_ -match "^\s*[^#].*=" } | ForEach-Object {
    $k, $v = $_ -split "=", 2; $envmap[$k.Trim()] = $v.Trim()
}
$vpsHost = $envmap["VPS_HOST"]; $vpsUser = $envmap["VPS_USER"]; $vpsPass = $envmap["VPS_PASSWORD"]

git archive --format=tar.gz -o deploy\trade.tar.gz HEAD
$pw = ConvertTo-SecureString $vpsPass -AsPlainText -Force
$cred = New-Object System.Management.Automation.PSCredential($vpsUser, $pw)

$sftp = New-SFTPSession -ComputerName $vpsHost -Credential $cred -AcceptKey
Set-SFTPItem -SessionId $sftp.SessionId -Path "$PWD\deploy\trade.tar.gz" -Destination "/tmp" -Force
Remove-SFTPSession -SessionId $sftp.SessionId | Out-Null

$s = New-SSHSession -ComputerName $vpsHost -Credential $cred -AcceptKey
$cmd = @'
set -e
tar xzf /tmp/trade.tar.gz -C /opt/trade
cd /opt/trade && venv/bin/pip install -q -r requirements.txt
systemctl restart trade
sleep 8
systemctl is-active trade
curl -s -o /dev/null -w "web:%{http_code}\n" http://127.0.0.1:8488/
'@
$r = Invoke-SSHCommand -SessionId $s.SessionId -Command $cmd -TimeOut 300
$r.Output
Remove-SSHSession -SessionId $s.SessionId | Out-Null
Write-Host "部署完成" -ForegroundColor Green
