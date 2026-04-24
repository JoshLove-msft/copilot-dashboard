# Copilot Dashboard

A terminal UI for browsing your local GitHub Copilot CLI sessions and opening
any of them in a new terminal tab.

```
┌─ Copilot Dashboard ────────────────────────────────────────────────────────┐
│ Summary                          Updated   ID        Repo / Branch    CWD  │
│ Fixing build errors in Azure…    ● LIVE 2m a1b2c3d4  acme/api         …api │
│ Refactor TypeSpec emitter        ○ 18m   00a45e77   user/azure-sdk    …foo │
│ Address PR feedback              3h      01d46c39   user/typespec     …ts  │
└────────────────────────────────────────────────────────────────────────────┘
 245/312 sessions   ● 1 live   ○ 4 recent   root: C:\Users\you\.copilot\…
```

## Features

- Lists every session in `~/.copilot/session-state/`, newest first.
- Marks a session **● LIVE** when a `copilot` process is currently holding
  that session's files open, or its `events.jsonl` was touched in the last
  60 s.
- Shows the **Agent** state for each session — `▶ working` (mid-turn or
  user message awaiting reply), `… waiting` (turn finished, waiting on you),
  or `✓ done` (process exited).
- Live sessions are sorted to the top of the list, then "recent" sessions,
  then everything else (newest first within each group).
- Search/filter with `/`, refresh with `r`.
- **Enter** or **double-click** a row → if a tab is already running that
  session, **focus it**; otherwise open the session in a new terminal tab.
  Either way the dashboard stays open so you can keep launching/switching.

## Install

One line in PowerShell (Windows):

```powershell
iwr -useb https://raw.githubusercontent.com/JoshLove-msft/copilot-dashboard/main/install/install.ps1 | iex
```

That downloads the dashboard into `%USERPROFILE%\.copilot-dashboard` and
installs the Python dependencies (`textual`, `pyyaml`, `psutil`,
`uiautomation`) into your current Python environment. Requires Python 3.10+.

## Run

```powershell
& "$env:USERPROFILE\.copilot-dashboard\copilot-dash.ps1"
```

### Optional alias

Add to your PowerShell `$PROFILE`:

```powershell
function cdash { & "$env:USERPROFILE\.copilot-dashboard\copilot-dash.ps1" @args }
```

Then just run `cdash` from anywhere.

## Update

Re-run the install one-liner above — it overwrites the files in place.

## Keys

| Key             | Action                                     |
|-----------------|--------------------------------------------|
| `↑` / `↓`       | Move cursor                                |
| `Enter`         | Open selected session in a new tab         |
| Double-click    | Same as Enter                              |
| `/`             | Toggle search/filter                       |
| `Esc`           | Clear filter / close search                |
| `r`             | Reload sessions from disk                  |
| `a`             | Toggle showing sessions with empty summary |
| `l`             | Toggle live-only filter                    |
| `g`             | Toggle group-by-repo                       |
| `v`             | Open selected session folder in VSCode     |
| `q`             | Quit                                       |

## How it works

`dashboard.py` reads each session's `workspace.yaml` for metadata (id, cwd,
repository, branch, summary, timestamps).

When you press Enter (or double-click), the dashboard checks whether a
`copilot` process is currently holding that session's files open (`session.db`
under `~/.copilot/session-state/<uuid>/`). If so, it walks the process's
ancestors looking for a visible top-level window (preferring
`WindowsTerminal.exe`) and brings it to the front via Win32
`SetForegroundWindow`.

If nothing is running for that session, it spawns a new tab/window in this
priority order:

1. **Inside Windows Terminal** (`$env:WT_SESSION` is set) — adds a tab to the
   current WT window via `wt.exe -w 0 nt --title "copilot:<short_id>" -d "<cwd>" pwsh -NoExit -Command "copilot --resume=<id>"`.
2. **`wt.exe` is on PATH** — opens a tab in the most-recent WT window
   (`wt.exe nt …`).
3. **Otherwise** (e.g. VSCode integrated terminal, conhost, no WT installed)
   — spawns a fresh console window with `CREATE_NEW_CONSOLE`.

The shell is `pwsh.exe` if available, otherwise `powershell.exe`.

After launch/focus, the dashboard's status bar shows what happened (e.g.
`→ focused existing tab for a1b2c3d4` or `→ launched a1b2c3d4 in wt tab`) and
the dashboard remains focused.
