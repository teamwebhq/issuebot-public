"""Wiring a connection to this environment, end to end through the real CLI.

The plugin's own hooks are covered in isolation next door (``test_wizard.py``
for its questions, ``test_cli.py`` for its commands). What is left is the claim
those cannot make on their own: that ``issuebot connect`` — a generic command
that names no environment — really does produce a working railway connection,
whether the user scripted it with ``--set`` or walked the wizard, and that
``issuebot doctor`` then checks it.

These used to live in ``tests/test_cli.py``, where they were core's only
knowledge of this plugin. They are here because deleting the plugin should mean
deleting two directories, and a core test file importing
``plugins.environments.railway.settings`` made that false.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from conftest import StubClient, WizardStubClient, cli_runner, config, ok_client, sink_answers
from issuebot import cli, plugins
from issuebot.config import Connection, load_config, save_config
from issuebot.plugins.environments.railway.doctor import doctor as railway_doctor
from issuebot.plugins.environments.railway.settings import for_connection as railway_settings

runner = CliRunner()


@pytest.fixture
def config_with_railway_connection(tmp_path: Path) -> Path:
    """A saved config with a single executor='railway' connection, for doctor."""
    path = tmp_path / "config.toml"
    cfg = config()
    cfg.connections = [
        Connection(
            name="p",
            board="b",
            repo="https://example.com/x/y.git",
            git_init="branch",
            executor="railway",
            railway={"environment_id": "env-123"},
        )
    ]
    save_config(cfg, path)
    return path


# ---------------------------------------------------------------------------
# Scripted: --set
# ---------------------------------------------------------------------------


def test_set_persists_this_environments_own_settings(config_path: Path):
    """An environment's settings reach a scripted connect through the generic
    `--set <plugin>.<key>=<value>`, defaulting the rest of its model."""
    save_config(config(), config_path)
    runner = cli_runner(StubClient())

    result = runner.invoke(
        cli.app,
        [
            "connect",
            "--name", "web",
            "--board", "b-1",
            "--repo", "git@example.com:o/r.git",
            "--isolation", "branch",
            "--executor", "railway",
            "--set", "railway.environment_id=env1",
        ],
    )  # fmt: skip
    assert result.exit_code == 0, result.output

    cfg = load_config(config_path)
    assert cfg is not None
    conn = cfg.connection("web")
    assert conn is not None
    assert conn.executor == "railway"

    settings = railway_settings(conn)
    assert settings is not None
    assert settings.environment_id == "env1"
    assert settings.network == "isolated"
    assert conn.git_init == "branch"
    assert conn.repo == "git@example.com:o/r.git"


def test_set_persists_a_per_connection_token(config_path: Path):
    """A project token only reaches one project, so the credential is per
    connection rather than one process-wide env var."""
    save_config(config(), config_path)
    runner = cli_runner(StubClient())

    result = runner.invoke(
        cli.app,
        [
            "connect",
            "--name", "rw",
            "--board", "b9",
            "--repo", "https://example.com/r.git",
            "--isolation", "branch",
            "--executor", "railway",
            "--set", "railway.environment_id=env-9",
            "--set", "railway.token=tok-b",
            "--set", "railway.token_kind=account",
        ],
    )  # fmt: skip
    assert result.exit_code == 0, result.output

    cfg = load_config(config_path)
    assert cfg is not None
    conn = cfg.connection("rw")
    assert conn is not None

    settings = railway_settings(conn)
    assert settings is not None
    assert settings.token == "tok-b"
    assert settings.token_kind == "account"


@pytest.mark.parametrize(
    ("assignment", "expected"),
    [
        ("railway.envrionment_id=x", "environment_id"),
        ("railway.network=publik", "isolated"),
    ],
)
def test_a_typo_in_this_plugins_setting_is_refused_by_name(
    config_path: Path, tmp_path: Path, assignment: str, expected: str
):
    """A typo in a key or a value is refused against this plugin's own settings
    model, so the message is in the plugin's words rather than a generic one."""
    save_config(config(), config_path)
    folder = tmp_path / "work"
    folder.mkdir()

    result = runner.invoke(
        cli.app,
        ["connect", "--name", "web", "--board", "b-1", "--folder", str(folder),
         "--set", assignment],
    )  # fmt: skip

    assert result.exit_code == 1
    assert expected in result.output


def test_settings_for_an_environment_the_connection_does_not_use_are_refused(
    config_path: Path, tmp_path: Path
):
    """Settings for an environment the connection doesn't run in would be saved
    and then refuse to load; they are refused up front instead.

    The connection must name *another* environment for that to be what is under
    test. Naming none is a different error — "you have not said where this runs"
    — and a version of this test that let it stand passed on the wrong sentence.
    """
    save_config(config(), config_path)
    runner = cli_runner(StubClient())
    folder = tmp_path / "work"
    folder.mkdir()

    elsewhere = next(name for name in plugins.names_of("environments") if name != "railway")

    result = runner.invoke(
        cli.app,
        ["connect", "--name", "web", "--board", "b-1", "--folder", str(folder),
         "--executor", elsewhere, "--set", "railway.environment_id=env1"],
    )  # fmt: skip

    assert result.exit_code == 1
    assert "does not use railway" in result.output


def test_this_plugins_settings_are_documented_by_connect_help(config_path: Path):
    """`--set` documents itself from the registry, so an installed plugin's
    settings appear in `connect --help` without `cli.py` naming it — and the
    plugin gets no flags of its own on a generic command."""
    result = runner.invoke(cli.app, ["connect", "--help"])

    assert result.exit_code == 0
    assert "railway.environment_id" in result.output
    assert "--railway" not in result.output


# ---------------------------------------------------------------------------
# Interactive: the wizard
# ---------------------------------------------------------------------------


def test_the_wizard_builds_a_railway_connection(config_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Choosing 'railway' at the executor prompt skips isolation/folder/mode and
    instead asks for a repo, an environment id and the network mode, forcing
    a cloned working copy, a task branch and mode=build."""
    save_config(config(), config_path)
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/railway")
    monkeypatch.setenv("RAILWAY_API_TOKEN", "tok")
    runner = cli_runner(WizardStubClient())

    # name=<enter>, executor=2 (railway), env-id=<id>, railway_network=<enter>,
    # railway_token=<enter> (blank: inherit the env), confirm=<enter>,
    # done=<enter> (mode is forced, not asked), repo=<url>, update_base=<enter>,
    # sink=<enter>.
    result = runner.invoke(
        cli.app,
        ["connect"],
        input="\n2\nenv-123\n\n\n\n\ngit@example.com:o/r.git\n\n" + sink_answers(),
    )
    assert result.exit_code == 0, result.output

    cfg = load_config(config_path)
    assert cfg is not None
    conn = cfg.connection("frontend")
    assert conn is not None
    assert conn.executor == "railway"
    assert conn.git_init == "branch"
    assert conn.repo == "git@example.com:o/r.git"
    assert conn.mode == "build"
    assert conn.folder is None  # a sandbox has no folder on this machine

    settings = railway_settings(conn)
    assert settings is not None
    assert settings.environment_id == "env-123"
    assert settings.network == "isolated"


def test_the_wizard_persists_a_per_connection_token(
    config_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """The token entered in the wizard is stored on the connection, so two
    railway connections in different projects can each authenticate."""
    save_config(config(), config_path)
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/railway")
    runner = cli_runner(WizardStubClient())

    # ..., railway_network=<enter>, token="tok-a", token kind=<enter> (project),
    # then confirm/done defaults, the repo, and update_base/sinks defaults.
    result = runner.invoke(
        cli.app,
        ["connect"],
        input="\n2\nenv-123\n\ntok-a\n\n\n\ngit@example.com:o/r.git\n\n" + sink_answers(),
    )
    assert result.exit_code == 0, result.output

    cfg = load_config(config_path)
    assert cfg is not None
    conn = cfg.connection("frontend")
    assert conn is not None

    settings = railway_settings(conn)
    assert settings is not None
    assert settings.token == "tok-a"
    assert settings.token_kind == "project"


def test_the_wizard_warns_about_missing_prereqs_without_refusing(
    config_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """A missing CLI or token is the user's to fix later, not a reason to throw
    away the connection they just described — so both warn and it still saves."""
    save_config(config(), config_path)
    monkeypatch.setattr(shutil, "which", lambda name: None)
    monkeypatch.delenv("RAILWAY_API_TOKEN", raising=False)
    runner = cli_runner(WizardStubClient())

    result = runner.invoke(
        cli.app,
        ["connect"],
        input="\n2\nenv-123\n\n\n\n\ngit@example.com:o/r.git\n\n" + sink_answers(),
    )
    assert result.exit_code == 0, result.output
    assert "railway' cli not found" in result.output.lower()
    assert "railway_api_token" in result.output.lower()
    assert "build-template" in result.output.lower()

    cfg = load_config(config_path)
    assert cfg is not None
    assert cfg.connection("frontend") is not None


# ---------------------------------------------------------------------------
# issuebot doctor
# ---------------------------------------------------------------------------


def test_doctor_warns_when_the_cli_is_missing(
    monkeypatch: pytest.MonkeyPatch, config_with_railway_connection: Path
):
    """A railway-executor connection warns when the 'railway' CLI is missing and
    when RAILWAY_API_TOKEN is unset."""
    monkeypatch.setenv("ISSUEBOT_CONFIG", str(config_with_railway_connection))
    monkeypatch.delenv("RAILWAY_API_TOKEN", raising=False)
    monkeypatch.setattr(shutil, "which", lambda name: None if name == "railway" else "/x")
    runner = cli_runner(ok_client())
    # The connection's working copy is a clone; stub out the reachability check.
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: type("R", (), {"returncode": 0})())

    result = runner.invoke(cli.app, ["doctor"])

    assert "railway" in result.output.lower()
    assert "RAILWAY_API_TOKEN" in result.output


def test_doctor_is_quiet_when_the_environment_is_healthy(
    monkeypatch: pytest.MonkeyPatch, config_with_railway_connection: Path
):
    """No warnings when the CLI is on PATH, the token is set, and the connection
    has an environment id."""
    monkeypatch.setenv("ISSUEBOT_CONFIG", str(config_with_railway_connection))
    monkeypatch.setenv("RAILWAY_API_TOKEN", "tok")
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/" + name)
    runner = cli_runner(ok_client())
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: type("R", (), {"returncode": 0})())

    result = runner.invoke(cli.app, ["doctor"])

    assert "warning" not in result.output.lower()


def test_doctor_looks_for_the_executable_the_connection_configured(
    monkeypatch: pytest.MonkeyPatch,
):
    """A connection that names an absolute CLI path is warned about that path —
    a bare 'railway' it never runs says nothing about whether it can run."""
    asked: list[str] = []

    def which(name: str) -> str | None:
        asked.append(name)
        return None

    monkeypatch.setattr(shutil, "which", which)

    conn = Connection(
        name="p",
        board="b",
        executor="railway",
        railway={"environment_id": "env-123", "command": "/opt/rw/bin/railway"},
    )
    messages: list[str] = []

    railway_doctor(conn, echo=messages.append)

    assert asked == ["/opt/rw/bin/railway"]
    assert any("/opt/rw/bin/railway" in message for message in messages)
