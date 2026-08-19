"""Per-task live activity reporter.

A :class:`ConsoleReporter` renders a concise, human-readable feed of agent
activity to a stream (``sys.stderr`` by default) while *also* teeing every raw
stream-json line to a per-run log file under the XDG state directory, so the
full transcript is always recoverable even though the feed is deliberately
terse.

A small background thread emits a "still running?" warning when the agent goes
quiet for a while (the :func:`stall_message` signal), so a stuck run is visible
rather than looking like a hang.
"""

from __future__ import annotations

import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Protocol, TextIO

from issuebot.agent_state import AgentState
from issuebot.events import AgentEvent
from issuebot.state import open_private, state_dir

# How long the agent may be silent before we surface a "still running?" warning.
STALL_AFTER = 60.0

# How often the background stall-tick wakes to check for silence.
_STALL_TICK_S = 15.0

# Feed glyphs per event kind. A wrench for tools, a speech bubble for prose,
# a tick for the terminal result, and a dot for anything raw/unknown. The init
# event is not rendered (it only carries the session id), so it has no glyph.
_ICONS = {
    "tool_use": "\U0001f527",
    "text": "\U0001f4ac",
    "result": "✓",
    "raw": "·",
}

# Map a parsed AgentEvent kind to the log-tail entry kind the dashboard colours.
_ENTRY_KIND = {"tool_use": "tool", "text": "text", "result": "result", "raw": "raw"}


def format_event(ev: AgentEvent) -> str:
    """Render one event as a concise ``<icon> <summary>`` feed line.

    Shared by the live :class:`ConsoleReporter` feed and the ``issuebot logs``
    renderer so both use the same glyphs. Unknown kinds fall back to ``·``.
    """
    icon = _ICONS.get(ev.kind, "·")
    return f"{icon} {ev.summary}"


class Reporter(Protocol):
    """Anything that can narrate a single task run to the user."""

    def start(self, ref: str, folder: str) -> None: ...

    def event(self, ev: AgentEvent) -> None: ...

    def raw(self, line: str) -> None: ...

    def finish(self, status: str, elapsed: float) -> None: ...


class NullReporter:
    """A reporter that says nothing — used when output is suppressed."""

    def start(self, ref: str, folder: str) -> None:
        """Do nothing."""

    def event(self, ev: AgentEvent) -> None:
        """Do nothing."""

    def raw(self, line: str) -> None:
        """Do nothing."""

    def finish(self, status: str, elapsed: float) -> None:
        """Do nothing."""


def default_log_dir() -> Path:
    """Where per-run logs are written — ``<state dir>/logs``.

    A run log holds the agent's full transcript, so it is written 0600 into a
    0700 directory like every other thing issuebot persists (see
    :func:`issuebot.state.open_private`)."""
    return state_dir() / "logs"


def stall_message(ref: str, idle_s: float, elapsed_s: float) -> str | None:
    """Return a "still running?" warning, or ``None`` if not yet warranted.

    The signal stays silent until the agent has produced no output for
    :data:`STALL_AFTER` seconds, then nudges the user that the run is alive but
    quiet (and how to bail out).
    """
    if idle_s < STALL_AFTER:
        return None

    return (
        f"⚠ {ref} — no output for {idle_s:.0f}s "
        f"(elapsed {elapsed_s:.0f}s) — still running; Ctrl-C to abort"
    )


class ConsoleReporter:
    """Render a concise live feed to a stream and tee raw lines to a log."""

    def __init__(
        self,
        *,
        ref: str,
        show_prefix: bool = False,
        log_dir: Path | None = None,
        stream: TextIO | None = None,
        clock: Callable[[], float] = time.monotonic,
        agent_state: AgentState | None = None,
    ) -> None:
        """Configure a reporter for one task run.

        ``stream`` defaults to ``sys.stderr`` so the feed never pollutes any
        machine-readable stdout. ``clock`` is injectable so tests can drive
        timing deterministically.
        """
        self._ref = ref
        self._prefix = f"[{ref}] " if show_prefix else ""
        self._log_dir = log_dir if log_dir is not None else default_log_dir()
        self._stream = stream if stream is not None else sys.stderr
        self._clock = clock
        # When set, every feed line is mirrored into the shared AgentState log
        # tail so the dashboard shows the agent's live activity, not just the
        # sparse issuebot logger records.
        self._agent_state = agent_state

        self._log: TextIO | None = None
        self._log_path: Path | None = None
        self._t0 = 0.0
        self._last_activity = 0.0

        # Background stall-tick machinery. The thread is optional and entirely
        # gated by this flag so tests can disable it for determinism.
        self._stall_enabled = True
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # -- console plumbing ------------------------------------------------

    def _write(self, text: str) -> None:
        """Emit one prefixed line to the stream and flush immediately."""
        self._stream.write(f"{self._prefix}{text}\n")
        self._stream.flush()

    def _append_entry(self, text: str, kind: str) -> None:
        """Mirror a typed entry into the shared AgentState tail (if attached)."""
        if self._agent_state is not None:
            self._agent_state.append_log(text, kind=kind)

    # -- Reporter protocol ----------------------------------------------

    def start(self, ref: str, folder: str) -> None:
        """Open the run: timestamp, log file, and the opening feed line."""
        self._t0 = self._clock()
        self._last_activity = self._t0

        try:
            name = f"{ref}-{time.strftime('%Y%m%d-%H%M%S')}.jsonl"
            self._log_path = self._log_dir / name
            self._log = open_private(self._log_path)
        except OSError as exc:
            # A missing/un-writable log dir must never sink the run; warn and
            # carry on feeding the console only.
            self._log = None
            self._log_path = None
            self._write(f"  (could not open log: {exc})")

        self._write(f"▶ {ref} — working in {folder}")
        self._append_entry(f"working in {folder}", "system")
        if self._log is not None:
            self._write(f"  log: {self._log_path}")

        self._start_stall_thread()

    def event(self, ev: AgentEvent) -> None:
        """Render one parsed event and mark fresh activity."""
        self._last_activity = self._clock()
        self._write(f"  {format_event(ev)}")
        kind = _ENTRY_KIND.get(ev.kind, "text")
        if ev.kind == "result" and ev.is_error:
            kind = "error"
        self._append_entry(ev.summary, kind)

    def raw(self, line: str) -> None:
        """Tee a raw stream-json line to the per-run log, if open."""
        if self._log is not None:
            self._log.write(line.rstrip("\n") + "\n")
            self._log.flush()

    def finish(self, status: str, elapsed: float) -> None:
        """Close the run: stop the stall-tick, summarise, close the log."""
        self._stop_stall_thread()

        mark = "✓" if status == "done" else "✗"
        self._write(f"{mark} {self._ref} {status} in {elapsed:.0f}s")
        self._append_entry(f"{status} in {elapsed:.0f}s", "system" if status == "done" else "error")

        if self._log is not None:
            self._log.close()
            self._log = None

    # -- background stall-tick ------------------------------------------

    def _start_stall_thread(self) -> None:
        """Launch the daemon stall-tick, unless disabled."""
        if not self._stall_enabled:
            return

        self._stop.clear()
        self._thread = threading.Thread(target=self._stall_loop, daemon=True)
        self._thread.start()

    def _stop_stall_thread(self) -> None:
        """Signal the stall-tick to exit and join it briefly."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)
            self._thread = None

    def _stall_loop(self) -> None:
        """Wake periodically and warn if the agent has gone quiet."""
        while not self._stop.wait(_STALL_TICK_S):
            now = self._clock()
            idle = now - self._last_activity
            elapsed = now - self._t0

            msg = stall_message(self._ref, idle, elapsed)
            if msg is not None:
                self._write(msg)
                self._append_entry(msg, "system")
