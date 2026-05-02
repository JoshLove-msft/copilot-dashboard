"""Unit tests for module-level pure functions in dashboard.py.

These tests intentionally avoid spinning up the Textual App; they exercise
the helpers that don't depend on a running event loop. The dashboard's
filesystem-dependent paths (config, PR-watch state, session events) are
redirected via monkeypatch to a tmp dir so the tests don't touch real
user state.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import dashboard as D


# --------------------------------------------------------------------------- #
# truncate
# --------------------------------------------------------------------------- #
class TestTruncate:
    def test_returns_input_when_short_enough(self):
        assert D.truncate("hello", 10) == "hello"

    def test_exact_length_unchanged(self):
        assert D.truncate("hello", 5) == "hello"

    def test_truncates_with_ellipsis(self):
        out = D.truncate("hello world", 6)
        assert out.endswith("…")
        assert len(out) == 6

    def test_empty_string(self):
        assert D.truncate("", 5) == ""


# --------------------------------------------------------------------------- #
# _summarize_tool_args
# --------------------------------------------------------------------------- #
class TestSummarizeToolArgs:
    def test_returns_empty_for_non_dict(self):
        assert D._summarize_tool_args("any", None) == ""
        assert D._summarize_tool_args("any", "string") == ""
        assert D._summarize_tool_args("any", [1, 2]) == ""

    def test_prefers_command_field(self):
        out = D._summarize_tool_args("powershell", {"command": "ls", "description": "list"})
        assert "ls" in out

    def test_prefers_path_when_command_missing(self):
        out = D._summarize_tool_args("view", {"path": "/x/y.py", "view_range": [1, 10]})
        assert "/x/y.py" in out

    def test_replaces_newlines_in_value(self):
        out = D._summarize_tool_args("x", {"command": "line1\nline2"})
        assert "\n" not in out

    def test_truncates_long_values(self):
        long = "a" * 300
        out = D._summarize_tool_args("x", {"command": long})
        assert len(out) <= 140

    def test_falls_back_to_first_string_field(self):
        out = D._summarize_tool_args("custom", {"weird_field": "  hello  "})
        # Falls back to "key=value" form.
        assert out.startswith("weird_field=")
        assert "hello" in out

    def test_skips_non_string_fields(self):
        # No string-valued fields ⇒ empty.
        assert D._summarize_tool_args("x", {"count": 5, "ok": True}) == ""


# --------------------------------------------------------------------------- #
# humanize_age
# --------------------------------------------------------------------------- #
class TestHumanizeAge:
    def test_none_returns_question_mark(self):
        assert D.humanize_age(None) == "?"

    def test_seconds(self):
        dt = datetime.now(timezone.utc) - timedelta(seconds=12)
        assert D.humanize_age(dt).endswith("s ago")

    def test_minutes(self):
        dt = datetime.now(timezone.utc) - timedelta(minutes=5)
        assert D.humanize_age(dt) == "5m ago"

    def test_hours(self):
        dt = datetime.now(timezone.utc) - timedelta(hours=3)
        assert D.humanize_age(dt) == "3h ago"

    def test_days(self):
        dt = datetime.now(timezone.utc) - timedelta(days=4)
        assert D.humanize_age(dt) == "4d ago"

    def test_months(self):
        dt = datetime.now(timezone.utc) - timedelta(days=70)
        assert D.humanize_age(dt).endswith("mo ago")

    def test_years(self):
        dt = datetime.now(timezone.utc) - timedelta(days=400)
        assert D.humanize_age(dt).endswith("y ago")


# --------------------------------------------------------------------------- #
# _parse_dt
# --------------------------------------------------------------------------- #
class TestParseDt:
    def test_none(self):
        assert D._parse_dt(None) is None
        assert D._parse_dt("") is None

    def test_iso_z(self):
        out = D._parse_dt("2024-01-02T03:04:05Z")
        assert out is not None
        assert out.tzinfo is not None
        assert out.year == 2024 and out.hour == 3

    def test_iso_offset(self):
        out = D._parse_dt("2024-01-02T03:04:05+00:00")
        assert out is not None and out.year == 2024

    def test_naive_datetime_gets_utc(self):
        out = D._parse_dt(datetime(2024, 1, 1))
        assert out is not None and out.tzinfo == timezone.utc

    def test_invalid_returns_none(self):
        assert D._parse_dt("not-a-date") is None


# --------------------------------------------------------------------------- #
# copilot_command
# --------------------------------------------------------------------------- #
class TestCopilotCommand:
    def test_default_no_flags(self):
        assert D.copilot_command({}) == "copilot"

    def test_yolo_only(self):
        assert D.copilot_command({"yolo": True}) == "copilot --yolo"

    def test_autopilot_only(self):
        assert D.copilot_command({"autopilot": True}) == "copilot --autopilot"

    def test_both_flags(self):
        cmd = D.copilot_command({"yolo": True, "autopilot": True})
        assert cmd == "copilot --yolo --autopilot"

    def test_with_resume(self):
        cmd = D.copilot_command({"yolo": True}, resume_id="abc-123")
        assert cmd == 'copilot --yolo --resume="abc-123"'


# --------------------------------------------------------------------------- #
# load_config / save_config
# --------------------------------------------------------------------------- #
class TestConfigRoundtrip:
    def test_load_returns_defaults_when_missing(self, tmp_path, monkeypatch):
        monkeypatch.setattr(D, "CONFIG_PATH", tmp_path / "config.json")
        cfg = D.load_config()
        assert cfg == D.DEFAULT_CONFIG
        # Returned dict must be a copy — modifying it must not mutate the default.
        cfg["yolo"] = "tampered"
        assert D.DEFAULT_CONFIG["yolo"] is True

    def test_save_then_load_preserves_known_keys(self, tmp_path, monkeypatch):
        monkeypatch.setattr(D, "CONFIG_PATH", tmp_path / "config.json")
        cfg = D.load_config()
        cfg["yolo"] = False
        cfg["refresh_interval"] = 99
        D.save_config(cfg)
        reloaded = D.load_config()
        assert reloaded["yolo"] is False
        assert reloaded["refresh_interval"] == 99

    def test_load_ignores_unknown_keys(self, tmp_path, monkeypatch):
        cfg_path = tmp_path / "config.json"
        cfg_path.write_text(json.dumps({"yolo": False, "bogus_key": 1}))
        monkeypatch.setattr(D, "CONFIG_PATH", cfg_path)
        cfg = D.load_config()
        assert cfg["yolo"] is False
        assert "bogus_key" not in cfg

    def test_load_tolerates_garbage_file(self, tmp_path, monkeypatch):
        cfg_path = tmp_path / "config.json"
        cfg_path.write_text("not json {{{")
        monkeypatch.setattr(D, "CONFIG_PATH", cfg_path)
        cfg = D.load_config()
        # Falls back to defaults silently.
        assert cfg == D.DEFAULT_CONFIG


# --------------------------------------------------------------------------- #
# PR-watch state
# --------------------------------------------------------------------------- #
class TestPrWatchState:
    def test_load_missing_returns_empty_dict(self, tmp_path, monkeypatch):
        monkeypatch.setattr(D, "PR_WATCH_STATE_PATH", tmp_path / "state.json")
        assert D._load_pr_watch_state() == {}

    def test_save_then_load(self, tmp_path, monkeypatch):
        monkeypatch.setattr(D, "PR_WATCH_STATE_PATH", tmp_path / "state.json")
        D._save_pr_watch_state({"https://x/1": {"fingerprint": "ci/build"}})
        out = D._load_pr_watch_state()
        assert out == {"https://x/1": {"fingerprint": "ci/build"}}

    def test_load_garbage_returns_empty(self, tmp_path, monkeypatch):
        path = tmp_path / "state.json"
        path.write_text("[]")  # valid JSON but wrong type
        monkeypatch.setattr(D, "PR_WATCH_STATE_PATH", path)
        assert D._load_pr_watch_state() == {}


# --------------------------------------------------------------------------- #
# _pr_failed_checks
# --------------------------------------------------------------------------- #
class TestPrFailedChecks:
    def test_returns_none_when_gh_returns_none(self, monkeypatch):
        monkeypatch.setattr(D, "_run_gh_json", lambda args, timeout=30.0: None)
        assert D._pr_failed_checks("https://x/1") is None

    def test_returns_none_when_gh_returns_non_list(self, monkeypatch):
        monkeypatch.setattr(D, "_run_gh_json", lambda args, timeout=30.0: {"err": "x"})
        assert D._pr_failed_checks("https://x/1") is None

    def test_filters_to_fail_bucket(self, monkeypatch):
        data = [
            {"name": "ci/build", "bucket": "fail", "state": "FAILURE"},
            {"name": "ci/lint", "bucket": "pass", "state": "SUCCESS"},
            {"name": "ci/test", "bucket": "FAIL", "state": "FAILURE"},  # case-insensitive
            {"name": "ci/skip", "bucket": "skipping", "state": ""},
        ]
        monkeypatch.setattr(D, "_run_gh_json", lambda args, timeout=30.0: data)
        out = D._pr_failed_checks("https://x/1")
        assert sorted(out) == ["ci/build", "ci/test"]

    def test_returns_empty_list_when_no_failures(self, monkeypatch):
        monkeypatch.setattr(
            D, "_run_gh_json",
            lambda args, timeout=30.0: [{"name": "ok", "bucket": "pass"}],
        )
        assert D._pr_failed_checks("https://x/1") == []


# --------------------------------------------------------------------------- #
# poll_pr_watches — the meat of the PR-watch feature
# --------------------------------------------------------------------------- #
def _watch(**kw):
    base = {"name": "w", "query": "is:pr is:open", "only_failing": False}
    base.update(kw)
    return base


def _pr(num, title, *, repo="o/r"):
    return {
        "number": num,
        "url": f"https://x/{num}",
        "title": title,
        "repository": {"nameWithOwner": repo},
    }


class TestPollPrWatches:
    def test_skips_watch_with_empty_query(self, monkeypatch):
        monkeypatch.setattr(D, "_run_gh_json", lambda *a, **k: pytest.fail("should not call gh"))
        items = D.poll_pr_watches([{"name": "x", "query": ""}], {})
        assert items == []

    def test_skips_non_dict_entries(self, monkeypatch):
        monkeypatch.setattr(D, "_run_gh_json", lambda *a, **k: [])
        items = D.poll_pr_watches([None, "x", _watch()], {})
        assert items == []

    def test_returns_only_latest_when_no_filters_and_only_failing_false(self, monkeypatch):
        # Watcher is single-PR-per-cycle: highest PR number wins.
        monkeypatch.setattr(D, "_run_gh_json",
                            lambda args, timeout=30.0: [_pr(1, "fix"), _pr(2, "feat")])
        items = D.poll_pr_watches([_watch(only_failing=False)], {})
        assert [i["number"] for i in items] == [2]
        assert items[0]["fingerprint"] == "any"

    def test_title_contains_filter_case_insensitive(self, monkeypatch):
        monkeypatch.setattr(D, "_run_gh_json",
                            lambda args, timeout=30.0: [_pr(1, "FIX bug"), _pr(2, "feat")])
        items = D.poll_pr_watches([_watch(title_contains="fix")], {})
        # Only PR 1 matches the filter, so only PR 1 is returned.
        assert [i["number"] for i in items] == [1]

    def test_title_pattern_filter_picks_latest_matching(self, monkeypatch):
        monkeypatch.setattr(
            D, "_run_gh_json",
            lambda args, timeout=30.0: [_pr(1, "fix: a"), _pr(2, "chore: b"), _pr(3, "feat: c")],
        )
        # Two match the pattern; keep only the latest (highest number).
        items = D.poll_pr_watches([_watch(title_pattern=r"^(fix|chore):")], {})
        assert [i["number"] for i in items] == [2]

    def test_invalid_pattern_skips_watch(self, monkeypatch):
        called = {"n": 0}

        def fake(args, timeout=30.0):
            called["n"] += 1
            return []

        monkeypatch.setattr(D, "_run_gh_json", fake)
        items = D.poll_pr_watches([_watch(title_pattern="[invalid")], {})
        # Bad regex skips the entire watch BEFORE the gh call.
        assert items == []
        assert called["n"] == 0

    def test_only_failing_drops_clean_prs(self, monkeypatch):
        # Latest is #2 — its checks are clean ⇒ skipped (and we don't
        # fall back to older PRs; "latest only" means latest only).
        monkeypatch.setattr(D, "_run_gh_json",
                            lambda args, timeout=30.0: [_pr(1, "x"), _pr(2, "y")])

        def fake_checks(url):
            return ["ci/build"] if url.endswith("/1") else []

        monkeypatch.setattr(D, "_pr_failed_checks", fake_checks)
        items = D.poll_pr_watches([_watch(only_failing=True)], {})
        assert items == []

    def test_only_failing_drops_when_check_query_returns_none(self, monkeypatch):
        # `None` from _pr_failed_checks means "no info" — skip.
        monkeypatch.setattr(D, "_run_gh_json",
                            lambda args, timeout=30.0: [_pr(1, "x")])
        monkeypatch.setattr(D, "_pr_failed_checks", lambda url: None)
        items = D.poll_pr_watches([_watch(only_failing=True)], {})
        assert items == []

    def test_dedup_by_fingerprint(self, monkeypatch):
        monkeypatch.setattr(D, "_run_gh_json",
                            lambda args, timeout=30.0: [_pr(1, "x")])
        monkeypatch.setattr(D, "_pr_failed_checks", lambda url: ["ci/build"])
        state = {"https://x/1": {"fingerprint": "ci/build"}}
        items = D.poll_pr_watches([_watch(only_failing=True)], state)
        assert items == []

    def test_relaunches_when_failure_set_changes(self, monkeypatch):
        monkeypatch.setattr(D, "_run_gh_json",
                            lambda args, timeout=30.0: [_pr(1, "x")])
        monkeypatch.setattr(D, "_pr_failed_checks", lambda url: ["ci/build", "ci/lint"])
        state = {"https://x/1": {"fingerprint": "ci/build"}}
        items = D.poll_pr_watches([_watch(only_failing=True)], state)
        assert len(items) == 1
        assert items[0]["fingerprint"] == "ci/build|ci/lint"

    def test_combined_filters_all_must_match(self, monkeypatch):
        monkeypatch.setattr(
            D, "_run_gh_json",
            lambda args, timeout=30.0: [
                _pr(1, "Update foo version"),
                _pr(2, "Update bar version"),
                _pr(3, "Other"),
            ],
        )
        items = D.poll_pr_watches([_watch(
            only_failing=False,
            title_contains="update",
            title_pattern=r"foo",
        )], {})
        assert [i["number"] for i in items] == [1]

    def test_picks_latest_per_watch_independently(self, monkeypatch):
        # Two watches in the same poll cycle — each should yield ONE PR
        # (its respective latest), not be merged.
        prs_by_query = {
            "watch-a": [_pr(10, "A v1"), _pr(11, "A v2"), _pr(12, "A v3")],
            "watch-b": [_pr(20, "B v1"), _pr(25, "B v2")],
        }

        def fake(args, timeout=30.0):
            # args = ['search', 'prs', <query>, ...]
            q = args[2]
            return prs_by_query.get(q, [])

        monkeypatch.setattr(D, "_run_gh_json", fake)
        items = D.poll_pr_watches([
            _watch(name="A", query="watch-a", only_failing=False),
            _watch(name="B", query="watch-b", only_failing=False),
        ], {})
        assert sorted(i["number"] for i in items) == [12, 25]

    def test_latest_only_false_returns_all_matches(self, monkeypatch):
        monkeypatch.setattr(
            D, "_run_gh_json",
            lambda args, timeout=30.0: [_pr(1, "a"), _pr(2, "b"), _pr(3, "c")],
        )
        items = D.poll_pr_watches(
            [_watch(only_failing=False, latest_only=False)], {}
        )
        assert sorted(i["number"] for i in items) == [1, 2, 3]

    def test_repo_extraction(self, monkeypatch):
        monkeypatch.setattr(
            D, "_run_gh_json",
            lambda args, timeout=30.0: [_pr(1, "x", repo="myorg/myrepo")],
        )
        items = D.poll_pr_watches([_watch(only_failing=False)], {})
        assert items[0]["repo"] == "myorg/myrepo"


# --------------------------------------------------------------------------- #
# _first_user_message
# --------------------------------------------------------------------------- #
class TestFirstUserMessage:
    def test_missing_file(self, tmp_path):
        assert D._first_user_message(tmp_path / "missing.jsonl") == ""

    def test_picks_first_user_message(self, tmp_path):
        p = tmp_path / "events.jsonl"
        lines = [
            json.dumps({"type": "tool.execution_start", "data": {"toolName": "x"}}),
            json.dumps({"type": "user.message", "data": {"content": "Hi there\nLine 2"}}),
            json.dumps({"type": "user.message", "data": {"content": "Second"}}),
        ]
        p.write_text("\n".join(lines) + "\n", encoding="utf-8")
        # Multi-line content collapsed to spaces, returns the FIRST user msg.
        assert D._first_user_message(p) == "Hi there Line 2"

    def test_skips_garbage_lines(self, tmp_path):
        p = tmp_path / "events.jsonl"
        p.write_text(
            "not-json\n"
            + json.dumps({"type": "user.message", "data": {"content": "ok"}})
            + "\n",
            encoding="utf-8",
        )
        assert D._first_user_message(p) == "ok"

    def test_no_user_messages(self, tmp_path):
        p = tmp_path / "events.jsonl"
        p.write_text(
            json.dumps({"type": "assistant.message", "data": {"content": "yo"}}) + "\n",
            encoding="utf-8",
        )
        assert D._first_user_message(p) == ""


# --------------------------------------------------------------------------- #
# _render_session_preview
# --------------------------------------------------------------------------- #
class TestRenderSessionPreview:
    def _setup_session(self, tmp_path, monkeypatch, lines):
        sid = "abc-123"
        sess_dir = tmp_path / sid
        sess_dir.mkdir(parents=True)
        (sess_dir / "events.jsonl").write_text(
            "\n".join(json.dumps(l) for l in lines) + "\n", encoding="utf-8"
        )
        monkeypatch.setattr(D, "SESSION_ROOT", tmp_path)
        return sid

    def test_no_events_file(self, tmp_path, monkeypatch):
        monkeypatch.setattr(D, "SESSION_ROOT", tmp_path)
        text = D._render_session_preview("nope")
        assert "no events" in text.plain.lower()

    def test_renders_user_assistant_and_tools(self, tmp_path, monkeypatch):
        sid = self._setup_session(tmp_path, monkeypatch, [
            {"type": "user.message", "data": {"content": "hello"}},
            {"type": "tool.execution_start",
             "data": {"toolName": "view", "arguments": {"path": "/x.py"}}},
            {"type": "tool.execution_complete",
             "data": {"toolName": "view", "success": True}},
            {"type": "assistant.message", "data": {"content": "world"}},
            {"type": "tool.execution_complete",
             "data": {"toolName": "broken", "success": False,
                      "result": {"error": "boom"}}},
        ])
        text = D._render_session_preview(sid)
        plain = text.plain
        assert "user" in plain and "hello" in plain
        assert "assistant" in plain and "world" in plain
        assert "view" in plain and "/x.py" in plain
        assert "broken" in plain and "boom" in plain

    def test_caps_to_max_events(self, tmp_path, monkeypatch):
        events = [
            {"type": "user.message", "data": {"content": f"msg-{i}"}}
            for i in range(50)
        ]
        sid = self._setup_session(tmp_path, monkeypatch, events)
        text = D._render_session_preview(sid, max_events=5)
        plain = text.plain
        assert "msg-49" in plain  # last is kept
        assert "msg-45" in plain  # 5th-from-last is kept
        assert "msg-44" not in plain  # 6th-from-last is dropped

    def test_filters_out_unknown_event_types(self, tmp_path, monkeypatch):
        sid = self._setup_session(tmp_path, monkeypatch, [
            {"type": "session.start", "data": {}},
            {"type": "intent", "data": {"text": "doing stuff"}},
        ])
        text = D._render_session_preview(sid)
        assert "no recent activity" in text.plain.lower()


# --------------------------------------------------------------------------- #
# Misc
# --------------------------------------------------------------------------- #
class TestMisc:
    def test_resolve_shell_returns_string(self):
        # We don't assert the exact value (depends on host), only that it
        # picks something usable.
        out = D._resolve_shell()
        assert isinstance(out, str) and out
        assert "powershell" in out.lower() or "pwsh" in out.lower()

    def test_load_sessions_empty_dir(self, tmp_path):
        assert D.load_sessions(tmp_path) == []

    def test_load_sessions_skips_non_session_dirs(self, tmp_path):
        # No workspace.yaml ⇒ not a session.
        (tmp_path / "stray").mkdir()
        (tmp_path / "stray" / "garbage.txt").write_text("x")
        assert D.load_sessions(tmp_path) == []


# --------------------------------------------------------------------------- #
# _scan_events_for_prs / _attach_store_data fallback for fresh sessions
# --------------------------------------------------------------------------- #
class TestScanEventsForPrs:
    def test_missing_file_returns_empty(self, tmp_path):
        assert D._scan_events_for_prs(tmp_path / "missing.jsonl") == {}

    def test_extracts_unique_pr_urls(self, tmp_path):
        p = tmp_path / "events.jsonl"
        p.write_text(
            "noise\n"
            'something https://github.com/Azure/azure-sdk-for-net/pull/58941 in here\n'
            'mention https://github.com/microsoft/typespec/pull/10464 again\n'
            # Repeated number — first URL wins.
            'https://github.com/other/repo/pull/58941 should not overwrite\n',
            encoding="utf-8",
        )
        out = D._scan_events_for_prs(p)
        assert out[58941] == "https://github.com/Azure/azure-sdk-for-net/pull/58941"
        assert out[10464] == "https://github.com/microsoft/typespec/pull/10464"

    def test_ignores_lines_without_pull(self, tmp_path):
        p = tmp_path / "events.jsonl"
        p.write_text("https://github.com/x/y/issues/1\n", encoding="utf-8")
        assert D._scan_events_for_prs(p) == {}


class TestAttachStoreDataFallback:
    """Fresh PR-watch sessions aren't in session-store.db yet — make sure
    we still surface their PR by scanning events.jsonl directly."""

    def _make_session(self, tmp_path, sid, repo, events_lines):
        sess_dir = tmp_path / sid
        sess_dir.mkdir(parents=True)
        ev = sess_dir / "events.jsonl"
        ev.write_text("\n".join(events_lines) + "\n", encoding="utf-8")
        s = D.Session(
            id=sid, cwd=str(sess_dir), repository=repo, branch="",
            summary="", created_at=None, updated_at=None,
            mtime=ev.stat().st_mtime, events_mtime=ev.stat().st_mtime,
        )
        return s

    def test_pr_populated_from_events_when_db_missing(self, tmp_path, monkeypatch):
        monkeypatch.setattr(D, "SESSION_ROOT", tmp_path)
        # Point at a non-existent DB so the DB branch is skipped.
        monkeypatch.setattr(D, "SESSION_STORE_DB", tmp_path / "missing.db")
        sess = self._make_session(
            tmp_path, "abc-1", "Azure/azure-sdk-for-net",
            ['{"type":"user.message","data":{"content":"monitor pr https://github.com/Azure/azure-sdk-for-net/pull/58941"}}'],
        )
        D._attach_store_data([sess])
        assert sess.pr == "#58941"
        assert sess.pr_url == "https://github.com/Azure/azure-sdk-for-net/pull/58941"
        assert sess.prs == [(58941, "https://github.com/Azure/azure-sdk-for-net/pull/58941")]

    def test_no_prs_when_events_have_none(self, tmp_path, monkeypatch):
        monkeypatch.setattr(D, "SESSION_ROOT", tmp_path)
        monkeypatch.setattr(D, "SESSION_STORE_DB", tmp_path / "missing.db")
        sess = self._make_session(
            tmp_path, "abc-2", "Azure/azure-sdk-for-net",
            ['{"type":"user.message","data":{"content":"hello"}}'],
        )
        D._attach_store_data([sess])
        assert sess.pr == ""
        assert sess.prs == []

    def test_multiple_prs_sorted_top_is_highest(self, tmp_path, monkeypatch):
        monkeypatch.setattr(D, "SESSION_ROOT", tmp_path)
        monkeypatch.setattr(D, "SESSION_STORE_DB", tmp_path / "missing.db")
        sess = self._make_session(
            tmp_path, "abc-3", "o/r",
            [
                '{"data":{"content":"see https://github.com/o/r/pull/100"}}',
                '{"data":{"content":"and https://github.com/o/r/pull/250"}}',
                '{"data":{"content":"and https://github.com/o/r/pull/175"}}',
            ],
        )
        D._attach_store_data([sess])
        assert [n for n, _ in sess.prs] == [100, 175, 250]
        assert sess.pr == "#250"
