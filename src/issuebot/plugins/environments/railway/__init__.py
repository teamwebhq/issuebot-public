"""Running tasks in Railway sandboxes.

`environment` names the `RailwayEnvironment` implementation, and
`cli`/`doctor`/`wizard` bring Railway's own commands, health checks and setup
questions inside the folder they belong to.

`settings` is the model that owns `[connections.railway]`. Nothing outside
this folder spells `RAILWAY_TOKEN`, `${{shared.NAME}}` or the sandbox
template's name (ADR-0002).
"""

from __future__ import annotations

from issuebot.plugins.base import EnvironmentPlugin
from issuebot.plugins.environments.railway.cli import app as railway_cli
from issuebot.plugins.environments.railway.doctor import doctor as railway_doctor
from issuebot.plugins.environments.railway.environment import RailwayEnvironment
from issuebot.plugins.environments.railway.settings import RailwaySettings
from issuebot.plugins.environments.railway.wizard import wizard as railway_wizard

# Each hook is aliased on the way in so it does not shadow the submodule of the
# same name on this package — `railway.wizard` stays the module, which is what
# lets anything (a test, a later CLI-mounting task) reach inside it.
PLUGIN = EnvironmentPlugin(
    name="railway",
    environment=RailwayEnvironment,
    settings=RailwaySettings,
    flat=False,
    cli=railway_cli,
    doctor=railway_doctor,
    wizard=railway_wizard,
)
