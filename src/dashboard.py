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
import asyncio
import time
from dataclasses import dataclass, field
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
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Footer, Header, Input, Label, RichLog, Static, Switch


SESSION_ROOT = Path(os.environ.get("COPILOT_CONFIG_DIR", Path.home() / ".copilot")) / "session-state"
SESSION_STORE_DB = Path(os.environ.get("COPILOT_CONFIG_DIR", Path.home() / ".copilot")) / "session-store.db"
ACTIVE_WINDOW_SECONDS = 10 * 60  # session updated within 10 min => "recent"
LIVE_WINDOW_SECONDS = 60  # events.jsonl touched within this => "live"

CONFIG_PATH = Path.home() / ".copilot-dashboard" / "config.json"
DEFAULT_CONFIG = {
    "yolo": True,                # pass --yolo when launching copilot
    "autopilot": True,           # pass --autopilot when launching copilot
    "refresh_interval": 30,      # auto-refresh interval, seconds
}


def load_config() -> dict:
    cfg = DEFAULT_CONFIG.copy()
    try:
        if CONFIG_PATH.exists():
            data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                for k in cfg:
                    if k in data:
                        cfg[k] = data[k]
    except Exception:
        pass
    return cfg


def save_config(cfg: dict) -> None:
    try:
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        CONFIG_PATH.write_text(
            json.dumps(cfg, indent=2) + "\n", encoding="utf-8"
        )
    except Exception:
        pass


def _file_debug(msg: str) -> None:
    """Module-level debug logger (used outside the App class)."""
    try:
        log_path = Path.home() / ".copilot-dashboard" / "debug.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%H:%M:%S')} {msg}\n")
    except Exception:
        pass


def copilot_command(cfg: dict, *, resume_id: str | None = None) -> str:
    parts = ["copilot"]
    if cfg.get("yolo"):
        parts.append("--yolo")
    if cfg.get("autopilot"):
        parts.append("--autopilot")
    if resume_id:
        parts.append(f'--resume="{resume_id}"')
    return " ".join(parts)



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
    prs: list[tuple[int, str]] = field(default_factory=list)  # all PRs as (number, url)
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
        # PR refs — collect ALL per session, not just the highest-numbered.
        all_prs: dict[str, set[int]] = {}
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
                all_prs.setdefault(sid, set()).add(n)
        except sqlite3.Error:
            pass
        for sid, nums in all_prs.items():
            sess = by_id[sid]
            sorted_nums = sorted(nums)
            # Resolve canonical URL per PR via events.jsonl (one scan per file).
            ev = SESSION_ROOT / sid / "events.jsonl"
            url_by_n: dict[int, str] = {}
            if ev.exists():
                pat = re.compile(
                    r"https://github\.com/([^/\s\"\\]+)/([^/\s\"\\]+)/pull/(\d+)\b"
                )
                try:
                    with ev.open("r", encoding="utf-8", errors="replace") as f:
                        for line in f:
                            if "/pull/" not in line:
                                continue
                            for m in pat.finditer(line):
                                try:
                                    pn = int(m.group(3))
                                except ValueError:
                                    continue
                                if pn in nums and pn not in url_by_n:
                                    url_by_n[pn] = m.group(0)
                            if len(url_by_n) >= len(nums):
                                break
                except OSError:
                    pass
            sess.prs = []
            for n in sorted_nums:
                url = url_by_n.get(n) or (
                    f"https://github.com/{sess.repository}/pull/{n}"
                    if sess.repository else ""
                )
                sess.prs.append((n, url))
            # Maintain back-compat single-PR fields (highest-numbered).
            if sess.prs:
                top_n, top_url = sess.prs[-1]
                sess.pr = f"#{top_n}"
                sess.pr_url = top_url
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


def _quick_live_check(session: "Session") -> None:
    """Fast targeted live-check for a single session.

    Avoids the full `process_iter + open_files` sweep that
    `detect_live_sessions` does for every process. We only inspect copilot
    processes, and we trust the cached pid first (cheap), falling back to a
    cmdline `--resume=<id>` scan (no open_files calls).
    """
    if psutil is None:
        return
    short = session.id[:8]
    # 1) Fast path: re-validate the cached pid.
    if session.pid:
        try:
            p = psutil.Process(session.pid)
            cmd = " ".join((p.cmdline() or [])[:6]).lower()
            if "copilot" in (p.name() or "").lower() or "copilot" in cmd:
                session.running = True
                return
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
        session.running = False
        session.pid = None
    # 2) Cheap secondary scan: cmdline `--resume=<id>` only (no open_files).
    for proc in psutil.process_iter(["name", "cmdline", "pid"]):
        try:
            name = (proc.info.get("name") or "").lower()
            cmdline = proc.info.get("cmdline") or []
            joined_head = " ".join(cmdline[:6]).lower()
            if "copilot" not in name and "copilot" not in joined_head:
                continue
            for arg in cmdline:
                if not arg:
                    continue
                low = arg.lower()
                if "resume" not in low and "connect" not in low:
                    continue
                token = arg.split("=", 1)[-1].strip().strip('"').strip("'")
                if token == session.id or (len(token) >= 7 and token == short):
                    session.running = True
                    session.pid = proc.info["pid"]
                    return
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


def _summarize_tool_args(tool: str, args) -> str:
    if not isinstance(args, dict):
        return ""
    # Pick the most useful field per common tool, falling back to a generic
    # short JSON-ish representation.
    candidates = (
        "command", "description", "path", "file_path", "url", "pattern",
        "query", "prompt", "message", "intent",
    )
    for key in candidates:
        v = args.get(key)
        if isinstance(v, str) and v.strip():
            return truncate(v.strip().replace("\n", " "), 140)
    # Fallback: first short string-valued field.
    for k, v in args.items():
        if isinstance(v, str) and v.strip():
            return f"{k}={truncate(v.strip(), 100)}"
    return ""


def _render_session_preview(session_id: str, max_events: int = 25) -> "Text":
    """Return a Rich Text summary of recent events.jsonl entries for a session.

    Reads only the tail of the file (last ~256KB) so this is cheap even on
    long-lived sessions. Returns a friendly "no events" placeholder rather
    than raising when the file is missing/short.
    """
    sess_dir = SESSION_ROOT / session_id
    events_file = sess_dir / "events.jsonl"
    if not events_file.exists():
        return Text("(no events recorded yet)", style="dim")
    try:
        with events_file.open("rb") as f:
            f.seek(0, 2)
            size = f.tell()
            seek_to = max(0, size - 256 * 1024)
            f.seek(seek_to)
            tail = f.read().decode("utf-8", errors="replace")
    except Exception as exc:
        return Text(f"(error reading events.jsonl: {exc})", style="red")
    raw_lines = tail.split("\n")
    # If we seeked into the middle of the file, the first line is likely
    # partial — drop it.
    if seek_to > 0 and raw_lines:
        raw_lines = raw_lines[1:]
    events = []
    for ln in raw_lines:
        ln = ln.strip()
        if not ln:
            continue
        try:
            events.append(json.loads(ln))
        except Exception:
            continue
    # Filter to the types we render and take the tail.
    keep = {
        "user.message", "assistant.message",
        "tool.execution_start", "tool.execution_complete",
    }
    filtered = [ev for ev in events if ev.get("type") in keep]
    filtered = filtered[-max_events:]
    out = Text()
    if not filtered:
        out.append("(no recent activity)\n", style="dim")
        return out
    for ev in filtered:
        t = ev.get("type", "")
        d = ev.get("data") or {}
        if t == "user.message":
            txt = (d.get("content") or "").strip().replace("\n", " ")
            out.append("user      ", style="bold cyan")
            out.append(truncate(txt, 200) + "\n")
        elif t == "assistant.message":
            txt = (d.get("content") or "").strip().replace("\n", " ")
            out.append("assistant ", style="bold green")
            out.append(truncate(txt, 200) + "\n")
        elif t == "tool.execution_start":
            tool = d.get("toolName") or d.get("tool_name") or "?"
            arg_str = _summarize_tool_args(tool, d.get("arguments") or d.get("args"))
            out.append(f"  → {tool} ", style="bold yellow")
            if arg_str:
                out.append(arg_str + "\n", style="dim")
            else:
                out.append("\n")
        elif t == "tool.execution_complete":
            tool = d.get("toolName") or d.get("tool_name") or "?"
            success = d.get("success")
            if success is False:
                err = ""
                res = d.get("result") or {}
                if isinstance(res, dict):
                    err = (res.get("error") or "").strip().replace("\n", " ")
                out.append(f"  ✗ {tool} ", style="bold red")
                if err:
                    out.append(truncate(err, 160) + "\n", style="dim")
                else:
                    out.append("\n")
            else:
                out.append(f"  ✓ {tool}\n", style="green")
    return out


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


def _focus_wt_tab(titles: list[str]) -> tuple[str | None, list[str]]:
    """Find a WT tab whose name matches any of `titles` (in priority order)
    and select it.

    Returns (matched_title_or_None, all_seen_tab_names) — the seen list is
    useful for diagnostics when no match is found.
    """
    if sys.platform != "win32" or not titles:
        return None, []
    try:
        import uiautomation as auto  # type: ignore
    except ImportError:
        return None, []

    def _norm(s: str) -> str:
        return " ".join((s or "").strip().lower().split())

    def _alnum(s: str) -> str:
        # Keep only [a-z0-9] runs separated by single spaces. Strips spinner
        # glyphs, bullets, parentheses, em-dashes, status markers like
        # '(working)', '●', '⠋', etc. that copilot prefixes when active.
        out: list[str] = []
        run: list[str] = []
        for ch in (s or "").lower():
            if ch.isalnum():
                run.append(ch)
            elif run:
                out.append("".join(run))
                run = []
        if run:
            out.append("".join(run))
        return " ".join(out)

    wanted_raw = [t for t in titles if t]
    wanted = [_norm(t) for t in wanted_raw]
    wanted_alnum = [_alnum(t) for t in wanted_raw]
    if not any(wanted):
        return None, []

    try:
        desktop = auto.GetRootControl()
        wt_windows = [
            c for c in desktop.GetChildren()
            if (c.ClassName or "").upper().startswith("CASCADIA")
        ]
    except Exception:
        return None, []

    # Collect all tabs across all WT windows, but bail per-subtree as soon as
    # we've descended past the depth where WT puts the tab strip (~3-4).
    all_tabs: list[tuple[object, str, object]] = []  # (wt_window, tab_name, tab_ctrl)

    def collect(node, wt, depth: int = 0):
        if depth > 5:
            return
        try:
            for child in node.GetChildren():
                try:
                    ct = child.ControlTypeName
                except Exception:
                    ct = ""
                if ct == "TabItemControl":
                    try:
                        all_tabs.append((wt, child.Name or "", child))
                    except Exception:
                        pass
                    # TabItems don't contain other TabItems; skip recursion.
                    continue
                collect(child, wt, depth + 1)
        except Exception:
            return

    for wt in wt_windows:
        collect(wt, wt)

    seen_names = [n for _, n, _ in all_tabs]
    if not all_tabs:
        return None, seen_names

    norm_tabs = [(_norm(n), _alnum(n), wt, n, ctrl) for wt, n, ctrl in all_tabs]

    # Pass 1: exact (normalized) match.
    for want in wanted:
        if not want:
            continue
        for nname, _ana, wt, raw, ctrl in norm_tabs:
            if nname == want:
                _select_tab(ctrl, wt)
                return raw, seen_names

    # Pass 2: substring match in either direction (handles "X — extra"
    # suffixes added by terminals/profiles, or summaries that were truncated
    # in workspace.yaml relative to the OSC title set by the running CLI).
    for want in wanted:
        if not want or len(want) < 4:
            continue
        for nname, _ana, wt, raw, ctrl in norm_tabs:
            if not nname:
                continue
            if want in nname or nname in want:
                _select_tab(ctrl, wt)
                return raw, seen_names

    # Pass 3: alphanumeric-only substring match. Handles spinner glyphs,
    # bullets, "(working)" / "(waiting)" status markers, em-dashes, etc.
    # that the CLI prefixes/decorates the OSC title with while the agent is
    # active — the raw title may be e.g. "⠋ Add Paging Tests (working)" but
    # the alphanumeric core is still "add paging tests working".
    for want_a in wanted_alnum:
        if not want_a or len(want_a) < 4:
            continue
        for _nn, ana, wt, raw, ctrl in norm_tabs:
            if not ana:
                continue
            if want_a in ana or ana in want_a:
                _select_tab(ctrl, wt)
                return raw, seen_names

    return None, seen_names


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


def _focus_wt_tab_by_pid(session_id: str, session_pid: int | None) -> tuple[bool, str]:
    """Focus a WT tab by walking the session's process tree.

    This is title-independent — it works even when the CLI keeps rewriting
    the OSC tab title (e.g. while the agent is in 'working' mode).

    Strategy:
      1. Take session.pid (the running copilot process), walk its parents
         until we find a pwsh whose ppid is a WindowsTerminal.exe pid. As a
         backup, search psutil for any pwsh child of WT whose cmdline
         contains `--resume=<session_id>`.
      2. Find the WT UIA window whose ProcessId matches that WT pid.
      3. Sort that WT window's child pwsh processes by create_time. WT
         appends new tabs at the end of the strip, so this index typically
         matches the visual tab order.
      4. Sort the window's TabItem controls by their bounding-rect X
         coordinate (visual left-to-right) and select the one at the same
         index.

    Returns (ok, msg).
    """
    if sys.platform != "win32":
        return False, "non-windows"
    if psutil is None:
        return False, "psutil unavailable"

    short = session_id[:8] if session_id else ""

    # 1) Find our session's pwsh process (direct child of WT).
    target_shell = None
    if session_pid:
        try:
            cur = psutil.Process(session_pid)
            for _ in range(8):
                par = cur.parent()
                if par is None:
                    break
                try:
                    pname = (par.name() or "").lower()
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    break
                if pname.startswith("pwsh") or pname.startswith("powershell"):
                    # Is its parent WT?
                    try:
                        gp = par.parent()
                        if gp and (gp.name() or "").lower().startswith("windowsterminal"):
                            target_shell = par
                            break
                    except (psutil.NoSuchProcess, psutil.AccessDenied):
                        pass
                cur = par
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass

    if target_shell is None and session_id:
        # Fallback: scan WT's child shells for our --resume token.
        for proc in psutil.process_iter(["name", "cmdline", "ppid", "pid"]):
            try:
                pname = (proc.info.get("name") or "").lower()
                if not (pname.startswith("pwsh") or pname.startswith("powershell")):
                    continue
                par = proc.parent()
                if par is None or not (par.name() or "").lower().startswith("windowsterminal"):
                    continue
                cmdline = proc.info.get("cmdline") or []
                joined = " ".join(cmdline).lower()
                if session_id.lower() in joined or (short and short.lower() in joined and "resume" in joined):
                    target_shell = proc
                    break
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue

    if target_shell is None:
        return False, "no WT-hosted shell for this session"

    try:
        wt_pid = target_shell.parent().pid
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return False, "WT parent gone"

    # 2) Enumerate WT's child shells and find ours by index.
    siblings = []
    try:
        wt_proc = psutil.Process(wt_pid)
        for ch in wt_proc.children(recursive=False):
            try:
                cn = (ch.name() or "").lower()
                if cn.startswith("pwsh") or cn.startswith("powershell") or cn.startswith("cmd"):
                    siblings.append((ch.create_time(), ch.pid))
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return False, "WT process gone"

    siblings.sort()
    sibling_pids = [pid for _, pid in siblings]
    if target_shell.pid not in sibling_pids:
        return False, "shell not a direct WT child"
    tab_index = sibling_pids.index(target_shell.pid)

    # 3) Find the WT UIA window with that pid and select its tab[tab_index].
    try:
        import uiautomation as auto  # type: ignore
    except ImportError:
        return False, "uiautomation unavailable"
    try:
        desktop = auto.GetRootControl()
        wt_window = None
        for c in desktop.GetChildren():
            try:
                if (c.ClassName or "").upper().startswith("CASCADIA") and c.ProcessId == wt_pid:
                    wt_window = c
                    break
            except Exception:
                continue
        if wt_window is None:
            return False, "WT UIA window not found"

        tabs: list = []
        def _walk(node, d=0):
            if d > 5:
                return
            try:
                for ch in node.GetChildren():
                    try:
                        if ch.ControlTypeName == "TabItemControl":
                            tabs.append(ch)
                            continue
                    except Exception:
                        pass
                    _walk(ch, d + 1)
            except Exception:
                return
        _walk(wt_window)

        if not tabs:
            return False, "WT window has no tabs"

        # Order tabs left-to-right by bounding rectangle, falling back to
        # discovery order.
        def _xkey(t):
            try:
                r = t.BoundingRectangle
                return (r.left, r.top)
            except Exception:
                return (0, 0)
        tabs_sorted = sorted(tabs, key=_xkey)

        if tab_index >= len(tabs_sorted):
            # Tabs and shells out of sync (e.g. user reordered). Just focus
            # the WT window so the user can see it.
            try:
                hwnd = wt_window.NativeWindowHandle
                if hwnd:
                    _focus_hwnd(hwnd)
            except Exception:
                pass
            return False, (
                f"shell index {tab_index} out of range ({len(tabs_sorted)} tabs); "
                "focused window only"
            )

        tab = tabs_sorted[tab_index]
        _select_tab(tab, wt_window)
        try:
            name = tab.Name or ""
        except Exception:
            name = ""
        return True, f"→ focused tab #{tab_index + 1} ({name[:40] or 'untitled'})"
    except Exception as exc:
        return False, f"UIA error: {exc}"


def _spawn_wt(argv: list[str], cwd: str | None) -> tuple[bool, str]:
    """Spawn a wt.exe command, fully detached, and capture stderr on failure.

    wt.exe is a thin dispatcher that exits almost immediately after handing
    work off to the real WindowsTerminal.exe. We wait briefly for that exit
    so we can surface a meaningful error if it failed (bad -d, malformed
    args, no WT installed, etc.) instead of silently doing nothing.
    """
    DETACHED = getattr(subprocess, "DETACHED_PROCESS", 0)
    NEW_GROUP = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    flags = DETACHED | NEW_GROUP | NO_WINDOW
    try:
        proc = subprocess.Popen(
            argv,
            cwd=cwd if (cwd and os.path.isdir(cwd)) else None,
            creationflags=flags,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            close_fds=True,
        )
    except (OSError, FileNotFoundError) as e:
        return False, f"launch failed: {e}"
    try:
        _, err = proc.communicate(timeout=4)
    except subprocess.TimeoutExpired:
        # wt has handed off to WT; no error means success.
        return True, ""
    if proc.returncode != 0:
        msg = (err.decode("utf-8", "replace").strip().splitlines() or [""])[0]
        return False, f"wt exited {proc.returncode}: {msg or 'no output'}"
    return True, ""


def launch_new_session(cwd: str | None = None, cfg: dict | None = None) -> tuple[bool, str]:
    """Open a fresh `copilot` session as a new WT tab."""
    cfg = cfg or load_config()
    shell = _resolve_shell()
    cwd = cwd or os.getcwd()
    if not os.path.isdir(cwd):
        cwd = os.path.expanduser("~")
    cmd = copilot_command(cfg)
    helper = Path(__file__).resolve().parent / "_new-session-launcher.ps1"
    if helper.exists():
        # Use the helper so the WT tab title auto-updates to the session
        # summary as soon as `copilot` creates one. Falls back to plain
        # invocation if the helper is missing for any reason.
        inner = [shell, "-NoExit", "-NoProfile", "-File", str(helper),
                 "-CopilotCommand", cmd]
    else:
        inner = [shell, "-NoExit", "-Command", cmd]
    title = "copilot:new"
    wt = _resolve_wt()
    if wt:
        # Note: deliberately NOT passing --suppressApplicationTitle here.
        # We want the `copilot` CLI to update the WT tab title to the session
        # summary once it has one. We pass --title only as an initial label.
        argv = [
            wt, "-w", "0", "new-tab",
            "--title", title,
            "-d", cwd,
            "--", *inner,
        ]
        ok, err = _spawn_wt(argv, cwd)
        if not ok:
            return False, err
        return True, f"→ launched new copilot session in new tab (cwd: {cwd})"

    # No wt — spawn a detached console window.
    try:
        creationflags = 0
        if sys.platform == "win32":
            creationflags = subprocess.CREATE_NEW_CONSOLE  # type: ignore[attr-defined]
        subprocess.Popen(
            inner,
            cwd=cwd,
            creationflags=creationflags,
            close_fds=True,
        )
    except (OSError, FileNotFoundError) as e:
        return False, f"launch failed: {e}"
    return True, f"→ launched new copilot session in new console window (cwd: {cwd})"


def launch_session_tab(session: "Session", cfg: dict | None = None) -> tuple[bool, str]:
    """Open the session as a NEW TAB in the current WT window (or a new console).

    Uses `wt -w 0 new-tab` so each session becomes a tab in the user's existing
    WT window rather than spawning a separate window.
    """
    cfg = cfg or load_config()
    shell = _resolve_shell()
    cwd = session.cwd or os.path.expanduser("~")
    resume_cmd = copilot_command(cfg, resume_id=session.id)
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
            "--", *inner,
        ]
        ok, err = _spawn_wt(argv, cwd)
        if not ok:
            return False, err
        _record_spawn(session, "tab")
        return True, f"→ launched {session.short_id} as new tab"

    # No wt — spawn a detached console window.
    try:
        creationflags = 0
        if sys.platform == "win32":
            creationflags = subprocess.CREATE_NEW_CONSOLE  # type: ignore[attr-defined]
        subprocess.Popen(
            inner,
            cwd=cwd if os.path.isdir(cwd) else None,
            creationflags=creationflags,
            close_fds=True,
        )
    except (OSError, FileNotFoundError) as e:
        return False, f"launch failed: {e}"
    return True, f"→ launched {session.short_id} as new console window"


def focus_session(session: "Session") -> tuple[bool, str]:
    """Surface an already-open session.

    Strategy:
      0. If we know the session's running pid, walk its process tree to find
         the hosting WT window + tab index. This is title-independent and
         works even while the CLI is rewriting the OSC tab title (e.g. in
         'working' mode).
      1. Otherwise, use UI Automation to find a WT tab whose name matches
         a candidate (summary, copilot:short_id, short_id).
    """
    _file_debug(
        f"focus_session sid={session.id[:8]} short={session.short_id} "
        f"pid={session.pid} running={session.running} "
        f"summary={(session.summary or '')[:50]!r}"
    )
    # Process-tree path (most reliable for live sessions).
    if session.pid or session.running:
        ok, msg = _focus_wt_tab_by_pid(session.id, session.pid)
        _file_debug(f"  by_pid → ok={ok} msg={msg!r}")
        if ok:
            return True, msg

    candidates = [session.summary, f"copilot:{session.short_id}", session.short_id]
    matched, seen = _focus_wt_tab(candidates)
    _file_debug(f"  by_title candidates={candidates!r} matched={matched!r}")
    if matched:
        return True, f"→ focused tab '{matched[:60]}'"

    # Win32 fallback for non-WT consoles.
    hwnd = _find_session_hwnd(session)
    if hwnd is not None and _focus_hwnd(hwnd):
        return True, f"→ focused existing window for {session.short_id}"

    if seen:
        preview = ", ".join(f"'{t[:30]}'" for t in seen[:6])
        more = f" (+{len(seen)-6} more)" if len(seen) > 6 else ""
        want = (session.summary or session.short_id or "")[:40]
        return False, (
            f"no tab matching '{want}'; {len(seen)} WT tab(s): {preview}{more}"
        )
    return False, "no Windows Terminal tabs found"


class SettingsScreen(ModalScreen[dict | None]):
    """Modal for editing dashboard config (yolo, autopilot, refresh interval).

    Dismisses with the new config dict on Save, or None on Cancel/Esc.
    """

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
    ]

    DEFAULT_CSS = """
    SettingsScreen {
        align: center middle;
    }
    SettingsScreen > Vertical {
        width: 60;
        height: auto;
        padding: 1 2;
        border: thick $primary;
        background: $surface;
    }
    SettingsScreen .row {
        height: 3;
        align: left middle;
    }
    SettingsScreen .row Label {
        width: 1fr;
        content-align: left middle;
        padding: 1 1;
    }
    SettingsScreen .row Switch,
    SettingsScreen .row Input {
        width: 12;
    }
    SettingsScreen #buttons {
        height: 3;
        align: right middle;
        padding-top: 1;
    }
    SettingsScreen #buttons Button {
        margin-left: 1;
    }
    SettingsScreen #title {
        text-style: bold;
        padding-bottom: 1;
    }
    SettingsScreen #help {
        color: $text-muted;
        padding-top: 1;
    }
    """

    def __init__(self, cfg: dict) -> None:
        super().__init__()
        self._cfg = dict(cfg)

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static("Settings", id="title")
            with Horizontal(classes="row"):
                yield Label("--yolo  (allow all tools/paths/urls)")
                yield Switch(value=bool(self._cfg.get("yolo", True)), id="sw-yolo")
            with Horizontal(classes="row"):
                yield Label("--autopilot  (auto-continue without prompts)")
                yield Switch(value=bool(self._cfg.get("autopilot", True)), id="sw-autopilot")
            with Horizontal(classes="row"):
                yield Label("Auto-refresh interval (seconds)")
                yield Input(
                    value=str(self._cfg.get("refresh_interval", 30)),
                    id="in-refresh",
                    restrict=r"\d*",
                )
            yield Static(
                f"Config file: {CONFIG_PATH}",
                id="help",
            )
            with Horizontal(id="buttons"):
                yield Button("Cancel", id="cancel")
                yield Button("Save", id="save", variant="primary")

    def action_cancel(self) -> None:
        self.dismiss(None)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel":
            self.dismiss(None)
            return
        if event.button.id == "save":
            try:
                refresh = int(self.query_one("#in-refresh", Input).value or "30")
            except ValueError:
                refresh = 30
            new_cfg = {
                "yolo": self.query_one("#sw-yolo", Switch).value,
                "autopilot": self.query_one("#sw-autopilot", Switch).value,
                "refresh_interval": max(2, refresh),
            }
            self.dismiss(new_cfg)



class DashboardApp(App):
    TITLE = "Copilot Dashboard"
    ENABLE_COMMAND_PALETTE = False

    CSS = """
    Screen { layout: vertical; }
    #search { dock: top; height: 3; display: none; }
    #search.visible { display: block; }
    #status { dock: bottom; height: 1; color: $text-muted; padding: 0 1; }
    #preview { dock: bottom; height: 14; border-top: solid $accent; padding: 0 1; display: none; }
    #preview.visible { display: block; }
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
        Binding("g", "toggle_group", "Group by repo"),
        Binding("p", "toggle_preview", "Preview"),
        Binding("v", "open_in_vscode", "Open in VSCode"),
        Binding("s", "open_settings", "Settings"),
        Binding("q", "quit", "Quit"),
        Binding("ctrl+c", "quit", "Quit", show=False),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.config: dict = load_config()
        self.sessions: list[Session] = []
        self.row_keys: list[str] = []  # session id per visible row
        self.filter_text: str = ""
        self.show_empty: bool = False  # hide sessions with no summary by default
        self.live_only: bool = False   # when True, hide non-live sessions
        self.group_by_repo: bool = False  # when True, group rows by repository
        self.collapsed_repos: set[str] = set()  # repos collapsed in group view
        self._separator_repos: dict[int, str] = {}  # row index -> repo name
        self.sort_col: int | None = None  # None → default tier sort
        self.sort_desc: bool = False
        self.show_preview: bool = bool(self.config.get("show_preview", False))
        self._last_preview_sid: str = ""

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Input(placeholder="filter… (esc to clear)", id="search")
        yield DataTable(id="table", cursor_type="cell", zebra_stripes=True)
        yield RichLog(id="preview", wrap=True, markup=False, highlight=False, max_lines=200)
        yield Static("", id="status")
        yield Footer()

    REFRESH_INTERVAL = 30.0  # default; overridden by config['refresh_interval']

    def on_mount(self) -> None:
        # Honor refresh_interval from config (clamped to a sane range).
        try:
            self.REFRESH_INTERVAL = max(2.0, float(self.config.get("refresh_interval", 30)))
        except (TypeError, ValueError):
            self.REFRESH_INTERVAL = 30.0
        table = self.query_one(DataTable)
        # Reserve trailing space for the sort indicator on every label.
        self._base_labels = [
            "Summary  ", " ", "Agent  ", "Turns  ", "PR  ", "Updated  ",
            "ID  ", "Repo / Branch  ", "CWD  ",
        ]
        self.col_keys = list(table.add_columns(*self._base_labels))
        self.last_refresh: float = 0.0
        self._refresh_count: int = 0
        self._refresh_in_flight: bool = False
        self._last_tick: float = time.time()
        # Show an immediate "loading" hint so the first paint isn't a blank
        # screen — the actual session scan + psutil sweep can take several
        # seconds on a busy machine. Run it in a worker so the UI mounts
        # right away.
        self.query_one("#status", Static).update(
            "[yellow]⏳ loading sessions…[/yellow]"
        )
        self.set_focus(table)
        self._apply_preview_visibility()
        self.run_worker(self._initial_load(), exclusive=False)
        # Auto-refresh ticker: re-armed each pass via call_later so that a
        # slow / overlapping refresh can never starve the next tick the way
        # set_interval does (set_interval awaits the previous async callback
        # before scheduling the next one).
        self._schedule_auto_refresh()
        # 1-second countdown ticker. Use call_later self-rescheduling rather
        # than set_interval so that a transient exception in the callback
        # cannot stop the timer permanently.
        self._schedule_tick()

    def _schedule_auto_refresh(self) -> None:
        try:
            self.set_timer(self.REFRESH_INTERVAL, self._auto_refresh_kick)
        except Exception:
            pass

    def _auto_refresh_kick(self) -> None:
        # Re-arm immediately so the next refresh happens REFRESH_INTERVAL
        # seconds from now regardless of how long this one takes.
        self._schedule_auto_refresh()
        if self._refresh_in_flight:
            self._debug("auto_refresh skipped: previous still running")
            return
        self.run_worker(self._do_auto_refresh, exclusive=False)

    def _schedule_tick(self) -> None:
        try:
            self.set_timer(1.0, self._tick_kick)
        except Exception:
            pass

    def _tick_kick(self) -> None:
        # Always re-arm first, even if the body raises, so the timer cannot
        # die on us.
        self._schedule_tick()
        try:
            self._last_tick = time.time()
            self._refresh_status_line()
        except Exception as exc:
            self._debug(f"tick error: {exc}")
        # Watchdog: if auto_refresh somehow hasn't run in 2x the interval,
        # kick it.
        try:
            stale = (
                self.last_refresh
                and (time.time() - self.last_refresh) > (self.REFRESH_INTERVAL * 2)
                and not self._refresh_in_flight
            )
            if stale:
                self._debug("watchdog: forcing auto refresh")
                self.run_worker(self._do_auto_refresh, exclusive=False)
        except Exception:
            pass

    def _debug(self, msg: str) -> None:
        try:
            log_path = Path.home() / ".copilot-dashboard" / "debug.log"
            log_path.parent.mkdir(parents=True, exist_ok=True)
            with log_path.open("a", encoding="utf-8") as f:
                f.write(f"{time.strftime('%H:%M:%S')} {msg}\n")
        except Exception:
            pass

    async def _initial_load(self) -> None:
        try:
            sessions = await asyncio.to_thread(load_sessions)
            await asyncio.to_thread(detect_live_sessions, sessions)
        except Exception as exc:
            try:
                self.query_one("#status", Static).update(
                    f"[red]load error: {exc}[/red]"
                )
            except Exception:
                pass
            return
        self.sessions = sessions
        self.last_refresh = time.time()
        self._refresh_count += 1
        self._populate()

    async def _do_auto_refresh(self) -> None:
        # Run heavy I/O off the asyncio thread so the loop (and the 1s
        # countdown ticker) keep firing during refresh.
        self._refresh_in_flight = True
        try:
            try:
                table = self.query_one(DataTable)
                saved_row = table.cursor_row
                saved_col = table.cursor_column
            except Exception:
                saved_row, saved_col = 0, 0
            try:
                sessions = await asyncio.to_thread(load_sessions)
                await asyncio.to_thread(detect_live_sessions, sessions)
            except Exception as exc:
                try:
                    self.query_one("#status", Static).update(
                        f"[red]auto-refresh error: {exc}[/red]"
                    )
                except Exception:
                    pass
                self._debug(f"auto_refresh load error: {exc}")
                return
            self.sessions = sessions
            self.last_refresh = time.time()
            self._refresh_count += 1
            self._populate()
            try:
                table = self.query_one(DataTable)
                max_row = max(0, table.row_count - 1)
                row = min(saved_row or 0, max_row)
                col = saved_col or 0
                table.move_cursor(row=row, column=col, animate=False)
            except Exception:
                pass
        except Exception as exc:
            try:
                self.query_one("#status", Static).update(
                    f"[red]auto-refresh error: {exc}[/red]"
                )
            except Exception:
                pass
            self._debug(f"auto_refresh outer error: {exc}")
        finally:
            self._refresh_in_flight = False

    async def action_refresh(self) -> None:
        # Offload to a worker so the manual refresh doesn't block the event
        # loop (and freeze the countdown ticker).
        if self._refresh_in_flight:
            return
        await self._do_auto_refresh()

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
            sec_key, sec_desc = keyfn, self.sort_desc
            ordered = sorted(self.sessions, key=keyfn, reverse=self.sort_desc)
        else:
            def _sort_key(s: Session):
                tier = 0 if s.is_live else (1 if s.is_recent else 2)
                ts = s.updated_at or datetime.fromtimestamp(s.mtime, tz=timezone.utc)
                return (tier, -ts.timestamp())
            sec_key, sec_desc = _sort_key, False
            ordered = sorted(self.sessions, key=_sort_key)
        # If grouping by repo, re-sort: primary = repo (empty last),
        # secondary = whatever sort was already applied (stable sort).
        if self.group_by_repo:
            ordered = sorted(ordered, key=lambda s: ((s.repository or "") == "",
                                                     (s.repository or "").lower()))
        # Live and recent counts always reflect the actual sessions, not order.
        # Pre-filter so we know the visible set (needed for group separators).
        visible: list[Session] = []
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
                pr_str = " ".join(f"#{n}" for n, _ in (s.prs or [])) or s.pr
                hay = " ".join((s.id, s.cwd, s.repository, s.branch, s.summary, pr_str)).lower()
                if needle not in hay:
                    continue
            visible.append(s)

        # Pre-compute per-group counts for the separator labels.
        group_counts: dict[str, int] = {}
        if self.group_by_repo:
            for s in visible:
                k = s.repository or "—"
                group_counts[k] = group_counts.get(k, 0) + 1

        last_repo: str | None = None
        self._separator_repos = {}
        for s in visible:
            if self.group_by_repo:
                repo = s.repository or "—"
                if repo != last_repo:
                    is_collapsed = repo in self.collapsed_repos
                    marker = "▶" if is_collapsed else "▼"
                    label = Text(f"{marker} {repo}  ({group_counts[repo]})",
                                 style="bold magenta")
                    table.add_row(label, "", "", "", "", "", "", "", "",
                                  key=f"__sep__{repo}__{len(self.row_keys)}")
                    self._separator_repos[len(self.row_keys)] = repo
                    self.row_keys.append("")  # sentinel: separator row
                    last_repo = repo
                if repo in self.collapsed_repos:
                    continue  # skip rows in collapsed groups
            repo_branch = s.repository or "—"
            if s.branch:
                repo_branch = f"{repo_branch} ({s.branch})" if s.repository else s.branch
            pr_cell: object = ""
            if s.prs:
                # Build a multi-line clickable Text with one PR per line.
                pr_text = Text()
                for i, (n, url) in enumerate(s.prs):
                    if i:
                        pr_text.append("\n")
                    label = f"#{n}"
                    if url:
                        pr_text.append(
                            label,
                            style=Style(
                                color="cyan",
                                underline=True,
                                meta={"@click": f"open_pr({url!r})"},
                            ),
                        )
                    else:
                        pr_text.append(label)
                pr_cell = pr_text
            elif s.pr:
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
            row_height = max(1, len(s.prs)) if s.prs else 1
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
                height=row_height,
                key=s.id,
            )
            self.row_keys.append(s.id)
        status = self.query_one("#status", Static)
        total = len(self.sessions)
        shown = sum(1 for k in self.row_keys if k)  # exclude separators
        suffix_bits = [f"{shown}/{total} sessions"]
        if live_count:
            suffix_bits.append(f"● {live_count} live")
        if recent_count:
            suffix_bits.append(f"○ {recent_count} recent")
        if hidden_empty:
            suffix_bits.append(f"({hidden_empty} empty hidden — press 'a')")
        if self.live_only:
            suffix_bits.append("[live only — press 'l']")
        if self.group_by_repo:
            suffix_bits.append("[grouped by repo — press 'g']")
        flags = []
        if self.config.get("yolo"):
            flags.append("yolo")
        if self.config.get("autopilot"):
            flags.append("autopilot")
        if flags:
            suffix_bits.append(f"launch: {'+'.join(flags)} (press 's')")
        else:
            suffix_bits.append("launch: default (press 's')")
        suffix_bits.append(f"root: {SESSION_ROOT}")
        self._status_suffix = "   ".join(suffix_bits)
        self._refresh_status_line()

    def _refresh_age(self) -> str:
        if not getattr(self, "last_refresh", 0):
            return "—"
        delta = max(0, int(time.time() - self.last_refresh))
        return f"{delta}s ago"

    def _next_refresh_in(self) -> int:
        if not getattr(self, "last_refresh", 0):
            return int(self.REFRESH_INTERVAL)
        remaining = self.REFRESH_INTERVAL - (time.time() - self.last_refresh)
        return max(0, int(remaining + 0.5))

    def _countdown_text(self) -> str:
        return f"autorefresh in {self._next_refresh_in()}s"

    def _refresh_status_line(self) -> None:
        """Update the bottom status bar + header subtitle with the live countdown.

        Cheap to call repeatedly (no table rebuild)."""
        try:
            suffix = getattr(self, "_status_suffix", "")
            text = f"{self._countdown_text()}   {suffix}" if suffix else self._countdown_text()
            self.query_one("#status", Static).update(text)
        except Exception:
            pass
        try:
            self.sub_title = self._countdown_text()
        except Exception:
            pass

    def _tick_countdown(self) -> None:
        # Refresh just the countdown bits — no table work.
        self._refresh_status_line()

    def action_toggle_empty(self) -> None:
        self.show_empty = not self.show_empty
        self._populate()

    def action_toggle_live(self) -> None:
        self.live_only = not self.live_only
        self._populate()

    def action_toggle_group(self) -> None:
        self.group_by_repo = not self.group_by_repo
        self._populate()

    def action_toggle_preview(self) -> None:
        self.show_preview = not self.show_preview
        self.config["show_preview"] = self.show_preview
        try:
            save_config(self.config)
        except Exception:
            pass
        self._apply_preview_visibility()
        if self.show_preview:
            # Force-refresh for the currently selected row.
            self._last_preview_sid = ""
            self._update_preview_for_cursor()

    def _apply_preview_visibility(self) -> None:
        try:
            preview = self.query_one("#preview", RichLog)
            preview.set_class(self.show_preview, "visible")
        except Exception:
            pass

    def _selected_session_for_preview(self) -> "Session | None":
        try:
            table = self.query_one(DataTable)
            row = table.cursor_row
            if row is None or row < 0 or row >= len(self.row_keys):
                return None
            sid = self.row_keys[row]
            if not sid:  # separator row
                return None
            return next((s for s in self.sessions if s.id == sid), None)
        except Exception:
            return None

    def _update_preview_for_cursor(self) -> None:
        if not self.show_preview:
            return
        sess = self._selected_session_for_preview()
        if sess is None:
            return
        self._update_preview_for_session(sess.id)

    def _update_preview_for_session(self, sid: str) -> None:
        if not self.show_preview or not sid:
            return
        sess = next((s for s in self.sessions if s.id == sid), None)
        if sess is None:
            return
        try:
            preview = self.query_one("#preview", RichLog)
        except Exception:
            return
        preview.clear()
        head = Text()
        head.append(f"{sess.short_id} ", style="bold")
        if sess.summary:
            head.append(sess.summary[:80], style="bold cyan")
        head.append("\n")
        if sess.cwd:
            head.append(f"  cwd: {sess.cwd}\n", style="dim")
        preview.write(head)
        try:
            body = _render_session_preview(sess.id, max_events=25)
        except Exception as exc:
            preview.write(Text(f"(preview error: {exc})", style="red"))
            return
        preview.write(body)

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
        where = cwd or os.path.expanduser("~")
        self.query_one("#status", Static).update(
            f"[yellow]⏳ launching new copilot session in {where}…[/yellow]"
        )
        self._run_launch(lambda: launch_new_session(cwd, cfg=self.config))

    def _run_launch(self, fn) -> None:
        """Run a blocking launch/focus call without freezing the UI.

        We yield to the event loop once so the spinner status update from the
        caller actually paints before we kick off the (potentially fast)
        worker — otherwise a sub-frame worker completion races the renderer
        and the user never sees the spinner.
        """
        async def _runner() -> None:
            # Force a paint of the spinner status before doing any work.
            await asyncio.sleep(0.05)
            try:
                ok, msg = await asyncio.to_thread(fn)
            except Exception as exc:  # pragma: no cover - defensive
                ok, msg = False, f"launch error: {exc}"
            text = msg if ok else f"[red]{msg}[/red]"
            try:
                self.query_one("#status", Static).update(text)
            except Exception:
                pass
        self.run_worker(_runner(), exclusive=False)

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

    def action_open_settings(self) -> None:
        def _on_close(result: dict | None) -> None:
            if not result:
                return
            self.config.update(result)
            save_config(self.config)
            # Re-arm the autorefresh timer if the interval changed.
            new_interval = max(2.0, float(self.config.get("refresh_interval", 30)))
            if new_interval != self.REFRESH_INTERVAL:
                self.REFRESH_INTERVAL = new_interval
                # Next auto-refresh tick will pick up the new interval. We
                # don't need to stop a timer because _schedule_auto_refresh
                # uses self-rescheduling set_timer calls.
                self._schedule_auto_refresh()
                # Reset countdown anchor.
                self.last_refresh = time.time()
            self._populate()
            self.query_one("#status", Static).update("→ settings saved")

        self.push_screen(SettingsScreen(self.config), _on_close)

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
            self._debug(f"_jump_row out-of-range idx={idx} len={len(self.row_keys)}")
            return
        sid = self.row_keys[idx]
        sess = next((s for s in self.sessions if s.id == sid), None)
        if sess is None:
            self._debug(f"_jump_row no session for sid={sid}")
            return
        self._debug(
            f"_jump_row idx={idx} sid={sid[:8]} short={sess.short_id} "
            f"summary={(sess.summary or '')[:40]!r} pid={sess.pid} live={sess.is_live}"
        )
        status = self.query_one("#status", Static)
        label = (sess.summary or sess.short_id).strip() or sess.short_id
        if sess.is_live:
            status.update(f"[yellow]⏳ focusing tab for {label}…[/yellow]")
        else:
            status.update(f"[yellow]⏳ launching {label}…[/yellow]")

        def _do_jump() -> tuple[bool, str]:
            # Fast targeted live-check (re-validates cached pid + cheap
            # cmdline scan). Avoids the full process_iter + open_files sweep
            # that detect_live_sessions does — that's the main source of the
            # multi-second delay when clicking a row.
            _quick_live_check(sess)
            ok, msg = focus_session(sess)
            if ok:
                return ok, msg
            # No existing tab (or focus failed) — launch a fresh one.
            return launch_session_tab(sess, cfg=self.config)

        self._run_launch(_do_jump)

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
            if sess:
                # Per-link @click meta on each PR `Text` segment dispatches
                # `action_open_pr(url)` for the specific PR clicked. Here in
                # the cell-selected fallback (keyboard Enter, or a click on
                # blank space inside the cell) we open just one PR — the
                # highest-numbered one — rather than ALL of them, otherwise
                # mouse clicks on a single link end up firing both this
                # handler AND the per-link action and the user gets every
                # PR launched.
                url = sess.pr_url or (sess.prs[-1][1] if sess.prs else "")
                if url:
                    import webbrowser
                    webbrowser.open(url)
                    self.query_one("#status", Static).update(f"→ opened {url}")
                    return
        self._jump_row(row_idx)

    def _toggle_collapse(self, repo: str, force: bool | None = None) -> None:
        """Toggle (or set) collapsed state for a repo group, then re-render."""
        if force is True or (force is None and repo not in self.collapsed_repos):
            self.collapsed_repos.add(repo)
        else:
            self.collapsed_repos.discard(repo)
        self._populate()
        try:
            table = self.query_one(DataTable)
            new_idx = next((i for i, r in self._separator_repos.items() if r == repo), 0)
            table.move_cursor(row=new_idx, column=0, animate=False)
        except Exception:
            pass

    def on_key(self, event) -> None:
        """Intercept left/right/space on a repo separator row to toggle collapse."""
        if not self.group_by_repo:
            return
        if event.key not in ("right", "left", "space"):
            return
        try:
            table = self.query_one(DataTable)
            row_idx = table.cursor_row
        except Exception:
            return
        repo = self._separator_repos.get(row_idx)
        if repo is None:
            return  # not a separator row — let default cursor movement happen
        if event.key == "right":
            self._toggle_collapse(repo, force=True)
        elif event.key == "left":
            self._toggle_collapse(repo, force=False)
        else:  # space
            self._toggle_collapse(repo)
        event.stop()
        try:
            event.prevent_default()
        except Exception:
            pass

    def _row_index_from_key(self, key) -> int | None:
        """Resolve a Textual DataTable row key to our logical row index.

        Variable-height rows in Textual can desync the integer
        `coordinate.row` from our `self.row_keys` order. Always prefer the
        explicit row key (which we set to the session id) when available.
        """
        if key is None:
            return None
        # Textual wraps keys in a RowKey object whose .value is the original
        # str we passed to add_row. Some Textual versions just return the str.
        val = getattr(key, "value", key)
        if val is None:
            return None
        if isinstance(val, str):
            if val.startswith("__sep__"):
                # Separator row — find by reverse lookup in the separator map.
                for idx, repo in self._separator_repos.items():
                    if val == f"__sep__{repo}__{idx}":
                        return idx
                return None
            try:
                return self.row_keys.index(val)
            except ValueError:
                return None
        return None

    def on_data_table_cell_highlighted(self, event) -> None:
        # Update the preview pane when the cursor moves to a different row.
        # Resolve the row via row_key first to avoid the variable-row-height
        # offset bug; fall back to coordinate.row.
        try:
            cell_key = getattr(event, "cell_key", None)
            row_key = getattr(cell_key, "row_key", None) if cell_key else None
            idx = self._row_index_from_key(row_key)
            if idx is None:
                coord = getattr(event, "coordinate", None)
                idx = coord.row if coord is not None else None
            if idx is None:
                return
            sid = self.row_keys[idx] if 0 <= idx < len(self.row_keys) else ""
            if sid and sid != self._last_preview_sid:
                self._last_preview_sid = sid
                if self.show_preview:
                    self._update_preview_for_session(sid)
        except Exception:
            pass

    def on_data_table_cell_selected(self, event) -> None:
        # Prefer row_key for hit-testing — variable-height rows desync the
        # integer coordinate from our row_keys order.
        cell_key = getattr(event, "cell_key", None)
        row_key = getattr(cell_key, "row_key", None) if cell_key else None
        col_key = getattr(cell_key, "column_key", None) if cell_key else None
        row_via_key = self._row_index_from_key(row_key)
        coord = getattr(event, "coordinate", None)
        coord_row = coord.row if coord is not None else None
        row = row_via_key if row_via_key is not None else coord_row
        # Column index: try the column key first, then coordinate.column.
        col = None
        if col_key is not None:
            try:
                col_keys = getattr(self, "col_keys", [])
                col_val = getattr(col_key, "value", col_key)
                for i, k in enumerate(col_keys):
                    kv = getattr(k, "value", k)
                    if kv == col_val or k == col_key:
                        col = i
                        break
            except Exception:
                col = None
        if col is None and coord is not None:
            col = coord.column
        if row is None:
            try:
                table = self.query_one(DataTable)
                row, col = table.cursor_row, table.cursor_column
            except Exception:
                return
        # If this is a repo separator row, toggle its collapsed state.
        if self.group_by_repo and row in self._separator_repos:
            self._toggle_collapse(self._separator_repos[row])
            return
        try:
            sid = self.row_keys[row] if 0 <= row < len(self.row_keys) else "(out-of-range)"
            sess = next((s for s in self.sessions if s.id == sid), None)
            label = (sess.summary or sess.short_id)[:50] if sess else "(none)"
            self._debug(
                f"cell_selected row_via_key={row_via_key} coord_row={coord_row} "
                f"chosen_row={row} col={col} sid={sid[:8] if sid else ''} → {label!r}"
            )
        except Exception:
            pass
        self._activate(row, col)

    def on_click(self, event) -> None:
        # Single click on PR cells is handled by the @click meta on the Text.
        # Single click on a repo separator row toggles collapse.
        # Double click on a session row → activate (jump).
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
        chain = getattr(event, "chain", 1)
        row = table.cursor_row
        # Single-click on a separator row toggles collapse for that group.
        if self.group_by_repo and row in self._separator_repos:
            self._toggle_collapse(self._separator_repos[row])
            return
        if chain < 2:
            return
        self._activate(row, table.cursor_column)

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
