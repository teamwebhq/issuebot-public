"""Runner identity, cached so it survives a restart.

Two ids live here:

* the Parade-minted **install id**, minted at first registration and reused
  afterwards so an install stays stable across restarts; and
* the **agent id** — the runner's own user id, learned from the ``connect()``
  response, so a restart (where connect returns 409 with no body) still knows
  who the agent is without a ``GET /me``.

Both are identity tokens sitting alongside session resume tokens, so they get
the same private, atomic, never-raise treatment from :mod:`issuebot.state`
(ADR-0005).

Stored as plain text, not JSON, so existing installs are read unchanged.
"""

from __future__ import annotations

from pathlib import Path

from issuebot.state import StateFile, state_path


def default_install_path() -> Path:
    return state_path("install_id")


def default_agent_path() -> Path:
    return state_path("agent_id")


def _read(path: Path) -> str | None:
    text = StateFile(path).read_text()
    return text.strip() or None if text is not None else None


def load_install_id(path: Path | None = None) -> str | None:
    """The persisted install id, or None if not yet registered."""
    return _read(path or default_install_path())


def save_install_id(path: Path | None, install_id: str) -> None:
    """Persist the minted install id."""
    StateFile(path or default_install_path()).write_text(install_id)


def load_agent_id(path: Path | None = None) -> str | None:
    """The cached agent (user) id, or None if not yet learned."""
    return _read(path or default_agent_path())


def save_agent_id(path: Path | None, agent_id: str) -> None:
    """Persist the agent's own user id."""
    StateFile(path or default_agent_path()).write_text(agent_id)
