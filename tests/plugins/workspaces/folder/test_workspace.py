"""Tests for the folder workspace: in-place, a throwaway copy, and the
unreachable `commit_and_push`."""

from __future__ import annotations

from pathlib import Path

import pytest

from issuebot.config import Connection
from issuebot.plugins.workspaces.folder.settings import Settings
from issuebot.plugins.workspaces.folder.workspace import FolderWorkspace
from issuebot.reporter import NullReporter


def _project(folder: Path) -> Connection:
    return Connection(name="p", board="b", folder=str(folder))


def test_prepare_in_place_returns_the_folder_untouched(tmp_path: Path) -> None:
    (tmp_path / "f").write_text("x")
    prepared = FolderWorkspace().prepare(_project(tmp_path), "ISS-1", settings=Settings())
    assert prepared.folder == str(tmp_path)
    # A plain folder has no upstream to diverge from — never a problem to report.
    assert prepared.problem is None


def test_prepare_copy_leaves_the_source_untouched(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "src"
    source.mkdir()
    (source / "f.txt").write_text("hello")
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))

    prepared = FolderWorkspace().prepare(
        _project(source), "ISS-2", settings=Settings(folder_init="copy")
    )

    assert prepared.folder != str(source)
    assert (Path(prepared.folder) / "f.txt").read_text() == "hello"
    assert (source / "f.txt").read_text() == "hello"


def test_commit_and_push_is_unreachable(tmp_path: Path) -> None:
    """`produces` excludes `changes`, so nothing should ever call this — it
    raises rather than silently pretending to have derived one."""
    from issuebot.plugins.workspaces.base import Prepared

    with pytest.raises(NotImplementedError):
        FolderWorkspace().commit_and_push(
            Prepared(folder=str(tmp_path)), "msg", settings=Settings()
        )


def test_a_plain_folder_is_required_to_be_nothing_in_particular(tmp_path: Path) -> None:
    """The ABC's no-op hooks, which this workspace takes as they come: it asks
    nothing of the folder a connection names (so `issuebot connect` rejects no
    folder on its account), and a warm boot leaves that folder exactly as it
    found it — nothing cut, moved or reset, which is the observable half of
    "nothing to top up"."""
    (tmp_path / "f.txt").write_text("x")
    before = sorted(child.name for child in tmp_path.iterdir())

    assert FolderWorkspace.folder_problem(str(tmp_path)) is None

    FolderWorkspace().refresh(_project(tmp_path), "ISS-1", reporter=NullReporter())

    assert sorted(child.name for child in tmp_path.iterdir()) == before


def test_it_cannot_produce_changes() -> None:
    """The other end of `test_commit_and_push_is_unreachable`: `produces`
    excludes `changes` because a plain folder has no history to diff, and the
    config check that intersects it with a connection's permits is what makes
    that method unreachable rather than merely unimplemented."""
    assert "changes" not in FolderWorkspace.produces
