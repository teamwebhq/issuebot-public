"""``issuebot claude session ...``, driven through the real CLI.

These used to live in ``tests/test_cli.py``, back when ``issuebot session`` was
a top-level group. It is this harness's group now — a stored session id is its
own resumption token — so its tests are here, and deleting the plugin deletes
them with it. What ``store_for`` decides *in general* is core's, and stays in
``tests/test_sessions.py``; what is asserted here is that this harness is one of
the ones it decides yes for, and that its own commands work.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from conftest import config
from issuebot import cli
from issuebot.config import Config, save_config
from issuebot.plugins.harnesses.claude.harness import ClaudeHarness
from issuebot.sessions import SessionStore, store_for

runner = CliRunner()


@pytest.fixture
def config_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point ISSUEBOT_CONFIG at a per-test temp file holding a valid config."""
    path = tmp_path / "config.toml"
    monkeypatch.setenv("ISSUEBOT_CONFIG", str(path))
    save_config(config(harness=ClaudeHarness.name), path)
    return path


# --- the capability ----------------------------------------------------------
#
# That this harness declares `resumes_sessions` is asserted by exercising it,
# not by reading the constant back: the pair below only separate if the flag is
# set *and* core honours it.


def test_no_store_when_this_harnesss_table_does_not_ask_for_one() -> None:
    cfg = Config.model_validate({"claude": {"resume_sessions": False}})
    assert store_for(cfg, ClaudeHarness()) is None


def test_a_store_is_kept_when_this_harnesss_table_asks_for_one() -> None:
    cfg = Config.model_validate({"claude": {"resume_sessions": True}})
    assert isinstance(store_for(cfg, ClaudeHarness()), SessionStore)


# --- the commands ------------------------------------------------------------


def test_session_list_and_prune_all(
    config_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    state = tmp_path / "sessions.json"
    monkeypatch.setenv("ISSUEBOT_STATE", str(state))
    SessionStore(state).set("t1", "s1")

    listed = runner.invoke(cli.app, ["claude", "session", "list"])
    assert "t1" in listed.output and "s1" in listed.output

    pruned = runner.invoke(cli.app, ["claude", "session", "prune", "--all"])
    assert pruned.exit_code == 0
    assert SessionStore(state).all() == {}


def test_session_prune_requires_selector(
    config_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("ISSUEBOT_STATE", str(tmp_path / "sessions.json"))
    result = runner.invoke(cli.app, ["claude", "session", "prune"])
    assert result.exit_code != 0


def test_session_prune_by_ref(config_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    state = tmp_path / "sessions.json"
    monkeypatch.setenv("ISSUEBOT_STATE", str(state))
    store = SessionStore(state)
    store.set("t1", "s1")
    store.set("t2", "s2")

    result = runner.invoke(cli.app, ["claude", "session", "prune", "t1"])
    assert result.exit_code == 0
    assert SessionStore(state).all() == {"t2": "s2"}


def test_session_prune_reports_when_ref_not_found(
    config_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    state = tmp_path / "sessions.json"
    monkeypatch.setenv("ISSUEBOT_STATE", str(state))
    SessionStore(state).set("t1", "s1")

    result = runner.invoke(cli.app, ["claude", "session", "prune", "nope"])

    assert result.exit_code == 0
    assert "no matching" in result.output.lower()
    assert SessionStore(state).all() == {"t1": "s1"}  # the real entry is untouched


# --- reading a recorded run back ---------------------------------------------


def test_logs_render_this_harnesss_own_stream_concisely(
    config_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """`issuebot logs` reads a run back with the harness that wrote it, so a
    config naming this one turns its stream-json into the same concise feed
    watching the run live showed."""
    state = tmp_path / "state"
    monkeypatch.setenv("XDG_STATE_HOME", str(state))
    log_dir = state / "issuebot" / "logs"
    log_dir.mkdir(parents=True)
    tool = json.dumps(
        {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "tool_use", "name": "Edit", "input": {"file_path": "src/foo.py"}}
                ]
            },
        }
    )
    (log_dir / "ISS-1-20260629-200000.jsonl").write_text(tool + "\n", encoding="utf-8")

    result = runner.invoke(cli.app, ["logs", "ISS-1"])

    assert result.exit_code == 0, result.output
    assert "Edit: src/foo.py" in result.output
