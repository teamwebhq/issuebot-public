"""The response file: where the agent reports structured outputs, and how
``run.execute`` reads it back into ``Response.outputs``.

A missing document and an empty ``{"outputs": []}`` document must never be
conflated — the first means the agent never finished, the second is a run
that deliberately had nothing to report. Both are exercised here, alongside
the read-back happy path and the malformed-document failure.
"""

from __future__ import annotations

from pydantic import BaseModel

from conftest import FakeSource, FakeWorkspace, RecordingReporter, connection, wiring, work
from issuebot.contracts import Answer, Changed, Changes, Handoff, Job
from issuebot.plugins.harnesses.base import LaunchSpec
from issuebot.plugins.harnesses.fake.harness import FakeHarness
from issuebot.run import RESPONSE_ENV, execute

ALL_PERMITS = frozenset({"changes", "answer", "needs_input", "handoff"})


class _NoSettings(BaseModel):
    """A workspace settings stand-in — `FakeWorkspace` never reads it."""


def _job(**overrides) -> Job:
    """A Job with sensible defaults, overridable per test (mirrors test_run.py's)."""
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


def _run(job=None, *, harness=None, **overrides):
    """Call `execute` over a doubled wiring, with test-friendly defaults for
    everything not under test (mirrors test_run.py's)."""
    w = wiring(
        connection(),
        harness=harness or FakeHarness(),
        workspace=overrides.pop("workspace", None) or FakeWorkspace(),
        workspace_settings=_NoSettings(),
        # A real `Source` double, not `FakeApi` (the board client): `execute`
        # calls `Source`-only methods on this unconditionally (see test_run.py's
        # own `_run`).
        source=overrides.pop("source", None) or FakeSource(),
    )
    kwargs: dict = dict(reporter=RecordingReporter())
    kwargs.update(overrides)
    return execute(job or _job(), w, **kwargs)


def _quiet_workspace() -> FakeWorkspace:
    """A workspace whose commit moved nothing.

    Used where a test is about the response document alone: a workspace that
    committed real work makes `_finish` add a git-derived `changes` output of
    its own (see `run._derived_changes_output`), which would drown out the
    outputs actually under test."""
    return FakeWorkspace(
        changes=Changes(branch="b", base_sha="a", head_sha="a", stat="", files_changed=0)
    )


def _launch_spec_for(job: Job) -> LaunchSpec:
    """The LaunchSpec `execute` built for `job`, captured off a FakeHarness."""
    harness = FakeHarness()
    _run(job, harness=harness)
    return harness.calls[0]


def test_the_agent_is_told_where_to_write_its_response():
    """Outside the workspace, so it can never appear in a commit."""
    job = _job()
    spec = _launch_spec_for(job)
    assert RESPONSE_ENV in spec.env
    assert not spec.env[RESPONSE_ENV].startswith(job.folder)


def test_the_outputs_are_read_back_into_the_response():
    outputs = [Changed(summary="did the thing"), Handoff(assignee="sam", note="over to you")]
    response = _run(harness=FakeHarness(outputs=outputs))
    assert response.status == "done"
    assert [o.kind for o in response.outputs] == ["changes", "handoff"]


def test_a_missing_response_file_fails_the_run():
    """Distinct from a run that deliberately reported nothing — that writes a
    document with an empty list. A missing file means the agent never finished."""
    response = _run(harness=FakeHarness(writes_response=False))
    assert response.status == "failed"
    assert "response" in (response.result_text or "").lower()


def test_a_malformed_response_file_fails_the_run():
    response = _run(harness=FakeHarness(response_raw="not json at all"))
    assert response.status == "failed"
    assert "response" in (response.result_text or "").lower()


def test_an_empty_outputs_list_is_a_successful_run_with_nothing_to_deliver():
    response = _run(harness=FakeHarness(outputs=[]), workspace=_quiet_workspace())
    assert response.status == "done"
    assert response.outputs == []


def test_outputs_are_read_back_even_when_changes_are_not_permitted():
    """A mention-shaped job still gets its answer back — only `changes` itself
    is unreachable (no commit/push happens), not the response mechanism."""
    job = _job(permits=frozenset({"answer"}))
    response = _run(job, harness=FakeHarness(outputs=[Answer(text="here you go")]))
    assert response.status == "done"
    assert response.changes is None
    assert [o.kind for o in response.outputs] == ["answer"]


class _LateHarness(FakeHarness):
    """A resuming harness that writes its response document only on the Nth
    launch, so `execute`'s one-shot retry can be exercised end to end."""

    resumes_sessions = True

    def __init__(self, *, writes_on: int | None, outputs=None) -> None:
        """``writes_on`` is the 1-based launch that writes the document; None
        never writes one."""
        super().__init__(session_id="sess-1", outputs=outputs)
        self._writes_on = writes_on

    def launch(self, spec, reporter, cancel=None):
        """Record the launch as usual, but only write the document on the
        scripted attempt."""
        self._writes_response = len(self.calls) + 1 == self._writes_on
        return super().launch(spec, reporter, cancel)


def test_a_missing_response_document_earns_one_resumed_retry():
    """Seven minutes of real work must not be thrown away over a file the agent
    forgot to write — it gets asked once more, in the same session."""
    harness = _LateHarness(writes_on=2, outputs=[Answer(text="here you go")])
    response = _run(harness=harness, workspace=_quiet_workspace())

    assert response.status == "done"
    assert [o.kind for o in response.outputs] == ["answer"]
    assert len(harness.calls) == 2
    assert harness.calls[1].resume_session_id == "sess-1"


def test_the_retry_is_bounded_to_one():
    """An agent that still writes nothing fails the run rather than being asked
    over and over."""
    harness = _LateHarness(writes_on=None)
    response = _run(harness=harness)

    assert response.status == "failed"
    assert len(harness.calls) == 2
