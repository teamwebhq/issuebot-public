import os
import shlex
import subprocess
import sys
import threading

import pytest

from issuebot import commands, release
from issuebot.commands import run_command_loop
from issuebot.config import DEFAULT_UPDATE_COMMAND, Config
from issuebot.runner import Supervisor


class _Listener:
    def __init__(self, *, drains: bool = True) -> None:
        self.stopped = False
        self.held: list[float] = []
        self.resumed = 0
        self._drains = drains
        # What the runner had done by the time the update command ran, so a test
        # can assert the order rather than only that both happened.
        self.events: list[str] = []

    def stop(self) -> None:
        self.stopped = True

    def hold(self, timeout: float) -> bool:
        self.held.append(timeout)
        self.events.append("held")
        return self._drains

    def resume(self) -> None:
        self.resumed += 1
        self.events.append("resumed")


class _CmdClient:
    """Serves one command on the first wait, then [] and sets stop."""

    def __init__(self, command: dict, stop: threading.Event) -> None:
        self._command = command
        self._served = False
        self._stop = stop
        self.acks: list[dict] = []
        self.seen_install_ids: list[str | None] = []

    def wait_for_commands(self, *, install_id: str | None = None, timeout: int = 25) -> list[dict]:
        self.seen_install_ids.append(install_id)
        if not self._served:
            self._served = True
            return [self._command]
        self._stop.set()
        return []

    def ack_command(self, command_id: str, *, status: str, result=None) -> None:
        self.acks.append({"id": command_id, "status": status, "result": result})


def test_restart_acks_stops_listeners_and_relaunches():
    stop = threading.Event()
    client = _CmdClient({"id": "c1", "kind": "restart"}, stop)
    listener = _Listener()
    relaunched = {"hit": False}

    def relaunch() -> None:
        relaunched["hit"] = True
        stop.set()  # restart re-exec never returns; stop the loop in the test

    run_command_loop(
        client,
        stop=stop,
        listeners=[listener],
        relaunch=relaunch,
        run_update=lambda cmd: None,
        update_command="x",
        wait_timeout=0,
    )

    assert client.acks[0] == {"id": "c1", "status": "done", "result": "restarting"}
    assert listener.stopped is True
    assert relaunched["hit"] is True


def test_update_runs_command_then_relaunches():
    stop = threading.Event()
    client = _CmdClient({"id": "c2", "kind": "update"}, stop)
    ran = {"cmd": None}

    def run_update(cmd: str) -> None:
        ran["cmd"] = cmd

    def relaunch() -> None:
        stop.set()

    run_command_loop(
        client,
        stop=stop,
        listeners=[],
        relaunch=relaunch,
        run_update=run_update,
        update_command="uv tool upgrade issuebot",
        wait_timeout=0,
    )

    assert ran["cmd"] == "uv tool upgrade issuebot"
    assert client.acks[0]["status"] == "done"


def test_update_failure_acks_failed_and_does_not_relaunch():
    stop = threading.Event()
    client = _CmdClient({"id": "c3", "kind": "update"}, stop)
    relaunched = {"hit": False}

    def run_update(cmd: str) -> None:
        raise RuntimeError("upgrade boom")

    def relaunch() -> None:
        relaunched["hit"] = True

    run_command_loop(
        client,
        stop=stop,
        listeners=[],
        relaunch=relaunch,
        run_update=run_update,
        update_command="x",
        wait_timeout=0,
    )

    assert client.acks[0]["status"] == "failed"
    assert "upgrade boom" in (client.acks[0]["result"] or "")
    assert relaunched["hit"] is False


def test_command_loop_passes_install_id_to_wait():
    """run_command_loop must forward install_id to each wait_for_commands call."""
    stop = threading.Event()
    # Use a no-op command so the loop runs at least twice (command then stop).
    client = _CmdClient({"id": "cx", "kind": "unknown"}, stop)

    run_command_loop(
        client,
        stop=stop,
        listeners=[],
        relaunch=lambda: None,
        run_update=lambda cmd: None,
        update_command="x",
        wait_timeout=0,
        install_id="inst-77",
    )

    assert all(iid == "inst-77" for iid in client.seen_install_ids)


def test_the_default_update_command_runs_the_installer_through_a_shell(monkeypatch):
    """The default has to survive the executor, which uses no shell.

    `_default_run_update` splits the command with `shlex.split` and hands the
    argv to `subprocess.run`, so a bare `curl … | sh` would reach `curl` as
    three extra arguments and the pipe would never be a pipe. This asserts the
    argv that actually gets executed, not just the string: the two halves are
    one contract and testing either alone lets the other drift.

    The old default (`uv tool upgrade issuebot`) fails this — and failed for
    real, resolving a package name that is not on any index and never will be.
    """
    ran: dict[str, list[str]] = {}

    def fake_run(argv, **kwargs):
        ran["argv"] = argv
        assert kwargs.get("check") is True  # a failed update must not ack "done"
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(commands.subprocess, "run", fake_run)
    commands._default_run_update(DEFAULT_UPDATE_COMMAND)

    shell, flag, script = ran["argv"]
    assert (shell, flag) == ("sh", "-c")
    assert script == release.INSTALL_COMMAND
    assert shlex.split(DEFAULT_UPDATE_COMMAND) == ["sh", "-c", release.INSTALL_COMMAND]


def test_the_default_update_command_is_defined_once():
    """Three copies of this string is how two of them go stale — and did."""
    assert Config().update_command == DEFAULT_UPDATE_COMMAND
    assert run_command_loop.__kwdefaults__["update_command"] == DEFAULT_UPDATE_COMMAND
    assert Supervisor.__init__.__kwdefaults__["update_command"] == DEFAULT_UPDATE_COMMAND


def test_update_waits_for_in_flight_work_before_touching_anything():
    """An update replaces the files this process is running from and then execs
    over itself, so it drains first: the runner stops claiming and the tasks
    already running are allowed to finish."""
    stop = threading.Event()
    client = _CmdClient({"id": "c4", "kind": "update"}, stop)
    listener = _Listener()

    def run_update(cmd: str) -> None:
        listener.events.append("updated")

    run_command_loop(
        client,
        stop=stop,
        listeners=[listener],
        relaunch=stop.set,
        run_update=run_update,
        update_command="x",
        wait_timeout=0,
        drain_timeout=1.5,
    )

    assert listener.events == ["held", "updated"]
    assert listener.held == [1.5]


def test_a_failed_update_starts_claiming_again():
    """The runner keeps serving on the old code, so the hold has to come off —
    one that stays on is a runner that looks alive and takes no work."""
    stop = threading.Event()
    client = _CmdClient({"id": "c5", "kind": "update"}, stop)
    listener = _Listener()

    def run_update(cmd: str) -> None:
        raise RuntimeError("upgrade boom")

    run_command_loop(
        client,
        stop=stop,
        listeners=[listener],
        relaunch=lambda: None,
        run_update=run_update,
        update_command="x",
        wait_timeout=0,
    )

    assert listener.events == ["held", "resumed"]
    assert client.acks[0]["status"] == "failed"


def test_an_update_that_cannot_drain_is_refused():
    """An update that does not land costs a retry; one that lands on top of a
    running task costs the task. So the work wins and the runner carries on
    serving on the old code."""
    stop = threading.Event()
    client = _CmdClient({"id": "c6", "kind": "update"}, stop)
    listener = _Listener(drains=False)
    relaunched = {"hit": False}

    def relaunch() -> None:
        relaunched["hit"] = True

    run_command_loop(
        client,
        stop=stop,
        listeners=[listener],
        relaunch=relaunch,
        run_update=lambda cmd: listener.events.append("updated"),
        update_command="x",
        wait_timeout=0,
        drain_timeout=0.01,
    )

    assert listener.events == ["held", "resumed"]
    assert relaunched["hit"] is False
    assert client.acks[0]["status"] == "failed"
    assert "still in flight" in (client.acks[0]["result"] or "")


def test_a_restart_does_not_wait_for_in_flight_work():
    """Restart is the lever for a runner that is stuck, so it must not queue
    behind whatever is stuck."""
    stop = threading.Event()
    client = _CmdClient({"id": "c7", "kind": "restart"}, stop)
    listener = _Listener()

    run_command_loop(
        client,
        stop=stop,
        listeners=[listener],
        relaunch=stop.set,
        run_update=lambda cmd: None,
        update_command="x",
        wait_timeout=0,
    )

    assert listener.held == []
    assert listener.stopped is True


def test_an_update_reinstalls_over_the_running_binary(tmp_path, monkeypatch):
    """The installer must land back on the binary that is running.

    install.sh re-derives its own BIN_DIR and refuses a directory that is not on
    the PATH it was handed — which is how an update of a working
    `~/.local/bin/issuebot` failed from a runner whose PATH had no `~/.local/bin`.
    The running console script is the proof that its own directory is reachable,
    so the update names it and puts it on PATH.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "issuebot").touch()
    monkeypatch.setattr(sys, "argv", [str(bin_dir / "issuebot"), "run"])

    recorded = tmp_path / "env.txt"
    script = f'printf "%s\\n%s\\n" "$ISSUEBOT_BIN_DIR" "$PATH" > {shlex.quote(str(recorded))}'
    commands._default_run_update(shlex.join(["sh", "-c", script]))

    seen_bin_dir, seen_path = recorded.read_text().splitlines()
    assert seen_bin_dir == str(bin_dir)
    assert str(bin_dir) in seen_path.split(os.pathsep)


def test_a_failed_update_says_what_the_installer_said(tmp_path, monkeypatch):
    """`_handle` acks the exception text, so the board reads the real reason.

    The complaint lives in the installer, not in the command line, because
    `str(CalledProcessError)` quotes the argv and would otherwise pass by
    accident while the board still learned nothing.
    """
    monkeypatch.setattr(sys, "argv", ["pytest"])
    complaint = "issuebot: /home/richard/.local/bin is not on PATH"
    installer = tmp_path / "install.sh"
    installer.write_text(f"echo {shlex.quote(complaint)} >&2\nexit 1\n")

    with pytest.raises(Exception) as failure:
        commands._default_run_update(shlex.join(["sh", str(installer)]))

    assert complaint in str(failure.value)
