"""List, render, and follow per-run agent logs from the command line.

Each ``issuebot listen`` run tees its raw ``stream-json`` to
``~/.local/state/issuebot/logs/<ref>-<timestamp>.jsonl`` (see
:mod:`issuebot.reporter`). This module turns those files into the ``issuebot
logs`` command: list recent runs, render one (the concise feed by default, raw
``jsonl`` with ``--raw``), and follow a live run ``tail -f`` style.

The concise render reuses the same glyphs as the live feed, and reads the lines
back with the *same harness* that wrote them, so ``issuebot logs ISS-42`` shows
exactly what watching the run showed. Which harness that is comes in as
``parse`` — a log file holds whatever its harness prints, and this module has no
business knowing any harness's wire format.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

from issuebot.events import AgentEvent, raw_event
from issuebot.reporter import format_event

# Reads one recorded line into a feed event, or None when it carries nothing
# worth showing. `Harness.parse_line` is what the CLI passes; `raw_event` is the
# default for a caller with no harness to hand, which shows every line verbatim
# rather than refusing to render at all.
ParseLine = Callable[[str], "AgentEvent | None"]


@dataclass(frozen=True)
class Run:
    """One recorded run: its task ref, start timestamp, and log file path."""

    ref: str
    started: str  # "YYYYmmdd-HHMMSS", as embedded in the filename
    path: Path


def _parse_name(path: Path) -> Run | None:
    """Parse a ``<ref>-<YYYYmmdd>-<HHMMSS>.jsonl`` filename into a :class:`Run`.

    The ref itself may contain dashes (e.g. ``ISS-42``), so the two trailing
    dash-separated tokens are the date and time and everything before them is the
    ref. Returns ``None`` for names that don't match the run-log shape.
    """
    if path.suffix != ".jsonl":
        return None
    stem = path.stem  # filename without ".jsonl"
    parts = stem.rsplit("-", 2)
    if len(parts) != 3 or not parts[0]:
        return None
    ref, date, clock = parts
    if not (date.isdigit() and clock.isdigit()):
        return None
    return Run(ref=ref, started=f"{date}-{clock}", path=path)


def list_runs(log_dir: Path) -> list[Run]:
    """All recorded runs under ``log_dir``, newest first.

    A missing log directory simply yields no runs. The ``started`` token sorts
    lexically in chronological order, so a reverse sort is newest-first; the path
    is a stable tiebreaker.
    """
    try:
        names = list(log_dir.glob("*.jsonl"))
    except OSError:
        return []
    runs = [run for run in (_parse_name(p) for p in names) if run is not None]
    runs.sort(key=lambda r: (r.started, str(r.path)), reverse=True)
    return runs


def latest_run(log_dir: Path) -> Run | None:
    """The single most recent run under ``log_dir``, or ``None`` if there are none."""
    runs = list_runs(log_dir)
    return runs[0] if runs else None


def latest_run_for_ref(ref: str, log_dir: Path) -> Run | None:
    """The most recent run for ``ref``, or ``None`` if that ref has no runs."""
    for run in list_runs(log_dir):
        if run.ref == ref:
            return run
    return None


def read_lines(path: Path) -> list[str]:
    """Read all lines of a log file (best-effort; missing/unreadable → empty)."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    return text.splitlines()


def render_lines(lines: Iterable[str], *, raw: bool, parse: ParseLine = raw_event) -> list[str]:
    """Render a run's recorded output lines for display.

    With ``raw`` the non-empty lines pass through verbatim. Otherwise ``parse``
    — the harness's own reading of its output — turns each into an
    :class:`~issuebot.events.AgentEvent` rendered as a concise ``<icon>
    <summary>`` feed line; lines that carry nothing feed-worthy (session init,
    partials, blanks) are dropped.
    """
    out: list[str] = []
    for line in lines:
        if raw:
            if line.strip():
                out.append(line.rstrip("\n"))
            continue

        ev = parse(line)
        if ev is None or ev.kind == "init":
            continue
        out.append(format_event(ev))
    return out


def tail(lines: list[str], n: int | None) -> list[str]:
    """The last ``n`` lines (all of them when ``n`` is falsy or non-positive)."""
    if not n or n <= 0:
        return lines
    return lines[-n:]


def _emit(out: TextIO, rendered: Iterable[str]) -> None:
    """Write each rendered line to ``out`` and flush."""
    for line in rendered:
        out.write(line + "\n")
    out.flush()


def drain_new(
    path: Path, seen: int, *, out: TextIO, raw: bool, parse: ParseLine = raw_event
) -> int:
    """Render any lines appended past ``seen`` and return the new total count.

    Pure enough to test directly: given a file and how many lines were already
    shown, it emits only the newcomers and reports the updated watermark.
    """
    lines = read_lines(path)
    if len(lines) > seen:
        _emit(out, render_lines(lines[seen:], raw=raw, parse=parse))
    return len(lines)


def follow_log(
    path: Path,
    *,
    out: TextIO,
    raw: bool = False,
    n: int | None = 10,
    stop: threading.Event | None = None,
    poll: float = 0.5,
    parse: ParseLine = raw_event,
) -> int:
    """Print the last ``n`` lines, then ``tail -f`` the file until ``stop`` is set.

    Returns the final line count seen. ``stop.wait(poll)`` provides the polling
    delay and is interruptible, so a set ``stop`` returns promptly without
    sleeping (and makes the loop deterministic in tests).
    """
    stop = stop or threading.Event()
    existing = read_lines(path)
    _emit(out, render_lines(tail(existing, n), raw=raw, parse=parse))
    seen = len(existing)
    while not stop.wait(poll):
        seen = drain_new(path, seen, out=out, raw=raw, parse=parse)
    return seen


def active_run(
    log_dir: Path,
    status_payload: dict | None,
    *,
    is_fresh: Callable[[dict], bool],
) -> Run | None:
    """Pick the run to follow when no ref is given: the connection a fresh status
    file reports as ``working``, else simply the most recent run on disk."""
    if status_payload is not None and is_fresh(status_payload):
        for entry in status_payload.get("connections", []):
            if isinstance(entry, dict) and entry.get("phase") == "working" and entry.get("ref"):
                run = latest_run_for_ref(str(entry["ref"]), log_dir)
                if run is not None:
                    return run
    return latest_run(log_dir)
