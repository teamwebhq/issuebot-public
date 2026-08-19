"""A sink that records what it was handed instead of publishing it anywhere."""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from issuebot.contracts import SinkResult
from issuebot.plugins.sinks.base import Sink

if TYPE_CHECKING:
    from issuebot.contracts import Delivery, OutputKind
    from issuebot.plugins.harnesses.base import Harness


class FakeSink(Sink):
    """Records every delivery it is handed and hands back a scripted result.

    Production code rather than a test-local double, for the same reason
    :class:`~issuebot.plugins.harnesses.fake.harness.FakeHarness` is: a core
    test needs *a* sink wired to a connection, not a particular one, and naming
    a real sink is exactly the coupling the plugin boundary
    exists to catch. Registered, so ``sinks = ["fake"]`` validates, resolves and
    delivers like any other sink — which is what makes deleting every real sink
    a thing the suite can still run.

    ``harness`` is accepted and ignored: :func:`issuebot.runner.sinks_for`
    hands every sink the run's summarizer, and this one has no description to
    write. ``ok`` scripts whether the delivery succeeds.
    """

    name: ClassVar[str] = "fake"
    accepts: ClassVar[frozenset[OutputKind]] = frozenset({"changes", "answer"})

    def __init__(self, *, ok: bool = True, harness: Harness | None = None) -> None:
        self._ok = ok
        self.deliveries: list[Delivery] = []

    def deliver(self, delivery: Delivery) -> SinkResult:
        """Record the delivery and report the scripted outcome."""
        self.deliveries.append(delivery)

        summary = "delivered" if self._ok else "could not deliver"
        return SinkResult(sink=self.name, ok=self._ok, summary=summary)
