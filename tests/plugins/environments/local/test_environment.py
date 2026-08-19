"""The local environment: run the job in this process, in the workspace we were given.

`executor = "local"` is the default, so every connection that says nothing gets
this. That makes "the setting resolves" the uninteresting half — what matters is
that the agent is really launched, in the right folder, and the run comes back
done.

The generic obligations every environment shares (name, `run` returning a
`Response`, ...) are in ``../test_conformance.py``; only what is local's own
lives here.
"""

from __future__ import annotations

from pathlib import Path

from conftest import FakeSource, RecordingReporter, connection, wiring, work
from issuebot import release
from issuebot.plugins.environments.local.environment import LocalEnvironment
from issuebot.plugins.harnesses.fake.harness import FakeHarness
from issuebot.runner import job_for


def _run(conn, harness: FakeHarness) -> object:
    """Wire the connection the way a listener does, then run one job in it."""
    # A source double rather than a real source: what is under test is the
    # environment, and a real one would make this file a test of two plugins
    # at once.
    w = wiring(conn, harness=harness, source=FakeSource())
    assert isinstance(w.environment, LocalEnvironment)

    job = job_for(work(), w, run_id="r1")
    return w.environment.run(job, reporter=RecordingReporter())


def test_a_connection_that_names_no_environment_runs_here(
    tmp_path: Path,
    monkeypatch,
):
    """The default resolves to this environment, and it really does the work —
    the agent is launched and the run comes back done."""
    harness = FakeHarness()
    monkeypatch.setattr(release, "is_installed_wheel", lambda: False)

    response = _run(connection(folder=str(tmp_path)), harness)

    assert harness.calls, "the harness was never launched"
    assert response.status == "done"


def test_the_launch_runs_in_the_workspace_the_connection_names(tmp_path: Path):
    """A folder connection works in place, so that is where the agent lands.

    This is the difference between running locally and running in a sandbox:
    there is no boot, no clone and no transfer — the folder on this machine is
    the workspace."""
    harness = FakeHarness()

    _run(connection(folder=str(tmp_path)), harness)

    assert harness.calls[0].folder == str(tmp_path)
