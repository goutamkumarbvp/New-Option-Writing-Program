<#
.SYNOPSIS
  Stop the IORT terminal. Data (database, events) is kept; start.ps1 brings it back.
#>
. "$PSScriptRoot\common.ps1"
Write-Step 'Stopping the terminal'
Invoke-Compose stop
Write-Ok 'Stopped'
