"""One connection's live state: what it is doing, what it has said, what it produced.

Two readers, one writer. Server telemetry reports a snapshot every tick, and the
local status file mirrors the same snapshot for ``issuebot status``. One holder
behind one lock, one published shape, so the two views cannot drift apart.
"""

from __future__ import annotations

import logging
import threading
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime

_TAIL_LINES = 200


@dataclass(frozen=True)
class ConnectionSnapshot:
    """One connection's published state — the single shape every publisher gets.

    Produced only by :meth:`ConnectionState.snapshot`. The Supervisor's publish
    loop hands one batch of these to every publisher each tick: the status file
    keeps the identity half (name, board, target, phase, ref), and the source
    client translates the live half to its server's wire format. One shape, so
    the offline view and the dashboard view cannot disagree.
    """

    # Identity — stamped by the listener, which knows its connection.
    name: str = ""
    board: str = ""
    target: str = ""

    # Live state — read from the ConnectionState under its lock.
    phase: str = "idle"
    ref: str | None = None
    log_tail: str = ""
    links: list[dict] = field(default_factory=list)


class ConnectionState:
    """Thread-safe live state for one connection."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._phase = "idle"
        self._ref: str | None = None
        self._log_tail: deque[str] = deque(maxlen=_TAIL_LINES)
        self._links: list[dict] = []

    # -- writing ------------------------------------------------------------

    def set_phase(self, phase: str, ref: str | None = None) -> None:
        """Set the coarse activity phase (idle|waiting|working|blocked|error),
        and the ref being worked when there is one.

        One call updates everything both readers see, so they cannot disagree.
        """
        with self._lock:
            self._phase = phase
            self._ref = ref

    def append_log(self, text: str, kind: str = "runner") -> None:
        """Append one typed entry to the rolling tail (last 200).

        Stored as a tab-delimited ``<iso-utc>\\t<kind>\\t<text>`` line so the
        dashboard can render a timestamp and colour-code the kind. ``kind`` is
        one of tool|text|result|system|runner|error|raw. Tabs and newlines in
        the text are flattened so each entry stays one parseable line.
        """
        ts = datetime.now(UTC).isoformat()
        clean = text.replace("\t", " ").replace("\n", " ").rstrip()
        with self._lock:
            self._log_tail.append(f"{ts}\t{kind}\t{clean}")

    def set_links(self, links: list[dict]) -> None:
        """Replace the current work's branch/PR link snapshot."""
        with self._lock:
            self._links = list(links)

    def clear_links(self) -> None:
        """Clear links (nothing in flight)."""
        with self._lock:
            self._links = []

    # -- reading ------------------------------------------------------------

    def snapshot(self, *, name: str = "", board: str = "", target: str = "") -> ConnectionSnapshot:
        """A consistent :class:`ConnectionSnapshot` of the live state.

        The identity fields are the caller's to stamp — the listener knows its
        connection. They default empty for callers that only observe the live
        half (a run watching its own phase, a test).
        """
        with self._lock:
            return ConnectionSnapshot(
                name=name,
                board=board,
                target=target,
                phase=self._phase,
                ref=self._ref,
                log_tail="\n".join(self._log_tail),
                links=list(self._links),
            )


# The name this held while it was only the telemetry half.
AgentState = ConnectionState


class LogTailHandler(logging.Handler):
    """Logging handler that mirrors formatted records into a state's log tail."""

    def __init__(self, state: ConnectionState) -> None:
        super().__init__()
        self._state = state

    def emit(self, record: logging.LogRecord) -> None:
        """Append the formatted record to the tail (never raises)."""
        try:
            kind = "error" if record.levelno >= logging.WARNING else "runner"
            self._state.append_log(self.format(record), kind=kind)
        except Exception:  # noqa: BLE001 — logging must never crash the caller
            self.handleError(record)
