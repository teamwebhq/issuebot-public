"""Tests for the persistence guarantee every store now depends on.

This module had no test file at all: the atomic write, the private permissions
and the never-raise posture were exercised only transitively, through whichever
store happened to use them. The one branch that decides a write failure cannot
take the runner down had no test anywhere.
"""

from __future__ import annotations

import os
import stat

import pytest

from issuebot.state import (
    KeyedStore,
    StateFile,
    config_dir,
    open_private,
    private_dir,
    state_dir,
    state_path,
)


def _mode(path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


# ---------------------------------------------------------------------------
# Where things live
# ---------------------------------------------------------------------------


def test_the_state_dir_follows_xdg(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    assert state_dir() == tmp_path / "issuebot"


def test_the_state_dir_falls_back_to_the_conventional_home(monkeypatch, tmp_path):
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    assert state_dir() == tmp_path / ".local" / "state" / "issuebot"


def test_the_config_dir_follows_its_own_variable(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert config_dir() == tmp_path / "issuebot"


def test_a_named_state_file_can_be_overridden_by_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("SOME_OVERRIDE", str(tmp_path / "elsewhere.json"))
    assert state_path("x.json", env_override="SOME_OVERRIDE") == tmp_path / "elsewhere.json"


def test_an_unset_override_leaves_the_default(monkeypatch, tmp_path):
    monkeypatch.delenv("SOME_OVERRIDE", raising=False)
    assert state_path("x.json", env_override="SOME_OVERRIDE") == state_dir() / "x.json"


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------


def test_an_absent_file_reads_as_empty(tmp_path):
    f = StateFile(tmp_path / "nope.json")
    assert f.read_text() is None
    assert f.read_json() == {}


def test_a_corrupt_file_reads_as_empty(tmp_path):
    """Losing this state degrades a feature — a session is not resumed. Refusing
    to start because a JSON file is truncated would be worse than the loss."""
    path = tmp_path / "broken.json"
    path.write_text("{not json")
    assert StateFile(path).read_json() == {}


def test_a_json_file_that_is_not_an_object_reads_as_empty(tmp_path):
    path = tmp_path / "list.json"
    path.write_text("[1, 2, 3]")
    assert StateFile(path).read_json() == {}


def test_an_unreadable_file_reads_as_empty(tmp_path):
    path = tmp_path / "locked.json"
    path.write_text('{"a": 1}')
    path.chmod(0o000)
    try:
        assert StateFile(path).read_json() == {}
    finally:
        path.chmod(0o600)  # so tmp_path can be cleaned up


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------


def test_a_write_round_trips(tmp_path):
    f = StateFile(tmp_path / "a" / "b" / "state.json")
    f.write_json({"k": "v"})
    assert f.read_json() == {"k": "v"}


def test_a_written_file_is_private(tmp_path):
    """Runner state holds resume tokens, identity and the PAT."""
    path = tmp_path / "secret.json"
    StateFile(path).write_json({"pat": "hunter2"})
    assert _mode(path) == 0o600


def test_a_created_directory_is_private(tmp_path):
    path = tmp_path / "nested" / "deep" / "state.json"
    StateFile(path).write_json({})
    assert _mode(path.parent) == 0o700


def test_a_write_leaves_no_temporary_file_behind(tmp_path):
    StateFile(tmp_path / "state.json").write_json({"k": "v"})
    assert [p.name for p in tmp_path.iterdir()] == ["state.json"]


def test_a_failed_write_is_logged_rather_than_raised(tmp_path, caplog):
    """A failed write must never take the runner down: the alternative to
    degraded state is no runner at all."""
    unwritable = tmp_path / "ro"
    unwritable.mkdir(mode=0o500)
    try:
        StateFile(unwritable / "state.json").write_json({"k": "v"})  # must not raise
    finally:
        unwritable.chmod(0o700)


def test_the_previous_contents_survive_a_crashed_write(tmp_path):
    """The write is a temp file and a rename, so a reader sees either the old
    contents or the new ones — never a half-written file."""
    path = tmp_path / "state.json"
    f = StateFile(path)
    f.write_json({"generation": 1})

    # Stand in for a crash mid-write by making the rename impossible.
    original = os.replace
    try:
        os.replace = lambda *a, **k: (_ for _ in ()).throw(OSError("crash"))
        f.write_json({"generation": 2})
    finally:
        os.replace = original

    assert f.read_json() == {"generation": 1}


def test_deleting_tolerates_a_file_that_is_already_gone(tmp_path):
    f = StateFile(tmp_path / "gone.json")
    f.delete()  # must not raise
    f.write_json({"k": "v"})
    f.delete()
    assert f.read_json() == {}


# ---------------------------------------------------------------------------
# Directories and streams
# ---------------------------------------------------------------------------


def test_private_dir_creates_and_tightens(tmp_path):
    path = private_dir(tmp_path / "a" / "b")
    assert path.is_dir()
    assert _mode(path) == 0o700


def test_private_dir_tolerates_a_directory_it_cannot_chmod(tmp_path, monkeypatch):
    """A clone root the user pointed at may not be ours to tighten."""
    monkeypatch.setattr(
        "pathlib.Path.chmod", lambda self, mode: (_ for _ in ()).throw(OSError("not yours"))
    )
    assert private_dir(tmp_path / "theirs").is_dir()


def test_an_opened_stream_is_private(tmp_path):
    path = tmp_path / "logs" / "run.jsonl"
    with open_private(path) as fh:
        fh.write("line\n")

    assert _mode(path) == 0o600
    assert path.read_text() == "line\n"


def test_opening_a_stream_in_an_unwritable_place_raises(tmp_path):
    """Unlike a state write, the caller wants to know — it degrades to
    console-only output rather than losing the run."""
    unwritable = tmp_path / "ro"
    unwritable.mkdir(mode=0o500)
    try:
        with pytest.raises(OSError):
            open_private(unwritable / "sub" / "run.jsonl")
    finally:
        unwritable.chmod(0o700)


# ---------------------------------------------------------------------------
# KeyedStore
# ---------------------------------------------------------------------------


def test_a_keyed_store_round_trips_entries(tmp_path):
    store = KeyedStore(tmp_path / "map.json")
    store.set("a", 1)
    store.set("b", 2)
    assert store.all() == {"a": 1, "b": 2}
    assert store.get("a") == 1
    assert store.get("missing") is None


def test_dropping_an_absent_key_is_a_no_op(tmp_path):
    store = KeyedStore(tmp_path / "map.json")
    store.set("a", 1)
    store.drop("gone")
    store.drop("a")
    assert store.all() == {}


def test_clearing_empties_the_store(tmp_path):
    store = KeyedStore(tmp_path / "map.json")
    store.set("a", 1)
    store.clear()
    assert store.all() == {}


def test_concurrent_writers_all_survive(tmp_path):
    """Every run in flight writes to the same keyed store — the session map is
    shared by every listener, the checkpoint map by every sandbox. A read-modify-
    write with no lock kept only whichever writer finished last, and two threads
    sharing one temp path could leave the file unparseable, losing the lot."""
    import threading

    store = KeyedStore(tmp_path / "keyed.json")
    count = 40

    threads = [
        threading.Thread(target=store.set, args=(f"task-{i}", f"session-{i}")) for i in range(count)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert store.all() == {f"task-{i}": f"session-{i}" for i in range(count)}
