"""Per-connection and global settings for the git workspace plugin.

Two independent choices. Where the working copy **comes from** is `repo`
against `folder`: a clone URL means a fresh clone per task, a folder means the
repository already on this machine. What happens **inside** it is `git_init`:
cut a task branch, cut a worktree, or neither. Keeping them separate is what
makes "a clone worked in directly" writable at all; the retired
`git_init="clone"` value is rejected with the two-setting translation
(:meth:`Settings._split_out_clone`).
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, field_validator

# How a task branch is brought up to date with its base. `Settings.update_base`
# carries it; the wizard hook offers its values as a menu.
UpdateBase = Literal["none", "rebase", "merge"]

# CLI and wizard vocabulary for the strategy choice: `git_init`'s two values
# plus "none" for the key's *absence* (working directly cuts no branch). A
# saved connection never carries "none" — `connect` and the wizard hook
# translate it to leaving `git_init` unset.
Isolation = Literal["none", "branch", "worktree"]


class Settings(BaseModel):
    """Per-connection: where and how the agent's working copy is prepared.

    ``git_init=None`` is working directly in the copy — not a third strategy
    value, but the field's *absence*. That is what makes `branch_prefix` with
    no `git_init` unrepresentable rather than merely rejected by `validate`.
    """

    # The clone URL, for a connection whose working copy is a fresh clone per
    # task rather than a folder already on this machine. One or the other, and
    # `validate` refuses both.
    repo: str | None = None

    # What is cut inside the working copy: a task branch, a worktree beside it,
    # or — absent — neither, working on whatever branch is checked out.
    git_init: Literal["worktree", "branch"] | None = None

    # Prefix for branches this connection cuts. Only meaningful with a
    # `git_init` strategy — working directly cuts no branch.
    branch_prefix: str = "issuebot/"

    # How a branch is brought up to date with its base: "none", "rebase",
    # "merge". Only meaningful with a `git_init` strategy, same as above.
    update_base: UpdateBase = "none"

    # Whether `commit_and_push` pushes at all, once there is a strategy and a
    # remote. False keeps the work local; `Changes.pushed` still reports
    # what happened either way.
    push: bool = True

    @field_validator("git_init", mode="before")
    @classmethod
    def _split_out_clone(cls, value: Any) -> Any:
        """Name the two settings that replaced ``git_init="clone"``.

        Pydantic's own message for a retired literal ("Input should be
        'worktree' or 'branch'") is true and useless: it tells the user their
        value is gone without telling them that the concept has moved to the
        setting directly above it. This one config error is worth a sentence
        because the answer is not "pick another value", it is "you now say this
        with two keys".
        """
        if value == "clone":
            raise ValueError(
                "git_init='clone' is now two settings: 'repo' says the working copy is a "
                "fresh clone per task, and git_init says what to cut inside it — set "
                'repo = "<url>" and git_init = "branch" for what \'clone\' used to mean, '
                "or drop git_init to work directly in the clone"
            )
        return value


class GlobalSettings(BaseModel):
    """`[git]`: where cut worktrees and clones live on disk, across every
    connection using this plugin. None for either means the XDG state dir
    default (see `resolve_worktree_root`/`resolve_clone_root`)."""

    worktree_root: str | None = None
    clone_root: str | None = None
