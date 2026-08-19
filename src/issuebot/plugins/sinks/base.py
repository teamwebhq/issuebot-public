"""The Sink ABC: where a run's deliverable outputs are sent once they exist.

An ABC rather than a Protocol, matching :class:`~issuebot.plugins.sources.base.
Source` and :class:`~issuebot.plugins.workspaces.base.Workspace`: every sink
must actually subclass this (checked by the conformance suite), not merely
happen to match its shape.

A sink is controller-side: it runs wherever the
runner itself runs, never inside a sandbox, so a sink credential (a PAT, a
webhook secret) never has to reach one. ``deliver`` is handed only what it
needs to act and to verify substantively before acting — the controller's own
checks (:mod:`issuebot.verify`) are structural and forge-agnostic; only a sink
can ask its own forge "does this really carry work".
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, ClassVar

if TYPE_CHECKING:
    from issuebot.contracts import Delivery, OutputKind, SinkResult


class Sink(ABC):
    """Somewhere a run's deliverable outputs (``changes``, ``answer``) can go.

    ``accepts`` is a sink's own declaration of which deliverable kinds it can
    do anything with — a sink is only ever handed an output whose kind is in
    its own ``accepts`` (see :func:`issuebot.run.deliver_all`'s "a sink only
    sees outputs it accepts").
    """

    # Set by each subclass; also the name it is registered under in the plugin
    # registry (`plugins.get("sinks", sink.name)`).
    name: ClassVar[str]

    # Which deliverable output kinds this sink can act on.
    accepts: ClassVar[frozenset[OutputKind]]

    # True when this sink can only act on work that has reached the remote —
    # it publishes *from* a branch rather than from the local tree, so a
    # workspace that never pushes leaves it nothing to publish.
    #
    # Declared here rather than asked of a named sink somewhere else: the git
    # workspace has to reject `push = false` on a connection wired to a sink
    # like that, and it reads this to find out which of the connection's sinks
    # qualify. Asking after one sink by name instead made a workspace plugin
    # undeletable together with a sink plugin — and would have silently missed
    # the second sink that publishes the same way.
    #
    # Defaults False, which is the honest answer for a sink that posts a
    # comment, calls a webhook or records nothing.
    needs_pushed_branch: ClassVar[bool] = False

    @abstractmethod
    def deliver(self, delivery: Delivery) -> SinkResult:
        """Act on one deliverable and report what happened.

        Prefer reporting an ordinary failure (a rejected PR, an unreachable
        API) as ``SinkResult(ok=False, ...)`` directly — it costs nothing and
        lets a caller reading only ``deliver``'s return value see what
        happened. Raising is not a trap, though: :func:`issuebot.run.
        deliver_all` catches anything a sink lets escape and turns it into the
        same ``SinkResult(ok=False, ...)`` shape itself, because rule 2 ("a
        required sink failing cancels the decisions") has to hold even for a
        sink that crashes outright."""
