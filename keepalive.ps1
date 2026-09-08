﻿<#
.SYNOPSIS
    小米便签导出保活提醒 —— 每 N 分钟蜂鸣 + 弹窗，提醒你去续 cookie。

.DESCRIPTION
    小米云登录态约 30 分钟失效，导出会静默中断。
    本脚本按固定间隔提醒你回到 cmd 窗口续 cookie，或重跑导出命令续上。

.PARAMETER Minutes
    提醒间隔（分钟），默认 25 —— 比 30 分钟留 5 分钟提前量。

.PARAMETER Times
    提醒次数，默认 6（覆盖约 2.5 小时）。

.EXAMPLE
    .\keepalive.ps1
    .\keepalive.ps1 -Minutes 25 -Times 8

.NOTES
    若提示"禁止运行脚本"，用下面这条绕过（仅对当前窗口生效）：
    powershell -ExecutionPolicy Bypass -File .\keepalive.ps1
#>

param(
    [int]$Minutes = 25,
    [int]$Times = 6
)

$ws = New-Object -ComObject Wscript.Shell
$total = $Minutes * $Times

Write-Host ""
Write-Host "  小米便签导出 · 保活提醒已启动" -ForegroundColor Green
Write-Host "  每 $Minutes 分钟提醒一次，共 $Times 次，覆盖约 $total 分钟" -ForegroundColor Gray
Write-Host "  停止：关闭本窗口，或按 Ctrl+C" -ForegroundColor DarkGray
Write-Host ""

for ($i = 1; $i -le $Times; $i++) {
    $at = (Get-Date).AddMinutes($Minutes).ToString("HH:mm")
    Write-Host "  [$(Get-Date -Format 'HH:mm:ss')] 第 $i/$Times 次提醒将在 $at 触发，等待中..." -ForegroundColor DarkGray

    Start-Sleep -Seconds ($Minutes * 60)

    # 蜂鸣两声，人不在电脑前也能听见
    [console]::Beep(880, 600)
    Start-Sleep -Milliseconds 250
    [console]::Beep(880, 600)

    Write-Host "  [$(Get-Date -Format 'HH:mm:ss')] 第 $i/$Times 次提醒已触发" -ForegroundColor Yellow

    # 弹窗 60 秒后自动关闭 —— 设成 0 会一直等待，导致后续提醒全部卡住
    $null = $ws.Popup(
        "第 $i / $Times 次保活提醒`n`n看 cmd 有没有刷出红色「导出失败 error」。`n`n如果有：重跑 npx mi-note-export`n它会自动开浏览器让你登录，登录后增量续上（不重复下载）",
        60,
        "小米便签导出 · 保活提醒",
        64
    )
}

Write-Host ""
Write-Host "  提醒次数已用完。导出若还没跑完，重新运行本脚本即可。" -ForegroundColor Cyan
Write-Host ""
