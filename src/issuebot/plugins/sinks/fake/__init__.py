"""A scripted sink for tests and dry runs."""

from __future__ import annotations

from issuebot.plugins.base import SinkPlugin
from issuebot.plugins.sinks.fake.doctor import doctor
from issuebot.plugins.sinks.fake.sink import FakeSink

# `hidden`: never offered by the wizard or named in `--help`. A connection can
# still wire it up deliberately — and `doctor` says so when one does.
PLUGIN = SinkPlugin(name="fake", sink=FakeSink, doctor=doctor, hidden=True)
