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

# What this connection does with the board's work. `Settings.mode` carries it.
# "board" defers to the item: the column the task sits in says what the run is
# for, and this connection does that. The other two are overrides — they ignore
# what the column asked for and always build, or always respond.
Mode = Literal["board", "build", "respond"]

# The board and this runner name the same two things differently, and the
# crossing belongs here, on the plugin that talks to that board — core knows
# neither vocabulary. An item's `mode` arrives in the board's words and is read
# through `BOARD_MODES`; a claim and a telemetry entry report in the board's
# words and are written through `WIRE_MODES`.
BOARD_MODES: dict[str, str] = {"edit_code": "build", "research": "respond"}
WIRE_MODES: dict[str, str] = {"build": "edit_code", "respond": "research"}

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

    # What this connection does with the board's work: "board" to do what the
    # item's column asks for, or "build"/"respond" to override it and always do
    # the one thing. An item whose column asks for nothing builds, which is what
    # every connection did before a column could ask for anything.
    mode: Mode = "board"
