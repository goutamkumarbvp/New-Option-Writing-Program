<#
.SYNOPSIS
  Update to the latest code: back up the database, pull, rebuild and restart. .env is never touched.
#>
param([string]$Branch = '')
. "$PSScriptRoot\common.ps1"
& "$PSScriptRoot\backup.ps1"
Write-Step 'Pulling the latest code'
Push-Location $script:RepoRoot
try {
    if ($Branch) { & git fetch origin $Branch; & git checkout $Branch }
    & git pull --ff-only
    if ($LASTEXITCODE -ne 0) { throw 'git pull failed: resolve local changes first (git status).' }
} finally { Pop-Location }
Write-Step 'Rebuilding and restarting'
Invoke-Compose up -d --build
if (Wait-Terminal 240) { Write-Ok "Updated and running: $script:TerminalUrl" } else { Write-Bad 'Not answering after the update. Run logs.ps1.'; exit 1 }
