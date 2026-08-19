"""Wiring a connection to an environment: which one, and what it is asked to run.

The registry half (a setting resolves to an environment, an unknown one says
what exists) plus the half that used to have no home at all — the `Job` the
controller builds. Both live here because both are `runner`'s own factories,
and because between them they are the whole answer to "does a connection
actually run its task".

The environments themselves are covered generically in
``tests/plugins/environments/test_conformance.py`` and, for the sandbox
controller, in ``tests/test_sandbox.py``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from conftest import (
    FakeSource,
    RecordingReporter,
    connection,
    needs_in_process,
    sandbox_connection,
    wiring,
    work,
)
from issuebot import plugins
from issuebot.contracts import McpServer, Response, WorkItem
from issuebot.plugins.base import EnvironmentPlugin
from issuebot.plugins.harnesses.fake.harness import FakeHarness
from issuebot.runner import in_process_environment, job_for


def _build(conn, *, harness=None, source=None):
    """The environment a connection resolves to, wired the way a listener wires it."""
    return wiring(conn, harness=harness, source=source).environment


def _job(conn, *, source, item: WorkItem | None = None):
    """The job a listener would build for this connection and work item."""
    return job_for(item or work(), wiring(conn, source=source), run_id="r1")


# ---------------------------------------------------------------------------
# Which environment a connection resolves to
# ---------------------------------------------------------------------------


def test_a_connection_naming_the_in_process_environment_gets_one_that_runs_here():
    """Asserted on the capability rather than a class, and the class is never
    imported here: naming one environment in a core test file is the coupling
    the plugin boundary exists to prevent — and it is how the worker
    came to be the reason `local` could not be deleted."""
    environment = _build(connection(executor=needs_in_process()))

    assert environment.runs_in_process


def _environments(monkeypatch, **runs_here: bool) -> None:
    """Pretend exactly these environments are installed, each declaring
    ``runs_in_process`` as given.

    Patching `all_of` rather than `names_of` is deliberate, for the reason
    spelled out at `tests/test_config.py`'s `_installed`: both `names_of` and
    `get` read through `all_of`, so every name here resolves for real and a
    guard that wrongly picked one would *succeed* rather than raise from some
    other code path."""
    installed = {
        name: EnvironmentPlugin(
            name=name,
            environment=type(
                f"Env_{name}",
                (),
                {
                    "name": name,
                    "runs_in_process": here,
                    # Built by `environment_for` over the wiring, like every
                    # real environment.
                    "__init__": lambda self, *_args, **_kwargs: None,
                },
            ),
        )
        for name, here in runs_here.items()
    }

    # Only this axis is replaced: `_build` resolves a workspace through the same
    # registry, and answering {} for every other kind would break it for a reason
    # that has nothing to do with what is under test.
    real = plugins.all_of
    monkeypatch.setattr(
        plugins, "all_of", lambda kind: installed if kind == "environments" else real(kind)
    )


def test_a_connection_naming_no_environment_gets_the_one_installed(monkeypatch):
    """The `executor_name` fallback, reached the way a listener reaches it.

    The claim the old `isinstance(_build(connection()), LocalEnvironment)` made
    — that a connection naming *nothing* still resolves — kept, without a core
    test naming the plugin it resolved to."""
    _environments(monkeypatch, only=True)

    assert _build(connection(executor=None)).name == "only"


def test_the_environment_that_runs_work_here_is_found_by_what_it_declares(monkeypatch):
    """By capability, never by name — which is what lets the in-sandbox worker
    ask for "run it where I am" without knowing any environment exists."""
    _environments(monkeypatch, here=True, elsewhere=False)

    assert in_process_environment() == "here"


def test_no_in_process_environment_is_a_sentence_not_a_crash(monkeypatch):
    """Delete the only environment that runs work here and the worker must say
    so, not die of a StopIteration five frames down."""
    _environments(monkeypatch, elsewhere=False)

    with pytest.raises(plugins.UnknownPlugin, match="0 installed environments run work"):
        in_process_environment()


def test_two_in_process_environments_refuse_to_guess(monkeypatch):
    """Taking whichever sorted first is how a privileged default gets reinvented
    under a new name."""
    _environments(monkeypatch, here=True, also_here=True)

    with pytest.raises(plugins.UnknownPlugin, match="2 installed environments run work"):
        in_process_environment()


def test_an_unknown_environment_names_the_ones_that_exist():
    """The name is checked at config load against the same registry, so reaching
    this means a hand-built connection — the message still has to say what is
    installed.

    What "installed" means comes from the registry rather than a written-down
    list, so this stays true when an environment plugin is added or deleted."""
    conn = connection()
    object.__setattr__(conn, "executor", "lambda")

    with pytest.raises(ValueError, match="unknown environment 'lambda'") as raised:
        _build(conn)

    for name in plugins.names_of("environments"):
        assert name in str(raised.value)


# ---------------------------------------------------------------------------
# What the job is built from
# ---------------------------------------------------------------------------


def test_a_jobs_prompt_comes_from_its_source(tmp_path: Path):
    """The prompt is the source's to render — it knows the work item, the
    connection's own settings and what this run is permitted to report. The
    environment renders nothing."""
    conn = connection(folder=str(tmp_path))
    harness = FakeHarness()
    source = FakeSource()

    job = _job(conn, source=source)
    _build(conn, harness=harness, source=source).run(job, reporter=RecordingReporter())

    assert harness.calls, "the harness was never launched"
    assert harness.calls[0].prompt == source.prompt(work(), conn, permits=job.permits)


def test_a_jobs_mcp_servers_come_from_its_source(tmp_path: Path):
    """`agent_access` is how a source hands the agent its own channel — the run
    must actually wire it into the launch, on top of the board's own.

    A double that wants one, because a source needing nothing beyond the board
    MCP every launch already gets would make "the launch wired it in"
    indistinguishable from "the launch dropped it"."""
    conn = connection(folder=str(tmp_path))
    harness = FakeHarness()
    server = McpServer(name="tracker", type="http", url="https://tracker.example/mcp")
    source = FakeSource(access=(server,), prompt="a very specific prompt")

    job = _job(conn, source=source)
    _build(conn, harness=harness, source=source).run(job, reporter=RecordingReporter())

    assert harness.calls, "the harness was never launched"
    assert server.to_fragment() in harness.calls[0].mcp_servers
    assert harness.calls[0].prompt == source.prompt(work(), conn, permits=job.permits)


# ---------------------------------------------------------------------------
# permits = source.permits ∩ workspace.produces
# ---------------------------------------------------------------------------


def test_a_folder_connections_job_may_not_report_changes(tmp_path: Path):
    """`connection()` names no git strategy, so it resolves to the folder
    workspace — which has no git to derive `Changes` from and says so in
    `produces`. The source permits `changes` for an assignment regardless of
    workspace, so only the intersection gets this right."""
    conn = connection(folder=str(tmp_path))
    source = FakeSource()

    permits = _job(conn, source=source).permits

    assert "changes" not in permits
    assert "changes" in source.permits(work())  # the source alone would have allowed it
    assert "answer" in permits  # and nothing else was lost


def test_no_job_withholds_a_tool_and_that_is_deliberate(tmp_path: Path):
    """The natural companion to `permits` — "a run that may not report `changes`
    should not hold the tools that make them" — is not implemented, and this
    pins that it is a decision rather than an oversight.

    `job_for`'s `ponytail:` note says why: the only vocabulary for naming a tool
    today is one agent CLI's own tool names, and spelling those in the runner is
    the leak ADR-0002 forbids. A harness-neutral capability name has to exist
    first. So whoever fills this in has to delete an explanation to do it,
    rather than quietly changing what a run is allowed to hold."""
    conn = connection(folder=str(tmp_path))

    assert _job(conn, source=FakeSource()).withheld_tools == ()


def test_a_git_connections_job_may_report_changes(tmp_path: Path):
    """The intersection must not be a blanket ban — a workspace that can derive
    `Changes` still gets to."""
    conn = connection(folder=str(tmp_path), git_init="branch")
    source = FakeSource()

    assert "changes" in _job(conn, source=source).permits


def test_a_folder_connections_launch_never_offers_changes(tmp_path: Path):
    """The prompt is where a permit becomes visible to the agent: telling it it
    may report `changes` its workspace cannot produce is an instruction to fail."""
    conn = connection(folder=str(tmp_path))
    harness = FakeHarness()
    source = FakeSource()

    _build(conn, harness=harness, source=source).run(
        _job(conn, source=source), reporter=RecordingReporter()
    )

    assert harness.calls, "the harness was never launched"
    assert '"kind": "changes"' not in harness.calls[0].prompt


# ---------------------------------------------------------------------------
# Delivering from a connection with no local checkout
# ---------------------------------------------------------------------------


def test_a_clone_connection_hands_a_sink_its_repository_not_a_checkout():
    """The cwd hole, on core's side of it. A clone-based or sandboxed
    connection's `Changes` are as real as a local one's — the environment
    really did push — but no checkout exists on this machine for a sink's own
    tools to run in. So `deliver_all` tells every sink the connection's
    repository and an empty folder, rather than leaving a sink to infer either
    from a cwd: one that did failed such a run for a purely local reason,
    blamed the branch, and silently dropped the decision that came with it.

    What a particular sink then does with the repository is its own test."""
    from issuebot import run as run_pipeline
    from issuebot.config import SinkRef
    from issuebot.contracts import Changed, Changes
    from issuebot.plugins.sinks.fake.sink import FakeSink

    conn = sandbox_connection(repo="https://example.com/o/r.git")
    assert conn.folder is None  # the workspace lives only in the sandbox/clone

    changes = Changes(
        branch="issuebot/ISS-1", base_sha="a", head_sha="b", stat="1 file", files_changed=1
    )
    response = Response(status="done", changes=changes, outputs=[Changed(summary="did stuff")])
    sink = FakeSink()
    sinks = [(SinkRef(name="fake", required=True), sink)]

    results = run_pipeline.deliver_all(work(), response, conn, sinks=sinks)

    assert not run_pipeline.required_failed(results, sinks), (
        f"a genuinely successful run was reported as a required-sink failure: {results}"
    )
    assert [(d.repo, d.folder) for d in sink.deliveries] == [("https://example.com/o/r.git", "")]


def test_a_connection_that_cuts_no_branch_may_not_report_changes():
    """The narrowing is per connection, not per workspace class: git *can*
    derive changes, and this connection has no task branch to derive them from,
    so its runs are never told they may report any."""
    conn = connection(folder=None, repo="https://example.com/o/r.git")

    assert "changes" not in _job(conn, source=FakeSource()).permits


def test_a_cloning_connection_that_cuts_a_branch_may_report_changes():
    conn = connection(folder=None, repo="https://example.com/o/r.git", git_init="branch")

    assert "changes" in _job(conn, source=FakeSource()).permits
