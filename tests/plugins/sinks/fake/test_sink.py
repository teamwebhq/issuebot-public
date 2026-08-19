"""Fake-sink-specific behaviour beyond the shared conformance suite: the
recording and the one scripting knob (`ok`) core tests build on."""

from __future__ import annotations

from conftest import work
from issuebot.contracts import Changed, Delivery
from issuebot.plugins.sinks.fake.sink import FakeSink


def _delivery() -> Delivery:
    """One deliverable, with the fields a sink is always handed."""
    return Delivery(
        work=work(), output=Changed(summary="did stuff"), changes=None, folder="/work/p"
    )


def test_it_records_what_it_was_handed() -> None:
    sink = FakeSink()
    delivery = _delivery()

    sink.deliver(delivery)

    assert sink.deliveries == [delivery]


def test_it_reports_success_by_default() -> None:
    result = FakeSink().deliver(_delivery())

    assert result.ok
    assert result.sink == "fake"


def test_it_can_be_scripted_to_fail() -> None:
    """Rule 2 ("a required sink's failure cancels the decisions") needs a sink
    that fails on demand — this is the one core tests use."""
    result = FakeSink(ok=False).deliver(_delivery())

    assert not result.ok
