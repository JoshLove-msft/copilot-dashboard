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
    $owner      = 'JoshLove-msft'
    $repo       = 'copilot-dashboard'
    $apiBase    = "https://api.github.com/repos/$owner/$repo"
    $files      = @('dashboard.py', 'copilot-dash.ps1', 'requirements.txt', '_new-session-launcher.ps1')
    $reqBefore  = if (Test-Path (Join-Path $root 'requirements.txt')) {
        Get-FileHash (Join-Path $root 'requirements.txt') -Algorithm SHA256
    } else { $null }
    try {
        # Resolve main → commit SHA via the API (this endpoint is not behind
        # the raw CDN and is up-to-date within seconds of a push). Then fetch
        # each file pinned to that immutable SHA so we never get a stale CDN
        # response for `main`.
        $headers = @{
            'Cache-Control' = 'no-cache'
            'Pragma'        = 'no-cache'
            'User-Agent'    = 'copilot-dashboard-launcher'
            'Accept'        = 'application/vnd.github+json'
        }
        $shaResp = Invoke-RestMethod -Uri "$apiBase/commits/main" -Headers $headers -TimeoutSec 8
        $sha     = $shaResp.sha
        $rawBase = "https://raw.githubusercontent.com/$owner/$repo/$sha/src"
        foreach ($f in $files) {
            $dest = Join-Path $root $f
            $tmp  = "$dest.new"
            Invoke-WebRequest -UseBasicParsing -TimeoutSec 8 `
                -Headers $headers `
                -Uri "$rawBase/$f" -OutFile $tmp
            Move-Item -Force $tmp $dest
        }
        Write-Host " done ($($sha.Substring(0,7)))" -ForegroundColor Green
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
