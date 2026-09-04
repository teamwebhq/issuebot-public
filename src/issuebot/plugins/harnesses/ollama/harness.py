"""Ollama harness: Claude Code, run against an Ollama model.

`ollama launch claude` starts Claude Code and points it at Ollama, so what runs
is still Claude Code — the same stream-json output, the same session resume, the
same board MCP servers. This harness is therefore the Claude harness with a
different command line, and it inherits everything that reads what comes back.

The wrapper's shape, which the two overrides below build:

    ollama launch claude --model <model> --yes -- <the Claude Code flags>

The model is Ollama's argument, not Claude Code's. `--yes` answers the
interactive model menu, which an unattended run cannot. Everything after `--`
goes to Claude Code unchanged.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from issuebot.plugins.harnesses.base import LaunchSpec
from issuebot.plugins.harnesses.claude.harness import ClaudeHarness
from issuebot.process import REAL, Process


class OllamaClaudeHarness(ClaudeHarness):
    """Runs Claude Code headlessly against an Ollama model."""

    name = "ollama"

    def __init__(self, *, command: str = "ollama", proc: Process = REAL) -> None:
        """``command`` is the *Ollama* executable — the program actually run.
        Claude Code is named on its command line instead, so it is found the way
        `ollama launch` finds it rather than from this install's config."""
        super().__init__(command=command, proc=proc)

    def _wrap(self, model: str | None, tail: list[str]) -> list[str]:
        """`tail` (a Claude Code command line, without its program name) as an
        `ollama launch` invocation.

        The one place the wrapper's shape is written, so the launch call and the
        PR-summary call cannot drift apart."""
        argv = [self._command, "launch", "claude"]

        # No model means the board named none. `ollama launch` then chooses one,
        # which `--yes` lets it do without asking anybody.
        if model:
            argv += ["--model", model]

        return [*argv, "--yes", "--", *tail]

    def _launch_argv(self, spec: LaunchSpec, mcp_path: Path) -> list[str]:
        """The full `ollama launch claude` invocation for this launch.

        The Claude half is the parent's, built from a spec with the model taken
        out: `spec.model` names an Ollama model, so it belongs to the wrapper and
        would name nothing Claude Code knows. The parent's argv[0] is dropped
        because the program here is Ollama."""
        tail = super()._launch_argv(replace(spec, model=None), mcp_path)[1:]

        return self._wrap(spec.model, tail)

    def _summary_argv(self, model: str | None) -> list[str]:
        """The PR-summary invocation, wrapped exactly as the launch is.

        The description is written by the same local model, so it goes the same
        way — and keeps the parent's read-only, MCP-free surface, which is
        entirely in the half after the separator."""
        tail = super()._summary_argv(None)[1:]

        return self._wrap(model, tail)
