"""Behavioural tests for the git workspace plugin, against real temporary git
repositories, driven through the `Workspace` ABC surface (`prepare`/`refresh`)
— the same interface `run.execute` consumes.

The handful of tests that exercise genuinely internal rules (branch naming,
the provisioning gate) import the private helpers honestly rather than driving
them through the seam, and say so — see also `test_git_branch_resolution.py`
for the branch-resolution rules themselves.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from issuebot.config import Connection
from issuebot.plugins.workspaces.base import Prepared
from issuebot.plugins.workspaces.git import workspace
from issuebot.plugins.workspaces.git.settings import Settings
from issuebot.plugins.workspaces.git.workspace import (
    Git,
    GitWorkspace,
    _branch_name,
    _needs_provision,
    _resolve_branch,
    _shared_clone_path,
    resolve_clone_root,
    resolve_worktree_root,
)
from issuebot.reporter import NullReporter


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.email=t@example.com", "-c", "user.name=Test", *args],
        cwd=cwd,
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """An initialised repo with one commit on branch 'main'."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "README.md").write_text("hi\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "init")
    return repo


def _project(folder: Path | None = None, **kw) -> Connection:
    if folder is not None:
        kw.setdefault("folder", str(folder))
    return Connection(name="p", board="b", **kw)


def _prepare(
    conn: Connection,
    ref: str,
    *,
    worktree_root: str | None = None,
    clone_root: str | None = None,
) -> Prepared:
    """Prepare through the seam: a `GitWorkspace` built the way `runner.
    workspace_for` builds one, asked through the ABC's own `prepare`."""
    ws = GitWorkspace(worktree_root=worktree_root, clone_root=clone_root)
    return ws.prepare(conn, ref, settings=Settings())


# ---------------------------------------------------------------------------
# Branch naming: internal rules, imported honestly
# ---------------------------------------------------------------------------


def test_branch_name_uses_prefix_and_ref():
    assert _branch_name("bot/", "ISS-42") == "bot/ISS-42"


def test_branch_name_sanitises_messy_ref():
    assert _branch_name("issuebot/", "feat: my ref") == "issuebot/feat-my-ref"
    assert _branch_name("issuebot/", "-bad..ref") == "issuebot/bad-ref"


def test_resolve_branch_reuses_a_fresh_untouched_branch(repo: Path):
    """`git branch --merged` is ancestor-*inclusive*: a branch cut with zero
    commits is trivially its own base's ancestor, and would misread as merged
    on the very next call without `_is_finished`'s tip-equality guard — which
    would defeat the one stability `_resolve_branch` promises callers asking
    more than once."""
    p = _project(repo, git_init="branch")
    g = Git(str(repo))
    first = _resolve_branch(g, p, "ISS-60")
    _git(repo, "checkout", "-b", first)  # cut it, exactly as `_prepare_branch` would

    second = _resolve_branch(g, p, "ISS-60")

    assert second == first == "issuebot/ISS-60"


# ---------------------------------------------------------------------------
# Preparing: folder connections, branches and worktrees
# ---------------------------------------------------------------------------


def test_prepare_none_returns_folder_untouched(repo: Path):
    p = _project(repo, git_init=None)
    prepared = _prepare(p, "ISS-1")
    assert prepared.folder == str(repo)
    assert prepared.problem is None
    assert _git(repo, "rev-parse", "--abbrev-ref", "HEAD") == "main"


def test_prepare_branch_creates_then_reuses(repo: Path):
    p = _project(repo, git_init="branch")
    prepared = _prepare(p, "ISS-1")
    assert prepared.folder == str(repo)
    assert _git(repo, "rev-parse", "--abbrev-ref", "HEAD") == "issuebot/ISS-1"

    # Switch away, then a second call must reuse (checkout) the existing branch.
    _git(repo, "checkout", "main")
    _prepare(p, "ISS-1")
    assert _git(repo, "rev-parse", "--abbrev-ref", "HEAD") == "issuebot/ISS-1"


def test_prepare_worktree_creates_then_reuses(repo: Path, tmp_path: Path):
    root = tmp_path / "wt"
    p = _project(repo, git_init="worktree")

    prepared = _prepare(p, "ISS-9", worktree_root=str(root))
    expected = root / "p" / "ISS-9"
    assert prepared.folder == str(expected)
    assert expected.is_dir()
    assert _git(expected, "rev-parse", "--abbrev-ref", "HEAD") == "issuebot/ISS-9"

    # Second call reuses the same worktree without error.
    again = _prepare(p, "ISS-9", worktree_root=str(root))
    assert again.folder == str(expected)


def test_a_recut_branch_lands_in_its_own_worktree_directory(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """The wiring this task exists to fix, exercised end to end against a real
    repo rather than the pure-function unit tests in
    `test_git_branch_resolution.py`: those call `_workspace_path_for_branch`
    directly and would not notice `_prepare_worktree` going back to keying on
    `ref`. Here, a task whose branch merged is run again and the *worktree
    `prepare` actually hands back* must be the new branch's own directory,
    freshly checked out on it — not the old worktree, still sitting on the
    branch that just merged."""
    root = tmp_path / "wt"
    p = _project(repo, git_init="worktree")

    first = _prepare(p, "ISS-97", worktree_root=str(root))
    assert Path(first.folder).name == "ISS-97"

    # The first branch is now merged. `pr_merged` (the `gh`-backed check) is
    # the source of truth a real squash/rebase-merged PR is detected by, so
    # that is what is faked here — the ancestry side of `_is_finished` stays
    # real, and correctly says "not diverged" for an untouched branch.
    monkeypatch.setattr(
        workspace, "pr_merged", lambda folder, branch, **_: branch == "issuebot/ISS-97"
    )

    second = _prepare(p, "ISS-97", worktree_root=str(root))

    assert Path(second.folder).name == "ISS-97-2"
    assert second.folder != first.folder
    assert _git(Path(second.folder), "rev-parse", "--abbrev-ref", "HEAD") == "issuebot/ISS-97-2"


def test_resolve_worktree_root_prefers_config(monkeypatch):
    assert resolve_worktree_root("/custom/wt") == Path("/custom/wt")
    monkeypatch.setenv("XDG_STATE_HOME", "/state")
    assert resolve_worktree_root(None) == Path("/state/issuebot/worktrees")


def test_prepare_worktree_rejects_stale_dir(repo: Path, tmp_path: Path):
    root = tmp_path / "wt"
    p = _project(repo, git_init="worktree")
    stale = root / "p" / "ISS-7"
    stale.mkdir(parents=True)  # exists but is not a worktree
    with pytest.raises(RuntimeError):
        _prepare(p, "ISS-7", worktree_root=str(root))


# ---------------------------------------------------------------------------
# Preparing: clone connections
# ---------------------------------------------------------------------------


def test_resolve_clone_root_prefers_config(monkeypatch):
    assert resolve_clone_root("/custom/cl") == Path("/custom/cl")
    monkeypatch.setenv("XDG_STATE_HOME", "/state")
    assert resolve_clone_root(None) == Path("/state/issuebot/clones")


def test_prepare_clone_creates_then_reuses(repo: Path, tmp_path: Path):
    root = tmp_path / "cl"
    p = _project(git_init="branch", repo=str(repo))

    prepared = _prepare(p, "ISS-3", clone_root=str(root))
    expected = root / "p" / "ISS-3"
    assert prepared.folder == str(expected)
    assert (expected / ".git").exists()
    assert _git(expected, "rev-parse", "--abbrev-ref", "HEAD") == "issuebot/ISS-3"

    again = _prepare(p, "ISS-3", clone_root=str(root))
    assert again.folder == str(expected)
    assert _git(expected, "rev-parse", "--abbrev-ref", "HEAD") == "issuebot/ISS-3"


def test_prepare_clone_branches_off_latest(repo: Path, tmp_path: Path):
    root = tmp_path / "cl"
    p = _project(git_init="branch", repo=str(repo))
    _prepare(p, "ISS-4", clone_root=str(root))

    (repo / "new.txt").write_text("later\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "later")

    prepared = _prepare(p, "ISS-5", clone_root=str(root))
    assert (Path(prepared.folder) / "new.txt").exists()


def test_prepare_clone_fetches_on_same_ref_reuse(repo: Path, tmp_path: Path):
    root = tmp_path / "cl"
    p = _project(git_init="branch", repo=str(repo))

    # First prepare: clone + branch off origin/main.
    _prepare(p, "ISS-7", clone_root=str(root))

    # A new commit lands on the remote default branch.
    (repo / "fresh.txt").write_text("fresh\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "fresh")

    # Second prepare for the SAME ref must `git fetch`, so origin/main advances
    # in the clone even though the checked-out task branch is reused.
    prepared = _prepare(p, "ISS-7", clone_root=str(root))
    remote_log = _git(Path(prepared.folder), "log", "--oneline", "origin/main")
    assert "fresh" in remote_log


def test_prepare_repoints_origin_when_the_connection_repo_drifted(tmp_path, repo):
    """A connection's `repo` can be corrected against its linked Parade project
    after the clone already exists on disk (`runner.Wiring.sync_repo`).
    That correction only reaches the in-memory `Connection` — the physical
    clone's remote has to be repointed too, or every fetch/push/PR after it
    keeps hitting the old repository while the correction reports success."""
    root = tmp_path / "cl"
    old_origin = tmp_path / "old-origin.git"
    new_origin = tmp_path / "new-origin.git"
    _git(repo, "clone", "--bare", str(repo), str(old_origin))
    _git(repo, "clone", "--bare", str(repo), str(new_origin))

    p = _project(git_init="branch", repo=str(old_origin))
    _prepare(p, "ISS-90", clone_root=str(root))
    clone = root / "p" / "ISS-90"
    assert _git(clone, "remote", "get-url", "origin") == str(old_origin)

    p.repo = str(new_origin)  # the correction landing on the live Connection
    _prepare(p, "ISS-90", clone_root=str(root))

    assert _git(clone, "remote", "get-url", "origin") == str(new_origin)


def test_prepare_leaves_a_matching_origin_alone(tmp_path, repo):
    """The ordinary case — nothing drifted — must not touch the remote at all,
    only fetch it."""
    root = tmp_path / "cl"
    origin = tmp_path / "origin.git"
    _git(repo, "clone", "--bare", str(repo), str(origin))

    p = _project(git_init="branch", repo=str(origin))
    _prepare(p, "ISS-91", clone_root=str(root))
    clone = root / "p" / "ISS-91"

    # Reused on a second call with the same `repo` — the remote is unchanged.
    _prepare(p, "ISS-91", clone_root=str(root))

    assert _git(clone, "remote", "get-url", "origin") == str(origin)


def test_prepare_adds_origin_when_the_clone_has_none(tmp_path, repo):
    """A clone can be missing `origin` entirely — removed by hand, or from
    before this plugin always added one — and that must not crash the run.
    `git remote set-url` only rewrites an existing remote; it errors on one
    with none, so the no-remote case has to fall through to `remote add`."""
    root = tmp_path / "cl"
    origin = tmp_path / "origin.git"
    _git(repo, "clone", "--bare", str(repo), str(origin))

    p = _project(git_init="branch", repo=str(origin))
    _prepare(p, "ISS-96", clone_root=str(root))
    clone = root / "p" / "ISS-96"
    _git(clone, "remote", "remove", "origin")

    # Must not raise, and must leave the clone usably pointed at `repo` again.
    _prepare(p, "ISS-96", clone_root=str(root))

    assert _git(clone, "remote", "get-url", "origin") == str(origin)


def test_prepare_repoints_the_worktree_strategys_shared_clone_too(tmp_path, repo):
    """The worktree strategy funnels every task through one shared clone
    (`_shared_clone_path`) rather than the per-ref clone path — the fix has to
    cover that route as well, not just the per-task clone."""
    root = tmp_path / "cl"
    old_origin = tmp_path / "old-origin.git"
    new_origin = tmp_path / "new-origin.git"
    _git(repo, "clone", "--bare", str(repo), str(old_origin))
    _git(repo, "clone", "--bare", str(repo), str(new_origin))

    p = _project(git_init="worktree", repo=str(old_origin))
    wt_root = tmp_path / "wt"
    _prepare(p, "ISS-92", worktree_root=str(wt_root), clone_root=str(root))
    shared = _shared_clone_path(p, str(root))
    assert _git(shared, "remote", "get-url", "origin") == str(old_origin)

    p.repo = str(new_origin)
    _prepare(p, "ISS-93", worktree_root=str(wt_root), clone_root=str(root))

    assert _git(shared, "remote", "get-url", "origin") == str(new_origin)


def test_prepare_never_touches_a_folder_connections_remote(tmp_path, repo):
    """A folder connection has no `repo` at all — it works from the user's own
    checkout, and its remote must be left completely alone. Never repoint a
    user's own clone."""
    origin = tmp_path / "origin.git"
    _git(repo, "clone", "--bare", str(repo), str(origin))
    _git(repo, "remote", "add", "origin", str(origin))

    p = _project(repo, git_init="branch")  # a folder connection: no `repo` setting
    _prepare(p, "ISS-94")

    assert _git(repo, "remote", "get-url", "origin") == str(origin)


# ---------------------------------------------------------------------------
# Syncing against origin
# ---------------------------------------------------------------------------


def _bare_origin(tmp_path, repo):
    origin = tmp_path / "origin.git"
    _git(repo, "clone", "--bare", str(repo), str(origin))
    _git(repo, "remote", "add", "origin", str(origin))
    _git(repo, "push", "origin", "main")
    return origin


def test_prepare_branch_fast_forwards_when_behind(tmp_path, repo):
    _bare_origin(tmp_path, repo)
    p = _project(repo, git_init="branch")
    # Create + push the task branch, then advance it on origin via a second clone.
    _prepare(p, "ISS-1")
    _git(repo, "push", "-u", "origin", "issuebot/ISS-1")
    other = tmp_path / "other"
    _git(repo, "clone", str(tmp_path / "origin.git"), str(other))
    _git(other, "checkout", "issuebot/ISS-1")
    (other / "new.txt").write_text("x\n")
    _git(other, "add", "-A")
    _git(other, "commit", "-m", "remote work")
    _git(other, "push", "origin", "issuebot/ISS-1")
    # Re-prepare locally: it should fetch + fast-forward to include remote work.
    _git(repo, "checkout", "main")
    _prepare(p, "ISS-1")
    assert (repo / "new.txt").exists()


def test_prepare_with_no_remote_branch_is_a_noop_sync(tmp_path, repo):
    _bare_origin(tmp_path, repo)
    p = _project(repo, git_init="branch")
    # Branch exists locally but was never pushed → nothing to sync, no problem.
    assert _prepare(p, "ISS-3").problem is None
    assert _prepare(p, "ISS-3").problem is None


def test_update_base_merge_brings_in_base_commits(tmp_path, repo):
    _bare_origin(tmp_path, repo)
    p = _project(repo, git_init="branch", update_base="merge")
    _prepare(p, "ISS-4")
    # Advance origin/main from another clone.
    other = tmp_path / "om"
    _git(repo, "clone", str(tmp_path / "origin.git"), str(other))
    (other / "base.txt").write_text("base\n")
    _git(other, "add", "-A")
    _git(other, "commit", "-m", "base move")
    _git(other, "push", "origin", "main")
    # Re-prepare the task branch with update_base=merge → base.txt appears.
    _prepare(p, "ISS-4")
    assert (repo / "base.txt").exists()


# A diverged branch (and a conflicted base update) no longer raise out of
# `prepare` — they come back as `Prepared.problem`. Those scenarios live in
# `test_workspace_abc.py`, beside the rest of the ABC-surface behaviour.


# ---------------------------------------------------------------------------
# Warm boot: refreshing an inherited clone
# ---------------------------------------------------------------------------


def test_refresh_reuses_existing_clone_and_tops_up(repo: Path, tmp_path: Path):
    """Warm-boot prep: a project checkpoint captured whatever ref cold-populated
    it (ISS-1 here); a later task (ISS-2) must reuse that same clone in place —
    renamed to its own ref path — fetch + hard-reset to top it up, then check
    out its own task branch, instead of `prepare`'s fresh `git clone`."""
    root = tmp_path / "cl"
    p = _project(git_init="branch", repo=str(repo))
    old = _prepare(p, "ISS-1", clone_root=str(root))

    # The remote advances after the checkpoint was taken.
    (repo / "new.txt").write_text("later\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "later")

    GitWorkspace(clone_root=str(root)).refresh(p, "ISS-2", reporter=NullReporter())

    expected = root / "p" / "ISS-2"
    assert not Path(old.folder).exists()  # reused/renamed in place, not duplicated
    assert (expected / "new.txt").exists()  # topped up via fetch + reset --hard
    assert _git(expected, "rev-parse", "--abbrev-ref", "HEAD") == "issuebot/ISS-2"


def test_refresh_reuses_existing_task_branch(repo: Path, tmp_path: Path):
    """A second warm boot for the SAME ref reuses the already-checked-out branch
    rather than trying (and failing) to create it again."""
    root = tmp_path / "cl"
    p = _project(git_init="branch", repo=str(repo))
    _prepare(p, "ISS-1", clone_root=str(root))
    ws = GitWorkspace(clone_root=str(root))

    ws.refresh(p, "ISS-1", reporter=NullReporter())
    ws.refresh(p, "ISS-1", reporter=NullReporter())

    expected = root / "p" / "ISS-1"
    assert _git(expected, "rev-parse", "--abbrev-ref", "HEAD") == "issuebot/ISS-1"


def test_refresh_raises_when_no_existing_clone(tmp_path: Path):
    """A warm boot with no clone at all under the project's clone root is a
    broken assumption (the checkpoint should always carry one) — raise loudly
    rather than silently falling through to something unexpected."""
    p = _project(git_init="branch", repo="https://example.com/r.git")
    with pytest.raises(RuntimeError):
        GitWorkspace(clone_root=str(tmp_path / "cl")).refresh(p, "ISS-1", reporter=NullReporter())


def test_refresh_raises_when_multiple_existing_clones(repo: Path, tmp_path: Path):
    """More than one clone under the project's clone root violates the
    one-clone-per-ephemeral-sandbox assumption a warm boot relies on — raise
    loudly rather than silently picking the alphabetically-first one."""
    root = tmp_path / "cl"
    p = _project(git_init="branch", repo=str(repo))
    _prepare(p, "ISS-1", clone_root=str(root))
    _prepare(p, "ISS-2", clone_root=str(root))
    with pytest.raises(RuntimeError):
        GitWorkspace(clone_root=str(root)).refresh(p, "ISS-3", reporter=NullReporter())


# ---------------------------------------------------------------------------
# The provisioning gate: internal rule, imported honestly
# ---------------------------------------------------------------------------


def test_needs_provision_true_when_marker_absent_then_false_once_stored(tmp_path: Path):
    """First check (no stored manifest hash) always needs provisioning; an
    unchanged manifest on the next check does not."""
    folder = tmp_path / "ws"
    folder.mkdir()
    (folder / ".issuebear.toml").write_text('[bootstrap]\nsetup=["echo hi"]\n')

    assert _needs_provision(str(folder)) is True
    assert _needs_provision(str(folder)) is False


def test_needs_provision_true_when_lockfile_changes(tmp_path: Path):
    """A lockfile-only change (no change to `.issuebear.toml` itself) must still
    trip the gate — that's the whole point of hashing lockfiles too."""
    folder = tmp_path / "ws"
    folder.mkdir()
    (folder / "uv.lock").write_text("v1")

    assert _needs_provision(str(folder)) is True
    assert _needs_provision(str(folder)) is False

    (folder / "uv.lock").write_text("v2")
    assert _needs_provision(str(folder)) is True


def test_needs_provision_marker_stays_out_of_the_working_tree(repo: Path):
    """The manifest-hash marker must never land in the working tree: a stray
    file there is picked up by `commit_and_push`'s `git add -A` and committed
    into the task branch (and makes an otherwise-empty run look dirty)."""
    (repo / "uv.lock").write_text("v1")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "lock")

    assert _needs_provision(str(repo)) is True

    assert _git(repo, "status", "--porcelain") == ""
    assert _needs_provision(str(repo)) is False


# ---------------------------------------------------------------------------
# Coming back to a task whose working copy is gone
# ---------------------------------------------------------------------------


def _clone_conn(origin: Path, **kw) -> Connection:
    """A connection whose working copy is a fresh clone of ``origin``."""
    return Connection(name="p", board="b", repo=str(origin), **kw)


def test_a_re_clone_resumes_the_task_branch_pushed_by_an_earlier_turn(tmp_path, repo):
    """A task can come back long after its clone went away — pruned, a new
    machine, a fresh sandbox. Its branch did not: a run that may report changes
    always pushes. Cutting the branch from the default instead silently reverted
    the task to nothing, and the push that followed was rejected."""
    origin = _bare_origin(tmp_path, repo)
    root = tmp_path / "cl"
    conn = _clone_conn(origin, git_init="branch")

    first = Path(_prepare(conn, "ISS-40", clone_root=str(root)).folder)
    (first / "turn1.txt").write_text("earlier work\n")
    _git(first, "add", "-A")
    _git(first, "commit", "-m", "turn 1")
    _git(first, "push", "-u", "origin", "issuebot/ISS-40")
    pushed = _git(first, "rev-parse", "HEAD")

    shutil.rmtree(first)  # the working copy is gone; the branch is not

    second = Path(_prepare(conn, "ISS-40", clone_root=str(root)).folder)

    assert _git(second, "rev-parse", "--abbrev-ref", "HEAD") == "issuebot/ISS-40"
    assert _git(second, "rev-parse", "HEAD") == pushed
    assert (second / "turn1.txt").exists()


def test_a_re_clone_of_a_task_that_never_pushed_still_starts_from_current_code(tmp_path, repo):
    """The other half of the same rule: with no branch of its own on the remote,
    a new task cuts from the default branch, freshly fetched."""
    origin = _bare_origin(tmp_path, repo)
    root = tmp_path / "cl"
    conn = _clone_conn(origin, git_init="branch")

    (repo / "later.txt").write_text("landed after the clone root existed\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "later")
    _git(repo, "push", "origin", "main")

    prepared = _prepare(conn, "ISS-41", clone_root=str(root))

    assert (Path(prepared.folder) / "later.txt").exists()


def test_a_worktree_cut_after_the_branch_was_pushed_resumes_it_too(tmp_path, repo):
    """Same question, other strategy: a worktree whose directory was pruned must
    come back to the work, not to the base."""
    _bare_origin(tmp_path, repo)
    conn = _project(repo, git_init="worktree")
    root = tmp_path / "wt"

    first = Path(_prepare(conn, "ISS-43", worktree_root=str(root)).folder)
    (first / "turn1.txt").write_text("earlier work\n")
    _git(first, "add", "-A")
    _git(first, "commit", "-m", "turn 1")
    _git(first, "push", "-u", "origin", "issuebot/ISS-43")
    pushed = _git(first, "rev-parse", "HEAD")

    # The worktree and its local branch both go, as a prune --force would leave it.
    _git(repo, "worktree", "remove", "--force", str(first))
    _git(repo, "branch", "-D", "issuebot/ISS-43")

    second = Path(_prepare(conn, "ISS-43", worktree_root=str(root)).folder)

    assert _git(second, "rev-parse", "HEAD") == pushed
    assert (second / "turn1.txt").exists()


# ---------------------------------------------------------------------------
# repo + worktree: one shared clone, worktrees cut from it
# ---------------------------------------------------------------------------


def test_repo_plus_worktree_shares_one_clone_across_tasks(tmp_path, repo):
    """The worktree strategy's shape: one clone per connection, per-task copies
    cut from it as worktrees. A clone per task *and* a worktree from each clone
    was two copies of the repository per task, only one of which was used."""
    origin = _bare_origin(tmp_path, repo)
    conn = Connection(name="p", board="b", repo=str(origin), git_init="worktree")
    clone_root, wt_root = tmp_path / "cl", tmp_path / "wt"

    first = _prepare(conn, "ISS-50", worktree_root=str(wt_root), clone_root=str(clone_root))
    second = _prepare(conn, "ISS-51", worktree_root=str(wt_root), clone_root=str(clone_root))

    # Two worktrees, each on its own task branch…
    assert first.folder == str(wt_root / "p" / "ISS-50")
    assert _git(Path(first.folder), "rev-parse", "--abbrev-ref", "HEAD") == "issuebot/ISS-50"
    assert _git(Path(second.folder), "rev-parse", "--abbrev-ref", "HEAD") == "issuebot/ISS-51"

    # …from ONE clone, at the shared path — no per-task clones at all.
    shared = _shared_clone_path(conn, str(clone_root))
    assert (shared / ".git").exists()
    per_task = [c.name for c in (clone_root / "p").iterdir() if not c.name.startswith(".")]
    assert per_task == []


def test_the_shared_clone_is_not_listed_or_offered_for_pruning(tmp_path, repo):
    """`clone prune --all` deleting the shared clone would break every worktree
    cut from it — it is infrastructure, not a task's workspace."""
    from issuebot.plugins.workspaces.git import inventory

    origin = _bare_origin(tmp_path, repo)
    conn = Connection(name="p", board="b", repo=str(origin), git_init="worktree")
    clone_root, wt_root = tmp_path / "cl", tmp_path / "wt"
    _prepare(conn, "ISS-52", worktree_root=str(wt_root), clone_root=str(clone_root))

    clones = inventory.list_clones(conn, clone_root=str(clone_root))
    worktrees = inventory.list_worktrees(
        conn, worktree_root=str(wt_root), clone_root=str(clone_root)
    )

    assert clones == []  # the shared clone is not a per-task clone
    assert [wt.ref for wt in worktrees] == ["ISS-52"]  # found via the shared clone
