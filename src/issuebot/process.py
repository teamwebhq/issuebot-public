"""Running other programs — the one place issuebot shells out (ADR-0003).

One interface, two adapters — :class:`RealProcess` in production and
:class:`RecordingProcess` in tests — so the cancel ladder (SIGTERM, wait a
grace period, SIGKILL) and the missing-binary guard (``FileNotFoundError``
becomes exit 127) are written once and can be tested once.

Callers get :class:`Completed` rather than ``subprocess.CompletedProcess``:
nothing outside this module should have to import ``subprocess`` to describe
what it expects a command to do.
"""

from __future__ import annotations

import os
import subprocess
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol

# Exit code reported when the program could not be started at all — a missing
# binary or an invalid working directory. Mirrors the shell's "command not
# found", so callers that already branch on a non-zero exit need no new case.
NOT_RUN = 127

# How long a terminated child gets to exit cleanly before it is hard-killed.
KILL_GRACE = 5.0

# How often the cancel watcher wakes to check whether it should kill the child.
_POLL = 0.3


@dataclass(frozen=True)
class Completed:
    """A finished command: what was run, how it exited, and what it said."""

    argv: list[str]
    code: int
    out: str = ""
    err: str = ""

    @property
    def ok(self) -> bool:
        """True when the command exited zero."""
        return self.code == 0

    @property
    def message(self) -> str:
        """The most useful line of output to show a human or put in an error.

        Prefers stderr, because a failing command explains itself there, and
        falls back to stdout for the programs that do not."""
        return self.err.strip() or self.out.strip()


class Process(Protocol):
    """Runs other programs.

    Two adapters: :class:`RealProcess` and :class:`RecordingProcess`.
    """

    def run(
        self,
        argv: list[str],
        *,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
    ) -> Completed:
        """Run to completion, capturing output. Never raises: a program that
        could not be started comes back as :data:`NOT_RUN`, because every caller
        already handles a non-zero exit and none of them expect an exception."""
        ...

    def spawn(
        self,
        argv: list[str],
        *,
        on_line: Callable[[str], None],
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        cancel: threading.Event | None = None,
    ) -> int:
        """Run while streaming stdout line by line to ``on_line``, and return the
        exit code. If ``cancel`` is set mid-run the child is terminated, then
        killed if it does not go quietly."""
        ...


def _child_env(env: dict[str, str] | None) -> dict[str, str] | None:
    """The child's full environment: this process' env with ``env`` applied.

    An empty value means *remove* the variable rather than set it to empty —
    a present-but-blank credential is still a credential to most CLIs, so
    "unset this" needs a spelling that survives the overlay. Returns None when
    there is no overlay, so the child simply inherits.
    """
    if not env:
        return None
    merged = dict(os.environ)
    for key, value in env.items():
        if value:
            merged[key] = value
        else:
            merged.pop(key, None)
    return merged


class RealProcess:
    """Runs programs for real, via :mod:`subprocess`."""

    def run(
        self,
        argv: list[str],
        *,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
    ) -> Completed:
        """Run to completion and capture output."""
        try:
            r = subprocess.run(
                argv,
                cwd=cwd,
                text=True,
                capture_output=True,
                check=False,
                env=_child_env(env),
            )
        except (FileNotFoundError, NotADirectoryError, PermissionError) as exc:
            # The program is missing, or the working directory is not one. This
            # is a failed invocation, not an exceptional condition — a machine
            # without `gh` installed should get a clean "not available", not a
            # traceback out of whichever caller happened to ask first.
            return Completed(argv, NOT_RUN, err=str(exc))
        return Completed(argv, r.returncode, r.stdout, r.stderr)

    def spawn(
        self,
        argv: list[str],
        *,
        on_line: Callable[[str], None],
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        cancel: threading.Event | None = None,
    ) -> int:
        """Stream the child's output, honouring ``cancel``."""
        try:
            proc = subprocess.Popen(  # noqa: S603 - argv is built by callers, never shell
                argv,
                cwd=cwd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                env=_child_env(env),
            )
        except (FileNotFoundError, NotADirectoryError, PermissionError) as exc:
            on_line(f"could not start {argv[0]}: {exc}")
            return NOT_RUN

        watcher = None
        if cancel is not None:
            watcher = threading.Thread(target=self._watch_cancel, args=(proc, cancel), daemon=True)
            watcher.start()

        # stdout is a pipe because Popen was told so; reading it to EOF is what
        # blocks until the child is done talking.
        assert proc.stdout is not None
        for line in proc.stdout:
            on_line(line.rstrip("\n"))

        code = proc.wait()

        # Let the watcher notice the child is gone before returning, so it does
        # not outlive the run and fire at a recycled pid.
        if watcher is not None:
            watcher.join(timeout=KILL_GRACE)
        return code

    @staticmethod
    def _watch_cancel(proc: subprocess.Popen[str], cancel: threading.Event) -> None:
        """Terminate the child once cancelled; kill it if it lingers.

        The one copy of this ladder. Polls rather than waiting on the process,
        because it has to react to the cancel Event and to natural exit, and
        only one of those two is waitable.
        """
        while proc.poll() is None:
            if cancel.wait(_POLL):
                proc.terminate()
                try:
                    proc.wait(timeout=KILL_GRACE)
                except subprocess.TimeoutExpired:
                    proc.kill()
                return


@dataclass
class RecordingProcess:
    """Answers from a script instead of running anything, and records the calls.

    The test adapter, shipped beside the real one for the same reason
    :class:`~issuebot.plugins.harnesses.fake.harness.FakeHarness` is: every test
    file was otherwise growing its own, and they disagreed.

    ``replies`` maps a substring of the command to the :class:`Completed` it
    should produce — the first match wins, so a test scripts only the commands
    it cares about and everything else succeeds silently. ``lines`` is what
    :meth:`spawn` streams.
    """

    replies: dict[str, Completed] = field(default_factory=dict)
    lines: list[str] = field(default_factory=list)
    exit_code: int = 0

    # Every call made, in order, for assertions.
    calls: list[list[str]] = field(default_factory=list)
    cwds: list[str | None] = field(default_factory=list)
    envs: list[dict[str, str] | None] = field(default_factory=list)

    def run(
        self,
        argv: list[str],
        *,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
    ) -> Completed:
        """Record the call and return the scripted reply, else a clean exit."""
        self.calls.append(list(argv))
        self.cwds.append(cwd)
        self.envs.append(env)

        joined = " ".join(argv)
        for pattern, reply in self.replies.items():
            if pattern in joined:
                return reply
        return Completed(argv, 0)

    def spawn(
        self,
        argv: list[str],
        *,
        on_line: Callable[[str], None],
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        cancel: threading.Event | None = None,
    ) -> int:
        """Record the call, replay ``lines``, and stop early if cancelled."""
        self.calls.append(list(argv))
        self.cwds.append(cwd)
        self.envs.append(env)

        for line in self.lines:
            if cancel is not None and cancel.is_set():
                break
            on_line(line)
        return self.exit_code


# The adapter every caller defaults to. Stateless, so one instance is enough.
REAL: Process = RealProcess()
