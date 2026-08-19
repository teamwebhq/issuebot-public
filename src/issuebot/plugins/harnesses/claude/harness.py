"""Claude Code harness: headless `claude -p <prompt>` with the launch's MCP
servers injected via a temporary --mcp-config file, run in the project folder.

Output is requested as ``--output-format stream-json`` and streamed line-by-line
to the reporter as it arrives; the spawn can be cancelled (for Ctrl-C abort /
timeout), in which case the child is terminated and the read loop unwinds."""

from __future__ import annotations

import json
import logging
import tempfile
import threading
from pathlib import Path

from issuebot.events import AgentEvent
from issuebot.plugins.harnesses.base import Harness, LaunchResult, LaunchSpec
from issuebot.plugins.harnesses.claude.events import parse_stream_json_line
from issuebot.plugins.harnesses.claude.skills import body, plugin_dir
from issuebot.process import REAL, Process
from issuebot.reporter import Reporter

logger = logging.getLogger("issuebot")

# Substrings that mark a transient, retryable API failure in the streamed
# output. Anthropic surfaces an overload as a 529 with an ``overloaded_error``
# type; either spelling is enough to know a backoff-and-resume is worth trying.
_RETRYABLE_MARKERS = ("overloaded", "error: 529", "status code 529", "status 529")

# This call loads no plugin, so the `writing-pull-requests` skill cannot be
# selected the usual way -- its body is inlined at {guidance} instead. The output
# contract stays here rather than in the skill, because it is what `_describe` in
# the GitHub sink parses back out, and it must still be stated when a broken
# install leaves {guidance} empty.
_SUMMARY_PROMPT = (
    "Write a pull request title and description for the following change.\n"
    "Output the title on the first line, then a blank line, then a concise "
    "markdown description. Do not include backticks around the whole "
    "response.\n\n"
    "{guidance}\n\n"
    "Task context:\n{context}\n\nDiff:\n{diff}\n"
)


def _is_retryable_error_line(line: str) -> bool:
    """True if ``line`` looks like a transient API overload (529) worth retrying."""
    low = line.lower()
    return any(marker in low for marker in _RETRYABLE_MARKERS)


class ClaudeHarness(Harness):
    """Runs Claude Code headlessly via `claude -p`."""

    name = "claude"

    # `claude --resume <id>` reopens a conversation with its full in-session
    # context, so a run that was paused or aborted can carry on where it left
    # off rather than re-reading the repo from scratch.
    resumes_sessions = True

    def __init__(self, *, command: str = "claude", proc: Process = REAL) -> None:
        self._command = command
        self._proc = proc

    def parse_line(self, line: str) -> AgentEvent | None:
        """Read one `--output-format stream-json` line — the format this harness
        asks for in :meth:`_launch_argv`, so it is the one that can read it back."""
        return parse_stream_json_line(line)

    def _launch_argv(self, spec: LaunchSpec, mcp_path: Path, plugin_root: Path) -> list[str]:
        """The full `claude -p` invocation for this launch."""
        argv = [
            self._command,
            "-p",
            spec.prompt,
            "--mcp-config",
            str(mcp_path),
            # Use ONLY our injected MCP servers, ignoring any globally
            # configured ones, so the agent's surface is exactly what this
            # launch was handed.
            "--strict-mcp-config",
            # Bypass all permission prompts. A headless/unattended runner
            # cannot grant interactive approvals, and acceptEdits still
            # blocks MCP tool calls ("needs permission grant"). This is the
            # standard for autonomous headless agents; see the Security
            # section of the README for the tradeoff and how to contain it.
            "--dangerously-skip-permissions",
            # Stream structured events so the reporter can render a live feed
            # (--verbose is required for stream-json to emit per-turn lines).
            "--output-format",
            "stream-json",
            "--verbose",
        ]

        # Load the bundled board-skills plugin so the agent has the board-native
        # skills available; warn (but still launch) if it cannot be located on
        # this install. Written into plugin_root, a subdir of this launch's own
        # temp dir, so it is cleaned up with everything else once the process
        # exits -- no directory left behind per launch/retry.
        plugin = plugin_dir(plugin_root)
        if plugin is not None:
            argv += ["--plugin-dir", plugin]
        else:
            logger.warning("issuebot board-skills plugin not found; launching without it")

        for d in spec.plugin_dirs:
            argv += ["--plugin-dir", d]

        # Add --resume to continue a prior Claude session (full in-session
        # context) instead of starting fresh. Claude-only; only set when the
        # runner has a stored session id for this task.
        if spec.resume_session_id:
            argv += ["--resume", spec.resume_session_id]

        if spec.disallowed_tools:
            argv += ["--disallowedTools", ",".join(spec.disallowed_tools)]

        return argv

    def launch(
        self,
        spec: LaunchSpec,
        reporter: Reporter,
        cancel: threading.Event | None = None,
    ) -> LaunchResult:
        """Run `claude -p` to completion on one task, streaming its output."""
        with tempfile.TemporaryDirectory() as tmp:
            mcp_path = Path(tmp) / "mcp.json"
            mcp_path.write_text(json.dumps(spec.mcp_document()))
            plugin_root = Path(tmp) / "plugin"
            argv = self._launch_argv(spec, mcp_path, plugin_root)

            captured: dict[str, str | None] = {"session_id": None, "result_text": None}
            retryable = {"hit": False}

            def on_line(line: str) -> None:
                """Tee every raw line to the reporter, surface any parsed
                stream-json event as a feed entry, capture the session id as soon
                as any event carries one, and note a transient overload. The init
                event provides the session id up front, so a turn that later
                aborts on a transient API error still leaves a resumable id
                behind for the supervisor to back off and resume against."""
                reporter.raw(line)
                if _is_retryable_error_line(line):
                    retryable["hit"] = True
                ev = self.parse_line(line)
                if ev is None:
                    return
                if ev.session_id:
                    captured["session_id"] = ev.session_id
                if ev.kind == "result" and ev.detail:
                    captured["result_text"] = ev.detail
                # The init event exists only to surface the session id (captured
                # above); it carries no activity worth showing and Claude emits it
                # repeatedly, so keep it out of the live feed.
                if ev.kind != "init":
                    reporter.event(ev)

            code = self._proc.spawn(
                argv, on_line=on_line, cwd=spec.folder, env=spec.env, cancel=cancel
            )

        return LaunchResult(
            exit_code=code,
            session_id=captured["session_id"],
            retryable=retryable["hit"],
            result_text=captured["result_text"] or "",
        )

    def summarize(self, diff: str, *, context: str, model: str | None, folder: str) -> str:
        """Generate PR text from a diff via a tools-free, MCP-free `claude -p`.
        Runs in ``folder`` and returns the collected stdout."""
        argv = [
            self._command,
            "-p",
            _SUMMARY_PROMPT.format(
                guidance=body("writing-pull-requests"), context=context, diff=diff
            ),
            # MCP-free for real: with no --mcp-config to name any, this says
            # "only the ones named there", i.e. none. Without it the user's own
            # globally configured servers load — every one of them started and
            # handshaked — to write a PR description from a diff already in the
            # prompt.
            "--strict-mcp-config",
            "--output-format",
            "text",
        ]
        if model:
            argv += ["--model", model]
        out: list[str] = []
        self._proc.spawn(argv, on_line=out.append, cwd=folder)
        return "\n".join(out).strip()
