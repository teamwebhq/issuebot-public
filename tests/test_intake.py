"""Tests for taking on a new connection.

This used to be a seventeen-parameter Typer command whose every failure was
``typer.Exit``, so it could only be driven through ``CliRunner``. The rules are
the same; they are now callable.
"""

from __future__ import annotations

import pytest

from conftest import config, connection, ctx, in_process_environment
from issuebot import intake
from issuebot.config import conn_setting
from issuebot.plugins.sources.base import ConnectionConflict
from issuebot.runner import workspace_for


class StubClient:
    """The one board call intake makes."""

    def __init__(self, *, response=None, error: Exception | None = None) -> None:
        self.response = response if response is not None else {}
        self.error = error
        self.calls: list[tuple] = []

    def connect(self, board_id, name=None, *, install_id=None):
        self.calls.append((board_id, name, install_id))
        if self.error is not None:
            raise self.error
        return self.response


@pytest.fixture
def repo_dir(tmp_path):
    """A folder that passes the intrinsic folder checks."""
    folder = tmp_path / "work"
    folder.mkdir()
    return folder


def _draft(**settings) -> intake.Draft:
    """A draft as the CLI or the wizard would gather it.

    Both of those always answer "where do tasks run" — the wizard asks, the flag
    is passed on — so a draft that named no environment would be testing a shape
    neither path produces, and would be refused for that rather than for
    whatever the test is about."""
    settings.setdefault("executor", in_process_environment())
    return intake.Draft(name="web", board="b-1", settings=settings)


# ---------------------------------------------------------------------------
# What it refuses
# ---------------------------------------------------------------------------


def test_a_connection_with_no_repo_and_no_folder_has_nowhere_to_work():
    with pytest.raises(intake.IntakeError, match="folder is required"):
        intake.build(config(), _draft(git_init="branch"))


def test_a_repo_must_look_like_a_git_url():
    with pytest.raises(intake.IntakeError, match="https/ssh git URL"):
        intake.build(config(), _draft(repo="not-a-url", git_init="branch"))


@pytest.mark.parametrize("url", ["https://x/r.git", "git@github.com:o/r.git", "ssh://git@x/r.git"])
def test_the_accepted_repo_url_shapes(url):
    conn = intake.build(config(), _draft(repo=url, git_init="branch"))
    assert conn.repo == url
    assert conn.folder is None  # a connection that clones stores no folder


def test_a_clone_can_be_worked_in_directly():
    """The combination the one four-valued setting could not express: a fresh
    copy per task that cuts no branch, for work that only has to read."""
    conn = intake.build(config(), _draft(repo="https://x/r.git"))

    assert conn.repo == "https://x/r.git"
    assert conn_setting(conn, "git_init", None) is None


def test_anything_other_than_a_clone_needs_a_folder():
    with pytest.raises(intake.IntakeError, match="folder is required"):
        intake.build(config(), _draft(git_init="worktree"))


def test_the_folder_must_exist(tmp_path):
    with pytest.raises(intake.IntakeError, match="existing absolute directory"):
        intake.build(config(), _draft(folder=str(tmp_path / "nope")))


def test_the_folder_must_be_absolute():
    with pytest.raises(intake.IntakeError, match="existing absolute directory"):
        intake.build(config(), _draft(folder="relative/path"))


def test_a_strategy_needs_a_git_repo(repo_dir):
    with pytest.raises(intake.IntakeError, match="requires a git repo"):
        intake.build(config(), _draft(folder=str(repo_dir), git_init="worktree"))


def test_one_board_may_not_have_two_connections(repo_dir):
    """Both would claim the same work."""
    cfg = config(connections=[connection(name="other", board="b-1")])
    with pytest.raises(intake.IntakeError, match="already has a connection to board b-1"):
        intake.build(cfg, _draft(folder=str(repo_dir)))


def test_reconnecting_the_same_name_to_the_same_board_is_fine(repo_dir):
    """Changing a connection's settings is not a clash with itself."""
    cfg = config(connections=[connection(name="web", board="b-1")])
    assert intake.build(cfg, _draft(folder=str(repo_dir))).name == "web"


# ---------------------------------------------------------------------------
# What it does
# ---------------------------------------------------------------------------


def test_it_registers_then_persists(repo_dir, tmp_path):
    cfg = config()
    client = StubClient(response={})
    path = tmp_path / "config.toml"

    result = intake.finalize(cfg, _draft(folder=str(repo_dir)), client, path=path)

    assert result.registered is True
    assert client.calls[0][:2] == ("b-1", "web")
    assert path.exists()
    assert cfg.connection("web") is not None


def test_a_server_warning_is_passed_back_not_swallowed(repo_dir, tmp_path):
    client = StubClient(response={"warning": "board is archived"})
    result = intake.finalize(
        config(), _draft(folder=str(repo_dir)), client, path=tmp_path / "c.toml"
    )
    assert "board is archived" in result.warnings


def test_a_conflict_aborts_without_writing(repo_dir, tmp_path):
    """Saving a connection the server has already refused would leave one that
    can never claim anything."""
    path = tmp_path / "config.toml"
    client = StubClient(error=ConnectionConflict("b-1"))

    with pytest.raises(intake.IntakeError, match="already connected"):
        intake.finalize(config(), _draft(folder=str(repo_dir)), client, path=path)

    assert not path.exists()


def test_an_unreachable_server_still_saves_the_connection(repo_dir, tmp_path):
    """A network blip must not lose the setup the user just did — `issuebot
    listen` reconciles it on startup."""
    cfg = config()
    client = StubClient(error=RuntimeError("connection refused"))
    path = tmp_path / "config.toml"

    result = intake.finalize(cfg, _draft(folder=str(repo_dir)), client, path=path)

    assert result.registered is False
    assert any("retry" in w for w in result.warnings)
    assert path.exists()
    assert cfg.connection("web") is not None


def test_a_refused_connect_is_reported_without_a_stack_trace(repo_dir, tmp_path, caplog):
    """`connect` is a command somebody is sitting in front of, and this failure
    is handled — the caller prints a sentence saying what happens next. Logging
    it with a traceback puts that sentence underneath something that reads as a
    crash, and scrolls the user's own answers away."""
    client = StubClient(error=RuntimeError("422: nope"))
    path = tmp_path / "config.toml"

    with caplog.at_level("WARNING"):
        intake.finalize(config(), _draft(folder=str(repo_dir)), client, path=path)

    record = next(r for r in caplog.records if "server connect failed" in r.getMessage())
    assert record.exc_info is None
    assert "422: nope" in record.getMessage()


def test_reconnecting_replaces_rather_than_duplicates(repo_dir, tmp_path):
    cfg = config(connections=[connection(name="web", board="b-1", folder="/old")])
    intake.finalize(cfg, _draft(folder=str(repo_dir)), StubClient(), path=tmp_path / "c.toml")

    assert len(cfg.connections) == 1
    assert cfg.connection("web").folder == str(repo_dir)


# ---------------------------------------------------------------------------
# In-place work sets no workspace key at all
# ---------------------------------------------------------------------------


def test_in_place_work_selects_a_workspace_that_derives_nothing(repo_dir):
    """`--isolation none` must leave `git_init` *absent*, not present-and-None.

    Both `plugins_in_play` and `runner.workspace_for` resolve a flat plugin by
    which of its keys the connection sets — by presence, not by value — so a
    `git_init: None` sitting in `model_extra` selects git for a connection that
    has no git strategy at all. That misroutes the run itself: `GitWorkspace`
    prepares a branch that does not exist (its git calls no-op on a plain
    folder) and claims to produce `changes`, which an unstrategised workspace
    never does, silently widening what in-place work may return.

    Driven through `from_flags` with every flag at its Typer default, exactly
    as `connect` sends them, so it pins the whole flag path: the translation
    (`_connection_shaped`) must drop "none" rather than write a None, and must
    drop the no-op `branch_prefix`/`update_base` defaults that would otherwise
    put git in play. This asserts the consequence rather than the key, so it
    still pins the rule if the translation is ever rewritten. The config-level
    symptom is not enough on its own: TOML cannot hold a None, so the written
    file is identical either way.
    """
    draft = intake.from_flags(
        "web",
        "b-1",
        settings={
            "folder": str(repo_dir),
            "repo": None,
            "isolation": "none",
            "branch_prefix": "issuebot/",
            "update_base": "none",
            "executor": in_process_environment(),
        },
        sinks=[],
        assignments=[],
    )
    conn = intake.build(config(), draft)

    assert "git_init" not in (conn.model_extra or {})
    assert "branch_prefix" not in (conn.model_extra or {})
    assert "update_base" not in (conn.model_extra or {})

    workspace, _ = workspace_for(conn, ctx())
    assert "changes" not in workspace.produces


def test_an_explicit_none_isolation_never_writes_a_present_but_none_git_init():
    """`isolation=None` (a programmatic caller, not the flag's "none" default)
    used to pass the `!= "none"` test and write a present-but-None `git_init`
    — which selects git by key presence for a connection with no strategy."""
    draft = intake.from_flags(
        "web",
        "b-1",
        settings={"folder": "/x", "isolation": None, "executor": in_process_environment()},
        sinks=[],
        assignments=[],
    )

    assert "git_init" not in draft.settings


def test_the_drop_at_default_pruning_follows_the_owning_model():
    """The no-op defaults pruned for a strategy-less draft are the owning
    plugin's own declared defaults, not copies spelled in core — a value the
    caller genuinely changed survives (and is then accurately refused by the
    owner's validate hook)."""
    from issuebot import plugins

    owner = plugins.claimant("branch_prefix")
    assert owner is not None and owner.settings is not None
    default = owner.settings.model_fields["branch_prefix"].get_default()

    draft = intake.from_flags(
        "web",
        "b-1",
        settings={
            "folder": "/x",
            "isolation": "none",
            "branch_prefix": default,
            "update_base": "none",
            "executor": in_process_environment(),
        },
        sinks=[],
        assignments=[],
    )

    assert "branch_prefix" not in draft.settings
    assert "update_base" not in draft.settings


# ---------------------------------------------------------------------------
# The shared folder check
# ---------------------------------------------------------------------------


def test_folder_error_is_none_for_a_usable_folder(repo_dir):
    assert intake.folder_error(str(repo_dir), {}) is None


def test_folder_error_is_what_the_wizard_shows_as_you_type(tmp_path):
    """The wizard re-asks on this, so the check the user fails interactively is
    the check that would have rejected the connection anyway."""
    assert "existing absolute directory" in intake.folder_error(str(tmp_path / "nope"), {})


def test_folder_error_asks_the_workspace_the_drafts_own_keys_select(repo_dir):
    """A draft carrying a workspace's key is held to that workspace's own
    folder rules — and a key merely *present* as None (a flag never passed)
    selects nothing, exactly as its absence on the saved connection would."""
    assert "requires a git repo" in intake.folder_error(str(repo_dir), {"git_init": "branch"})
    assert intake.folder_error(str(repo_dir), {"git_init": None}) is None


def test_a_draft_with_both_a_folder_and_a_repo_is_refused():
    """Two answers to one question. Repo used to silently win — the folder was
    dropped before the workspace's own 'not both' rule could ever see it."""
    with pytest.raises(intake.IntakeError, match="not both"):
        intake.build(config(), _draft(folder="/tmp", repo="https://x/r.git", git_init="branch"))


# ---------------------------------------------------------------------------
# The scripted entry path: a Draft from flags
# ---------------------------------------------------------------------------


def test_from_flags_gathers_a_draft_with_parsed_sinks_and_plugin_settings():
    """The second way a Draft is produced, beside the wizard: sink refs are
    parsed, `--set` assignments resolved against the installed plugins, and
    everything lands under the same Connection-shaped keys."""
    draft = intake.from_flags(
        "web",
        "b-1",
        settings={"folder": "/x", "executor": in_process_environment()},
        sinks=["fake:best-effort"],
        assignments=["git.push=false"],
    )

    assert draft.name == "web"
    assert draft.board == "b-1"
    assert [(s.name, s.required) for s in draft.get("sinks")] == [("fake", False)]
    # git is a flat plugin, so its `--set` key lands on the connection itself.
    assert draft.get("push") is False


def test_from_flags_refuses_a_set_for_a_flag_owned_key():
    """A `--set` for a key one of connect's own flags writes names the flag."""
    with pytest.raises(intake.IntakeError, match="--isolation"):
        intake.from_flags(
            "web",
            "b-1",
            settings={"executor": in_process_environment()},
            sinks=[],
            assignments=["git.git_init=worktree"],
        )


def test_from_flags_refuses_what_no_installed_plugin_can_honour():
    """A bad assignment or sink ref is an IntakeError, not a bare ValueError."""
    with pytest.raises(intake.IntakeError, match="nosuchplugin"):
        intake.from_flags("web", "b-1", settings={}, sinks=[], assignments=["nosuchplugin.k=1"])


def test_from_flags_with_no_executor_is_refused_when_several_could_run_it():
    """With more than one environment installed silence means nothing — the
    refusal is its own error type, so the CLI can word the fix as a flag."""
    from issuebot import plugins

    if len(plugins.names_of("environments")) < 2:
        pytest.skip("one environment installed, so a draft need not name it")

    with pytest.raises(intake.MissingExecutor):
        intake.from_flags("web", "b-1", settings={"executor": None}, sinks=[], assignments=[])
