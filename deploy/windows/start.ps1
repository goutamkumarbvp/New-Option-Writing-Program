<#
.SYNOPSIS
  Start the IORT terminal (Docker Desktop must be running) and open it in the browser.
#>
param([switch]$NoBrowser)
. "$PSScriptRoot\common.ps1"

if (-not (Test-DockerReady)) {
    Write-Step 'Starting Docker Desktop'
    $dd = Join-Path $env:ProgramFiles 'Docker\Docker\Docker Desktop.exe'
    if (Test-Path -LiteralPath $dd) { Start-Process $dd } else { Write-Bad 'Docker Desktop not found. Run install.ps1 first.'; exit 1 }
    $deadline = (Get-Date).AddMinutes(3)
    while (-not (Test-DockerReady)) {
        if ((Get-Date) -gt $deadline) { Write-Bad 'Docker Desktop did not start within 3 minutes.'; exit 1 }
        Start-Sleep -Seconds 5
    }
}
if (-not (Test-Path -LiteralPath $script:EnvPath)) { Write-Bad '.env not found. Run deploy\windows\install.ps1 first.'; exit 1 }

Write-Step 'Starting the terminal'
Invoke-Compose up -d
if (Wait-Terminal 180) {
    Write-Ok "Terminal is up: $script:TerminalUrl"
    if (-not $NoBrowser) { Start-Process $script:TerminalUrl }
} else {
    Write-Bad 'The terminal did not answer within 3 minutes. Run deploy\windows\logs.ps1 to see why.'
    exit 1
}
