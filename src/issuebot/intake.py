"""Taking on a new board↔folder connection: validate it, register it, keep it.

Both ways of adding a connection — ``issuebot connect`` with flags and the
interactive wizard — end here, so they cannot apply different rules or produce
different messages (ADR-0006).

Two shapes carry it: :class:`Draft` is what a caller has gathered, and
:class:`Result` is what happened. Neither knows what a terminal is, and a
draft is a Connection-shaped value from the start, so adding a
:class:`~issuebot.config.Connection` field is one edit here.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from issuebot import install as install_store
from issuebot import plugins
from issuebot.config import (
    Config,
    Connection,
    conn_setting,
    connection_problems,
    executor_name,
    parse_sink,
    save_config,
    workspaces_claiming,
)
from issuebot.plugins.base import WorkspacePlugin
from issuebot.plugins.sources.base import ConnectionConflict

logger = logging.getLogger("issuebot")

# Accepted forms for a clone URL. Public because the wizard asks the same
# question interactively and had its own identical copy: one shape, checked
# once, so the flag path and the prompt can never disagree about what a clone
# URL is.
REPO_URL = re.compile(r"^(https://|git@|ssh://).+")


class IntakeError(ValueError):
    """The connection cannot be taken on, and why."""


class MissingExecutor(IntakeError):
    """More than one environment is installed and the draft names none.

    Its own type, not just a sentence: the rule is :func:`config.executor_name`'s
    and its message speaks config (``set executor = "…"``), which is the wrong
    vocabulary at a command line. The CLI catches this and names its own
    ``--executor`` flag instead. The wizard never raises it — it asks."""


# Connection keys `connect` writes itself, from its own dedicated flags, and
# the flag that writes each — in the order `connections` prints them back. A
# `--set` for one of these would be overwritten in :func:`build` — the core
# flag wins, silently — so it is refused instead and the flag named.
#
# Only keys some installed plugin can actually claim belong here — `--folder`
# writes `folder`, but that is a declared `Connection` field no plugin claims,
# so a `--set` could never produce it and an entry for it could never fire.
#
# **This table (with `_connection_shaped` below, and `connect`'s matching flag
# declarations in `cli.py`) is the single residue of plugin vocabulary in
# core.** `board`/`done`/`confirm`/`mode` are the issuebear source's;
# `git_init`/`branch_prefix`/`update_base`/`repo` are the git workspace's —
# every other trace (the wizard's questions, the value domains, the settings'
# types) now lives on the owning plugins. The flags themselves stay because
# deleting one is a user-visible CLI change; the CLI-mounting mechanism mounts
# whole command groups per plugin, not flags on core's own commands, so the
# mapping is declared here, once, instead.
#
# What the `--set` design does hold for is a *new* plugin: one added today
# claims its keys, declares its settings model, and reaches the CLI through
# `plugins.settings_from` with no edit to this file — proven by mounting a
# throwaway plugin (`tests/plugins/test_mounting.py`).
FLAG_OWNED = {
    "board": "--board",
    "repo": "--repo",
    "mode": "--mode",
    "git_init": "--isolation",
    "done": "--done",
    "confirm": "--confirm",
    "update_base": "--update-base",
    "branch_prefix": "--branch-prefix",
}


def _flag_default(key: str) -> Any:
    """The owning plugin's declared default for a flag-owned key, or None when
    nothing installed claims it.

    Read off the claimant's settings model — the same generic machinery
    ``--set`` resolves through — so the drop-at-default pruning below cannot
    drift from the owner's own defaults, and no plugin module is imported into
    core (which would block deleting the plugin)."""
    owner = plugins.claimant(key)
    if owner is None or owner.settings is None or key not in owner.settings.model_fields:
        return None
    return owner.settings.model_fields[key].get_default()


def _connection_shaped(settings: dict[str, Any]) -> dict[str, Any]:
    """The flags' own vocabulary translated to the keys a connection is saved
    with — the flag half of the residue :data:`FLAG_OWNED` documents.

    ``--isolation`` speaks "none"/"branch"/"worktree", where a saved connection
    carries the git workspace's own ``git_init`` and working directly is the
    key's *absence*. Never a present-but-None key (whether the caller said
    "none" or passed a literal None): a flat plugin is selected by key
    presence, so writing the None would put git in play for a connection with
    no git strategy at all.

    And because the flags are ordinary Typer options with defaults, ``connect``
    always sends ``branch_prefix``/``update_base`` whether or not the caller
    chose one — so their mere presence can't mean "the user asked for this".
    With no strategy there is no branch to prefix or update, so they are
    dropped when they sit at the owning model's declared default
    (:func:`_flag_default` — the value is the owner's, never a copy spelled
    here); a value the caller genuinely changed survives, and git's
    ``validate`` correctly rejects *that* as a strategy-less setting nothing
    would ever use.

    The ``"none"`` literal below is ``--isolation``'s own flag vocabulary —
    the flag-path residue :data:`FLAG_OWNED` documents, spelled in this one
    translation only.
    """
    shaped = dict(settings)

    isolation = shaped.pop("isolation", None)
    if isolation not in (None, "none"):
        shaped["git_init"] = isolation

    if shaped.get("git_init") is None:
        for key in ("branch_prefix", "update_base"):
            if key in shaped and shaped[key] == _flag_default(key):
                shaped.pop(key)

    return shaped


def from_flags(
    name: str,
    board: str,
    *,
    settings: dict[str, Any],
    sinks: Sequence[str],
    assignments: Sequence[str],
) -> Draft:
    """A Draft from a scripted ``connect``'s flags — the second way one is
    gathered, beside :func:`issuebot.wizard.run`.

    ``settings`` is the flags' own keys; this turns them Connection-shaped
    (:func:`_connection_shaped`) and adds what still needs interpreting:
    ``sinks`` refs are parsed, ``assignments`` (``--set plugin.key=value``)
    are resolved against the installed plugins, and an assignment for a key
    one of the flags writes is refused so the flag cannot silently win.
    Everything it refuses is an :class:`IntakeError`, before any board call.

    A draft naming no executor is refused (:class:`MissingExecutor`) exactly
    when :func:`config.executor_name` could not resolve one later — asked here,
    up front, so the user is still looking at what they typed.
    """
    try:
        plugin_settings = plugins.settings_from(assignments)
        chosen_sinks = [parse_sink(text) for text in sinks]
    except ValueError as exc:
        raise IntakeError(str(exc)) from exc

    for key in sorted(plugin_settings.keys() & FLAG_OWNED.keys()):
        raise IntakeError(f"'{key}' is set by {FLAG_OWNED[key]}, not --set — use the flag.")

    # The same rule `build` enforces via `connection_problems`, asked before
    # anything else so the refusal carries its own type (see MissingExecutor).
    if settings.get("executor") is None:
        try:
            executor_name(Connection())
        except plugins.UnknownPlugin as exc:
            raise MissingExecutor(str(exc)) from exc

    return Draft(
        name=name,
        board=board,
        settings={**_connection_shaped(settings), "sinks": chosen_sinks, **plugin_settings},
    )


class _Client(Protocol):
    """The one board call intake makes."""

    def connect(
        self, board_id: str, name: str | None = ..., *, install_id: str | None = ...
    ) -> dict[str, Any]: ...


@dataclass(frozen=True)
class Draft:
    """A connection as gathered, before it is known to be valid.

    ``settings`` holds whatever the caller collected, keyed exactly as
    :class:`~issuebot.config.Connection` fields — so a new field is a new key,
    not a new parameter on four signatures.
    """

    name: str
    board: str
    settings: dict[str, Any] = field(default_factory=dict)

    def get(self, key: str, default: Any = None) -> Any:
        """One gathered setting, or its default when the caller did not collect it."""
        return self.settings.get(key, default)


@dataclass(frozen=True)
class Result:
    """What taking on a connection did."""

    connection: Connection

    # The board's response, when it answered. Empty when registration could not
    # be reached — the connection is still saved, and `issuebot listen` retries.
    registered: bool = False

    # Anything the caller should show the user that is not an error: a server
    # warning, or the note that registration will be retried later.
    warnings: list[str] = field(default_factory=list)


def _core_folder_error(folder: str) -> str | None:
    """Core's own folder rule — an existing absolute directory — on its own.

    Split out of :func:`folder_error` because :func:`build` applies the two
    halves at different times: this one before anything else (there is nowhere
    to work), the workspace's own requirement only *after* the plugin
    `validate` hooks have had their say, so a contradictory draft gets the
    owning plugin's accurate sentence first."""
    path = Path(folder)
    if not path.is_absolute() or not path.is_dir():
        return f"Folder must be an existing absolute directory: {folder}"
    return None


def folder_error(folder: str, settings: Mapping[str, Any]) -> str | None:
    """Why this folder cannot be worked in, or None when it can.

    Two rules, and only the first is core's: a folder has to be an existing
    absolute directory. What *else* a folder must be depends entirely on the
    workspace that will prepare a working copy in it — a git strategy needs a
    repository, working in place needs nothing — so this asks whichever
    workspace the draft's gathered ``settings`` select (`workspaces_claiming`,
    the same presence-of-keys rule the run resolves by) rather than knowing any
    strategy itself. Keys still unset (None) select nothing, exactly as they
    would on a saved connection, where an unset key is absent. A workspace with
    no requirement (and a draft that selects none at all) answers None, and the
    folder is accepted.

    Shared by intake and by the wizard's own as-you-type validation, so the
    check the user fails interactively is the check that would have rejected the
    connection anyway — which is why `folder_problem` is synchronous and cheap.
    A sink's own prerequisites (the GitHub sink needs an `origin` remote to
    open a PR against) are `issuebot doctor`'s job once `sinks` names it, not
    intake's — a connection with no sinks yet configured must not be rejected
    for a requirement it doesn't have."""
    core = _core_folder_error(folder)
    if core is not None:
        return core

    # In name order when a draft contradicts itself with two workspaces' keys —
    # the same tie `runner.workspace_for` breaks the same way. A workspace
    # plugin with no implementation behind it is `workspace_for`'s to complain
    # about, when something tries to run in it.
    claimed = {key for key, value in settings.items() if value is not None}
    plugin = next(iter(workspaces_claiming(claimed)), None)
    if not isinstance(plugin, WorkspacePlugin):
        return None

    return plugin.workspace.folder_problem(folder)


def _resolve_source(draft: Draft) -> str | None:
    """The folder to persist, or None when the working copy is a clone.

    Where the copy comes from is its own question, and the draft answers it by
    which of ``repo``/``folder`` it carries — not by any workspace strategy,
    which says what gets cut *inside* the copy and applies to both alike. A
    connection that clones stores no folder; one that does not needs one that
    exists."""
    repo = draft.get("repo")
    folder = draft.get("folder")

    # Refused here, not left to the workspace's own "not both" rule: this
    # function is about to *drop* the folder for a repo draft, which would hide
    # the contradiction from that rule entirely — the user typed two answers to
    # one question and would have one of them silently discarded.
    if repo and folder:
        raise IntakeError("give --folder or --repo, not both: a connection works from one place.")

    if repo:
        if not REPO_URL.match(repo):
            raise IntakeError(f"repo must be an https/ssh git URL: {repo}")
        return None

    if not folder:
        raise IntakeError("a folder is required unless a clone URL is given.")

    # Only core's half of the folder check here. The workspace's own
    # requirement waits until `build` has run the plugin `validate` hooks: a
    # draft whose settings contradict each other (a stray `--update-base` with
    # no strategy) must get the owning plugin's accurate sentence, not a
    # complaint about a repo the saved connection was never going to use.
    error = _core_folder_error(folder)
    if error is not None:
        raise IntakeError(error)

    return folder


def build(cfg: Config, draft: Draft) -> Connection:
    """Validate a draft into a Connection, or raise :class:`IntakeError`.

    Everything a connection must satisfy on its own is
    :class:`~issuebot.config.Connection`'s own validators; what needs the rest of
    the config — that no other connection already holds this board — is here.
    """
    # A draft arrives already Connection-shaped: the wizard's hooks return each
    # plugin's own keys, and the flag path went through `_connection_shaped`.
    # Nothing here interprets any plugin's key by name — a plugin's rules about
    # its own keys are its settings model's and `validate` hook's, applied
    # below through `connection_problems` like everything else's.
    settings = dict(draft.settings)
    settings["folder"] = _resolve_source(draft)
    if settings["folder"] is not None:
        settings.pop("repo", None)

    # One agent must never hold two connections to the same board: both would
    # claim the same work. Checked before any board call or config write.
    clash = next(
        (
            c
            for c in cfg.connections
            if conn_setting(c, "board") == draft.board and c.name != draft.name
        ),
        None,
    )
    if clash is not None:
        raise IntakeError(
            f"This agent already has a connection to board {draft.board} "
            f"('{clash.name}'). Disconnect it first."
        )

    try:
        connection = Connection.model_validate(
            {**settings, "name": draft.name, "board": draft.board}
        )
    except ValueError as exc:
        # Cross-field combinations are the plugin `validate` hooks' business,
        # not Connection's, but a malformed value (e.g. a settings dict
        # pydantic can't coerce) still raises here.
        raise IntakeError(str(exc)) from exc

    # The check `load_config` runs, run before the write rather than after: a
    # connection whose plugin settings don't hang together (a table for a plugin
    # it doesn't use, a value outside a plugin's own domain) would otherwise be
    # saved happily and then refuse to load, leaving the user with a config only
    # a text editor can fix.
    #
    # It is the same *function*, but not always the same verdict: this runs
    # against the in-memory Connection, and load runs against what TOML could
    # hold of it. Any key whose value TOML drops (a None) is in play here and
    # absent there — which is why nothing above may write one.
    problems = connection_problems(connection, cfg.harness)
    if problems:
        raise IntakeError("\n".join(problems))

    # The workspace's own folder requirement, asked last — after the plugin
    # rules above have accepted the settings that select it (see
    # `_resolve_source`). Same rule the wizard applies as-you-type.
    if connection.folder is not None:
        error = folder_error(connection.folder, settings)
        if error is not None:
            raise IntakeError(error)

    return connection


def finalize(cfg: Config, draft: Draft, client: _Client, *, path: Path | None = None) -> Result:
    """Validate, register with the board, and persist — in that order.

    Ordering is the whole point of doing these together. A board that says the
    connection already exists aborts *without* writing config, because saving it
    would leave a connection that can never claim anything. A board that cannot
    be reached at all does not abort: the config is written and ``issuebot
    listen`` reconciles it on startup, so a network blip does not lose the setup
    the user just did.

    Raises :class:`IntakeError` for anything the user must fix.
    """
    connection = build(cfg, draft)
    warnings: list[str] = []
    registered = False

    try:
        response = client.connect(
            draft.board, draft.name, install_id=install_store.load_install_id()
        )
        registered = True
        if response.get("warning"):
            warnings.append(str(response["warning"]))
    except ConnectionConflict as exc:
        raise IntakeError(
            f"This agent is already connected to board {draft.board} on the server. "
            f"Disconnect it first."
        ) from exc
    except Exception as exc:  # noqa: BLE001 - transient: keep the config, retry on listen
        # No `exc_info`: this is a *handled* failure that the caller is about to
        # explain in one line, and `connect` is a command somebody is sitting in
        # front of. A stack trace in the middle of a wizard reads as a crash —
        # the user's own answers scroll away and the sentence that says what to
        # do next arrives underneath something that looks much worse.
        logger.warning("server connect failed for board %s: %s", draft.board, exc)
        warnings.append(f"server connect failed: {exc}; the runner will retry on `issuebot listen`")

    # Replace a connection with the same name, else append.
    cfg.connections = [c for c in cfg.connections if c.name != draft.name]
    cfg.connections.append(connection)
    save_config(cfg, path)

    return Result(connection=connection, registered=registered, warnings=warnings)
