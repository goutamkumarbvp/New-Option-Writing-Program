<#
.SYNOPSIS
  Follow the terminal's log (Ctrl+C to stop). -All shows Postgres and Redis too.
#>
param([switch]$All, [int]$Tail = 200)
. "$PSScriptRoot\common.ps1"
if ($All) { Invoke-Compose logs -f --tail $Tail } else { Invoke-Compose logs -f --tail $Tail terminal }
