"""``issuebot railway ...``: the plugin's own project-wide commands.

They reach the CLI through ``plugins.mount_cli``, so these drive the real
top-level app — the same way ``issuebot git worktree`` is exercised — rather
than the plugin's Typer object in isolation. What is asserted is which Railway
calls each command makes, never a live CLI.
"""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from issuebot import cli
from issuebot.plugins.environments.railway import cli as railway_cli
from issuebot.plugins.environments.railway.environment import TEMPLATE, RailwayProvider
from issuebot.process import RecordingProcess

runner = CliRunner()


def test_railway_build_template_builds_the_shared_template(monkeypatch: pytest.MonkeyPatch):
    proc = RecordingProcess()
    monkeypatch.setattr(
        railway_cli, "RailwayProvider", lambda **kw: RailwayProvider(proc=proc, **kw)
    )

    result = runner.invoke(cli.app, ["railway", "build-template"])

    assert result.exit_code == 0, result.output

    # What the template *contains* is the provider's test; this one checks the
    # command reaches it, under the connection's own credential.
    argv = proc.calls[0]
    assert argv[:4] == ["railway", "sandbox", "template", "build"]
    assert argv[argv.index("--name") + 1] == TEMPLATE
