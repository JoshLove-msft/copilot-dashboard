# Copilot Dashboard launcher.
# Auto-updates dashboard.py from GitHub on every launch (best-effort, fast),
# installs deps on first run, then launches the TUI.
# Pressing Enter or double-clicking a row inside the dashboard opens the
# selected session in a new terminal tab/window — the dashboard itself stays
# open so you can launch more.
#
# Skip the auto-update with -NoUpdate (or set $env:COPILOT_DASH_NO_UPDATE=1).
[CmdletBinding()]
param(
    [switch] $NoUpdate
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path

Write-Host ''
Write-Host '  Copilot Dashboard ' -NoNewline -ForegroundColor Cyan
Write-Host 'starting…' -ForegroundColor DarkGray

# --- Auto-update from GitHub ------------------------------------------------
$skip = $NoUpdate -or ($env:COPILOT_DASH_NO_UPDATE -eq '1')
if (-not $skip) {
    Write-Host '  • checking for updates…' -NoNewline -ForegroundColor DarkGray
    $base       = 'https://raw.githubusercontent.com/JoshLove-msft/copilot-dashboard/main'
    $srcBase    = "$base/src"
    $files      = @('dashboard.py', 'copilot-dash.ps1', 'requirements.txt', '_new-session-launcher.ps1')
    $reqBefore  = if (Test-Path (Join-Path $root 'requirements.txt')) {
        Get-FileHash (Join-Path $root 'requirements.txt') -Algorithm SHA256
    } else { $null }
    try {
        foreach ($f in $files) {
            $dest = Join-Path $root $f
            $tmp  = "$dest.new"
            # Cache-buster + no-cache header: raw.githubusercontent.com is fronted
            # by a CDN that can serve stale content for several minutes after a
            # push. The ?t=<ticks> query string + header forces a fresh fetch.
            $bust = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
            Invoke-WebRequest -UseBasicParsing -TimeoutSec 8 `
                -Headers @{ 'Cache-Control' = 'no-cache'; 'Pragma' = 'no-cache' } `
                -Uri "$srcBase/$f`?t=$bust" -OutFile $tmp
            Move-Item -Force $tmp $dest
        }
        Write-Host ' done' -ForegroundColor Green
    } catch {
        Write-Host " skipped ($($_.Exception.Message))" -ForegroundColor DarkGray
    }
    # If requirements.txt changed, re-install deps.
    $reqAfter = if (Test-Path (Join-Path $root 'requirements.txt')) {
        Get-FileHash (Join-Path $root 'requirements.txt') -Algorithm SHA256
    } else { $null }
    if ($reqBefore -and $reqAfter -and $reqBefore.Hash -ne $reqAfter.Hash) {
        Remove-Item -Force (Join-Path $root '.deps-installed') -ErrorAction SilentlyContinue
    }
} else {
    Write-Host '  • update check skipped' -ForegroundColor DarkGray
}

# --- Install deps on first run (or when requirements.txt changed) ----------
$marker = Join-Path $root '.deps-installed'
if (-not (Test-Path $marker)) {
    Write-Host '  • installing dependencies (one-time)…' -NoNewline -ForegroundColor Cyan
    python -m pip install --quiet --disable-pip-version-check -r (Join-Path $root 'requirements.txt')
    if ($LASTEXITCODE -ne 0) { Write-Host ' failed' -ForegroundColor Red; throw 'pip install failed' }
    New-Item -ItemType File -Path $marker -Force | Out-Null
    Write-Host ' done' -ForegroundColor Green
}

Write-Host '  • launching dashboard…' -ForegroundColor DarkGray
python (Join-Path $root 'dashboard.py')
exit $LASTEXITCODE
