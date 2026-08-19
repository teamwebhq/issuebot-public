"""``issuebot railway ...``: project-wide Railway administration.

Mounted under the plugin's own name by ``plugins.mount_cli`` — the same
mechanism the git workspace's ``worktree``/``clone`` commands use — so the
top-level CLI declares no Railway command group of its own.

These act on a whole Railway *project* rather than one task, which is why they
borrow a connection's credentials rather than being handed one: a runner may
drive several projects at once, and each of these has to pick one.
"""

from __future__ import annotations

import typer

from issuebot import task_checkpoints
from issuebot.config import load_config_or_fail
from issuebot.plugins.environments.railway import settings as railway_settings
from issuebot.plugins.environments.railway.environment import TEMPLATE, RailwayProvider

app = typer.Typer(help="Manage Railway sandbox tooling.", no_args_is_help=True)


def auth_for(connection: str | None) -> dict[str, str]:
    """The credential a project-wide ``issuebot railway ...`` command runs under.

    The connection named by ``--connection``, else the only railway connection
    that configures a token of its own.

    With several such connections and no ``--connection`` the choice is genuinely
    ambiguous — each names a different Railway project — so no overlay is applied
    (falling back to the ambient environment) and a note points at the flag,
    rather than silently sweeping just one of them.

    No config at all is not an error here — these are project-wide commands and
    the ambient ``RAILWAY_TOKEN`` is a legitimate way to run them — but a config
    that exists and is *broken* is, and is reported as one rather than as a
    traceback out of ``load_config``."""
    cfg = load_config_or_fail()
    if cfg is None:
        return {}

    if connection is not None:
        conn = cfg.connection(connection)
        if conn is None:
            typer.echo(f"Unknown connection: {connection}", err=True)
            raise typer.Exit(1)
        found = railway_settings.for_connection(conn)
        return railway_settings.token_env(found.token, found.token_kind) if found else {}

    with_tokens = [
        (c, found)
        for c in cfg.connections
        if (found := railway_settings.for_connection(c)) and found.token
    ]
    if len(with_tokens) > 1:
        names = ", ".join(c.key for c, _ in with_tokens)
        typer.echo(
            f"Several railway connections have their own token ({names}); using the "
            "ambient environment. Pass --connection to pick one.",
            err=True,
        )
        return {}
    if with_tokens:
        _, found = with_tokens[0]
        return railway_settings.token_env(found.token, found.token_kind)
    return {}


@app.command("build-template")
def build_template(
    connection: str = typer.Option(
        None, "--connection", help="Railway connection whose token to build under."
    ),
) -> None:
    """Build the shared sandbox template.

    A prebuilt template lets sandbox `create` start warm (git/gh/node already
    installed) instead of re-installing them on every fresh sandbox. The
    template lives in one Railway project, so with connections across several
    projects run this once per project with ``--connection``.
    """
    RailwayProvider(auth=auth_for(connection)).build_template()
    typer.echo(f"Built template '{TEMPLATE}'.")


@app.command("prune-checkpoints")
def prune_checkpoints(
    ttl_hours: int = typer.Option(
        168, "--ttl-hours", help="Delete task-* checkpoints older than this (default: 7 days)."
    ),
    connection: str = typer.Option(
        None, "--connection", help="Railway connection whose token to sweep under."
    ),
) -> None:
    """Delete `task-*` sandbox checkpoints older than the TTL.

    A `task-<id>` checkpoint is created when a run ends waiting on a human (a
    `needs_input` output) so the next run for that task can resume straight back
    into it; this sweep reclaims the ones nobody ever came back to answer, past
    ``--ttl-hours`` (default 7 days).

    Each swept task is forgotten from the local bookkeeping whether or not the
    Railway-side delete succeeded: it is past its TTL either way, and leaving
    the entry behind would make every later sweep retry a name that is already
    gone — which raises and would kill the command. A failed delete is reported,
    not raised, so one bad entry can't stop the sweep.
    """
    provider = RailwayProvider(auth=auth_for(connection))
    aged = task_checkpoints.aged(ttl_hours * 3600)
    for task_id in aged:
        try:
            provider.delete_checkpoint(task_checkpoints.checkpoint_name(task_id))
        except Exception as exc:  # noqa: BLE001 - one bad entry must not stop the sweep
            typer.echo(f"(could not delete checkpoint for {task_id}: {exc})", err=True)
        task_checkpoints.forget(task_id)
    typer.echo(f"Pruned {len(aged)} task checkpoint(s).")
