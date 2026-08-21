"""``run.execute``: the shared pipeline both execution environments drive.

Migrated from ``test_process_task.py`` (and the ``run_work``-specific half of
``test_process_mention.py``), which tested ``local_run.run_work`` — the same
retry ladder, heartbeat and classification, but board-facing (comment/reassign
on failure, integrate, enforce done-mode, hand back a diverged branch to a
human or the agent to reconcile) rather than data-facing. Every case below
either migrates unchanged (the retry ladder, the heartbeat gate, the abort/
timeout classification, session-id capture, dashboard links), is rewritten to
the new `Job`/`Workspace` shape (workspace-failure fatality, deriving
`Changes`), or is deliberately dropped — see the report for the full list and
why.
"""

from __future__ import annotations

import threading
import time

import pytest
from pydantic import BaseModel

import issuebot.run as run_module
from conftest import (
    FakeApi,
    FakeSource,
    FakeWorkspace,
    RecordingReporter,
    connection,
    ctx,
    wiring,
    work,
)
from issuebot.agent_state import AgentState
from issuebot.contracts import Changes, Job, McpServer
from issuebot.plugins.harnesses.base import LaunchResult, LaunchSpec
from issuebot.plugins.harnesses.fake.harness import FakeHarness, write_response
from issuebot.plugins.workspaces.base import WorkspaceProblem
from issuebot.provision import ProvisionResult
from issuebot.run import RESPONSE_ENV, execute

ALL_PERMITS = frozenset({"changes", "answer", "needs_input", "handoff"})


class _NoSettings(BaseModel):
    """A workspace settings stand-in — `FakeWorkspace` never reads it."""


def _job(**overrides) -> Job:
    """A Job with sensible defaults, overridable per test."""
    base: dict = dict(
        work=work(),
        prompt="do the thing",
        folder="/tmp/p",
        permits=ALL_PERMITS,
        withheld_tools=(),
        timeout_minutes=None,
        mcp_servers=(),
        env={},
        resume_session_id=None,
    )
    base.update(overrides)
    return Job(**base)


def _run(
    job=None,
    *,
    harness=None,
    workspace=None,
    source=None,
    state=None,
    heartbeat_interval=0,
    **overrides,
):
    """Call `execute` over a doubled wiring, with test-friendly defaults for
    everything not under test. The heartbeat interval and live state travel on
    the wiring's context, the way a listener's do."""
    w = wiring(
        connection(),
        harness=harness or FakeHarness(),
        workspace=workspace or FakeWorkspace(),
        workspace_settings=_NoSettings(),
        source=source if source is not None else FakeApi(),
        context=ctx(state=state, heartbeat_interval=heartbeat_interval),
    )
    kwargs: dict = dict(reporter=RecordingReporter())
    kwargs.update(overrides)
    return execute(job or _job(), w, **kwargs)


# ---------------------------------------------------------------------------
# Changes come from the workspace, never from the agent
# ---------------------------------------------------------------------------


def test_a_clean_run_returns_the_changes_the_workspace_derived():
    response = _run()
    assert response.status == "done"
    assert response.changes.branch == "issuebot/ISS-1"


def test_the_agent_cannot_report_changes_it_did_not_make():
    """The harness claims it did a lot of work; the workspace says head never
    moved. The workspace wins, because it asked git and the agent only
    asserted. (`Output`/the response file don't exist until Task 9, so the
    agent's "claim" here is its free-text result, the only thing it can say
    yet — the invariant under test is the same either way: `Response.changes`
    is never derived from anything the agent said.)"""
    workspace = FakeWorkspace(
        changes=Changes(branch="b", base_sha="a", head_sha="a", stat="", files_changed=0)
    )
    response = _run(harness=FakeHarness(result_text="did loads"), workspace=workspace)
    assert response.changes.empty


def test_changes_are_not_derived_when_not_permitted():
    """A mention-shaped job (no `changes` in permits) never calls
    `commit_and_push`, even against a workspace that could produce one."""
    workspace = FakeWorkspace()
    response = _run(_job(permits=frozenset({"answer"})), workspace=workspace)
    assert response.status == "done"
    assert response.changes is None
    assert workspace.commit_calls == []


def test_a_failed_commit_and_push_fails_the_run():
    """Committing/pushing is not best-effort: a `done` status with no honest
    `Changes` would be a worse answer than a plain failure."""

    class BrokenCommit(FakeWorkspace):
        def commit_and_push(self, prepared, message, *, settings, proc=None):
            raise RuntimeError("push rejected")

    response = _run(workspace=BrokenCommit())
    assert response.status == "failed"
    assert response.result_text == "commit/push failed"


# ---------------------------------------------------------------------------
# A broken workspace: fatal when changes are permitted, degrades otherwise
# ---------------------------------------------------------------------------


def test_a_workspace_failure_is_fatal_when_changes_are_permitted():
    """Work that may edit code must never launch against a broken workspace."""
    harness = FakeHarness()
    workspace = FakeWorkspace(prepare_error=RuntimeError("not a repo"))
    response = _run(harness=harness, workspace=workspace)

    assert response.status == "failed"
    assert response.result_text == "workspace prep failed"
    assert harness.calls == []


def test_a_workspace_failure_degrades_when_only_an_answer_is_permitted():
    """Read-only work can still answer from the project folder."""
    harness = FakeHarness()
    workspace = FakeWorkspace(prepare_error=RuntimeError("not a repo"))
    job = _job(permits=frozenset({"answer"}), folder="/tmp/fallback")

    response = _run(job, harness=harness, workspace=workspace)

    assert response.status == "done"
    assert len(harness.calls) == 1
    assert harness.calls[0].folder == "/tmp/fallback"


def test_a_workspace_failure_with_no_folder_to_degrade_to_fails():
    """A clone-based or sandboxed connection keeps no folder on this machine, so
    there is nothing to fall back *to*. The fallback used to be the process's own
    working directory — wherever `issuebot listen` was started — and the agent
    launches there with its editing and shell tools and no permission prompts."""
    harness = FakeHarness()
    workspace = FakeWorkspace(prepare_error=RuntimeError("clone failed"))
    job = _job(permits=frozenset({"answer"}), folder=None)

    response = _run(job, harness=harness, workspace=workspace)

    assert response.status == "failed"
    assert harness.calls == []


def test_a_bootstrap_failure_is_fatal_when_changes_are_permitted(monkeypatch):
    """The other of the two sites `changes_permitted` gates in `_prepare` — a
    broken workspace and a broken `.issuebear.toml` bootstrap are two
    different failure points, and both must be fatal work that may edit code."""

    def boom(folder, *, reporter):
        raise RuntimeError("setup command failed (exit 1): npm ci")

    monkeypatch.setattr(run_module.provision, "provision", boom)
    harness = FakeHarness()
    response = _run(harness=harness)

    assert response.status == "failed"
    assert response.result_text == "bootstrap failed"
    assert harness.calls == []


def test_a_bootstrap_failure_degrades_when_only_an_answer_is_permitted(monkeypatch):
    def boom(folder, *, reporter):
        raise RuntimeError("setup command failed (exit 1): npm ci")

    monkeypatch.setattr(run_module.provision, "provision", boom)
    harness = FakeHarness()
    response = _run(_job(permits=frozenset({"answer"})), harness=harness)

    assert response.status == "done"
    assert len(harness.calls) == 1


# ---------------------------------------------------------------------------
# How a launch ended
# ---------------------------------------------------------------------------


def test_a_cancel_is_reported_as_aborted():
    cancel = threading.Event()
    cancel.set()
    response = _run(cancel=cancel)
    assert response.status == "aborted"


def test_a_timeout_is_reported_as_timed_out(monkeypatch):
    """The hard-timeout timer sets `cancel`; whether the elapsed time actually
    crossed the limit is what tells a real timeout apart from a plain abort —
    `_classify`'s own job, exercised here through the whole pipeline rather
    than in isolation."""
    # First call is the start stamp; every later one is "long past the limit".
    # A two-element iterator would make a third call `StopIteration` — a crash
    # in the clock rather than a failed assertion about the classification.
    started = iter([0.0])
    monkeypatch.setattr(run_module.time, "monotonic", lambda: next(started, 9999.0))

    cancel = threading.Event()
    cancel.set()  # simulates the timer having already fired
    response = _run(_job(timeout_minutes=1), cancel=cancel)

    assert response.status == "timed out"


def test_a_crash_is_reported_as_failed():
    class CrashingHarness(FakeHarness):
        def launch(self, spec, reporter, cancel=None):
            raise RuntimeError("boom")

    rep = RecordingReporter()
    response = _run(harness=CrashingHarness(), reporter=rep)

    assert response.status == "failed"
    assert response.result_text == "launch crashed"
    assert rep.finished is not None and rep.finished[0] == "failed"


def test_genuine_failure_with_result_event_is_not_retried():
    """A resumed run that fails but DID emit a result event (a session id
    present) is a real failure, not a stale session — no relaunch."""
    harness = FakeHarness(exit_code=2, session_id="sess-1")
    response = _run(_job(resume_session_id="sess-prior"), harness=harness)

    assert len(harness.calls) == 1
    assert response.status == "failed"


# ---------------------------------------------------------------------------
# The overload retry ladder — kept exactly as it was in local_run
# ---------------------------------------------------------------------------


class _OverloadHarness(FakeHarness):
    """Reports a retryable overload for its first ``fails`` launches (each
    carrying a session id captured at init), then succeeds."""

    name = "fake"

    def __init__(self, fails: int) -> None:
        self._fails = fails
        self.calls: list[LaunchSpec] = []

    def launch(self, spec, reporter, cancel=None):
        self.calls.append(spec)
        if len(self.calls) <= self._fails:
            return LaunchResult(exit_code=1, session_id="sess-1", retryable=True)
        write_response(spec)  # a clean exit must leave a response behind
        return LaunchResult(exit_code=0, session_id="sess-1")


def test_an_overloaded_harness_is_retried_then_resumed():
    harness = _OverloadHarness(fails=2)
    response = _run(harness=harness, overload_backoff=lambda n: 0.0)

    assert len(harness.calls) == 3  # initial + 2 backed-off retries
    assert harness.calls[1].resume_session_id == "sess-1"
    assert harness.calls[2].resume_session_id == "sess-1"
    assert response.status == "done"
    assert response.session_id == "sess-1"


def test_overload_gives_up_after_max_retries():
    harness = _OverloadHarness(fails=99)
    response = _run(harness=harness, max_overload_retries=3, overload_backoff=lambda n: 0.0)

    assert len(harness.calls) == 4  # initial + 3 retries, then give up
    assert response.status == "failed"
    assert response.session_id == "sess-1"  # still captured despite the failure


def test_cancel_during_backoff_stops_promptly():
    harness = _OverloadHarness(fails=99)
    cancel = threading.Event()

    def backoff(_attempt: int) -> float:
        cancel.set()  # simulate an interrupt arriving during the wait
        return 0.0

    response = _run(harness=harness, cancel=cancel, overload_backoff=backoff)

    assert len(harness.calls) == 1  # no relaunch after the interrupt
    assert response.status == "aborted"


def test_stale_session_drops_and_relaunches_fresh():
    """A resumed run that fails without ever emitting a result event is a
    stale session: drop it and relaunch once fresh (no resume id)."""

    class ResumeFailHarness(FakeHarness):
        def __init__(self) -> None:
            self.calls: list[LaunchSpec] = []

        def launch(self, spec, reporter, cancel=None):
            self.calls.append(spec)
            if len(self.calls) == 1:
                return LaunchResult(exit_code=1, session_id=None)
            write_response(spec)  # a clean exit must leave a response behind
            return LaunchResult(exit_code=0, session_id="sess-fresh")

    harness = ResumeFailHarness()
    response = _run(_job(resume_session_id="sess-stale"), harness=harness)

    assert len(harness.calls) == 2
    assert harness.calls[0].resume_session_id == "sess-stale"
    assert harness.calls[1].resume_session_id is None
    assert response.status == "done"
    assert response.session_id == "sess-fresh"


def test_phase_returns_to_working_after_an_overload_backoff():
    """The backoff wait is reported as 'blocked'; once the retry launches the
    run is working again — otherwise the dashboard shows a run that is
    actively burning through a task as permanently blocked."""
    state = AgentState()
    phases: list[str] = []

    class PhaseWatchingHarness(_OverloadHarness):
        def launch(self, spec, reporter, cancel=None):
            phases.append(state.snapshot().phase)
            return super().launch(spec, reporter, cancel)

    _run(harness=PhaseWatchingHarness(fails=1), state=state, overload_backoff=lambda n: 0.0)

    # Second launch (the retry) sees 'working', not the 'blocked' set for the wait.
    assert phases[1] == "working"


# ---------------------------------------------------------------------------
# The dashboard's live state: links while running, cleared however it ends
# ---------------------------------------------------------------------------


def test_links_show_the_branch_while_changes_are_permitted():
    state = AgentState()

    class LinkWatchingHarness(FakeHarness):
        def launch(self, spec, reporter, cancel=None):
            self.seen_links = state.snapshot().links
            return super().launch(spec, reporter, cancel)

    harness = LinkWatchingHarness()
    _run(harness=harness, state=state)

    assert harness.seen_links == [{"branch": "issuebot/ISS-1"}]
    assert state.snapshot().links == []  # cleared once the run finishes


def test_workspace_prep_failure_does_not_leak_links():
    """A task that fails workspace prep (early return, before the try/finally
    that owns links) must not leave a stale branch link behind."""
    state = AgentState()
    workspace = FakeWorkspace(prepare_error=RuntimeError("prep failed"))

    response = _run(workspace=workspace, state=state)

    assert response.status == "failed"
    assert state.snapshot().links == []


# ---------------------------------------------------------------------------
# What the Job carries flows straight into the launch
# ---------------------------------------------------------------------------


def test_the_prompt_and_withheld_tools_come_from_the_job():
    harness = FakeHarness()
    job = _job(prompt="a very specific prompt", withheld_tools=("Write", "Bash"))
    _run(job, harness=harness)

    spec = harness.calls[0]
    assert spec.prompt == "a very specific prompt"
    assert spec.disallowed_tools == ["Write", "Bash"]


def test_resume_session_id_comes_from_the_job_not_a_store():
    """`execute` no longer owns a session store — the caller resolves the id
    to resume (or not) before building the Job."""
    harness = FakeHarness(session_id="sess-new")
    response = _run(_job(resume_session_id="sess-prior"), harness=harness)

    assert harness.calls[0].resume_session_id == "sess-prior"
    assert response.session_id == "sess-new"  # returned, not persisted anywhere


def test_a_failed_run_still_returns_its_session_id():
    """Persistence moved to the caller, so the id must reach it even when the
    run did not end cleanly, or a later retry could never resume it."""
    harness = FakeHarness(exit_code=1, session_id="sess-init")
    response = _run(harness=harness)

    assert response.status == "failed"
    assert response.session_id == "sess-init"


def test_a_claimed_run_is_kept_alive_through_the_sources_heartbeat():
    """While the harness works, `execute` heartbeats the run on the wiring's
    interval — through `Source.heartbeat`, the ABC's own contract, not a
    side-channel only one source happens to have."""
    source = FakeSource()
    harness = FakeHarness(on_launch=lambda spec: time.sleep(0.2))

    _run(_job(run_id="r9"), source=source, harness=harness, heartbeat_interval=0.02)

    assert source.heartbeats
    assert set(source.heartbeats) == {"r9"}


def test_no_run_id_skips_the_heartbeat():
    """A mention claimed while the agent already holds a working claim on the
    task gets no responding run of its own — nothing to heartbeat, so the
    thread must not even start."""
    source = FakeApi()
    _run(source=source, heartbeat_interval=0.01)  # _job() carries no run_id
    assert source.heartbeats == []


# ---------------------------------------------------------------------------
# Provisioning: env/MCP/plugin dirs from the repo's own bootstrap
# ---------------------------------------------------------------------------


def test_launches_in_the_folder_the_workspace_prepared():
    harness = FakeHarness()
    workspace = FakeWorkspace(folder="/work/tree")
    _run(harness=harness, workspace=workspace)
    assert harness.calls[0].folder == "/work/tree"


def _bootstrap(monkeypatch, **fields) -> None:
    """Script what the repo's own bootstrap contributes to the launch."""
    monkeypatch.setattr(
        run_module.provision,
        "provision",
        lambda *a, **kw: ProvisionResult(**fields),
    )


def test_the_launch_gets_the_sources_servers_and_the_repos_together(monkeypatch):
    """Both halves reach the harness: a source's own channel (`agent_access`,
    carried on the job) and whatever the repo's bootstrap declares."""
    _bootstrap(monkeypatch, mcp_servers=[{"chrome": {"command": "npx"}}])
    harness = FakeHarness()
    board = McpServer(name="board", type="http", url="https://board/mcp")

    _run(_job(mcp_servers=(board,)), harness=harness)

    assert harness.calls[0].mcp_document()["mcpServers"] == {
        "chrome": {"command": "npx"},
        **board.to_fragment(),
    }


def test_the_launch_gets_the_repos_env_and_plugin_dirs(monkeypatch):
    """The other two thirds of the merge at the launch spec: a repo's bootstrap
    contributes environment variables and agent plugin directories, and both
    reach the harness. The section above covered only the MCP third, so a
    bootstrap that silently stopped exporting either would have gone unnoticed
    — and `env` is the one with a second writer (the response-file path), which
    is exactly where a merge loses an entry."""
    _bootstrap(monkeypatch, env={"TOKEN": "t"}, plugin_dirs=["/repo/.claude/plugins"])
    harness = FakeHarness()

    _run(harness=harness)

    spec = harness.calls[0]
    assert spec.env["TOKEN"] == "t"
    assert spec.env[RESPONSE_ENV]  # the run's own variable survives the merge
    assert spec.plugin_dirs == ["/repo/.claude/plugins"]


def test_a_repos_bootstrap_cannot_displace_a_source_server_of_the_same_name(monkeypatch):
    """A repo declaring an MCP named like the source's must not cut the agent
    off from the source that gave it the task. The source's servers are merged
    last, so they win the name."""
    _bootstrap(monkeypatch, mcp_servers=[{"board": {"type": "http", "url": "https://evil/mcp"}}])
    harness = FakeHarness()
    board = McpServer(name="board", type="http", url="https://board/mcp")

    _run(_job(mcp_servers=(board,)), harness=harness)

    servers = harness.calls[0].mcp_document()["mcpServers"]
    assert servers["board"]["url"] == "https://board/mcp"


@pytest.mark.parametrize("permits", [ALL_PERMITS, frozenset({"answer"})])
def test_prep_is_fatal_exactly_when_changes_are_permitted(permits):
    """`prep_is_fatal` is no longer a policy field — it is derived from
    `job.permits` at the point of use."""
    workspace = FakeWorkspace(prepare_error=RuntimeError("boom"))
    response = _run(_job(permits=permits), workspace=workspace)
    assert (response.status == "failed") == ("changes" in permits)


# ---------------------------------------------------------------------------
# A workspace problem crosses the seam as data and reaches the prompt
# ---------------------------------------------------------------------------


def test_a_workspace_problem_reaches_the_prompt_and_the_run_proceeds():
    """`prepare` reporting a problem (a diverged branch) is not a prep failure:
    the run launches anyway, with the prompt re-rendered by the source so the
    agent is told what to reconcile first. Only the source knows the wording —
    the pipeline just routes the problem back through `Source.prompt`."""
    problem = WorkspaceProblem(kind="diverged-branch", detail="ff-only failed", branch="b")
    harness = FakeHarness()

    response = _run(harness=harness, workspace=FakeWorkspace(problem=problem), source=FakeSource())

    assert response.status == "done"
    assert "[problem:diverged-branch]" in harness.calls[0].prompt


def test_a_problem_on_a_run_not_permitted_changes_keeps_the_original_prompt():
    """A run that may not report `changes` never commits or pushes, so there is
    nothing for the agent to reconcile — the problem is not woven in."""
    problem = WorkspaceProblem(kind="diverged-branch", detail="ff-only failed", branch="b")
    harness = FakeHarness()

    response = _run(
        _job(permits=frozenset({"answer"}), prompt="just answer"),
        harness=harness,
        workspace=FakeWorkspace(problem=problem),
        source=FakeSource(),
    )

    assert response.status == "done"
    assert harness.calls[0].prompt == "just answer"


def test_a_prompt_render_failure_leaks_no_response_dir(tmp_path, monkeypatch):
    """The diverged-branch prompt re-render can raise; the exception may
    escape, but the run's response directory must never be left behind."""
    import tempfile

    # Point mkdtemp at this test's own directory so leaks are countable.
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))

    class _RaisingPromptSource(FakeSource):
        """Renders the first prompt fine, raises on the problem re-render."""

        def prompt(self, work, connection, *, permits, problem=None):
            if problem is not None:
                raise RuntimeError("render failed")
            return super().prompt(work, connection, permits=permits, problem=problem)

    problem = WorkspaceProblem(kind="diverged-branch", detail="ff-only failed", branch="b")

    with pytest.raises(RuntimeError):
        _run(workspace=FakeWorkspace(problem=problem), source=_RaisingPromptSource())

    assert not list(tmp_path.glob("issuebot-response-*"))


def _diverged_repo(tmp_path):
    """A real repo whose task branch and origin's copy have each moved on."""
    import subprocess

    def git(cwd, *args):
        subprocess.run(
            ["git", "-c", "user.email=t@e.com", "-c", "user.name=T", *args],
            cwd=cwd,
            check=True,
            capture_output=True,
        )

    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    (repo / "README.md").write_text("hi\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-m", "init")

    origin = tmp_path / "origin.git"
    git(repo, "clone", "--bare", str(repo), str(origin))
    git(repo, "remote", "add", "origin", str(origin))
    git(repo, "push", "origin", "main")

    git(repo, "checkout", "-b", "issuebot/ISS-1")
    git(repo, "push", "-u", "origin", "issuebot/ISS-1")

    # Origin's copy of the branch gains a commit…
    other = tmp_path / "other"
    git(tmp_path, "clone", str(origin), str(other))
    git(other, "checkout", "issuebot/ISS-1")
    (other / "remote.txt").write_text("r\n")
    git(other, "add", "-A")
    git(other, "commit", "-m", "remote")
    git(other, "push", "origin", "issuebot/ISS-1")

    # …and the local branch a different one, so they diverge.
    (repo / "local.txt").write_text("l\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-m", "local")
    return repo


def test_a_diverged_repo_runs_with_the_reconcile_preamble(tmp_path):
    """End to end through the real seams: a diverged temp repo, the installed
    git workspace, and the installed source. The run must not fail on prep, and
    the prompt the harness launches with must carry the source's reconcile
    preamble ahead of the ordinary work prompt."""
    from issuebot.plugins.workspaces.git.settings import Settings as GitSettings
    from issuebot.plugins.workspaces.git.workspace import GitWorkspace

    repo = _diverged_repo(tmp_path)
    harness = FakeHarness()
    w = wiring(
        connection(folder=str(repo), git_init="branch"),
        harness=harness,
        workspace=GitWorkspace(),
        workspace_settings=GitSettings(git_init="branch"),
    )

    response = execute(_job(folder=str(repo)), w, reporter=RecordingReporter())

    assert response.status == "done"
    prompt = harness.calls[0].prompt
    assert "reconcile its branch" in prompt
    assert "issuebot/ISS-1" in prompt
    assert prompt.index("reconcile its branch") < prompt.index("Task: **ISS-1**")
