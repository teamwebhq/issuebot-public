"""The response file: where the agent reports structured outputs, and how
``run.execute`` reads it back into ``Response.outputs``.

A missing document and an empty ``{"outputs": []}`` document must never be
conflated — the first means the agent never finished, the second is a run
that deliberately had nothing to report. Both are exercised here, alongside
the read-back happy path and the malformed-document failure.
"""

from __future__ import annotations

from pydantic import BaseModel

from conftest import FakeApi, FakeWorkspace, RecordingReporter, connection, wiring, work
from issuebot.contracts import Answer, Changed, Handoff, Job
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
        source=overrides.pop("source", None) or FakeApi(),
    )
    kwargs: dict = dict(reporter=RecordingReporter())
    kwargs.update(overrides)
    return execute(job or _job(), w, **kwargs)


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
    response = _run(harness=FakeHarness(outputs=[]))
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
