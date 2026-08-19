"""Tests for the in-sandbox worker: running ONE already-claimed task.

These used to drive ``issuebot run-one`` through ``CliRunner`` — 93 monkeypatches
across 17 tests, with ``run_work`` itself stubbed out in 16 of them, so what was
actually under test was the argv-and-exit-code shell rather than the work. Now
the work is a function, so it is called; the shell is tested separately, once,
in ``test_cli.py``.
"""

from __future__ import annotations

import pytest

from conftest import (
    VERSION,
    FakeApi,
    config,
    connection,
    ctx,
    needs_in_process,
    sandbox_connection,
)
from issuebot import plugins, runner, worker
from issuebot.config import Connection, conn_setting, harness_settings
from issuebot.contracts import Response, WorkItem
from issuebot.plugins.harnesses.fake.harness import FakeHarness
from issuebot.plugins.workspaces.base import Workspace
from issuebot.process import REAL
from issuebot.reporter import ConsoleReporter
from issuebot.sandbox_protocol import BootMode, WorkerEnv
from issuebot.sessions import SessionStore, default_state_path


class _FakeSourcePlugin:
    """Stands in for the installed source plugin, handing back a fake board.

    The worker asks the source for its own client rather than building one, so
    that ask is the seam a test replaces — one patch, and nothing here names a
    source or a client class."""

    class source:  # noqa: N801 - stands in for the plugin's `source` class attribute
        @staticmethod
        def client(cfg) -> FakeApi:
            return FakeApi()


@pytest.fixture
def cfg():
    """A config with one sandbox-shaped connection named 'parade'.

    Which environment the controller *chose* is not the worker's business — it
    is the process running inside the sandbox that environment booted, and it
    always runs its work locally. So the connection names no environment."""
    return config(connections=[sandbox_connection(name="parade")])


@pytest.fixture
def wire() -> WorkerEnv:
    """What the controller would have sent for an ordinary cold boot."""
    return WorkerEnv()


@pytest.fixture
def ran(monkeypatch: pytest.MonkeyPatch) -> dict:
    """Capture the call the worker makes into ``run_work`` without running it.

    One seam, patched once — the worker's job is to assemble this call
    correctly, and what happens after it is ``run.execute``'s own tests."""
    seen: dict = {}

    def fake_run_work(api, harness, project, work, **kwargs):
        seen["api"] = api
        seen["harness"] = harness
        seen["project"] = project
        seen["work"] = work
        seen.update(kwargs)
        return Response(status="done", result_text="https://example/pr/1")

    # Session resume is gated on the harness declaring it can resume, so the
    # double declares it — no harness plugin is named here, because which ones
    # can resume is not the worker's business.
    harness = FakeHarness()
    harness.resumes_sessions = True

    monkeypatch.setattr(worker, "run_work", fake_run_work)
    monkeypatch.setattr(worker, "source_plugin", lambda name=None: _FakeSourcePlugin())
    monkeypatch.setattr(worker, "harness_for", lambda *a, **k: harness)
    return seen


def _run(cfg, wire, **overrides):
    kwargs = {
        "task_id": "t1",
        "run_id": "R1",
        "connection_name": "parade",
        "kind": "assigned",
        "env": wire,
    }
    kwargs.update(overrides)
    return worker.run_one(cfg, **kwargs)


# ---------------------------------------------------------------------------
# What the worker refuses
# ---------------------------------------------------------------------------


def test_an_unknown_kind_of_work_is_refused(cfg, wire):
    with pytest.raises(worker.UnknownWork, match="banana"):
        _run(cfg, wire, kind="banana")


def test_an_unknown_connection_is_refused(cfg, wire):
    with pytest.raises(worker.UnknownWork, match="nope"):
        _run(cfg, wire, connection_name="nope")


def test_a_version_the_controller_did_not_ask_for_is_refused(cfg, ran):
    """The backstop on the controller's self-update: whatever the sandbox was
    supposed to be, this is the process that would actually do the work, and a
    mismatch here means the alignment did not take. Failing is loud; running
    anyway would be a wrong answer nobody could see."""
    other = "9.8.7"
    wire = WorkerEnv(version=other)

    outcome = _run(cfg, wire)

    assert outcome.status == "failed"
    assert VERSION in outcome.result_text
    assert other in outcome.result_text
    assert ran == {}  # no work was attempted


# ---------------------------------------------------------------------------
# What it hands to the run
# ---------------------------------------------------------------------------


def test_it_runs_the_task_and_returns_its_outcome(cfg, wire, ran):
    outcome = _run(cfg, wire)
    assert outcome.status == "done"
    assert outcome.result_text == "https://example/pr/1"
    assert ran["run_id"] == "R1"


def test_it_never_claims_or_releases(cfg, wire, ran):
    """The controller claimed the run before this sandbox existed and holds the
    lock until the outcome comes back."""
    _run(cfg, wire)
    api = ran["api"]
    assert api.claims == []
    assert api.releases == []


def test_the_work_item_is_rebuilt_from_the_task_and_the_wire(cfg, wire, ran):
    _run(cfg, wire)
    work: WorkItem = ran["work"]
    assert work.task_id == "t1"
    assert work.reference == "ISS-1"  # from the task record
    assert work.kind == "assigned"


def test_mention_context_is_taken_off_the_wire(cfg, ran):
    """A mention's actor and excerpt are not on the task record, so they can only
    reach the sandbox over the wire — this is exactly what once went missing."""
    wire = WorkerEnv(
        actor_name="Ada",
        comment_excerpt="what do you think?",
    )
    _run(cfg, wire, kind="mention")

    work: WorkItem = ran["work"]
    assert work.kind == "mention"
    assert work.actor_name == "Ada"
    assert work.comment_excerpt == "what do you think?"


def test_the_agent_id_is_taken_off_the_wire(cfg, ran):
    """It travels on the runner context, the same value a controller-side run
    reads it from — so a mention session can self-assign inside a sandbox."""
    wire = WorkerEnv(agent_id="u-agent")
    _run(cfg, wire)
    assert ran["ctx"].agent_id == "u-agent"


# ---------------------------------------------------------------------------
# Booting warm, cold and resumed
# ---------------------------------------------------------------------------


@pytest.fixture
def prepped(monkeypatch: pytest.MonkeyPatch) -> list:
    """Record whether the warm-boot top-up was asked of the connection's workspace.

    The seam is `workspace_for`, the same factory `run_work` resolves the real
    workspace through — so what this proves is that the worker asks *whichever*
    workspace the connection wired up, rather than one it named itself. The
    double is a bare `Workspace` subclass: `refresh` has a no-op default on the
    ABC, so recording it here is recording an override of the real hook."""
    calls: list = []

    class Recorder(Workspace):
        name = "recorder"
        produces = frozenset({"answer"})

        def prepare(self, connection, ref, *, settings, proc=REAL):
            raise AssertionError("run_work is stubbed; nothing should prepare here")

        def commit_and_push(self, prepared, message, *, settings, proc=REAL):
            raise AssertionError("run_work is stubbed; nothing should commit here")

        def refresh(self, connection, ref, *, reporter, proc=REAL):
            calls.append((connection, ref))

    monkeypatch.setattr(runner, "workspace_for", lambda connection, ctx: (Recorder(), None))
    return calls


def test_a_warm_boot_tops_up_the_inherited_workspace(cfg, ran, prepped):
    _run(cfg, WorkerEnv(boot=BootMode.WARM))
    assert [ref for _, ref in prepped] == ["ISS-1"]


@pytest.mark.parametrize("boot", [BootMode.COLD, BootMode.RESUME])
def test_no_other_boot_touches_the_workspace(cfg, ran, prepped, boot):
    """A cold boot has nothing to top up, and a resumed one is already this
    task's own branch mid-work — the ordinary prep is idempotent over both."""
    _run(cfg, WorkerEnv(boot=boot))
    assert prepped == []


# ---------------------------------------------------------------------------
# Session resume
# ---------------------------------------------------------------------------


def test_a_resuming_harness_always_gets_a_session_store(cfg, wire, ran):
    """Not gated on cfg.resume_sessions, which is off here: without a store, a
    run that pauses captures no session id, and the resume that follows has no
    conversation to reopen."""
    assert harness_settings(cfg).get("resume_sessions", False) is False
    _run(cfg, wire)
    assert isinstance(ran["ctx"].store, SessionStore)


def test_a_prior_session_is_available_to_resume(cfg, wire, ran):
    SessionStore(default_state_path()).set("t1", "prior-session-id")
    _run(cfg, wire)
    assert ran["ctx"].store.get("t1") == "prior-session-id"


def test_a_harness_without_sessions_degrades_rather_than_crashing(cfg, wire, ran, monkeypatch):
    monkeypatch.setattr(worker, "harness_for", lambda *a, **k: FakeHarness())
    _run(cfg, wire)
    assert ran["ctx"].store is None  # a plain FakeHarness does not resume


def test_session_store_choice_follows_what_the_harness_declares():
    resuming = FakeHarness()
    resuming.resumes_sessions = True

    assert isinstance(worker.session_store(resuming), SessionStore)
    assert worker.session_store(FakeHarness()) is None


# ---------------------------------------------------------------------------
# Reporting back
# ---------------------------------------------------------------------------


def test_the_outcome_is_published_on_both_channels(tmp_path, monkeypatch):
    """The controller parses the sentinel as it streams, and falls back to the
    file when a run was cut short before the line was flushed."""
    path = tmp_path / "result.json"
    monkeypatch.setattr("issuebot.worker.write_result_file", lambda r: path.write_text(r.to_json()))

    result = worker.report(Response(status="done", result_text="https://example/pr/2"))

    assert result.sentinel_line().startswith("##ISSUEBOT-RESULT##")
    assert "https://example/pr/2" in path.read_text()


def test_a_local_connection_works_too(wire, ran, tmp_path):
    """Nothing about the worker is sandbox-specific beyond how it was started."""
    cfg = config(connections=[connection(name="p", folder=str(tmp_path))])
    outcome = worker.run_one(
        cfg, task_id="t1", run_id="R1", connection_name="p", kind="assigned", env=wire
    )
    assert outcome.status == "done"


# ---------------------------------------------------------------------------
# run_work itself: the assembly, unstubbed
# ---------------------------------------------------------------------------
#
# Every test above patches `run_work` out, because their subject is what the
# worker hands it. These do not: the worker's whole claim is that it rebuilds
# the controller's own wiring — the same `runner.wire` and `runner.job_for` —
# rather than a second copy of that reasoning, and a claim nothing exercises
# is how the two paths drifted the last time.


@pytest.fixture
def wired(monkeypatch: pytest.MonkeyPatch) -> FakeHarness:
    """Everything *around* the worker doubled, and `run_work` left real."""
    harness = FakeHarness()
    monkeypatch.setattr(worker, "source_plugin", lambda name=None: _FakeSourcePlugin())
    monkeypatch.setattr(worker, "harness_for", lambda *a, **k: harness)
    return harness


def _local_cfg(folder, **overrides) -> object:
    """A config with one in-place connection working in ``folder``."""
    return config(connections=[connection(name="p", folder=str(folder), **overrides)])


def _somewhere_else() -> str:
    """An installed environment that does *not* run work in this process.

    Read off the registry, never named: what this file needs is "the other kind
    of environment", and spelling one would be the coupling that made the worker
    the reason `local` could not be deleted.

    Both ways there is nothing to compare against — no in-process environment
    installed, or only the one — skip rather than raise: with the in-process
    plugin deleted this behaviour is *gone*, not broken, and a skip says so."""
    here = needs_in_process()
    other = next((name for name in plugins.names_of("environments") if name != here), None)
    if other is None:
        pytest.skip("only one environment installed, so there is no elsewhere to name")
    return other


def test_the_worker_runs_here_whatever_the_connection_named(tmp_path, wire, wired):
    """Inside a sandbox there is no sandbox.

    The connection's own environment is the one that booted *this* machine, so
    the worker must run the work where it stands rather than boot another. It
    resolves that by capability (`runner.in_process_environment`), which is why
    a connection naming a remote environment still launches the harness here.
    """
    worker.run_one(
        _local_cfg(tmp_path, executor=_somewhere_else()),
        task_id="t1",
        run_id="R1",
        connection_name="p",
        kind="assigned",
        env=wire,
    )

    assert wired.calls, "the harness was never launched in this process"


def test_the_worker_really_runs_the_work(tmp_path, wire, wired):
    """End to end through the real assembly: an agent is launched and the run
    comes back with an outcome the controller can release from."""
    outcome = worker.run_one(
        _local_cfg(tmp_path),
        task_id="t1",
        run_id="R1",
        connection_name="p",
        kind="assigned",
        env=wire,
    )

    assert wired.calls, "the harness was never launched"
    assert outcome.status == "done"


def test_the_worker_launches_in_the_connections_own_workspace(tmp_path, wire, wired):
    """`workspace_for` really resolved and prepared one — an in-place connection
    works in its own folder."""
    worker.run_one(
        _local_cfg(tmp_path),
        task_id="t1",
        run_id="R1",
        connection_name="p",
        kind="assigned",
        env=wire,
    )

    assert wired.calls[0].folder == str(tmp_path)


def test_the_worker_launches_the_prompt_its_source_rendered(tmp_path, wire, wired):
    """`source_for` and `job_for` really ran: the prompt is the source's, carries
    the task's ref, and offers only what this workspace can produce (an in-place
    folder connection derives no `Changes`)."""
    worker.run_one(
        _local_cfg(tmp_path),
        task_id="t1",
        run_id="R1",
        connection_name="p",
        kind="assigned",
        env=wire,
    )

    prompt = wired.calls[0].prompt
    assert "ISS-1" in prompt
    assert '"kind": "changes"' not in prompt


# ---------------------------------------------------------------------------
# The repo the linked project supplies
# ---------------------------------------------------------------------------
#
# A sandboxed connection can set no `repo` of its own: the wizard takes it from
# the board's linked project, and `sync_repo` keeps it true afterwards. The
# workspace is *selected* by which keys the connection sets, so the sync must
# land before the selection — synced later, such a connection resolves to the
# unconfigured workspace and the run never touches the repo.


class _LinkedProjectApi(FakeApi):
    """FakeApi plus the two lookups `sync_repo` makes: a board linked to a
    project that carries the repo."""

    def get_board(self, board_id: str) -> dict:
        return {"id": board_id, "project_id": "proj-1"}

    def get_project(self, project_id: str) -> dict:
        return {"id": project_id, "github_repo": {"ssh_url": "git@github.com:acme/web.git"}}


@pytest.fixture
def assembled(monkeypatch: pytest.MonkeyPatch) -> dict:
    """Run `run_work` for real up to the environment, and capture the assembly.

    The environment is the one stub: a real one would clone the (fake) repo.
    Everything before it — `wire`, the sync, `job_for` — runs unpatched, which
    is the subject here."""
    seen: dict = {}

    class _Env:
        def run(self, job, *, reporter):
            seen["job"] = job
            return Response(status="done", result_text="ok")

    def fake_environment_for(wiring, *, name=None):
        seen["wiring"] = wiring
        return _Env()

    monkeypatch.setattr(runner, "environment_for", fake_environment_for)
    return seen


def _run_work(conn: Connection, seen: dict) -> dict:
    """Drive `run_work` over a linked-project board and hand back the capture."""
    worker.run_work(
        _LinkedProjectApi(),
        FakeHarness(),
        conn,
        WorkItem(task_id="t1", reference="ISS-1"),
        run_id="R1",
        ctx=ctx(),
        reporter=ConsoleReporter(ref="ISS-1"),
    )
    return seen


def test_a_repo_from_the_linked_project_selects_the_git_workspace(assembled):
    """A connection with no workspace keys at all, on a board whose project
    supplies the repo: the sync lands the `repo` key before the workspace is
    selected, so the run gets the workspace that can derive changes — asserted
    by capability (`produces`), never by a plugin's name."""
    conn = Connection.model_validate({"name": "parade", "board": "b"})

    seen = _run_work(conn, assembled)

    wiring = seen["wiring"]
    assert "changes" in wiring.workspace.produces
    assert conn_setting(wiring.connection, "repo") == "git@github.com:acme/web.git"


def test_a_task_branch_connection_without_its_own_repo_keeps_the_changes_permit(assembled):
    """The sandbox worker pushes: a connection that cuts a task branch but takes
    its `repo` from the linked project still holds the `changes` permit."""
    conn = Connection.model_validate({"name": "parade", "board": "b", "git_init": "branch"})

    seen = _run_work(conn, assembled)

    assert "changes" in seen["job"].permits
