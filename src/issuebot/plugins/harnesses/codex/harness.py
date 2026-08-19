"""Codex harness: headless `codex exec <prompt>` with the Issuebear MCP injected
via a temporary --mcp-config file, run in the project folder.

Output is streamed line-by-line to the reporter as it arrives; the spawn can be
cancelled (for Ctrl-C abort / timeout), in which case the child is terminated
and the read loop unwinds — see :class:`issuebot.process.RealProcess`, which
owns that ladder for every harness."""

from __future__ import annotations

import json
import tempfile
import threading
from pathlib import Path

from issuebot.plugins.harnesses.base import Harness, LaunchResult, LaunchSpec
from issuebot.process import REAL, Process
from issuebot.reporter import Reporter


class CodexHarness(Harness):
    """Runs Codex headlessly via `codex exec`."""

    name = "codex"

    def __init__(self, *, command: str = "codex", proc: Process = REAL) -> None:
        self._command = command
        self._proc = proc

    def launch(
        self,
        spec: LaunchSpec,
        reporter: Reporter,
        cancel: threading.Event | None = None,
    ) -> LaunchResult:
        """Run codex to completion on one task, streaming its output."""
        with tempfile.TemporaryDirectory() as tmp:
            mcp_path = Path(tmp) / "mcp.json"
            mcp_path.write_text(json.dumps(spec.mcp_document()))
            # NOTE: codex's real MCP wiring may not match the shape `mcp_document`
            # builds (the flag name / config format is not yet confirmed). This is
            # verified at integration time; keep argv construction isolated here
            # so it is the single place to adjust. Nothing here asks for a
            # structured output stream — codex output is plain text, which is why
            # this harness keeps the ABC's default `parse_line`.
            argv = [
                self._command,
                "exec",
                spec.prompt,
                "--mcp-config",
                str(mcp_path),
            ]

            def on_line(line: str) -> None:
                """Tee every line to the reporter, and show it in the feed as
                whatever this harness makes of it — codex output is plain text,
                so that is the ABC's default reading, one raw event per line."""
                reporter.raw(line)
                ev = self.parse_line(line)
                if ev is not None:
                    reporter.event(ev)

            # `env` carries the repo's own bootstrap variables and, always,
            # `RESPONSE_ENV` — the path codex must write its structured
            # response to. Previously dropped here (a real gap the fake
            # harness doesn't exercise, since it never spawns anything).
            code = self._proc.spawn(
                argv, on_line=on_line, cwd=spec.folder, env=spec.env, cancel=cancel
            )

        return LaunchResult(exit_code=code)

    def summarize(self, diff: str, *, context: str, model: str | None, folder: str) -> str:
        """Not supported: codex has no tools-free one-shot mode wired up yet, so
        callers fall back to the mechanical PR description."""
        raise NotImplementedError("codex harness cannot generate PR descriptions yet")
