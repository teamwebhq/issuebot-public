"""Background command handler: long-poll for restart/update and execute them.

Both commands relaunch the runner via os.execv so an "update" picks up new code.
relaunch/run_update are injectable so tests never actually exec or upgrade.

An **update waits for the work in flight to finish** before it changes anything,
and is **refused** if that work is still going when the wait runs out. It
replaces the very files the running process imports from and then execs over
itself, so doing that mid-run abandons a claimed task at whatever point it had
reached — and possibly breaks it before that, since a long-lived run has not
necessarily imported every module it still needs. An update that does not land
costs a retry; one that lands on top of a running task costs the task.

A **restart** does not wait: it is the answer to a runner that is stuck, so
making it queue behind the thing that is stuck would take away the only lever
there is.
"""

from __future__ import annotations

import logging
import os
import shlex
import subprocess
import sys
import threading
from collections.abc import Callable
from typing import Protocol

from issuebot.config import DEFAULT_UPDATE_COMMAND
from issuebot.release import install_bin_dir
from issuebot.transient import log_poll_failure

logger = logging.getLogger("issuebot")


class _CommandClient(Protocol):
    """Minimal interface a client must expose for the command loop."""

    def wait_for_commands(
        self, *, install_id: str | None = None, timeout: int = 25
    ) -> list[dict]: ...

    def ack_command(self, command_id: str, *, status: str, result: str | None = None) -> None: ...


# How long an update waits for in-flight work before refusing to update at all.
# Long enough for an ordinary task to finish, bounded because the alternative is
# a command thread that never comes back and a control the board never hears
# about again.
DRAIN_TIMEOUT = 30 * 60.0


class _Stoppable(Protocol):
    """Any object that can be stopped (e.g. a listener thread).

    ``hold``/``resume`` are the pause an update needs: stop taking new work and
    wait for what is running, reversibly, so a failed update leaves a runner
    that is still working rather than one that has quietly stopped claiming.
    """

    def stop(self) -> None: ...

    def hold(self, timeout: float) -> bool: ...

    def resume(self) -> None: ...


def _default_relaunch() -> None:
    """Replace this process with a fresh copy of the exact original invocation."""
    os.execv(sys.orig_argv[0], sys.orig_argv)


# How much of the installer's own complaint travels back to the board. Enough
# to carry the line that explains the failure, bounded because the ack is not a
# log file.
_REASON_CHARS = 400


def _update_env() -> dict[str, str]:
    """Environment that makes the installer reinstall over the running binary.

    ``install.sh`` re-derives where to install and then refuses a directory that
    is not on the PATH it was handed. A runner whose PATH lacks ``~/.local/bin``
    would therefore be told its own working install is unreachable. Naming the
    directory of the running console script answers both: it is where the update
    must land, and the running process is the proof that it is reachable.
    """
    env = dict(os.environ)
    bin_dir = install_bin_dir()

    if bin_dir is None:
        return env  # not started from the console script; nothing to pin

    env["ISSUEBOT_BIN_DIR"] = str(bin_dir)
    env["PATH"] = os.pathsep.join(filter(None, [str(bin_dir), env.get("PATH", "")]))
    return env


def _default_run_update(command: str) -> None:
    """Run the self-update command (no shell), raising on a non-zero exit.

    The installer's output is captured and logged, and a failure carries the tail
    of its stderr into the exception message — ``_handle`` acks that message, and
    ``str(CalledProcessError)`` alone only ever says "returned non-zero exit
    status 1", which tells the board nothing about what went wrong.
    """
    try:
        done = subprocess.run(
            shlex.split(command),
            check=True,
            env=_update_env(),
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as exc:
        output = ((exc.stdout or "") + (exc.stderr or "")).strip()
        logger.warning("installer failed (exit %s):\n%s", exc.returncode, output)
        reason = output[-_REASON_CHARS:] or "no output"
        raise RuntimeError(f"installer failed (exit {exc.returncode}): {reason}") from exc

    logger.info("installer output:\n%s", ((done.stdout or "") + (done.stderr or "")).strip())


def run_command_loop(
    client: _CommandClient,
    *,
    stop: threading.Event,
    listeners: list[_Stoppable],
    relaunch: Callable[[], None] = _default_relaunch,
    run_update: Callable[[str], None] = _default_run_update,
    update_command: str = DEFAULT_UPDATE_COMMAND,
    wait_timeout: int = 25,
    install_id: str | None = None,
    drain_timeout: float = DRAIN_TIMEOUT,
) -> None:
    """Long-poll for control commands and execute restart/update until ``stop``.

    Pass ``install_id`` to scope each poll to commands for this install. Network
    failures are logged and retried with a short back-off (like the work loop).
    A failed update acks ``failed``, resumes claiming and keeps the runner
    serving. ``drain_timeout`` is how long an update waits for in-flight work
    before giving up on this attempt and acking ``failed``.
    """
    transient_fails = 0
    while not stop.is_set():
        try:
            commands = client.wait_for_commands(install_id=install_id, timeout=wait_timeout)
        except Exception as exc:  # noqa: BLE001 — transient; back off and retry
            transient_fails = log_poll_failure(logger, "Command API", exc, transient_fails)
            stop.wait(3)
            continue
        transient_fails = 0

        for command in commands:
            if stop.is_set():
                return
            _handle(command, client, listeners, relaunch, run_update, update_command, drain_timeout)


def _handle(
    command: dict,
    client: _CommandClient,
    listeners: list[_Stoppable],
    relaunch: Callable[[], None],
    run_update: Callable[[str], None],
    update_command: str,
    drain_timeout: float = DRAIN_TIMEOUT,
) -> None:
    """Execute one command: restart or update (unknown kinds are logged and ignored)."""
    kind = command.get("kind")
    command_id: str = command.get("id") or ""

    if kind == "restart":
        # Ack BEFORE stopping listeners so the server knows we received it.
        client.ack_command(command_id, status="done", result="restarting")

        for listener in listeners:
            listener.stop()  # abort any in-flight agent

        relaunch()  # re-exec; does not return in production
        return

    if kind == "update":
        # Before anything is written: stop claiming and let the running tasks
        # finish. Every listener is asked (a list comprehension, not `all()`,
        # which would stop at the first False and leave the rest still claiming).
        drained = [listener.hold(drain_timeout) for listener in listeners]

        if not all(drained):
            # Refuse rather than update over live work. An update that does not
            # land is an inconvenience — the next one will, or the user runs the
            # installer by hand — while an update that lands on top of a running
            # task destroys work that cannot be got back.
            for listener in listeners:
                listener.resume()
            problem = f"work still in flight after {drain_timeout:.0f}s; did not update"
            logger.warning(problem)
            client.ack_command(command_id, status="failed", result=problem)
            return

        try:
            run_update(update_command)
        except Exception as exc:  # noqa: BLE001 — a bad upgrade must not brick the runner
            logger.warning("update command failed", exc_info=True)
            # The runner carries on serving on the old code, so it has to start
            # claiming again — a held runner that never relaunches is one that
            # looks alive and takes no work.
            for listener in listeners:
                listener.resume()
            client.ack_command(command_id, status="failed", result=str(exc))
            return

        client.ack_command(command_id, status="done", result="updated")
        relaunch()
        return

    logger.warning("ignoring unknown command kind: %r", kind)
