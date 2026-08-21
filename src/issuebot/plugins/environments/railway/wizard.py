"""The ``issuebot connect`` wizard's railway branch.

Moved off ``wizard.py``, which otherwise spelled out one vendor's environment
id, network modes, token kinds and both token variable names. The generic
wizard now asks whichever environment the user picked for its own hook, so a
second environment adds its questions by writing this file.
"""

from __future__ import annotations

import shutil
from typing import Any, get_args

import typer

from issuebot.plugins.environments.railway.settings import (
    DEFAULT_COMMAND,
    RailwayNetwork,
    RailwaySettings,
    RailwayTokenKind,
    ambient_token,
)


def _warn_prereqs(has_token: bool = False) -> None:
    """Doctor-style, non-fatal warnings for a freshly-wizarded railway
    connection: the ``railway`` CLI on PATH and *some* credential available,
    plus a reminder to build the shared sandbox template once.

    ``has_token`` is True when the connection just configured its own token, in
    which case the ambient-environment fallback is irrelevant and unmentioned.

    The same checks ``issuebot doctor`` runs against an already-saved
    connection, said early enough to act on. The connection is saved regardless.
    """
    # The wizard does not ask where the CLI is, so the check is the default name
    # on PATH. A connection that needs an absolute path sets `railway.command`
    # afterwards, and `issuebot doctor` then checks that path instead.
    if shutil.which(DEFAULT_COMMAND) is None:
        typer.echo(
            f"Warning: '{DEFAULT_COMMAND}' CLI not found on PATH (required to run railway tasks).",
            err=True,
        )
    if not has_token and not ambient_token():
        typer.echo(
            "Warning: this connection has no token and neither RAILWAY_TOKEN nor "
            "RAILWAY_API_TOKEN is set (needed to create sandboxes).",
            err=True,
        )
    typer.echo(
        "Reminder: run 'issuebot railway build-template' once before your first "
        "railway task. You can raise max_concurrent in config.toml to run more "
        "railway tasks in parallel."
    )


def wizard(*, choose_literal: Any) -> dict[str, Any]:
    """Gather the settings this environment owns — nothing else's.

    A Railway sandbox boots empty; the *consequences* of that (a fresh clone
    per task, the work cut onto a task branch, build mode) are the workspace's
    and the source's own hooks to draw, told through the wizard's neutral
    ``sandboxed`` fact — this hook answers only for ``[connections.railway]``.
    ``checkpoint`` isn't prompted — it derives a sane default — and neither is
    ``max_concurrent``, which is a global ``Config`` setting rather than
    per-connection; :func:`_warn_prereqs` points at both.

    ``choose_literal`` is the generic wizard's own numbered picker, handed in
    rather than imported, so this module stays a leaf and the questions look
    identical to every other one asked.
    """
    typer.echo(
        "A Railway sandbox starts empty, so railway tasks clone a repo into it "
        "and work on a task branch — both are set for you."
    )

    environment_id = typer.prompt(
        "Railway environment id (the Railway environment sandboxes are created in)"
    ).strip()

    typer.echo(
        "'private' joins the environment's private network so the sandbox can "
        "reach your other Railway services; 'isolated' (default) has no such access."
    )
    network = choose_literal("Railway network", get_args(RailwayNetwork), "isolated")

    # Per-connection credential: a Railway *project* token only reaches one
    # project, so a runner listening on connections in different projects needs
    # one token each. Blank inherits whatever the `issuebot listen` process was
    # started with.
    typer.echo(
        "Railway token for this connection. Leave blank to use whatever "
        "RAILWAY_TOKEN / RAILWAY_API_TOKEN the runner is started with — but note "
        "one env var can only reach one Railway project."
    )
    token = typer.prompt("Railway token (blank = use the runner's env)", default="").strip()

    token_kind = "project"
    if token:
        typer.echo(
            "A project token is scoped to one project+environment (read from "
            "RAILWAY_TOKEN); an account/team token covers the whole account "
            "(RAILWAY_API_TOKEN)."
        )
        token_kind = choose_literal("Token kind", get_args(RailwayTokenKind), "project")

    _warn_prereqs(has_token=bool(token))

    return {
        "railway": RailwaySettings(
            environment_id=environment_id,
            network=network,
            token=token or None,
            token_kind=token_kind,
        ),
    }
