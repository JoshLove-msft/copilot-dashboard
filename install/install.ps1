# Copilot Dashboard installer.
#
# One-line install (PowerShell):
#   iwr -useb https://raw.githubusercontent.com/JoshLove-msft/copilot-dashboard/main/install/install.ps1 | iex
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

$base    = "https://raw.githubusercontent.com/$Owner/$RepoName/$Branch"
$srcBase = "$base/src"
$files   = @('dashboard.py', 'copilot-dash.ps1', 'requirements.txt', '_new-session-launcher.ps1')
$rootFiles = @('README.md', 'LICENSE')

Write-Host "Installing Copilot Dashboard..." -ForegroundColor Cyan
Write-Host "  source : $Owner/$RepoName@$Branch"
Write-Host "  target : $InstallDir"

if (-not (Test-Path $InstallDir)) {
    New-Item -ItemType Directory -Path $InstallDir -Force | Out-Null
}
foreach ($f in $files) {
    Write-Host "  -> $f" -ForegroundColor DarkGray
    Invoke-WebRequest -UseBasicParsing -Uri "$srcBase/$f" -OutFile (Join-Path $InstallDir $f)
}
foreach ($f in $rootFiles) {
    Write-Host "  -> $f" -ForegroundColor DarkGray
    Invoke-WebRequest -UseBasicParsing -Uri "$base/$f" -OutFile (Join-Path $InstallDir $f)
}

Write-Host "Installing Python dependencies..." -ForegroundColor DarkGray
python -m pip install --quiet --disable-pip-version-check -r (Join-Path $InstallDir 'requirements.txt')
if ($LASTEXITCODE -ne 0) { throw 'pip install failed' }
New-Item -ItemType File -Path (Join-Path $InstallDir '.deps-installed') -Force | Out-Null

# --- Create a `cdash` launcher on PATH so users can run the dashboard by name ---
$binDir = Join-Path $InstallDir 'bin'
if (-not (Test-Path $binDir)) { New-Item -ItemType Directory -Path $binDir -Force | Out-Null }

$dashScript = Join-Path $InstallDir 'copilot-dash.ps1'

# .cmd shim — works from cmd.exe, the Win+R Run dialog, and other shells.
$cmdShimPath = Join-Path $binDir 'cdash.cmd'
$cmdShim = @"
@echo off
where pwsh >nul 2>nul
if %ERRORLEVEL%==0 (
    pwsh -NoLogo -NoProfile -ExecutionPolicy Bypass -File "$dashScript" %*
) else (
    powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "$dashScript" %*
)
"@
Set-Content -Path $cmdShimPath -Value $cmdShim -Encoding ASCII

# .ps1 shim — handy when invoked from a PowerShell session (cdash.ps1 -> dashboard).
$ps1ShimPath = Join-Path $binDir 'cdash.ps1'
$ps1Shim = "& `"$dashScript`" @args"
Set-Content -Path $ps1ShimPath -Value $ps1Shim -Encoding UTF8

# Add the bin dir to the user PATH (persistent + current session) if not present.
$userPath = [Environment]::GetEnvironmentVariable('Path', 'User')
if (-not $userPath) { $userPath = '' }
$pathParts = $userPath -split ';' | Where-Object { $_ -ne '' }
$alreadyOnUserPath = $pathParts | Where-Object { $_.TrimEnd('\') -ieq $binDir.TrimEnd('\') }
if (-not $alreadyOnUserPath) {
    $newUserPath = if ($userPath) { "$userPath;$binDir" } else { $binDir }
    [Environment]::SetEnvironmentVariable('Path', $newUserPath, 'User')
    Write-Host "Added $binDir to your user PATH." -ForegroundColor DarkGray
}
# Make `cdash` available in this session immediately, too.
if (-not (($env:Path -split ';') | Where-Object { $_.TrimEnd('\') -ieq $binDir.TrimEnd('\') })) {
    $env:Path = "$env:Path;$binDir"
}

Write-Host ""
Write-Host "Installed to $InstallDir" -ForegroundColor Green
Write-Host ""
Write-Host "Run from any new terminal with:" -ForegroundColor Cyan
Write-Host "  cdash"
Write-Host ""
Write-Host "(Open a fresh shell/Run dialog so the updated PATH takes effect." -ForegroundColor DarkGray
Write-Host " It already works in this session.)" -ForegroundColor DarkGray
