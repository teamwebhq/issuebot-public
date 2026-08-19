"""Doctor checks for the GitHub sink: can it actually open a PR.

A connection asks for a PR by listing ``github`` among its ``sinks``
(ADR-0012), so this only runs for a connection that actually uses it.
"""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Callable
from typing import TYPE_CHECKING

# The module, not the name: the package binds `doctor` to this file's function,
# so `github.doctor` is not reachable as a module and a test cannot script an
# `origin` imported by value here.
from issuebot.plugins.sinks.github import sink
from issuebot.process import REAL

if TYPE_CHECKING:
    from issuebot.config import Connection

Echo = Callable[[str], None]


def doctor(conn: Connection, *, echo: Echo = print) -> None:
    """Warn about anything that would stop this connection's GitHub sink from
    opening a PR: no ``gh`` on PATH, no authenticated ``gh`` session, or (for
    a connection with its own local folder) no ``origin`` remote to open one
    against.

    The origin-remote check only applies to a connection that keeps a folder of
    its own — a clone-based connection's workspace does not exist yet at doctor
    time, so there is nothing on disk to check. ``conn.folder`` alone says that:
    a connection that clones stores none (``folder`` alongside ``repo`` is a
    load-time error, see the git plugin's ``validate``), so this reads no git
    workspace key to work it out.
    """
    if shutil.which("gh") is None:
        echo(f"Warning: connection '{conn.name}' needs 'gh' on PATH for PRs.")
        return

    if subprocess.run(["gh", "auth", "status"], capture_output=True).returncode != 0:
        echo(f"Warning: 'gh' is not authenticated (connection '{conn.name}').")

    if conn.folder and not sink.origin(REAL, conn.folder):
        echo(f"Warning: connection '{conn.name}' has no 'origin' remote.")
