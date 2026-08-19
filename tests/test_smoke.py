from typer.testing import CliRunner

from issuebot.cli import app


def test_version_command():
    result = CliRunner().invoke(app, ["version"])
    assert result.exit_code == 0
    assert result.stdout.strip()


def test_help_shows_usage():
    """Bare `issuebot` prints its help rather than doing anything.

    Only "Usage" is asserted, deliberately: the help text names whatever
    plugins are installed, so matching any of them would make this smoke test
    fail the day one is removed — which is exactly the thing it must survive."""
    result = CliRunner().invoke(app, [])
    assert "Usage" in result.stdout
