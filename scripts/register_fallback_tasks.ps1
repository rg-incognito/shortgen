# Re-registers the local fallback tasks (Windows Task Scheduler) for the shortGen publish slots.
# BACKLOG P0-8d: each fallback starts ~2 h before its slot and runs
#     run_local.py <campaign> [--slot HH:MM] --if-gh-missed <GitHub cron, HH:MM UTC>
# run_local waits for a GitHub run of the same cron that is still going, then runs the pipeline,
# which exits at once if the slot already holds a Short and otherwise uploads only if the slot is
# still empty after the render (never "publish now": a passed slot rolls to the next free one).
#
# Times are IST and computed for EDT (US daylight time), when a slot is earliest in IST:
#   slot ET -> UTC (EDT = UTC-4) -> IST (UTC+5:30) -> minus 2 h.
# In EST (Nov-Mar) every slot is 1 h later in IST, so the same tasks start ~3 h before it; no
# re-run is needed when US clocks change. If a slot (src/campaigns.py publish_slots_et) or a cron
# (.github/workflows/generate_shorts.yml) changes, change it here too.
#
# Usage (from any folder; existing tasks of the same name are replaced):
#   powershell -ExecutionPolicy Bypass -File scripts\register_fallback_tasks.ps1 -WhatIf   # preview
#   powershell -ExecutionPolicy Bypass -File scripts\register_fallback_tasks.ps1
[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [string]$Python = 'C:\Python314\python.exe',
    [string]$Repo   = 'C:\Users\rohit\Desktop\shortGen',
    [string]$Prefix = 'shortGen fallback '
)

$ErrorActionPreference = 'Stop'

# 2026-10-31: the Whop campaigns are retired; this PC posts two podcast Shorts a day on its own
# (GitHub Actions minutes are used up). Each run picks/downloads/transcribes/renders in the IST
# afternoon, while the laptop is in use, and uploads the Short scheduled for its US-evening slot.
#          task name          start (IST)  run_local.py arguments           slot ET (UTC EDT/EST)
$tasks = @(
    @{ Name = 'PODCASTS 1900'; At = '14:00'; Args = 'podcasts --slot 19:00' }    # 23:00 / 00:00
    @{ Name = 'PODCASTS 2200'; At = '17:30'; Args = 'podcasts --slot 22:00' }    # 02:00 / 03:00
)
# Old Whop fallback tasks that are no longer in $tasks are removed.
$retired = @('MILKXP 1845', 'MILKXP 1945', 'MM 2000', 'UNPACKED 2100', 'LADBIBLE 2230', 'LADBIBLE 2315')

if (-not (Test-Path $Python)) { throw "Python not found at $Python" }
if (-not (Test-Path (Join-Path $Repo 'run_local.py'))) { throw "run_local.py not found in $Repo" }

$settings = New-ScheduledTaskSettingsSet `
    -WakeToRun `
    -StartWhenAvailable `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Hours 4) `
    -MultipleInstances IgnoreNew
# Runs as you, only while you are signed in to Windows (same as the existing tasks; no password stored).
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited

foreach ($r in $retired) {
    $name = "$Prefix$r"
    if ((Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue) -and $PSCmdlet.ShouldProcess($name, 'unregister')) {
        Unregister-ScheduledTask -TaskName $name -Confirm:$false
        Write-Host "removed     $name"
    }
}

foreach ($t in $tasks) {
    $name = "$Prefix$($t.Name)"
    $action = New-ScheduledTaskAction -Execute $Python -Argument "-X utf8 run_local.py $($t.Args)" -WorkingDirectory $Repo
    $trigger = New-ScheduledTaskTrigger -Daily -At ([datetime]::ParseExact($t.At, 'HH:mm', $null))
    if ($PSCmdlet.ShouldProcess($name, "register daily at $($t.At) IST: run_local.py $($t.Args)")) {
        Register-ScheduledTask -TaskName $name -Action $action -Trigger $trigger -Settings $settings `
            -Principal $principal -Description "shortGen local fallback (scripts/register_fallback_tasks.ps1): run_local.py $($t.Args)" `
            -Force | Out-Null
        Write-Host ("registered  {0,-34} {1} IST  run_local.py {2}" -f $name, $t.At, $t.Args)
    }
}

Write-Host ''
Get-ScheduledTask -TaskName "$Prefix*" -ErrorAction SilentlyContinue |
    Where-Object { $_.State -ne 'Disabled' } |
    ForEach-Object {
        $i = $_ | Get-ScheduledTaskInfo
        "{0,-34} next {1}  {2}" -f $_.TaskName, $i.NextRunTime, (($_.Actions | ForEach-Object { $_.Arguments }) -join ' ')
    }
