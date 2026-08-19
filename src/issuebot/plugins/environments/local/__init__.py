"""Running tasks directly on this machine, no sandbox.

No settings of its own: running here needs no configuration beyond the
connection's own workspace and harness, so there is no `[connections.local]`
table to own. That is the honest answer, not an omission — a plugin declaring
an empty settings model would claim a config key nobody can usefully set.
"""

from __future__ import annotations

from issuebot.plugins.base import EnvironmentPlugin
from issuebot.plugins.environments.local.environment import LocalEnvironment

PLUGIN = EnvironmentPlugin(name="local", environment=LocalEnvironment)
