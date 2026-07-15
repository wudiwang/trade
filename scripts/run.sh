#!/usr/bin/env bash
# 本地回测看图器 (Mac / Linux)。Windows 用 run.ps1。
#
# 首次:
#   python3 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt
# 之后:
#   ./scripts/run.sh                 # 只本机能开: http://127.0.0.1:8530
#   ./scripts/run.sh --lan           # 同局域网的手机/其它电脑也能开
#   BT_USER=peter BT_PASS=xxx ./scripts/run.sh --lan   # 加登录(上局域网/公网必设)
#
# 需要 .btcache/ 里有K线数据。没有就先刷:
#   python3 scripts/bt_refresh.py --tfs 5m,15m,1h --days 30 --top 0

set -euo pipefail
cd "$(dirname "$0")/.."

PORT=8530
HOST=127.0.0.1
for a in "$@"; do
  case "$a" in
    --lan)  HOST=0.0.0.0 ;;
    --port=*) PORT="${a#*=}" ;;
  esac
done

PY=python3
[ -x ".venv/bin/python" ] && PY=".venv/bin/python"

if [ ! -d .btcache ] || [ -z "$(ls -A .btcache 2>/dev/null)" ]; then
  echo "⚠ .btcache/ 是空的 —— 先刷K线数据:" >&2
  echo "   $PY scripts/bt_refresh.py --tfs 5m,15m,1h --days 30 --top 0" >&2
  exit 1
fi

if [ "$HOST" = "0.0.0.0" ] && { [ -z "${BT_USER:-}" ] || [ -z "${BT_PASS:-}" ]; }; then
  echo "⚠ --lan 对外暴露必须设账号密码(看图器有 POST 写接口):" >&2
  echo '   BT_USER=peter BT_PASS=<自己设> ./scripts/run.sh --lan' >&2
  exit 1
fi

echo "看图器: http://127.0.0.1:$PORT  (Ctrl+C 停)"
[ "$HOST" = "0.0.0.0" ] && echo "局域网: http://$(ipconfig getifaddr en0 2>/dev/null || hostname -I 2>/dev/null | awk '{print $1}'):$PORT"
exec "$PY" scripts/bt_viewer.py --port "$PORT" --host "$HOST"
