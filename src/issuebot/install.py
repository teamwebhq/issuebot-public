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

Stored as plain text, not JSON, so existing installs are read unchanged. Either
can be named by an environment variable instead, for the runner whose
filesystem does not outlive it (see :data:`INSTALL_ID_ENV`,
:data:`AGENT_ID_ENV`): a cache is no use on a machine that cannot keep one.
"""

from __future__ import annotations

import os
from pathlib import Path

from issuebot.state import StateFile, state_path

# Names the install id outright, for a runner whose filesystem does not outlive
# it: a container with no persistent volume loses the cached id on every
# restart, mints a fresh one, and arrives at the server as a new install each
# time — a new row, and the per-install controls aimed at the old one.
INSTALL_ID_ENV = "ISSUEBOT_INSTALL_ID"

# The same escape hatch for the other id, and for the same runner: the agent id
# is normally learned from the first `connect()` of a run and cached, but a
# restart's `connect()` answers 409 with no body — so a runner that lost the
# cache does not learn it again until a board it has never connected to appears,
# and until then it cannot assign a mention session to itself.
AGENT_ID_ENV = "ISSUEBOT_AGENT_ID"


def default_install_path() -> Path:
    return state_path("install_id")


def default_agent_path() -> Path:
    return state_path("agent_id")


def _read(path: Path) -> str | None:
    text = StateFile(path).read_text()
    return text.strip() or None if text is not None else None


def _named_or_cached(env: str, path: Path) -> str | None:
    """``$env`` when it is set, else the id cached at ``path``, else None.

    One shape for both ids, because the runner that needs it needs it for both:
    the variable states an identity that this machine cannot keep on disk, and
    the file is only a cache of what an earlier run was told. So the variable
    wins — a cache that disagrees with what the operator named is the stale
    half.
    """
    override = os.environ.get(env, "").strip()
    if override:
        return override

    return _read(path)


def load_install_id(path: Path | None = None) -> str | None:
    """This runner's install id: ``$ISSUEBOT_INSTALL_ID`` when it is set, else
    the persisted one, else None (not registered yet).

    Read here rather than at each caller so registration and the connect draft
    (:mod:`issuebot.intake`) cannot disagree about which install this is.

    A runner given an id never mints one, so nothing writes the file either —
    :meth:`issuebot.runner.Supervisor.start` registers only when this returns
    None. The id has to be one the server already knows; a name invented here
    registers nothing and telemetry goes nowhere.
    """
    return _named_or_cached(INSTALL_ID_ENV, path or default_install_path())


def save_install_id(path: Path | None, install_id: str) -> None:
    """Persist the minted install id."""
    StateFile(path or default_install_path()).write_text(install_id)


def load_agent_id(path: Path | None = None) -> str | None:
    """This runner's own user id: ``$ISSUEBOT_AGENT_ID`` when it is set, else
    the cached one, else None (not learned yet).

    Unlike the install id, a named agent id is a starting point rather than the
    last word: the board resolves the calling agent from the PAT, so a
    ``connect()`` that answers with an identity is the server's own statement of
    who this runner is, and :meth:`issuebot.runner.Supervisor._remember_agent_id`
    lets that answer replace this one. A variable naming another workspace's
    agent therefore corrects itself on the first fresh connect rather than
    self-assigning that agent's work forever.
    """
    return _named_or_cached(AGENT_ID_ENV, path or default_agent_path())


def save_agent_id(path: Path | None, agent_id: str) -> None:
    """Persist the agent's own user id."""
    StateFile(path or default_agent_path()).write_text(agent_id)
