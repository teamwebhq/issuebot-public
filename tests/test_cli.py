from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from typer.testing import CliRunner

import issuebot
from conftest import (
    StubClient,
    cli_runner,
    config,
    connection,
    in_process_environment,
    ok_client,
    sink_answers,
    source_table,
)
from issuebot import cli, doctor_checks, plugins
from issuebot.config import (
    Config,
    Connection,
    conn_setting,
    harness_settings,
    load_config,
    save_config,
    source_plugin,
)
from issuebot.plugins.base import SinkPlugin
from issuebot.plugins.sinks.fake.sink import FakeSink
from issuebot.plugins.sources.base import ConnectionConflict

runner = CliRunner()


def _where_to_run() -> list[str]:
    """The ``--executor`` flag a scripted, non-interactive `connect` must pass.

    With more than one environment installed a connection has to say where its
    work runs — there is no privileged default any more (`config.executor_name`)
    — so every flag-driven `connect` here names the one that runs work in this
    process. On an install with a single environment the flag is unnecessary and
    left off, which is also what keeps these tests true when one is deleted.
    """
    name = in_process_environment()
    return ["--executor", name] if name else []


def _executor_answer() -> str:
    """The line the wizard's executor question consumes, which may be none.

    A numbered picker with a single option is not asked at all — it announces
    the answer and moves on. So how many input lines a scripted wizard run must
    supply depends on how many environment plugins are installed. Deriving it
    from the registry is what keeps these tests true when an environment plugin
    is added or, more to the point, deleted."""
    return "\n" if len(plugins.offered("environments")) > 1 else ""


def _base_config() -> Config:
    """Minimal valid config with no connections."""
    return config()


@pytest.fixture
def config_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point ISSUEBOT_CONFIG at a per-test temp file."""
    path = tmp_path / "config.toml"
    monkeypatch.setenv("ISSUEBOT_CONFIG", str(path))
    return path


def _stub_source_plugin() -> SimpleNamespace:
    """The installed source, answering `issuebot init` with a fixed table.

    Named off the registry, so this stays true whichever source is installed —
    what `init` has to get right is that it asks the source and files the answer
    under the source's own name, not which questions that source asks. Those are
    the plugin's own (`tests/plugins/sources/<name>/`)."""
    name = source_plugin().name
    return SimpleNamespace(name=name, setup=lambda: source_table()[name])


# --- init --------------------------------------------------------------------


def test_init_runs_the_chosen_harnesses_setup_hook(
    config_path: Path, monkeypatch: pytest.MonkeyPatch
):
    runner = cli_runner(ok_client())
    monkeypatch.setattr(cli.wizard, "source_plugin", _stub_source_plugin)
    calls: list[str] = []
    monkeypatch.setattr(
        cli.doctor_checks, "run_harness_doctor", lambda cfg, **kw: calls.append("ran")
    )

    # Prompts left in core: Harness (default), harness executable (default).
    result = runner.invoke(cli.app, ["init"], input="\n\n")
    assert result.exit_code == 0, result.output
    assert calls == ["ran"]


def test_init_skip_harness_setup(config_path: Path, monkeypatch: pytest.MonkeyPatch):
    runner = cli_runner(ok_client())
    monkeypatch.setattr(cli.wizard, "source_plugin", _stub_source_plugin)
    calls: list[str] = []
    monkeypatch.setattr(
        cli.doctor_checks, "run_harness_doctor", lambda cfg, **kw: calls.append("ran")
    )

    result = runner.invoke(cli.app, ["init", "--skip-harness-setup"], input="\n\n")
    assert result.exit_code == 0, result.output
    assert calls == []


# --- connect -----------------------------------------------------------------


def test_connect_writes_config_and_calls_api(config_path: Path, tmp_path: Path):
    """connect saves the Connection to config AND calls client.connect(board)."""
    save_config(_base_config(), config_path)
    folder = tmp_path / "work"
    folder.mkdir()
    calls: list = []
    runner = cli_runner(StubClient(calls))

    result = runner.invoke(
        cli.app,
        [
            "connect",
            *_where_to_run(),
            "--name",
            "frontend",
            "--board",
            "b-1",
            "--folder",
            str(folder),
        ],
    )
    assert result.exit_code == 0, result.output
    # The connection's name is sent to the server so the dashboard can label it.
    assert ("connect", "b-1", "frontend") in calls
    cfg = load_config(config_path)
    assert cfg is not None
    conn = cfg.connection("frontend")
    assert conn is not None
    assert conn.board == "b-1"


def test_connect_refuses_board_already_used_locally(config_path: Path, tmp_path: Path):
    """A local pre-check refuses a board already used by another config connection."""
    folder = tmp_path / "work"
    folder.mkdir()
    cfg = _base_config()
    cfg.connections.append(connection(name="existing", board="b-1", folder=str(folder)))
    save_config(cfg, config_path)
    calls: list = []
    runner = cli_runner(StubClient(calls))

    result = runner.invoke(
        cli.app,
        [
            "connect",
            *_where_to_run(),
            "--name",
            "second",
            "--board",
            "b-1",
            "--folder",
            str(folder),
        ],
    )
    assert result.exit_code != 0
    # No server call, and no second connection written.
    assert calls == []
    reloaded = load_config(config_path)
    assert reloaded is not None
    assert reloaded.connection("second") is None
    assert len(reloaded.connections) == 1


def test_connect_server_conflict_does_not_write_config(config_path: Path, tmp_path: Path):
    """A server ConnectionConflict aborts: the CLI exits non-zero and writes nothing."""
    save_config(_base_config(), config_path)
    folder = tmp_path / "work"
    folder.mkdir()
    calls: list = []
    runner = cli_runner(StubClient(calls, raises=ConnectionConflict("b-1")))

    result = runner.invoke(
        cli.app,
        [
            "connect",
            *_where_to_run(),
            "--name",
            "frontend",
            "--board",
            "b-1",
            "--folder",
            str(folder),
        ],
    )
    assert result.exit_code != 0
    reloaded = load_config(config_path)
    assert reloaded is not None
    assert reloaded.connection("frontend") is None


def test_connect_then_connections(config_path: Path, tmp_path: Path):
    """connect saves the connection; connections lists it."""
    save_config(_base_config(), config_path)
    folder = tmp_path / "work"
    folder.mkdir()
    calls: list = []
    runner = cli_runner(StubClient(calls))

    result = runner.invoke(
        cli.app,
        [
            "connect",
            *_where_to_run(),
            "--name",
            "p",
            "--board",
            "b-1",
            "--folder",
            str(folder),
            "--done",
            "review",
        ],
    )
    assert result.exit_code == 0, result.output

    cfg = load_config(config_path)
    assert cfg is not None
    conn = cfg.connection("p")
    assert conn is not None
    assert conn.board == "b-1"
    assert conn.folder == str(folder)
    assert conn.done == "review"

    listed = runner.invoke(cli.app, ["connections"])
    assert listed.exit_code == 0
    assert "p" in listed.output
    assert "b-1" in listed.output


def test_connect_without_confirm(config_path: Path, tmp_path: Path):
    """--confirm no is persisted and appears in the connections list."""
    save_config(_base_config(), config_path)
    folder = tmp_path / "work"
    folder.mkdir()
    calls: list = []
    runner = cli_runner(StubClient(calls))

    result = runner.invoke(
        cli.app,
        [
            "connect",
            *_where_to_run(),
            "--name",
            "p",
            "--board",
            "b-1",
            "--folder",
            str(folder),
            "--confirm",
            "no",
        ],
    )
    assert result.exit_code == 0, result.output

    cfg = load_config(config_path)
    assert cfg is not None
    conn = cfg.connection("p")
    assert conn is not None
    assert conn.confirm is False

    listed = runner.invoke(cli.app, ["connections"])
    assert listed.exit_code == 0
    assert "confirm" in listed.output
    assert "no" in listed.output


def test_connect_defaults_to_confirming(config_path: Path, tmp_path: Path):
    """Sign-off before code is the default: an unflagged connect gets it."""
    save_config(_base_config(), config_path)
    folder = tmp_path / "work"
    folder.mkdir()
    runner = cli_runner(StubClient([]))

    result = runner.invoke(
        cli.app,
        ["connect", *_where_to_run(), "--name", "p", "--board", "b-1", "--folder", str(folder)],
    )
    assert result.exit_code == 0, result.output
    assert load_config(config_path).connection("p").confirm is True


def test_connect_rejects_missing_folder(config_path: Path, tmp_path: Path):
    """A non-existent --folder exits 1."""
    save_config(_base_config(), config_path)

    result = runner.invoke(
        cli.app,
        [
            "connect",
            *_where_to_run(),
            "--name",
            "p",
            "--board",
            "b-1",
            "--folder",
            str(tmp_path / "nope"),
        ],
    )
    assert result.exit_code == 1


def test_connect_needs_a_folder_or_a_repo(config_path: Path):
    """Where the working copy comes from is its own question, and a connection
    that answers neither has nowhere to work."""
    save_config(_base_config(), config_path)

    result = runner.invoke(
        cli.app,
        ["connect", *_where_to_run(), "--name", "p", "--board", "b", "--isolation", "branch"],
    )
    assert result.exit_code == 1
    assert "folder" in result.output.lower()


def test_connect_clone_persists_repo_without_folder(config_path: Path):
    """A connection whose working copy is a clone stores repo and no folder."""
    save_config(_base_config(), config_path)
    calls: list = []
    runner = cli_runner(StubClient(calls))

    result = runner.invoke(
        cli.app,
        [
            "connect",
            *_where_to_run(),
            "--name",
            "p",
            "--board",
            "b",
            "--isolation",
            "branch",
            "--repo",
            "https://github.com/o/r.git",
        ],
    )
    assert result.exit_code == 0, result.output

    cfg = load_config(config_path)
    assert cfg is not None
    p = cfg.connection("p")
    assert p is not None
    assert p.repo == "https://github.com/o/r.git"
    assert p.folder is None


def test_connect_clone_rejects_bad_repo_url(config_path: Path):
    """A non-git --repo URL exits 1."""
    save_config(_base_config(), config_path)

    result = runner.invoke(
        cli.app,
        [
            "connect",
            *_where_to_run(),
            "--name",
            "p",
            "--board",
            "b",
            "--isolation",
            "branch",
            "--repo",
            "not-a-url",
        ],
    )
    assert result.exit_code == 1
    assert "url" in result.output.lower()


def test_connect_sinks_are_configurable_from_the_command_line(config_path: Path, tmp_path: Path):
    """`--sinks` is how a scripted connect says "open a PR" — and its
    `:best-effort` suffix is how it says "but don't block on it"."""
    save_config(_base_config(), config_path)
    runner = cli_runner(StubClient([]))
    folder = tmp_path / "work"
    folder.mkdir()

    result = runner.invoke(
        cli.app,
        [
            "connect",
            *_where_to_run(),
            "--name",
            "web",
            "--board",
            "b-1",
            "--folder",
            str(folder),
            "--sinks",
            "fake:best-effort",
        ],
    )
    assert result.exit_code == 0, result.output

    cfg = load_config(config_path)
    assert cfg is not None
    conn = cfg.connection("web")
    assert conn is not None
    assert [(s.name, s.required) for s in conn.sinks] == [("fake", False)]

    # ...and the listing reads the qualifier back.
    listed = runner.invoke(cli.app, ["connections"])
    assert "fake (best-effort)" in listed.output


def test_connect_rejects_an_unknown_sink(config_path: Path, tmp_path: Path):
    """A sink nothing installed answers to is named, with what does exist."""
    save_config(_base_config(), config_path)
    folder = tmp_path / "work"
    folder.mkdir()

    result = runner.invoke(
        cli.app,
        [
            "connect",
            *_where_to_run(),
            "--name",
            "web",
            "--board",
            "b-1",
            "--folder",
            str(folder),
            "--sinks",
            "gitbub",
        ],
    )
    assert result.exit_code == 1
    assert "gitbub" in result.output
    # ...and every sink that does exist is named, so the typo can be corrected.
    for name in plugins.names_of("sinks"):
        assert name in result.output


@pytest.mark.parametrize(
    ("assignment", "expected"),
    [
        ("nosuchplugin.key=1", "nosuchplugin"),
        ("nodots", "<plugin>.<key>=<value>"),
    ],
)
def test_connect_refuses_a_setting_no_plugin_can_honour(
    config_path: Path, tmp_path: Path, assignment: str, expected: str
):
    """A `--set` naming nothing installed, or not shaped like an assignment at
    all, is refused by name rather than silently dropped.

    A typo in an *installed* plugin's own key or value is refused the same way,
    but only that plugin's test directory can say so without core learning its
    name — see the plugin suites for those cases."""
    save_config(_base_config(), config_path)
    folder = tmp_path / "work"
    folder.mkdir()

    result = runner.invoke(
        cli.app,
        [
            "connect",
            *_where_to_run(),
            "--name",
            "web",
            "--board",
            "b-1",
            "--folder",
            str(folder),
            "--set",
            assignment,
        ],
    )
    assert result.exit_code == 1
    assert expected in result.output


def test_connect_help_lists_the_settings_this_install_takes(config_path: Path):
    """`--set` documents itself from the registry, so `connect --help` stays
    useful without naming any one plugin in `cli.py`.

    The expectation is built from the registry rather than written down, which
    is the only way to assert "every installed plugin's settings are listed"
    without this file learning any plugin's name — and it keeps the test true
    when a plugin is added or deleted."""
    result = runner.invoke(cli.app, ["connect", "--help"])
    assert result.exit_code == 0

    # Typer wraps and re-indents help text, so compare on a whitespace-free
    # rendering rather than trying to predict the line breaks.
    rendered = "".join(result.output.split())

    # Table plugins only, in both halves. A *flat* plugin's keys are connection
    # fields, so one may legitimately have a core flag of its own (`--folder`)
    # and be excluded from `--set` because of it; which those are is the
    # command's knowledge, not something to re-derive here. A *table* plugin's
    # settings are its own, so they must be listed, and it must have no flag —
    # that second half is ADR-0002 as an assertion.
    for plugin in plugins.every():
        if plugin.settings is None or plugin.flat:
            continue

        for key in plugin.settings.model_fields:
            assert f"{plugin.name}.{key}" in rendered, f"--set help omits {plugin.name}.{key}"

        assert f"--{plugin.name}" not in result.output


def test_connect_without_config_says_run_init(config_path: Path):
    """connect without a config file tells the user to run init."""
    result = runner.invoke(
        cli.app,
        ["connect", *_where_to_run(), "--name", "p", "--board", "b-1", "--folder", "/tmp"],
    )
    assert result.exit_code == 1
    assert "init" in result.output.lower()


def test_connect_without_an_executor_names_the_flag_not_the_config_key(
    config_path: Path, tmp_path: Path
):
    """A command line gets command-line advice.

    `executor_name`'s own sentence says `set executor = "…"`, which is right in
    a config file and wrong here: what the user missed is a flag, and telling
    them to edit TOML sends them to the wrong surface. The rule is still
    `executor_name`'s — this only re-words its refusal."""
    if not _where_to_run():
        pytest.skip("one environment installed, so a connection need not name it")

    save_config(_base_config(), config_path)
    folder = tmp_path / "work"
    folder.mkdir()
    runner = cli_runner(StubClient([]))

    result = runner.invoke(
        cli.app, ["connect", "--name", "p", "--board", "b-1", "--folder", str(folder)]
    )

    assert result.exit_code == 1
    assert "--executor" in result.output
    assert "set executor =" not in result.output


def test_connect_stray_update_base_gets_the_accurate_refusal(config_path: Path, tmp_path: Path):
    """`--update-base rebase` with no `--isolation` on a plain folder used to
    be refused with "a git workspace requires a git repo" — a complaint about a
    repo the connection was never going to use. The accurate message is git's
    own cross-field rule: an update-base setting with no strategy to update."""
    save_config(_base_config(), config_path)
    folder = tmp_path / "plain"
    folder.mkdir()
    runner = cli_runner(StubClient([]))

    result = runner.invoke(
        cli.app,
        [
            "connect",
            *_where_to_run(),
            "--name",
            "p",
            "--board",
            "b-1",
            "--folder",
            str(folder),
            "--update-base",
            "rebase",
        ],
    )

    assert result.exit_code == 1
    assert "'update_base' with no git_init" in result.output
    assert "requires a git repo" not in result.output


def test_the_flag_literal_aliases_match_the_plugin_owned_literals():
    """`cli.py`'s five `*Flag` aliases re-spell plugin-owned value domains
    because a Typer option needs its choices as an annotation and core cannot
    import a plugin module (see `intake.FLAG_OWNED`). Re-spelled means they
    can drift; this ties each alias to its owner so drift fails a test
    instead of shipping. The imports are the test's, not core's — a test may
    look at a plugin."""
    from typing import get_args

    from issuebot.plugins.sources.issuebear.settings import ConfirmChoice, DoneMode, Mode
    from issuebot.plugins.workspaces.git.settings import Isolation, UpdateBase

    assert get_args(cli.DoneFlag) == get_args(DoneMode)
    assert get_args(cli.ConfirmFlag) == get_args(ConfirmChoice)
    assert get_args(cli.ModeFlag) == get_args(Mode)
    assert get_args(cli.IsolationFlag) == get_args(Isolation)
    assert get_args(cli.UpdateBaseFlag) == get_args(UpdateBase)


def test_old_project_command_is_gone():
    """The removed 'project' sub-app must exit non-zero (no such command)."""
    result = runner.invoke(cli.app, ["project", "add", "--name", "x", "--board", "b"])
    assert result.exit_code != 0


def test_connect_partial_flags_error_points_at_wizard(config_path: Path):
    """Giving only --name (no --board) errors and mentions the no-flag wizard."""
    save_config(_base_config(), config_path)

    result = runner.invoke(cli.app, ["connect", *_where_to_run(), "--name", "p"])
    assert result.exit_code == 1
    assert "wizard" in result.output.lower()


# --- connect wizard ----------------------------------------------------------


class _WizardStubClient:
    """Fake client exposing the org/project/board listings and connect the wizard
    needs, recording the listing/connect calls made against it."""

    def __init__(
        self,
        calls: list,
        *,
        orgs: list[dict],
        projects: list[dict],
        boards: list[dict],
    ) -> None:
        self._calls = calls
        self._orgs = orgs
        self._projects = projects
        self._boards = boards

    def list_organisations(self) -> list[dict]:
        self._calls.append(("list_organisations",))
        return self._orgs

    def list_projects(self, org_id: str) -> list[dict]:
        self._calls.append(("list_projects", org_id))
        return self._projects

    def list_boards(self, project_id: str) -> list[dict]:
        self._calls.append(("list_boards", project_id))
        return self._boards

    def connect(
        self, board_id: str, name: str | None = None, install_id: str | None = None
    ) -> dict:
        self._calls.append(("connect", board_id, name))
        return {"warning": None}

    def close(self) -> None:
        """No-op for the stub."""


def test_connect_wizard_builds_connection(config_path: Path, tmp_path: Path):
    """`issuebot connect` with no flags walks the wizard: one org/project are
    auto-selected, a board is picked by number, and the suggested name and
    default settings are accepted, writing a connection and connecting server-side."""
    save_config(_base_config(), config_path)
    folder = tmp_path / "work"
    folder.mkdir()

    calls: list = []
    stub = _WizardStubClient(
        calls,
        orgs=[{"id": "o1", "name": "Acme"}],
        projects=[{"id": "p1", "name": "Web"}],
        boards=[{"id": "b1", "name": "Frontend"}, {"id": "b2", "name": "Backend"}],
    )
    runner = cli_runner(stub)

    # board=2 (Backend), name=<enter>, executor=<enter> (local, if asked),
    # mode=<enter>, confirm=<enter>, done=<enter>, working copy=<enter>
    # (folder), isolation=<enter>, folder=<path>, then "no" to every
    # installed sink. Isolation defaults to "none", which cuts no branch, so
    # update-base is not asked at all.
    user_input = f"2\n\n{_executor_answer()}\n\n\n\n\n{folder}\n" + sink_answers()
    result = runner.invoke(cli.app, ["connect"], input=user_input)
    assert result.exit_code == 0, result.output

    # The board's human names were shown so the pick is informed.
    assert "Frontend" in result.output
    assert "Backend" in result.output

    # The server was told about the picked board under the suggested name.
    assert ("list_projects", "o1") in calls
    assert ("list_boards", "p1") in calls
    assert ("connect", "b2", "backend") in calls

    cfg = load_config(config_path)
    assert cfg is not None
    conn = cfg.connection("backend")
    assert conn is not None
    assert conn.board == "b2"
    assert conn.folder == str(folder)
    assert conn_setting(conn, "git_init", None) is None
    assert conn.confirm is True
    # The wizard's first offered environment, whatever the registry answers —
    # naming one here would be the coupling the plugin boundary prevents.
    assert conn.executor == plugins.offered("environments")[0]


def test_connect_wizard_reprompts_on_bad_folder(config_path: Path, tmp_path: Path):
    """A non-existent folder is re-prompted rather than aborting the wizard."""
    save_config(_base_config(), config_path)
    folder = tmp_path / "work"
    folder.mkdir()

    calls: list = []
    stub = _WizardStubClient(
        calls,
        orgs=[{"id": "o1", "name": "Acme"}],
        projects=[{"id": "p1", "name": "Web"}],
        boards=[{"id": "b1", "name": "Frontend"}],
    )
    runner = cli_runner(stub)

    # name/executor/mode/confirm/done/working-copy/isolation default, then a
    # bad folder, then the good one, then "no" to every installed sink.
    bad = tmp_path / "nope"
    user_input = f"\n\n\n\n\n\n\n{bad}\n{folder}\n" + sink_answers()
    result = runner.invoke(cli.app, ["connect"], input=user_input)
    assert result.exit_code == 0, result.output
    assert ("connect", "b1", "frontend") in calls

    cfg = load_config(config_path)
    assert cfg is not None
    conn = cfg.connection("frontend")
    assert conn is not None
    assert conn.folder == str(folder)


def test_connect_wizard_asks_about_each_offered_sink(
    config_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """The wizard asks a question per offered sink — the only interactive way to
    get results published — and records whether each one blocks the task.

    Two stub sinks rather than the shipped one. The rule under test is "one
    question each, and the answer lands on the connection", which is the same
    however many sinks there are and whoever they are; reading the real registry
    made this an `installed[0]` that raised `IndexError` the moment every real
    sink was deleted, which is the failure mode the plugin boundary
    warns about at the top. The other kinds still resolve for real, since the
    wizard has to walk the whole flow to reach the sink questions.
    """
    save_config(_base_config(), config_path)
    folder = tmp_path / "work"
    folder.mkdir()

    real_registry = plugins.all_of
    offered = {
        "publisher": SinkPlugin(name="publisher", sink=FakeSink),
        "shouter": SinkPlugin(name="shouter", sink=FakeSink),
    }
    monkeypatch.setattr(
        plugins, "all_of", lambda kind: offered if kind == "sinks" else real_registry(kind)
    )

    stub = _WizardStubClient(
        [],
        orgs=[{"id": "o1", "name": "Acme"}],
        projects=[{"id": "p1", "name": "Web"}],
        boards=[{"id": "b1", "name": "Frontend"}],
    )
    runner = cli_runner(stub)

    # ...defaults through the local flow, then best-effort for one of the two.
    # No update-base answer: isolation defaults to "none", which cuts no branch
    # to update, so the wizard does not ask.
    user_input = f"\n\n\n\n\n\n\n{folder}\n" + sink_answers(publisher="3")
    result = runner.invoke(cli.app, ["connect"], input=user_input)
    assert result.exit_code == 0, result.output

    # Both were asked about, not just the one that was taken.
    assert "publisher" in result.output
    assert "shouter" in result.output

    cfg = load_config(config_path)
    assert cfg is not None
    conn = cfg.connection("frontend")
    assert conn is not None
    assert [(s.name, s.required) for s in conn.sinks] == [("publisher", False)]


def test_connect_wizard_without_config_says_run_init(config_path: Path):
    """The wizard still requires an existing config (server URL + PAT)."""
    result = runner.invoke(cli.app, ["connect"], input="")
    assert result.exit_code == 1
    assert "init" in result.output.lower()


def test_connect_wizard_no_boards_aborts_cleanly(config_path: Path):
    """A server with no boards aborts with a message instead of looping."""
    save_config(_base_config(), config_path)
    calls: list = []
    stub = _WizardStubClient(
        calls,
        orgs=[{"id": "o1", "name": "Acme"}],
        projects=[{"id": "p1", "name": "Web"}],
        boards=[],
    )
    runner = cli_runner(stub)

    result = runner.invoke(cli.app, ["connect"], input="")
    assert result.exit_code == 1
    assert "no board" in result.output.lower()


# --- disconnect --------------------------------------------------------------


def test_disconnect_removes_config_and_calls_api(config_path: Path, tmp_path: Path):
    """disconnect removes the connection from config AND calls client.disconnect(board)."""
    folder = tmp_path / "work"
    folder.mkdir()
    cfg = _base_config()
    cfg.connections.append(connection(name="p", board="b-1", folder=str(folder)))
    save_config(cfg, config_path)
    calls: list = []
    runner = cli_runner(StubClient(calls))

    result = runner.invoke(cli.app, ["disconnect", "--name", "p"])
    assert result.exit_code == 0

    reloaded = load_config(config_path)
    assert reloaded is not None
    assert reloaded.connection("p") is None
    assert ("disconnect", "b-1") in calls


def test_disconnect_unknown_exits_1(config_path: Path):
    """Disconnecting an unknown name exits 1."""
    save_config(_base_config(), config_path)

    result = runner.invoke(cli.app, ["disconnect", "--name", "nope"])
    assert result.exit_code == 1


# --- doctor ------------------------------------------------------------------


def test_doctor_ok(config_path: Path):
    save_config(_base_config(), config_path)
    runner = cli_runner(ok_client())

    result = runner.invoke(cli.app, ["doctor"])
    assert result.exit_code == 0, result.output
    assert "ok" in result.output.lower()


def test_doctor_reports_a_pat_the_board_refuses(config_path: Path):
    """The board refusing the task read is the finding, not a reason to ask a
    second endpoint."""
    save_config(_base_config(), config_path)

    class _Refused(Exception):
        status = 401

    class _BadPat:
        def get_tasks(self, *, board_id: str | None = None, wait: int = 0) -> list[Any]:
            """The board refuses this PAT."""
            raise _Refused("bad token")

        def close(self) -> None:
            """No-op for the stub."""

    result = cli_runner(_BadPat()).invoke(cli.app, ["doctor"])

    assert result.exit_code == 1
    assert "PAT check failed" in result.output


def test_doctor_without_config_exits_1(config_path: Path):
    result = runner.invoke(cli.app, ["doctor"])
    assert result.exit_code == 1


@pytest.mark.parametrize(
    ("contents", "expected"),
    [
        ('[[connections]]\nname = "p"\nboard = "b"\nsinks = ["nosuchsink"]\n', "nosuchsink"),
        ("this is not toml\n", "="),
        ("harness = 123\n", "harness"),
    ],
    ids=["names-an-uninstalled-plugin", "is-not-toml", "is-the-wrong-shape"],
)
def test_a_broken_config_is_reported_not_raised(config_path: Path, contents: str, expected: str):
    """A config file that cannot be used is the user's file, not a bug — so it
    gets a message naming the path and exit 1, never a traceback.

    Found by deleting a plugin by hand: a config written against a build that
    had it is the first thing the next run reads, and the whole point of the
    registry answering `unknown sink 'x' (known: ...)` is wasted if that arrives
    inside a stack frame dump. The other two cases are the same failure for
    different reasons, and were crashing the same way."""
    config_path.write_text(contents)

    result = runner.invoke(cli.app, ["doctor"])

    assert result.exit_code == 1
    assert result.exception is None or isinstance(result.exception, SystemExit), result.exception
    assert str(config_path) in result.output  # which file, not just what is wrong
    assert expected in result.output


def test_doctor_reports_an_unnameable_harness_instead_of_raising():
    """`doctor` is the command you run when the install is broken, so a config
    this build cannot name a harness for has to come back as a finding.

    Reached directly rather than through the CLI: `load_config` rejects such a
    config first, which is a better message still — but `check` is called with
    whatever `Config` its caller holds, and one broken check must never take the
    rest of the report with it."""
    warnings: list[str] = []

    doctor_checks.check_harness(Config(harness="not-installed"), echo=print, warn=warnings.append)

    assert any("not-installed" in w for w in warnings)


def test_doctor_warns_when_harness_command_missing(config_path: Path, tmp_path: Path):
    cfg = _base_config()
    cfg.fake = {"command": str(tmp_path / "nonexistent-agent")}
    save_config(cfg, config_path)
    runner = cli_runner(ok_client())

    result = runner.invoke(cli.app, ["doctor"])

    # Missing harness is a warning, not a failure.
    assert result.exit_code == 0, result.output
    assert "ok" in result.output.lower()
    assert "warning" in result.output.lower()


def test_doctor_ok_with_executable_harness_command(config_path: Path, tmp_path: Path):
    exe = tmp_path / "agent"
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)

    cfg = _base_config()
    cfg.fake = {"command": str(exe)}
    save_config(cfg, config_path)
    runner = cli_runner(ok_client())

    result = runner.invoke(cli.app, ["doctor"])

    assert result.exit_code == 0, result.output
    assert "warning" not in result.output.lower()


def test_doctor_does_not_crash_on_missing_connection_folder(config_path: Path, tmp_path: Path):
    cfg = _base_config()
    cfg.connections = [
        connection(name="p", board="b", folder=str(tmp_path / "gone"), git_init="branch")
    ]
    save_config(cfg, config_path)
    runner = cli_runner(ok_client())
    result = runner.invoke(cli.app, ["doctor"])
    assert result.exit_code == 0, result.output
    assert "warning" in result.output.lower()  # surfaced as a warning, not a crash


# --- listen ------------------------------------------------------------------


def test_listen_passes_harness_command(
    config_path: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    folder = tmp_path / "work"
    folder.mkdir()
    cfg = _base_config()
    cfg.fake = {"command": "/custom/path/agent"}
    cfg.connections.append(connection(name="p", board="b-1", folder=str(folder)))
    save_config(cfg, config_path)

    captured: dict[str, object] = {}

    def fake_harness_for(cfg, **kw):
        captured["name"] = cfg.harness
        captured["command"] = harness_settings(cfg).get("command")
        return object()

    class FakeSupervisor:
        """Stub Supervisor that records nothing and does nothing."""

        @classmethod
        def from_config(cls, cfg, api, harness, **kw):
            return cls()

        def start(self) -> None:
            pass

        def stop(self) -> None:
            pass

    # Patch runner.Supervisor — the listen command now uses the Supervisor.
    monkeypatch.setattr("issuebot.runner.Supervisor", FakeSupervisor)

    runner = cli_runner(ok_client())
    monkeypatch.setattr(cli, "harness_for", fake_harness_for)

    # Stop the infinite loop immediately.
    def fake_sleep(_seconds):
        raise KeyboardInterrupt

    monkeypatch.setattr(cli.time, "sleep", fake_sleep)

    result = runner.invoke(cli.app, ["listen"])

    assert result.exit_code == 0, result.output
    assert captured["name"] == cfg.harness
    assert captured["command"] == "/custom/path/agent"


# --- version -----------------------------------------------------------------


def test_version(config_path: Path):
    result = runner.invoke(cli.app, ["version"])
    assert result.exit_code == 0
    assert result.output == f"{issuebot.__version__}\n"


def test_version_has_no_commit_option(config_path: Path):
    result = runner.invoke(cli.app, ["version", "--commit"])
    assert result.exit_code != 0


# --- git helpers used by worktree/clone tests --------------------------------


def _init_repo(path: Path) -> None:
    """Create a bare git repo at path."""
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-b", "main"], cwd=path, check=True, capture_output=True)


def _repo_with_origin(path: Path) -> None:
    """Init a git repo with a commit and a remote 'origin'."""
    _init_repo(path)
    subprocess.run(["git", "config", "user.email", "t@e.com"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=path, check=True)
    (path / "f").write_text("x")
    subprocess.run(["git", "add", "-A"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=path, check=True, capture_output=True)
    subprocess.run(
        ["git", "remote", "add", "origin", "https://example.invalid/r.git"],
        cwd=path,
        check=True,
    )


def _seed_repo(path: Path) -> None:
    """A real local repo with one commit, usable as a clone source."""
    _init_repo(path)
    subprocess.run(["git", "config", "user.email", "t@e.com"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=path, check=True)
    (path / "f").write_text("x")
    subprocess.run(["git", "add", "-A"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=path, check=True, capture_output=True)


# --- connect validation (isolation) -------------------------------------------


def test_connect_rejects_isolation_on_non_git_folder(config_path: Path, tmp_path: Path):
    save_config(_base_config(), config_path)
    folder = tmp_path / "plain"
    folder.mkdir()
    result = runner.invoke(
        cli.app,
        [
            "connect",
            *_where_to_run(),
            "--name",
            "p",
            "--board",
            "b",
            "--folder",
            str(folder),
            "--isolation",
            "branch",
        ],
    )
    assert result.exit_code == 1
    assert "git" in result.output.lower()


def test_connect_accepts_valid_isolation(config_path: Path, tmp_path: Path):
    save_config(_base_config(), config_path)
    folder = tmp_path / "repo"
    _init_repo(folder)
    calls: list = []
    runner = cli_runner(StubClient(calls))
    result = runner.invoke(
        cli.app,
        [
            "connect",
            *_where_to_run(),
            "--name",
            "p",
            "--board",
            "b",
            "--folder",
            str(folder),
            "--isolation",
            "worktree",
        ],
    )
    assert result.exit_code == 0
    loaded = load_config(config_path)
    assert loaded is not None
    p = loaded.connection("p")
    assert p is not None
    assert p.git_init == "worktree"


def test_connect_mode_respond_succeeds(config_path: Path, tmp_path: Path):
    """--mode respond alongside a workspace strategy: the two are independent,
    so naming both is fine."""
    save_config(_base_config(), config_path)
    folder = tmp_path / "work"
    _init_repo(folder)
    calls: list = []
    runner = cli_runner(StubClient(calls))

    result = runner.invoke(
        cli.app,
        [
            "connect",
            *_where_to_run(),
            "--name",
            "p",
            "--board",
            "b-1",
            "--folder",
            str(folder),
            "--mode",
            "respond",
            "--isolation",
            "branch",
        ],
    )
    assert result.exit_code == 0, result.output

    cfg = load_config(config_path)
    assert cfg is not None
    p = cfg.connection("p")
    assert p is not None
    assert p.mode == "respond"


def test_connect_mode_respond_in_place_succeeds(config_path: Path, tmp_path: Path):
    """Read-only work in the folder itself — no strategy, no branch.

    This is the combination the wizard produces for respond mode, and it was
    refused: `intake.build` wrote a `git_init=None` key, which put git in play
    for a rule that then demanded a strategy. TOML cannot hold the None, so the
    same config loaded back cleanly — the connection could only be created by
    hand-editing the file.
    """
    save_config(_base_config(), config_path)
    folder = tmp_path / "work"
    folder.mkdir()
    runner = cli_runner(StubClient([]))

    result = runner.invoke(
        cli.app,
        [
            "connect",
            *_where_to_run(),
            "--name",
            "r",
            "--board",
            "b-9",
            "--folder",
            str(folder),
            "--mode",
            "respond",
        ],
    )
    assert result.exit_code == 0, result.output

    cfg = load_config(config_path)
    assert cfg is not None
    conn = cfg.connection("r")
    assert conn is not None
    assert conn.mode == "respond"
    assert conn_setting(conn, "git_init", None) is None


def test_connect_wizard_respond_mode_survives_to_the_config(config_path: Path, tmp_path: Path):
    """The wizard's own respond path: it forces isolation='none', so every
    respond run died at the very end, after every question was answered."""
    save_config(_base_config(), config_path)
    folder = tmp_path / "work"
    folder.mkdir()

    stub = _WizardStubClient(
        [],
        orgs=[{"id": "o1", "name": "Acme"}],
        projects=[{"id": "p1", "name": "Web"}],
        boards=[{"id": "b1", "name": "Frontend"}],
    )
    runner = cli_runner(stub)

    # name=<enter>, executor=<enter> (if asked), mode=2 (respond),
    # confirm/done defaults, working copy=<enter> (folder), folder, then "no"
    # to every sink. respond skips isolation and update-base.
    user_input = f"\n{_executor_answer()}2\n\n\n\n{folder}\n" + sink_answers()
    result = runner.invoke(cli.app, ["connect"], input=user_input)
    assert result.exit_code == 0, result.output

    cfg = load_config(config_path)
    assert cfg is not None
    conn = cfg.connection("frontend")
    assert conn is not None
    assert conn.mode == "respond"


def test_connect_refuses_a_set_for_a_key_its_own_flag_writes(config_path: Path, tmp_path: Path):
    """`--set git.git_init=...` was accepted, then silently overwritten by
    `--isolation`'s value. It names the flag instead."""
    save_config(_base_config(), config_path)
    folder = tmp_path / "work"
    folder.mkdir()

    result = runner.invoke(
        cli.app,
        [
            "connect",
            *_where_to_run(),
            "--name",
            "p",
            "--board",
            "b-1",
            "--folder",
            str(folder),
            "--set",
            "git.git_init=worktree",
        ],
    )
    assert result.exit_code == 1
    assert "--isolation" in result.output


def test_connect_help_does_not_advertise_a_setting_it_will_refuse(config_path: Path):
    """A key `--set` refuses must not be listed as one it takes."""
    result = runner.invoke(cli.app, ["connect", "--help"])
    assert "git.push" in result.output  # reachable only through --set
    assert "git.git_init" not in result.output  # --isolation's to write


def test_connect_update_base_rebase_round_trips(config_path: Path, tmp_path: Path):
    """--update-base rebase should be saved into the connection.

    Only meaningful with a git_init strategy, so this also passes --isolation
    branch, on a real repo folder."""
    save_config(_base_config(), config_path)
    folder = tmp_path / "work"
    _init_repo(folder)
    calls: list = []
    runner = cli_runner(StubClient(calls))

    result = runner.invoke(
        cli.app,
        [
            "connect",
            *_where_to_run(),
            "--name",
            "p",
            "--board",
            "b-1",
            "--folder",
            str(folder),
            "--isolation",
            "branch",
            "--update-base",
            "rebase",
        ],
    )
    assert result.exit_code == 0, result.output

    cfg = load_config(config_path)
    assert cfg is not None
    p = cfg.connection("p")
    assert p is not None
    assert p.update_base == "rebase"


# --- connections -------------------------------------------------------------


def test_render_connections_empty():
    out = cli.render_connections([])
    assert "No connections configured" in out


def test_render_connections_shows_every_setting():
    """Every per-connection setting is surfaced with a label — including the ones
    the old positional line dropped (mode, update-base, branch-prefix)."""
    conn = Connection(
        name="mine",
        board="b-9",
        folder="/srv/work",
        done="complete",
        confirm=False,
        git_init="worktree",
        sinks=["fake"],
        branch_prefix="bot/",
        mode="build",
        update_base="merge",
    )
    out = cli.render_connections([conn])

    # Header identity.
    assert "mine" in out and "b-9" in out and "/srv/work" in out
    # Each setting appears labelled with its configured value.
    for label, value in [
        ("mode", "build"),
        ("isolation", "worktree"),
        ("sinks", "fake"),
        ("done", "complete"),
        ("confirm", "no"),
        ("update-base", "merge"),
        ("branch-prefix", "bot/"),
    ]:
        assert label in out, f"missing label {label!r}"
        assert value in out, f"missing value {value!r}"


def test_render_connections_clone_shows_repo_as_target():
    conn = Connection(name="c", board="b", repo="https://example.com/x.git", git_init="branch")
    out = cli.render_connections([conn])
    assert "https://example.com/x.git" in out


def test_render_connections_shows_absent_source_settings_as_absent():
    """A connection that reads no board loads fine (its source plugin is not in
    play), but then has no value *at all* for the source's flag-owned settings
    — the owning model cannot even resolve its defaults without the required
    `board`. The listing used to print "board None" and "confirm none", values
    the settings can never hold; absent must render as absent ("—")."""
    conn = Connection.model_validate(
        {"name": "solo", "folder": "/srv/work", "executor": in_process_environment()}
    )

    out = cli.render_connections([conn])

    assert "board None" not in out
    assert "board —" in out
    # `done`/`confirm`/`mode` can never be "none" — absent renders as "—".
    for label in ("done", "confirm", "mode"):
        line = next(ln for ln in out.splitlines() if ln.strip().startswith(label))
        assert line.split()[-1] == "—", f"{label} rendered an impossible value: {line!r}"


def test_connections_command_surfaces_update_base(config_path: Path, tmp_path: Path):
    """The CLI command (not just the helper) shows update-base — the gap Richard hit."""
    folder = tmp_path / "work"
    folder.mkdir()
    cfg = _base_config()
    cfg.connections.append(
        connection(
            name="p", board="b-1", folder=str(folder), git_init="branch", update_base="merge"
        )
    )
    save_config(cfg, config_path)

    result = runner.invoke(cli.app, ["connections"])
    assert result.exit_code == 0, result.output
    assert "update-base" in result.output
    assert "merge" in result.output


# --- status ------------------------------------------------------------------


def _connected_config(folder: Path) -> Config:
    cfg = _base_config()
    cfg.connections.append(connection(name="p", board="b-1", folder=str(folder)))
    return cfg


def test_status_without_config_exits_1(config_path: Path):
    result = runner.invoke(cli.app, ["status"])
    assert result.exit_code == 1


def test_status_no_runner_lists_connections(
    config_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    folder = tmp_path / "work"
    folder.mkdir()
    save_config(_connected_config(folder), config_path)

    result = runner.invoke(cli.app, ["status"])
    assert result.exit_code == 0, result.output
    assert "no status file" in result.output
    assert "p" in result.output and "b-1" in result.output


def test_status_active_shows_runtime(
    config_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from datetime import UTC, datetime

    from issuebot.agent_state import ConnectionSnapshot
    from issuebot.status import StatusStore, build_payload, default_status_path

    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    folder = tmp_path / "work"
    folder.mkdir()
    save_config(_connected_config(folder), config_path)

    payload = build_payload(
        [ConnectionSnapshot(name="p", board="b-1", phase="working", ref="ISS-9")],
        version="0.1.0",
        interval=15.0,
        now=datetime.now(UTC),
        pid=1234,
    )
    StatusStore(default_status_path()).write(payload)

    result = runner.invoke(cli.app, ["status"])
    assert result.exit_code == 0, result.output
    assert "active" in result.output
    assert "working" in result.output and "ISS-9" in result.output


# --- logs --------------------------------------------------------------------


def _write_log(state_root: Path, name: str, lines: list[str]) -> Path:
    log_dir = state_root / "issuebot" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    path = log_dir / name
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def test_logs_list_when_no_ref(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    state = tmp_path / "state"
    monkeypatch.setenv("XDG_STATE_HOME", str(state))
    _write_log(state, "ISS-1-20260629-200000.jsonl", ["{}"])

    result = runner.invoke(cli.app, ["logs"])
    assert result.exit_code == 0, result.output
    assert "ISS-1" in result.output


def test_logs_empty(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    result = runner.invoke(cli.app, ["logs"])
    assert result.exit_code == 0
    assert "No runs found" in result.output


def test_logs_render_ref_reads_back_through_the_configured_harness(
    config_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """A run log holds whatever its harness printed, so the command asks that
    harness to read it back.

    The config here names one whose output is plain text, so every line shows
    verbatim — which is also what a build with no config, or one whose harness
    has been removed, degrades to. What a *structured* harness makes of its own
    stream is asserted beside that plugin."""
    save_config(_base_config(), config_path)
    state = tmp_path / "state"
    monkeypatch.setenv("XDG_STATE_HOME", str(state))
    _write_log(state, "ISS-1-20260629-200000.jsonl", ["did the thing"])

    result = runner.invoke(cli.app, ["logs", "ISS-1"])

    assert result.exit_code == 0, result.output
    assert "did the thing" in result.output


def test_logs_unknown_ref_exits_1(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    result = runner.invoke(cli.app, ["logs", "ISS-404"])
    assert result.exit_code == 1


def test_connect_wizard_does_not_ask_a_question_it_would_refuse(config_path: Path, tmp_path: Path):
    """In-place work cuts no branch, and git's own validation refuses
    `update_base` without one — so answering that question could only fail the
    connection at the very end, after every other answer had been given."""
    save_config(_base_config(), config_path)
    folder = tmp_path / "work"
    folder.mkdir()

    stub = _WizardStubClient(
        [],
        orgs=[{"id": "o1", "name": "Acme"}],
        projects=[{"id": "p1", "name": "Web"}],
        boards=[{"id": "b1", "name": "Frontend"}],
    )
    runner = cli_runner(stub)

    # mode/confirm/done default, working copy=1 (folder), isolation=1
    # ("none"), the folder — and no update-base answer at all, because it must
    # not be asked.
    user_input = f"\n\n\n\n\n1\n1\n{folder}\n" + sink_answers()
    result = runner.invoke(cli.app, ["connect"], input=user_input)

    assert result.exit_code == 0, result.output
    assert "Update base" not in result.output


def test_connect_wizard_asks_where_the_copy_comes_from_and_what_to_cut(
    config_path: Path, tmp_path: Path
):
    """Two questions, because they are two decisions. Answering "a clone" and
    "cut nothing" is a connection the old four-valued question could not
    express at all — "clone" silently also meant "and cut a branch"."""
    save_config(_base_config(), config_path)

    stub = _WizardStubClient(
        [],
        orgs=[{"id": "o1", "name": "Acme"}],
        projects=[{"id": "p1", "name": "Web"}],
        boards=[{"id": "b1", "name": "Frontend"}],
    )
    runner = cli_runner(stub)

    # name/executor/mode/confirm/done default, working copy=2 (clone),
    # isolation=1 (none), then the clone URL and "no" to every sink.
    user_input = "\n\n\n\n\n2\n1\nhttps://github.com/o/r.git\n" + sink_answers()
    result = runner.invoke(cli.app, ["connect"], input=user_input)
    assert result.exit_code == 0, result.output

    assert "Working copy" in result.output

    cfg = load_config(config_path)
    assert cfg is not None
    conn = cfg.connection("frontend")
    assert conn is not None
    assert conn.repo == "https://github.com/o/r.git"
    assert conn.folder is None
    assert conn_setting(conn, "git_init", None) is None
