"""``issuebot claude session ...``: the stored session ids Claude resumes from.

Moved off the top-level CLI, where ``issuebot session`` was a command group only
one harness could ever mean: a session id is Claude Code's resumption token,
read back by ``claude --resume`` and by nothing else. A harness that resumes
differently — or not at all — mounts its own group here instead of sharing this
one's vocabulary.
"""

from __future__ import annotations

import typer

from issuebot.config import require_config, source_plugin
from issuebot.sessions import SessionStore, default_state_path

app = typer.Typer(help="Claude Code harness administration.", no_args_is_help=True)

session_app = typer.Typer(
    help="Inspect and clean up stored Claude session ids (resume_sessions).",
    no_args_is_help=True,
)
app.add_typer(session_app, name="session")


@session_app.command("list")
def session_list() -> None:
    """List stored task_id -> session_id entries."""
    store = SessionStore(default_state_path())
    for task_id, session_id in store.all().items():
        typer.echo(f"{task_id}  {session_id}")


@session_app.command("prune")
def session_prune(
    refs: list[str] = typer.Argument(None, help="Task ids whose sessions to drop."),
    all_: bool = typer.Option(False, "--all", help="Clear every stored session."),
    completed: bool = typer.Option(
        False, "--completed", help="Drop sessions whose task is completed on the board."
    ),
) -> None:
    """Drop stored session ids. Requires a selector (task ids, --all, or
    --completed)."""
    if not refs and not all_ and not completed:
        typer.echo("Specify a selector: task ids, --all, or --completed.", err=True)
        raise typer.Exit(1)

    store = SessionStore(default_state_path())

    if all_:
        store.clear()
        typer.echo("Cleared all sessions.")
        return

    if refs:
        existing = store.all()
        dropped = 0
        for task_id in refs:
            if task_id in existing:
                store.drop(task_id)
                typer.echo(f"dropped {task_id}")
                dropped += 1
        if dropped == 0:
            typer.echo("No matching sessions to prune.")

    if completed:
        _prune_completed_sessions(store)


def _prune_completed_sessions(store: SessionStore) -> None:
    """Drop every stored session whose task the board reports as finished.

    A task that can't be reached is left alone — an API blip must not throw away
    a resumable session.

    The handle comes from the installed source, not from a client class this
    harness imports: "is this task finished" is a question about the work, and a
    harness has no business knowing which system answers it."""
    cfg = require_config()
    client = source_plugin().source.client(cfg)
    dropped = 0
    try:
        for task_id in list(store.all()):
            try:
                task = client.get_task(task_id)
            except Exception:  # noqa: BLE001 - unreachable task: leave it alone
                continue
            if task.get("completed") or task.get("status") == "closed":
                store.drop(task_id)
                typer.echo(f"dropped {task_id} (completed)")
                dropped += 1
    finally:
        client.close()

    if dropped == 0:
        typer.echo("No completed sessions to prune.")
