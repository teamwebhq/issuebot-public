"""Tests for the per-task live-feed Reporter."""

from __future__ import annotations

import io

from issuebot.events import AgentEvent
from issuebot.reporter import (
    ConsoleReporter,
    NullReporter,
    default_log_dir,
    stall_message,
)


def _counter():
    """A monotonic-style clock that ticks 1.0 per call."""
    n = {"t": 0.0}

    def clock() -> float:
        n["t"] += 1.0
        return n["t"]

    return clock


def _make(stream: io.StringIO, tmp_path, **kw) -> ConsoleReporter:
    """Build a ConsoleReporter with the stall-thread disabled for determinism."""
    r = ConsoleReporter(ref="ISS-1", stream=stream, log_dir=tmp_path, clock=_counter(), **kw)
    r._stall_enabled = False  # keep tests free of background-thread timing
    return r


def test_start_creates_single_log_file_and_prints_folder(tmp_path):
    stream = io.StringIO()
    r = _make(stream, tmp_path)
    r.start("ISS-1", "/work/dir")

    logs = list(tmp_path.glob("ISS-1-*.jsonl"))
    assert len(logs) == 1

    out = stream.getvalue()
    assert "ISS-1" in out
    assert "/work/dir" in out
    assert "log:" in out
    assert str(logs[0]) in out


def test_event_renders_icon_and_summary(tmp_path):
    stream = io.StringIO()
    r = _make(stream, tmp_path)
    r.start("ISS-1", "/work/dir")
    stream.truncate(0)
    stream.seek(0)

    r.event(AgentEvent("tool_use", "Edit: /tmp/x"))
    out = stream.getvalue()
    assert "\U0001f527" in out  # wrench icon
    assert "Edit: /tmp/x" in out


def test_show_prefix_prepends_ref(tmp_path):
    stream = io.StringIO()
    r = _make(stream, tmp_path, show_prefix=True)
    r.start("ISS-1", "/work/dir")
    r.event(AgentEvent("text", "hello"))

    for line in stream.getvalue().splitlines():
        if line:
            assert line.startswith("[ISS-1] ")


def test_raw_writes_lines_to_log(tmp_path):
    stream = io.StringIO()
    r = _make(stream, tmp_path)
    r.start("ISS-1", "/work/dir")
    r.raw('{"type":"system"}\n')
    r.raw('{"type":"result"}')
    r.finish("done", 3.0)

    log = next(tmp_path.glob("ISS-1-*.jsonl"))
    contents = log.read_text().splitlines()
    assert '{"type":"system"}' in contents
    assert '{"type":"result"}' in contents


def test_finish_prints_status(tmp_path):
    stream = io.StringIO()
    r = _make(stream, tmp_path)
    r.start("ISS-1", "/work/dir")
    stream.truncate(0)
    stream.seek(0)
    r.finish("done", 5.0)
    out = stream.getvalue()
    assert "ISS-1" in out
    assert "done" in out
    assert "✓" in out  # check mark for success


def test_finish_failure_uses_cross(tmp_path):
    stream = io.StringIO()
    r = _make(stream, tmp_path)
    r.start("ISS-1", "/work/dir")
    stream.truncate(0)
    stream.seek(0)
    r.finish("failed", 5.0)
    assert "✗" in stream.getvalue()  # cross mark


def test_null_reporter_is_noop():
    r = NullReporter()
    # None of these should raise or produce output.
    r.start("ISS-1", "/x")
    r.event(AgentEvent("text", "hi"))
    r.raw("line")
    r.finish("done", 1.0)


def test_default_log_dir_respects_xdg(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    d = default_log_dir()
    assert str(d).startswith(str(tmp_path))
    assert d.name == "logs"
    assert "issuebot" in str(d)


def test_default_log_dir_falls_back_to_local_state(monkeypatch):
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    d = default_log_dir()
    assert ".local/state/issuebot/logs" in str(d)


def test_stall_message_none_under_threshold():
    assert stall_message("ISS-1", 10.0, 30.0) is None
    assert stall_message("ISS-1", 59.9, 100.0) is None


def test_stall_message_warns_at_threshold():
    msg = stall_message("ISS-1", 60.0, 120.0)
    assert msg is not None
    assert "ISS-1" in msg
    assert "60" in msg  # idle seconds
    assert "120" in msg  # elapsed seconds


def test_feed_lines_are_mirrored_to_agent_state(tmp_path):
    """The human-readable feed (start/event/finish) is mirrored into AgentState's
    log tail so the dashboard shows what the agent is doing."""
    import io as _io

    from issuebot.agent_state import AgentState

    state = AgentState()
    stream = _io.StringIO()
    r = _make(stream, tmp_path, agent_state=state)
    r.start("ISS-1", "/work/dir")
    r.event(AgentEvent("text", "doing the thing"))
    r.event(AgentEvent("tool_use", "Edit: /tmp/x"))

    tail = state.snapshot().log_tail
    assert "doing the thing" in tail
    assert "Edit: /tmp/x" in tail
