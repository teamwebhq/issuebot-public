"""The ``issuebot`` command-line interface: one-URL setup via discovery, runner
-connection management, the listen loop, and a doctor health-check. All commands
read/write the TOML config at ``$ISSUEBOT_CONFIG`` (else the XDG default)."""

from __future__ import annotations

import sys
import time
import tomllib
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal, NoReturn

import typer
from pydantic import ValidationError

from issuebot import doctor_checks, intake, plugins, wizard, worker
from issuebot import logs as logs_mod
from issuebot.config import (
    BEST_EFFORT,
    Config,
    ConfigError,
    Connection,
    conn_setting,
    default_config_path,
    harness_for,
    load_config,
    require_config,
    save_config,
    source_plugin,
)
from issuebot.plugins.sources.base import SourceClient
from issuebot.reporter import default_log_dir
from issuebot.status import StatusStore, default_status_path, is_stale, render_status

app = typer.Typer(
    help="Run a coding agent against the tasks its configured source assigns it.",
    no_args_is_help=True,
)

# Every installed plugin's own CLI, mounted under its name — a workspace
# plugin's `worktree`/`clone` commands land here as `issuebot <plugin> worktree
# ...`, an environment plugin's template administration under its own name, and
# so on. This module declares no plugin's commands itself, which is why removing
# a plugin removes its commands and needs no edit here.
plugins.mount_cli(app)

# The two shipped plugins' dedicated `connect` flags speak these value domains
# — flag vocabulary for keys the issuebear source (`--done`/`--confirm`/
# `--mode`) and the git workspace (`--isolation`/`--branch-prefix`/
# `--update-base`) own. The settings' real types live on those plugins'
# settings models; these exist only because a Typer option needs its choices
# as an annotation, and importing a plugin module here would make core
# undeletable from. Part of the single residue `intake.FLAG_OWNED` documents —
# which key each flag writes, and why the flags are still here at all, is that
# table's comment to tell.
DoneFlag = Literal["review", "complete"]
ConfirmFlag = Literal["yes", "no"]
IsolationFlag = Literal["none", "branch", "worktree"]
ModeFlag = Literal["build", "respond"]
UpdateBaseFlag = Literal["none", "rebase", "merge"]


@app.callback()
def main(ctx: typer.Context) -> None:
    """Run a coding agent against the tasks its configured source assigns it."""
    # One Session per invocation, unless the caller (a test, through
    # `CliRunner.invoke(..., obj=...)`) already supplied one. This is the
    # commands' single seam to the outside: config and board client both come
    # through it, so a test swaps an interface, never a module attribute.
    if ctx.obj is None:
        ctx.obj = Session()


# --- helpers -----------------------------------------------------------------


def _fail(message: str) -> NoReturn:
    """Report a user-fixable problem on stderr and exit non-zero."""
    typer.secho(message, fg=typer.colors.RED, err=True)
    raise typer.Exit(1)


@dataclass(frozen=True)
class Session:
    """What a command needs from its surroundings: the saved config, and a
    board client for it.

    Built once in :func:`main` and carried to commands on ``ctx.obj``. The
    client is built lazily, per request — a command that never touches the
    board never pays for one, and an install with no source plugin only fails
    when something actually asks.

    ``make_client`` is the injection point: tests pass a Session whose factory
    returns a double, so a command is driven through ``CliRunner`` with a fake
    board and no monkeypatching of this module.
    """

    # None means "the installed source's own client" — resolved in `client`
    # rather than eagerly, for the same lazy reason as above.
    make_client: Callable[[Config], SourceClient] | None = None

    def config(self) -> Config:
        """The saved config, or exit(1) telling the user to run init."""
        return require_config()

    def client(self, cfg: Config) -> SourceClient:
        """The source's own API handle, built from its global settings.

        Asked of the source rather than constructed here: which class that is,
        and what it needs out of the config, is the source plugin's business.
        An install with no source at all fails with the registry's own sentence
        instead of an ImportError at the top of this module."""
        if self.make_client is not None:
            return self.make_client(cfg)
        try:
            return source_plugin().source.client(cfg)
        except plugins.UnknownPlugin as exc:
            _fail(str(exc))


def _log_parser() -> logs_mod.ParseLine:
    """How to read a recorded run's lines back: the configured harness's own.

    A log file holds whatever the harness printed, so only that harness can turn
    it into a feed. ``issuebot logs`` is deliberately runnable before ``init``
    and on a config whose harness this build no longer has, so anything that
    stops us naming one degrades to showing every line verbatim — strictly more
    than the alternative of refusing to render."""
    try:
        cfg = load_config(default_config_path())
        return harness_for(cfg).parse_line if cfg is not None else logs_mod.raw_event
    except (ConfigError, tomllib.TOMLDecodeError, ValidationError, plugins.UnknownPlugin):
        return logs_mod.raw_event


@app.command()
def version() -> None:
    """Print the issuebot version."""
    import issuebot

    typer.echo(issuebot.__version__)


@app.command()
def init(
    ctx: typer.Context,
    skip_harness_setup: bool = typer.Option(
        False,
        "--skip-harness-setup",
        help="Don't run the chosen harness's own setup hook (e.g. registering the board MCP).",
    ),
) -> None:
    """Set this install up against the source it works from. Prompts for one
    base URL, discovers the API + MCP URLs (falling back to asking directly),
    takes the credential, verifies it, and writes the config.

    Every word of that is the *installed source plugin's* — the questions come
    from its own `setup` hook, so this docstring names no board product; one
    that did would go stale the moment a second source landed, and prose is
    invisible to the deletion probe that catches the same mistake in code.

    An install with no source plugin at all is the one thing this cannot do
    anything about, and it is caught here rather than left to surface as a
    traceback out of the first question: `init` is the first command anyone
    runs, so it is the likeliest place to meet a build with nothing to connect
    to. Found by deleting the source plugin by hand — no grep could have."""
    session: Session = ctx.obj
    try:
        cfg = wizard.setup()
    except plugins.UnknownPlugin as exc:
        _fail(str(exc))

    client = session.client(cfg)
    try:
        work = client.get_tasks(wait=0)
    except Exception as exc:  # noqa: BLE001 - surface any connection/auth failure
        typer.echo(f"Could not verify the PAT: {exc}", err=True)
        raise typer.Exit(1) from exc
    finally:
        client.close()

    save_config(cfg, default_config_path())

    if not skip_harness_setup:
        doctor_checks.run_harness_doctor(cfg, echo=typer.echo)

    typer.echo(f"Connected — {len(work)} task(s) claimable.")


@app.command()
def connect(
    ctx: typer.Context,
    name: str = typer.Option(None, "--name", help="A short name for this connection."),
    board: str = typer.Option(None, "--board", help="The id of the board to work, per the source."),
    folder: str = typer.Option(None, "--folder", help="Absolute local dir the agent runs in."),
    repo: str = typer.Option(
        None, "--repo", help="Clone URL — a fresh clone per task, instead of --folder."
    ),
    done: DoneFlag = typer.Option("review", "--done"),
    confirm: ConfirmFlag = typer.Option(
        "yes",
        "--confirm",
        help="Wait for the plan to be approved before writing any code.",
    ),
    isolation: IsolationFlag = typer.Option("none", "--isolation"),
    branch_prefix: str = typer.Option("issuebot/", "--branch-prefix"),
    mode: ModeFlag = typer.Option("build", "--mode"),
    update_base: UpdateBaseFlag = typer.Option("none", "--update-base"),
    executor: str = typer.Option(
        None,
        "--executor",
        help=(
            "Which execution environment runs this connection's tasks. Omit only "
            "when one is installed. Installed: "
            f"{', '.join(plugins.offered('environments')) or 'none'}."
        ),
    ),
    sinks: list[str] = typer.Option(
        None,
        "--sinks",
        metavar=f"NAME[:{BEST_EFFORT}]",
        help=(
            "Publish results through this sink, repeatable and in order. Suffix "
            f"':{BEST_EFFORT}' to let the task finish even if the sink fails. "
            f"Installed: {', '.join(plugins.offered('sinks')) or 'none'}."
        ),
    ),
    set_: list[str] = typer.Option(
        None,
        "--set",
        metavar="PLUGIN.KEY=VALUE",
        help=(
            "One installed plugin's own connection setting, repeatable. Takes: "
            f"{plugins.settings_help(exclude=intake.FLAG_OWNED)}"
        ),
    ),
) -> None:
    """Connect this agent to a board and map it to a local folder/repo.

    Run with no ``--name``/``--board`` to drop into an interactive wizard that
    fetches the live boards from the server and walks you through the settings
    with numbered pickers. The flags remain for scripting/non-interactive use.

    A plugin's own settings arrive through ``--set <plugin>.<key>=<value>``,
    whose vocabulary is read off the installed plugins — so a plugin adds
    settings by being installed, not by growing flags on this command
    (ADR-0002). A newly written plugin needs no edit here at all.

    Not every flag below is core, though:
    ``--board``/``--done``/``--confirm``/``--mode`` and
    ``--isolation``/``--branch-prefix``/``--update-base``/``--repo`` write keys
    the issuebear source and the git workspace claim. They are the two shipped
    plugins' flags, kept because removing them would break every script that
    uses them — see ``intake.FLAG_OWNED``."""
    session: Session = ctx.obj
    cfg = session.config()

    # No identity flags → interactive wizard: fetch the org/project/board choices
    # from the server and gather a draft. Otherwise gather one from the flags —
    # both are intake's Draft producers; this command only picks the entry path.
    if name is None and board is None:
        client = session.client(cfg)
        try:
            draft = wizard.run(client, validate_folder=intake.folder_error)
        finally:
            client.close()
    elif name is None or board is None:
        _fail(
            "Provide both --name and --board, or run 'issuebot connect' with no "
            "flags for the interactive wizard."
        )
    else:
        try:
            draft = intake.from_flags(
                name,
                board,
                settings={
                    "folder": folder,
                    "repo": repo,
                    "done": done,
                    "confirm": confirm == "yes",
                    "isolation": isolation,
                    "branch_prefix": branch_prefix,
                    "mode": mode,
                    "update_base": update_base,
                    "executor": executor,
                },
                sinks=sinks or [],
                assignments=set_ or [],
            )
        except intake.MissingExecutor:
            # The rule is intake's; only the wording is this surface's. Its own
            # sentence says `set executor = "…"` — TOML advice at a command
            # line, naming a key rather than the flag the user actually missed.
            _fail(
                "Say where this connection's tasks run with --executor "
                f"(installed: {', '.join(plugins.offered('environments')) or 'none'})."
            )
        except intake.IntakeError as exc:
            _fail(str(exc))

    _take_on(session, cfg, draft)


def _take_on(session: Session, cfg: Config, draft: intake.Draft) -> None:
    """Register and persist a gathered connection, reporting what happened."""
    client = session.client(cfg)
    try:
        result = intake.finalize(cfg, draft, client, path=default_config_path())
    except intake.IntakeError as exc:
        _fail(str(exc))
    finally:
        client.close()

    for warning in result.warnings:
        typer.secho(f"⚠️  {warning}", fg=typer.colors.YELLOW)

    target = result.connection.folder or conn_setting(result.connection, "repo")
    typer.echo(f"Connected '{draft.name}' (board {draft.board} → {target}).")


@app.command()
def disconnect(
    ctx: typer.Context,
    name: str = typer.Option(..., "--name", help="The connection to remove."),
) -> None:
    """Disconnect this agent from a board and drop the local connection."""
    session: Session = ctx.obj
    cfg = session.config()
    conn = cfg.connection(name)

    if conn is None:
        _fail(f"No connection named '{name}'.")

    cfg.connections = [c for c in cfg.connections if c.name != name]
    save_config(cfg, default_config_path())

    try:
        session.client(cfg).disconnect(conn_setting(conn, "board"))
    except Exception as exc:  # noqa: BLE001 - server failure must not block local removal
        typer.secho(f"(server disconnect failed: {exc})", fg=typer.colors.YELLOW)

    typer.echo(f"Disconnected '{name}'.")


def render_connections(connections: Sequence[Connection]) -> str:
    """Render ``issuebot connections`` as a labelled block per connection.

    A header line names the connection (name · board · folder/repo target), then
    one indented ``label  value`` line for every remaining flag-owned setting
    plus the sinks — so nothing you've selected is invisible from the command
    line.

    Stays here, beside its one caller: it renders a `Connection` and never
    touches the status payload, so it is this command's output rather than the
    status mirror's — moving it into `status.py` would have been shrinking a
    file, not finding a home.
    """
    if not connections:
        return "No connections configured — add one with 'issuebot connect'."

    lines: list[str] = [f"{len(connections)} connection{'s' if len(connections) != 1 else ''}:"]
    for c in connections:
        target = c.folder or conn_setting(c, "repo") or "—"
        lines.append("")
        board = conn_setting(c, "board", "—")
        lines.append(f"{c.name or '(unnamed)'}  ·  board {board}  ·  {target}")
        # Every flag-owned setting not already in the header, labelled with its
        # CLI flag name — read off `intake.FLAG_OWNED` so this list and the
        # flags cannot drift apart, and through `conn_setting` so each value
        # (and each unset key's default) is the owning plugin's own. `git_init`
        # prints as "none" for its unset (in-place) value because its label is
        # `--isolation`'s, whose vocabulary spells the absence that way; a bool
        # prints as the `--confirm` flag's own yes/no. A key whose owner cannot
        # resolve a value at all (a connection that plugin is not in play for —
        # `conn_setting` answers the fallback default) renders as "—" rather
        # than a value the owning model can never hold.
        for key, flag in intake.FLAG_OWNED.items():
            if key in ("board", "repo"):
                continue  # already on the header line
            value = conn_setting(c, key, "—")
            if isinstance(value, bool):
                value = "yes" if value else "no"
            lines.append(f"    {flag.lstrip('-'):<13}  {value or 'none'}")
        # `integrate` dissolved into this: "pr" is now a sink on the connection,
        # "push"/"commit" are git's own `push` setting, and "none" is simply
        # an empty list — the honest default rather than a broken config. The
        # suffix is `--sinks`' own `best-effort` qualifier, read back.
        sinks = ", ".join(s.name if s.required else f"{s.name} (best-effort)" for s in c.sinks)
        lines.append(f"    {'sinks':<13}  {sinks or 'none'}")
    return "\n".join(lines)


@app.command()
def connections() -> None:
    """List the configured board connections and all their settings."""
    cfg = require_config()
    typer.echo(render_connections(cfg.connections))


@app.command()
def status() -> None:
    """Show what's connected and what each connection is currently doing.

    Reads the local status file a running ``issuebot listen`` mirrors its runtime
    to (no server round-trip). A fresh file lists each connection's live phase,
    the task it's working, and that run's log path; an absent or stale file means
    no runner is active on this machine — the configured connections are still
    listed so you can see what would run."""
    cfg = require_config()
    payload = StatusStore(default_status_path()).read()

    def resolve_log(ref: str) -> str | None:
        run = logs_mod.latest_run_for_ref(ref, default_log_dir())
        return str(run.path) if run is not None else None

    typer.echo(
        render_status(cfg.connections, payload, now=datetime.now(UTC), resolve_log=resolve_log)
    )


@app.command()
def logs(
    ref: str = typer.Argument(
        None,
        help="Task ref to show (default: list recent runs; with -f, follow the active run).",
    ),
    follow: bool = typer.Option(
        False, "-f", "--follow", help="Tail the live run as it grows (Ctrl-C to stop)."
    ),
    raw: bool = typer.Option(
        False, "--raw", help="Print raw stream-json lines instead of the concise feed."
    ),
    lines: int = typer.Option(
        40, "-n", "--lines", help="How many trailing lines to show (0 = all)."
    ),
) -> None:
    """Print or follow per-run agent logs.

    With a ``ref`` it renders that ref's most recent run (concise feed, or raw
    jsonl with ``--raw``). With no ref it lists recent runs to pick from. ``-f``
    follows a live run: a given ref's latest run, or — with no ref — whatever the
    runner is currently working (falling back to the most recent run on disk)."""
    log_dir = default_log_dir()

    if follow:
        if ref:
            run = logs_mod.latest_run_for_ref(ref, log_dir)
        else:
            payload = StatusStore(default_status_path()).read()
            run = logs_mod.active_run(
                log_dir, payload, is_fresh=lambda p: not is_stale(p, now=datetime.now(UTC))
            )
        if run is None:
            typer.echo("No run to follow." if not ref else f"No runs found for {ref}.", err=True)
            raise typer.Exit(1)
        typer.echo(f"following {run.ref} — {run.path} (Ctrl-C to stop)", err=True)
        try:
            logs_mod.follow_log(run.path, out=sys.stdout, raw=raw, n=lines, parse=_log_parser())
        except KeyboardInterrupt:
            pass
        return

    if not ref:
        runs = logs_mod.list_runs(log_dir)
        if not runs:
            typer.echo("No runs found.")
            return
        for run in runs:
            typer.echo(f"{run.ref}  {run.started}  {run.path}")
        return

    run = logs_mod.latest_run_for_ref(ref, log_dir)
    if run is None:
        typer.echo(f"No runs found for {ref}.", err=True)
        raise typer.Exit(1)
    rendered = logs_mod.render_lines(
        logs_mod.tail(logs_mod.read_lines(run.path), lines), raw=raw, parse=_log_parser()
    )
    for line in rendered:
        typer.echo(line)


@app.command()
def listen(
    ctx: typer.Context,
    names: list[str] = typer.Argument(None, help="Connections to listen on (default: all)."),
) -> None:
    """Listen for claimable tasks on the selected boards and run the agent on
    each. Blocks until interrupted (Ctrl-C). The config file is watched for
    new/removed connections so ``issuebot connect`` takes effect without a restart."""
    session: Session = ctx.obj
    cfg = session.config()

    if not cfg.connections:
        _fail("No connections configured — add one with 'issuebot connect'.")

    if names:
        # Validate requested names exist at startup; the Supervisor will still
        # hot-reload additions/removals for the filtered set going forward.
        for name in names:
            if cfg.connection(name) is None:
                _fail(f"Unknown connection: {name}")

    from issuebot import runner

    # The Supervisor watches the config path for hot-reload, so ``issuebot
    # connect`` / ``issuebot disconnect`` while this is running takes effect
    # without a restart. What it needs out of the config is its own to know.
    sup = runner.Supervisor.from_config(cfg, session.client(cfg), harness_for(cfg), names=names)
    sup.start()

    # Show the connections that will be listened on (filtered list if names given).
    conns_to_show = [c for c in cfg.connections if c.name in names] if names else cfg.connections
    boards = ", ".join(f"{c.name} ({conn_setting(c, 'board')})" for c in conns_to_show)
    typer.echo(f"Listening on: {boards}")

    try:
        while True:
            time.sleep(0.5)
    except KeyboardInterrupt:
        # stop() aborts any in-flight agent too; give the runs a moment to
        # terminate the child and release the run before we exit. A second
        # Ctrl-C during the grace period exits immediately.
        typer.echo("stopping…")
        sup.stop()
        try:
            time.sleep(2)
        except KeyboardInterrupt:
            pass


@app.command("run-one")
def run_one(
    task: str = typer.Option(..., "--task", help="Id of the already-claimed task to run."),
    run_id: str = typer.Option(..., "--run-id", help="Agent-run id to heartbeat under."),
    connection: str = typer.Option(..., "--connection", help="Runner-connection to use."),
    kind: str = typer.Option("assigned", "--kind", help="Work kind, as delivered by the board."),
) -> None:
    """Run ONE already-claimed task to completion in this process, then exit.

    Used inside a fresh per-task sandbox: the controller claimed the run before
    the sandbox existed and holds the lock, so this never claims or releases — it
    runs the work and reports the outcome for the controller to release with.

    Prints a ``##ISSUEBOT-RESULT##`` JSON line to stdout and writes the same
    payload to a file, then exits 0 when the task finished cleanly and 1
    otherwise. The work itself is :func:`issuebot.worker.run_one`.
    """
    try:
        outcome = worker.run_one(
            require_config(),
            task_id=task,
            run_id=run_id,
            connection_name=connection,
            kind=kind,
        )
    except worker.UnknownWork as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(2) from None

    typer.echo(worker.report(outcome).sentinel_line())
    raise typer.Exit(0 if outcome.status == "done" else 1)


@app.command()
def doctor(ctx: typer.Context) -> None:
    """Check that this install can do the work: the board answers, the harness
    runs, and every plugin each connection wires together is ready.

    The PAT check is fatal (nothing else matters if no work can be fetched);
    everything after it is a warning, so one unready connection still reports
    on the rest. The checks themselves are :mod:`issuebot.doctor_checks`."""
    session: Session = ctx.obj
    cfg = session.config()

    client = session.client(cfg)
    try:
        work = client.get_tasks(wait=0)
    except Exception as exc:  # noqa: BLE001 - surface any connection/auth failure
        typer.echo(f"PAT check failed: {exc}", err=True)
        raise typer.Exit(1) from exc
    finally:
        client.close()

    typer.echo(f"PAT ok — {len(work)} task(s) claimable.")

    doctor_checks.check(cfg, echo=typer.echo, warn=lambda message: typer.echo(message, err=True))
