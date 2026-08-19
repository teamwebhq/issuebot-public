from pathlib import Path

import pytest
from pydantic import BaseModel

from conftest import config, connection, in_process_environment
from issuebot import plugins
from issuebot.config import (
    DEFAULT_UPDATE_COMMAND,
    Config,
    Connection,
    SinkRef,
    conn_setting,
    executor_name,
    harness_for,
    harness_name,
    load_config,
    plugin_tables,
    save_config,
    source_plugin,
)
from issuebot.context import RunnerContext
from issuebot.plugins.base import EnvironmentPlugin, HarnessPlugin, Plugin, SourcePlugin
from issuebot.plugins.harnesses.base import Harness
from issuebot.process import REAL, RecordingProcess

# Stand-ins for the two settings shapes a plugin can have: flat keys written
# straight onto the connection, and a table named after the plugin. Made up
# here rather than borrowed from an installed plugin, so what is under test is
# the shape rather than any one plugin's model.


class _GitSettings(BaseModel):
    repo: str
    git_init: str


class _WidgetSettings(BaseModel):
    environment_id: str


_GIT = Plugin(name="git", settings=_GitSettings, flat=True)
_WIDGET = Plugin(name="widget", settings=_WidgetSettings, flat=False)


def test_a_flat_plugins_keys_are_readable_off_a_connection():
    conn = Connection(name="p", repo="git@x:o/r.git", git_init="branch")
    assert conn.settings_for(_GIT) == {"repo": "git@x:o/r.git", "git_init": "branch"}


def test_a_table_plugins_settings_are_readable_off_a_connection():
    conn = Connection(name="p", folder="/tmp/p", widget={"environment_id": "e"})
    assert conn.settings_for(_WIDGET) == {"environment_id": "e"}


def test_global_plugin_settings_are_readable_off_the_config():
    assert Config(git={"clone_root": "/mnt/c"}).settings_for(_GIT) == {"clone_root": "/mnt/c"}


def test_every_plugins_table_is_readable_at_once():
    """`plugin_tables` is `settings_for` for the whole config: the form a
    factory needs when it is handing several plugins their own settings and
    knows none of their names. Core fields are not tables and must not appear —
    a plugin called `harness` or `connections` would collide, which is why the
    filter is on the value's shape rather than on a list of names to skip."""
    cfg = Config(harness="fake", git={"clone_root": "/mnt/c"}, widget={"environment_id": "e"})

    assert plugin_tables(cfg) == {
        "git": {"clone_root": "/mnt/c"},
        "widget": {"environment_id": "e"},
    }


def test_a_context_carries_every_plugins_settings_table():
    """The mechanism that replaced `RunnerContext.summary_model`: the settings a
    plugin reads travel with the run under that plugin's own name, and core
    keeps no field of its own for any of them. If this stops carrying the
    tables, a sink built mid-run silently gets its model's defaults instead of
    what the config says — with no error anywhere."""
    cfg = config(widget={"environment_id": "e"})

    ctx = RunnerContext.from_config(cfg)

    assert ctx.plugin_settings["widget"] == {"environment_id": "e"}
    # ...including the source's own table, which is where the run's endpoints
    # came from — proof this is every plugin's, not a curated few.
    assert ctx.plugin_settings[source_plugin().name]["api_url"] == "https://api"


def test_the_config_keeps_only_what_is_core():
    """If this grows, ask which plugin the new field belongs to first."""
    assert set(Config.model_fields) == {
        "harness",
        "task_timeout_minutes",
        "update_command",
        "max_concurrent",
        "connections",
    }


def test_the_connection_keeps_only_what_is_core():
    """If this grows, ask which plugin the new field belongs to first."""
    assert set(Connection.model_fields) == {"name", "source", "folder", "executor", "sinks"}


def test_a_sink_list_of_names_means_all_required():
    conn = Connection(name="p", folder="/tmp/p", sinks=["fake"])
    assert conn.sinks == [SinkRef(name="fake", required=True)]


def test_a_sink_can_be_declared_best_effort():
    conn = Connection(name="p", folder="/tmp/p", sinks=[{"name": "slack", "required": False}])
    assert conn.sinks[0].required is False


def test_a_config_round_trips_through_toml(tmp_path: Path):
    cfg = config(
        connections=[
            connection(
                name="w",
                board="b",
                folder=None,
                repo="git@x:o/r.git",
                git_init="branch",
                sinks=["fake"],
            )
        ]
    )
    path = tmp_path / "config.toml"
    save_config(cfg, path)
    loaded = load_config(path)
    assert loaded is not None
    assert loaded.connection("w").settings_for(_GIT)["git_init"] == "branch"
    assert loaded.connection("w").settings_for(_GIT)["repo"] == "git@x:o/r.git"
    assert loaded.connection("w").sinks[0].name == "fake"


def test_a_config_using_every_flag_owned_key_round_trips_unchanged(tmp_path: Path):
    """The on-disk vocabulary is frozen: a connection spelling every key the
    dedicated `connect` flags write — the issuebear source's `board`/`done`/
    `confirm`/`mode` and the git workspace's `repo`/`git_init`/`branch_prefix`/
    `update_base` — must save, load and read back exactly as written, with
    `conn_setting` answering each value typed by the owning plugin's model."""
    written = {
        "name": "w",
        "board": "b-7",
        "repo": "git@x:o/r.git",
        "git_init": "worktree",
        "branch_prefix": "bot/",
        "update_base": "merge",
        "done": "complete",
        "confirm": False,
        "mode": "respond",
        "executor": in_process_environment(),
    }
    cfg = config(connections=[Connection.model_validate(written)])
    path = tmp_path / "config.toml"
    save_config(cfg, path)

    loaded = load_config(path)
    assert loaded is not None
    conn = loaded.connection("w")
    assert conn is not None

    for key, value in written.items():
        if key in ("name", "executor"):
            continue
        assert conn_setting(conn, key) == value, key


def test_connection_lookup_returns_none_when_missing():
    assert Config().connection("nope") is None


def test_load_missing_path_returns_none(tmp_path: Path):
    assert load_config(tmp_path / "absent.toml") is None


def test_saved_file_is_0600(tmp_path: Path):
    path = tmp_path / "config.toml"
    save_config(Config(), path)
    assert oct(path.stat().st_mode)[-3:] == "600"


def test_core_config_defaults():
    cfg = Config()
    assert cfg.harness is None  # resolved at use, see `harness_name`
    assert cfg.task_timeout_minutes is None
    assert cfg.update_command == DEFAULT_UPDATE_COMMAND
    assert cfg.max_concurrent == 1
    assert cfg.connections == []


def test_connection_name_optional():
    assert Connection(folder="/tmp/t").name is None


def test_connection_key_raises_when_nameless():
    with pytest.raises(RuntimeError):
        _ = Connection(folder="/tmp/t").key


def test_connection_local_folder_raises_when_unset():
    with pytest.raises(RuntimeError):
        _ = Connection(name="p").local_folder


# --- harness_for ---------------------------------------------------------------


def test_harness_for_returns_the_right_types():
    """`cfg.harness` resolves through the registry, so every installed harness
    plugin builds its own implementation — no hardcoded name switch here and
    none in `harness_for` either.

    Asked of the registry rather than a written-down list: naming the harnesses
    would put core's only knowledge of them in a core test file, which is the
    leak the plugin boundary exists to prevent."""
    for name, plugin in plugins.all_of("harnesses").items():
        assert isinstance(harness_for(Config(harness=name)), plugin.harness)


def test_harness_for_unknown_harness_raises():
    with pytest.raises(plugins.UnknownPlugin, match="unknown harness"):
        harness_for(Config(harness="nope"))


# --- source_plugin -------------------------------------------------------------
#
# There is no top-level `source` key, so an install-wide caller (`init`,
# `doctor`, the supervisor) asks for "the source" and gets the one installed.
# Both ways that can fail to be a single answer are checked here: neither is
# reachable from the shipped plugin set today, and the failure mode of the
# fallback silently picking one — an arbitrary source chosen by sort order,
# running the wrong board's work — is the kind nothing else would catch.


def _installed(monkeypatch, *names: str, kind: str = "sources") -> None:
    """Pretend exactly these plugins of one kind are installed — registry and all.

    Patching `all_of` rather than `names_of` is the whole point, and the first
    version of these tests got it wrong. `names_of` and `get` both read through
    `all_of`, so a name this returns really does resolve to a plugin.

    Patch only `names_of` and the two disagree: `get` still looks in the real
    registry, where an invented name is missing, so it raises its *own*
    `UnknownPlugin` — whose message is built from `', '.join(names_of(kind))`,
    i.e. the patched names. A guard that wrongly picked a source would still
    raise, with a message matching what the test asserted on, from a code path
    that is not the one under test. Ablating the guard left all four tests
    green. Consistency here is what makes the assertions mean anything.
    """
    make, field = {
        "harnesses": (HarnessPlugin, "harness"),
        "environments": (EnvironmentPlugin, "environment"),
    }.get(kind, (SourcePlugin, "source"))
    installed = {name: make(name=name, **{field: object}) for name in names}
    monkeypatch.setattr(plugins, "all_of", lambda asked: installed if asked == kind else {})


def test_the_only_installed_source_is_the_default():
    """A caller naming no source gets the one there is — against the real
    registry, so this also asserts the shipped set has exactly one."""
    assert plugins.names_of("sources") == [source_plugin().name]


def test_a_named_source_wins_over_the_default(monkeypatch):
    """Naming one skips the count entirely — a connection may say which source
    it reads even on an install that has several, which is the whole reason
    `Connection.source` exists."""
    _installed(monkeypatch, "issues", "tickets")

    assert source_plugin("tickets").name == "tickets"


def test_no_source_installed_is_a_sentence_not_a_crash(monkeypatch):
    """Deleting the last source plugin must name what is installed rather than
    reach for one that is not there."""
    _installed(monkeypatch)

    with pytest.raises(plugins.UnknownPlugin, match="no source named, and 0 are installed"):
        source_plugin()


def test_several_sources_installed_refuses_to_guess(monkeypatch):
    """The day a second source ships, an install-wide caller must be told to say
    which — picking whichever sorted first would silently run one board's work
    against another's settings.

    Both installed names resolve for real (see `_installed`), so a guard that
    picked one would *succeed* here rather than raise for an unrelated reason.
    That is what makes this fail when the branch is removed.
    """
    _installed(monkeypatch, "issues", "tickets")

    with pytest.raises(plugins.UnknownPlugin, match="no source named, and 2 are installed"):
        source_plugin()


# --- harness_name ------------------------------------------------------------
#
# The same shape as the source tests above, and for the same reason: there is no
# privileged harness any more, so "which one" is a resolution with a guard rather
# than a default someone wrote down.


def test_a_named_harness_wins_over_the_default(monkeypatch):
    _installed(monkeypatch, "one", "two", kind="harnesses")

    assert harness_name(Config(harness="two")) == "two"


def test_the_only_installed_harness_needs_no_naming(monkeypatch):
    """A config on a single-harness install may leave it out — there is exactly
    one thing it could have meant."""
    _installed(monkeypatch, "only", kind="harnesses")

    assert harness_name(Config()) == "only"


def test_no_harness_installed_is_a_sentence_not_a_crash(monkeypatch):
    _installed(monkeypatch, kind="harnesses")

    with pytest.raises(plugins.UnknownPlugin, match="no harness named, and 0 are installed"):
        harness_name(Config())


def test_several_harnesses_installed_refuses_to_guess(monkeypatch):
    """The shipped state: with more than one harness there is no non-arbitrary
    answer, so the config is told to say which and given the list.

    Both names resolve for real (see `_installed`), so a guard that picked one
    would succeed here rather than raise for an unrelated reason — which is what
    makes this go red when the branch is removed."""
    _installed(monkeypatch, "one", "two", kind="harnesses")

    with pytest.raises(plugins.UnknownPlugin, match="no harness named, and 2 are installed"):
        harness_name(Config())


# --- executor_name ------------------------------------------------------------
#
# The third instance of the same shape, for the same reason: `executor` used to
# default to one plugin's name, so that plugin was the one nobody could delete.
# The environment axis differs from the harness axis in one way only — the
# in-sandbox worker needs "the one that runs work here", which is a *capability*
# and is resolved by `runner.in_process_environment`, deliberately not by this.


def test_a_named_environment_wins_over_the_default(monkeypatch):
    _installed(monkeypatch, "here", "there", kind="environments")

    assert executor_name(Connection(name="p", executor="there")) == "there"


def test_the_only_installed_environment_needs_no_naming(monkeypatch):
    """A connection on a single-environment install may leave it out — there is
    exactly one thing it could have meant."""
    _installed(monkeypatch, "only", kind="environments")

    assert executor_name(Connection(name="p")) == "only"


def test_no_environment_installed_is_a_sentence_not_a_crash(monkeypatch):
    _installed(monkeypatch, kind="environments")

    with pytest.raises(plugins.UnknownPlugin, match="no environment named, and 0 are installed"):
        executor_name(Connection(name="p"))


def test_several_environments_installed_refuses_to_guess(monkeypatch):
    """The shipped state. Running a task on this machine and running it in a paid
    cloud sandbox are not interchangeable, so silence has nothing it can honestly
    mean and the connection is told to say which.

    Both names resolve for real (see `_installed`), so a guard that picked one
    would succeed here rather than raise for an unrelated reason."""
    _installed(monkeypatch, "here", "there", kind="environments")

    with pytest.raises(plugins.UnknownPlugin, match="no environment named, and 2 are installed"):
        executor_name(Connection(name="p"))


def test_harness_for_passes_command_and_proc(monkeypatch):
    """The `command` override in a harness's own table reaches its constructor,
    along with the process adapter the caller wired.

    Driven off a stub harness rather than an installed one: what is under test
    is that `harness_for` forwards both, not that any particular plugin's
    constructor takes them."""
    spawn = RecordingProcess()
    monkeypatch.setattr(
        plugins,
        "all_of",
        lambda kind: (
            {"stub": HarnessPlugin(name="stub", harness=_RecordingHarness)}
            if kind == "harnesses"
            else {}
        ),
    )
    cfg = Config.model_validate({"harness": "stub", "stub": {"command": "my-agent"}})

    harness = harness_for(cfg, proc=spawn)

    assert harness.command == "my-agent"
    assert harness.proc is spawn


class _RecordingHarness(Harness):
    """A harness that records how it was constructed and runs nothing."""

    name = "stub"

    def __init__(self, *, command: str = "default", proc: object = REAL) -> None:
        self.command = command
        self.proc = proc

    def launch(self, spec, reporter, cancel=None):  # pragma: no cover - never run
        raise NotImplementedError

    def summarize(self, diff, *, context, model, folder):  # pragma: no cover - never run
        raise NotImplementedError


def test_harness_for_defaults_to_real_process_when_unspecified():
    """The default `proc` is the module-wide REAL adapter, not a fresh one each
    call — constructing a harness never needs a caller-supplied process double
    in production."""
    harness = harness_for(Config(harness="fake"))
    assert harness._proc is REAL  # noqa: SLF001 - only way to see what was wired
