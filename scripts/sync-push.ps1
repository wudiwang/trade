<#
.SYNOPSIS
    一键同步推送：暂存 → 提交 → 拉取变基 → 推送。跑在任意 git 仓库里都行。

.DESCRIPTION
    解决「每次都要手敲一串 git 命令」的问题。默认只提交【已跟踪文件】的改动，
    不会把 output/ 、临时文件等未跟踪垃圾一起推上去（要带上请加 -All）。
    遇到变基冲突会自动中止并保持原状，绝不 force push。

    PowerShell 5.1 注意：本脚本刻意【不对 git 使用 2>$null】——在 PS5.1 里重定向
    原生程序的 stderr 会把每行包成 ErrorRecord(NativeCommandError)，令 $? 变成 $false，
    即使 git 返回 0 也会被误判为失败。因此一律用 $LASTEXITCODE 判断成败。

.PARAMETER Message
    提交信息。不给就自动生成（含改动文件数与时间）。

.PARAMETER All
    连未跟踪的新文件一起提交（相当于 git add -A）。

.PARAMETER DryRun
    只显示将要做什么，不实际提交/推送。

.EXAMPLE
    .\scripts\sync-push.ps1
    .\scripts\sync-push.ps1 -Message "feat: 新增交易原则卡片"
    .\scripts\sync-push.ps1 -All -DryRun
#>
param(
    [string]$Message = "",
    [switch]$All,
    [switch]$DryRun
)

# 不用 Stop：原生命令(git)写 stderr 时会触发 NativeCommandError，用 $LASTEXITCODE 判断
$ErrorActionPreference = "Continue"

function Say($text, $color = "Gray") { Write-Host $text -ForegroundColor $color }
function Die($text) { Say "✗ $text" "Red"; exit 1 }

# ── 0. 必须在 git 仓库里 ────────────────────────────────────────────
$null = git rev-parse --is-inside-work-tree
if ($LASTEXITCODE -ne 0) { Die "当前目录不是 git 仓库：$(Get-Location)" }

$branch = (git rev-parse --abbrev-ref HEAD).Trim()
$remote = git remote get-url origin
if ($LASTEXITCODE -ne 0) { Die "没有配置 origin 远端" }

Say ""
Say "仓库   $(Split-Path -Leaf (git rev-parse --show-toplevel))" "Cyan"
Say "分支   $branch"
Say "远端   $remote"

# HTTPS 远端在非交互环境下会卡在账号密码提示，提前点明
if ($remote -like "https://*") {
    Say "⚠ 远端是 HTTPS，推送可能要求输入账号密码。" "Yellow"
    Say "  改用 SSH 可免密：git remote set-url origin git@github.com:<用户名>/<仓库>.git" "Yellow"
}

# ── 1. 暂存 ────────────────────────────────────────────────────────
if ($All) { git add -A } else { git add -u }
if ($LASTEXITCODE -ne 0) { Die "git add 失败" }

$staged = @(git diff --cached --name-only | Where-Object { $_ })
$untracked = @(git ls-files --others --exclude-standard | Where-Object { $_ })

if ($staged.Count -gt 0) {
    Say ""
    Say "将提交 $($staged.Count) 个文件：" "Green"
    $staged | Select-Object -First 20 | ForEach-Object { Say "   $_" }
    if ($staged.Count -gt 20) { Say "   …还有 $($staged.Count - 20) 个" }
}
if ((-not $All) -and $untracked.Count -gt 0) {
    Say ""
    Say "跳过 $($untracked.Count) 个未跟踪文件（要一起提交请加 -All）：" "DarkGray"
    $untracked | Select-Object -First 8 | ForEach-Object { Say "   $_" "DarkGray" }
}

# ── 2. 有没有活要干 ────────────────────────────────────────────────
Say ""
Say "检查远端…"
git fetch origin $branch
if ($LASTEXITCODE -ne 0) { Die "git fetch 失败（网络或权限问题）" }

$ahead = git rev-list --count "origin/$branch..HEAD"
if ($LASTEXITCODE -ne 0 -or -not $ahead) { $ahead = 0 }

if ($staged.Count -eq 0 -and [int]$ahead -eq 0) {
    Say ""
    Say "✓ 没有任何改动，远端已是最新。" "Green"
    exit 0
}

if ($DryRun) {
    Say ""
    Say "[DryRun] 到此为止，未提交也未推送。（待推送提交 $ahead 个）" "Yellow"
    exit 0
}

# ── 3. 提交 ────────────────────────────────────────────────────────
if ($staged.Count -gt 0) {
    if (-not $Message) {
        $stamp = Get-Date -Format "yyyy-MM-dd HH:mm"
        $Message = "chore: 同步 $($staged.Count) 个文件改动（$stamp）"
    }
    git commit -q -m $Message
    if ($LASTEXITCODE -ne 0) { Die "提交失败" }
    Say ""
    Say "✓ 已提交：$Message" "Green"
}

# ── 4. 变基（把远端新提交垫到下面）──────────────────────────────────
Say ""
Say "拉取并变基…"
git pull --rebase --quiet origin $branch
if ($LASTEXITCODE -ne 0) {
    Say "✗ 变基冲突，正在中止，仓库回到变基前状态。" "Red"
    git rebase --abort
    Say "  请手动处理后重跑：git pull --rebase" "Yellow"
    exit 1
}

# ── 5. 推送 ────────────────────────────────────────────────────────
Say "推送到 origin/$branch…"
git push origin $branch
if ($LASTEXITCODE -ne 0) { Die "推送失败（见上方 git 输出）" }

Say ""
Say "✓ 完成。本地与 origin/$branch 已同步：$(git rev-parse --short HEAD)" "Green"
Say ""
