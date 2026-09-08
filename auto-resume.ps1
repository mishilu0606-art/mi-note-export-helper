<#
.SYNOPSIS
    mi-note-export 自动续跑器 —— 反复重跑同一条命令，直到待同步为零。

.DESCRIPTION
    背景：小米 cookie 有效期约 30 分钟，跑太久会部分成功、部分失败。
          （典型症状：第一轮跑完，几百条被误判成"云端清空僵尸条目"，
            其实是超时失效，不是数据没了。）

    对策：反复重跑 npx mi-note-export。
          每次重跑 = 重新走一遍登录 = 拿到新 cookie = 增量续跑。

    为什么能自动：npx mi-note-export 使用持久化浏览器身份
          （launchPersistentContext + browser-data 目录），
          小米认得这台设备，重跑时浏览器窗口"闪一下"就自动登录完成，
          **不需要人工点任何东西**。
          只有当这个身份也失效时，才需要人工介入（脚本会一直等待，你去点即可）。

    实测参考：约 1600 条笔记，前后登录 3 次跑完。

.PARAMETER MaxRounds
    最多重跑轮数，默认 10。

.PARAMETER WaitSec
    每轮之间的等待秒数，默认 8。给浏览器一点时间完全退出。

.PARAMETER RoundTimeoutSec
    单轮超时看门狗，默认 600 秒。某轮卡住不动（网络抖动、
    小米服务器无响应）时，超时强制结束该轮、进入下一轮。
    卡住那轮数据没拉全没关系——下一轮会刷新 cookie 自动补。

.PARAMETER NotesDir
    笔记输出目录，用于统计新增数量，默认 "output"。

.EXAMPLE
    .\auto-resume.ps1
    .\auto-resume.ps1 -MaxRounds 15 -WaitSec 10 -RoundTimeoutSec 900

.NOTES
    若提示"禁止运行脚本"：
    powershell -ExecutionPolicy Bypass -File .\auto-resume.ps1
#>

param(
    [int]$MaxRounds = 10,
    [int]$WaitSec = 8,
    [int]$RoundTimeoutSec = 600,
    [string]$NotesDir = "output"
)

function Get-NoteCount {
    if (Test-Path $NotesDir) {
        return (Get-ChildItem -Path $NotesDir -Recurse -Filter *.md -File -ErrorAction SilentlyContinue).Count
    }
    return 0
}

Write-Host ""
Write-Host "  mi-note-export 自动续跑器" -ForegroundColor Green
Write-Host "  最多 $MaxRounds 轮，每轮间隔 $WaitSec 秒，单轮超时 $RoundTimeoutSec 秒" -ForegroundColor Gray
Write-Host "  原理：重跑 = 重新登录 = 新 cookie = 增量续跑" -ForegroundColor Gray
Write-Host "  注意：若浏览器窗口停住不闪了，说明需要你手动登录，去点一下即可" -ForegroundColor DarkGray
Write-Host ""

$startCount = Get-NoteCount
Write-Host "  起始笔记数：$startCount" -ForegroundColor Gray
Write-Host ""

$noProgress = 0

for ($i = 1; $i -le $MaxRounds; $i++) {
    $before = Get-NoteCount
    Write-Host "  === 第 $i / $MaxRounds 轮 | 开始前 $before 条 ===" -ForegroundColor Cyan

    # 看门狗：单轮超时强制结束（杀整棵进程树），防 npx 卡死挂到天亮
    $proc = Start-Process -FilePath "cmd.exe" `
        -ArgumentList "/c", "npx", "mi-note-export" `
        -NoNewWindow -PassThru
    if (-not $proc.WaitForExit($RoundTimeoutSec * 1000)) {
        Write-Host "  !! 本轮超过 $RoundTimeoutSec 秒无响应，强制结束，进入下一轮" -ForegroundColor Red
        cmd /c "taskkill /T /F /PID $($proc.Id)" | Out-Null
        Start-Sleep -Seconds 2
    }

    $after = Get-NoteCount
    $added = $after - $before

    if ($added -gt 0) {
        Write-Host "  本轮新增 $added 条，累计 $after 条" -ForegroundColor Green
        $noProgress = 0
    }
    else {
        Write-Host "  本轮无新增（$after 条）" -ForegroundColor Yellow
        $noProgress++
    }

    # 连续两轮没有新增，认为已经同步干净
    if ($noProgress -ge 2) {
        Write-Host ""
        Write-Host "  连续两轮无新增，同步完成。累计 $after 条。" -ForegroundColor Green
        break
    }

    if ($i -lt $MaxRounds) {
        Write-Host "  等待 $WaitSec 秒后开始下一轮..." -ForegroundColor DarkGray
        Start-Sleep -Seconds $WaitSec
    }
}

$final = Get-NoteCount
Write-Host ""
Write-Host "  结束。$startCount → $final 条（新增 $($final - $startCount) 条）" -ForegroundColor Cyan
if ($noProgress -lt 2) {
    Write-Host "  未确认跑完，可能还有残留失败条目。把 -MaxRounds 调大再跑一次。" -ForegroundColor Yellow
}
Write-Host ""
