<#
vibe-stock 작업 스케줄러 등록 (Windows)

등록되는 작업 2개 (현재 사용자 계정, 관리자 권한 불필요):
  1. "vibe-stock 매매"      : 로그인할 때 run.bat 실행 → 프로그램이 계속 돌면서 프리마켓~애프터마켓 자동 매매.
                              휴장 중에는 다음 세션까지 스스로 대기하므로 매일 다시 켤 필요가 없다.
  2. "vibe-stock 일일 준비" : 화~토 10:30(한국시간) run.bat --collect-daily --screen
                              → 전날 미국장 일봉 수집 + 종목 선정. 애프터마켓 종료(서머타임 09:00 / 표준시 10:00 KST) 후,
                              그날 프리마켓(17:00 / 18:00 KST) 전에 끝난다.

사용법 (PowerShell):
  powershell -ExecutionPolicy Bypass -File scripts\register_tasks.ps1            # 등록 (이미 있으면 덮어씀)
  powershell -ExecutionPolicy Bypass -File scripts\register_tasks.ps1 -WhatIf    # 등록하지 않고 내용만 출력
  powershell -ExecutionPolicy Bypass -File scripts\register_tasks.ps1 -Unregister
  powershell -ExecutionPolicy Bypass -File scripts\register_tasks.ps1 -DailyTime 11:00

주의: PC 가 꺼지거나 절전에 들어가면 매매가 멈춘다. 전원 옵션에서 절전 모드를 '안 함'으로 두세요.
#>
param(
    [switch]$Unregister,
    [switch]$WhatIf,
    [string]$DailyTime = "10:30"
)
$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $PSScriptRoot
$RunBat = Join-Path $Root "run.bat"
$TraderTask = "vibe-stock 매매"
$DailyTask = "vibe-stock 일일 준비"
$User = "$env:USERDOMAIN\$env:USERNAME"

if ($Unregister) {
    foreach ($name in @($TraderTask, $DailyTask)) {
        if (Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue) {
            Unregister-ScheduledTask -TaskName $name -Confirm:$false
            Write-Host "삭제: $name"
        } else {
            Write-Host "없음: $name"
        }
    }
    exit 0
}

if (-not (Test-Path $RunBat)) { throw "run.bat 을 찾을 수 없습니다: $RunBat" }
$at = [datetime]::ParseExact($DailyTime, "HH:mm", $null)
$principal = New-ScheduledTaskPrincipal -UserId $User -LogonType Interactive -RunLevel Limited

# 1. 매매: 로그인 시 시작, 시간 제한 없음, 이미 실행 중이면 새로 시작하지 않음
$traderAction = New-ScheduledTaskAction -Execute "cmd.exe" -Argument "/c `"`"$RunBat`"`"" -WorkingDirectory $Root
$traderTrigger = New-ScheduledTaskTrigger -AtLogOn -User $User
$traderTrigger.Delay = "PT1M"   # 로그인 직후 네트워크 연결을 기다림
$traderSettings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 5)

# 2. 일일 준비: 화~토 지정 시각, 최대 6시간
$dailyAction = New-ScheduledTaskAction -Execute "cmd.exe" -Argument "/c `"`"$RunBat`" --collect-daily --screen`"" -WorkingDirectory $Root
$dailyTrigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Tuesday, Wednesday, Thursday, Friday, Saturday -At $at
$dailySettings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Hours 6) `
    -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries

$tasks = @(
    @{ Name = $TraderTask; Action = $traderAction; Trigger = $traderTrigger; Settings = $traderSettings;
       Desc = "vibe-stock 미국주식 자동매매 (로그인 시 시작, 24시간 세션 대기·매매)" },
    @{ Name = $DailyTask; Action = $dailyAction; Trigger = $dailyTrigger; Settings = $dailySettings;
       Desc = "vibe-stock 일봉 수집 + 종목 선정 (화~토 $DailyTime)" }
)

foreach ($t in $tasks) {
    Write-Host ("[{0}] {1} {2}" -f $t.Name, $t.Action.Execute, $t.Action.Arguments)
    if ($WhatIf) { continue }
    Register-ScheduledTask -TaskName $t.Name -Action $t.Action -Trigger $t.Trigger -Settings $t.Settings `
        -Principal $principal -Description $t.Desc -Force | Out-Null
    Write-Host "  → 등록 완료"
}
if ($WhatIf) { Write-Host "(-WhatIf: 등록하지 않음)"; exit 0 }
Write-Host ""
Write-Host "확인: 작업 스케줄러(taskschd.msc) → 작업 스케줄러 라이브러리에서 두 작업을 볼 수 있습니다."
Write-Host "지금 바로 시험 실행: Start-ScheduledTask -TaskName '$TraderTask'"
