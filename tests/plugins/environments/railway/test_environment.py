"""Tests for the Railway adapter — the only place Railway is allowed to appear.

Everything vendor-shaped lives here: the CLI's argv forms, which environment
variable each kind of token goes in, and the ``${{shared.NAME}}`` syntax the
platform resolves at boot. The controller that drives this is tested in
test_sandbox.py, which mentions Railway nowhere.

Plus the one claim about *core* that only this file can make: that a connection
naming this environment actually resolves to it. Core cannot assert that
without learning the plugin's name.
"""

from __future__ import annotations

import json
import shlex

import pytest
from typer.testing import CliRunner

from conftest import VERSION, FakeApi, completed, config, connection, ctx, sandbox_connection
from issuebot import cli, release
from issuebot.config import validate_config
from issuebot.plugins.environments.railway.environment import (
    TEMPLATE,
    RailwayEnvironment,
    RailwayError,
    RailwayProvider,
)
from issuebot.plugins.environments.railway.settings import TOKEN_VARS, ambient_token, token_env
from issuebot.plugins.harnesses.fake.harness import FakeHarness
from issuebot.process import Completed, RecordingProcess
from issuebot.runner import wire
from issuebot.sandbox_protocol import update_argv


def _provider(proc: RecordingProcess | None = None, **kwargs) -> RailwayProvider:
    kwargs.setdefault("auth", {})
    kwargs.setdefault("environment_id", "env-1")
    return RailwayProvider(proc=proc or RecordingProcess(), **kwargs)


# ---------------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------------


def test_a_project_token_goes_in_the_project_variable():
    assert token_env("tok", "project")[TOKEN_VARS["project"]] == "tok"


def test_an_account_token_goes_in_the_account_variable():
    assert token_env("tok", "account")[TOKEN_VARS["account"]] == "tok"


def test_configuring_a_token_explicitly_unsets_the_other_one():
    """An ambient token left in the shell for a different Railway project must
    not shadow the credential this connection asked for — and the process
    adapter reads an empty value as "remove this variable"."""
    env = token_env("tok", "project")
    assert env[TOKEN_VARS["account"]] == ""


def test_no_token_means_inherit_whatever_the_runner_has():
    assert token_env(None) == {}
    assert token_env("") == {}


def test_the_ambient_token_may_be_either_variable(monkeypatch):
    monkeypatch.delenv(TOKEN_VARS["project"], raising=False)
    monkeypatch.setenv(TOKEN_VARS["account"], "acct")
    assert ambient_token() == "acct"

    monkeypatch.setenv(TOKEN_VARS["project"], "proj")
    assert ambient_token() == "proj"


def test_no_ambient_token_at_all(monkeypatch):
    for var in TOKEN_VARS.values():
        monkeypatch.delenv(var, raising=False)
    assert ambient_token() is None


def test_a_connection_naming_this_environment_resolves_to_it():
    """`executor = "railway"` must dispatch to *this* environment.

    The one claim that no conformance test can make and no other plugin's suite
    can make for it: `test_conformance.py` builds each environment directly and
    never goes through `wire`, and `local`'s suite covers only the
    default. Without this, `environment_for` could ignore `executor` entirely
    and run every sandbox connection on the listener's own machine — pushing to
    the wrong repo, from the wrong working copy, with the operator's own
    credentials — and the whole suite would still pass.

    It lives here rather than in core because core naming this plugin is the
    leak the deletion test exists to catch; deleting the plugin deletes its
    dispatch test with it, which is correct."""
    environment = wire(FakeApi(), FakeHarness(), railway_connection(), ctx()).environment

    assert isinstance(environment, RailwayEnvironment)
    assert environment.name == "railway"


def railway_connection(**overrides):
    """A sandbox connection that names *this* environment.

    Lives here rather than in the shared conftest: core's doubles must not name
    a plugin, or deleting the plugin would take core's test suite with it."""
    return sandbox_connection(executor="railway", **overrides)


def test_a_connection_builds_a_provider_carrying_its_own_credential():
    conn = railway_connection(
        railway={"environment_id": "env-9", "token": "tok", "network": "private"}
    )
    provider = RailwayProvider.for_connection(conn)
    assert provider.name == "railway"
    assert provider.supports_checkpoints is True


def test_a_connection_with_no_railway_table_still_builds_a_provider():
    """An environment that cannot be *constructed* has nowhere to report a
    failed run from, so a missing table falls back to the ambient credential
    rather than raising — `validate_config` is what guarantees a real table for
    any connection that actually selects this environment."""
    provider = RailwayProvider.for_connection(connection())
    assert provider.name == "railway"


def test_a_connection_selecting_railway_without_a_table_is_rejected_at_load():
    """The other half of the test above, and the reason it is safe.

    `for_connection` deliberately never raises, so nothing at run time objects
    to a railway connection with no `[connections.railway]` — it would quietly
    run against whatever `RAILWAY_TOKEN`/`RAILWAY_API_TOKEN` happened to be in
    the runner's environment, in whatever Railway environment that credential
    defaults to. Validation is the only thing standing between a user and that,
    and until now nothing pinned it.

    Lives here rather than in `tests/test_config_validation.py` because naming
    this plugin outside its own directory is what makes it undeletable."""
    problems = validate_config(config(connections=[connection(executor="railway")]))

    assert any("environment_id" in problem for problem in problems), problems


# ---------------------------------------------------------------------------
# Secrets
# ---------------------------------------------------------------------------


def test_secrets_are_shared_variable_references_not_values():
    """Their values never pass through this process; Railway resolves each
    reference when it boots the sandbox."""
    secrets = _provider().secret_env()
    assert secrets["ANTHROPIC_API_KEY"] == "${{shared.ANTHROPIC_API_KEY}}"
    assert secrets["GH_TOKEN"] == "${{shared.GH_TOKEN}}"


# ---------------------------------------------------------------------------
# Sandboxes
# ---------------------------------------------------------------------------


def test_creating_a_sandbox_returns_its_id():
    proc = RecordingProcess(replies={"sandbox create": completed(out=json.dumps({"id": "sbx_9"}))})
    assert _provider(proc).create(env={}) == "sbx_9"


def test_a_cold_boot_uses_the_shared_tools_template():
    proc = RecordingProcess(replies={"sandbox create": completed(out='{"id": "s"}')})
    _provider(proc).create(env={})

    argv = proc.calls[0]
    assert "--template" in argv and argv[argv.index("--template") + 1] == TEMPLATE
    assert "--checkpoint" not in argv


def test_booting_from_a_checkpoint_replaces_the_template():
    """The checkpoint already carries a filesystem; layering a template over it
    would be meaningless."""
    proc = RecordingProcess(replies={"sandbox create": completed(out='{"id": "s"}')})
    _provider(proc).create(env={}, checkpoint="task-t1")

    argv = proc.calls[0]
    assert argv[argv.index("--checkpoint") + 1] == "task-t1"
    assert "--template" not in argv


def test_the_environment_is_baked_in_at_create_time():
    """A sandbox's environment cannot be changed once it is running."""
    proc = RecordingProcess(replies={"sandbox create": completed(out='{"id": "s"}')})
    _provider(proc).create(env={"A": "1", "B": "2"})

    argv = proc.calls[0]
    assert "A=1" in argv and "B=2" in argv


def test_the_environment_is_pinned_on_the_invocation():
    proc = RecordingProcess(replies={"sandbox create": completed(out='{"id": "s"}')})
    _provider(proc, environment_id="env-7").create(env={})

    argv = proc.calls[0]
    assert argv[argv.index("--environment") + 1] == "env-7"


def test_a_private_network_is_requested_only_when_asked_for():
    proc = RecordingProcess(replies={"sandbox create": completed(out='{"id": "s"}')})
    _provider(proc, private_network=True).create(env={})
    assert "--private-network" in proc.calls[0]

    proc = RecordingProcess(replies={"sandbox create": completed(out='{"id": "s"}')})
    _provider(proc).create(env={})
    assert "--private-network" not in proc.calls[0]


def test_a_create_that_returns_no_id_is_an_error():
    proc = RecordingProcess(replies={"sandbox create": completed(out="{}")})
    with pytest.raises(RailwayError, match="no sandbox id"):
        _provider(proc).create(env={})


def test_a_failed_command_becomes_a_railway_error():
    proc = RecordingProcess(replies={"sandbox destroy": Completed([], 1, err="not found")})
    with pytest.raises(RailwayError, match="not found"):
        _provider(proc).destroy("sbx_1")


def test_a_missing_railway_cli_is_a_railway_error_not_a_traceback():
    """The real adapter reports a program it could not start as a failed run, so
    a machine without the CLI gets the error callers already handle."""
    provider = RailwayProvider(auth={}, environment_id="e")
    with pytest.raises(RailwayError):
        provider.destroy("sbx_1")


def test_reading_a_file_cats_it_inside_the_sandbox():
    proc = RecordingProcess(replies={"cat": completed(out='{"status":"done"}')})
    assert _provider(proc).read_file("sbx_1", "/tmp/result.json") == '{"status":"done"}'


def test_execing_streams_the_command_output():
    proc = RecordingProcess(lines=["one", "two"])
    seen: list[str] = []

    code = _provider(proc).exec_stream("sbx_1", ["issuebot", "run-one"], on_line=seen.append)

    assert code == 0
    assert seen == ["one", "two"]
    assert proc.calls[0][:5] == ["railway", "sandbox", "exec", "--id", "sbx_1"]
    assert proc.calls[0][-2:] == ["issuebot", "run-one"]


# ---------------------------------------------------------------------------
# Checkpoints
# ---------------------------------------------------------------------------


def test_listing_checkpoints_returns_their_names():
    payload = json.dumps([{"name": "project-p"}, {"name": "task-t1"}])
    proc = RecordingProcess(replies={"checkpoint list": completed(out=payload)})
    assert _provider(proc).list_checkpoints() == ["project-p", "task-t1"]


def test_no_checkpoints_at_all():
    proc = RecordingProcess(replies={"checkpoint list": completed(out="")})
    assert _provider(proc).list_checkpoints() == []


def test_creating_and_deleting_a_checkpoint():
    proc = RecordingProcess()
    provider = _provider(proc)

    provider.create_checkpoint("sbx_1", "task-t1")
    provider.delete_checkpoint("task-t1")

    assert proc.calls[0][-3:] == ["create", "sbx_1", "task-t1"]
    assert proc.calls[1][-2:] == ["delete", "task-t1"]


# ---------------------------------------------------------------------------
# Project administration
# ---------------------------------------------------------------------------


def test_building_the_template_installs_the_declared_packages():
    proc = RecordingProcess()
    _provider(proc).build_template()

    argv = proc.calls[0]
    assert argv[:5] == ["railway", "sandbox", "template", "build", TEMPLATE]
    assert "--package" in argv
    assert "git" in argv and "gh" in argv


def test_the_template_pins_the_issuebot_that_built_it():
    """The template carries the controller's exact released issuebot version."""
    proc = RecordingProcess()
    _provider(proc).build_template()

    argv = proc.calls[0]
    assert argv[argv.index("--run") + 1] == shlex.join(update_argv(VERSION))


def test_a_template_cannot_be_built_from_a_source_install(monkeypatch):
    monkeypatch.setattr(release, "is_installed_wheel", lambda: False)

    with pytest.raises(RailwayError, match="released issuebot wheel") as raised:
        _provider().build_template()

    assert release.INSTALL_COMMAND in str(raised.value)


def test_the_rebuild_command_quoted_at_users_is_one_the_cli_has():
    """The controller prints this when a template is stale, so it has to be a
    command that exists — a rename here reaches the user as advice that fails."""
    words = RailwayProvider.rebuild_command.split()
    assert words[0] == "issuebot"

    result = CliRunner().invoke(cli.app, [*words[1:], "--help"])
    assert result.exit_code == 0


# ---------------------------------------------------------------------------
# Which executable is run
# ---------------------------------------------------------------------------


def test_the_connection_chooses_which_executable_the_cli_calls():
    """A runner started as a service gets a minimal PATH, so a connection may
    name an absolute path to the CLI instead of a name resolved on PATH."""
    proc = RecordingProcess(replies={"sandbox create": completed(out='{"id": "s"}')})
    conn = railway_connection(railway={"environment_id": "e", "command": "/opt/rw/bin/railway"})

    provider = RailwayProvider.for_connection(conn, proc)
    provider.create(env={})
    provider.exec_stream("s", ["issuebot", "run-one"], on_line=lambda _line: None)

    assert [call[0] for call in proc.calls] == ["/opt/rw/bin/railway"] * 2


def test_an_unconfigured_executable_is_still_the_name_on_path():
    proc = RecordingProcess(replies={"sandbox create": completed(out='{"id": "s"}')})
    conn = railway_connection(railway={"environment_id": "e"})

    RailwayProvider.for_connection(conn, proc).create(env={})

    assert proc.calls[0][0] == "railway"
