# Internal helper invoked by the dashboard when launching a fresh `copilot`
# session in a new Windows Terminal tab.
#
# It runs the supplied copilot command in the foreground while a thread job
# polls $env:USERPROFILE\.copilot\session-state for the brand-new session.
# As soon as it sees a non-empty `summary:` in workspace.yaml, it emits an
# OSC 0 escape sequence to retitle the WT tab — running in the same process
# means [Console]::Out writes to the same terminal.
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string] $CopilotCommand
)

$ErrorActionPreference = 'Continue'

$watcher = {
    $start = Get-Date
    $deadline = $start.AddMinutes(10)
    $sessionRoot = Join-Path $env:USERPROFILE '.copilot\session-state'
    while ((Get-Date) -lt $deadline) {
        Start-Sleep -Seconds 1
        if (-not (Test-Path $sessionRoot)) { continue }
        $dirs = Get-ChildItem $sessionRoot -Directory -ErrorAction SilentlyContinue |
            Where-Object { $_.LastWriteTime -gt $start }
        foreach ($d in $dirs) {
            $ws = Join-Path $d.FullName 'workspace.yaml'
            if (-not (Test-Path $ws)) { continue }
            try { $content = Get-Content $ws -Raw -ErrorAction Stop } catch { continue }
            if ($content -match '(?m)^summary:\s*"?([^"\r\n]+?)"?\s*$') {
                $title = $matches[1].Trim()
                if ($title) {
                    # OSC 0 ; <title> BEL  -> set WT tab title
                    [Console]::Out.Write("`e]0;$title`a")
                    [Console]::Out.Flush()
                    return
                }
            }
        }
    }
}

if (Get-Command Start-ThreadJob -ErrorAction SilentlyContinue) {
    Start-ThreadJob -ScriptBlock $watcher | Out-Null
}

# Run the copilot command in the foreground so the user can interact with it.
Invoke-Expression $CopilotCommand
