"""Runner config: a wiring diagram, not a settings dump.

``Config`` and ``Connection`` hold only what is core to every install — which
harness runs, how many tasks run at once, which connections exist, and (per
connection) which source/workspace/environment/sinks it wires together.
Everything plugin-specific — the issuebear PAT, git's clone root, a sink's
summary model — lives in a plugin-owned table or flat key and is read back
through ``settings_for``, never as a named field here.

Both models allow extra keys (``extra="allow"``): a flat key belongs to
whichever plugin's settings model claims it (checked at plugin discovery, not
here — this module cannot import the registry's plugins without a cycle), a
table belongs to the plugin of the same name. :func:`validate_config` checks
at load that every key has an owner.

Stored as TOML, written through :mod:`issuebot.state` so it gets the same
atomic, 0600 treatment as everything else issuebot persists — a connection's
plugin tables may hold secrets (issuebear's PAT, a sink's token). The
Issuebear server never sees any of this.
"""

from __future__ import annotations

import difflib
import os
import shlex
import tomllib
from collections.abc import Iterable
from pathlib import Path
from typing import TYPE_CHECKING, Any

import tomli_w
import typer
from pydantic import BaseModel, ConfigDict, ValidationError, model_validator

from issuebot import plugins, release
from issuebot.plugins.base import (
    EnvironmentPlugin,
    HarnessPlugin,
    Plugin,
    SourcePlugin,
    WorkspacePlugin,
)
from issuebot.process import REAL, Process
from issuebot.state import StateFile, config_dir

if TYPE_CHECKING:
    from issuebot.plugins.harnesses.base import Harness

# Full-path override for the config file, honoured ahead of the XDG location.
CONFIG_ENV = "ISSUEBOT_CONFIG"

# What a runner runs when the board tells it to update itself, unless the config
# says otherwise. The one definition — `Config.update_command`'s default, the
# `Supervisor`'s and the command loop's — because three copies of a string is
# how two of them end up stale.
#
# It runs issuebot's own latest-release installer. The release installer then
# resolves the latest immutable wheel before installing it.
#
# `sh -c` because the command is executed WITHOUT a shell (`commands.
# _default_run_update` splits it with `shlex.split` and hands argv to
# `subprocess.run`), so the download/install shell program must be one argv.
DEFAULT_UPDATE_COMMAND = shlex.join(["sh", "-c", release.installer_command()])


class SinkRef(BaseModel):
    """One sink on a connection, and whether its failure blocks the decisions.

    `extra="forbid"`: unlike `Connection`/`Config`, nothing ever claims a key
    inside this table — it has no plugin of its own — so a typo here (
    `requird` for `required`) has nowhere to be caught except here. Silently
    dropping it (pydantic's default) would leave `required` at its default
    with no error, exactly the false belief the governing rule exists to catch.
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    required: bool = True

    @model_validator(mode="before")
    @classmethod
    def _accept_a_bare_name(cls, value: Any) -> Any:
        """A bare name (`sinks = ["…"]`) is the common case: no table needed."""
        return {"name": value} if isinstance(value, str) else value


# How a command line says the `required=False` half of a SinkRef. A suffix
# rather than a second flag: one `--sinks` reads in the order the sinks run,
# and the same word the `connections` listing already prints back.
BEST_EFFORT = "best-effort"


def parse_sink(text: str) -> SinkRef:
    """`github` or `github:best-effort` → one :class:`SinkRef`.

    The name is checked against the installed sinks *here*, when the user typed
    it, so a typo names the sinks that do exist instead of writing a config that
    only fails on the next load."""
    name, _, qualifier = text.strip().partition(":")
    if qualifier not in ("", "required", BEST_EFFORT):
        raise ValueError(
            f"unknown sink qualifier '{qualifier}' (use 'required' or '{BEST_EFFORT}')"
        )

    plugins.get("sinks", name)  # raises UnknownPlugin, which already names them
    return SinkRef(name=name, required=qualifier != BEST_EFFORT)


class Connection(BaseModel):
    """One source→workspace→environment→sinks wiring.

    Everything beyond the five fields below belongs to a plugin: a flat key
    (a workspace's `repo`, a source's `board`) or a table named after the plugin
    (`[connections.<plugin>]`). Cross-field validation of those (clone needs a
    repo, ...) is the owning plugin's `validate` hook, not this model's.
    """

    model_config = ConfigDict(extra="allow")

    name: str | None = None
    # Which source plugin this connection reads work from. Unset means the one
    # source this install has (see `source_plugin`).
    source: str | None = None
    # Absolute local path the agent runs in. A connection whose working copy
    # is made fresh per task (a workspace's clone strategy) leaves this None.
    folder: str | None = None
    # Which execution environment plugin runs this connection's tasks. Unset
    # means the one environment this install has — see `executor_name`, which is
    # the only thing that should read this field directly.
    #
    # Plain str, not a closed Literal: that would need editing every time an
    # environment plugin is added or deleted, and would reject an unknown name
    # with a pydantic error before `validate_config` gets a chance to name it
    # properly against the installed registry.
    executor: str | None = None
    sinks: list[SinkRef] = []

    @property
    def key(self) -> str:
        """The connection's name as a definite ``str``.

        ``name`` is typed Optional because a bare Connection may omit it, but the
        ``connect`` CLI sets it on every connection it writes, so every connection
        the runner manages has one. Code that keys runtime state by connection
        (listener/board maps, status.json, workspace directory names) uses this to
        read the name without re-narrowing ``str | None`` at each call site. Raises
        if called on a nameless connection — a misuse the CLI prevents (mirrors
        ``local_folder``)."""
        if self.name is None:
            raise RuntimeError("connection has no name")
        return self.name

    @property
    def local_folder(self) -> str:
        """The local working folder as a definite ``str``.

        ``folder`` is typed Optional because a connection working from a fresh
        clone has none. Code on the local-folder paths uses this to read the
        folder without re-narrowing ``str | None`` at each git call site. Raises
        if called on a connection with no folder — a misuse the workspace
        plugin's own validator catches at load."""
        if self.folder is None:
            raise RuntimeError(f"connection '{self.name}' has no local folder")
        return self.folder

    def settings_for(self, plugin: Plugin) -> dict[str, Any]:
        """This connection's settings for one plugin: its flat keys, or its table.

        A flat plugin (``plugin.flat``) claims bare keys on the connection, so
        this returns whichever of ``plugin.claimed_keys`` are actually set. A
        table plugin's settings live under a key named after the plugin."""
        dumped = self.model_dump()
        if plugin.flat:
            return {k: v for k, v in dumped.items() if k in plugin.claimed_keys}
        return dumped.get(plugin.name) or {}


class Config(BaseModel):
    """The wiring diagram: the harness, run limits, and every connection.

    Extra keys are allowed and become plugin global settings, one table per
    plugin name, read back through ``settings_for`` once a plugin exists to
    claim the table.
    """

    model_config = ConfigDict(extra="allow")

    # Which harness plugin runs the agent. Its own settings (command path,
    # whatever else it takes) live in a table of the same name. Unset means the
    # one harness this install has — see `harness_name`, which is the only
    # thing that should read this field directly.
    harness: str | None = None
    # Optional hard wall-clock limit per task. None → no timeout (the run only
    # ends when the agent exits or the user aborts). When set, a run that
    # exceeds it is auto-aborted and classified "timed out".
    task_timeout_minutes: int | None = None
    # Command run to self-update on an "update" control before re-exec.
    update_command: str = DEFAULT_UPDATE_COMMAND
    # Maximum number of tasks the runner works on concurrently.
    max_concurrent: int = 1
    connections: list[Connection] = []

    def connection(self, name: str) -> Connection | None:
        """Return the runner-connection with this name, or None if not configured."""
        return next((p for p in self.connections if p.name == name), None)

    def settings_for(self, plugin: Plugin) -> dict[str, Any]:
        """This config's global settings table for one plugin, by its name."""
        return self.model_dump().get(plugin.name) or {}


# ---------------------------------------------------------------------------
# Accessors for settings that moved off Config/Connection into a plugin table
# ---------------------------------------------------------------------------
#
# Two shapes below: a typed accessor (`global_settings`) validates one plugin's
# table against that plugin's own settings model, with the *caller* saying which
# plugin. A raw-table accessor (`plugin_tables`, `harness_settings`) reads tables
# directly instead: `plugin_tables` because its whole job is to hand each plugin
# its own table without knowing what any of them mean; `harness_settings`
# because `command` is the one field every harness's table might carry
# regardless of whether that harness has a settings model of its own (see its
# own docstring).
#
# What is deliberately *not* here: an accessor that names the plugin it reads —
# that turns one plugin's settings into runner-wide fields wearing neutral
# names.


def source_plugin(name: str | None = None) -> SourcePlugin:
    """The source plugin `name` selects, or the only one installed.

    The source counterpart of :func:`harness_for`: one place turns a config
    value into a plugin, so nothing else has to know a source's name. Unlike the
    harness there is no top-level `source` key to read — a source is chosen per
    connection — so the fallback is "the one installed", which is what an
    install-wide caller (`init`, `doctor`, the supervisor) means when it asks for
    "the source" and what a config with one source table already says.

    A default rather than a hard-coded name, so deleting the last source plugin
    is an ``UnknownPlugin`` naming what *is* installed rather than an
    ``ImportError``; and so the day a second source ships, an install-wide caller
    is told to say which rather than silently getting whichever sorted first.
    """
    if name is None:
        installed = plugins.names_of("sources")
        if len(installed) != 1:
            raise plugins.UnknownPlugin(
                f"no source named, and {len(installed)} are installed "
                f"(known: {', '.join(installed) or 'none installed'})"
            )
        name = installed[0]

    plugin = plugins.get("sources", name)
    if not isinstance(plugin, SourcePlugin):
        # Mirrors `harness_for`: narrows the registry's generic `Plugin` for the
        # type checker, and catches a source never upgraded past a placeholder.
        raise TypeError(f"source plugin '{name}' has no implementation")
    return plugin


def global_settings(cfg: Config, plugin: Plugin) -> Any:
    """One plugin's global settings table, typed against that plugin's own model.

    The single-plugin form of :func:`plugin_tables`, for the callers that hold a
    ``Config`` rather than a `RunnerContext`. The caller supplies the plugin, so
    the only thing this knows is "a plugin's table validates against its own
    model" — which is exactly what :func:`~issuebot.runner.sinks_for` does per
    sink at construction time. An accessor that named the plugin it reads would
    make that plugin's settings look like a fact about the config, so a second
    plugin on the same axis would have to declare the first one's field names or
    the read would raise.
    """
    assert plugin.global_settings is not None
    return plugin.global_settings.model_validate(cfg.settings_for(plugin))


def plugin_tables(cfg: Config) -> dict[str, Any]:
    """Every top-level plugin settings table, keyed by the plugin that owns it.

    The whole-config form of :meth:`Config.settings_for`, which answers for one
    plugin at a time. A factory that builds plugins deep inside a run —
    :func:`issuebot.runner.sinks_for`, reached from a listener long after the
    ``Config`` went out of scope — is handed this and gives each plugin its own
    table, so no runner-wide value has to be one plugin's setting in disguise.

    Every table is a dict and no core field is, so the filter is exactly "the
    extra keys that name a plugin"; a table whose plugin is not installed is
    already a load-time error (:func:`validate_config`), never reached here.
    """
    return {name: table for name, table in cfg.model_dump().items() if isinstance(table, dict)}


def harness_name(cfg: Config) -> str:
    """Which harness this config runs: the one it names, or the one installed.

    The harness counterpart of :func:`source_plugin`'s fallback, and the only
    place `Config.harness` is read. Not a hard default of one plugin's name —
    that is a privileged plugin dressed up as a default: every config naming no
    harness becomes invalid the day that plugin is deleted.

    Unlike sources there is genuinely more than one harness installed here, so
    the fallback usually *cannot* resolve, and that is the point: with several
    to choose from there is no non-arbitrary answer, so the config is told to
    say which and given the list. `issuebot init` asks, so every config the
    wizard writes names one; the fallback is for a hand-written config on an
    install that has only one harness to mean. The message below names the key
    to write, not only the plugins to choose from.
    """
    if cfg.harness is not None:
        return cfg.harness

    installed = plugins.names_of("harnesses")
    if len(installed) != 1:
        raise plugins.UnknownPlugin(
            f"no harness named, and {len(installed)} are installed — "
            f'set harness = "…" (known: {", ".join(installed) or "none installed"})'
        )
    return installed[0]


def harness_settings(cfg: Config, name: str | None = None) -> dict[str, Any]:
    """One harness's raw settings table (`command` and whatever else it takes),
    or {} if absent. Defaults to the harness this config runs.

    Reads the table as written rather than through a typed model: `command` is
    the one field every harness's table might carry (a harness with a
    `global_settings` model has it field-checked there; another may have no
    model at all), so a raw read here is what stays generic across both.

    `name` is for a caller that already holds a harness and wants *its* table
    rather than the configured one — the session store, which is handed the
    harness it is deciding about."""
    return cfg.model_dump().get(name or harness_name(cfg)) or {}


def harness_for(cfg: Config, *, proc: Process = REAL) -> Harness:
    """The configured harness plugin's implementation, given its `command`
    override (if any) and wired to `proc`.

    The one place that turns a config into a runnable `Harness`, so a new
    harness plugin needs no change here."""
    name = harness_name(cfg)
    plugin = plugins.get("harnesses", name)
    if not isinstance(plugin, HarnessPlugin):
        # Every plugin discovered under "harnesses" is registered as a
        # HarnessPlugin — this narrows the registry's generic `Plugin` return
        # type for the type checker and doubles as a guard against a
        # harness plugin that was never upgraded past a placeholder.
        raise TypeError(f"harness plugin '{name}' has no implementation")
    command = harness_settings(cfg).get("command")
    kwargs: dict[str, Any] = {"proc": proc}
    if command:
        kwargs["command"] = command
    return plugin.harness(**kwargs)


def executor_name(conn: Connection) -> str:
    """Which environment this connection runs in: the one it names, or the one
    installed.

    The third instance of one idiom, after :func:`source_plugin` and
    :func:`harness_name`, and the only place `Connection.executor` is read. Not
    a hard default of one plugin's name — a privileged plugin dressed up as a
    default: every connection naming no environment becomes invalid the day
    that plugin is deleted.

    Two environments ship, so — exactly as with the harness — the fallback
    usually *cannot* resolve, and that is the point rather than a shortcoming:
    running a task on this machine and running it in a paid cloud sandbox are
    not interchangeable, so with both installed there is no answer silence can
    honestly mean. Both the wizard and `connect --executor` ask, so every
    connection this tool writes names one; the fallback is for a hand-written
    config on an install with a single environment to mean.

    Not resolved on `runs_in_process` (`runner.in_process_environment`) either,
    tempting as "default to running here" sounds: that would make the capability
    a privilege and hand the same plugin the same silent win under a new name.
    A capability answers "which one can do this job", never "which one did you
    mean".
    """
    if conn.executor is not None:
        return conn.executor

    installed = plugins.names_of("environments")
    if len(installed) != 1:
        raise plugins.UnknownPlugin(
            f"no environment named, and {len(installed)} are installed — "
            f'set executor = "…" (known: {", ".join(installed) or "none installed"})'
        )
    return installed[0]


def maybe_executor_name(conn: Connection) -> str | None:
    """:func:`executor_name`, or None when it cannot be told which environment
    this is.

    For the two validation helpers that only need a *name to match against* —
    "is this table's plugin in play" — and have no business raising over a
    connection whose real problem is reported once, properly, by
    :func:`_named_plugin_problems`."""
    try:
        return executor_name(conn)
    except plugins.UnknownPlugin:
        return None


def conn_setting(conn: Connection, key: str, default: Any = None) -> Any:
    """One plugin-owned flat connection setting, read back through the owning
    plugin's own settings model.

    ``Connection`` declares none of these keys, but each is a field of the
    plugin that claims it — so the value comes back typed by that model, and an
    unset key answers with the *owner's* declared default rather than a copy of
    it spelled here. ``default`` answers only when no installed plugin claims
    the key, or the connection cannot satisfy the owner's model at all — both
    configs :func:`validate_config` refuses to load, met here only by
    hand-built values.
    """
    plugin = plugins.claimant(key)
    if plugin is None or plugin.settings is None:
        return getattr(conn, key, default)

    try:
        return getattr(plugin.settings.model_validate(conn.settings_for(plugin)), key)
    except ValidationError:
        return getattr(conn, key, default)


def default_config_path() -> Path:
    """The XDG config path for the runner config file.

    ``$ISSUEBOT_CONFIG`` overrides it with a full path — the same escape hatch
    every state file has, and the one the CLI and the in-sandbox worker use."""
    override = os.environ.get(CONFIG_ENV)
    if override:
        return Path(override)
    return config_dir() / "config.toml"


class ConfigError(ValueError):
    """A config says something no installed plugin can honour.

    Raised by :func:`load_config` with every problem :func:`validate_config`
    found, newline-joined — a hand-edited file usually has more than one, and
    reporting them a round trip at a time is miserable.
    """


def _maybe_plugin(kind: str, name: str) -> Plugin | None:
    """The installed plugin `name` of `kind`, or None if it doesn't exist."""
    try:
        return plugins.get(kind, name)
    except plugins.UnknownPlugin:
        return None


def _check_named_plugin(kind: str, name: str | None) -> list[str]:
    """`name` must name an installed plugin of `kind` — [] if unset or found.

    Reuses :class:`~issuebot.plugins.UnknownPlugin`'s own message (it already
    names what exists) rather than building a second version of the same text.
    """
    if name is None:
        return []
    try:
        plugins.get(kind, name)
    except plugins.UnknownPlugin as exc:
        return [str(exc)]
    return []


def _suggest(key: str) -> str:
    """' — did you mean 'x'?' for a key close to a real one, or '' if none.

    The candidate pool is every core field name plus every plugin's claimed
    keys — everything a config key could sensibly have meant."""
    candidates = set(Config.model_fields) | set(Connection.model_fields)
    for plugin in plugins.every():
        candidates |= plugin.claimed_keys
    match = difflib.get_close_matches(key, candidates, n=1, cutoff=0.6)
    return f" — did you mean '{match[0]}'?" if match else ""


def _model_problems(model: type[BaseModel], data: dict[str, Any], label: str) -> list[str]:
    """Field-level errors from validating `data` against `model`, one per
    error, each labelled `label.field: message` (ordinary pydantic validation,
    surfaced instead of swallowed)."""
    try:
        model.model_validate(data)
    except ValidationError as exc:
        return [f"{label}.{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()]
    return []


def _key_problems(conn: Connection, label: str) -> list[str]:
    """Keys nothing claims, and keys for a plugin this connection does not use.

    A table plugin is only meaningful when the connection
    actually selects it; a flat plugin's keys carry no separate selector —
    their presence on the connection *is* the selection.

    "Which environment" can fail to resolve at all, and the environments axis is
    then skipped rather than treated as "none of them". The difference is a false
    accusation: a connection carrying an environment's settings table but no
    `executor`, on an install with several, has not said it does not use that
    environment — it has said nothing, which `_named_plugin_problems` reports
    once, correctly. Telling it both at once sent the user to fix the wrong line.
    """
    environment = maybe_executor_name(conn)

    used = {environment, *(s.name for s in conn.sinks)}
    if conn.source:
        used.add(conn.source)

    problems: list[str] = []
    for key in conn.model_extra or {}:
        owner = plugins.claimant(key)
        if owner is None:
            problems.append(f"connection '{label}': unknown key '{key}'{_suggest(key)}")
        elif environment is None and isinstance(owner, EnvironmentPlugin):
            continue  # cannot say whether it is used until the axis resolves
        elif not owner.flat and owner.name not in used:
            problems.append(
                f"connection '{label}': '{key}' sets {owner.name}'s settings, but this "
                f"connection does not use {owner.name}"
            )
    return problems


def _named_plugin_problems(conn: Connection, label: str) -> list[str]:
    """`source`/`executor`/each sink must resolve to an installed plugin.

    The environment goes through :func:`executor_name`, so both ways it can fail
    are caught at load with the same sentence the run would have raised: a name
    nothing answers to, and no name at all on an install with more than one
    environment to mean (mirrors :func:`_harness_problems`).
    """
    problems: list[str] = []
    try:
        environment: str | None = executor_name(conn)
    except plugins.UnknownPlugin as exc:
        environment, problems = None, [f"connection '{label}': {exc}"]

    for kind, name in (("sources", conn.source), ("environments", environment)):
        problems += [f"connection '{label}': {p}" for p in _check_named_plugin(kind, name)]
    for sink in conn.sinks:
        problems += [f"connection '{label}': {p}" for p in _check_named_plugin("sinks", sink.name)]
    return problems


def unconfigured_workspace() -> WorkspacePlugin:
    """The workspace a connection that claims no workspace keys at all resolves to.

    Not resolved the way `source_plugin`/`harness_name`/`executor_name` resolve:
    those read a setting the connection wrote, and a ``workspace = "..."``
    setting here would make people say something they never say. An environment
    is *chosen* — running on this machine and running in a paid cloud sandbox
    are not interchangeable, so silence there cannot honestly mean either —
    whereas a plain folder is what you get when you ask for nothing. Silence is
    a real answer here, so it is answered rather than refused.

    Answered by *capability*, the way :func:`in_process_environment` is: a
    connection with no strategy configured has no branch and no base to diff
    against, so it can derive no :class:`~issuebot.contracts.Changes` — and
    "could a run here produce ``changes``" is a fact each workspace already
    declares about itself (``Workspace.produces``). Core reads that declaration
    instead of spelling a name.

    The second rung is what keeps the capability from handing the declaring
    plugin a silent monopoly: an install where every workspace has a
    version-control strategy still runs a keyless connection, provided there is
    only one workspace to mean — with one installed there is no choice to get
    wrong, and a keyless connection in git is in-place work, which git already
    supports (working directly is its strategy key's absence).

    Anything else is refused with the list: two workspaces deriving nothing, or
    none deriving nothing and several installed, has no non-arbitrary answer, and
    taking whichever sorted first is how a privileged plugin gets reinvented
    under a new name.

    **That refusal cannot be answered by editing a connection, and the message
    says so.** No connection setting selects a workspace, so the only remedies
    are to uninstall a workspace or to give the connection some workspace's own
    keys — the message carries both rather than sending the user to
    `config.toml` to look for a key that does not exist.

    The two failing shapes need different words: each rung reports its own
    count, so an install with two changes-producing workspaces and none without
    is never told "2 of them derive no changes".
    """
    installed = {
        name: plugin
        for name, plugin in plugins.all_of("workspaces").items()
        if isinstance(plugin, WorkspacePlugin)
    }

    # Rung 1: the workspaces that derive nothing — what a connection with no
    # strategy configured actually wants.
    derives_nothing = sorted(
        name for name, plugin in installed.items() if "changes" not in plugin.workspace.produces
    )

    # Rung 2: failing that, the whole set, which resolves only when it holds one.
    candidates = derives_nothing or sorted(installed)

    if len(candidates) == 1:
        return installed[candidates[0]]

    # Which rung is speaking, in its own numbers. `derives_nothing` empty means
    # rung 1 found nothing and rung 2 could not choose; non-empty means rung 1
    # itself was ambiguous, and `candidates` *is* `derives_nothing`.
    trouble = (
        f"{len(derives_nothing)} of them do: {', '.join(derives_nothing)}"
        if derives_nothing
        else f"none does, and the {len(candidates)} installed are not a choice between them"
    )
    raise plugins.UnknownPlugin(
        f"a connection setting no workspace keys resolves to the installed workspace that "
        f"derives no changes, and {trouble} "
        f"(installed: {', '.join(plugins.names_of('workspaces')) or 'none'}). "
        f"No connection setting selects a workspace, so this is fixed by uninstalling one "
        f"or by giving the connection a workspace's own keys."
    )


def plugins_in_play(conn: Connection, harness: str | None = None) -> dict[str, Plugin]:
    """Every plugin this connection actually resolves to, by name: named
    directly (source/executor/sinks/harness), flat and claiming at least one key
    the connection sets, or — for a connection that claims no workspace's keys
    at all — the workspace the *run* would resolve for it anyway.

    That last clause keeps validation and the run agreeing. `runner.
    workspace_for` resolves a workspace for a keyless connection, so that
    workspace's settings model and `validate` hook must be applied at load too:
    on a git-only install a connection with neither `folder` nor `repo` is
    rejected at load with git's own "nowhere to work" sentence rather than
    meeting it at run time.

    Resolved through :func:`unconfigured_workspace`, the same
    call the run makes, rather than a second copy of the rule — so the two
    cannot answer differently, and it names no plugin, so nothing here becomes
    undeletable.

    When *that* cannot choose (two changes-free workspaces installed, say), no
    workspace goes in play: the refusal is the run's to make, in its own words,
    and repeating it as a per-connection config problem would report one
    install-wide fact once per connection.
    """

    extra = conn.model_extra or {}
    named = [
        ("sources", conn.source),
        ("environments", maybe_executor_name(conn)),
        ("harnesses", harness),
    ]
    named += [("sinks", sink.name) for sink in conn.sinks]

    resolved = {p.name: p for n, v in named if v and (p := _maybe_plugin(n, v)) is not None}
    for plugin in plugins.every():
        if plugin.flat and plugin.claimed_keys & extra.keys():
            resolved[plugin.name] = plugin

    if not any(isinstance(p, WorkspacePlugin) for p in resolved.values()):
        try:
            fallback = unconfigured_workspace()
        except plugins.UnknownPlugin:
            return resolved
        resolved[fallback.name] = fallback

    return resolved


def workspaces_claiming(keys: Iterable[str]) -> list[Plugin]:
    """Every flat workspace plugin that owns at least one of these connection keys.

    The same rule :func:`plugins_in_play` applies to a saved connection, asked of
    a bare set of keys — because the two callers that need it do not both have a
    connection. ``runner.workspace_for`` does; `issuebot connect` and the wizard
    do not, and still have to know whose rules the folder being typed will be
    held to (see `Workspace.folder_problem`).

    In name order, so a connection that contradicts itself by setting keys of two
    workspaces gets the same answer from both callers rather than one each. That
    contradiction is nothing's to reject yet — see `runner.workspace_for`.
    """
    wanted = set(keys)
    return [
        plugin
        for name in plugins.names_of("workspaces")
        if (plugin := plugins.get("workspaces", name)).flat and plugin.claimed_keys & wanted
    ]


def _plugin_use_problems(conn: Connection, label: str, harness: str | None) -> list[str]:
    """Each plugin actually in play: its settings model against its table
    (or flat keys), and its `validate` hook against the whole connection."""
    problems: list[str] = []
    for plugin in plugins_in_play(conn, harness).values():
        if plugin.settings is not None:
            errors = _model_problems(plugin.settings, conn.settings_for(plugin), plugin.name)
            problems += [f"connection '{label}': {e}" for e in errors]
        if plugin.validate is not None:
            problems += [f"connection '{label}': {p}" for p in plugin.validate(conn)]
    return problems


def _source_problems(cfg: Config) -> list[str]:
    """The installed source's global settings table must be there, and complete.

    Unlike every other plugin's table this one is not optional: `source_settings`
    is read by every command that reaches the board, so a config without it is a
    config no command can run. The top-level loop in :func:`validate_config`
    cannot catch that — it walks the keys the file *has* — so an absent table
    would surface as a raw pydantic `ValidationError` out of the first API
    call; this check reports it at load instead.
    """
    try:
        plugin = source_plugin()
    except plugins.UnknownPlugin:
        return []  # no source installed at all: `init` reports that in its words

    if plugin.global_settings is None or plugin.name in (cfg.model_extra or {}):
        return []  # nothing to require, or the loop above already checked it

    return _model_problems(plugin.global_settings, {}, f"[{plugin.name}]")


def _harness_problems(cfg: Config) -> list[str]:
    """The harness this config runs must resolve to one installed plugin.

    Both ways it can fail land here with the registry's own sentence: a name
    nothing answers to, and no name at all on an install with more than one
    harness to mean. Caught at load rather than at launch so a config that
    cannot run is rejected by the command that reads it, not by the supervisor
    an hour into a listen."""
    try:
        plugins.get("harnesses", harness_name(cfg))
    except plugins.UnknownPlugin as exc:
        return [str(exc)]
    return []


def connection_problems(conn: Connection, harness: str | None) -> list[str]:
    """Everything wrong with one connection: keys nothing claims, keys for a
    plugin this connection does not use, plugin names that don't exist, and
    each plugin actually in play — its settings model against its table, and
    its `validate` hook against the whole connection."""
    label = conn.name or "<unnamed>"
    return (
        _key_problems(conn, label)
        + _named_plugin_problems(conn, label)
        + _plugin_use_problems(conn, label, harness)
    )


def validate_config(cfg: Config) -> list[str]:
    """Everything this config says that no installed plugin can honour.

    The governing rule: a key that cannot apply is an error, not something
    ignored. This walks the whole config — top-level tables, the harness, and
    every connection's keys, named plugins and settings — and returns every
    problem found, empty when the config is safe to load.
    """
    problems: list[str] = []

    for key, value in (cfg.model_extra or {}).items():
        if key not in plugins.every_name():
            problems.append(f"unknown key '{key}'{_suggest(key)}")
            continue
        plugin = plugins.named(key)
        if plugin is not None and plugin.global_settings is not None:
            problems += _model_problems(plugin.global_settings, value, f"[{key}]")

    problems += _source_problems(cfg)
    problems += _harness_problems(cfg)

    for conn in cfg.connections:
        problems += connection_problems(conn, cfg.harness)

    return problems


def load_config(path: Path | None = None) -> Config | None:
    """Load and validate the config from TOML, or None if the file is absent.

    Unlike runner state, a corrupt config raises: it is user-authored, so
    silently reading it as empty would drop the user's connections rather than
    tell them their file is broken. A structurally valid but impossible config
    (a key no plugin claims, a name nothing installed answers to) raises too —
    see :func:`validate_config`."""
    path = path or default_config_path()
    if not path.exists():
        return None
    with path.open("rb") as fh:
        data = tomllib.load(fh)
    cfg = Config.model_validate(data)
    problems = validate_config(cfg)
    if problems:
        raise ConfigError("\n".join(problems))
    return cfg


def load_config_or_fail(path: Path | None = None) -> Config | None:
    """Load the config, or exit(1) saying what is wrong with the user's file.

    Three ways it can fail and every one of them is the file rather than a bug
    in issuebot, so not one should reach the user as a traceback:

    * it is not TOML (``TOMLDecodeError``);
    * it is TOML but the wrong shape, e.g. ``harness = 123`` (pydantic
      ``ValidationError``);
    * it loads but names nothing this install has (:class:`ConfigError`) — a
      mistyped key, or a plugin not installed here, which is exactly what a
      user meets the first time they run against a build with a plugin removed.

    All three are reported the same way because the user cannot act on the
    distinction: whichever it is, the answer is to go and look at the file, and
    the message says which file and what is wrong with it.

    Absent is not a failure here — it answers ``None``, which lets a command
    that has a sensible no-config behaviour (``issuebot railway ...`` falling
    back to the ambient token) keep it. A command that needs a config calls
    :func:`require_config` instead.

    This lives in ``config`` rather than ``cli`` because *every* mounted plugin
    command needs it, and a plugin CLI importing ``issuebot.cli`` would be a
    circular import — ``cli`` is what mounts them.
    """
    path = path or default_config_path()

    try:
        return load_config(path)
    except (ConfigError, tomllib.TOMLDecodeError, ValidationError) as problem:
        typer.echo(f"Config error in {path}:\n{problem}", err=True)
        raise typer.Exit(1) from None


def require_config(path: Path | None = None) -> Config:
    """The config, or exit(1) — :func:`load_config_or_fail` plus refusing absence.

    The one place the "run ``issuebot init`` first" sentence is spelled, for
    core and plugin CLIs alike.
    """
    cfg = load_config_or_fail(path)

    if cfg is None:
        typer.echo("No config found — run 'issuebot init' first.", err=True)
        raise typer.Exit(1)

    return cfg


def save_config(cfg: Config, path: Path | None = None) -> None:
    """Write the config to TOML, atomically and privately — connection plugin
    tables may hold secrets (issuebear's PAT, a sink's token).

    TOML has no null, so unset optional fields are dropped and round-trip as
    their declared defaults."""
    path = path or default_config_path()
    StateFile(path).write_text(tomli_w.dumps(cfg.model_dump(exclude_none=True)))
