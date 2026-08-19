"""Tests for the process adapter — including the parts that were never run.

The cancel ladder (SIGTERM, grace, SIGKILL) existed in three copies, each the
default of an injectable parameter, so every test injected past all three and
none of them ever executed. There is one copy now, and these run it against real
processes: the semantics are the kernel's, and a double would only assert that
the double works.
"""

from __future__ import annotations

import sys
import threading
import time

import pytest

from issuebot.process import NOT_RUN, Completed, RealProcess, RecordingProcess

# A python that is definitely on this machine — the interpreter running us.
PY = sys.executable


@pytest.fixture
def proc() -> RealProcess:
    return RealProcess()


# ---------------------------------------------------------------------------
# Completed
# ---------------------------------------------------------------------------


def test_a_zero_exit_is_ok():
    assert Completed(["x"], 0).ok is True
    assert Completed(["x"], 1).ok is False


def test_the_message_prefers_stderr_but_falls_back_to_stdout():
    """A failing program usually explains itself on stderr — but not all of them
    do, and an error message that says nothing is worse than a noisy one."""
    assert Completed(["x"], 1, out="out", err="err").message == "err"
    assert Completed(["x"], 1, out="out", err="   ").message == "out"
    assert Completed(["x"], 1).message == ""


# ---------------------------------------------------------------------------
# run
# ---------------------------------------------------------------------------


def test_it_captures_output_and_exit_code(proc):
    r = proc.run([PY, "-c", "import sys; print('hi'); sys.exit(3)"])
    assert r.code == 3
    assert r.out.strip() == "hi"


def test_a_missing_program_is_a_failed_run_not_an_exception(proc):
    """Every caller already branches on a non-zero exit; none of them expect a
    traceback because a machine happens not to have `gh` installed."""
    r = proc.run(["issuebot-definitely-not-a-real-binary"])
    assert r.code == NOT_RUN
    assert not r.ok
    assert r.message


def test_a_missing_working_directory_is_a_failed_run_too(proc, tmp_path):
    r = proc.run([PY, "-c", "pass"], cwd=str(tmp_path / "nope"))
    assert r.code == NOT_RUN


def test_it_runs_in_the_given_directory(proc, tmp_path):
    r = proc.run([PY, "-c", "import os; print(os.getcwd())"], cwd=str(tmp_path))
    assert str(tmp_path) in r.out


def test_an_env_overlay_is_applied_on_top_of_the_process_env(proc):
    r = proc.run(
        [PY, "-c", "import os; print(os.environ['ISSUEBOT_TEST_VAR'])"],
        env={"ISSUEBOT_TEST_VAR": "set"},
    )
    assert r.out.strip() == "set"


def test_an_empty_overlay_value_removes_the_variable(proc, monkeypatch):
    """A present-but-blank credential is still a credential to most CLIs, so
    "unset this" needs a spelling that survives the overlay — that is what lets
    one connection's own credential stop an ambient one shadowing it."""
    monkeypatch.setenv("ISSUEBOT_TEST_VAR", "ambient")
    script = "import os; print('ISSUEBOT_TEST_VAR' in os.environ)"

    assert proc.run([PY, "-c", script], env={"ISSUEBOT_TEST_VAR": ""}).out.strip() == "False"
    assert proc.run([PY, "-c", script]).out.strip() == "True"


# ---------------------------------------------------------------------------
# spawn
# ---------------------------------------------------------------------------


def test_it_streams_lines_as_they_arrive(proc):
    lines: list[str] = []
    code = proc.spawn([PY, "-c", "print('one'); print('two')"], on_line=lines.append)
    assert code == 0
    assert lines == ["one", "two"]


def test_it_streams_stderr_too(proc):
    """The agent's own errors are part of its transcript, so stderr is merged
    into the stream rather than discarded."""
    lines: list[str] = []
    proc.spawn([PY, "-c", "import sys; print('bad', file=sys.stderr)"], on_line=lines.append)
    assert lines == ["bad"]


def test_a_missing_program_reports_rather_than_raising(proc):
    lines: list[str] = []
    code = proc.spawn(["issuebot-definitely-not-a-real-binary"], on_line=lines.append)
    assert code == NOT_RUN
    assert lines and "could not start" in lines[0]


def test_cancelling_terminates_a_running_child(proc):
    """The ladder, actually executed: a child that would run for a minute is
    signalled and gone well inside it."""
    cancel = threading.Event()
    threading.Timer(0.2, cancel.set).start()

    started = time.monotonic()
    code = proc.spawn(
        [PY, "-c", "import time; time.sleep(60)"], on_line=lambda _: None, cancel=cancel
    )
    elapsed = time.monotonic() - started

    assert elapsed < 10, "the child outlived its cancellation"
    assert code != 0


def test_cancelling_kills_a_child_that_ignores_the_signal(proc):
    """SIGTERM is a request. A child that traps it is killed after the grace
    period rather than hanging the runner — the half of the ladder that a
    well-behaved test program would never reach."""
    ignores_sigterm = (
        "import signal, time\n"
        "signal.signal(signal.SIGTERM, lambda *a: None)\n"
        "print('ready', flush=True)\n"
        "time.sleep(60)\n"
    )
    cancel = threading.Event()
    lines: list[str] = []

    def cancel_once_ready(line: str) -> None:
        lines.append(line)
        cancel.set()

    started = time.monotonic()
    code = proc.spawn([PY, "-c", ignores_sigterm], on_line=cancel_once_ready, cancel=cancel)
    elapsed = time.monotonic() - started

    assert lines == ["ready"]
    assert elapsed < 30, "the child was never hard-killed"
    assert code != 0


def test_an_uncancelled_run_is_left_alone(proc):
    cancel = threading.Event()  # never set
    code = proc.spawn([PY, "-c", "print('done')"], on_line=lambda _: None, cancel=cancel)
    assert code == 0


# ---------------------------------------------------------------------------
# The test adapter
# ---------------------------------------------------------------------------


def test_the_recording_adapter_answers_the_first_matching_reply():
    proc = RecordingProcess(replies={"git status": Completed([], 0, out=" M f")})

    assert proc.run(["git", "status", "--porcelain"]).out == " M f"
    assert proc.run(["git", "log"]).ok  # unscripted commands succeed silently


def test_the_recording_adapter_records_what_it_was_asked():
    proc = RecordingProcess()
    proc.run(["git", "status"], cwd="/repo", env={"A": "1"})

    assert proc.calls == [["git", "status"]]
    assert proc.cwds == ["/repo"]
    assert proc.envs == [{"A": "1"}]


def test_the_recording_adapter_stops_streaming_when_cancelled():
    proc = RecordingProcess(lines=["one", "two", "three"])
    cancel = threading.Event()
    seen: list[str] = []

    def stop_after_first(line: str) -> None:
        seen.append(line)
        cancel.set()

    proc.spawn(["x"], on_line=stop_after_first, cancel=cancel)
    assert seen == ["one"]
