"""What core's `issuebot init` asks, and what it writes down.

The harness path question is the one that has to survive the runner being
started as a service: such a process gets a minimal PATH, so a config that
names the harness by bare name alone is a config that only works for the
person who typed it, in their login shell.
"""

from __future__ import annotations

import typer
from typer.testing import CliRunner

from issuebot import wizard

runner = CliRunner()


class _Source:
    """A source plugin with nothing of its own to ask."""

    name = "fake_source"

    def setup(self) -> dict:
        return {}


def _init(monkeypatch, *, found: str | None, keys: str) -> dict:
    """Run `wizard.setup()` under a scripted stdin, returning the config dict.

    `which` is stubbed rather than the PATH, so the test says the same thing on
    a machine that happens to have a real `claude` as one that does not.
    """
    asked: list[str] = []

    def which(name: str) -> str | None:
        asked.append(name)
        return found

    monkeypatch.setattr(wizard, "source_plugin", _Source)
    monkeypatch.setattr(wizard.plugins, "offered", lambda kind: ["claude"])
    monkeypatch.setattr(wizard.shutil, "which", which)

    gathered: dict = {}
    app = typer.Typer()
    app.command()(lambda: gathered.update(wizard.setup().model_dump()))

    result = runner.invoke(app, [], input=keys)
    assert result.exit_code == 0, result.output

    # The harness plugin's name is the executable name we look for.
    assert asked == ["claude"]
    return gathered


def test_pressing_enter_stores_the_resolved_harness_path(monkeypatch) -> None:
    """A service gets PATH=/usr/bin:/bin and cannot find a bare `claude`, so the
    wizard resolves it now and offers the absolute path as the answer."""
    config = _init(monkeypatch, found="/opt/homebrew/bin/claude", keys="\n")

    assert config["claude"] == {"command": "/opt/homebrew/bin/claude"}


def test_a_typed_path_still_wins_over_the_resolved_one(monkeypatch) -> None:
    """The offered path is a default, not a verdict — the user may be writing a
    config for another machine."""
    config = _init(monkeypatch, found="/opt/homebrew/bin/claude", keys="/srv/bin/claude\n")

    assert config["claude"] == {"command": "/srv/bin/claude"}


def test_an_unfound_harness_leaves_the_answer_blank(monkeypatch) -> None:
    """Nothing on PATH here is not a reason to fail: a config written on one box
    for another must still be possible, and blank means "resolve at run time"."""
    config = _init(monkeypatch, found=None, keys="\n")

    assert "claude" not in config
    assert config["harness"] == "claude"
