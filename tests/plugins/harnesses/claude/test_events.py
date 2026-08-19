"""Tests for this harness's stream-json → AgentEvent parser.

Lives with the plugin because every line here is a key one vendor's CLI chose to
emit; core's own event tests are about the shape, not the wire format.
"""

from __future__ import annotations

import json

from issuebot.events import AgentEvent
from issuebot.plugins.harnesses.claude.events import parse_stream_json_line


def _assistant(*blocks: dict) -> str:
    return json.dumps({"type": "assistant", "message": {"content": list(blocks)}})


def test_text_block_becomes_text_event_first_line():
    line = _assistant({"type": "text", "text": "\n\nFirst line\nsecond line"})
    ev = parse_stream_json_line(line)
    assert isinstance(ev, AgentEvent)
    assert ev.kind == "text"
    assert ev.summary == "First line"
    assert ev.detail == "\n\nFirst line\nsecond line"


def test_tool_use_edit_includes_file_path():
    line = _assistant(
        {
            "type": "tool_use",
            "id": "t1",
            "name": "Edit",
            "input": {"file_path": "/tmp/foo.py", "old_string": "a", "new_string": "b"},
        }
    )
    ev = parse_stream_json_line(line)
    assert ev is not None
    assert ev.kind == "tool_use"
    assert ev.summary.startswith("Edit")
    assert "/tmp/foo.py" in ev.summary


def test_tool_use_bash_includes_command():
    line = _assistant(
        {"type": "tool_use", "id": "t2", "name": "Bash", "input": {"command": "ls -la"}}
    )
    ev = parse_stream_json_line(line)
    assert ev is not None
    assert ev.kind == "tool_use"
    assert "ls -la" in ev.summary


def test_mcp_tool_name_is_shortened():
    line = _assistant(
        {
            "type": "tool_use",
            "id": "t3",
            "name": "mcp__issuebot__add_comment",
            "input": {"body": "hi"},
        }
    )
    ev = parse_stream_json_line(line)
    assert ev is not None
    assert "add_comment" in ev.summary
    assert "mcp__issuebot__" not in ev.summary


def test_result_event():
    line = json.dumps(
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "result": "All done",
            "duration_ms": 1234,
        }
    )
    ev = parse_stream_json_line(line)
    assert ev is not None
    assert ev.kind == "result"
    assert ev.is_error is False
    assert ev.summary == "All done"


def test_result_error_event():
    line = json.dumps({"type": "result", "subtype": "error", "is_error": True, "result": "boom"})
    ev = parse_stream_json_line(line)
    assert ev is not None
    assert ev.kind == "result"
    assert ev.is_error is True


def test_system_init_event_exposes_session_id():
    # The init line is the first thing Claude Code emits and carries the session
    # id, so it must be surfaced for resume even before any model turn runs.
    line = json.dumps({"type": "system", "subtype": "init", "session_id": "x"})
    ev = parse_stream_json_line(line)
    assert ev is not None
    assert ev.kind == "init"
    assert ev.session_id == "x"


def test_system_event_without_session_id_is_none():
    line = json.dumps({"type": "system", "subtype": "other"})
    assert parse_stream_json_line(line) is None


def test_rate_limit_event_is_none():
    line = json.dumps({"type": "rate_limit_event", "remaining": 5})
    assert parse_stream_json_line(line) is None


def test_tool_result_user_event_is_none():
    line = json.dumps(
        {"type": "user", "message": {"content": [{"type": "tool_result", "content": "ok"}]}}
    )
    assert parse_stream_json_line(line) is None


def test_garbage_becomes_raw_event():
    ev = parse_stream_json_line("not json at all {")
    assert ev is not None
    assert ev.kind == "raw"
    assert ev.detail == "not json at all {"


def test_empty_line_is_none():
    assert parse_stream_json_line("   ") is None


def test_assistant_prefers_tool_use_over_text():
    line = _assistant(
        {"type": "text", "text": "let me edit"},
        {"type": "tool_use", "id": "t4", "name": "Edit", "input": {"file_path": "/x"}},
    )
    ev = parse_stream_json_line(line)
    assert ev is not None
    assert ev.kind == "tool_use"


def test_result_event_exposes_session_id() -> None:
    line = json.dumps({"type": "result", "result": "done", "session_id": "sess-xyz"})
    ev = parse_stream_json_line(line)
    assert ev is not None
    assert ev.kind == "result"
    assert ev.session_id == "sess-xyz"


def test_result_event_without_session_id_is_none() -> None:
    line = json.dumps({"type": "result", "result": "done"})
    ev = parse_stream_json_line(line)
    assert ev is not None
    assert ev.session_id is None


def test_non_result_event_has_no_session_id() -> None:
    line = json.dumps(
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "hi"}]}}
    )
    ev = parse_stream_json_line(line)
    assert ev is not None
    assert ev.session_id is None
