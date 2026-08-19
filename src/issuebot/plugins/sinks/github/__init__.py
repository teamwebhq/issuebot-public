"""The GitHub sink: opens (or reuses) a PR from a pushed branch.

`sink` names the `GitHubSink` implementation of the `Sink` ABC.
"""

from __future__ import annotations

from issuebot.plugins.base import SinkPlugin
from issuebot.plugins.sinks.github.doctor import doctor
from issuebot.plugins.sinks.github.settings import GlobalSettings
from issuebot.plugins.sinks.github.sink import GitHubSink

PLUGIN = SinkPlugin(
    name="github",
    sink=GitHubSink,
    flat=False,
    global_settings=GlobalSettings,
    doctor=doctor,
)
