<#
.SYNOPSIS
  Back up the terminal database (orders, fills, audit chain) to backups\iort-YYYYMMDD-HHMMSS.dump.
  Restore with: docker compose exec -T postgres pg_restore -U iort -d iort --clean /tmp/restore.dump
#>
. "$PSScriptRoot\common.ps1"
$dir = Join-Path $script:RepoRoot 'backups'
New-Item -ItemType Directory -Force -Path $dir | Out-Null
$name = 'iort-' + (Get-Date -Format 'yyyyMMdd-HHmmss') + '.dump'
Write-Step "Backing up the database to backups\$name"
# Custom-format dump made inside the container and copied out: no PowerShell text encoding touches it.
Invoke-Compose exec -T postgres pg_dump -U iort -d iort -F c -f "/tmp/$name"
Invoke-Compose cp "postgres:/tmp/$name" (Join-Path $dir $name)
Invoke-Compose exec -T postgres rm -f "/tmp/$name"
Write-Ok "Saved $(Join-Path $dir $name)"
