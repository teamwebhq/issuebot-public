"""A plugin owns its user-facing surface: its commands, its checks, its questions.

The behavioural boundary (which class runs the task) was settled earlier; this
is the other half of it. If a plugin's commands, doctor checks or setup
questions were declared by the generic CLI, adding a second environment would
mean editing `cli.py` — which is exactly what the boundary exists to prevent.
"""

from __future__ import annotations

from typing import Any, ClassVar

import pytest
import typer
from pydantic import BaseModel
from typer.testing import CliRunner

from issuebot import doctor_checks, plugins, wizard
from issuebot.cli import app
from issuebot.config import Connection
from issuebot.plugins.base import (
    EnvironmentPlugin,
    Plugin,
    SinkPlugin,
    SourcePlugin,
    WorkspacePlugin,
)
from issuebot.plugins.harnesses.fake.harness import FakeHarness
from issuebot.plugins.sinks.fake.sink import FakeSink
from issuebot.runner import sinks_for

runner = CliRunner()


def test_a_plugin_with_a_cli_is_mounted_under_its_name(plugin_tree) -> None:
    """Any plugin, not just the shipped ones: a throwaway tree is mounted the
    same way, which is what "addable by writing one folder" has to mean."""
    root = plugin_tree(
        gadget="""
        import typer

        from issuebot.plugins.base import Plugin

        cli = typer.Typer()

        @cli.command("ping")
        def ping() -> None:
            typer.echo("pong from gadget")

        PLUGIN = Plugin(name="gadget", cli=cli)
        """
    )

    mounted = typer.Typer()
    plugins.mount_cli(mounted, root=root, kinds=("widgets",))

    result = runner.invoke(mounted, ["gadget", "ping"])
    assert result.exit_code == 0, result.output
    assert "pong from gadget" in result.output


def test_git_mounts_the_worktree_and_clone_groups() -> None:
    """Those are git's commands, not issuebot's."""
    under_git = runner.invoke(app, ["git", "--help"])
    assert "worktree" in under_git.output
    assert "clone" in under_git.output

    # ...and so they are not the top-level CLI's own vocabulary: `issuebot
    # worktree list` is not a command, `issuebot git worktree list` is.
    assert runner.invoke(app, ["worktree", "list"]).exit_code == 2
    assert runner.invoke(app, ["clone", "list"]).exit_code == 2


def test_a_mounted_command_reports_a_bad_config_rather_than_a_traceback(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The "never a traceback" promise is the whole CLI's, not `cli.py`'s.

    Mounting is what makes that hard to keep: a plugin's command group is
    reached without passing through any core command, so it has to load the
    config itself, and three of them did it by hand — two spelling the
    "run init first" sentence again and all three letting a broken file out as
    a Rich traceback. `config.require_config` is the single answer; this holds
    a mounted command to it.
    """
    broken = tmp_path / "config.toml"
    broken.write_text("harness = 123\n")
    monkeypatch.setenv("ISSUEBOT_CONFIG", str(broken))

    result = runner.invoke(app, ["git", "worktree", "list"])

    assert result.exit_code == 1
    assert "Config error in" in result.output
    assert result.exception is None or isinstance(result.exception, SystemExit)


def test_doctor_runs_every_installed_plugins_checks(monkeypatch: pytest.MonkeyPatch) -> None:
    """A connection's workspace, environment and sinks each report for
    themselves — the command holds none of their prerequisites.

    Which environment and sink the connection selects is read off the registry
    rather than written down, so this stays a test of the *dispatch* and names
    no plugin. Every tool is missing (`which` answers None) and the repo is not
    one, so each selected plugin has something to complain about; what is
    asserted is that each one's complaint reaches the report."""
    monkeypatch.setattr("shutil.which", lambda name: None)

    environment = next(
        (name for name, p in plugins.all_of("environments").items() if p.doctor is not None), None
    )
    sink = next((name for name, p in plugins.all_of("sinks").items() if p.doctor is not None), None)

    # A test that would otherwise die of StopIteration on the day the last
    # environment or sink plugin is deleted, hiding a real regression behind an
    # unrelated error. There is nothing to dispatch to, so say so and stop.
    if environment is None or sink is None:
        pytest.skip("no installed environment or sink has a doctor hook to dispatch to")

    def report(**overrides: Any) -> list[str]:
        """Every warning `doctor` produces for this connection."""
        conn = Connection.model_validate(
            {
                "name": "p",
                "board": "b",
                # A local path that is not a repository: `git ls-remote` fails
                # without touching the network.
                "repo": "/nonexistent/not-a-repo.git",
                "git_init": "branch",
                "executor": environment,
                **overrides,
            }
        )
        warnings: list[str] = []
        doctor_checks.check_connection(conn, warn=warnings.append)
        return warnings

    warnings = report(sinks=[sink])
    reported = "\n".join(warnings).lower()

    assert "unreachable" in reported  # the git workspace spoke
    assert environment in reported  # ...and so did the environment

    # The sink is asserted by *subtraction* rather than by wording. A sink's
    # warning names the tool it needs ("needs 'gh' on path"), not itself, so
    # there is no string this test can look for without either learning a
    # plugin's prose or — as an earlier version did — matching a substring so
    # short ("gh") that it also matched "through" and passed on nothing.
    assert len(warnings) > len(report(sinks=[])), "declaring a sink added no check"


def test_a_plugins_check_that_raises_does_not_end_the_report(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A broken check is itself a finding, not a traceback — and every plugin
    after it is still asked."""

    def explode(conn: Connection, *, echo: Any) -> None:
        raise RuntimeError("its api is down")

    broken = Plugin(name="broken", doctor=explode)
    fine = Plugin(name="fine", doctor=lambda conn, *, echo: echo("Warning: from fine"))
    monkeypatch.setattr(
        doctor_checks, "plugins_in_play", lambda conn: {"broken": broken, "fine": fine}
    )

    warnings: list[str] = []
    doctor_checks.check_connection(Connection(name="p"), warn=warnings.append)

    assert "plugin 'broken' check failed: its api is down" in warnings[0]
    assert "Warning: from fine" in warnings[1]


class _RunsHere:
    """A stand-in environment class that runs work in this process, like the
    local plugin — the capability the wizard reads to derive `sandboxed`."""

    runs_in_process: ClassVar[bool] = True


class _RunsElsewhere:
    """A stand-in for a sandbox-shaped environment: a machine somewhere else."""

    runs_in_process: ClassVar[bool] = False


def _tickets_source(**overrides: Any) -> SourcePlugin:
    """A source double that identifies the connection through its own hook —
    the wizard asks *a* source rather than walking any particular one's
    hierarchy, which is why there is nothing to script for it."""
    kwargs: dict[str, Any] = {
        "name": "tickets",
        "source": object,
        "wizard": lambda client, *, choose: {"board": "b1", "name": "frontend"},
    }
    kwargs.update(overrides)
    return SourcePlugin(**kwargs)


def test_the_wizard_asks_the_selected_environment_for_its_questions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Picking an environment asks that plugin's own hook for its settings,
    rather than the wizard branching on which one was picked."""
    asked: list[str] = []

    def widget_wizard(*, choose_literal: Any) -> dict[str, Any]:
        asked.append("widget")
        return {"widget": {"region": "eu"}}

    widget = EnvironmentPlugin(name="widget", environment=_RunsHere, wizard=widget_wizard)
    # One patch, not three: `offered`, `names_of` and `get` all read through
    # `all_of`, so moving the whole registry at once is both shorter and free of
    # the half-patched state where an assertion can pass for the wrong reason.
    monkeypatch.setattr(
        plugins, "all_of", lambda kind: {"widget": widget} if kind == "environments" else {}
    )
    monkeypatch.setattr(wizard, "source_plugin", _tickets_source)

    # name=<enter>, then the core folder prompt (no workspace hook installed);
    # the environment is the only one installed, so it is auto-selected.
    monkeypatch.setattr(typer, "prompt", lambda *a, **kw: kw.get("default", "/tmp"))

    draft = wizard.run(object(), validate_folder=lambda folder, settings: None)

    assert asked == ["widget"]
    assert draft.settings["executor"] == "widget"
    assert draft.settings["widget"] == {"region": "eu"}


def test_the_wizard_asks_the_workspace_axis_for_its_own_questions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The workspace hook mirrors the environment one: the installed workspace
    plugin that declares a hook gathers its own keys, told the two neutral
    facts (`sandboxed`, from the environment's declared capability, and
    `changes`, from the source's settings hook) rather than reading any other
    plugin's answer."""
    told: dict[str, Any] = {}

    def bench_wizard(
        *, choose_literal: Any, prompt_repo: Any, prompt_folder: Any, sandboxed: bool, changes: bool
    ) -> dict[str, Any]:
        told.update(sandboxed=sandboxed, changes=changes)
        return {"bench_init": "copy"}

    bench = WorkspacePlugin(name="bench", workspace=object, wizard=bench_wizard)
    widget = EnvironmentPlugin(name="widget", environment=_RunsElsewhere)
    registry: dict[str, dict[str, Any]] = {"environments": {"widget": widget}}
    registry["workspaces"] = {"bench": bench}
    monkeypatch.setattr(plugins, "all_of", lambda kind: registry.get(kind, {}))

    source = _tickets_source(
        settings_wizard=lambda *, choose_literal, sandboxed: ({"tempo": "brisk"}, False)
    )
    monkeypatch.setattr(wizard, "source_plugin", lambda: source)
    monkeypatch.setattr(typer, "prompt", lambda *a, **kw: kw.get("default", ""))

    draft = wizard.run(object(), validate_folder=lambda folder, settings: None)

    assert told == {"sandboxed": True, "changes": False}
    assert draft.settings["bench_init"] == "copy"
    assert draft.settings["tempo"] == "brisk"


def test_a_sandboxed_environment_with_no_workspace_hook_is_refused(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """With no workspace plugin declaring wizard questions (removing the git
    plugin is a supported config) the wizard falls back to asking for a local
    folder — but a sandboxed environment runs each task on a fresh machine
    where that folder does not exist, and nothing downstream would reject it.
    The wizard must refuse with a sentence instead of saving a broken
    connection."""
    widget = EnvironmentPlugin(name="widget", environment=_RunsElsewhere)
    registry: dict[str, dict[str, Any]] = {"environments": {"widget": widget}, "workspaces": {}}
    monkeypatch.setattr(plugins, "all_of", lambda kind: registry.get(kind, {}))
    monkeypatch.setattr(wizard, "source_plugin", _tickets_source)
    monkeypatch.setattr(typer, "prompt", lambda *a, **kw: kw.get("default", "/tmp"))

    with pytest.raises(typer.Exit):
        wizard.run(object(), validate_folder=lambda folder, settings: None)

    assert "workspace plugin" in capsys.readouterr().err


def test_run_echoes_the_projects_repo_when_a_path_takes_it(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A wizard that silently skips a question it used to ask is confusing —
    when a workspace path actually takes the project's repo, `run()` must say
    which repo it took and that it came from the project."""

    def bench_wizard(
        *, choose_literal: Any, prompt_repo: Any, prompt_folder: Any, sandboxed: bool, changes: bool
    ) -> dict[str, Any]:
        return {"repo": prompt_repo()}

    bench = WorkspacePlugin(name="bench", workspace=object, wizard=bench_wizard)
    widget = EnvironmentPlugin(name="widget", environment=_RunsHere)
    registry: dict[str, dict[str, Any]] = {
        "environments": {"widget": widget},
        "workspaces": {"bench": bench},
    }
    monkeypatch.setattr(plugins, "all_of", lambda kind: registry.get(kind, {}))

    source = _tickets_source(
        wizard=lambda client, *, choose: {
            "board": "b1",
            "name": "frontend",
            "repo": "git@github.com:acme/web.git",
        }
    )
    monkeypatch.setattr(wizard, "source_plugin", lambda: source)
    monkeypatch.setattr(typer, "prompt", lambda *a, **kw: kw.get("default", "/tmp"))

    draft = wizard.run(object(), validate_folder=lambda folder, settings: None)

    out = capsys.readouterr().out
    assert "Repository: git@github.com:acme/web.git (from the project)" in out
    assert draft.settings["repo"] == "git@github.com:acme/web.git"


def test_run_does_not_announce_a_project_repo_the_chosen_path_discards(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The source may hand over a repo the workspace questions never use — the
    git hook's folder branch, or no workspace hook at all. Announcing
    "Repository: … (from the project)" and then saving a connection without it
    tells the user a lie; the announcement must only happen on a path that
    actually takes the repo."""
    widget = EnvironmentPlugin(name="widget", environment=_RunsHere)
    monkeypatch.setattr(
        plugins, "all_of", lambda kind: {"widget": widget} if kind == "environments" else {}
    )

    source = _tickets_source(
        wizard=lambda client, *, choose: {
            "board": "b1",
            "name": "frontend",
            "repo": "git@github.com:acme/web.git",
        }
    )
    monkeypatch.setattr(wizard, "source_plugin", lambda: source)
    monkeypatch.setattr(typer, "prompt", lambda *a, **kw: kw.get("default", "/tmp"))

    draft = wizard.run(object(), validate_folder=lambda folder, settings: None)

    assert "repo" not in draft.settings  # the folder path saved no repo…
    assert "from the project" not in capsys.readouterr().out  # …so none was promised


def test_run_says_nothing_about_the_repo_when_the_user_typed_it(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The echo names the project as the source of the repo, so it must stay
    silent when nobody but the user answered the question — saying "from the
    project" about a URL the user just typed would be worse than saying
    nothing at all."""

    def bench_wizard(
        *, choose_literal: Any, prompt_repo: Any, prompt_folder: Any, sandboxed: bool, changes: bool
    ) -> dict[str, Any]:
        return {"repo": prompt_repo()}

    bench = WorkspacePlugin(name="bench", workspace=object, wizard=bench_wizard)
    widget = EnvironmentPlugin(name="widget", environment=_RunsHere)
    registry: dict[str, dict[str, Any]] = {
        "environments": {"widget": widget},
        "workspaces": {"bench": bench},
    }
    monkeypatch.setattr(plugins, "all_of", lambda kind: registry.get(kind, {}))

    monkeypatch.setattr(wizard, "source_plugin", _tickets_source)
    monkeypatch.setattr(wizard, "_prompt_repo", lambda: "git@github.com:acme/typed.git")
    monkeypatch.setattr(typer, "prompt", lambda *a, **kw: kw.get("default", ""))

    draft = wizard.run(object(), validate_folder=lambda folder, settings: None)

    assert draft.settings["repo"] == "git@github.com:acme/typed.git"
    assert "from the project" not in capsys.readouterr().out


class _SinkSettings(BaseModel):
    """A settings model for a sink that has one, so the table has a shape."""

    tone: str = "plain"


def _tone_a_sink_was_built_with(table: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> str:
    """Build a stub sink through `sinks_for` and report the `tone` it was given.

    A stub rather than a shipped sink: what is under test is that a sink's own
    global table becomes its own constructor keywords, which is the same
    mechanism whoever the sink is. `all_of` is the whole registry as far as
    `plugins.get` and `plugins.names_of` are concerned, so patching it is enough.
    """
    built: list[str] = []

    class _RecordingSink(FakeSink):
        name: ClassVar[str] = "recorder"

        def __init__(self, *, harness: Any = None, tone: str = "plain") -> None:
            super().__init__(harness=harness)
            built.append(tone)

    plugin = SinkPlugin(name="recorder", sink=_RecordingSink, global_settings=_SinkSettings)
    monkeypatch.setattr(
        plugins, "all_of", lambda kind: {"recorder": plugin} if kind == "sinks" else {}
    )

    sinks_for(Connection(name="p", sinks=["recorder"]), FakeHarness(), table)
    return built[0]


def test_a_sink_is_built_with_its_own_global_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """A sink's global table reaches the sink as constructor keywords *it*
    declares, validated against its own model.

    The other half of the same boundary: core carries no field for a setting
    only one plugin reads. `summary_model` used to be a `RunnerContext` field,
    resolved from one sink's table on every run of every connection — including
    the connections that wired up no sink at all.
    """
    assert _tone_a_sink_was_built_with({"recorder": {"tone": "breezy"}}, monkeypatch) == "breezy"


def test_a_sink_whose_table_is_absent_gets_its_own_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A config that never wrote the table is the common case, not an error:
    the plugin's own model supplies the defaults."""
    assert _tone_a_sink_was_built_with({}, monkeypatch) == "plain"


class _RootSettings(BaseModel):
    """A global settings model for a stub plugin on a non-sink axis."""

    root: str = "default"


def _root_a_plugin_was_built_with(
    axis: str, factory: Any, tables: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> str:
    """Build a stub plugin on `axis` through its own factory and report the
    `root` its constructor was handed.

    One helper for both remaining axes because it is one mechanism: a factory
    that builds a plugin hands that plugin its *own* global table, validated
    against its own model, and nothing else. Patching `all_of` is enough —
    `plugins.get` and `plugins.names_of` both read through it — so the stub is
    the whole registry for that axis and no shipped plugin can answer instead.
    """
    built: list[str] = []

    class _Recorder:
        name: ClassVar[str] = "recorder"
        produces: ClassVar[frozenset[str]] = frozenset({"answer"})

        def __init__(self, root: str = "unset", **_ignored: Any) -> None:
            built.append(root)

    plugin = factory(_Recorder)
    monkeypatch.setattr(
        plugins, "all_of", lambda kind: {"recorder": plugin} if kind == axis else {}
    )

    _build_through_the_axis(axis, tables)
    return built[0]


def _build_through_the_axis(axis: str, tables: dict[str, Any]) -> None:
    """Call the production factory for `axis` with a context carrying `tables`."""
    from types import SimpleNamespace

    from issuebot.runner import source_for, workspace_for

    conn = Connection(name="p", folder="/tmp/p", source="recorder")
    ctx = SimpleNamespace(plugin_settings=tables)

    if axis == "workspaces":
        workspace_for(conn, ctx)  # ty: ignore[invalid-argument-type]
    else:
        source_for(object(), conn, ctx)  # ty: ignore[invalid-argument-type]


def test_a_workspace_is_built_with_its_own_global_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A workspace's global table reaches it as constructor keywords it declares.

    `worktree_root` and `clone_root` used to be `RunnerContext` fields, read off
    one workspace plugin's model by name — so building a context at all required
    that plugin to be installed and to have spelled both. Same defect as
    `summary_model`, same fix.
    """
    root = _root_a_plugin_was_built_with(
        "workspaces",
        lambda cls: WorkspacePlugin(
            name="recorder", workspace=cls, settings=_RootSettings, global_settings=_RootSettings
        ),
        {"recorder": {"root": "/mnt/w"}},
        monkeypatch,
    )
    assert root == "/mnt/w"


def test_a_source_is_built_with_its_own_global_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The same for the source axis, where `api_url`/`mcp_url`/`pat` were three
    more `RunnerContext` fields — so a second source plugin could only be built
    at all by declaring exactly those three names."""
    root = _root_a_plugin_was_built_with(
        "sources",
        lambda cls: SourcePlugin(name="recorder", source=cls, global_settings=_RootSettings),
        {"recorder": {"root": "https://board"}},
        monkeypatch,
    )
    assert root == "https://board"


def test_a_plugin_whose_table_is_absent_gets_its_own_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Nothing configured is the common case on these axes too — a connection
    that never wrote `[git]` still has to resolve a workspace."""
    root = _root_a_plugin_was_built_with(
        "workspaces",
        lambda cls: WorkspacePlugin(
            name="recorder", workspace=cls, settings=_RootSettings, global_settings=_RootSettings
        ),
        {},
        monkeypatch,
    )
    assert root == "default"
