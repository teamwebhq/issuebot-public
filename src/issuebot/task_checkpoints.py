"""Per-task sandbox checkpoint bookkeeping: task_id -> when it was taken.

A ``task-<id>`` checkpoint is created only when a run ends waiting on a human —
a ``needs_input`` output (see :mod:`issuebot.sandbox`) — so this module's whole
job is remembering *when*, so a TTL sweep can find the ones nobody came back to
answer. Kept on this side because a sandbox provider is not obliged to expose
per-checkpoint metadata, and the one that exists does not.
"""

from __future__ import annotations

import time
from pathlib import Path

from issuebot.state import KeyedStore, state_path


def default_state_path() -> Path:
    """Where the checkpoint bookkeeping lives, beside the session map."""
    return state_path("task-checkpoints.json")


def checkpoint_name(task_id: str) -> str:
    """The sandbox checkpoint name for a task's paused state.

    The single source of the ``task-`` prefix: the boot ladder, the end-of-run
    decision and the TTL sweep all name checkpoints through here, so they can
    never drift apart on the spelling."""
    return f"task-{task_id}"


def _store(path: Path | None) -> KeyedStore:
    return KeyedStore(path or default_state_path())


def record(task_id: str, *, path: Path | None = None, now: float | None = None) -> None:
    """Record that this task was just checkpointed, refreshing an existing
    timestamp if it was."""
    _store(path).set(task_id, now if now is not None else time.time())


def forget(task_id: str, *, path: Path | None = None) -> None:
    """Drop the entry — its checkpoint was deleted, or never existed."""
    _store(path).drop(task_id)


def aged(ttl_seconds: float, *, path: Path | None = None, now: float | None = None) -> list[str]:
    """The task ids whose checkpoint was recorded more than ``ttl_seconds`` ago.

    Returns ids rather than checkpoint names so the caller can both delete the
    checkpoint (via :func:`checkpoint_name`) and drop its bookkeeping (via
    :func:`forget`) — a sweep that deleted without forgetting would retry the
    same already-deleted names on every later run."""
    cutoff = (now if now is not None else time.time()) - ttl_seconds
    return [
        task_id
        for task_id, ts in _store(path).all().items()
        if isinstance(ts, int | float) and ts < cutoff
    ]
