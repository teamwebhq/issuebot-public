"""Cross-field rules for the git workspace: how `git_init`, `repo`, `folder`,
`branch_prefix`, `update_base`, `push` and the rest of the connection interact.

Each rule below is git's to enforce; a rule that spans another plugin's key,
like the extra-key check, lives in `config.py` instead. Cross-field rules span
a plugin boundary — `folder` is the connection's, `git_init` is git's — so
`validate` receives the whole connection.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING

from issuebot import plugins
from issuebot.plugins.base import SinkPlugin

if TYPE_CHECKING:
    from issuebot.config import Connection


def _strategy_problems(
    *, git_init: str | None, repo: str | None, folder: str | None, extra: dict
) -> Iterable[str]:
    """How `git_init`, `repo`, `folder`, `branch_prefix` and `update_base`
    contradict each other — the rejection table's git-only rows.

    Two independent settings, so two independent groups of rule. The first says
    the working copy comes from exactly one place; the second says a setting
    about a task branch needs there to be a task branch. Nothing here relates
    the two: every combination of them is a connection somebody could want,
    including a fresh clone worked in directly.
    """
    # Where the working copy comes from: a clone URL, or a folder. Not both —
    # the two answer the same question, and a connection that gives both has
    # not said which one to believe.
    if repo and folder is not None:
        yield "both 'repo' and 'folder': a clone or a local folder, not both"

    if not repo and folder is None:
        yield "neither 'folder' nor 'repo': nowhere to work"

    # What is cut inside it. Both of these describe a task branch, so both need
    # a `git_init` that cuts one.
    if "branch_prefix" in extra and git_init is None:
        yield "'branch_prefix' with no git_init: working directly cuts no branch"

    if "update_base" in extra and git_init is None:
        yield "'update_base' with no git_init: working directly cuts no branch to update"


def _sinks_needing_a_push(conn: Connection) -> Iterable[str]:
    """The names of this connection's sinks that publish from a pushed branch.

    Asks each sink rather than knowing any of them: a sink declares
    :attr:`~issuebot.plugins.sinks.base.Sink.needs_pushed_branch` and this reads
    it off the registered class, so git's rule holds for every such sink and
    names none of them. Read off the class, never an instance — validation runs
    at load, long before anything is built, and a sink's constructor may want a
    harness this has no way to supply.

    A sink the registry cannot resolve is skipped: `_named_plugin_problems`
    already reports that connection's unknown sink by name, and a second
    sentence about it here would be noise.
    """
    for ref in conn.sinks:
        try:
            plugin = plugins.get("sinks", ref.name)
        except plugins.UnknownPlugin:
            continue

        if isinstance(plugin, SinkPlugin) and plugin.sink.needs_pushed_branch:
            yield ref.name


def validate(conn: Connection) -> Iterable[str]:
    """Every way this connection's git settings contradict `git_init`, `repo`,
    `folder`, or the rest of the connection (`sinks`).

    Nothing here reads `mode`: workspace strategy decides where work happens,
    `permits` decides what may come back, and write permission derives from
    neither (ADR-0011) — a rule tying `mode` to a strategy would couple the
    two.
    """
    extra = conn.model_extra or {}
    git_init = extra.get("git_init")
    repo = extra.get("repo")

    yield from _strategy_problems(git_init=git_init, repo=repo, folder=conn.folder, extra=extra)

    # "a sink requiring a pushed branch while git's push=false" — the sinks say
    # which of them those are, git only knows it is the half that never pushes.
    if extra.get("push", True) is False:
        for name in _sinks_needing_a_push(conn):
            yield f"the '{name}' sink needs 'push' enabled — it publishes from a pushed branch"
