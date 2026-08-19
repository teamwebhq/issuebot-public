"""Working from a git clone, worktree, or branch in place.

`workspace` names the `Workspace` implementation, `validate` enforces the
cross-field rules `git_init`/`repo`/`folder`/`branch_prefix`/`update_base` owe
each other, `wizard` asks for those settings in the connect wizard, and `cli`
mounts `worktree`/`clone` under ``issuebot git``.

`git_init` is the one name for the strategy, and working in place is its
*absence*, not a value. There is no `integrate` setting: it dissolved into
`changes` being permitted, git's own `push` setting, and a connection's
`sinks` list (ADR-0012).
"""

from __future__ import annotations

from issuebot.plugins.base import WorkspacePlugin
from issuebot.plugins.workspaces.git.cli import app as git_cli
from issuebot.plugins.workspaces.git.doctor import doctor as git_doctor
from issuebot.plugins.workspaces.git.settings import GlobalSettings, Settings
from issuebot.plugins.workspaces.git.validate import validate
from issuebot.plugins.workspaces.git.wizard import wizard as git_wizard
from issuebot.plugins.workspaces.git.workspace import GitWorkspace

PLUGIN = WorkspacePlugin(
    name="git",
    workspace=GitWorkspace,
    settings=Settings,
    global_settings=GlobalSettings,
    flat=True,
    validate=validate,
    cli=git_cli,
    doctor=git_doctor,
    wizard=git_wizard,
)
