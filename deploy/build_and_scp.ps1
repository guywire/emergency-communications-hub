# ECH Build & Deploy Script
# Run from the project root OR the deploy/ subfolder:
#   powershell -ExecutionPolicy Bypass -File .\deploy\build_and_scp.ps1
#
# Reads credentials from deploy\local.env (gitignored).
#
# Transport preference: PuTTY plink/pscp > sshpass+scp/ssh > interactive scp/ssh.
# plink/pscp are preferred because they ship with PuTTY (already installed on
# this machine, no extra setup) and work non-interactively out of the box —
# see the "why plink, not ssh -t" note below.
#
# Why plink+pscp instead of scp / ssh -t + sudo password prompt:
# install.sh is full of plain `sudo` calls (no -S) that expect an interactive
# terminal to prompt for a password. `ssh -t` allocates a pty for exactly that
# reason — but a real terminal must exist on THIS end to type the password
# into it. When this script is driven non-interactively (no human at a
# keyboard on this end, e.g. from an agent or CI), there's nobody to answer
# that prompt and every `sudo` call in install.sh fails/hangs one at a time.
# The fix: pipe the sudo password into `sudo -S -v` ONCE at the very start of
# the remote command, in the SAME remote shell session that then runs
# install.sh (`echo pass | sudo -S -v; bash install.sh` as one ssh/plink
# invocation, not two). That caches sudo's credential timestamp for that
# session, so every subsequent plain `sudo` call in install.sh succeeds
# without prompting again. This must stay one continuous remote command —
# sudo's timestamp cache is scoped per session/tty, so splitting the `sudo -v`
# and the install.sh run across two separate ssh/plink invocations does NOT
# work (confirmed: each new plink connection gets its own pty/session, so a
# ticket cached in one invocation is invisible to the next).

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

# ── Git Bash path (only needed to build the tarball with tar) ────────────────
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

# ── Build tarball (includes VERSION) ─────────────────────────────────────────
Write-Host "Building tarball..." -ForegroundColor Gray
& $gitBash -c "cd '$bashSrc' && tar -czf '$bashTar' ech/ config.yaml deploy/install.sh deploy/ech.service deploy/ech-sim.service VERSION"

if (-not (Test-Path $TAR)) {
    Write-Error "Failed to create tarball. Is Git Bash installed?"
    exit 1
}

$size = [math]::Round((Get-Item $TAR).Length / 1KB, 1)
Write-Host "Tarball: $TAR ($size KB)  version: $VERSION" -ForegroundColor Green

# ── Upload & install ──────────────────────────────────────────────────────────
Write-Host "Uploading to $RHOST ..." -ForegroundColor Gray

$plink = Get-Command plink.exe -ErrorAction SilentlyContinue
$pscp  = Get-Command pscp.exe  -ErrorAction SilentlyContinue
$hasSshpass = "no"
if ($SSH_PASS) {
    $hasSshpass = & $gitBash -c "command -v sshpass >/dev/null 2>&1 && echo yes || echo no" 2>$null
}

if ($plink -and $pscp -and $SSH_PASS) {
    # ── Preferred path: PuTTY plink/pscp, fully non-interactive ──────────────
    Write-Host "Using plink/pscp (automated)" -ForegroundColor Gray
    & $pscp.Source -batch -pw $SSH_PASS $TAR "${RHOST}:/tmp/ech_deploy.tar.gz"
    & $pscp.Source -batch -pw $SSH_PASS "$SRC\deploy\install.sh" "${RHOST}:/tmp/install.sh"

    # Escape any single quotes in the password for safe embedding in the
    # single-quoted remote shell string (standard '\'' shell-escaping trick).
    $escapedPass = $SSH_PASS -replace "'", "'\''"
    # One continuous remote session: cache sudo, then run install.sh in it —
    # see the "why plink+pscp" note at the top of this file for why this must
    # NOT be split into two separate plink invocations.
    $remoteCmd = "echo '$escapedPass' | sudo -S -v; bash /tmp/install.sh"
    Write-Host "Running install.sh on server ..." -ForegroundColor Cyan
    & $plink.Source -batch -pw $SSH_PASS $RHOST $remoteCmd
} elseif ($hasSshpass -eq "yes") {
    Write-Host "Using sshpass (automated)" -ForegroundColor Gray
    $sshOpts = "-t -o StrictHostKeyChecking=no"
    & $gitBash -c "sshpass -p '$SSH_PASS' scp -o StrictHostKeyChecking=no '$bashTar' '${RHOST}:/tmp/ech_deploy.tar.gz'"
    & $gitBash -c "sshpass -p '$SSH_PASS' scp -o StrictHostKeyChecking=no '$bashInstall' '${RHOST}:/tmp/install.sh'"
    Write-Host "Running install.sh on server (you may be prompted for SUDO password)..." -ForegroundColor Cyan
    & $gitBash -c "sshpass -p '$SSH_PASS' ssh $sshOpts $RHOST 'bash /tmp/install.sh'"
} else {
    Write-Host "No plink/pscp found and no sshpass — falling back to interactive prompts." -ForegroundColor Yellow
    Write-Host "  You may be prompted for the SSH password 2x (scp x2) and the SUDO password (ssh -t)." -ForegroundColor Yellow
    Write-Host "  To automate on Windows: PuTTY (plink/pscp) is usually the easiest fix — winget install PuTTY.PuTTY" -ForegroundColor Gray
    $sshOpts = "-t -o StrictHostKeyChecking=no"
    $scpOpts = "-o StrictHostKeyChecking=no -o BatchMode=no"
    & $gitBash -c "scp -o StrictHostKeyChecking=no '$bashTar' '${RHOST}:/tmp/ech_deploy.tar.gz'"
    & $gitBash -c "scp -o StrictHostKeyChecking=no '$bashInstall' '${RHOST}:/tmp/install.sh'"
    Write-Host "Running install.sh on server (you may be prompted for SUDO password)..." -ForegroundColor Cyan
    & $gitBash -c "ssh $sshOpts $RHOST 'bash /tmp/install.sh'"
}

Write-Host "=== Deploy complete - v$VERSION ===" -ForegroundColor Green
