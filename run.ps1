# 本机启动脚本
$env:PYTHONIOENCODING = "utf-8"
Set-Location $PSScriptRoot
python -m app.main
