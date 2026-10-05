<#
.SYNOPSIS
  Run the machine and Kotak login diagnostics inside the running terminal.
  -Login also performs one real Kotak login (a refused login counts toward Kotak's lockout).
#>
param([switch]$Login)
. "$PSScriptRoot\common.ps1"
$extra = @()
if ($Login) { $extra += '--login' }
Push-Location $script:RepoRoot
try { & docker compose exec -T terminal python -m app.doctor @extra; exit $LASTEXITCODE } finally { Pop-Location }
