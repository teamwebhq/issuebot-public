"""Tests for the per-task sandbox-checkpoint bookkeeping store.

The store is provider-neutral: it records which checkpoint belongs to which
task and when it was made, and never learns who can snapshot a filesystem.
"""

from __future__ import annotations

import stat
from pathlib import Path

from issuebot import task_checkpoints


def test_record_then_aged_round_trips(tmp_path: Path) -> None:
    path = tmp_path / "task-checkpoints.json"
    task_checkpoints.record("t1", path=path, now=1000.0)

    assert task_checkpoints.aged(500, path=path, now=1000.0) == []  # not aged yet
    assert task_checkpoints.aged(500, path=path, now=1600.0) == ["t1"]


def test_forget_removes_entry(tmp_path: Path) -> None:
    path = tmp_path / "task-checkpoints.json"
    task_checkpoints.record("t1", path=path, now=1000.0)
    task_checkpoints.forget("t1", path=path)

    assert task_checkpoints.aged(0, path=path, now=2000.0) == []


def test_forget_unknown_task_is_noop(tmp_path: Path) -> None:
    path = tmp_path / "task-checkpoints.json"
    task_checkpoints.forget("nope", path=path)  # must not raise


def test_aged_returns_multiple_ids_sorted_by_nothing_in_particular(tmp_path: Path) -> None:
    path = tmp_path / "task-checkpoints.json"
    task_checkpoints.record("t1", path=path, now=1000.0)
    task_checkpoints.record("t2", path=path, now=1100.0)

    ids = task_checkpoints.aged(50, path=path, now=1200.0)
    assert set(ids) == {"t1", "t2"}


def test_missing_file_reads_as_empty(tmp_path: Path) -> None:
    path = tmp_path / "does-not-exist.json"
    assert task_checkpoints.aged(0, path=path, now=1000.0) == []


def test_corrupt_file_reads_as_empty(tmp_path: Path) -> None:
    path = tmp_path / "task-checkpoints.json"
    path.write_text("{ not json")
    assert task_checkpoints.aged(0, path=path, now=1000.0) == []
    # And a subsequent write recovers cleanly.
    task_checkpoints.record("t1", path=path, now=1000.0)
    assert task_checkpoints.aged(0, path=path, now=2000.0) == ["t1"]


def test_record_overwrites_existing_timestamp(tmp_path: Path) -> None:
    path = tmp_path / "task-checkpoints.json"
    task_checkpoints.record("t1", path=path, now=1000.0)
    task_checkpoints.record("t1", path=path, now=5000.0)  # refreshed, not aged

    assert task_checkpoints.aged(4000, path=path, now=6000.0) == []


def test_store_file_is_written_0600(tmp_path: Path) -> None:
    path = tmp_path / "task-checkpoints.json"
    task_checkpoints.record("t1", path=path, now=1000.0)

    mode = stat.S_IMODE(path.stat().st_mode)
    assert mode == 0o600


def test_default_state_path_uses_xdg_state_home(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    assert task_checkpoints.default_state_path() == tmp_path / "issuebot" / "task-checkpoints.json"


def test_checkpoint_name_is_the_single_source_of_the_task_prefix() -> None:
    """The `task-<id>` naming lives here so the executor, the boot ladder and
    the prune sweep can never drift apart on it."""
    assert task_checkpoints.checkpoint_name("t1") == "task-t1"
