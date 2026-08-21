"""``issuebot doctor``'s railway checks: prerequisites for running sandboxes.

Moved off ``cli.py``, where the top-level command spelled out Railway's CLI
name, both of its token environment variables and its plan limits. The CLI now
asks whichever environment a connection selects for its own doctor hook — the
same shape it already uses for sinks — so a second environment adds checks by
writing this file, not by editing ``doctor``.
"""

from __future__ import annotations

import shutil
from collections.abc import Callable
from typing import TYPE_CHECKING

from issuebot.plugins.environments.railway import settings as railway_settings

if TYPE_CHECKING:
    from issuebot.config import Connection


def doctor(conn: Connection, *, echo: Callable[[str], None]) -> None:
    """Warn about anything missing for a connection that runs on Railway: the
    ``railway`` CLI on PATH, a usable credential, and a configured environment
    id. Also notes that Railway's Free-plan 5-minute idle cap makes it unsafe
    for long-running tasks."""
    railway = railway_settings.for_connection(conn)

    # The executable this connection actually runs, which a connection may point
    # at an absolute path — a bare name is what PATH must answer for.
    command = railway.command if railway else railway_settings.DEFAULT_COMMAND

    if shutil.which(command) is None:
        echo(f"Warning: '{command}' CLI not found on PATH (required for executor=railway)")

    if not (railway and railway.token) and not railway_settings.ambient_token():
        variables = " nor ".join(railway_settings.TOKEN_VARS.values())
        echo(
            f"Warning: connection '{conn.name}' has no railway token and neither "
            f"{variables} is set (needed to create sandboxes)"
        )

    if railway is None:
        echo(f"Warning: connection '{conn.name}' has no [railway] settings block")

    echo(
        f"Note: connection '{conn.name}' uses executor=railway — the Free plan's "
        "5-minute idle cap is unsafe for long-running tasks; use a paid plan."
    )
