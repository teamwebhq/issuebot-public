"""Driving Codex as the agent harness — implementation kept, not installed.

This package deliberately does not define `PLUGIN`: `plugins.discover` only
registers a harness subpackage that exposes one (see its docstring), so Codex
is invisible to the registry — absent from `plugins.names_of("harnesses")`,
unresolvable through `plugins.get`, and never offered by the `issuebot init`
wizard. `config.harness_name` also gives a config that already names
`harness = "codex"` its own actionable error, rather than the generic
"unknown harness" one this non-registration alone would produce.

Why: agent skills now come from the board, loaded into the agent via Claude
Code's `--plugin-dir`. Codex has no equivalent, so a Codex run would silently
receive none of them — a prompt built for a skill-aware agent, handed to one
that has no skills loaded at all. That gap is being closed, but not yet, so
Codex is out of the choice until it is.

`CodexHarness` itself (`harness.py`) is untouched and still exercised by its
own test suite — this is churn-free to reverse: bring the harness back by
restoring the `PLUGIN = HarnessPlugin(...)` line below once Codex has a real
skills story.
"""

from __future__ import annotations

# Kept for the day this is reinstated — not assigned to `PLUGIN`, so
# `plugins.discover` does not pick it up. See the module docstring.
# from issuebot.plugins.base import HarnessPlugin
# from issuebot.plugins.harnesses.codex.harness import CodexHarness
#
# PLUGIN = HarnessPlugin(name="codex", harness=CodexHarness)
