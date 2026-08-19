"""Driving Claude Code as the agent harness.

`global_settings` models the `[claude]` table (`command`, `resume_sessions`),
so a typo there is now field-checked the same way every other plugin's table
already is. `doctor` wires the source's MCP into the user's own Claude Code
(`issuebot init`/`issuebot doctor`); it is a no-op for any other harness, so it
is safe to call unconditionally once resolved through this plugin.
"""

from __future__ import annotations

from pydantic import BaseModel

from issuebot.plugins.base import HarnessPlugin
from issuebot.plugins.harnesses.claude.cli import app as claude_cli
from issuebot.plugins.harnesses.claude.harness import ClaudeHarness
from issuebot.plugins.harnesses.claude.mcp_setup import ensure_claude_mcp


class GlobalSettings(BaseModel):
    """`[claude]`: how to run Claude Code, and whether to resume prior sessions."""

    # Path to the `claude` executable. None resolves it on PATH.
    command: str | None = None

    # Whether the local runner resumes a task's prior Claude session
    # (`claude --resume`) instead of starting fresh each launch.
    resume_sessions: bool = False


PLUGIN = HarnessPlugin(
    name="claude",
    harness=ClaudeHarness,
    global_settings=GlobalSettings,
    cli=claude_cli,
    doctor=ensure_claude_mcp,
)
