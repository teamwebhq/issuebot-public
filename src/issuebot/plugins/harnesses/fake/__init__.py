"""A scripted harness for tests and dry runs."""

from __future__ import annotations

from issuebot.plugins.base import HarnessPlugin
from issuebot.plugins.harnesses.fake.harness import FakeHarness

# `hidden`: a scripted harness is something a config wires up on purpose, never
# something `issuebot init` should offer as one of the answers.
PLUGIN = HarnessPlugin(name="fake", harness=FakeHarness, hidden=True)
