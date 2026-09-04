"""Driving Claude Code through `ollama launch` as the agent harness.

`global_settings` models the `[ollama]` table (`command`, `resume_sessions`) the
same way the `[claude]` table is modelled, so a typo there is field-checked.

`doctor` is the Claude plugin's own hook, which wires the board's MCP into the
user's interactive Claude Code. This install runs Claude Code too, so it wants
that step as much as a `claude` install does — and the hook registers Claude
Code's executable, never the `ollama` one that starts it.
"""

from __future__ import annotations

from pydantic import BaseModel

from issuebot.plugins.base import HarnessPlugin
from issuebot.plugins.harnesses.claude.mcp_setup import ensure_claude_mcp
from issuebot.plugins.harnesses.ollama.harness import OllamaClaudeHarness


class GlobalSettings(BaseModel):
    """`[ollama]`: how to run Ollama, and whether to resume prior sessions."""

    # Path to the `ollama` executable. None resolves it on PATH.
    command: str | None = None

    # Whether the local runner resumes a task's prior Claude Code session
    # (`claude --resume`) instead of starting fresh each launch.
    resume_sessions: bool = False


PLUGIN = HarnessPlugin(
    name="ollama",
    harness=OllamaClaudeHarness,
    global_settings=GlobalSettings,
    doctor=ensure_claude_mcp,
)
