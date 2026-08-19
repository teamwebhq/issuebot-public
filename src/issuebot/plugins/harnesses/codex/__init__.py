"""Driving Codex as the agent harness.

No settings model yet: codex takes only the generic `command` override, read
straight off its raw `[codex]` table by `config.harness_for` — there is
nothing codex-specific to field-check until it grows its own knobs.
"""

from __future__ import annotations

from issuebot.plugins.base import HarnessPlugin
from issuebot.plugins.harnesses.codex.harness import CodexHarness

PLUGIN = HarnessPlugin(name="codex", harness=CodexHarness)
