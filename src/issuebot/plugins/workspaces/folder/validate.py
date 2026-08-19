"""Cross-field rules for the folder workspace."""

from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from issuebot.config import Connection


def validate(conn: Connection) -> Iterable[str]:
    """`folder_init='copy'` has nothing to copy without a `folder`."""
    extra = conn.model_extra or {}
    if extra.get("folder_init") == "copy" and conn.folder is None:
        yield "folder_init='copy' requires 'folder' — nothing to copy"
