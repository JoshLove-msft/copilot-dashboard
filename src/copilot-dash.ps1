# Copilot Dashboard launcher.
# Installs deps on first run, then launches the TUI.
# Pressing Enter or double-clicking a row inside the dashboard opens the
# selected session in a new terminal tab/window — the dashboard itself stays
# open so you can launch more.
[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path

$marker = Join-Path $root '.deps-installed'
if (-not (Test-Path $marker)) {
    Write-Host 'Installing dashboard dependencies (first run)…' -ForegroundColor Cyan
    python -m pip install --quiet --disable-pip-version-check -r (Join-Path $root 'requirements.txt')
    if ($LASTEXITCODE -ne 0) { throw 'pip install failed' }
    New-Item -ItemType File -Path $marker -Force | Out-Null
}

python (Join-Path $root 'dashboard.py')
exit $LASTEXITCODE
