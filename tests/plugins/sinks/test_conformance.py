"""One suite every sink plugin runs against, plus the ordering and failure
rules `run.deliver_all`/`run.required_failed` enforce for every sink alike.

Mirrors `tests/plugins/workspaces/test_conformance.py` and
`tests/plugins/sources/test_conformance.py`. The "deliverables before
decisions" half of the design's ordering rule — that a required sink's
failure actually stops a decision from being applied — is a `runner.
ProjectListener._finish` behaviour, not a `Sink`/`deliver_all` one (a sink
knows nothing about decisions, which are a source's business); those tests
live in `tests/test_listen.py`, beside the rest of `_finish`'s coverage.
"""

from __future__ import annotations

from typing import ClassVar

import pytest

from conftest import connection, work
from issuebot import plugins, run
from issuebot.config import SinkRef
from issuebot.contracts import Answer, Changed, Delivery, Response, SinkResult
from issuebot.plugins.sinks.base import Sink
from issuebot.plugins.sinks.fake.sink import FakeSink

SINKS = plugins.names_of("sinks")


@pytest.fixture(params=SINKS)
def sink(request: pytest.FixtureRequest) -> Sink:
    """Every installed sink, constructed with no arguments."""
    return plugins.get("sinks", request.param).sink()


def test_every_sink_subclasses_the_abc(sink: Sink) -> None:
    """A sink plugin's implementation must actually be a Sink."""
    assert isinstance(sink, Sink)


def test_every_sink_names_itself(sink: Sink) -> None:
    """A sink's `name` must match the plugin name it is registered under."""
    assert sink.name in SINKS


def test_every_sink_declares_what_it_accepts(sink: Sink) -> None:
    """`accepts` is never empty, and never claims a kind outside the four the
    design defines."""
    assert sink.accepts
    assert sink.accepts <= {"changes", "answer", "needs_input", "handoff"}


def test_a_sink_only_sees_outputs_it_accepts() -> None:
    """`deliver_all` filters by `accepts` before a sink ever sees an output —
    a sink declaring `accepts = {"answer"}` is never handed a `changes`
    deliverable, even though the response carries both."""

    class _AnswersOnlySink(FakeSink):
        name: ClassVar[str] = "answers-only"
        accepts: ClassVar[frozenset[str]] = frozenset({"answer"})

    only_answers = _AnswersOnlySink()
    response = Response(status="done", outputs=[Changed(summary="did stuff"), Answer(text="hi")])

    run.deliver_all(
        work(), response, connection(), sinks=[(SinkRef(name="answers-only"), only_answers)]
    )

    assert [d.output.kind for d in only_answers.deliveries] == ["answer"]


def test_every_deliverable_reaches_every_accepting_sink() -> None:
    """Any number of sinks may accept the same deliverable kind; each gets a
    turn, in the connection's own order."""
    first, second = FakeSink(), FakeSink()
    response = Response(status="done", outputs=[Changed(summary="did stuff")])

    results = run.deliver_all(
        work(),
        response,
        connection(),
        sinks=[(SinkRef(name="first"), first), (SinkRef(name="second"), second)],
    )

    assert len(first.deliveries) == 1
    assert len(second.deliveries) == 1
    assert len(results) == 2


def test_a_later_sink_still_runs_after_an_earlier_ones_failure() -> None:
    """A best-effort (or required) sink's own failure must not silently skip
    a sink listed after it — every sink gets a turn regardless."""
    failing, healthy = FakeSink(ok=False), FakeSink(ok=True)
    response = Response(status="done", outputs=[Changed(summary="did stuff")])

    run.deliver_all(
        work(),
        response,
        connection(),
        sinks=[(SinkRef(name="failing"), failing), (SinkRef(name="healthy"), healthy)],
    )

    assert len(healthy.deliveries) == 1


class _CrashingSink(FakeSink):
    """A sink whose `deliver` raises instead of returning — rule 2 must hold
    even for this, not just an ordinary `ok=False`."""

    name: ClassVar[str] = "crashing"
    accepts: ClassVar[frozenset[str]] = frozenset({"changes"})

    def deliver(self, delivery: Delivery) -> SinkResult:
        raise RuntimeError("boom")


def test_a_crashing_sink_is_reported_not_silently_dropped() -> None:
    """A required sink that raises must still produce a failed `SinkResult` —
    otherwise `required_failed` never sees it, the caller applies the run's
    decisions unguarded, and a crash reads as success. `deliver_all` itself
    is what has to catch this: a sink cannot be trusted to."""
    crashing = _CrashingSink()
    ref = SinkRef(name="crashing", required=True)
    response = Response(status="done", outputs=[Changed(summary="did stuff")])

    results = run.deliver_all(work(), response, connection(), sinks=[(ref, crashing)])

    assert len(results) == 1
    assert not results[0].ok
    assert run.required_failed(results, [(ref, crashing)])


def test_a_crashing_best_effort_sink_is_reported_and_does_not_fail_the_run() -> None:
    """The other half of rule 2, and the one that would have gone unnoticed:
    catching a crash must not silently promote it to a *required* failure.
    A best-effort sink that raises is reported like any other failed delivery
    and the run's decisions still go ahead."""
    crashing = _CrashingSink()
    ref = SinkRef(name="crashing", required=False)
    response = Response(status="done", outputs=[Changed(summary="did stuff")])

    results = run.deliver_all(work(), response, connection(), sinks=[(ref, crashing)])

    assert [(r.sink, r.ok) for r in results] == [("crashing", False)]
    assert not run.required_failed(results, [(ref, crashing)])


def test_a_sink_that_crashes_does_not_cost_the_next_sink_its_turn() -> None:
    """ "Every sink gets a turn regardless of an earlier one's failure" has to
    survive a crash, not just an `ok=False`. If an exception escaped the loop
    the required sink below would never be asked, `required_failed` would see
    nothing to object to, and the run would apply its decisions on the strength
    of a delivery that never happened."""
    crashing = (SinkRef(name="crashing", required=False), _CrashingSink())
    later = (SinkRef(name="fake", required=True), FakeSink())
    response = Response(status="done", outputs=[Changed(summary="did stuff")])

    results = run.deliver_all(work(), response, connection(), sinks=[crashing, later])

    assert [(r.sink, r.ok) for r in results] == [("crashing", False), ("fake", True)]
    assert len(later[1].deliveries) == 1


def test_required_failed_is_true_only_for_a_failing_required_sink() -> None:
    """The design's rule 2: a required sink's own failure is what matters, not
    a best-effort one's."""
    required = (SinkRef(name="required", required=True), FakeSink())
    best_effort = (SinkRef(name="best-effort", required=False), FakeSink())

    only_best_effort_failed = [
        SinkResult(sink="required", ok=True, summary="ok"),
        SinkResult(sink="best-effort", ok=False, summary="down"),
    ]
    assert not run.required_failed(only_best_effort_failed, [required, best_effort])

    the_required_one_failed = [
        SinkResult(sink="required", ok=False, summary="down"),
        SinkResult(sink="best-effort", ok=False, summary="down"),
    ]
    assert run.required_failed(the_required_one_failed, [required, best_effort])
