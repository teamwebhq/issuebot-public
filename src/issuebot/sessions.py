"""Per-task agent session ids: task_id -> session_id, so a later run can reopen
the same conversation instead of starting the task over.

A session id is a resumption token, so the file is private — which is
:mod:`issuebot.state`'s job, along with atomic writes and the never-raise
posture. What's left here is what the map *means*.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from issuebot.config import Config, harness_settings
from issuebot.state import KeyedStore, state_path

if TYPE_CHECKING:
    from issuebot.plugins.harnesses.base import Harness

# Full-path override, honoured ahead of the state directory.
STATE_ENV = "ISSUEBOT_STATE"


def default_state_path() -> Path:
    """Where the session map lives."""
    return state_path("sessions.json", env_override=STATE_ENV)


class SessionStore:
    """A ``task_id -> session_id`` map.

    Composes a :class:`~issuebot.state.KeyedStore` rather than subclassing it,
    so the methods can speak in task ids without narrowing the general
    contract."""

    def __init__(self, path: Path) -> None:
        self._store = KeyedStore(path)

    def get(self, task_id: str) -> str | None:
        """The stored session id for a task, or None."""
        value = self._store.get(task_id)
        return value if isinstance(value, str) else None

    def set(self, task_id: str, session_id: str) -> None:
        """Remember the session to resume for this task."""
        self._store.set(task_id, session_id)

    def drop(self, task_id: str) -> None:
        """Forget this task's session — it could not be reopened."""
        self._store.drop(task_id)

    def all(self) -> dict[str, str]:
        """The whole map (empty if missing or corrupt)."""
        return {k: v for k, v in self._store.all().items() if isinstance(v, str)}

    def clear(self) -> None:
        """Drop every stored session."""
        self._store.clear()


def store_for(cfg: Config, harness: Harness) -> SessionStore | None:
    """The session store this run should keep, or None when nothing resumes.

    Two things have to be true: the harness has to declare that it can reopen a
    conversation from a stored id (`resumes_sessions`), and the install has to
    have asked for it in that harness's own table (`resume_sessions`). Either
    missing means the runner starts every task fresh, which is the default.

    The settings are read under the *given* harness's name rather than the
    config's: this decides about the harness it was handed, and asking for
    someone else's table would be a different question."""
    if not harness.resumes_sessions:
        return None
    if not harness_settings(cfg, harness.name).get("resume_sessions", False):
        return None
    return SessionStore(default_state_path())
