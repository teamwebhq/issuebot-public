"""Per-connection and global settings for the Issuebear source plugin.

``DoneMode``/``Mode``/``ConfirmChoice`` live here, on the plugin that owns the
settings they describe — core imports none of them. ``connect``'s dedicated
``--done``/``--mode``/``--confirm`` flags re-spell the same choices as flag
vocabulary (see ``intake.FLAG_OWNED``).
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

# What happens to a task the agent hands back. `Settings.done` carries it; the
# wizard hook offers its values as a menu.
DoneMode = Literal["review", "complete"]

# Whether the agent may edit the workspace at all. `Settings.mode` carries it.
Mode = Literal["build", "respond"]

# CLI and wizard vocabulary for `Settings.confirm`, which is a bool. Spelled
# as a value rather than a `--confirm/--no-confirm` flag pair so it reads like
# every other setting on `connect` — and because the flag pair costs `--help`
# a whole column, which truncates the `--set` plugin docs at 80 columns.
ConfirmChoice = Literal["yes", "no"]


class GlobalSettings(BaseModel):
    """`[issuebear]`: how to reach the board and identify this install."""

    api_url: str
    mcp_url: str
    pat: str
    install_name: str | None = None
    telemetry_interval_seconds: int = 15


class Settings(BaseModel):
    """Per-connection: which board this connection works, and how."""

    board: str

    # What happens to a task the agent hands back: "review" or "complete".
    done: DoneMode = "review"

    # Whether a human signs the plan off before the agent writes any code.
    # This is the only real choice about how the agent approaches the work: it
    # always plans (`set_plan`) and always raises genuine ambiguity
    # (`ask_questions`) whatever this says. Off, it plans and gets on with it.
    confirm: bool = True

    # Whether the agent may edit the workspace at all: "build" or "respond".
    mode: Mode = "build"
