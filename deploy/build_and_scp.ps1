# ECH Build & Deploy Script
# Run from the project root OR the deploy/ subfolder:
#   powershell -ExecutionPolicy Bypass -File .\deploy\build_and_scp.ps1
#
# Reads credentials from deploy\local.env (gitignored).
# Uploads tarball, then SSHes with -t so sudo can prompt for a password.

$SRC = (Resolve-Path "$PSScriptRoot\..").Path   # always project root regardless of cwd
$TAR = "$env:TEMP\ech_deploy.tar.gz"

# ── Load credentials from deploy\local.env ────────────────────────────────────
$envFile = Join-Path $PSScriptRoot "local.env"
if (Test-Path $envFile) {
    foreach ($line in Get-Content $envFile) {
        if ($line -match '^\s*#' -or $line.Trim() -eq '') { continue }
        if ($line -match '^([^=]+)=(.*)$') {
            [System.Environment]::SetEnvironmentVariable($Matches[1].Trim(), $Matches[2].Trim(), 'Process')
        }
    }
}

$SSH_HOST = if ($env:ECH_SSH_HOST) { $env:ECH_SSH_HOST } else { "192.168.6.200" }
$SSH_USER = if ($env:ECH_SSH_USER) { $env:ECH_SSH_USER } else { "mesh" }
$SSH_PASS = $env:ECH_SSH_PASS
$RHOST    = "$SSH_USER@$SSH_HOST"

# ── Read version ──────────────────────────────────────────────────────────────
$versionFile = Join-Path $SRC "VERSION"
$VERSION = if (Test-Path $versionFile) { (Get-Content $versionFile).Trim() } else { "unknown" }
Write-Host "=== ECH Deploy - v$VERSION ===" -ForegroundColor Cyan

# ── Git Bash path ─────────────────────────────────────────────────────────────
$gitBash = "C:\Program Files\Git\bin\bash.exe"
if (-not (Test-Path $gitBash)) { $gitBash = "bash" }

function To-BashPath($p) {
    $p = $p -replace '\\', '/'
    if ($p -match '^([A-Za-z]):(.*)') { $p = '/' + $Matches[1].ToLower() + $Matches[2] }
    return $p
}

$bashSrc     = To-BashPath $SRC
$bashTar     = To-BashPath $TAR
$bashInstall = To-BashPath "$SRC\deploy\install.sh"
$scpOpts     = "-o StrictHostKeyChecking=no -o BatchMode=no"
# -t allocates a pseudo-TTY so sudo can prompt for password interactively
$sshOpts     = "-t -o StrictHostKeyChecking=no"

# ── Build tarball (includes VERSION) ─────────────────────────────────────────
Write-Host "Building tarball..." -ForegroundColor Gray
& $gitBash -c "cd '$bashSrc' && tar -czf '$bashTar' ech/ config.yaml deploy/install.sh VERSION"

if (-not (Test-Path $TAR)) {
    Write-Error "Failed to create tarball. Is Git Bash installed?"
    exit 1
}

$size = [math]::Round((Get-Item $TAR).Length / 1KB, 1)
Write-Host "Tarball: $TAR ($size KB)  version: $VERSION" -ForegroundColor Green

# ── Upload & install ──────────────────────────────────────────────────────────
Write-Host "Uploading to $RHOST ..." -ForegroundColor Gray

$hasSshpass = "no"
if ($SSH_PASS) {
    $hasSshpass = & $gitBash -c "command -v sshpass >/dev/null 2>&1 && echo yes || echo no" 2>$null
}

if ($hasSshpass -eq "yes") {
    Write-Host "Using sshpass (automated)" -ForegroundColor Gray
    & $gitBash -c "sshpass -p '$SSH_PASS' scp -o StrictHostKeyChecking=no '$bashTar' '${RHOST}:/tmp/ech_deploy.tar.gz'"
    & $gitBash -c "sshpass -p '$SSH_PASS' scp -o StrictHostKeyChecking=no '$bashInstall' '${RHOST}:/tmp/install.sh'"
    Write-Host "Running install.sh on server (you may be prompted for SUDO password)..." -ForegroundColor Cyan
    & $gitBash -c "sshpass -p '$SSH_PASS' ssh $sshOpts $RHOST 'bash /tmp/install.sh'"
} else {
    if ($SSH_PASS) {
        Write-Host "sshpass not found - you will be prompted for the SSH password 3 times (scp x2, ssh x1)." -ForegroundColor Yellow
        Write-Host "  To automate: install sshpass via Git Bash: pacman -S sshpass" -ForegroundColor Gray
    } else {
        Write-Host "No password in local.env - using SSH key or interactive prompt." -ForegroundColor Gray
    }
    & $gitBash -c "scp -o StrictHostKeyChecking=no '$bashTar' '${RHOST}:/tmp/ech_deploy.tar.gz'"
    & $gitBash -c "scp -o StrictHostKeyChecking=no '$bashInstall' '${RHOST}:/tmp/install.sh'"
    Write-Host "Running install.sh on server (you may be prompted for SUDO password)..." -ForegroundColor Cyan
    & $gitBash -c "ssh $sshOpts $RHOST 'bash /tmp/install.sh'"
}

Write-Host "=== Deploy complete - v$VERSION ===" -ForegroundColor Green
