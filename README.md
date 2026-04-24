# Copilot Dashboard

A terminal UI for browsing your local GitHub Copilot CLI sessions, jumping into
the tab that's already running one, or launching new tabs for ones that aren't.

```
┌─ Copilot Dashboard ───────────────── autorefresh in 27s ───────────────────┐
│ Summary ▼                       Agent     Turns  PR    Updated   ID       …│
│ ▼ acme/api  (12)                                                           │
│   Fixing build errors in Azure  ▶ working    14  #482  ● LIVE 2m a1b2c3d4 …│
│   Refactor TypeSpec emitter     … waiting     7         ○ 18m   00a45e77  …│
│ ▶ user/typespec  (8)                                                       │
│ ▼ (no repo)  (3)                                                           │
│   Address PR feedback           ✓ done        3  #91    3h      01d46c39  …│
└────────────────────────────────────────────────────────────────────────────┘
 245/312 sessions   ● 1 live   ○ 4 recent   autorefresh in 27s
```

## Features

- Lists every session in `~/.copilot/session-state/`, newest first.
- Marks a session **● LIVE** when a `copilot` process is currently holding
  that session's files open, or its `events.jsonl` was touched in the last
  60 s (and the agent hasn't shut down).
- **Agent** column shows what the session is doing right now:
  - `▶ working` — mid-turn, or you sent a message that's being processed
  - `… waiting` — turn finished, agent is waiting on you
  - `✓ done` — process exited / session ended
- **Turns** column counts assistant turns; **PR** column shows the linked PR
  number (single-click to open in your browser).
- Live sessions sort to the top, then "recent" sessions, then everything else.
- Click any column header to sort by it; click again to reverse.
- **Group by repo** (`g`): sessions are bucketed under `▼ <repo> (count)`
  separators. Collapse/expand a group with **→** / **←**, **Space**, or by
  clicking the separator row.
- Auto-refreshes every 30 seconds; status bar shows the countdown.
- Search/filter with `/`, manual refresh with `r`.
- **Enter** or **double-click** a row → if a tab is already running that
  session, **focus it** (Windows Terminal tab is brought forward via UI
  Automation); otherwise open the session in a new terminal tab. Either way
  the dashboard stays open so you can keep launching/switching.

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

| Key                | Action                                                 |
|--------------------|--------------------------------------------------------|
| `↑` / `↓`          | Move cursor                                            |
| `Enter`            | Focus existing tab for the session, or open a new one  |
| Double-click row   | Same as Enter                                          |
| `n`                | Start a new `copilot` session in a new tab (uses the   |
|                    | selected row's cwd if available)                       |
| `→` / `←` / `Space`| On a `▼/▶` repo header: collapse / expand the group    |
| Click repo header  | Toggle collapse/expand for that group                  |
| Click PR cell      | Open the linked pull request in your browser           |
| Click column head  | Sort by that column (click again to reverse)           |
| `/`                | Toggle search/filter                                   |
| `Esc`              | Clear filter / close search                            |
| `r`                | Reload sessions from disk now                          |
| `a`                | Toggle showing sessions with empty summary             |
| `l`                | Toggle live-only filter                                |
| `g`                | Toggle group-by-repo                                   |
| `v`                | Open selected session folder in VS Code                |
| `q`                | Quit                                                   |

## How it works

`dashboard.py` reads each session's `workspace.yaml` for metadata (id, cwd,
repository, branch, summary, timestamps) and scans `events.jsonl` for turn
count and live agent state. PR links come from `~/.copilot/session-store.db`
(opened read-only).

When you press Enter (or double-click), the dashboard checks whether a
`copilot` process is currently holding that session's files open (`session.db`
under `~/.copilot/session-state/<uuid>/`). If so:

1. It walks the process's ancestors looking for `WindowsTerminal.exe` and uses
   UI Automation to find the tab whose title matches the session summary (or
   `copilot:<short_id>`) and selects it.
2. Failing that, it brings any matching top-level window to the front via
   Win32 `SetForegroundWindow`.

If nothing is running for that session, it spawns a new tab/window in this
priority order:

1. **Inside Windows Terminal** (`$env:WT_SESSION` is set) — adds a tab to the
   current WT window via
   `wt.exe -w 0 nt --suppressApplicationTitle --title "<summary>" -d "<cwd>" pwsh -NoExit -Command "copilot --resume=<id>"`.
2. **`wt.exe` is on PATH** — opens a tab in the most-recent WT window
   (`wt.exe nt …`).
3. **Otherwise** (e.g. VS Code integrated terminal, conhost, no WT installed)
   — spawns a fresh console window with `CREATE_NEW_CONSOLE`.

The shell is `pwsh.exe` if available, otherwise `powershell.exe`.

After launch/focus, the dashboard's status bar shows what happened (e.g.
`→ focused existing tab for a1b2c3d4` or `→ launched a1b2c3d4 in wt tab`) and
the dashboard remains focused.

