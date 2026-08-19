"""The neutral shape a run's output is shown in: one :class:`AgentEvent` per
interesting thing the agent did.

Deliberately *only* the shape. Reading a harness's wire format into these is
that harness's own job (:meth:`issuebot.plugins.harnesses.base.Harness.parse_line`)
— a parser here would make a core module the expert on one vendor's JSON. What
lives here is what every consumer of a run genuinely shares: the reporter
renders these, the live dashboard colours them, ``issuebot logs`` replays
them, and the sandbox controller wraps a subprocess line in one.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

EventKind = Literal["init", "tool_use", "text", "result", "raw"]

_MAX = 100  # summary truncation


@dataclass(frozen=True)
class AgentEvent:
    kind: EventKind
    summary: str
    detail: str = ""
    is_error: bool = False
    session_id: str | None = None


def truncate(text: str) -> str:
    """One line, at most ``_MAX`` characters, for the feed's summary column.

    Shared rather than private because a harness building its own events wants
    its summaries to sit at the same width as everyone else's — the feed is one
    column whoever wrote the line.
    """
    text = text.strip().splitlines()[0] if text.strip() else ""
    return text if len(text) <= _MAX else text[: _MAX - 1] + "…"


def raw_event(line: str) -> AgentEvent | None:
    """A line no harness claims to understand, as a ``raw`` event (None if blank).

    The reading a harness with no structured output gets for free, and the
    fallback wherever a line has to be shown without knowing who wrote it.
    """
    return AgentEvent("raw", truncate(line), detail=line) if line.strip() else None
