"""Tests for the workspace inventory: what is lying around, and what can go.

Real temporary git repositories where the behaviour is git's, and a
`RecordingProcess` where it is `gh`'s — there is no local `gh` to talk to.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from conftest import completed
from issuebot.config import Connection
from issuebot.plugins.workspaces.git import inventory as workspaces
from issuebot.plugins.workspaces.git.settings import Settings
from issuebot.plugins.workspaces.git.workspace import GitWorkspace
from issuebot.process import RecordingProcess


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, text=True, capture_output=True, check=True
    ).stdout.strip()


def _prepare(
    conn: Connection, ref: str, *, worktree_root: str | None = None, clone_root: str | None = None
) -> str:
    """Cut a workspace through the seam, returning its folder."""
    ws = GitWorkspace(worktree_root=worktree_root, clone_root=clone_root)
    return ws.prepare(conn, ref, settings=Settings()).folder


def _project(folder: Path | None = None, **kw) -> Connection:
    if folder is not None:
        kw.setdefault("folder", str(folder))
    return Connection(name="p", board="b", **kw)


def test_list_worktrees_filters_to_root(repo: Path, tmp_path: Path):
    root = tmp_path / "wt"
    p = _project(repo, git_init="worktree")
    _prepare(p, "ISS-5", worktree_root=str(root))

    infos = workspaces.list_worktrees(p, worktree_root=str(root))
    assert len(infos) == 1
    wt = infos[0]
    assert wt.ref == "ISS-5"
    assert wt.branch == "issuebot/ISS-5"
    assert wt.path == str(root / "p" / "ISS-5")
    assert wt.dirty is False
    # A fresh worktree with no commits beyond the base branch has nothing to lose.
    assert wt.unpushed is False

    # A commit that is not on the base branch (and has no upstream) IS unpushed.
    _git(Path(wt.path), "commit", "--allow-empty", "-m", "work")
    again = workspaces.list_worktrees(p, worktree_root=str(root))[0]
    assert again.unpushed is True


def test_remove_worktree_refuses_dirty_without_force(repo: Path, tmp_path: Path):
    root = tmp_path / "wt"
    p = _project(repo, git_init="worktree")
    path = _prepare(p, "ISS-6", worktree_root=str(root))
    (Path(path) / "dirty.txt").write_text("x")  # make it dirty

    wt = workspaces.list_worktrees(p, worktree_root=str(root))[0]
    assert workspaces.remove(wt, project_folder=str(repo), force=False) is False
    assert Path(path).is_dir()
    assert workspaces.remove(wt, project_folder=str(repo), force=True) is True
    assert not Path(path).is_dir()


def test_pr_merged_true_when_state_merged():
    proc = RecordingProcess(replies={"gh pr view": completed(out="MERGED\n")})
    assert workspaces.pr_merged("/tmp/x", "b/1", proc=proc) is True


def test_pr_merged_false_when_open_or_missing():
    run_open = RecordingProcess(replies={"gh pr view": completed(out="OPEN\n")})
    assert workspaces.pr_merged("/tmp/x", "b/1", proc=run_open) is False
    run_none = RecordingProcess(replies={"gh pr view": completed(code=1)})
    assert workspaces.pr_merged("/tmp/x", "b/1", proc=run_none) is False


def test_list_clones_discovers_and_flags(repo: Path, tmp_path: Path):
    root = tmp_path / "cl"
    p = _project(git_init="branch", repo=str(repo))
    _prepare(p, "ISS-8", clone_root=str(root))

    infos = workspaces.list_clones(p, clone_root=str(root))
    assert len(infos) == 1
    c = infos[0]
    assert c.ref == "ISS-8"
    assert c.branch == "issuebot/ISS-8"
    assert c.path == str(root / "p" / "ISS-8")
    assert c.dirty is False and c.unpushed is False


def test_list_clones_empty_when_root_missing(repo: Path, tmp_path: Path):
    p = _project(git_init="branch", repo=str(repo))
    assert workspaces.list_clones(p, clone_root=str(tmp_path / "nope")) == []


def test_remove_clone_refuses_dirty_without_force(repo: Path, tmp_path: Path):
    root = tmp_path / "cl"
    p = _project(git_init="branch", repo=str(repo))
    path = _prepare(p, "ISS-9", clone_root=str(root))
    (Path(path) / "dirty.txt").write_text("x")

    clone = workspaces.list_clones(p, clone_root=str(root))[0]
    assert clone.dirty is True

    assert workspaces.remove(clone, force=False) is False
    assert Path(path).is_dir()
    assert workspaces.remove(clone, force=True) is True
    assert not Path(path).exists()
