"""Railway's own settings, and the credential they resolve to.

Naming one vendor's environment variables from the core config module is
exactly the leak ADR-0002 forbids: the token variable names live here and
nowhere else.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from issuebot.config import Connection

# Network mode for a sandbox: isolated (no inbound/outbound beyond what the
# sandbox needs) or private (attached to the project's private network).
RailwayNetwork = Literal["isolated", "private"]

# Which kind of Railway credential a connection holds, and so which environment
# variable the `railway` CLI must read it from (see :func:`token_env`).
RailwayTokenKind = Literal["project", "account"]

# Which environment variable the `railway` CLI reads each kind of token from.
# A *project* token (scoped to one project+environment) goes in RAILWAY_TOKEN;
# an *account*/team token in RAILWAY_API_TOKEN. Putting one in the other's
# variable simply fails to authenticate, so the kind is configured explicitly
# per connection rather than guessed from the token's shape.
TOKEN_VARS: dict[str, str] = {"project": "RAILWAY_TOKEN", "account": "RAILWAY_API_TOKEN"}


class RailwaySettings(BaseModel):
    """What a connection needs to run its tasks in Railway sandboxes.

    Each field's ``description`` is not decoration: it is what ``issuebot
    connect --set`` shows for that key, so a setting only reachable through the
    generic flag is as self-explanatory as a dedicated flag's help would be.
    A plugin that leaves them off gets its keys listed bare.
    """

    environment_id: str = Field(
        description="Railway environment the per-task sandbox is created in."
    )

    network: RailwayNetwork = Field(
        default="isolated",
        description=(
            "'private' joins the environment's private network, so the sandbox can reach "
            "your other Railway services; 'isolated' has no such access."
        ),
    )

    # Per-connection rather than one process-wide env var so a single runner can
    # drive sandboxes in several Railway projects at once (a project token only
    # reaches one project).
    token: str | None = Field(
        default=None,
        description="This connection's own Railway credential. Unset inherits the runner's env.",
    )

    token_kind: RailwayTokenKind = Field(
        default="project",
        description=(
            "Which kind 'token' is, and so which variable the CLI reads it from: a project "
            "token is scoped to one project+environment, an account/team token is not. Not "
            "guessable from the token itself."
        ),
    )


def for_connection(conn: Connection) -> RailwaySettings | None:
    """This connection's ``[railway]`` settings, typed — or None if it has none.

    ``railway`` is not a declared ``Connection`` field (a connection is a
    neutral wiring diagram), so after a TOML round trip it arrives as a plain
    dict; this re-validates it for typed access. A connection that actually
    selects this environment always has a valid table by the time it runs —
    ``validate_config`` checks it against this model at load."""
    raw = getattr(conn, "railway", None)
    return RailwaySettings.model_validate(raw) if raw else None


def token_env(token: str | None, kind: RailwayTokenKind = "project") -> dict[str, str]:
    """The environment overlay carrying one connection's Railway credential.

    Returns ``{}`` when the connection configures no token — the child then
    inherits whatever the process was started with, which is the pre-existing
    single-global-token behaviour.

    When a token IS configured the *other* token variable is mapped to ``""``,
    which :class:`~issuebot.process.RealProcess` reads as "remove this variable":
    an ambient token left in the shell for a different Railway project must not
    shadow the credential this connection asked for.
    """
    if not token:
        return {}
    chosen = TOKEN_VARS.get(kind, TOKEN_VARS["project"])
    return {var: (token if var == chosen else "") for var in TOKEN_VARS.values()}


def ambient_token() -> str | None:
    """Whatever Railway credential this process was started with, if any.

    The pre-per-connection way of authenticating, still honoured as a fallback:
    a connection with no token of its own inherits this. The one place that
    knows both variable names are candidates."""
    return os.environ.get(TOKEN_VARS["project"]) or os.environ.get(TOKEN_VARS["account"])
