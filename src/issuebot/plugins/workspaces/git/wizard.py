"""The ``issuebot connect`` wizard's workspace questions.

They used to sit in the generic ``issuebot.wizard``, which spelled this
plugin's whole vocabulary — ``git_init``, ``update_base``, the folder-against-
clone choice — as core's own. The generic wizard now asks the installed
workspace plugin's own hook, exactly as it asks the environment the user
picked, so a second workspace with a strategy question of its own declares one
by writing a file like this.

The hook returns the keys a saved connection carries: this plugin's own flat
keys, plus core's ``folder``/this plugin's ``repo`` for where the working copy
comes from. Working directly is ``git_init``'s *absence*, never a value — a
present-but-None key would select this plugin for a connection with no git
strategy at all (flat plugins are selected by key presence).
"""

from __future__ import annotations

from typing import Any, get_args

from issuebot.plugins.workspaces.git.settings import Isolation, UpdateBase

# Where a connection's working copy comes from. Not a `Settings` field: the two
# answers are already spelled on a saved connection as `folder` against `repo`,
# so this exists only to ask the question.
WORKING_COPY = ("folder", "clone")

# One line per answer, shown beside it in the menu. A value on its own is not a
# question anyone can answer: "worktree" and "clone" both mean "git", and which
# of them a user wants turns on who provides the checkout — which the word does
# not say. Keyed by the prompt's own label because "none" is an answer to two
# different questions and means a different thing in each.
_HELP: dict[str, dict[str, str]] = {
    "Working copy": {
        "folder": "a git repo already on this machine — you give the path",
        "clone": "issuebot clones the repo itself, fresh for each task — you give the URL",
    },
    "Isolation": {
        "none": "work on the branch that is checked out — nothing is committed",
        "branch": "cut a task branch and commit the work to it",
        "worktree": "a separate checkout per task, on its own task branch",
    },
    "Update base": {
        "none": "leave the task branch where it was cut",
        "rebase": "replay the task branch on the latest default branch before the run",
        "merge": "merge the latest default branch into the task branch before the run",
    },
}


def _ask_update_base(choose_literal: Any) -> str:
    """The update-base menu — only ever offered when there is a task branch to
    update; working directly has none, and `validate` refuses the setting
    without a strategy."""
    return choose_literal(
        "Update base", get_args(UpdateBase), "none", help_for=_HELP["Update base"]
    )


def wizard(
    *,
    choose_literal: Any,
    prompt_repo: Any,
    prompt_folder: Any,
    sandboxed: bool,
    changes: bool,
) -> dict[str, Any]:
    """Gather this workspace's per-connection settings.

    ``sandboxed`` says the chosen environment hands each task to a fresh
    machine somewhere else. There is then no folder on this machine to work in,
    so the working copy can only be a fresh clone — and the work is cut onto a
    task branch, because a clone that committed to the checked-out default
    branch would push straight to it. Both are facts about that shape of
    environment rather than choices to offer, so neither is asked.

    ``changes`` says whether this connection's runs may report changes at all.
    A run that never edits cuts no branch, so the strategy and update-base
    questions are skipped — offering an answer this plugin's own ``validate``
    refuses at the very end of the wizard would take every other answer down
    with it.

    ``choose_literal``/``prompt_repo``/``prompt_folder`` are the generic
    wizard's own numbered picker, repo-URL loop and validated folder loop,
    handed in rather than imported, so this module stays a leaf and the
    questions look identical to every other plugin's. ``prompt_folder`` takes
    the keys gathered so far, so the as-you-type check is run against the
    workspace those keys actually select (the same rule intake applies).
    """
    if sandboxed:
        # A respond-only connection cuts no branch even sandboxed: forcing
        # `git_init` here would create and rebase a task branch whose changes
        # a run may never report — the same reason the non-sandboxed path
        # below skips the isolation question when `changes` is off.
        if not changes:
            return {"repo": prompt_repo()}

        return {
            "repo": prompt_repo(),
            "git_init": "branch",
            "update_base": _ask_update_base(choose_literal),
        }

    # Two questions, because they are two decisions. Where the working copy
    # comes from is one; what gets cut inside it is the other, and every
    # pairing of them is a connection somebody could want — including a fresh
    # clone worked in directly.
    working_copy = choose_literal(
        "Working copy", WORKING_COPY, "folder", help_for=_HELP["Working copy"]
    )

    isolation = (
        "none"
        if not changes
        else choose_literal("Isolation", get_args(Isolation), "none", help_for=_HELP["Isolation"])
    )

    settings: dict[str, Any] = {} if isolation == "none" else {"git_init": isolation}

    if working_copy == "clone":
        settings["repo"] = prompt_repo()
    else:
        settings["folder"] = prompt_folder(dict(settings))

    if isolation != "none":
        settings["update_base"] = _ask_update_base(choose_literal)

    return settings
