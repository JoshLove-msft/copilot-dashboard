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

# --- Auto-update from GitHub ------------------------------------------------
$skip = $NoUpdate -or ($env:COPILOT_DASH_NO_UPDATE -eq '1')
if (-not $skip) {
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
            Invoke-WebRequest -UseBasicParsing -TimeoutSec 8 `
                -Uri "$srcBase/$f" -OutFile $tmp
            Move-Item -Force $tmp $dest
        }
    } catch {
        Write-Host "(auto-update skipped: $($_.Exception.Message))" -ForegroundColor DarkGray
    }
    # If requirements.txt changed, re-install deps.
    $reqAfter = if (Test-Path (Join-Path $root 'requirements.txt')) {
        Get-FileHash (Join-Path $root 'requirements.txt') -Algorithm SHA256
    } else { $null }
    if ($reqBefore -and $reqAfter -and $reqBefore.Hash -ne $reqAfter.Hash) {
        Remove-Item -Force (Join-Path $root '.deps-installed') -ErrorAction SilentlyContinue
    }
}

# --- Install deps on first run (or when requirements.txt changed) ----------
$marker = Join-Path $root '.deps-installed'
if (-not (Test-Path $marker)) {
    Write-Host 'Installing dashboard dependencies…' -ForegroundColor Cyan
    python -m pip install --quiet --disable-pip-version-check -r (Join-Path $root 'requirements.txt')
    if ($LASTEXITCODE -ne 0) { throw 'pip install failed' }
    New-Item -ItemType File -Path $marker -Force | Out-Null
}

python (Join-Path $root 'dashboard.py')
exit $LASTEXITCODE
