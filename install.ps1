# AUGUR — one-line installer for Windows (PowerShell)
#
# Usage:
#   iwr -useb https://raw.githubusercontent.com/Mikegris/augur/main/install.ps1 | iex
#
# Tip: to read the script before running, open the URL in your browser, or:
#   iwr -useb https://raw.githubusercontent.com/Mikegris/augur/main/install.ps1
#
# Optional environment overrides:
#   $env:AUGUR_DIR     — install location (default: $HOME\augur)
#   $env:AUGUR_REPO    — source repo (default: Mikegris/augur)
#   $env:AUGUR_BRANCH  — branch (default: main)
#   $env:AUGUR_NO_LINK — set to "1" to skip the Start Menu / WindowsApps shim

$ErrorActionPreference = 'Stop'

$repo   = if ($env:AUGUR_REPO)   { $env:AUGUR_REPO }   else { 'Mikegris/augur' }
$branch = if ($env:AUGUR_BRANCH) { $env:AUGUR_BRANCH } else { 'main' }
$target = if ($env:AUGUR_DIR)    { $env:AUGUR_DIR }    else { Join-Path $HOME 'augur' }

function Say($m)  { Write-Host "▸ $m" -ForegroundColor Cyan }
function Ok($m)   { Write-Host "✓ $m" -ForegroundColor Green }
function Warn($m) { Write-Host "⚠ $m" -ForegroundColor Yellow }
function Err($m)  { Write-Host "✗ $m" -ForegroundColor Red }

# Detect whether we can actually prompt the user. PowerShell's
# `Read-Host` will throw in non-interactive contexts (CI runners, scheduled
# tasks, certain `iex`-from-pipe contexts). Guard so we degrade gracefully.
function CanPrompt {
    if (-not [Environment]::UserInteractive) { return $false }
    try {
        if ([Console]::IsInputRedirected) { return $false }
    } catch { return $false }
    return $true
}

function MaybeInstallPathShim($targetDir) {
    if ($env:AUGUR_NO_LINK -eq '1') { return }
    # Drop a Start Menu shortcut + a small batch shim on PATH so users can
    # type `augur` from any shell. We pick %USERPROFILE%\.local\bin to match
    # the macOS/Linux convention; it's created if missing.
    $binDir = Join-Path $HOME '.local\bin'
    if (-not (Test-Path $binDir)) {
        New-Item -ItemType Directory -Path $binDir -Force | Out-Null
    }
    $shim = Join-Path $binDir 'augur.bat'
    "@echo off`r`ncall `"$targetDir\run.bat`" %*" | Set-Content -Path $shim -Encoding ASCII
    Ok "Installed launcher: $shim -> $targetDir\run.bat"

    $userPath = [Environment]::GetEnvironmentVariable('Path', 'User')
    if (-not ($userPath -split ';' | Where-Object { $_ -eq $binDir })) {
        Warn "$binDir is not on your user PATH."
        Write-Host ""
        Write-Host "  Add it permanently with:"
        Write-Host "    setx PATH `"$binDir;%PATH%`""
        Write-Host "  Then close + reopen PowerShell."
        Write-Host ""
    }
}

Write-Host ""
Write-Host "╔══════════════════════════════════════════════════╗"
Write-Host "║   AUGUR — one-line installer                     ║"
Write-Host "║   Wealth Intelligence System                     ║"
Write-Host "╚══════════════════════════════════════════════════╝"
Write-Host ""

# ── Python check ────────────────────────────────────────────────────────────
$python = Get-Command python -ErrorAction SilentlyContinue
if (-not $python) {
    Err "Python not found in PATH."
    Write-Host ""
    Write-Host "Install Python 3.9 or newer via one of:"
    Write-Host "  • winget:    winget install Python.Python.3.12"
    Write-Host "  • Installer: https://www.python.org/downloads/windows/"
    Write-Host "               (CHECK ""Add python.exe to PATH"" during install)"
    Write-Host ""
    Write-Host "Then close + reopen PowerShell and re-run this installer."
    exit 1
}

$pyVer = & python -c "import sys; print('%d.%d' % sys.version_info[:2])"
$pyMaj = [int](& python -c "import sys; print(sys.version_info[0])")
$pyMin = [int](& python -c "import sys; print(sys.version_info[1])")
if ($pyMaj -lt 3 -or ($pyMaj -eq 3 -and $pyMin -lt 9)) {
    Err "Python $pyVer detected — AUGUR needs 3.9 or newer."
    exit 1
}
Ok "Python $pyVer"

# ── target directory ────────────────────────────────────────────────────────
# If it already exists and is an augur git checkout, offer to update in place.
if (Test-Path $target) {
    $isAugurCheckout = $false
    if ((Test-Path (Join-Path $target '.git')) -and (Get-Command git -ErrorAction SilentlyContinue)) {
        $originUrl = & git -C $target remote get-url origin 2>$null
        if ($originUrl -and $originUrl -match "[/:]$([regex]::Escape($repo))(\.git)?$") {
            $isAugurCheckout = $true
        }
    }

    if ($isAugurCheckout) {
        Say "Existing AUGUR install found at $target"
        $doUpdate = $true
        if (CanPrompt) {
            $reply = Read-Host "Update in place (git pull + re-run setup)? [Y/n]"
            if ($reply -match '^(n|no)$') { $doUpdate = $false }
        }
        if ($doUpdate) {
            Say "Updating $target ..."
            & git -C $target pull --ff-only origin $branch
            if ($LASTEXITCODE -ne 0) { Err "git pull failed."; exit 1 }
            Set-Location $target
            & cmd /c .\setup.bat
            if ($LASTEXITCODE -ne 0) { Err "setup.bat failed."; exit 1 }
            MaybeInstallPathShim $target
            Ok "Update complete."
            Write-Host ""
            Write-Host "To start AUGUR:"
            Write-Host "  cd $target"
            Write-Host "  .\run.bat"
            exit 0
        }
        Err "Aborted."
        exit 1
    }

    Err "$target already exists (not an augur checkout)."
    Write-Host ""
    Write-Host "Choose one:"
    Write-Host "  • cd `"$target`"; .\run.bat"
    Write-Host "      (start the existing install)"
    Write-Host "  • Move-Item `"$target`" `"${target}.backup`""
    Write-Host "      (move it out of the way, then re-run this installer)"
    Write-Host "  • `$env:AUGUR_DIR='C:\path\to\another\location'; iwr -useb https://raw.githubusercontent.com/$repo/$branch/install.ps1 | iex"
    Write-Host "      (install to a different location)"
    exit 1
}

# Make sure parent directory exists
$parent = Split-Path -Parent $target
if ($parent -and -not (Test-Path $parent)) {
    New-Item -ItemType Directory -Path $parent | Out-Null
}

# ── fetch source ────────────────────────────────────────────────────────────
$git = Get-Command git -ErrorAction SilentlyContinue
if ($git) {
    Say "Cloning https://github.com/$repo into $target ..."
    & git clone --depth 1 --branch $branch "https://github.com/$repo.git" $target
    if ($LASTEXITCODE -ne 0) { Err "git clone failed."; exit 1 }
} else {
    Say "git not found — downloading source archive instead"
    $tmpZip     = Join-Path $env:TEMP ("augur-{0}.zip" -f ([guid]::NewGuid()))
    $tmpExtract = Join-Path $env:TEMP ("augur-extract-{0}" -f ([guid]::NewGuid()))
    $url        = "https://github.com/$repo/archive/refs/heads/$branch.zip"
    try {
        Invoke-WebRequest -Uri $url -OutFile $tmpZip -UseBasicParsing
        Expand-Archive -Path $tmpZip -DestinationPath $tmpExtract -Force
        $inner = Get-ChildItem $tmpExtract | Select-Object -First 1
        if (-not $inner) { Err "Archive appears empty"; exit 1 }
        Move-Item -Path $inner.FullName -Destination $target
    } finally {
        if (Test-Path $tmpZip)     { Remove-Item -Force $tmpZip }
        if (Test-Path $tmpExtract) { Remove-Item -Recurse -Force $tmpExtract }
    }
}
Ok "Source downloaded to $target"

# ── run setup.bat ───────────────────────────────────────────────────────────
Set-Location $target
Say "Running setup.bat (creates venv + installs dependencies, ~2-5 min)..."
& cmd /c .\setup.bat
if ($LASTEXITCODE -ne 0) { Err "setup.bat failed."; exit 1 }

# ── install launcher shim ──────────────────────────────────────────────────
MaybeInstallPathShim $target

# ── offer to start ──────────────────────────────────────────────────────────
Write-Host ""
$start = $false
if (CanPrompt) {
    $reply = Read-Host "Start AUGUR now? [Y/n]"
    if (-not ($reply -match '^(n|no)$')) { $start = $true }
}

if ($start) {
    & cmd /c .\run.bat
} else {
    Ok "Install complete."
    Write-Host ""
    Write-Host "To start AUGUR later:"
    Write-Host "  cd $target"
    Write-Host "  .\run.bat"
    Write-Host ""
    Write-Host "(or just ``augur`` if ~\.local\bin is on your PATH)"
    Write-Host ""
    Write-Host "The app will open at http://localhost:5001"
}
