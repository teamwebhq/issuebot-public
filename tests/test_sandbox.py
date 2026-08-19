"""The sandbox execution environment: boot → report → exec → recover → destroy.

Written against `SandboxProvider`, so these cover every provider. Nothing here
depends on one: no provider adapter, no platform setting, no vendor syntax in an
assertion. A test that needs any of those is the leak ADR-0002 exists to catch.
Each provider's own adapter is tested inside that provider's plugin, under
tests/plugins/environments/<name>/.

Read-only work is expressed the way the controller expresses it — a job whose
`permits` exclude `changes` — not by naming a kind of work: which kinds are
read-only is a source's judgement, and the sandbox holds no source to ask.
"""

from __future__ import annotations

import pytest

from conftest import (
    VERSION,
    FakeApi,
    FakeProvider,
    FakeSource,
    FakeWorkspace,
    NoSettings,
    RecordingReporter,
    ctx,
    mention,
    sandbox_connection,
    source_table,
    work,
)
from issuebot import release, sandbox_protocol
from issuebot.config import source_plugin
from issuebot.contracts import Changed, Job, NeedsInput, Response, WorkItem
from issuebot.plugins.harnesses.fake.harness import FakeHarness
from issuebot.runner import Wiring
from issuebot.sandbox import SandboxEnvironment
from issuebot.sandbox_protocol import (
    BootMode,
    WorkerEnv,
)

ALL_PERMITS = frozenset({"changes", "answer", "needs_input", "handoff"})
READ_ONLY_PERMITS = frozenset({"answer", "needs_input", "handoff"})


def _wiring(api: FakeApi | None = None, context=None, **conn_kw) -> Wiring:
    """A wiring of doubles for the controller to read its slice of.

    Built directly rather than through `wire`: what is under test is the
    controller, and the harness/workspace/source halves are decided on the far
    side of the wire anyway — the controller never reads them."""
    return Wiring(
        api=api or FakeApi(),
        ctx=context or ctx(),
        connection=sandbox_connection(**conn_kw),
        harness=FakeHarness(),
        source=FakeSource(),
        workspace=FakeWorkspace(),
        workspace_settings=NoSettings(),
        sinks=[],
    )


def _executor(provider: FakeProvider, api: FakeApi | None = None, **conn_kw) -> SandboxEnvironment:
    return SandboxEnvironment(_wiring(api, **conn_kw), provider)


def _job(item: WorkItem, *, permits: frozenset[str] | None = None) -> Job:
    """The job the controller would have built for this item.

    A mention defaults to read-only permits because that is what every source
    in the tree says about one; a test that cares says so explicitly instead.
    """
    if permits is None:
        permits = READ_ONLY_PERMITS if item.kind == "mention" else ALL_PERMITS
    return Job(
        work=item,
        prompt="do the thing",
        folder=None,
        permits=permits,  # ty: ignore[invalid-argument-type]
        withheld_tools=(),
        timeout_minutes=None,
        mcp_servers=(),
        env={},
        resume_session_id=None,
        run_id="R1",
    )


def _sent(provider: FakeProvider) -> WorkerEnv:
    """What the controller told the worker, read back off the wire.

    Decoding rather than reading raw keys: a test that asserts on variable names
    is testing the encoding twice and the contract not at all."""
    return WorkerEnv.decode(provider.created["env"])


def test_runs_the_worker_and_returns_its_outcome():
    provider = FakeProvider(
        result={
            "status": "done",
            "result_text": "did stuff",
            "session_id": "s",
        }
    )
    api = FakeApi()

    outcome = _executor(provider, api).run(_job(work()), reporter=RecordingReporter())

    assert outcome == Response(status="done", result_text="did stuff", session_id="s")
    assert provider.exec_argv is not None
    assert "run-one" in provider.exec_argv
    assert "R1" in provider.exec_argv and "t1" in provider.exec_argv


def test_what_the_run_produced_survives_the_wire():
    """A whole Response comes back, not just a status: a sandboxed run derives
    `Changes` and reads the agent's outputs exactly like a local one, and a
    hand-off or a PR that evaporated at the wire would be released as a bare
    success."""
    provider = FakeProvider(
        result={
            "status": "done",
            "changes": {
                "branch": "issuebot/ISS-1",
                "base_sha": "a",
                "head_sha": "b",
                "stat": "1 file",
                "files_changed": 1,
                "pushed": True,
            },
            "outputs": [Changed(summary="did stuff").model_dump()],
        }
    )

    outcome = _executor(provider).run(_job(work()), reporter=RecordingReporter())

    assert outcome.changes is not None and outcome.changes.head_sha == "b"
    assert [o.kind for o in outcome.outputs] == ["changes"]


def test_outputs_we_cannot_read_fail_the_run_rather_than_vanishing():
    """ "The agent reported nothing" and "we lost what it reported" mean opposite
    things; conflating them releases a dropped decision as a success. The
    failure has to say what went wrong somewhere the person watching a run can
    read it — this fires on version skew, and the log is not where they look."""
    provider = FakeProvider(result={"status": "done", "outputs": [{"kind": "nonsense"}]})

    outcome = _executor(provider).run(_job(work()), reporter=RecordingReporter())

    assert outcome.status == "failed"
    assert "nonsense" in outcome.result_text


def test_a_branch_orphaned_by_unreadable_outputs_is_named():
    """The run is failed, so `_finish` returns before any sink sees the
    `Changes` — nothing else would ever mention that a branch is sitting on the
    remote with nobody coming for it."""
    provider = FakeProvider(
        result={
            "status": "done",
            "changes": {
                "branch": "issuebot/ISS-1",
                "base_sha": "a",
                "head_sha": "b",
                "stat": "1 file",
                "files_changed": 1,
                "pushed": True,
            },
            "outputs": [{"kind": "nonsense"}],
        }
    )

    outcome = _executor(provider).run(_job(work()), reporter=RecordingReporter())

    assert outcome.status == "failed"
    assert "issuebot/ISS-1" in outcome.result_text


def test_reports_the_sandbox_lifecycle_to_the_board():
    """The controller reports where the run executes in neutral vocabulary —
    `plugins.sources.base.SandboxLifecycle` — never one board's own
    execution-metadata column names."""
    api = FakeApi()
    _executor(FakeProvider(), api).run(_job(work()), reporter=RecordingReporter())

    assert [r["event"] for r in api.sandbox_reports] == ["started", "destroyed"]
    assert api.sandbox_reports[0]["environment"] == "sandbox"
    assert api.sandbox_reports[0]["sandbox_id"] == "sbx_1"
    assert all(r["run_id"] == "R1" for r in api.sandbox_reports)


def test_a_client_without_sandbox_reporting_is_skipped_not_crashed():
    """Recording the sandbox lifecycle is an optional capability: a source
    client that lacks it (a bare double, a board with no execution metadata)
    is silently skipped and the run still completes."""

    class Bare:
        """No sandbox reporting at all."""

    outcome = _executor(FakeProvider(), api=Bare()).run(  # ty: ignore[invalid-argument-type]
        _job(work()), reporter=RecordingReporter()
    )

    assert outcome.status == "done"


def test_destroys_the_sandbox_even_when_the_run_crashes():
    provider = FakeProvider(raises={"exec_stream": RuntimeError("network gone")})

    outcome = _executor(provider).run(_job(work()), reporter=RecordingReporter())

    assert outcome.status == "failed"
    assert "network gone" in outcome.result_text
    assert provider.destroyed == "sbx_1"


def test_nothing_to_destroy_when_the_sandbox_never_booted():
    provider = FakeProvider(raises={"create": RuntimeError("token expired")})

    outcome = _executor(provider).run(_job(work()), reporter=RecordingReporter())

    assert outcome.status == "failed"
    assert provider.destroyed is None


def test_a_failed_destroy_does_not_discard_the_outcome():
    provider = FakeProvider(raises={"destroy": RuntimeError("boom")})

    outcome = _executor(provider).run(_job(work()), reporter=RecordingReporter())

    assert outcome.status == "done"


def test_falls_back_to_the_result_file_when_no_sentinel_arrives():
    provider = FakeProvider(
        emit_sentinel=False, lines=["some ordinary log line"], result_file='{"status": "done"}'
    )

    outcome = _executor(provider).run(_job(work()), reporter=RecordingReporter())

    assert outcome.status == "done"


def test_a_worker_that_reports_nothing_at_all_is_a_failure():
    provider = FakeProvider(emit_sentinel=False, exit_code=1)

    outcome = _executor(provider).run(_job(work()), reporter=RecordingReporter())

    assert outcome.status == "failed"
    assert outcome.result_text == "no result from sandbox worker"


def test_an_unknown_status_from_the_worker_is_not_trusted():
    """The status crosses a process boundary as JSON, so it is narrowed before
    it reaches a Literal-typed field."""
    provider = FakeProvider(result={"status": "banana"}, exit_code=0)

    outcome = _executor(provider).run(_job(work()), reporter=RecordingReporter())

    assert outcome.status == "failed"


# --- the reporter lifecycle ------------------------------------------------


def test_the_run_is_narrated_and_logged_like_a_local_one():
    """ConsoleReporter only writes once start() has opened the per-run log, so a
    sandbox run that skipped it left no transcript and an empty dashboard tail."""
    rep = RecordingReporter()
    provider = FakeProvider(lines=["working on it"])

    _executor(provider).run(_job(work()), reporter=rep)

    assert rep.started is not None and rep.started[0] == "ISS-1"
    assert rep.finished is not None and rep.finished[0] == "done"
    assert "working on it" in rep.raw_lines
    assert rep.summaries == ["working on it"]


def test_the_result_sentinel_is_logged_but_kept_out_of_the_feed():
    rep = RecordingReporter()
    _executor(FakeProvider()).run(_job(work()), reporter=rep)

    assert any(line.startswith("##ISSUEBOT-RESULT##") for line in rep.raw_lines)
    assert rep.summaries == []


def test_a_failed_run_finishes_the_reporter_as_failed():
    rep = RecordingReporter()
    _executor(FakeProvider(result={"status": "failed", "result_text": "nope"})).run(
        _job(work()), reporter=rep
    )

    assert rep.finished is not None and rep.finished[0] == "failed"


def test_a_transport_crash_still_closes_the_reporter():
    rep = RecordingReporter()
    _executor(FakeProvider(raises={"exec_stream": RuntimeError("gone")})).run(
        _job(work()), reporter=rep
    )

    assert rep.finished is not None and rep.finished[0] == "failed"


# --- the boot ladder -------------------------------------------------------


def test_cold_boot_uses_the_shared_template():
    provider = FakeProvider(checkpoints=[])
    _executor(provider).run(_job(work()), reporter=RecordingReporter())

    assert provider.created is not None
    assert provider.created["checkpoint"] is None


def test_warm_boot_uses_the_project_checkpoint():
    provider = FakeProvider(checkpoints=["project-p"])
    _executor(provider).run(_job(work()), reporter=RecordingReporter())

    assert provider.created["checkpoint"] == "project-p"
    assert _sent(provider).boot is BootMode.WARM


def test_a_task_checkpoint_resumes_that_task(monkeypatch):
    _no_bookkeeping(monkeypatch)
    provider = FakeProvider(checkpoints=["task-t1", "project-p"])
    _executor(provider).run(_job(work()), reporter=RecordingReporter())

    assert provider.created["checkpoint"] == "task-t1"
    assert _sent(provider).boot is BootMode.RESUME


def test_a_provider_without_checkpoints_always_boots_cold():
    """An environment that cannot snapshot skips the ladder rather than needing
    its own controller."""
    provider = FakeProvider(supports_checkpoints=False, checkpoints=["project-p", "task-t1"])
    _executor(provider).run(_job(work()), reporter=RecordingReporter())

    assert provider.created["checkpoint"] is None
    assert provider.checkpoint_creates == []
    assert provider.checkpoint_deletes == []


def test_read_only_work_never_resumes_a_task_checkpoint(monkeypatch):
    """Only a run that could have left a half-finished workspace behind has a
    checkpoint of its own worth resuming into."""
    _no_bookkeeping(monkeypatch)
    provider = FakeProvider(checkpoints=["task-t1", "project-p"])
    _executor(provider).run(_job(mention()), reporter=RecordingReporter())

    assert provider.created["checkpoint"] == "project-p"


# --- the checkpoint decision ----------------------------------------------


def _no_bookkeeping(monkeypatch) -> list[str]:
    """Silence the on-disk task-checkpoint bookkeeping, recording the calls."""
    from issuebot import task_checkpoints

    recorded: list[str] = []
    monkeypatch.setattr(task_checkpoints, "record", recorded.append)
    monkeypatch.setattr(task_checkpoints, "forget", lambda t: None)
    return recorded


def test_a_cold_run_populates_the_project_checkpoint(monkeypatch):
    _no_bookkeeping(monkeypatch)
    provider = FakeProvider(checkpoints=[])
    _executor(provider).run(_job(work()), reporter=RecordingReporter())

    assert provider.checkpoint_creates == [("sbx_1", "project-p")]


def test_a_warm_run_does_not_re_snapshot(monkeypatch):
    _no_bookkeeping(monkeypatch)
    provider = FakeProvider(checkpoints=["project-p"])
    _executor(provider).run(_job(work()), reporter=RecordingReporter())

    assert provider.checkpoint_creates == []


def test_read_only_work_leaves_nothing_worth_caching(monkeypatch):
    _no_bookkeeping(monkeypatch)
    provider = FakeProvider(checkpoints=[])
    _executor(provider).run(_job(mention()), reporter=RecordingReporter())

    assert provider.checkpoint_creates == []


def test_a_finished_run_clears_its_task_checkpoint(monkeypatch):
    forgotten: list[str] = []
    from issuebot import task_checkpoints

    monkeypatch.setattr(task_checkpoints, "record", lambda t: None)
    monkeypatch.setattr(task_checkpoints, "forget", forgotten.append)
    provider = FakeProvider(checkpoints=["task-t1"])

    _executor(provider).run(_job(work()), reporter=RecordingReporter())

    assert provider.checkpoint_deletes == ["task-t1"]
    assert forgotten == ["t1"]


def test_work_waiting_on_a_human_keeps_its_own_checkpoint(monkeypatch):
    """The other end of the boot ladder's top rung. Nothing populated a
    `task-<id>` checkpoint once `RunStatus.paused` went away; the trigger is now
    the agent's own conclusion — a `needs_input` output — so the next run for
    this task resumes straight back into the sandbox it stopped in."""
    recorded = _no_bookkeeping(monkeypatch)
    provider = FakeProvider(
        result={"status": "done", "outputs": [NeedsInput(question="which one?").model_dump()]},
        checkpoints=[],
    )

    _executor(provider).run(_job(work()), reporter=RecordingReporter())

    assert provider.checkpoint_creates == [("sbx_1", "task-t1")]
    assert recorded == ["t1"]  # so the TTL sweep can find it later


def test_a_kept_checkpoint_is_neither_deleted_nor_shared(monkeypatch):
    """A sandbox held for one task's resume must not also become the warm boot
    every other task in the connection starts from."""
    _no_bookkeeping(monkeypatch)
    provider = FakeProvider(
        result={"status": "done", "outputs": [NeedsInput(question="which one?").model_dump()]},
        checkpoints=["task-t1"],
    )

    _executor(provider).run(_job(work()), reporter=RecordingReporter())

    assert provider.checkpoint_deletes == []
    assert ("sbx_1", "project-p") not in provider.checkpoint_creates


def test_a_failed_snapshot_of_a_paused_run_does_not_fail_the_run(monkeypatch):
    """Losing the resume point costs a cold start next time, nothing more — and
    it is not recorded, so the TTL sweep never chases a checkpoint that isn't
    there."""
    recorded = _no_bookkeeping(monkeypatch)
    provider = FakeProvider(
        result={"status": "done", "outputs": [NeedsInput(question="which one?").model_dump()]},
        raises={"create_checkpoint": RuntimeError("quota")},
    )

    outcome = _executor(provider).run(_job(work()), reporter=RecordingReporter())

    assert outcome.status == "done"
    assert recorded == []


def test_a_resumed_run_is_not_folded_into_the_project_checkpoint(monkeypatch):
    """One task's branch state must not leak into another task's warm boot."""
    _no_bookkeeping(monkeypatch)
    provider = FakeProvider(checkpoints=["task-t1"])

    _executor(provider).run(_job(work()), reporter=RecordingReporter())

    assert ("sbx_1", "project-p") not in provider.checkpoint_creates


@pytest.mark.parametrize("failing", ["create_checkpoint", "delete_checkpoint"])
def test_checkpoint_failures_never_fail_the_run(monkeypatch, failing):
    _no_bookkeeping(monkeypatch)
    provider = FakeProvider(raises={failing: RuntimeError("quota")}, checkpoints=[])

    outcome = _executor(provider).run(_job(work()), reporter=RecordingReporter())

    assert outcome.status == "done"


# --- what crosses the wire -------------------------------------------------


def test_the_sources_own_settings_ride_the_wire():
    """The worker is told where the board is; it does not carry a config file.

    Whose settings, and the settings themselves — the controller names the
    source plugin and hands over that plugin's table verbatim, rather than
    three endpoint fields the wire would have to redefine for a second source.
    Asserted against the registry-keyed table the fixtures build, so nothing
    here spells a source's name."""
    provider = FakeProvider()
    _executor(provider).run(_job(work()), reporter=RecordingReporter())

    sent = _sent(provider)
    assert sent.source == source_plugin().name
    assert sent.source_settings == source_table()[source_plugin().name]


def test_infrastructure_secrets_come_from_the_provider():
    """Each provider names its secrets in whatever form it injects them: one
    platform's variable references, another's real values, a third's nothing at
    all. The controller merges the answer and never reads it."""
    provider = FakeProvider()
    _executor(provider).run(_job(work()), reporter=RecordingReporter())

    assert provider.created["env"]["FAKE_SECRET"] == "s3cret"


def test_the_mention_context_rides_the_wire():
    """run-one rebuilds the work item from the task record, which has no actor
    or comment on it — so they have to travel in the environment or be lost.
    The agent's own id rides along too, from the runner context every
    environment is built with."""
    provider = FakeProvider()
    environment = SandboxEnvironment(_wiring(context=ctx(agent_id="u-1")), provider)
    environment.run(
        _job(mention(actor_name="Ada", comment_excerpt="ping?")), reporter=RecordingReporter()
    )

    sent = _sent(provider)
    assert sent.actor_name == "Ada"
    assert sent.comment_excerpt == "ping?"
    assert sent.agent_id == "u-1"


def test_absent_mention_context_is_omitted_rather_than_blanked():
    provider = FakeProvider()
    _executor(provider).run(
        _job(work(kind="mention", actor_name=None, comment_excerpt=None)),
        reporter=RecordingReporter(),
    )

    assert "ISSUEBOT_ACTOR_NAME" not in provider.created["env"]
    assert "ISSUEBOT_AGENT_ID" not in provider.created["env"]
    assert _sent(provider).actor_name is None


def test_the_worker_is_told_which_kind_of_work_it_has():
    provider = FakeProvider()
    _executor(provider).run(_job(mention()), reporter=RecordingReporter())

    argv = provider.exec_argv
    assert argv[argv.index("--kind") + 1] == "mention"


# --- version skew ----------------------------------------------------------
#
# The sandbox runs issuebot's own code, so a run is only correct when both ends
# are the same released version. Nothing here names a provider: the ladder —
# ask, compare, update, warn — is the controller's, and every provider inherits
# it.

OTHER = "9.8.7"


def test_the_controller_sends_its_own_version():
    """Which build this run must be is the controller's to state, so it rides
    the same wire as everything else it tells the worker."""
    provider = FakeProvider()
    _executor(provider).run(_job(work()), reporter=RecordingReporter())

    assert _sent(provider).version == VERSION


def test_a_matching_template_starts_work_immediately():
    """The common case, and the one a subtly wrong reading would ruin: a
    sandbox already on the right version must cost nothing but the question.
    Every boot reinstalling issuebot would still pass the tests below."""
    provider = FakeProvider(installed_version=VERSION)
    _executor(provider).run(_job(work()), reporter=RecordingReporter())

    assert provider.exec_calls[0] == sandbox_protocol.version_argv()
    assert provider.exec_calls[1][:2] == ["issuebot", "run-one"]
    assert len(provider.exec_calls) == 2


def test_a_stale_template_updates_itself_before_working():
    """A stale template is a performance problem, never a correctness one."""
    provider = FakeProvider(installed_version=OTHER)

    outcome = _executor(provider).run(_job(work()), reporter=RecordingReporter())

    assert outcome.status == "done"
    assert provider.exec_calls[1] == sandbox_protocol.update_argv(VERSION)
    # Before, not alongside: the worker is exec'd only once the update returned.
    assert provider.exec_calls[3][:2] == ["issuebot", "run-one"]


def test_a_sandbox_with_no_issuebot_at_all_installs_one():
    """An unanswerable probe reads as a mismatch, not as a match — a sandbox
    that cannot say what it is certainly cannot be trusted to be the right
    thing."""
    provider = FakeProvider(installed_version="")
    _executor(provider).run(_job(work()), reporter=RecordingReporter())

    assert provider.exec_calls[1] == sandbox_protocol.update_argv(VERSION)


def test_a_stale_template_warns_the_user_to_rebuild():
    """The self-update makes the run correct; only a rebuild makes it fast
    again, and the person watching `issuebot listen` is the one who can do it."""
    rep = RecordingReporter()
    provider = FakeProvider(installed_version=OTHER)

    _executor(provider).run(_job(work()), reporter=rep)

    warning = " ".join(rep.summaries)
    assert OTHER in warning and VERSION in warning
    assert provider.rebuild_command in warning


def test_a_failed_self_update_fails_the_run(monkeypatch):
    """Loudly, rather than working on code we know is the wrong code:
    correctness is the whole reason the update exists."""
    _no_bookkeeping(monkeypatch)
    provider = FakeProvider(installed_version=OTHER, update_exit=1)

    outcome = _executor(provider).run(_job(work()), reporter=RecordingReporter())

    assert outcome.status == "failed"
    assert VERSION in outcome.result_text
    assert provider.exec_argv is None  # no work was attempted
    assert provider.destroyed == "sbx_1"


def test_a_source_controller_never_creates_a_sandbox(monkeypatch):
    monkeypatch.setattr(release, "is_installed_wheel", lambda: False)
    provider = FakeProvider()

    outcome = _executor(provider).run(_job(work()), reporter=RecordingReporter())

    assert outcome.status == "failed"
    assert "released issuebot wheel" in outcome.result_text
    assert provider.created is None


def test_a_successful_update_is_probed_again_before_work():
    provider = FakeProvider(installed_version="9.8.7")

    _executor(provider).run(_job(work()), reporter=RecordingReporter())

    assert provider.exec_calls[:3] == [
        sandbox_protocol.version_argv(),
        sandbox_protocol.update_argv(VERSION),
        sandbox_protocol.version_argv(),
    ]


def test_an_update_that_did_not_change_the_selected_binary_fails(monkeypatch):
    _no_bookkeeping(monkeypatch)
    provider = FakeProvider(installed_version="9.8.7", update_applies=False)

    outcome = _executor(provider).run(_job(work()), reporter=RecordingReporter())

    assert outcome.status == "failed"
    assert "still reports 9.8.7" in outcome.result_text
    assert provider.exec_argv is None


@pytest.mark.parametrize(
    "output",
    [
        ["Connecting...", "  1.2.3  ", "Connection closed."],
        ["1.2.3\r\n"],
        ["1.2.3"],
    ],
)
def test_the_probes_answer_is_found_by_shape_not_by_position(output):
    """The provider merges the sandbox CLI's stderr into the stream, so the
    answer arrives wrapped in that wrapper's chatter. "The last line" would read
    a postamble as the answer and reinstall on every single boot."""
    assert sandbox_protocol.parse_version(output) == "1.2.3"


@pytest.mark.parametrize(
    "output",
    [[], ["Connection closed."], ["not a version"], ["1.2"], ["version 1.2.3"]],
)
def test_a_probe_that_answered_something_else_answered_nothing(output):
    """Which reads as a mismatch, and gets the sandbox reinstalled — the safe
    direction. Guessing at a half-recognised answer is the unsafe one."""
    assert sandbox_protocol.parse_version(output) == ""


def test_a_worker_that_says_nothing_on_a_clean_exit_is_still_a_failure():
    """Both channels the worker reports on are gone, so what the run produced is
    not "nothing" — it is unknown. Reading the exit code as `done` released the
    task as finished with no outputs, no delivery and no decision."""
    provider = FakeProvider(emit_sentinel=False, exit_code=0)

    outcome = _executor(provider).run(_job(work()), reporter=RecordingReporter())

    assert outcome.status == "failed"
    assert outcome.result_text == "no result from sandbox worker"


def test_the_feed_and_the_outcome_agree_about_a_silent_worker():
    """A silent worker fails the run — so the narrated close must not be a ✓.
    The log said done while the board was told failed."""
    provider = FakeProvider(emit_sentinel=False, exit_code=0)
    reporter = RecordingReporter()

    outcome = _executor(provider).run(_job(work()), reporter=reporter)

    assert outcome.status == "failed"
    assert reporter.finished is not None
    assert reporter.finished[0] == "failed"
