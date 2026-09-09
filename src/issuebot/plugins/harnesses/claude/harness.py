"""Claude Code harness: headless `claude -p <prompt>` with the launch's MCP
servers injected via a temporary --mcp-config file, run in the project folder.

Output is requested as ``--output-format stream-json`` and streamed line-by-line
to the reporter as it arrives; the spawn can be cancelled (for Ctrl-C abort /
timeout), in which case the child is terminated and the read loop unwinds."""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
from collections.abc import Mapping
from pathlib import Path

from issuebot.events import AgentEvent
from issuebot.plugins.harnesses.base import Harness, LaunchResult, LaunchSpec
from issuebot.plugins.harnesses.claude.events import parse_stream_json_line
from issuebot.process import NOT_RUN, REAL, Process
from issuebot.reporter import Reporter

logger = logging.getLogger("issuebot")

# Substrings that mark a transient, retryable API failure in the streamed
# output. Anthropic surfaces an overload as a 529 with an ``overloaded_error``
# type; either spelling is enough to know a backoff-and-resume is worth trying.
_RETRYABLE_MARKERS = ("overloaded", "error: 529", "status code 529", "status 529")

# Substrings that together mark a board MCP server the agent can no longer
# reach. Claude Code says this itself when a tool call finds the server
# detached — the call never leaves the machine. Both substrings must be
# present: "is not connected" on its own is ordinary agent output. They are two
# substrings rather than one because the stream is JSON, which escapes the
# quotes around the server name.
#
# ponytail: an agent that writes this phrase in its own prose ends its run for
# nothing. It costs one resumed relaunch, not the task, so match the tool_result
# shape only if it ever actually happens.
_MCP_LOST_MARKERS = ("mcp server", "is not connected")

# How often the cancel mirror wakes to notice the caller's abort.
_CANCEL_POLL = 0.3

# This call loads no plugin, so the board's PR-writing guidance can only ever
# reach it inlined at {guidance} (`summarize`'s own parameter, ultimately
# `Delivery.guidance`). The output contract stays here rather than in any
# skill, because it is what `_describe` in the GitHub sink parses back out,
# and it must still be stated even when {guidance} is empty.
_SUMMARY_PROMPT = (
    "Write a pull request title and description for a change you must read "
    "first.\n\n"
    "{change}\n\n"
    "Read as much of the change as you need, then answer in exactly this "
    "shape:\n\n"
    "Title: <one line, imperative, under 72 characters, without the task "
    "reference>\n\n"
    "<concise markdown description>\n\n"
    "The title is read from the line labelled `Title:`, and anything you write "
    "before that line is ignored — so a note about reading the change can "
    "never be mistaken for the title. Do not include backticks around the "
    "whole response.\n\n"
    "{guidance}\n\n"
    "Task context:\n{context}\n"
)

# What the description call may do: look at the change, and nothing else. Every
# entry reads; not one writes.
_SUMMARY_TOOLS = (
    "Read,Grep,Glob,"
    "Bash(git diff:*),Bash(git log:*),Bash(git show:*),Bash(git status:*),"
    "Bash(gh api:*),Bash(gh pr diff:*),Bash(gh pr view:*)"
)


def _is_retryable_error_line(line: str) -> bool:
    """True if ``line`` looks like a transient API overload (529) worth retrying."""
    low = line.lower()
    return any(marker in low for marker in _RETRYABLE_MARKERS)


def _is_mcp_lost_line(line: str) -> bool:
    """True if ``line`` reports an MCP server the agent can no longer reach.

    Claude Code drops a disconnected MCP server for the life of the process and
    never re-attaches it, so this is not a passing error the agent can retry
    past: every later board tool call fails the same way. Only a fresh process
    gets a fresh attachment.
    """
    low = line.lower()
    return all(marker in low for marker in _MCP_LOST_MARKERS)


def _mirror_cancel(cancel: threading.Event | None, stop: threading.Event) -> None:
    """Set ``stop`` once the caller cancels, until ``stop`` ends this thread.

    The spawn is given the launch's own ``stop`` event rather than the caller's,
    because ending a run to reconnect a dropped MCP server is not the caller's
    abort — `run._launch_with_retries` reads the caller's event to tell a retry
    from an interrupt, and a retry that looked like an interrupt would abandon
    the task. A polling thread rather than a check in ``on_line``: a child that
    prints nothing more must still be killable.
    """
    while not stop.wait(_CANCEL_POLL):
        if cancel is not None and cancel.is_set():
            stop.set()
            return


# Claude Code refuses `--dangerously-skip-permissions` when it is running as
# root, unless it is told it is inside a sandbox. That check exists because the
# flag removes every guardrail, which on a real machine's root account is
# indefensible — but a per-task sandbox that is created for one run and
# destroyed after it is exactly the case the escape hatch is for.
#
# Set only when this process really is root: on any other account Claude Code
# never asks the question, and setting it there would be claiming something
# untrue about the machine.
_SANDBOX_ENV = {"IS_SANDBOX": "1"}


def _root_env() -> dict[str, str]:
    """The overlay that lets an unattended launch run as root, or ``{}``.

    ``geteuid`` is the same question Claude Code itself asks, so this is on
    exactly when its check would otherwise fire. Absent on Windows, where the
    check does not exist either.
    """
    euid = getattr(os, "geteuid", None)
    return dict(_SANDBOX_ENV) if euid is not None and euid() == 0 else {}


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

    def _launch_argv(self, spec: LaunchSpec, mcp_path: Path) -> list[str]:
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

        # Every plugin directory this launch was handed -- the repo's own
        # bootstrap plugins, then (last, so it can only add to what came
        # before, never displace it) the board's own skill bundle. Neither
        # half is this harness's business to know apart; `spec.plugin_dirs`
        # is already ordered by `run.execute`.
        for d in spec.plugin_dirs:
            argv += ["--plugin-dir", d]

        # Add --resume to continue a prior Claude session (full in-session
        # context) instead of starting fresh. Claude-only; only set when the
        # runner has a stored session id for this task.
        if spec.resume_session_id:
            argv += ["--resume", spec.resume_session_id]

        if spec.disallowed_tools:
            argv += ["--disallowedTools", ",".join(spec.disallowed_tools)]

        # The board's requested model, passed straight through. A request, not
        # an order (see `WorkItem.model`): unlike the harness itself, there is
        # nothing to fall back to here or reason to -- an unrecognised name is
        # `claude`'s own error to raise, not core's to pre-validate.
        if spec.model:
            argv += ["--model", spec.model]

        return argv

    def launch(
        self,
        spec: LaunchSpec,
        reporter: Reporter,
        cancel: threading.Event | None = None,
    ) -> LaunchResult:
        """Run `claude -p` to completion on one task, streaming its output."""
        # The spawn is cancelled by this launch's own event, so a dropped board
        # MCP can end the run without looking like the caller's abort. A daemon
        # thread carries the caller's cancel across; setting `stop` on the way
        # out reaps it.
        stop = threading.Event()
        threading.Thread(target=_mirror_cancel, args=(cancel, stop), daemon=True).start()

        try:
            return self._run(spec, reporter, stop)
        finally:
            stop.set()  # reap the mirror thread, whatever ended the run

    def _run(self, spec: LaunchSpec, reporter: Reporter, stop: threading.Event) -> LaunchResult:
        """Spawn `claude -p` and read its stream, ending the run early when the
        board MCP server drops out. ``stop`` cancels the child."""
        with tempfile.TemporaryDirectory() as tmp:
            mcp_path = Path(tmp) / "mcp.json"
            mcp_path.write_text(json.dumps(spec.mcp_document()))
            argv = self._launch_argv(spec, mcp_path)

            captured: dict[str, str | None] = {"session_id": None, "result_text": None}
            retryable = {"hit": False}
            mcp_lost = {"hit": False}

            def on_line(line: str) -> None:
                """Tee every raw line to the reporter, surface any parsed
                stream-json event as a feed entry, capture the session id as soon
                as any event carries one, note a transient overload, and end the
                run when the board MCP server drops out. The init event provides
                the session id up front, so a turn that later aborts on a
                transient API error still leaves a resumable id behind for the
                supervisor to back off and resume against."""
                reporter.raw(line)
                if _is_retryable_error_line(line):
                    retryable["hit"] = True

                # A dropped board MCP cannot recover in this process, so the run
                # ends here and the retry ladder resumes it in a new one. Once
                # only: the agent goes on calling the dead server, and one
                # warning is the news rather than thirty-five.
                if not mcp_lost["hit"] and _is_mcp_lost_line(line):
                    mcp_lost["hit"] = True
                    retryable["hit"] = True
                    logger.warning(
                        "the board MCP server dropped out and Claude Code cannot re-attach "
                        "it in this process; ending the run so a fresh one can reconnect"
                    )
                    stop.set()

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
                argv,
                on_line=on_line,
                cwd=spec.folder,
                # The launch's own environment first: `_root_env` states a fact
                # about this machine, which nothing upstream is in a position
                # to know or to override.
                env={**(spec.env or {}), **_root_env()},
                cancel=stop,
            )

        return LaunchResult(
            exit_code=code,
            session_id=captured["session_id"],
            retryable=retryable["hit"],
            result_text=captured["result_text"] or "",
        )

    def _summary_argv(self, model: str | None) -> list[str]:
        """The read-only, MCP-free `claude -p` invocation that writes PR text.

        Its own method so a harness that wraps this one in another command
        (`ollama launch`) can wrap the summary call the same way it wraps the
        launch, rather than inheriting a command line that names the wrapper
        where the agent should be."""
        argv = [
            self._command,
            # No prompt argument: `claude -p` reads it from stdin instead, which
            # is the only way to hand a program text of unbounded size — the
            # board's guidance and the task context both arrive from elsewhere
            # and neither has a length this harness controls.
            "-p",
            # MCP-free for real: with no --mcp-config to name any, this says
            # "only the ones named there", i.e. none. Without it the user's own
            # globally configured servers load — every one of them started and
            # handshaked — to write one PR description.
            "--strict-mcp-config",
            "--output-format",
            "text",
            # Read-only, and deliberately not `--dangerously-skip-permissions`
            # the way `launch` is: a denied tool costs this call its written
            # description and falls back to the mechanical one, which is a far
            # better failure than giving a description-writing call write access
            # to somebody's checkout.
            "--allowedTools",
            _SUMMARY_TOOLS,
        ]
        if model:
            argv += ["--model", model]

        return argv

    def summarize(
        self,
        *,
        context: str,
        change: str,
        model: str | None,
        folder: str,
        guidance: str = "",
        env: Mapping[str, str] | None = None,
    ) -> str:
        """Generate PR text via a read-only, MCP-free `claude -p` that reads the
        change ``change`` names. Runs in ``folder`` and returns the collected
        stdout."""
        prompt = _SUMMARY_PROMPT.format(guidance=guidance, context=context, change=change)
        argv = self._summary_argv(model)
        out: list[str] = []
        code = self._proc.spawn(
            argv,
            on_line=out.append,
            cwd=folder,
            # The run's forge credentials, so a `gh` call the agent makes while
            # reading the change authenticates as whoever pushed the branch.
            env=dict(env) if env else None,
            stdin=prompt,
        )
        text = "\n".join(out).strip()

        # An empty answer here is the caller's cue to fall back to a mechanical
        # PR description. Log why it is about to happen, naming the command,
        # the exit code and what it said, so the next one is diagnosable from
        # the log alone.
        if code != 0 or not text:
            logger.warning(
                "PR summary command %r exited %s and said: %s",
                self._command,
                code,
                text or "(nothing)",
            )

        # The command never started — it is not on this machine's PATH, or the
        # folder is gone. What `spawn` collected is its own explanation of that,
        # not a description of anything, and returning it would have the caller
        # look for a title in it.
        if code == NOT_RUN:
            return ""

        return text
