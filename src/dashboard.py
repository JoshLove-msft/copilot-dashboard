"""
Copilot Dashboard — terminal UI for browsing and jumping to Copilot CLI sessions.

Reads sessions from ~/.copilot/session-state/<uuid>/workspace.yaml and presents
them in a sortable, searchable table. Press Enter (or double-click a row) to
open the selected session in a NEW terminal tab; the dashboard stays open so
you can launch more.

Tab-spawning strategy (Windows):
  1. Inside Windows Terminal ($WT_SESSION set) → wt.exe -w 0 nt …
  2. wt.exe on PATH               → wt.exe nt …
  3. Otherwise (VSCode, conhost)  → subprocess.Popen(..., CREATE_NEW_CONSOLE)
"""

from __future__ import annotations

import os
import json
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

import yaml

try:
    import psutil
except ImportError:  # pragma: no cover
    psutil = None

from rich.style import Style
from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.widgets import DataTable, Footer, Header, Input, Static


SESSION_ROOT = Path(os.environ.get("COPILOT_CONFIG_DIR", Path.home() / ".copilot")) / "session-state"
SESSION_STORE_DB = Path(os.environ.get("COPILOT_CONFIG_DIR", Path.home() / ".copilot")) / "session-store.db"
ACTIVE_WINDOW_SECONDS = 10 * 60  # session updated within 10 min => "recent"


LIVE_WINDOW_SECONDS = 60  # events.jsonl touched within this => "live"


@dataclass
class Session:
    id: str
    cwd: str
    repository: str
    branch: str
    summary: str
    created_at: datetime | None
    updated_at: datetime | None
    mtime: float
    events_mtime: float = 0.0
    running: bool = False  # a copilot process is actively using this session
    pid: int | None = None  # pid of the running copilot process (when known)
    pr: str = ""  # PR ref like "#1234" if this session created/touched a PR
    pr_url: str = ""  # Full URL to the PR, when known
    turns: int = 0  # number of user/assistant turns in this session
    agent_state: str = ""  # "working" | "waiting" | "done" | ""

    @property
    def short_id(self) -> str:
        return self.id[:8]

    @property
    def is_recent(self) -> bool:
        if self.updated_at is None:
            return False
        age = (datetime.now(timezone.utc) - self.updated_at).total_seconds()
        return age <= ACTIVE_WINDOW_SECONDS

    @property
    def is_live(self) -> bool:
        if self.running:
            return True
        # If the events log shows a definitive shutdown/abort, trust that
        # over the mtime heuristic — otherwise we'd flag a just-exited
        # session as LIVE simply because events.jsonl was touched seconds ago.
        if self.agent_state == "done":
            return False
        if self.events_mtime <= 0:
            return False
        return (time.time() - self.events_mtime) <= LIVE_WINDOW_SECONDS

    @property
    def status(self) -> str:
        if self.is_live:
            return "● LIVE"
        if self.is_recent:
            return "○ recent"
        return ""


def _parse_dt(value) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        s = str(value).replace("Z", "+00:00")
        return datetime.fromisoformat(s)
    except Exception:
        return None


def _first_user_message(events_path: Path) -> str:
    """Return the first user.message content from events.jsonl, or ''."""
    if not events_path.exists():
        return ""
    try:
        with events_path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or '"user.message"' not in line:
                    continue
                try:
                    e = json.loads(line)
                except Exception:
                    continue
                if e.get("type") != "user.message":
                    continue
                content = (e.get("data") or {}).get("content") or ""
                return str(content).strip().replace("\r", " ").replace("\n", " ")
    except OSError:
        return ""
    return ""


def load_sessions(root: Path = SESSION_ROOT) -> list[Session]:
    if not root.exists():
        return []
    sessions: list[Session] = []
    for child in root.iterdir():
        if not child.is_dir():
            continue
        ws = child / "workspace.yaml"
        if not ws.exists():
            continue
        try:
            with ws.open("r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
        except Exception:
            continue
        try:
            mtime = ws.stat().st_mtime
        except OSError:
            mtime = 0.0
        events = child / "events.jsonl"
        try:
            events_mtime = events.stat().st_mtime if events.exists() else 0.0
        except OSError:
            events_mtime = 0.0
        summary = (str(data.get("summary") or "")).strip().replace("\r", " ").replace("\n", " ")
        if not summary:
            summary = _first_user_message(events)
        sessions.append(
            Session(
                id=str(data.get("id") or child.name),
                cwd=str(data.get("cwd") or ""),
                repository=str(data.get("repository") or ""),
                branch=str(data.get("branch") or ""),
                summary=summary,
                created_at=_parse_dt(data.get("created_at")),
                updated_at=_parse_dt(data.get("updated_at")),
                mtime=mtime,
                events_mtime=events_mtime,
            )
        )
    sessions.sort(key=lambda s: (s.updated_at or datetime.fromtimestamp(s.mtime, tz=timezone.utc)), reverse=True)
    _attach_store_data(sessions)
    return sessions


_TURNS_CACHE: dict[str, tuple[float, int, str]] = {}  # session_id → (events_mtime, turns, agent_state)


def _scan_events(events_path: Path, mtime: float) -> tuple[int, str]:
    """Return (turn_count, agent_state) for a session's events.jsonl, cached
    by mtime.

    agent_state is one of:
      - "working" : agent is mid-turn (turn_start without matching turn_end,
        or a user.message just queued for the agent)
      - "waiting" : agent finished its turn and is awaiting user input
      - "done"    : final event indicates the session shut down / aborted
      - ""        : unknown (empty file)
    """
    sid = events_path.parent.name
    cached = _TURNS_CACHE.get(sid)
    if cached and cached[0] == mtime:
        return cached[1], cached[2]
    n = 0
    # Track line index of the last occurrence of each interesting marker.
    last_turn_start = -1
    last_turn_end = -1
    last_user_msg = -1
    last_shutdown = -1
    last_abort = -1
    idx = -1
    try:
        with events_path.open("rb") as f:
            for idx, line in enumerate(f):
                if b'"user.message"' in line:
                    n += 1
                    last_user_msg = idx
                if b'"assistant.turn_start"' in line:
                    last_turn_start = idx
                elif b'"assistant.turn_end"' in line:
                    last_turn_end = idx
                if b'"session.shutdown"' in line:
                    last_shutdown = idx
                elif b'"abort"' in line:
                    last_abort = idx
    except OSError:
        if cached:
            return cached[1], cached[2]
        return 0, ""

    last_done = max(last_shutdown, last_abort)
    last_active = max(last_turn_start, last_user_msg)
    if idx < 0:
        state = ""
    elif last_done > max(last_active, last_turn_end):
        state = "done"
    elif last_turn_start > last_turn_end or last_user_msg > last_turn_end:
        # In the middle of a turn, or user sent a message and the agent
        # hasn't replied yet.
        state = "working"
    elif last_turn_end >= 0:
        state = "waiting"
    else:
        state = ""
    _TURNS_CACHE[sid] = (mtime, n, state)
    return n, state


def _count_turns(events_path: Path, mtime: float) -> int:
    """Backward-compatible turn counter (delegates to _scan_events)."""
    return _scan_events(events_path, mtime)[0]


def _attach_store_data(sessions: list[Session]) -> None:
    """Annotate sessions with PR refs + turn counts from `session-store.db`,
    plus resolve canonical PR URLs from events.jsonl when possible.
    """
    if not SESSION_STORE_DB.exists():
        return
    import sqlite3
    import re
    by_id = {s.id: s for s in sessions}
    if not by_id:
        return
    try:
        uri = f"file:{SESSION_STORE_DB.as_posix()}?mode=ro&immutable=1"
        con = sqlite3.connect(uri, uri=True, timeout=1.0)
    except sqlite3.Error:
        return
    try:
        # Turn counts + agent state come from events.jsonl (real-time). The
        # session-store.db value is a stale snapshot from checkpoints.
        for sess in sessions:
            ev = SESSION_ROOT / sess.id / "events.jsonl"
            if ev.exists() and sess.events_mtime > 0:
                sess.turns, sess.agent_state = _scan_events(ev, sess.events_mtime)
        # PR refs (highest-numbered per session).
        best: dict[str, int] = {}
        try:
            for sid, val in con.execute(
                "SELECT session_id, ref_value FROM session_refs WHERE ref_type='pr'"
            ):
                if sid not in by_id:
                    continue
                try:
                    n = int(str(val))
                except (TypeError, ValueError):
                    continue
                if n > best.get(sid, -1):
                    best[sid] = n
        except sqlite3.Error:
            pass
        for sid, n in best.items():
            sess = by_id[sid]
            sess.pr = f"#{n}"
            ev = SESSION_ROOT / sid / "events.jsonl"
            if ev.exists():
                pat = re.compile(
                    rf"https://github\.com/([^/\s\"\\]+)/([^/\s\"\\]+)/pull/{n}\b"
                )
                try:
                    with ev.open("r", encoding="utf-8", errors="replace") as f:
                        for line in f:
                            if "/pull/" not in line:
                                continue
                            m = pat.search(line)
                            if m:
                                sess.pr_url = m.group(0)
                                break
                except OSError:
                    pass
            if not sess.pr_url and sess.repository:
                sess.pr_url = f"https://github.com/{sess.repository}/pull/{n}"
    finally:
        con.close()


_SESSION_PATH_FRAG = os.path.normcase(os.path.join("session-state", ""))


def detect_live_sessions(sessions: Iterable[Session]) -> None:
    """Mark sessions whose session-state files are held open by a copilot process.

    This catches BOTH freshly-launched and `--resume`-launched copilot
    processes, because every session keeps `session.db` / `events.jsonl` open
    while it's running. Cmdline matching is used as a secondary signal so we
    can pick up sessions whose db isn't open yet (rare, very early startup).
    """
    if psutil is None:
        return
    by_id = {s.id: s for s in sessions}
    by_short = {s.id[:8]: s for s in sessions}
    # Reset before re-detection so closed sessions clear their pid.
    for s in sessions:
        s.running = False
        s.pid = None

    def _record(target: Session, pid: int) -> None:
        if not target.running:
            target.running = True
            target.pid = pid

    for proc in psutil.process_iter(["name", "cmdline", "pid"]):
        try:
            name = (proc.info.get("name") or "").lower()
            cmdline = proc.info.get("cmdline") or []
            joined_head = " ".join((cmdline or [])[:6]).lower()
            if "copilot" not in name and "copilot" not in joined_head:
                continue

            # Primary signal: open file handles into session-state/<uuid>/.
            try:
                for f in proc.open_files():
                    path_norm = os.path.normcase(f.path)
                    idx = path_norm.find(_SESSION_PATH_FRAG)
                    if idx < 0:
                        continue
                    rest = path_norm[idx + len(_SESSION_PATH_FRAG):]
                    sep = rest.find(os.sep)
                    uuid = rest if sep < 0 else rest[:sep]
                    target = by_id.get(uuid)
                    if target is not None:
                        _record(target, proc.info["pid"])
            except (psutil.AccessDenied, psutil.NoSuchProcess):
                pass

            # Secondary signal: --resume=<id> in cmdline.
            for arg in cmdline:
                if not arg:
                    continue
                low = arg.lower()
                if "resume" not in low and "connect" not in low:
                    continue
                token = arg.split("=", 1)[-1].strip().strip('"').strip("'")
                target = by_id.get(token) or (by_short.get(token) if len(token) >= 7 else None)
                if target is not None:
                    _record(target, proc.info["pid"])
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue


def humanize_age(dt: datetime | None) -> str:
    if dt is None:
        return "?"
    delta = datetime.now(timezone.utc) - dt
    secs = int(delta.total_seconds())
    if secs < 60:
        return f"{secs}s ago"
    if secs < 3600:
        return f"{secs // 60}m ago"
    if secs < 86400:
        return f"{secs // 3600}h ago"
    days = secs // 86400
    if days < 30:
        return f"{days}d ago"
    if days < 365:
        return f"{days // 30}mo ago"
    return f"{days // 365}y ago"


def truncate(text: str, n: int) -> str:
    if len(text) <= n:
        return text
    return text[: n - 1] + "…"


_AGENT_LABELS = {
    "working": ("▶ working", "yellow"),
    "waiting": ("… waiting", "cyan"),
    "done":    ("✓ done",    "green"),
}


def _agent_cell(s: "Session") -> object:
    """Render the agent state column with light color hinting."""
    state = s.agent_state
    if not s.is_live:
        # Once the process is gone, the session is effectively done.
        state = "done" if state else ""
    if not state:
        return ""
    label, color = _AGENT_LABELS.get(state, (state, "white"))
    return Text(label, style=color)


# ─── Tab launching ──────────────────────────────────────────────────────────

def _resolve_shell() -> str:
    """Return path to pwsh.exe if available, else powershell.exe."""
    return shutil.which("pwsh") or shutil.which("powershell") or "powershell.exe"


def _resolve_wt() -> str | None:
    """Return path to wt.exe if available, else None."""
    return shutil.which("wt")


def _hwnds_for_pid(target_pid: int) -> list[int]:
    """Return visible top-level HWNDs owned by the given process id."""
    if sys.platform != "win32":
        return []
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    EnumWindows = user32.EnumWindows
    GetWindowThreadProcessId = user32.GetWindowThreadProcessId
    IsWindowVisible = user32.IsWindowVisible

    found: list[int] = []
    WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)

    def _cb(hwnd, _lparam):
        pid = wintypes.DWORD()
        GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value == target_pid and IsWindowVisible(hwnd):
            found.append(int(hwnd))
        return True

    EnumWindows(WNDENUMPROC(_cb), 0)
    return found


def _focus_hwnd(hwnd: int) -> bool:
    """Restore-and-foreground the given HWND. Returns True on success.

    Windows blocks SetForegroundWindow unless the calling thread owns the
    current foreground window. Pressing/releasing ALT once tricks the OS into
    granting the call (a well-known and Microsoft-documented workaround).
    """
    if sys.platform != "win32" or not hwnd:
        return False
    import ctypes

    user32 = ctypes.windll.user32
    SW_RESTORE = 9
    VK_MENU = 0x12
    KEYEVENTF_KEYUP = 0x0002
    try:
        # If minimised, restore first.
        if user32.IsIconic(hwnd):
            user32.ShowWindow(hwnd, SW_RESTORE)
        # Bypass SetForegroundWindow restrictions.
        user32.keybd_event(VK_MENU, 0, 0, 0)
        user32.keybd_event(VK_MENU, 0, KEYEVENTF_KEYUP, 0)
        user32.BringWindowToTop(hwnd)
        ok = bool(user32.SetForegroundWindow(hwnd))
        user32.SetActiveWindow(hwnd)
        return ok
    except OSError:
        return False


def _self_wt_pid() -> int | None:
    """Pid of the WindowsTerminal.exe process hosting this dashboard, if any."""
    if psutil is None:
        return None
    try:
        for p in psutil.Process(os.getpid()).parents():
            if (p.name() or "").lower() == "windowsterminal.exe":
                return p.pid
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        pass
    return None


def _find_session_wt_pid(session: "Session") -> int | None:
    """Return the WindowsTerminal.exe pid hosting this session, if any."""
    if psutil is None or session.pid is None:
        return None
    try:
        proc = psutil.Process(session.pid)
        for p in (proc, *proc.parents()):
            if (p.name() or "").lower() == "windowsterminal.exe":
                return p.pid
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return None
    return None


def _find_session_hwnd(session: "Session") -> int | None:
    """Walk the session's process + ancestors looking for a visible window."""
    if psutil is None or session.pid is None:
        return None
    try:
        proc = psutil.Process(session.pid)
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return None
    candidates = [proc, *proc.parents()]
    preferred = [p for p in candidates if (p.name() or "").lower() == "windowsterminal.exe"]
    for p in (*preferred, *candidates):
        try:
            hwnds = _hwnds_for_pid(p.pid)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        if hwnds:
            return hwnds[0]
    return None


def _wt_window_name(session: "Session") -> str:
    """Stable per-session WT window name used by `wt -w <name>`."""
    return f"copilot-{session.short_id}"


def _marker_path(session: "Session") -> Path:
    return SESSION_ROOT / session.id / ".dash-window"


def _record_spawn(session: "Session", window_name: str) -> None:
    try:
        _marker_path(session).write_text(window_name, encoding="utf-8")
    except OSError:
        pass


def _has_known_window(session: "Session") -> str | None:
    """Return the window name we previously launched for this session, if any."""
    try:
        return _marker_path(session).read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def _focus_wt_tab(titles: list[str]) -> str | None:
    """Find a WT tab whose name matches any of `titles` (in priority order)
    and select it. Returns the matched title, or None."""
    if sys.platform != "win32" or not titles:
        return None
    try:
        import uiautomation as auto  # type: ignore
    except ImportError:
        return None

    wanted = [t for t in titles if t]
    if not wanted:
        return None

    try:
        desktop = auto.GetRootControl()
        wt_windows = [
            c for c in desktop.GetChildren()
            if (c.ClassName or "").upper().startswith("CASCADIA")
        ]
    except Exception:
        return None

    # Collect all tabs across all WT windows once.
    all_tabs: list[tuple[object, str, object]] = []  # (wt_window, tab_name, tab_ctrl)

    def collect(node, wt, depth: int = 0):
        if depth > 6:
            return
        try:
            for child in node.GetChildren():
                try:
                    if child.ControlTypeName == "TabItemControl":
                        all_tabs.append((wt, child.Name or "", child))
                except Exception:
                    pass
                collect(child, wt, depth + 1)
        except Exception:
            return

    for wt in wt_windows:
        collect(wt, wt)

    if not all_tabs:
        return None

    for want in wanted:
        for wt, name, tab in all_tabs:
            if name == want:
                _select_tab(tab, wt)
                return want
    # Fallback: substring match (handles e.g. tab name "X — extra")
    for want in wanted:
        wl = want.lower()
        for wt, name, tab in all_tabs:
            if wl and wl in name.lower():
                _select_tab(tab, wt)
                return name
    return None


def _select_tab(tab, wt) -> None:
    try:
        sel = tab.GetSelectionItemPattern()
        if sel is not None:
            sel.Select()
        else:
            inv = tab.GetInvokePattern()
            if inv is not None:
                inv.Invoke()
    except Exception:
        pass
    try:
        hwnd = wt.NativeWindowHandle
        if hwnd:
            _focus_hwnd(hwnd)
    except Exception:
        pass


def launch_new_session(cwd: str | None = None) -> tuple[bool, str]:
    """Open a fresh `copilot` session as a new WT tab."""
    shell = _resolve_shell()
    cwd = cwd or os.getcwd()
    inner = [shell, "-NoExit", "-Command", "copilot"]
    title = "copilot:new"
    wt = _resolve_wt()
    if wt:
        argv = [
            wt, "-w", "0", "new-tab",
            "--suppressApplicationTitle",
            "--title", title,
            "-d", cwd,
            *inner,
        ]
        mode = "new tab"
    else:
        argv = inner
        mode = "new console window"
    try:
        creationflags = 0
        if not wt and sys.platform == "win32":
            creationflags = subprocess.CREATE_NEW_CONSOLE  # type: ignore[attr-defined]
        subprocess.Popen(
            argv,
            cwd=cwd if os.path.isdir(cwd) else None,
            creationflags=creationflags,
            close_fds=True,
        )
    except (OSError, FileNotFoundError) as e:
        return False, f"launch failed: {e}"
    return True, f"→ launched new copilot session in {mode} (cwd: {cwd})"


def launch_session_tab(session: "Session") -> tuple[bool, str]:
    """Open the session as a NEW TAB in the current WT window (or a new console).

    Uses `wt -w 0 new-tab` so each session becomes a tab in the user's existing
    WT window rather than spawning a separate window.
    """
    shell = _resolve_shell()
    cwd = session.cwd or os.path.expanduser("~")
    resume_cmd = f'copilot --resume="{session.id}"'
    inner = [shell, "-NoExit", "-Command", resume_cmd]
    # Prefer the session summary as the tab title; fall back to the short id.
    summary = (session.summary or "").strip()
    title = summary if summary else f"copilot:{session.short_id}"

    wt = _resolve_wt()
    if wt:
        argv = [
            wt, "-w", "0", "new-tab",
            "--suppressApplicationTitle",
            "--title", title,
            "-d", cwd,
            *inner,
        ]
        mode = "new tab"
    else:
        argv = inner
        mode = "new console window"

    try:
        creationflags = 0
        if not wt and sys.platform == "win32":
            creationflags = subprocess.CREATE_NEW_CONSOLE  # type: ignore[attr-defined]
        subprocess.Popen(
            argv,
            cwd=cwd if os.path.isdir(cwd) else None,
            creationflags=creationflags,
            close_fds=True,
        )
    except (OSError, FileNotFoundError) as e:
        return False, f"launch failed: {e}"
    if wt:
        _record_spawn(session, "tab")
    return True, f"→ launched {session.short_id} as {mode}"


def focus_session(session: "Session") -> tuple[bool, str]:
    """Surface an already-open session.

    Strategy: use UI Automation to find a WT tab whose name matches one of:
      1. session.summary  (copilot CLI sets WT tab title to the summary)
      2. f"copilot:{short_id}"  (our explicit --title for fresh launches)
      3. session.short_id
    Then select it and bring its WT window forward.
    """
    candidates = [session.summary, f"copilot:{session.short_id}", session.short_id]
    matched = _focus_wt_tab(candidates)
    if matched:
        return True, f"→ focused tab '{matched[:60]}'"

    # Win32 fallback for non-WT consoles.
    hwnd = _find_session_hwnd(session)
    if hwnd is not None and _focus_hwnd(hwnd):
        return True, f"→ focused existing window for {session.short_id}"
    return False, "no existing tab found"


class DashboardApp(App):
    CSS = """
    Screen { layout: vertical; }
    #search { dock: top; height: 3; display: none; }
    #search.visible { display: block; }
    #status { dock: bottom; height: 1; color: $text-muted; padding: 0 1; }
    DataTable { height: 1fr; }
    """

    BINDINGS = [
        Binding("enter", "jump", "Jump"),
        Binding("n", "new_session", "New session"),
        Binding("/", "toggle_search", "Search"),
        Binding("escape", "clear_search", "Clear"),
        Binding("r", "refresh", "Refresh"),
        Binding("a", "toggle_empty", "Show empty"),
        Binding("l", "toggle_live", "Live only"),
        Binding("v", "open_in_vscode", "Open in VSCode"),
        Binding("q", "quit", "Quit"),
        Binding("ctrl+c", "quit", "Quit", show=False),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.sessions: list[Session] = []
        self.row_keys: list[str] = []  # session id per visible row
        self.filter_text: str = ""
        self.show_empty: bool = False  # hide sessions with no summary by default
        self.live_only: bool = False   # when True, hide non-live sessions
        self.sort_col: int | None = None  # None → default tier sort
        self.sort_desc: bool = False

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Input(placeholder="filter… (esc to clear)", id="search")
        yield DataTable(id="table", cursor_type="cell", zebra_stripes=True)
        yield Static("", id="status")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one(DataTable)
        # Reserve trailing space for the sort indicator on every label.
        self._base_labels = [
            "Summary  ", " ", "Agent  ", "Turns  ", "PR  ", "Updated  ",
            "ID  ", "Repo / Branch  ", "CWD  ",
        ]
        self.col_keys = list(table.add_columns(*self._base_labels))
        self.action_refresh()
        self.set_focus(table)
        # Auto-refresh every 5 seconds; preserves cursor position.
        self.set_interval(5.0, self._auto_refresh)

    def _auto_refresh(self) -> None:
        try:
            table = self.query_one(DataTable)
            saved_row = table.cursor_row
            saved_col = table.cursor_column
        except Exception:
            saved_row, saved_col = 0, 0
        self.action_refresh()
        try:
            table = self.query_one(DataTable)
            max_row = max(0, table.row_count - 1)
            row = min(saved_row or 0, max_row)
            col = saved_col or 0
            table.move_cursor(row=row, column=col, animate=False)
        except Exception:
            pass

    def action_refresh(self) -> None:
        self.sessions = load_sessions()
        detect_live_sessions(self.sessions)
        self._populate()

    def _populate(self) -> None:
        table = self.query_one(DataTable)
        # Update header labels with sort indicator on the active sort column.
        try:
            for i, key in enumerate(getattr(self, "col_keys", [])):
                base = self._base_labels[i].rstrip()
                if i == self.sort_col and base:
                    label = f"{base} {'▼' if self.sort_desc else '▲'}"
                else:
                    label = self._base_labels[i]
                table.columns[key].label = Text(label)
            table.refresh()
        except Exception:
            pass
        table.clear()
        self.row_keys = []
        needle = self.filter_text.strip().lower()
        live_count = 0
        recent_count = 0
        hidden_empty = 0
        hidden_nonlive = 0
        # If user picked a sort column, use it; otherwise default tiered sort.
        if self.sort_col is not None and self.sort_col in self._sort_keys:
            _, keyfn, _ = self._sort_keys[self.sort_col]
            ordered = sorted(self.sessions, key=keyfn, reverse=self.sort_desc)
        else:
            def _sort_key(s: Session):
                tier = 0 if s.is_live else (1 if s.is_recent else 2)
                ts = s.updated_at or datetime.fromtimestamp(s.mtime, tz=timezone.utc)
                return (tier, -ts.timestamp())
            ordered = sorted(self.sessions, key=_sort_key)
        # Live and recent counts always reflect the actual sessions, not order.
        for s in ordered:
            if s.is_live:
                live_count += 1
            elif s.is_recent:
                recent_count += 1
            if self.live_only and not s.is_live:
                hidden_nonlive += 1
                continue
            if not self.show_empty and not (s.summary or "").strip() and not s.is_live:
                hidden_empty += 1
                continue
            if needle:
                hay = " ".join((s.id, s.cwd, s.repository, s.branch, s.summary, s.pr)).lower()
                if needle not in hay:
                    continue
            repo_branch = s.repository or "—"
            if s.branch:
                repo_branch = f"{repo_branch} ({s.branch})" if s.repository else s.branch
            pr_cell: object = ""
            if s.pr:
                if s.pr_url:
                    pr_cell = Text(
                        s.pr,
                        style=Style(
                            color="cyan",
                            underline=True,
                            meta={"@click": f"open_pr({s.pr_url!r})"},
                        ),
                    )
                else:
                    pr_cell = s.pr
            table.add_row(
                truncate(s.summary or "—", 50),
                s.status,
                _agent_cell(s),
                str(s.turns) if s.turns else "",
                pr_cell,
                humanize_age(s.updated_at),
                s.short_id,
                truncate(repo_branch, 50),
                truncate(s.cwd or "—", 50),
            )
            self.row_keys.append(s.id)
        status = self.query_one("#status", Static)
        total = len(self.sessions)
        shown = len(self.row_keys)
        bits = [f"{shown}/{total} sessions"]
        if live_count:
            bits.append(f"● {live_count} live")
        if recent_count:
            bits.append(f"○ {recent_count} recent")
        if hidden_empty:
            bits.append(f"({hidden_empty} empty hidden — press 'a')")
        if self.live_only:
            bits.append("[live only — press 'l']")
        bits.append(f"root: {SESSION_ROOT}")
        status.update("   ".join(bits))

    def action_toggle_empty(self) -> None:
        self.show_empty = not self.show_empty
        self._populate()

    def action_toggle_live(self) -> None:
        self.live_only = not self.live_only
        self._populate()

    def action_open_pr(self, url: str) -> None:
        import webbrowser
        webbrowser.open(url)
        self.query_one("#status", Static).update(f"→ opened {url}")

    def action_new_session(self) -> None:
        # Use the currently selected row's cwd as the starting dir, if any.
        cwd: str | None = None
        try:
            table = self.query_one(DataTable)
            idx = table.cursor_row
            if idx is not None and 0 <= idx < len(self.row_keys):
                sid = self.row_keys[idx]
                sess = next((s for s in self.sessions if s.id == sid), None)
                if sess and sess.cwd and os.path.isdir(sess.cwd):
                    cwd = sess.cwd
        except Exception:
            pass
        ok, msg = launch_new_session(cwd)
        self.query_one("#status", Static).update(msg if ok else f"[red]{msg}[/red]")

    def _selected_session(self) -> Session | None:
        try:
            table = self.query_one(DataTable)
            idx = table.cursor_row
            if idx is None or not (0 <= idx < len(self.row_keys)):
                return None
            sid = self.row_keys[idx]
            return next((s for s in self.sessions if s.id == sid), None)
        except Exception:
            return None

    def action_open_in_vscode(self) -> None:
        sess = self._selected_session()
        if sess is None:
            return
        target = SESSION_ROOT / sess.id
        status = self.query_one("#status", Static)
        # Find the `code` launcher (Windows ships it as code.cmd on PATH).
        code = shutil.which("code") or shutil.which("code.cmd") or shutil.which("code-insiders")
        if not code:
            status.update("[red]'code' not found on PATH[/red]")
            return
        try:
            subprocess.Popen(
                [code, str(target)],
                shell=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            status.update(f"→ opened {target} in VSCode")
        except Exception as exc:
            status.update(f"[red]failed to launch VSCode: {exc}[/red]")

    def on_data_table_header_selected(self, event) -> None:
        col = getattr(event, "column_index", None)
        if col is None:
            try:
                col = event.column_key  # may be ColumnKey, not int — skip if so
                col = None
            except Exception:
                col = None
        if col is None:
            return
        if col not in self._sort_keys:
            return
        if self.sort_col == col:
            self.sort_desc = not self.sort_desc
        else:
            _, _, default_desc = self._sort_keys[col]
            self.sort_col = col
            self.sort_desc = default_desc
        self._populate()

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "search":
            self.filter_text = event.value
            self._populate()

    def action_toggle_search(self) -> None:
        search = self.query_one("#search", Input)
        if "visible" in search.classes:
            search.remove_class("visible")
            self.set_focus(self.query_one(DataTable))
        else:
            search.add_class("visible")
            self.set_focus(search)

    def action_clear_search(self) -> None:
        search = self.query_one("#search", Input)
        if "visible" in search.classes or search.value:
            search.value = ""
            search.remove_class("visible")
            self.filter_text = ""
            self._populate()
            self.set_focus(self.query_one(DataTable))

    def _jump_row(self, idx: int | None) -> None:
        if idx is None or idx < 0 or idx >= len(self.row_keys):
            return
        sid = self.row_keys[idx]
        sess = next((s for s in self.sessions if s.id == sid), None)
        if sess is None:
            return
        status = self.query_one("#status", Static)
        # Refresh live status for this jump so a tab opened seconds ago is found.
        detect_live_sessions([sess])
        # Always try focus first — a tab might exist even if process detection missed it.
        ok, msg = focus_session(sess)
        if ok:
            status.update(msg)
            return
        # No existing tab (or focus failed) — launch a fresh one.
        ok, msg = launch_session_tab(sess)
        status.update(msg if ok else f"[red]{msg}[/red]")

    PR_COL = 4  # index of the PR column in the table

    # Map column index → (label, key function on Session, default descending?)
    @staticmethod
    def _pr_int(s: "Session") -> int:
        try:
            return int((s.pr or "#0").lstrip("#"))
        except ValueError:
            return 0

    @property
    def _sort_keys(self):
        # Order: working first, then waiting, then done, then unknown.
        agent_order = {"working": 0, "waiting": 1, "done": 2, "": 3}
        return {
            0: ("Summary",      lambda s: (s.summary or "").lower(),       False),
            1: ("Status",       lambda s: (0 if s.is_live else (1 if s.is_recent else 2)), False),
            2: ("Agent",        lambda s: agent_order.get(s.agent_state, 9), False),
            3: ("Turns",        lambda s: s.turns,                         True),
            4: ("PR",           lambda s: self._pr_int(s),                 True),
            5: ("Updated",      lambda s: (s.updated_at or datetime.fromtimestamp(s.mtime, tz=timezone.utc)).timestamp(), True),
            6: ("ID",           lambda s: s.short_id,                      False),
            7: ("Repo/Branch",  lambda s: (s.repository or "").lower() + " " + (s.branch or "").lower(), False),
            8: ("CWD",          lambda s: (s.cwd or "").lower(),           False),
        }

    def _activate(self, row_idx: int | None, col_idx: int | None) -> None:
        """Handle Enter or click activation: open PR if PR column, else jump."""
        if row_idx is None or row_idx < 0 or row_idx >= len(self.row_keys):
            return
        if col_idx == self.PR_COL:
            sid = self.row_keys[row_idx]
            sess = next((s for s in self.sessions if s.id == sid), None)
            if sess and sess.pr_url:
                import webbrowser
                webbrowser.open(sess.pr_url)
                self.query_one("#status", Static).update(f"→ opened {sess.pr_url}")
                return
        self._jump_row(row_idx)

    def on_data_table_cell_selected(self, event) -> None:
        coord = getattr(event, "coordinate", None)
        if coord is not None:
            self._activate(coord.row, coord.column)
            return
        # Fallback if no coordinate on event
        try:
            table = self.query_one(DataTable)
            self._activate(table.cursor_row, table.cursor_column)
        except Exception:
            pass

    def on_click(self, event) -> None:
        # Single click on PR cells is handled by the @click meta on the Text.
        # Here we only handle double-click on a row → activate (jump).
        if getattr(event, "chain", 1) < 2:
            return
        try:
            table = self.query_one(DataTable)
        except Exception:
            return
        widget = getattr(event, "widget", None)
        node = widget
        on_table = False
        while node is not None:
            if node is table:
                on_table = True
                break
            node = getattr(node, "parent", None)
        if not on_table:
            return
        self._activate(table.cursor_row, table.cursor_column)

    def action_jump(self) -> None:
        try:
            table = self.query_one(DataTable)
            self._activate(table.cursor_row, table.cursor_column)
        except Exception:
            pass


def main() -> int:
    if not SESSION_ROOT.exists():
        print(f"No Copilot session directory found at {SESSION_ROOT}", file=sys.stderr)
        return 1
    DashboardApp().run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
