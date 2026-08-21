"""Tests for `GitWorkspace`'s ABC methods (`prepare`/`commit_and_push`), as
opposed to the module-level functions they wrap — see `test_workspace.py`."""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

import pytest

from conftest import completed
from issuebot.config import Connection
from issuebot.plugins.workspaces.base import Prepared
from issuebot.plugins.workspaces.git.settings import Settings
from issuebot.plugins.workspaces.git.workspace import GitWorkspace
from issuebot.process import RecordingProcess
from issuebot.reporter import NullReporter


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, text=True, capture_output=True, check=True
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "README.md").write_text("hi\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "init")
    return repo


def test_prepare_records_the_branch_and_starting_sha(repo: Path) -> None:
    conn = Connection(name="p", board="b", folder=str(repo), git_init="branch")
    prepared = GitWorkspace().prepare(conn, "ISS-1", settings=Settings())
    assert prepared.folder == str(repo)
    assert prepared.branch == "issuebot/ISS-1"
    assert prepared.base_sha == _git(repo, "rev-parse", "HEAD")


def test_prepare_resolves_the_branch_exactly_once(repo: Path) -> None:
    """A task branch cut from a stale local checkout — not a fresh clone —
    can be a real, non-reflexive ancestor of the connection's default branch
    with zero commits of its own; `_is_finished`'s tip-equality guard does
    not catch that (see its docstring). If `prepare` re-resolved after
    cutting the branch, that second look could read the branch it just
    created as already merged and hand back a `-2` — while the folder stays
    checked out on the branch the first resolution actually cut, so the
    agent's own commits would land somewhere `Prepared.branch` never names.
    `prepare` must resolve once and read back what's actually checked out."""
    _git(repo, "checkout", "-b", "old")  # currently identical to main's tip
    _git(repo, "checkout", "main")
    (repo / "advance.txt").write_text("newer\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "advance")
    # "old" is now a real ancestor of "main", one commit behind — not a fresh
    # clone's tip-identical case, but zero commits of its own either way.
    _git(repo, "checkout", "old")

    conn = Connection(name="p", board="b", folder=str(repo), git_init="branch")
    prepared = GitWorkspace().prepare(conn, "ISS-70", settings=Settings())

    assert prepared.branch == "issuebot/ISS-70"
    assert _git(repo, "rev-parse", "--abbrev-ref", "HEAD") == "issuebot/ISS-70"


def test_prepare_in_place_uses_whatever_branch_is_already_checked_out(repo: Path) -> None:
    """`git_init=None` never checks out a task branch, so `Prepared.branch`
    must be the real current branch, not a synthesised one that was never
    actually created."""
    conn = Connection(name="p", board="b", folder=str(repo))
    prepared = GitWorkspace().prepare(conn, "ISS-1", settings=Settings())
    assert prepared.branch == "main"


def test_commit_and_push_reports_no_changes_on_a_clean_tree(repo: Path) -> None:
    conn = Connection(name="p", board="b", folder=str(repo), git_init="branch")
    prepared = GitWorkspace().prepare(conn, "ISS-2", settings=Settings())

    changes = GitWorkspace().commit_and_push(prepared, "no-op", settings=Settings(push=False))

    assert changes.empty
    assert changes.pushed is False
    assert changes.files_changed == 0


def test_commit_and_push_commits_and_reports_the_diff(repo: Path) -> None:
    conn = Connection(name="p", board="b", folder=str(repo), git_init="branch")
    prepared = GitWorkspace().prepare(conn, "ISS-3", settings=Settings())
    (repo / "new.txt").write_text("added\n")

    changes = GitWorkspace().commit_and_push(prepared, "add file", settings=Settings(push=False))

    assert not changes.empty
    assert changes.files_changed == 1
    assert changes.pushed is False  # settings.push=False: never pushed
    assert changes.head_sha != changes.base_sha


def test_commit_and_push_counts_a_path_with_a_space_once(repo: Path) -> None:
    """`git diff --name-only` prints one path per line, and a path can contain a
    space — counting whitespace-separated words made one file read as two."""
    conn = Connection(name="p", board="b", folder=str(repo), git_init="branch")
    prepared = GitWorkspace().prepare(conn, "ISS-3b", settings=Settings())
    (repo / "a file.txt").write_text("added\n")

    changes = GitWorkspace().commit_and_push(prepared, "add file", settings=Settings(push=False))

    assert changes.files_changed == 1


def test_commit_and_push_does_not_push_without_an_origin(repo: Path) -> None:
    """`settings.push=True` (the default) still can't push with no remote."""
    conn = Connection(name="p", board="b", folder=str(repo), git_init="branch")
    prepared = GitWorkspace().prepare(conn, "ISS-4", settings=Settings())
    (repo / "new.txt").write_text("added\n")

    changes = GitWorkspace().commit_and_push(prepared, "add file", settings=Settings())

    assert changes.pushed is False


def _bare_origin(tmp_path: Path, repo: Path) -> Path:
    """A bare clone of ``repo`` wired in as its ``origin``, with main pushed."""
    origin = tmp_path / "origin.git"
    _git(repo, "clone", "--bare", str(repo), str(origin))
    _git(repo, "remote", "add", "origin", str(origin))
    _git(repo, "push", "origin", "main")
    return origin


def test_a_diverged_branch_is_reported_as_a_problem_not_a_failure(
    repo: Path, tmp_path: Path
) -> None:
    """The task branch and origin's copy each gained commits of their own.
    `prepare` must still hand back a launchable workspace — the divergence
    travels as `Prepared.problem`, for the runner to tell the agent about,
    rather than as an exception the runner reads as an ordinary prep failure."""
    _bare_origin(tmp_path, repo)
    conn = Connection(name="p", board="b", folder=str(repo), git_init="branch")
    workspace = GitWorkspace()
    workspace.prepare(conn, "ISS-2", settings=Settings())
    _git(repo, "push", "-u", "origin", "issuebot/ISS-2")

    # Advance origin one way…
    other = tmp_path / "other"
    _git(repo, "clone", str(tmp_path / "origin.git"), str(other))
    _git(other, "-c", "user.email=t@e.com", "-c", "user.name=T", "checkout", "issuebot/ISS-2")
    (other / "remote.txt").write_text("r\n")
    _git(other, "add", "-A")
    _git(other, "-c", "user.email=t@e.com", "-c", "user.name=T", "commit", "-m", "remote")
    _git(other, "push", "origin", "issuebot/ISS-2")
    # …and the local branch a different way, so they diverge.
    (repo / "local.txt").write_text("l\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "local")

    prepared = workspace.prepare(conn, "ISS-2", settings=Settings())

    assert prepared.folder == str(repo)
    assert prepared.branch == "issuebot/ISS-2"
    assert prepared.problem is not None
    assert prepared.problem.kind == "diverged-branch"
    assert prepared.problem.branch == "issuebot/ISS-2"


def test_changes_after_a_reconciled_branch_divergence_cover_only_the_agents_work(
    repo: Path, tmp_path: Path
) -> None:
    """`prepare` records the *pre-reconcile* local tip as `base_sha`. The
    reconcile preamble then tells the agent to rebase onto origin's copy, so
    by commit time that sha is no longer an ancestor of HEAD — and a diff from
    it would span the commits other contributors pushed, claiming their work
    as the agent's. The reported `Changes` must cover only what sits on top of
    origin's copy of the branch."""
    _bare_origin(tmp_path, repo)
    conn = Connection(name="p", board="b", folder=str(repo), git_init="branch")
    workspace = GitWorkspace()
    workspace.prepare(conn, "ISS-2", settings=Settings())
    _git(repo, "push", "-u", "origin", "issuebot/ISS-2")

    # Another contributor advances origin's copy of the task branch…
    other = tmp_path / "other"
    _git(repo, "clone", str(tmp_path / "origin.git"), str(other))
    _git(other, "-c", "user.email=t@e.com", "-c", "user.name=T", "checkout", "issuebot/ISS-2")
    (other / "remote.txt").write_text("r\n")
    _git(other, "add", "-A")
    _git(other, "-c", "user.email=t@e.com", "-c", "user.name=T", "commit", "-m", "remote")
    _git(other, "push", "origin", "issuebot/ISS-2")
    # …while the local branch gained its own commit, so they diverge.
    (repo / "local.txt").write_text("l\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "local")

    prepared = workspace.prepare(conn, "ISS-2", settings=Settings())
    assert prepared.problem is not None  # the divergence rode the prompt

    # The agent follows the reconcile preamble: rebase onto origin's copy,
    # then does the task's own work.
    _git(repo, "fetch", "origin")
    _git(repo, "rebase", "origin/issuebot/ISS-2")
    (repo / "agent.txt").write_text("a\n")

    changes = workspace.commit_and_push(prepared, "agent work", settings=Settings())

    diffed = set(_git(repo, "diff", "--name-only", changes.base_sha, changes.head_sha).splitlines())
    assert "remote.txt" not in diffed, "the diff claims another contributor's commit"
    assert diffed == {"local.txt", "agent.txt"}
    assert changes.pushed is True  # the reconciled branch fast-forwards


def test_a_reconcile_that_leaves_nothing_new_still_reports_the_branch_as_pushed(
    repo: Path, tmp_path: Path
) -> None:
    """The agent's local commit was already squash-merged into origin's copy of
    the task branch, so the reconcile rebase drops it and HEAD lands exactly on
    origin's tip. Nothing needs pushing — but the branch *is* fully on origin,
    and `pushed=False` would read as work stuck on this machine."""
    _bare_origin(tmp_path, repo)
    conn = Connection(name="p", board="b", folder=str(repo), git_init="branch")
    workspace = GitWorkspace()
    workspace.prepare(conn, "ISS-2", settings=Settings())
    _git(repo, "push", "-u", "origin", "issuebot/ISS-2")

    # The local branch gains a commit…
    (repo / "local.txt").write_text("l\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "local")
    # …and origin's copy gains the *same patch* under a different sha (the
    # squash-merge shape), so the branches diverge but carry identical work.
    other = tmp_path / "other"
    _git(repo, "clone", str(tmp_path / "origin.git"), str(other))
    _git(other, "-c", "user.email=t@e.com", "-c", "user.name=T", "checkout", "issuebot/ISS-2")
    (other / "local.txt").write_text("l\n")
    _git(other, "add", "-A")
    _git(other, "-c", "user.email=t@e.com", "-c", "user.name=T", "commit", "-m", "squashed")
    _git(other, "push", "origin", "issuebot/ISS-2")

    prepared = workspace.prepare(conn, "ISS-2", settings=Settings())
    assert prepared.problem is not None and prepared.problem.kind == "diverged-branch"

    # The agent reconciles: the rebase recognises the duplicate patch and
    # drops the local commit, leaving HEAD equal to origin's tip.
    _git(repo, "fetch", "origin")
    _git(repo, "rebase", "origin/issuebot/ISS-2")
    assert _git(repo, "rev-parse", "HEAD") == _git(repo, "rev-parse", "origin/issuebot/ISS-2")

    changes = workspace.commit_and_push(prepared, "agent work", settings=Settings())

    assert changes.pushed is True  # everything on the branch is on origin


def test_changes_after_a_reconciled_base_divergence_exclude_the_base_commits(
    repo: Path, tmp_path: Path
) -> None:
    """The base-divergence twin: the agent rebases the task branch onto the
    updated base, so the recorded tip is rewritten. The diff must not span the
    commits that landed on the base branch — only the task branch's own work
    on top of it."""
    _bare_origin(tmp_path, repo)
    conn = Connection(
        name="p", board="b", folder=str(repo), git_init="branch", update_base="rebase"
    )
    workspace = GitWorkspace()
    workspace.prepare(conn, "ISS-5", settings=Settings())

    # Conflicting edits to the same file on the task branch and on origin/main.
    (repo / "README.md").write_text("task side\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "task edit")
    other = tmp_path / "oc"
    _git(repo, "clone", str(tmp_path / "origin.git"), str(other))
    (other / "README.md").write_text("base side\n")
    (other / "base.txt").write_text("b\n")
    _git(other, "add", "-A")
    _git(other, "-c", "user.email=t@e.com", "-c", "user.name=T", "commit", "-m", "base edit")
    _git(other, "push", "origin", "main")

    prepared = workspace.prepare(conn, "ISS-5", settings=Settings())
    assert prepared.problem is not None and prepared.problem.kind == "diverged-base"

    # The agent reconciles: rebase onto origin/main, resolving the conflict.
    _git(repo, "fetch", "origin")
    try:
        _git(repo, "rebase", "origin/main")
    except Exception:
        (repo / "README.md").write_text("resolved\n")
        _git(repo, "add", "-A")
        _git(repo, "-c", "core.editor=true", "rebase", "--continue")
    (repo / "agent.txt").write_text("a\n")

    changes = workspace.commit_and_push(prepared, "agent work", settings=Settings(push=False))

    diffed = set(_git(repo, "diff", "--name-only", changes.base_sha, changes.head_sha).splitlines())
    assert "base.txt" not in diffed, "the diff claims commits that landed on the base branch"
    assert "agent.txt" in diffed


def test_a_reconciled_base_rebase_still_reaches_origin(repo: Path, tmp_path: Path) -> None:
    """The whole point of a base reconcile. The task branch is already on
    origin, so the agent's rebase rewrites commits the remote holds and a plain
    push is rejected — the branch would sit finished on the runner's disk and
    every sink would report it as carrying nothing. The reconciled branch must
    land on origin."""
    _bare_origin(tmp_path, repo)
    conn = Connection(
        name="p", board="b", folder=str(repo), git_init="branch", update_base="rebase"
    )
    workspace = GitWorkspace()
    workspace.prepare(conn, "ISS-7", settings=Settings())

    # The task branch does some work and is pushed — origin now holds it.
    (repo / "README.md").write_text("task side\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "task edit")
    _git(repo, "push", "-u", "origin", "issuebot/ISS-7")

    # Meanwhile the base branch moves, conflicting with the task branch.
    other = tmp_path / "oc"
    _git(repo, "clone", str(tmp_path / "origin.git"), str(other))
    (other / "README.md").write_text("base side\n")
    _git(other, "add", "-A")
    _git(other, "-c", "user.email=t@e.com", "-c", "user.name=T", "commit", "-m", "base edit")
    _git(other, "push", "origin", "main")

    prepared = workspace.prepare(conn, "ISS-7", settings=Settings())
    assert prepared.problem is not None and prepared.problem.kind == "diverged-base"

    # The agent follows the reconcile preamble: rebase onto origin/main,
    # resolving the conflict, then does the task's own work.
    _git(repo, "fetch", "origin")
    try:
        _git(repo, "rebase", "origin/main")
    except Exception:
        (repo / "README.md").write_text("resolved\n")
        _git(repo, "add", "-A")
        _git(repo, "-c", "core.editor=true", "rebase", "--continue")
    (repo / "agent.txt").write_text("a\n")

    changes = workspace.commit_and_push(prepared, "agent work", settings=Settings())

    assert changes.pushed is True
    assert _git(repo, "rev-parse", "origin/issuebot/ISS-7") == changes.head_sha


def test_an_ordinary_rejected_push_is_never_forced() -> None:
    """Only a reconcile that rewrote history may force. An ordinary run whose
    push is rejected reports `pushed=False` and leaves origin alone — whatever
    is on the remote was put there by somebody this run never heard about."""
    proc = RecordingProcess(
        replies={
            # Ordered: `RecordingProcess` matches on insertion order, and
            # "remotes" would otherwise be swallowed by the "git remote" entry.
            "refs/remotes/origin/b": completed(out="somebody-else\n"),
            "rev-parse HEAD": completed(out="head-sha\n"),
            "git remote": completed(out="origin\n"),
            "push": completed(code=1, err="rejected: non-fast-forward"),
        }
    )
    prepared = Prepared(folder="/repo", branch="b", base_sha="base-sha", problem=None)

    changes = GitWorkspace().commit_and_push(prepared, "work", settings=Settings(), proc=proc)

    assert changes.pushed is False
    pushes = [c for c in proc.calls if "push" in c]
    assert len(pushes) == 1, f"the rejected push was retried: {pushes}"
    assert not any("--force-with-lease" in c for c in proc.calls)


def test_a_rejected_push_says_so_in_the_log(caplog: pytest.LogCaptureFixture) -> None:
    """`Changes(pushed=False)` is the only trace a rejected push used to leave,
    and nothing reads it until a sink refuses much later. Whoever reads the run
    log must be able to see that the branch never left the runner, and why."""
    proc = RecordingProcess(
        replies={
            "refs/remotes/origin/b": completed(out="somebody-else\n"),
            "rev-parse HEAD": completed(out="head-sha\n"),
            "git remote": completed(out="origin\n"),
            "push": completed(code=1, err="rejected: non-fast-forward"),
        }
    )
    prepared = Prepared(folder="/repo", branch="b", base_sha="base-sha", problem=None)

    with caplog.at_level(logging.WARNING, logger="issuebot"):
        GitWorkspace().commit_and_push(prepared, "work", settings=Settings(), proc=proc)

    logged = "\n".join(r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING)
    assert "b" in logged
    assert "non-fast-forward" in logged


def test_a_conflicted_base_update_is_reported_as_a_base_problem(repo: Path, tmp_path: Path) -> None:
    """`update_base="rebase"` conflicting must abort cleanly (no rebase left in
    progress) and report a `diverged-base` problem naming the base branch."""
    _bare_origin(tmp_path, repo)
    conn = Connection(
        name="p", board="b", folder=str(repo), git_init="branch", update_base="rebase"
    )
    workspace = GitWorkspace()
    workspace.prepare(conn, "ISS-5", settings=Settings())

    # Conflicting edits to the same file on the task branch and on origin/main.
    (repo / "README.md").write_text("task side\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "task edit")
    other = tmp_path / "oc"
    _git(repo, "clone", str(tmp_path / "origin.git"), str(other))
    (other / "README.md").write_text("base side\n")
    _git(other, "add", "-A")
    _git(other, "-c", "user.email=t@e.com", "-c", "user.name=T", "commit", "-m", "base edit")
    _git(other, "push", "origin", "main")

    prepared = workspace.prepare(conn, "ISS-5", settings=Settings())

    assert prepared.problem is not None
    assert prepared.problem.kind == "diverged-base"
    assert prepared.problem.base == "main"
    assert prepared.folder == str(repo)
    # No half-finished rebase left behind.
    assert not (repo / ".git" / "rebase-merge").exists()
    assert not (repo / ".git" / "rebase-apply").exists()
    # A rebase connection asks the agent for a rebase.
    assert prepared.problem.reconcile == "rebase"


def test_a_conflicted_merge_of_the_base_asks_the_agent_for_a_merge(
    repo: Path, tmp_path: Path
) -> None:
    """`update_base = "merge"` is a connection saying "never rewrite history".
    The problem it reports must carry that word through to the agent, so the
    reconcile instructions do not ask for the one thing the setting forbids."""
    _bare_origin(tmp_path, repo)
    conn = Connection(name="p", board="b", folder=str(repo), git_init="branch", update_base="merge")
    workspace = GitWorkspace()
    workspace.prepare(conn, "ISS-6", settings=Settings())

    # Conflicting edits to the same file on the task branch and on origin/main.
    (repo / "README.md").write_text("task side\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "task edit")
    other = tmp_path / "oc"
    _git(repo, "clone", str(tmp_path / "origin.git"), str(other))
    (other / "README.md").write_text("base side\n")
    _git(other, "add", "-A")
    _git(other, "-c", "user.email=t@e.com", "-c", "user.name=T", "commit", "-m", "base edit")
    _git(other, "push", "origin", "main")

    prepared = workspace.prepare(conn, "ISS-6", settings=Settings())

    assert prepared.problem is not None
    assert prepared.problem.kind == "diverged-base"
    assert prepared.problem.reconcile == "merge"
    # No half-finished merge left behind.
    assert not (repo / ".git" / "MERGE_HEAD").exists()


def test_folder_problem_wants_a_git_repository(repo: Path, tmp_path: Path) -> None:
    """What git requires of a folder, asked before any connection exists — this
    is what `issuebot connect` and the wizard reject a folder with. A plain
    directory and a folder that does not exist are both rejected."""
    assert GitWorkspace.folder_problem(str(repo)) is None

    plain = tmp_path / "plain"
    plain.mkdir()
    assert "requires a git repo" in (GitWorkspace.folder_problem(str(plain)) or "")
    assert "requires a git repo" in (GitWorkspace.folder_problem(str(tmp_path / "missing")) or "")


def test_refresh_tops_up_the_inherited_clone(repo: Path, tmp_path: Path) -> None:
    """The warm-boot hook: the clone an earlier task left behind is renamed onto
    this task's ref and brought up to date, so the `prepare` that follows has
    nothing left to clone."""
    root = tmp_path / "cl"
    conn = Connection(name="p", board="b", repo=str(repo), git_init="branch")
    workspace = GitWorkspace(clone_root=str(root))
    workspace.prepare(conn, "ISS-1", settings=Settings(git_init="branch", repo=str(repo)))

    workspace.refresh(conn, "ISS-2", reporter=NullReporter())

    assert not (root / "p" / "ISS-1").exists()  # reused in place, not duplicated
    assert _git(root / "p" / "ISS-2", "rev-parse", "--abbrev-ref", "HEAD") == "issuebot/ISS-2"


def test_refresh_defers_a_diverged_branch_for_prepare_to_report(repo: Path, tmp_path: Path) -> None:
    """A warm boot can inherit a clone whose task branch cannot be
    fast-forwarded onto origin's copy. `refresh` must not raise — the sandbox
    worker calls it bare, and an exception there kills the run with no result
    line at all. It leaves the branch as it stands; the `prepare` that follows
    re-detects the divergence and reports it as `Prepared.problem`, exactly as
    a local run would."""
    _bare_origin(tmp_path, repo)
    origin = str(tmp_path / "origin.git")
    root = tmp_path / "cl"
    conn = Connection(name="p", board="b", repo=origin, git_init="branch")
    settings = Settings(repo=origin, git_init="branch")
    workspace = GitWorkspace(clone_root=str(root))

    # An earlier run cut the task branch from main's tip and pushed its work.
    clone = root / "p" / "ISS-2"
    workspace.prepare(conn, "ISS-2", settings=settings)
    _git(clone, "config", "user.email", "t@example.com")
    _git(clone, "config", "user.name", "Test")
    (clone / "work.txt").write_text("w\n")
    _git(clone, "add", "-A")
    _git(clone, "commit", "-m", "work")
    _git(clone, "push", "-u", "origin", "issuebot/ISS-2")

    # Meanwhile main advanced, so refresh's reset lands the branch somewhere
    # origin's copy cannot be fast-forwarded onto.
    (repo / "advance.txt").write_text("n\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "advance")
    _git(repo, "push", "origin", "main")

    workspace.refresh(conn, "ISS-2", reporter=NullReporter())  # must not raise

    prepared = workspace.prepare(conn, "ISS-2", settings=settings)
    assert prepared.problem is not None
    assert prepared.problem.kind == "diverged-branch"
    assert prepared.folder == str(clone)


def test_it_can_produce_changes() -> None:
    """This workspace's half of the axis's `produces` contract, asserted where
    the plugin lives: git has a history to diff against, so a run in it may
    report `changes` — which is what makes `commit_and_push` above reachable at
    all. (The axis suite checks only that every workspace declares *something*
    valid; which kinds are this plugin's own claim.)"""
    assert "changes" in GitWorkspace.produces


def test_a_clone_can_be_worked_in_directly(repo: Path, tmp_path: Path) -> None:
    """The pairing the four-valued setting could not express: the working copy
    is a fresh clone, and nothing is cut inside it."""
    conn = Connection(name="p", board="b", repo=str(repo))
    settings = Settings(repo=str(repo))

    prepared = GitWorkspace(clone_root=str(tmp_path / "cl")).prepare(
        conn, "ISS-9", settings=settings
    )

    assert prepared.folder == str(tmp_path / "cl" / "p" / "ISS-9")
    assert prepared.branch == "main"  # the clone's own branch, not a task branch
    assert (Path(prepared.folder) / "README.md").exists()


def test_working_directly_reports_no_changes(repo: Path) -> None:
    """Nothing is cut, so the agent is on somebody else's branch — the one you
    have open, or a clone's default. A run there is never told it may report
    `changes`, which is what keeps it from committing to that branch."""
    workspace = GitWorkspace()

    assert "changes" not in workspace.produces_for(Settings())
    assert "changes" in workspace.produces_for(Settings(git_init="branch"))
    assert "changes" in workspace.produces_for(Settings(git_init="worktree"))


def test_a_clone_cuts_its_task_branch_from_the_freshly_fetched_default(
    repo: Path, tmp_path: Path
) -> None:
    """A clone's branch starts from `origin/<default>`, so every task starts from
    current code even when the clone directory was left behind by an earlier
    attempt on the same ref."""
    conn = Connection(name="p", board="b", repo=str(repo), git_init="branch")
    settings = Settings(repo=str(repo), git_init="branch")
    workspace = GitWorkspace(clone_root=str(tmp_path / "cl"))

    prepared = workspace.prepare(conn, "ISS-10", settings=settings)

    assert _git(Path(prepared.folder), "rev-parse", "--abbrev-ref", "HEAD") == "issuebot/ISS-10"
    assert _git(Path(prepared.folder), "rev-parse", "HEAD") == _git(repo, "rev-parse", "main")


def test_the_retired_clone_strategy_says_what_replaced_it() -> None:
    """`git_init='clone'` was one setting doing two jobs. Pydantic's own message
    for a retired literal would say the value is gone without saying that the
    concept moved to the key above it."""
    with pytest.raises(ValueError, match="now two settings"):
        Settings(git_init="clone")
