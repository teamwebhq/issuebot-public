"""Tests for the per-task agent session store, and who gets one."""

from __future__ import annotations

import stat
from pathlib import Path

from conftest import config
from issuebot.plugins.harnesses.fake.harness import FakeHarness
from issuebot.sessions import SessionStore, default_state_path, store_for


class _Resuming(FakeHarness):
    """A harness that declares it can reopen a prior conversation.

    Two throwaway subclasses rather than a real plugin, because what `store_for`
    keys on is the declaration — naming a harness that happens to make it here
    would test the registry instead, and would make this file one more thing to
    edit when that plugin is deleted."""

    name = "resuming"
    resumes_sessions = True


class _OneShot(FakeHarness):
    """A harness that starts every task fresh — the ABC's default."""

    name = "oneshot"


# --- who gets a store --------------------------------------------------------


def test_no_store_for_a_harness_that_cannot_resume() -> None:
    """Even with the setting on, a harness with no session concept gets none —
    the file would be a token nothing could redeem."""
    cfg = config(oneshot={"resume_sessions": True})

    assert store_for(cfg, _OneShot()) is None


def test_no_store_when_the_install_did_not_ask_for_one() -> None:
    """Resuming is opt-in: a capable harness still starts fresh by default."""
    assert store_for(config(), _Resuming()) is None


def test_a_store_when_the_harness_can_resume_and_the_install_asked() -> None:
    cfg = config(resuming={"resume_sessions": True})

    assert isinstance(store_for(cfg, _Resuming()), SessionStore)


def test_the_setting_is_read_from_the_given_harnesss_own_table() -> None:
    """`store_for` decides about the harness it was handed, so it reads that
    harness's table — not whichever one the config happens to name."""
    cfg = config(harness="fake", fake={"resume_sessions": True})

    assert store_for(cfg, _Resuming()) is None


# --- the store itself --------------------------------------------------------


def test_set_then_get_round_trips(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "sessions.json")
    store.set("task-1", "sess-abc")
    assert store.get("task-1") == "sess-abc"


def test_get_unknown_task_returns_none(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "sessions.json")
    assert store.get("nope") is None


def test_set_overwrites_existing(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "sessions.json")
    store.set("task-1", "old")
    store.set("task-1", "new")
    assert store.get("task-1") == "new"


def test_drop_removes_entry(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "sessions.json")
    store.set("task-1", "sess-abc")
    store.drop("task-1")
    assert store.get("task-1") is None


def test_drop_unknown_task_is_noop(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "sessions.json")
    store.drop("nope")  # must not raise


def test_persists_across_instances(tmp_path: Path) -> None:
    path = tmp_path / "sessions.json"
    SessionStore(path).set("task-1", "sess-abc")
    assert SessionStore(path).get("task-1") == "sess-abc"


def test_missing_file_reads_as_empty(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "does-not-exist.json")
    assert store.get("task-1") is None


def test_corrupt_file_reads_as_empty(tmp_path: Path) -> None:
    path = tmp_path / "sessions.json"
    path.write_text("{ not json")
    store = SessionStore(path)
    assert store.get("task-1") is None
    # And a subsequent write recovers cleanly.
    store.set("task-1", "sess-abc")
    assert store.get("task-1") == "sess-abc"


def test_file_created_with_0600_perms(tmp_path: Path) -> None:
    path = tmp_path / "sessions.json"
    SessionStore(path).set("task-1", "sess-abc")
    mode = stat.S_IMODE(path.stat().st_mode)
    assert mode == 0o600


def test_default_state_path_honours_issuebot_state(monkeypatch) -> None:
    monkeypatch.setenv("ISSUEBOT_STATE", "/tmp/custom/sessions.json")
    assert default_state_path() == Path("/tmp/custom/sessions.json")


def test_default_state_path_honours_xdg_state_home(monkeypatch) -> None:
    monkeypatch.delenv("ISSUEBOT_STATE", raising=False)
    monkeypatch.setenv("XDG_STATE_HOME", "/tmp/xdg")
    assert default_state_path() == Path("/tmp/xdg/issuebot/sessions.json")


def test_all_returns_full_map(tmp_path):
    store = SessionStore(tmp_path / "sessions.json")
    store.set("t1", "s1")
    store.set("t2", "s2")
    assert store.all() == {"t1": "s1", "t2": "s2"}


def test_clear_empties_the_store(tmp_path):
    store = SessionStore(tmp_path / "sessions.json")
    store.set("t1", "s1")
    store.clear()
    assert store.all() == {}
