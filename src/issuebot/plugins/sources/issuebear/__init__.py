"""The Issuebear board.

`source` names the `Issuebear` implementation of the `Source` ABC.
`done`/`mode` carry their real `Literal` types on `Settings` (see
`settings.py`); `issuebot connect` writes them on every connection.
`settings_wizard` asks for them interactively, so their vocabulary stays here.
"""

from __future__ import annotations

from issuebot.plugins.base import SourcePlugin
from issuebot.plugins.sources.issuebear import wizard
from issuebot.plugins.sources.issuebear.settings import GlobalSettings, Settings
from issuebot.plugins.sources.issuebear.source import Issuebear

PLUGIN = SourcePlugin(
    name="issuebear",
    source=Issuebear,
    settings=Settings,
    flat=True,
    global_settings=GlobalSettings,
    setup=wizard.setup,
    wizard=wizard.connection,
    settings_wizard=wizard.settings,
)
