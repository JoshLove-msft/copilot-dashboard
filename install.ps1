# Copilot Dashboard installer.
#
# One-line install (PowerShell):
#   iwr -useb https://raw.githubusercontent.com/JoshLove-msft/copilot-dashboard/main/install.ps1 | iex
#
# Downloads the dashboard files into  $env:USERPROFILE\.copilot-dashboard
# and installs Python deps. Run with:
#   & $env:USERPROFILE\.copilot-dashboard\copilot-dash.ps1
[CmdletBinding()]
param(
    [string] $Owner      = 'JoshLove-msft',
    [string] $RepoName   = 'copilot-dashboard',
    [string] $Branch     = 'main',
    [string] $InstallDir = (Join-Path $env:USERPROFILE '.copilot-dashboard')
)

$ErrorActionPreference = 'Stop'

if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
    throw "'python' is required but was not found on PATH."
}

$base  = "https://raw.githubusercontent.com/$Owner/$RepoName/$Branch"
$files = @('dashboard.py', 'copilot-dash.ps1', 'requirements.txt', 'README.md', 'LICENSE')

Write-Host "Installing Copilot Dashboard..." -ForegroundColor Cyan
Write-Host "  source : $Owner/$RepoName@$Branch"
Write-Host "  target : $InstallDir"

if (-not (Test-Path $InstallDir)) {
    New-Item -ItemType Directory -Path $InstallDir -Force | Out-Null
}
foreach ($f in $files) {
    Write-Host "  -> $f" -ForegroundColor DarkGray
    Invoke-WebRequest -UseBasicParsing -Uri "$base/$f" -OutFile (Join-Path $InstallDir $f)
}

Write-Host "Installing Python dependencies..." -ForegroundColor DarkGray
python -m pip install --quiet --disable-pip-version-check -r (Join-Path $InstallDir 'requirements.txt')
if ($LASTEXITCODE -ne 0) { throw 'pip install failed' }
New-Item -ItemType File -Path (Join-Path $InstallDir '.deps-installed') -Force | Out-Null

Write-Host ""
Write-Host "Installed to $InstallDir" -ForegroundColor Green
Write-Host ""
Write-Host "Run with:" -ForegroundColor Cyan
Write-Host "  & `"$InstallDir\copilot-dash.ps1`""
Write-Host ""
Write-Host "Tip: add an alias to your PowerShell `$PROFILE:" -ForegroundColor DarkGray
Write-Host "  function cdash { & `"$InstallDir\copilot-dash.ps1`" @args }"
