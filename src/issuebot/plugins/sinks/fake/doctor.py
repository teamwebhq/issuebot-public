"""The one thing worth saying about a connection that publishes nowhere."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from issuebot.config import Connection

Echo = Callable[[str], None]


def doctor(conn: Connection, *, echo: Echo = print) -> None:
    """Warn that this connection's results go nowhere.

    Unconditional, because there is no configuration under which this sink
    publishes anything: it records what it is handed and discards it. A
    connection that wires it up outside a test has almost certainly done so by
    accident, and `doctor` is where that gets noticed.
    """
    echo(
        f"Warning: connection '{conn.name}' uses the 'fake' sink, which records "
        f"results and discards them — nothing is published."
    )
