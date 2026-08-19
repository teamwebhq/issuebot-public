"""Local runner status mirror.

The design fork for "what's currently running" (ISS-87) was settled in favour of
a **local, self-contained** status file: a running ``issuebot listen`` mirrors
its live per-connection snapshots — the activity phase and the ref each is
working — to ``~/.local/state/issuebot/status.json`` every few seconds (the
Supervisor's publish loop writes it). A separate ``issuebot status`` invocation
reads that file: a fresh file means a runner is active on this machine; an
absent or stale file means none is.

No server round-trip is involved — the same snapshot batch feeds the dashboard
via telemetry, made readable here from the command line and offline.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from issuebot.config import conn_setting
from issuebot.state import StateFile, state_path

if TYPE_CHECKING:
    from issuebot.agent_state import ConnectionSnapshot

logger = logging.getLogger("issuebot")

# A status file is "stale" once it is older than this multiple of the writer's
# interval (so a runner that has gone away ages out of "active" quickly), with a
# floor so a tiny interval still tolerates a missed write or two.
_STALE_INTERVAL_MULTIPLE = 3
_STALE_FLOOR_SECONDS = 30.0


def default_status_path() -> Path:
    """Where the runner status file lives — a sibling of the per-run logs."""
    return state_path("status.json")


class StatusStore:
    """Read/write the runner status file.

    A thin naming layer over :class:`~issuebot.state.StateFile`, which supplies
    the atomic write, the permissions and the never-raise posture. This store
    was the only one that already wrote atomically; now every one does.
    """

    def __init__(self, path: Path) -> None:
        self._file = StateFile(path)

    def write(self, payload: dict[str, Any]) -> None:
        """Atomically replace the status file with ``payload``."""
        self._file.write_json(payload)

    def read(self) -> dict[str, Any] | None:
        """The parsed status payload, or None if absent or unreadable."""
        payload = self._file.read_json()
        return payload or None

    def clear(self) -> None:
        """Remove the status file — used when a runner shuts down."""
        self._file.delete()


def build_payload(
    connections: Sequence[ConnectionSnapshot],
    *,
    version: str,
    interval: float,
    now: datetime,
    pid: int,
) -> dict[str, Any]:
    """Assemble the status payload written to disk.

    Each connection entry keeps the identity half of its snapshot (``{name,
    board, target, phase, ref}``); the log tail and links stay out of the file —
    ``issuebot status`` does not show them, and rewriting a 200-line tail to
    disk every tick buys nothing offline. The header fields let a reader judge
    freshness and identify the writing process.
    """
    return {
        "pid": pid,
        "version": version,
        "interval": interval,
        "updated_at": now.isoformat(),
        "connections": [
            {"name": s.name, "board": s.board, "target": s.target, "phase": s.phase, "ref": s.ref}
            for s in connections
        ],
    }


def _parse_dt(value: Any) -> datetime | None:
    """Parse an ISO-8601 timestamp, or ``None`` if it is missing/malformed."""
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def status_age(payload: dict[str, Any], *, now: datetime) -> float | None:
    """Seconds since the payload was last written, or ``None`` if unknown."""
    updated = _parse_dt(payload.get("updated_at"))
    if updated is None:
        return None
    return (now - updated).total_seconds()


def is_stale(payload: dict[str, Any], *, now: datetime) -> bool:
    """True when the writing ``listen`` process has gone away (or never stamped a
    time): the file is older than ``3×`` its own write interval (min 30s)."""
    age = status_age(payload, now=now)
    if age is None:
        return True
    interval = payload.get("interval") or 15.0
    threshold = max(float(interval) * _STALE_INTERVAL_MULTIPLE, _STALE_FLOOR_SECONDS)
    return age > threshold


def _age_str(seconds: float | None) -> str:
    """A short human age like ``5s`` / ``3m`` / ``2h`` (``?`` when unknown)."""
    if seconds is None:
        return "?"
    seconds = max(0.0, seconds)
    if seconds < 90:
        return f"{seconds:.0f}s"
    if seconds < 5400:
        return f"{seconds / 60:.0f}m"
    return f"{seconds / 3600:.0f}h"


def render_status(
    connections: Sequence[Any],
    payload: dict[str, Any] | None,
    *,
    now: datetime,
    resolve_log: Callable[[str], str | None] | None = None,
) -> str:
    """Render the ``issuebot status`` table.

    A header line states whether a runner is active, stale, or absent; then one
    line per configured connection joining its static config (name, board,
    target) with the live ``phase`` and ``ref`` from a fresh status file.
    ``resolve_log(ref)`` (optional) supplies the active run's log path to show
    alongside a working connection.
    """
    lines: list[str] = []
    age = status_age(payload, now=now) if payload is not None else None
    active = payload is not None and not is_stale(payload, now=now)

    if payload is None:
        lines.append("Runner: no status file — no runner active on this machine.")
    elif not active:
        lines.append(f"Runner: stale (last seen {_age_str(age)} ago) — no runner active.")
    else:
        pid = payload.get("pid", "?")
        ver = payload.get("version") or "?"
        lines.append(f"Runner: active (pid {pid}, v{ver}, updated {_age_str(age)} ago).")

    lines.append("")

    if not connections:
        lines.append("No connections configured — add one with 'issuebot connect'.")
        return "\n".join(lines)

    runtime: dict[str, dict[str, Any]] = {}
    if active and payload is not None:
        for entry in payload.get("connections", []):
            if isinstance(entry, dict) and entry.get("name"):
                runtime[str(entry["name"])] = entry

    for conn in connections:
        target = conn.folder or conn_setting(conn, "repo") or ""
        rt = runtime.get(conn.name) or {}
        phase = rt.get("phase") or "—"
        ref = rt.get("ref") or "—"
        log = ""
        if resolve_log is not None and rt.get("ref"):
            resolved = resolve_log(str(rt["ref"]))
            if resolved:
                log = f"  {resolved}"
        board = conn_setting(conn, "board")
        lines.append(f"{conn.name}  {board}  {target}  {phase}  {ref}{log}")

    return "\n".join(lines)
