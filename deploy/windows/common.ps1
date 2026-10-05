# Shared helpers for the IORT Windows scripts. Dot-source it: . "$PSScriptRoot\common.ps1"
# Works in Windows PowerShell 5.1 (built into Windows 10/11) and PowerShell 7.

Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'

$script:RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$script:EnvPath = Join-Path $script:RepoRoot '.env'
$script:EnvExample = Join-Path $script:RepoRoot '.env.example'
$script:TerminalUrl = 'http://localhost:8000'

function Write-Step([string]$Text) { Write-Host ''; Write-Host "==> $Text" -ForegroundColor Cyan }
function Write-Ok([string]$Text) { Write-Host "    OK   $Text" -ForegroundColor Green }
function Write-Note([string]$Text) { Write-Host "    NOTE $Text" -ForegroundColor Yellow }
function Write-Bad([string]$Text) { Write-Host "    FAIL $Text" -ForegroundColor Red }

function Read-EnvLines([string]$Path) {
    # Returns the file as a list of lines without CR, BOM-safe.
    if (-not (Test-Path -LiteralPath $Path)) { return ,([System.Collections.Generic.List[string]]::new()) }
    $text = [System.IO.File]::ReadAllText($Path)
    if ($text.Length -gt 0 -and $text[0] -eq [char]0xFEFF) { $text = $text.Substring(1) }
    $list = [System.Collections.Generic.List[string]]::new()
    foreach ($line in ($text -split "`n")) { $list.Add($line.TrimEnd("`r")) }
    while ($list.Count -gt 0 -and $list[$list.Count - 1] -eq '') { $list.RemoveAt($list.Count - 1) }
    return ,$list
}

function Get-EnvValue($Lines, [string]$Key) {
    foreach ($line in $Lines) {
        if ($line -match ('^\s*' + [regex]::Escape($Key) + '\s*=(.*)$')) {
            $v = $Matches[1].Trim()
            if ($v.Length -ge 2 -and ($v[0] -eq "'" -or $v[0] -eq '"') -and $v[-1] -eq $v[0]) { $v = $v.Substring(1, $v.Length - 2) }
            elseif ($v.StartsWith('#')) { $v = '' }
            return $v
        }
    }
    return $null
}

function Set-EnvValue($Lines, [string]$Key, [string]$Value) {
    # Replaces KEY=... in place (keeping its position in the file) or appends it.
    $pattern = '^\s*' + [regex]::Escape($Key) + '\s*='
    for ($i = 0; $i -lt $Lines.Count; $i++) {
        if ($Lines[$i] -match $pattern) { $Lines[$i] = "$Key=$Value"; return }
    }
    $Lines.Add("$Key=$Value")
}

function Write-EnvLines([string]$Path, $Lines) {
    # UTF-8 without BOM and LF line ends: what Docker Compose and the terminal expect.
    $utf8 = New-Object System.Text.UTF8Encoding $false
    [System.IO.File]::WriteAllText($Path, (($Lines -join "`n") + "`n"), $utf8)
}

function New-RandomToken([int]$Bytes = 32) {
    $buf = New-Object byte[] $Bytes
    $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    try { $rng.GetBytes($buf) } finally { $rng.Dispose() }
    return ([Convert]::ToBase64String($buf)).TrimEnd('=').Replace('+', '-').Replace('/', '_')
}

function ConvertFrom-Secure([System.Security.SecureString]$Secure) {
    $ptr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($Secure)
    try { return [Runtime.InteropServices.Marshal]::PtrToStringBSTR($ptr) }
    finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($ptr) }
}

function Protect-EnvFile([string]$Path) {
    # Only the current Windows user may read .env (it holds the Kotak login).
    if ($env:OS -ne 'Windows_NT') { return }
    $who = "$env:USERDOMAIN\$env:USERNAME"
    & icacls.exe $Path /inheritance:r /grant:r "${who}:(R,W)" | Out-Null
    if ($LASTEXITCODE -ne 0) { Write-Note "Could not restrict .env permissions (icacls exit $LASTEXITCODE)." }
}

function Test-DockerReady {
    try { & docker info --format '{{.ServerVersion}}' 2>$null | Out-Null; return ($LASTEXITCODE -eq 0) } catch { return $false }
}

function Invoke-Compose {
    # docker compose, run from the repository root so it finds docker-compose.yml and .env.
    Push-Location $script:RepoRoot
    try {
        & docker compose @args
        if ($LASTEXITCODE -ne 0) { throw "docker compose $($args -join ' ') failed (exit $LASTEXITCODE)" }
    } finally { Pop-Location }
}

function Wait-Terminal([int]$TimeoutSec = 180) {
    $deadline = (Get-Date).AddSeconds($TimeoutSec)
    while ((Get-Date) -lt $deadline) {
        try {
            $r = Invoke-WebRequest -Uri "$script:TerminalUrl/health" -UseBasicParsing -TimeoutSec 4
            if ($r.StatusCode -eq 200) { return $true }
        } catch { Start-Sleep -Seconds 3 }
    }
    return $false
}
