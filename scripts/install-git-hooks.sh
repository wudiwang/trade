#!/bin/sh
# 安装 git 钩子到本仓库。换电脑/重新 clone 后跑一次即可。
#   sh scripts/install-git-hooks.sh
# Windows 用 Git Bash 跑；Mac/Linux 直接跑。
set -e
ROOT=$(git rev-parse --show-toplevel)
SRC="$ROOT/scripts/git-hooks"
DST="$(git rev-parse --git-dir)/hooks"
[ -d "$SRC" ] || { echo "✗ 找不到 $SRC"; exit 1; }
mkdir -p "$DST"
for f in "$SRC"/*; do
    [ -f "$f" ] || continue
    n=$(basename "$f")
    cp "$f" "$DST/$n"
    chmod +x "$DST/$n"
    echo "✅ 已安装 $n"
done
echo "完成。以后 git commit 会自动推送到 GitHub。"
