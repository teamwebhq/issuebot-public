"""Working in-place in an existing folder, no git strategy at all.

`workspace` names the `FolderWorkspace` implementation and `validate` its one
cross-field rule. `produces` excludes `changes` (see `workspace.py`), so a
connection asking a folder workspace for `changes` is rejected at config load.
"""

from __future__ import annotations

from issuebot.plugins.base import WorkspacePlugin
from issuebot.plugins.workspaces.folder.settings import Settings
from issuebot.plugins.workspaces.folder.validate import validate
from issuebot.plugins.workspaces.folder.workspace import FolderWorkspace

PLUGIN = WorkspacePlugin(
    name="folder",
    workspace=FolderWorkspace,
    settings=Settings,
    flat=True,
    validate=validate,
)
