"""Per-connection settings for the folder workspace plugin."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel


class Settings(BaseModel):
    """Per-connection: how (if at all) the folder is copied before a run.

    None (the default) is in-place: the agent runs directly in the
    connection's `folder`. `"copy"` cuts a throwaway copy per task instead —
    the folder equivalent of git's worktree, for a repo (or plain directory)
    with no git strategy at all.
    """

    folder_init: Literal["copy"] | None = None
