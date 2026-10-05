<#
.SYNOPSIS
  One-time setup of the IORT terminal on a Windows 10/11 PC with Docker Desktop.

.DESCRIPTION
  1. Checks Windows, WSL 2 and Docker Desktop (offers to install Docker Desktop with winget).
  2. Creates .env from .env.example with a random database password, operator token and audit key.
  3. Asks for your Kotak Neo login (hidden input for MPIN and TOTP secret), checks the format and
     writes it to section 1 of .env. Only your Windows user can read the file afterwards.
  4. Resyncs the Windows clock (TOTP codes depend on it) and, if asked, keeps the PC awake on AC power.
  5. Builds and starts the terminal, runs the diagnostics and opens http://localhost:8000.

  Order routing stays LOCKED (LIVE_TRADING=false): the terminal shows live data and refuses every order.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File deploy\windows\install.ps1
.EXAMPLE
  powershell -ExecutionPolicy Bypass -File deploy\windows\install.ps1 -SkipCredentials -KeepAwake
#>
param(
    [switch]$SkipCredentials,   # keep whatever Kotak login is already in .env
    [switch]$NoStart,           # prepare .env only; do not build or start
    [switch]$KeepAwake          # never sleep while on AC power (the feed stops when the PC sleeps)
)
. "$PSScriptRoot\common.ps1"

Write-Host 'IORT terminal setup for Windows' -ForegroundColor White

# ------------------------------------------------------------------ 1. Windows, WSL 2, Docker
Write-Step 'Checking Windows, WSL 2 and Docker Desktop'
if ($env:OS -ne 'Windows_NT') { Write-Note 'Not running on Windows: skipping Windows-only steps.' }
else {
    $build = [Environment]::OSVersion.Version.Build
    if ($build -lt 19041) { Write-Bad "Windows build $build is too old for WSL 2 (needs 19041+, Windows 10 2004 or newer)."; exit 1 }
    Write-Ok "Windows build $build"
    $wsl = Get-Command wsl.exe -ErrorAction SilentlyContinue
    if ($wsl) { Write-Ok 'WSL is available' } else { Write-Note 'WSL not found. Docker Desktop installs it; or run: wsl --install (as administrator), then restart.' }
}
if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    Write-Bad 'Docker Desktop is not installed.'
    $winget = Get-Command winget -ErrorAction SilentlyContinue
    if ($winget -and (Read-Host 'Install Docker Desktop now with winget? (y/n)') -eq 'y') {
        & winget install -e --id Docker.DockerDesktop --accept-package-agreements --accept-source-agreements
        Write-Note 'Docker Desktop installed. Restart Windows, start Docker Desktop once, then run this script again.'
    } else {
        Write-Note 'Install it from https://www.docker.com/products/docker-desktop/ then run this script again.'
    }
    exit 1
}
if (-not (Test-DockerReady)) {
    Write-Bad 'Docker is installed but not running. Start Docker Desktop, wait until it says "Engine running", then run this script again.'
    exit 1
}
Write-Ok 'Docker Desktop is running'

# ------------------------------------------------------------------ 2. .env with generated secrets
Write-Step 'Preparing .env'
if (-not (Test-Path -LiteralPath $script:EnvPath)) {
    Copy-Item -LiteralPath $script:EnvExample -Destination $script:EnvPath
    Write-Ok 'Created .env from .env.example'
}
$lines = Read-EnvLines $script:EnvPath
$pg = Get-EnvValue $lines 'POSTGRES_PASSWORD'
# Postgres keeps the password it was created with: never change it once the database volume exists.
$volumes = @(& docker volume ls --format '{{.Name}}' 2>$null) | Where-Object { $_ -like '*_pgdata' }
if ($volumes -and $pg) {
    Write-Ok 'Keeping the existing database password (the database already exists)'
} elseif (-not $pg -or $pg -eq 'CHANGE_ME') {
    $pg = New-RandomToken 24
    Set-EnvValue $lines 'POSTGRES_PASSWORD' $pg
    Set-EnvValue $lines 'DATABASE_URL' "postgresql+psycopg://iort:$pg@postgres:5432/iort"
    Write-Ok 'Generated a database password'
}
foreach ($k in 'OPERATOR_API_TOKEN', 'AUDIT_HMAC_KEY') {
    if (-not (Get-EnvValue $lines $k)) { Set-EnvValue $lines $k (New-RandomToken 32); Write-Ok "Generated $k" }
}
if ((Get-EnvValue $lines 'LIVE_TRADING') -ne 'false') { Write-Note 'LIVE_TRADING is not false in .env: order routing is LIVE. Check this is intended.' }

# ------------------------------------------------------------------ 3. Kotak Neo login
function Read-Checked([string]$Prompt, [string]$Pattern, [string]$Help, [switch]$Secret, [string]$Current) {
    while ($true) {
        $hint = ''
        if ($Current) { $hint = ' (Enter keeps the current value)' }
        if ($Secret) { $v = ConvertFrom-Secure (Read-Host -AsSecureString "$Prompt$hint") } else { $v = Read-Host "$Prompt$hint" }
        $v = $v.Trim()
        if (-not $v -and $Current) { return $Current }
        if ($v -match $Pattern) { return $v }
        Write-Bad $Help
    }
}
if (-not $SkipCredentials) {
    Write-Step 'Kotak Neo login (stored only in .env on this PC; never paste these into chat or email)'
    Write-Host '    Use NEW values if the old ones were ever shared: regenerate the consumer key and TOTP in Kotak Neo first.'
    $cur = @{}
    foreach ($k in 'KOTAK_API_KEY', 'KOTAK_MOBILE', 'KOTAK_CLIENT_CODE', 'KOTAK_MPIN', 'KOTAK_TOTP_SECRET') { $cur[$k] = Get-EnvValue $lines $k }
    $key = Read-Checked 'Consumer key (Kotak Neo > Trade API)' '^\S{8,}$' 'The consumer key has no spaces and at least 8 characters.' -Current $cur['KOTAK_API_KEY']
    $mob = Read-Checked 'Registered mobile, e.g. +919876543210' '^\+91\d{10}$' 'Write +91 followed by the 10-digit number.' -Current $cur['KOTAK_MOBILE']
    $ucc = Read-Checked 'Client code (UCC)' '^[A-Za-z0-9]{4,12}$' 'The client code has 4 to 12 letters and digits.' -Current $cur['KOTAK_CLIENT_CODE']
    $mpin = Read-Checked 'MPIN (6 digits, hidden)' '^\d{6}$' 'The MPIN is exactly 6 digits.' -Secret -Current $cur['KOTAK_MPIN']
    $totp = Read-Checked 'TOTP secret (base32 text behind the QR code, hidden)' '^[A-Za-z2-7 ]{16,}=*$' 'Use the base32 secret: letters A-Z and digits 2-7, at least 16 characters.' -Secret -Current $cur['KOTAK_TOTP_SECRET']
    Set-EnvValue $lines 'KOTAK_API_KEY' $key
    Set-EnvValue $lines 'KOTAK_MOBILE' $mob
    Set-EnvValue $lines 'KOTAK_CLIENT_CODE' $ucc.ToUpper()
    Set-EnvValue $lines 'KOTAK_MPIN' $mpin
    Set-EnvValue $lines 'KOTAK_TOTP_SECRET' ($totp -replace '\s', '').ToUpper()
    Set-EnvValue $lines 'KOTAK_TOTP' ''
    $mpin = $null; $totp = $null
    Write-Ok 'Kotak login saved to .env'
}
Write-EnvLines $script:EnvPath $lines
Protect-EnvFile $script:EnvPath
Write-Ok '.env written (UTF-8, LF) and readable only by you'

# ------------------------------------------------------------------ 4. Clock and power
Write-Step 'Clock and power'
if ($env:OS -eq 'Windows_NT') {
    & w32tm.exe /resync 2>$null | Out-Null
    if ($LASTEXITCODE -eq 0) { Write-Ok 'Windows clock resynced' }
    else { Write-Note 'Could not resync the clock (needs administrator). Settings > Time & language > Date & time > Sync now.' }
    if ($KeepAwake) {
        & powercfg.exe /change standby-timeout-ac 0 | Out-Null
        & powercfg.exe /change hibernate-timeout-ac 0 | Out-Null
        Write-Ok 'The PC will not sleep while on AC power'
    } else { Write-Note 'If the PC sleeps, the live feed stops (it reconnects on wake). Re-run with -KeepAwake to prevent sleep on AC power.' }
}

if ($NoStart) { Write-Host ''; Write-Ok 'Setup done. Start the terminal with deploy\windows\start.ps1'; exit 0 }

# ------------------------------------------------------------------ 5. Build, start, diagnose
Write-Step 'Building and starting the terminal (first build takes a few minutes)'
Invoke-Compose up -d --build
if (-not (Wait-Terminal 240)) { Write-Bad 'The terminal did not answer on http://localhost:8000 within 4 minutes. Run deploy\windows\logs.ps1'; exit 1 }
Write-Ok 'Terminal is up'

Write-Step 'Diagnostics'
Push-Location $script:RepoRoot
try { & docker compose exec -T terminal python -m app.doctor } finally { Pop-Location }
Write-Host ''
Write-Ok "Open $script:TerminalUrl  (operator token for control buttons: OPERATOR_API_TOKEN in .env)"
Start-Process $script:TerminalUrl
