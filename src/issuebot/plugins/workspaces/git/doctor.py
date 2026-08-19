"""``issuebot doctor``'s git checks: can this connection's working copy be made.

Moved off ``cli.py``, where the top-level command ran ``git ls-remote`` and
asked whether a folder was a worktree — one workspace strategy's prerequisites,
spelled in the generic command. ``doctor`` now asks every plugin a connection
resolves to for its own checks, so these live where the strategy does.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from typing import TYPE_CHECKING

from issuebot.config import conn_setting
from issuebot.plugins.workspaces.git.workspace import is_git_worktree

if TYPE_CHECKING:
    from issuebot.config import Connection

Echo = Callable[[str], None]


def doctor(conn: Connection, *, echo: Echo = print) -> None:
    """Warn when this connection's working copy could not be prepared.

    Checked against where the copy comes from, which is the question this can
    actually answer. A connection that clones has nothing on disk yet, so the
    only thing to check is that its URL answers — ``git ls-remote`` is the
    cheapest call that proves both reachability and access. One that works from
    a folder needs that folder to be a git repository, whatever it cuts inside
    it: even working directly, this plugin reads the branch and the head sha.
    """
    repo = conn_setting(conn, "repo")

    if repo:
        if subprocess.run(["git", "ls-remote", repo], capture_output=True).returncode != 0:
            echo(f"Warning: connection '{conn.name}' repo is unreachable: {repo}")
        return

    if not is_git_worktree(conn.local_folder):
        echo(f"Warning: connection '{conn.name}' folder is not a git repo.")
