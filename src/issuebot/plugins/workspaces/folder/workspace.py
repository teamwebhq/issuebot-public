"""Working in-place in an existing folder, no git strategy at all.

`FolderWorkspace` is deliberately the simple case: it never derives `Changes`
— that needs a git history to diff against, and a plain folder has none — so
`produces` excludes `changes`, and `commit_and_push` is unreachable (the
config check that intersects `produces` with a connection's permits sees to
that).
"""

from __future__ import annotations

import shutil
from typing import TYPE_CHECKING

from issuebot.plugins.workspaces.base import Prepared, Workspace
from issuebot.plugins.workspaces.folder.settings import Settings
from issuebot.process import REAL
from issuebot.state import private_dir, state_dir

if TYPE_CHECKING:
    from pydantic import BaseModel

    from issuebot.config import Connection
    from issuebot.contracts import Changes
    from issuebot.process import Process


class FolderWorkspace(Workspace):
    """A plain folder, worked in directly or (`folder_init="copy"`) copied to
    a throwaway location per task. No git, so no `changes` — read-only work
    only, matching `produces` below."""

    name = "folder"
    produces = frozenset({"answer", "needs_input", "handoff"})

    def __init__(self, **_ignored: object) -> None:
        """Accepts the fixed keyword shape every workspace is built with
        (``runner.workspace_for`` passes git's global roots) and needs none of
        it: a plain folder has no worktree or clone directory to place."""

    def prepare(
        self, connection: Connection, ref: str, *, settings: BaseModel, proc: Process = REAL
    ) -> Prepared:
        """Work directly in `connection.folder`, or a throwaway copy of it."""
        assert isinstance(settings, Settings)
        if settings.folder_init is None:
            return Prepared(folder=connection.local_folder)

        # ponytail: a plain recursive copy, skipping .git — good enough for a
        # folder with no git strategy of its own; nothing here needs to be
        # incremental the way git's worktree/clone reuse is.
        dest = state_dir() / "folders" / connection.key / ref.replace("/", "-")
        private_dir(dest.parent)
        if dest.exists():
            shutil.rmtree(dest)
        shutil.copytree(connection.local_folder, dest, ignore=shutil.ignore_patterns(".git"))
        return Prepared(folder=str(dest))

    def commit_and_push(
        self, prepared: Prepared, message: str, *, settings: BaseModel, proc: Process = REAL
    ) -> Changes:
        """Unreachable: `produces` excludes `changes`, and the config check
        that intersects it with a connection's permits enforces that no run
        in this workspace is ever asked to derive one."""
        raise NotImplementedError("the folder workspace never produces changes")
