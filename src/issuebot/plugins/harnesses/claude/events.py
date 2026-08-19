"""Reading Claude Code's ``--output-format stream-json`` back into AgentEvents.

One vendor's wire format, so it lives with the plugin that asks for it rather
than in core: every line here is a key Claude Code chose (``type``, ``message``,
``content``, ``session_id``), and nothing else in issuebot can be expected to
know them. Unknown or non-JSON lines become ``raw`` events so nothing a run
printed is silently lost.
"""

from __future__ import annotations

import json

from issuebot.events import AgentEvent, raw_event, truncate


def _tool_summary(name: str, tool_input: dict) -> str:
    """`Edit: /path/x` — the tool's short name plus whichever argument names
    what it acted on. An MCP tool is reported by its last segment, since the
    `mcp__<server>__` prefix is the same on every line."""
    short = name.split("__")[-1] if name.startswith("mcp__") else name
    arg = (
        tool_input.get("file_path")
        or tool_input.get("command")
        or tool_input.get("path")
        or tool_input.get("pattern")
        or ""
    )
    return f"{short}: {truncate(str(arg))}" if arg else short


def _from_assistant(obj: dict) -> AgentEvent | None:
    """One assistant turn: the tool it called, else the text it said, else
    nothing worth showing (an empty or tool-result-only turn)."""
    blocks = obj.get("message", {}).get("content", []) or []
    tool = next((b for b in blocks if b.get("type") == "tool_use"), None)
    if tool is not None:
        return AgentEvent(
            "tool_use", _tool_summary(tool.get("name", "?"), tool.get("input", {}) or {})
        )

    text = next((b for b in blocks if b.get("type") == "text"), None)
    if text is not None and text.get("text", "").strip():
        return AgentEvent("text", truncate(text["text"]), detail=text["text"])

    return None


def parse_stream_json_line(line: str) -> AgentEvent | None:
    """One stream-json line as an :class:`AgentEvent`, or None if it carries
    nothing the feed shows."""
    line = line.strip()
    if not line:
        return None

    try:
        obj = json.loads(line)
    except (ValueError, TypeError):
        return raw_event(line)

    if not isinstance(obj, dict):
        return raw_event(line)

    kind = obj.get("type")

    if kind == "assistant":
        return _from_assistant(obj)

    if kind == "system":
        # The init event is the first line Claude Code emits, before any model
        # turn — so it carries the session id even when the turn later aborts on
        # a transient API error (529). Surface it so the run can be resumed.
        sid = obj.get("session_id")
        if sid:
            return AgentEvent("init", f"session {sid}", session_id=sid)
        return None

    if kind == "result":
        return AgentEvent(
            "result",
            truncate(obj.get("result", "done")),
            detail=obj.get("result", ""),
            is_error=bool(obj.get("is_error")),
            session_id=obj.get("session_id"),
        )

    return None
