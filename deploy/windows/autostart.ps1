<#
.SYNOPSIS
  Start the terminal automatically when you log in to Windows (a per-user Scheduled Task).
  -Remove deletes the task.
#>
param([switch]$Remove)
. "$PSScriptRoot\common.ps1"
$name = 'IORT Terminal'
if ($Remove) {
    Unregister-ScheduledTask -TaskName $name -Confirm:$false -ErrorAction SilentlyContinue
    Write-Ok "Removed the '$name' logon task"
    exit 0
}
$start = Join-Path $PSScriptRoot 'start.ps1'
$action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$start`" -NoBrowser"
$trigger = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
$trigger.Delay = 'PT1M'  # give Docker Desktop a minute to come up after logon
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)
Register-ScheduledTask -TaskName $name -Action $action -Trigger $trigger -Settings $settings -Description 'Starts the IORT options terminal at logon' -Force | Out-Null
Write-Ok "The terminal now starts by itself one minute after you log in ('$name' in Task Scheduler)"
