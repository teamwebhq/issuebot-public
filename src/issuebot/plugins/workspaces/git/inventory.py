"""Listing and reclaiming the workspaces issuebot has cut — the housekeeping half.

Everything here answers "what is lying around, and what can go?". Cutting a
workspace and integrating the work in it is :mod:`issuebot.plugins.workspaces.
git.workspace`.

Worktrees and clones are one thing, :class:`Cut`, that knows which strategy
produced it; that is the only respect in which they differ, and only when
removing one.
"""

from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from issuebot.config import Connection
from issuebot.plugins.workspaces.git.workspace import (
    Git,
    is_merged,
    pr_merged,
    resolve_clone_root,
    resolve_worktree_root,
    source_repo_folder,
)
from issuebot.process import REAL, Process

# Re-exported for this module's own callers (the CLI's prune path): both live
# in `workspace` alongside `Git`, so `resolve_branch`'s own merge check can
# use them without this module importing back from there.
__all__ = ["Cut", "is_merged", "list_clones", "list_worktrees", "pr_merged", "remove"]

logger = logging.getLogger("issuebot")

WorkspaceKind = Literal["worktree", "clone"]


@dataclass(frozen=True)
class Cut:
    """One workspace issuebot cut for a task, and whether it is safe to remove."""

    kind: WorkspaceKind
    project: str
    ref: str
    branch: str
    path: str

    # Uncommitted changes, and commits that removing it would lose. Either one
    # makes removal destructive, so both are read up front rather than at the
    # point of deletion.
    dirty: bool
    unpushed: bool

    @property
    def safe_to_remove(self) -> bool:
        """True when nothing would be lost by deleting this workspace."""
        return not self.dirty and not self.unpushed


def _describe(g: Git, kind: WorkspaceKind, project: str, ref: str, branch: str) -> Cut:
    """Read the removable-ness of a workspace we have already located."""
    return Cut(
        kind=kind,
        project=project,
        ref=ref,
        branch=branch,
        path=g.folder,
        dirty=g.is_dirty(),
        unpushed=g.unpushed(),
    )


def list_worktrees(
    project: Connection,
    *,
    worktree_root: str | None,
    clone_root: str | None = None,
    proc: Process = REAL,
) -> list[Cut]:
    """The issuebot-managed worktrees for this project.

    Every git worktree whose path lives under the project's worktree root —
    a worktree the user added themselves is none of our business.

    Asked of the repository the worktrees were cut from: the connection's own
    folder, or — for a connection that clones — its shared clone, which is why
    this needs ``clone_root`` too. No shared clone yet means no worktrees."""
    root = resolve_worktree_root(worktree_root) / project.key
    source = source_repo_folder(project, clone_root)
    if not Path(source).is_dir():
        return []
    g = Git(source, proc)

    found: list[Cut] = []
    for block in g.git("worktree", "list", "--porcelain").out.split("\n\n"):
        path = branch = None
        for line in block.splitlines():
            if line.startswith("worktree "):
                path = line.removeprefix("worktree ")
            elif line.startswith("branch "):
                branch = line.removeprefix("branch ").removeprefix("refs/heads/")

        if not path or not Path(path).is_relative_to(root):
            continue

        found.append(_describe(g.at(path), "worktree", project.key, Path(path).name, branch or ""))
    return found


def list_clones(project: Connection, *, clone_root: str | None, proc: Process = REAL) -> list[Cut]:
    """The issuebot-managed clones for this project: each git repo directly under
    the project's clone root.

    Dot-directories are skipped: the worktree strategy's shared clone lives at
    ``.shared`` and is infrastructure, not a task's workspace — pruning it out
    from under the worktrees cut from it would break every one of them."""
    root = resolve_clone_root(clone_root) / project.key
    if not root.is_dir():
        return []

    found: list[Cut] = []
    for child in sorted(root.iterdir()):
        if not (child / ".git").exists() or child.name.startswith("."):
            continue
        # Each clone is its own repository, so every query runs inside it. A
        # clone connection has no project folder at all to run them from.
        here = Git(child, proc)
        branch = here.git("rev-parse", "--abbrev-ref", "HEAD").out.strip()
        found.append(_describe(here, "clone", project.key, child.name, branch))
    return found


def remove(
    workspace: Cut,
    *,
    project_folder: str | None = None,
    force: bool,
    proc: Process = REAL,
) -> bool:
    """Remove one workspace, returning True when it is gone.

    Refuses a workspace with anything to lose unless ``force``. The two kinds
    differ only here: a worktree is git's to remove and unregister — which has to
    be asked of the repo it belongs to, hence ``project_folder`` — while a clone
    is a standalone repository and just a directory to delete."""
    if not force and not workspace.safe_to_remove:
        return False

    if workspace.kind == "clone":
        shutil.rmtree(workspace.path, ignore_errors=True)
        return not Path(workspace.path).exists()

    if project_folder is None:
        raise ValueError("removing a worktree needs the folder of the repo it belongs to")

    g = Git(project_folder, proc)
    args = ["worktree", "remove", workspace.path]
    if force:
        args.append("--force")
    ok = g.git(*args).ok

    # Drop the administrative entry even when removal failed, so a worktree whose
    # directory has already gone by other means stops being listed.
    g.git("worktree", "prune")
    return ok
