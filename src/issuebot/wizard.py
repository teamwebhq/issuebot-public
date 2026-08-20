"""Interactive "add a project" wizard behind ``issuebot connect`` (run with no
flags), and the ``issuebot init`` questions.

It asks the installed source to identify the work — whatever hierarchy that
source has, walked by the source — then walks the remaining connection settings
with their defaults pre-selected. Nothing here knows what levels a source has,
what they are called, or how many there are.

Per the task decision it uses **numbered-selection prompts** rather than raw
arrow-key pickers: no new dependency, and — crucially — it reads plain numbers
from stdin, so the whole flow is exercisable under Typer's ``CliRunner`` (which
has no real TTY) exactly like every other command in this codebase.
"""

from __future__ import annotations

import shutil
from collections.abc import Callable, Mapping, Sequence
from typing import Any, TypeVar

import typer

from issuebot import plugins
from issuebot.config import BEST_EFFORT, Config, SinkRef, source_plugin
from issuebot.intake import REPO_URL, Draft
from issuebot.plugins.base import EnvironmentPlugin

T = TypeVar("T")
L = TypeVar("L", bound=str)

# A folder validator: given (folder, settings-gathered-so-far) return an error
# message to show, or None when the folder is acceptable. Injected by the CLI
# so the wizard reuses the exact same rules as the non-interactive path — the
# settings say which workspace's own rules the folder is held to.
FolderValidator = Callable[[str, Mapping[str, Any]], "str | None"]


def setup() -> Config:
    """Ask everything ``issuebot init`` needs and return the config it describes.

    Where the work comes from is asked by the source plugin's own ``setup``
    hook, and lands in a table named after it — so the questions about one board
    server's URLs, and the auto-discovery that saves you one of them, live with
    the plugin that needs them. This function asks only what every install has
    to answer whichever plugins it wires together: which harness, and where its
    executable is.

    Nothing is verified or written here — the caller proves the credentials
    against the server just described and saves it.
    """
    source = source_plugin()
    settings = source.setup() if source.setup is not None else {}

    # Offered from the registry, not typed free-hand against a default: the
    # installed harnesses are the only answers that can work, and `choose`
    # already announces rather than asks when there is only one of them.
    harness = choose("Harness", plugins.offered("harnesses"), to_label=str)

    # Where the harness executable is. Resolved here rather than left to the
    # run: a runner started as a service (systemd, launchd, container exec) gets
    # a minimal PATH — often just /usr/bin:/bin — so a bare name that a human
    # finds in their login shell is a name the service cannot find, and the
    # failure only surfaces later, inside a task. The harness plugin's name
    # ("claude", "codex") is assumed to be the executable's name.
    #
    # Nothing found is not an error: the config may be being written on one box
    # for another. Then the default stays empty, and blank means "resolve the
    # name on PATH at run time", exactly as before.
    found = shutil.which(harness) or ""
    label = "Harness executable path" if found else "Harness executable path (blank = on PATH)"
    command = typer.prompt(label, default=found).strip()

    return Config.model_validate(
        {
            "harness": harness,
            source.name: settings,
            **({harness: {"command": command}} if command else {}),
        }
    )


def choose(
    label: str,
    options: Sequence[T],
    *,
    to_label: Callable[[T], str],
    default_index: int = 0,
    describe: Callable[[T], str] | None = None,
) -> T:
    """Pick one of ``options`` by number, re-prompting until the input is valid.

    A single option is auto-selected (and announced) so the user isn't asked to
    "choose" from a list of one. Otherwise a numbered menu is rendered, the
    ``default_index`` entry marked, and Enter accepts that default. An empty
    ``options`` aborts with a clear message rather than looping unanswerably.

    ``describe`` adds a one-line explanation beside each option, for the
    questions whose answers are words the user has no way to tell apart.
    Optional because some menus name things the user chose themselves — a board
    they created, a harness they installed — and explaining those would be
    noise.
    """
    if not options:
        typer.echo(f"No {label.lower()} available on the server.", err=True)
        raise typer.Exit(1)

    if len(options) == 1:
        only = options[0]
        typer.echo(f"{label}: {to_label(only)} (only option)")
        return only

    typer.echo(f"{label}:")
    for i, opt in enumerate(options, start=1):
        marker = "  (default)" if i - 1 == default_index else ""
        help_text = describe(opt) if describe is not None else ""
        typer.echo(f"  {i}. {to_label(opt)}{marker}")
        if help_text:
            typer.echo(f"       {help_text}")

    while True:
        raw = typer.prompt("  Enter number", default=str(default_index + 1)).strip()
        try:
            idx = int(raw) - 1
        except ValueError:
            typer.echo("  Please enter a number.", err=True)
            continue

        if 0 <= idx < len(options):
            return options[idx]
        typer.echo(f"  Choose 1–{len(options)}.", err=True)


def _choose_literal(
    label: str, values: Sequence[L], default: L, *, help_for: Mapping[str, str] | None = None
) -> L:
    """Pick one value of a settings ``Literal`` by number, defaulting to ``default``.

    Generic over the literal so the choice keeps its type: the caller is building
    a validated config field, not a free string. ``help_for`` adds a line of
    help beside each answer — a literal's values are a vocabulary the user has
    never met, so the bare list is not a question they can answer. It is the
    caller's to supply because the values are the caller's vocabulary: a plugin
    hook sends its own explanations along with its own question."""
    default_index = values.index(default) if default in values else 0
    help_map = help_for or {}
    return choose(
        label,
        list(values),
        to_label=str,
        default_index=default_index,
        describe=lambda value: help_map.get(value, ""),
    )


def _prompt_folder(validate: FolderValidator, settings: Mapping[str, Any]) -> str:
    """Prompt for a local working folder, re-asking until it validates.

    Only the folder-intrinsic rules (existing absolute dir, plus whatever the
    workspace ``settings`` select requires of one) are checked here — so this
    loop only re-asks for things a new folder can actually fix.
    """
    while True:
        folder = typer.prompt("Local folder (absolute path the agent runs in)").strip()
        error = validate(folder, settings)
        if error is None:
            return folder
        typer.echo(f"  {error}", err=True)


def _prompt_repo() -> str:
    """Prompt for a clone URL, re-asking until it looks like an https/ssh git URL."""
    while True:
        repo = typer.prompt("Clone URL (https or ssh)").strip()
        if REPO_URL.match(repo):
            return repo
        typer.echo(f"  Must be an https/ssh git URL: {repo}", err=True)


def _repo_prompter(known: str | None) -> Callable[[], str]:
    """The repo question, answered in advance when the source already knows.

    Handing back a callable rather than threading an Optional through every
    hook means no workspace plugin needs to learn that a repo can arrive from
    somewhere other than a prompt — it calls `prompt_repo()` the same way
    either way and gets the project's answer.

    The "Repository: … (from the project)" line is echoed *here*, when the
    answer is actually taken, not when the source hands it over: a workspace
    path that never asks (the folder branch) saves no repo, and announcing one
    up front would tell the user it will be used and then drop it silently."""

    def use_known() -> str:
        assert known is not None
        typer.echo(f"Repository: {known} (from the project)")
        return known

    if known:
        return use_known
    return _prompt_repo


def _workspace_questions(
    validate_folder: FolderValidator,
    *,
    prompt_repo: Callable[[], str],
    sandboxed: bool,
    changes: bool,
) -> dict[str, Any]:
    """The workspace axis's questions, asked by the plugin that owns them.

    A workspace is not *chosen* the way an environment is — a saved connection
    selects one by which keys it sets — so the hook that runs is the installed
    workspace plugin that declares one, and what it returns *is* the selection.
    More than one declaring a hook is a genuine choice and is asked exactly
    like the environment question; none installed leaves only core's own
    ``folder`` field to ask for.

    ``sandboxed``/``changes`` are the neutral facts the hook constrains itself
    by: the environment boots a fresh machine per task (read off the declared
    ``runs_in_process`` capability), and runs may report ``changes`` at all
    (stated by the source's own ``settings_wizard``). Core hands both on
    without knowing what any workspace does about them.
    """
    with_hook = [
        plugin
        for name in plugins.offered("workspaces")
        if (plugin := plugins.get("workspaces", name)).wizard is not None
    ]

    def prompt_folder(settings: Mapping[str, Any]) -> str:
        return _prompt_folder(validate_folder, settings)

    if not with_hook:
        # The fallback is a *local* folder — meaningless to an environment that
        # boots a fresh machine per task, and with no workspace hook there is
        # nothing installed that could provision a working copy from a repo.
        # Refuse here with a sentence: saved, this connection would only fail
        # inside the sandbox, where nobody is watching the wizard's answers.
        if sandboxed:
            typer.echo(
                "This environment runs each task on a fresh machine, so a local "
                "folder cannot be used — install a workspace plugin that can "
                "provision a working copy from a repository, or pick an "
                "environment that runs tasks on this machine.",
                err=True,
            )
            raise typer.Exit(1)
        return {"folder": prompt_folder({})}

    plugin = (
        with_hook[0]
        if len(with_hook) == 1
        else choose("Workspace", with_hook, to_label=lambda p: p.name)
    )
    return dict(
        plugin.wizard(
            choose_literal=_choose_literal,
            prompt_repo=prompt_repo,
            prompt_folder=prompt_folder,
            sandboxed=sandboxed,
            changes=changes,
        )
    )


def _prompt_sinks() -> list[SinkRef]:
    """Ask, sink by sink, where this connection publishes its results.

    Every installed sink is offered — the registry answers, not a list here — so
    a newly installed sink is asked about without touching this file. Each is a
    three-way choice rather than a yes/no because a sink's failure either blocks
    the task's decisions or does not, which is a real difference the connection
    has to state (`required` is the safe default, so "no" leads).

    The label does name the plugin — there is nothing else generic enough to
    say, since only the sink knows what publishing to it *does* — so a plugin
    that is not a real answer must not be offered at all. That is `offered`'s
    job, not this loop's.
    """
    chosen: list[SinkRef] = []
    for name in plugins.offered("sinks"):
        label = f"Publish results to {name}"
        answer = _choose_literal(label, ("no", "required", BEST_EFFORT), "no")
        if answer != "no":
            chosen.append(SinkRef(name=name, required=answer == "required"))
    return chosen


def run(client: Any, *, validate_folder: FolderValidator) -> Draft:
    """Drive the wizard and return the connection it gathered.

    Asks the installed source to identify the work first — its own hierarchy,
    however many levels that is and whatever they are called — and takes back
    the settings that identify the connection plus a name to suggest. The walk
    is the source's own, never a fixed organisation → project → board shape:
    that would make one board server's data model a requirement of the axis.

    A source may also hand back a ``repo`` alongside its identity — the source
    already knows it (e.g. a Parade project linked to a GitHub repo), and that
    answer is final, so the repo question that follows is answered in advance
    rather than asked; see :func:`_repo_prompter`.

    Then: an editable connection name, where tasks should run — the installed
    environments, whatever they are — and each axis's own questions in turn:
    the picked environment's wizard hook, the source's ``settings_wizard``, the
    workspace hook (:func:`_workspace_questions`). The answers are merged
    without core naming a single plugin key; what couples the axes travels as
    two neutral facts, ``sandboxed`` (read off the environment class's
    ``runs_in_process`` capability) and ``changes`` (stated by the source).
    Sinks come last, after the work is described, because they are about what
    happens to the result. Validating and saving what comes back is
    :mod:`issuebot.intake`'s job — this module only asks.
    """
    source = source_plugin()
    identity = dict(source.wizard(client, choose=choose)) if source.wizard is not None else {}

    suggested = str(identity.pop("name", None) or "connection")
    name = typer.prompt("Connection name", default=suggested).strip() or suggested

    # The source may already know which repository this connection works in —
    # a Parade project linked to a GitHub repo answers it, and that answer is
    # final. Bound here so every downstream branch keeps calling one callable;
    # the prompter announces the project's answer only if a path takes it.
    prompt_repo = _repo_prompter(identity.pop("repo", None))

    # Offered from the registry with no pre-picked name, exactly like `setup`'s
    # harness question: a default here would be one environment privileged over
    # the others by core, and `choose` already announces rather than asks when
    # only one is installed.
    executor = choose("Where should tasks run", plugins.offered("environments"), to_label=str)
    plugin = plugins.get("environments", executor)

    # A machine booted somewhere else per task, against running right here —
    # read off the declared capability, so the fact names no plugin.
    sandboxed = isinstance(plugin, EnvironmentPlugin) and not plugin.environment.runs_in_process

    environment = (
        dict(plugin.wizard(choose_literal=_choose_literal)) if plugin.wizard is not None else {}
    )

    settings, changes = (
        source.settings_wizard(choose_literal=_choose_literal, sandboxed=sandboxed)
        if source.settings_wizard is not None
        else ({}, True)
    )

    workspace = _workspace_questions(
        validate_folder, prompt_repo=prompt_repo, sandboxed=sandboxed, changes=changes
    )

    # ponytail: `board` is a declared `Draft` field, so it is lifted back out
    # of the source's own answer here — the one place core spells one source's
    # key in this flow. The debt: `Draft` (and `intake`'s "one agent, one
    # board" rule) wants to speak in whatever a source calls the thing a
    # connection reads.
    return Draft(
        name=name,
        board=str(identity.pop("board", "")),
        settings={
            "executor": executor,
            **identity,
            **settings,
            **workspace,
            **environment,
            "sinks": _prompt_sinks(),
        },
    )
