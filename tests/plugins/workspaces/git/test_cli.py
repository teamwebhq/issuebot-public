"""Tests for the git plugin's own CLI: `worktree`/`clone` list and prune.

Exercises the plugin's `app` directly (`worktree list`, not `git worktree
list`) — the one integration test at the bottom proves the top-level
`issuebot` CLI actually mounts it under `git`, via `plugins.mount_cli`.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from conftest import config, connection
from issuebot.config import Config, save_config
from issuebot.plugins.workspaces.git import inventory as workspaces
from issuebot.plugins.workspaces.git import workspace
from issuebot.plugins.workspaces.git.cli import app
from issuebot.plugins.workspaces.git.settings import Settings
from issuebot.plugins.workspaces.git.workspace import GitWorkspace

runner = CliRunner()


def _prepare(
    conn, ref: str, *, worktree_root: str | None = None, clone_root: str | None = None
) -> str:
    """Cut a workspace through the seam, returning its folder — the setup
    every listing/pruning test below starts from."""
    ws = GitWorkspace(worktree_root=worktree_root, clone_root=clone_root)
    return ws.prepare(conn, ref, settings=Settings()).folder


def _base_config() -> Config:
    """Minimal valid config with no connections."""
    return config()


@pytest.fixture
def config_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point ISSUEBOT_CONFIG at a per-test temp file."""
    path = tmp_path / "config.toml"
    monkeypatch.setenv("ISSUEBOT_CONFIG", str(path))
    return path


def _init_repo(path: Path) -> None:
    """Create a bare git repo at path."""
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-b", "main"], cwd=path, check=True, capture_output=True)


def _repo_with_origin(path: Path) -> None:
    """Init a git repo with a commit and a remote 'origin'."""
    _init_repo(path)
    subprocess.run(["git", "config", "user.email", "t@e.com"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=path, check=True)
    (path / "f").write_text("x")
    subprocess.run(["git", "add", "-A"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=path, check=True, capture_output=True)
    subprocess.run(
        ["git", "remote", "add", "origin", "https://example.invalid/r.git"],
        cwd=path,
        check=True,
    )


def _seed_repo(path: Path) -> None:
    """A real local repo with one commit, usable as a clone source."""
    _init_repo(path)
    subprocess.run(["git", "config", "user.email", "t@e.com"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=path, check=True)
    (path / "f").write_text("x")
    subprocess.run(["git", "add", "-A"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=path, check=True, capture_output=True)


# --- worktree ------------------------------------------------------------


def test_worktree_list_shows_managed_worktrees(config_path: Path, tmp_path: Path):
    repo = tmp_path / "repo"
    _repo_with_origin(repo)
    wt_root = tmp_path / "wt"
    cfg = _base_config()
    cfg.git = {"worktree_root": str(wt_root)}
    cfg.connections = [connection(name="p", board="b", folder=str(repo), git_init="worktree")]
    save_config(cfg, config_path)

    _prepare(cfg.connections[0], "ISS-1", worktree_root=str(wt_root))

    result = runner.invoke(app, ["worktree", "list"])
    assert result.exit_code == 0
    assert "ISS-1" in result.output
    assert "issuebot/ISS-1" in result.output


def test_worktree_prune_requires_selector(config_path: Path, tmp_path: Path):
    save_config(_base_config(), config_path)
    result = runner.invoke(app, ["worktree", "prune"])
    # No selector -> help/usage, nothing removed.
    assert "selector" in result.output.lower() or result.exit_code != 0


def test_worktree_prune_removes_by_ref(config_path: Path, tmp_path: Path):
    repo = tmp_path / "repo"
    _repo_with_origin(repo)
    wt_root = tmp_path / "wt"
    cfg = _base_config()
    cfg.git = {"worktree_root": str(wt_root)}
    cfg.connections = [connection(name="p", board="b", folder=str(repo), git_init="worktree")]
    save_config(cfg, config_path)
    path = _prepare(cfg.connections[0], "ISS-2", worktree_root=str(wt_root))

    result = runner.invoke(app, ["worktree", "prune", "ISS-2"])
    assert result.exit_code == 0
    assert not Path(path).is_dir()


def test_worktree_prune_matches_every_recut_branch_by_ref(
    config_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """A merged branch is re-cut with a numbered suffix into its own
    branch-named worktree directory (`PAR-12-2`, not `PAR-12`) — `Cut.ref` for
    it is that branch leaf, not the task ref the user typed. Pruning by
    `PAR-12` must still find both the original and the re-cut worktree, and
    must not sweep in an unrelated task (`PAR-1`) whose ref is a literal
    prefix of `PAR-12`."""
    repo = tmp_path / "repo"
    _repo_with_origin(repo)
    wt_root = tmp_path / "wt"
    cfg = _base_config()
    cfg.git = {"worktree_root": str(wt_root)}
    cfg.connections = [connection(name="p", board="b", folder=str(repo), git_init="worktree")]
    save_config(cfg, config_path)
    conn = cfg.connections[0]

    first = _prepare(conn, "PAR-12", worktree_root=str(wt_root))
    # The first branch is merged — re-cut the second attempt into its own
    # branch-named worktree, exactly what a real run would do after a merge.
    # `_resolve_branch`/`_is_finished` live in `workspace`, not `inventory`, so
    # that is the module whose `pr_merged` name has to be patched.
    monkeypatch.setattr(
        workspace, "pr_merged", lambda folder, branch, **_: branch == "issuebot/PAR-12"
    )
    second = _prepare(conn, "PAR-12", worktree_root=str(wt_root))
    assert Path(second).name == "PAR-12-2"

    other = _prepare(conn, "PAR-1", worktree_root=str(wt_root))

    result = runner.invoke(app, ["worktree", "prune", "PAR-12"])
    assert result.exit_code == 0, result.output
    assert not Path(first).is_dir()
    assert not Path(second).is_dir()
    assert Path(other).is_dir()


def test_worktree_prune_matches_a_fallback_branch_past_max_attempts(
    config_path: Path, tmp_path: Path
):
    """`branch_candidates` stops at `MAX_BRANCH_ATTEMPTS` (20), but
    `resolve_branch`'s own fallback can produce attempt 21+ when every probed
    attempt came back merged. A matching rule built on `branch_candidates`
    would silently miss that worktree forever; the branch-leaf regex has no
    such bound. Cut directly with `git worktree add` rather than by resolving
    20 merged attempts first — same shape, far cheaper to set up."""
    repo = tmp_path / "repo"
    _repo_with_origin(repo)
    wt_root = tmp_path / "wt"
    cfg = _base_config()
    cfg.git = {"worktree_root": str(wt_root)}
    cfg.connections = [connection(name="p", board="b", folder=str(repo), git_init="worktree")]
    save_config(cfg, config_path)

    path = wt_root / "p" / "PAR-9-21"
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "worktree", "add", "-b", "issuebot/PAR-9-21", str(path)],
        cwd=repo,
        check=True,
        capture_output=True,
    )

    result = runner.invoke(app, ["worktree", "prune", "PAR-9"])
    assert result.exit_code == 0, result.output
    assert not path.is_dir()


def test_worktree_prune_matches_a_branch_cut_under_a_changed_prefix(
    config_path: Path, tmp_path: Path
):
    """`branch_candidates` reads the connection's *current* `branch_prefix` at
    prune time — a worktree cut while the prefix was `old/` would never match
    a candidate generated from `new/`. The branch-leaf regex does not care
    what the prefix was when the branch was cut."""
    repo = tmp_path / "repo"
    _repo_with_origin(repo)
    wt_root = tmp_path / "wt"
    cfg = _base_config()
    cfg.git = {"worktree_root": str(wt_root)}
    cfg.connections = [
        connection(name="p", board="b", folder=str(repo), git_init="worktree", branch_prefix="old/")
    ]
    save_config(cfg, config_path)
    path = _prepare(cfg.connections[0], "PAR-7", worktree_root=str(wt_root))
    assert Path(path).is_dir()

    cfg.connections[0].branch_prefix = "new/"  # ty: ignore[unresolved-attribute]
    save_config(cfg, config_path)

    result = runner.invoke(app, ["worktree", "prune", "PAR-7"])
    assert result.exit_code == 0, result.output
    assert not Path(path).is_dir()


def test_worktree_list_and_prune_identify_a_recut_worktree_by_its_branch(
    config_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """`worktree list` and the prune echo lines used to print the worktree
    *directory* name under a column read as the task ref — for a re-cut
    branch that is the branch leaf (`PAR-12-2`), not the ref
    (`PAR-12`), a value that looks like a ref but is not one a user could feed
    back into a task lookup. Both now print the true `branch` instead, which
    names the task and the attempt unambiguously."""
    repo = tmp_path / "repo"
    _repo_with_origin(repo)
    wt_root = tmp_path / "wt"
    cfg = _base_config()
    cfg.git = {"worktree_root": str(wt_root)}
    cfg.connections = [connection(name="p", board="b", folder=str(repo), git_init="worktree")]
    save_config(cfg, config_path)
    conn = cfg.connections[0]

    _prepare(conn, "PAR-12", worktree_root=str(wt_root))
    monkeypatch.setattr(
        workspace, "pr_merged", lambda folder, branch, **_: branch == "issuebot/PAR-12"
    )
    second = _prepare(conn, "PAR-12", worktree_root=str(wt_root))
    assert Path(second).name == "PAR-12-2"

    listing = runner.invoke(app, ["worktree", "list"])
    assert listing.exit_code == 0, listing.output
    assert "issuebot/PAR-12-2" in listing.output

    prune = runner.invoke(app, ["worktree", "prune", "PAR-12"])
    assert prune.exit_code == 0, prune.output
    assert "issuebot/PAR-12-2" in prune.output


def test_worktree_prune_reports_when_nothing_matches(config_path: Path, tmp_path: Path):
    repo = tmp_path / "repo"
    _repo_with_origin(repo)
    cfg = _base_config()
    cfg.git = {"worktree_root": str(tmp_path / "wt")}
    cfg.connections = [connection(name="p", board="b", folder=str(repo), git_init="worktree")]
    save_config(cfg, config_path)
    result = runner.invoke(app, ["worktree", "prune", "ISS-DOESNOTEXIST"])
    assert result.exit_code == 0
    assert "no matching" in result.output.lower()


# --- clone -----------------------------------------------------------------


def test_clone_prune_requires_selector(config_path: Path):
    save_config(_base_config(), config_path)
    result = runner.invoke(app, ["clone", "prune"])
    assert result.exit_code == 1
    assert "selector" in result.output.lower()


def test_clone_list_shows_managed_clones(config_path: Path, tmp_path: Path):
    seed = tmp_path / "seed"
    _seed_repo(seed)
    clone_root = tmp_path / "clones"
    cfg = _base_config()
    cfg.git = {"clone_root": str(clone_root)}
    cfg.connections = [
        connection(name="p", board="b", folder=None, repo=str(seed), git_init="branch")
    ]
    save_config(cfg, config_path)

    _prepare(cfg.connections[0], "ISS-L", clone_root=str(clone_root))

    result = runner.invoke(app, ["clone", "list"])
    assert result.exit_code == 0
    assert "ISS-L" in result.output
    assert "issuebot/ISS-L" in result.output
    assert str(clone_root) in result.output


def test_clone_prune_merged_removes_only_merged_clone(
    config_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    seed = tmp_path / "seed"
    _seed_repo(seed)
    clone_root = tmp_path / "clones"
    cfg = _base_config()
    cfg.git = {"clone_root": str(clone_root)}
    cfg.connections = [
        connection(name="p", board="b", folder=None, repo=str(seed), git_init="branch")
    ]
    save_config(cfg, config_path)

    path_a = _prepare(cfg.connections[0], "ISS-A", clone_root=str(clone_root))
    path_b = _prepare(cfg.connections[0], "ISS-B", clone_root=str(clone_root))

    monkeypatch.setattr(
        workspaces, "pr_merged", lambda folder, branch, **_: folder.endswith("ISS-A")
    )

    result = runner.invoke(app, ["clone", "prune", "--merged"])
    assert result.exit_code == 0, result.output
    assert "removed" in result.output.lower()
    assert not Path(path_a).is_dir()
    assert Path(path_b).is_dir()


# --- mounted under the top-level CLI ----------------------------------------


def test_mounted_under_the_top_level_cli_as_git_worktree(config_path: Path, tmp_path: Path):
    """`plugins.mount_cli` wires this plugin's `app` in under its own name, so
    the commands above are also reachable as `issuebot git worktree ...`."""
    from issuebot import cli

    repo = tmp_path / "repo"
    _repo_with_origin(repo)
    wt_root = tmp_path / "wt"
    cfg = _base_config()
    cfg.git = {"worktree_root": str(wt_root)}
    cfg.connections = [connection(name="p", board="b", folder=str(repo), git_init="worktree")]
    save_config(cfg, config_path)
    _prepare(cfg.connections[0], "ISS-1", worktree_root=str(wt_root))

    result = runner.invoke(cli.app, ["git", "worktree", "list"])
    assert result.exit_code == 0, result.output
    assert "ISS-1" in result.output
