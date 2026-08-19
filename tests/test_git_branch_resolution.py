"""Choosing a task's branch when earlier attempts may already have merged.

A task can produce several PRs over its life. Continuing on a merged branch
produces an empty diff and a PR nobody can review, so a merged branch is
retired and the next attempt gets its own numbered name — one that still
carries the task ref, so the new PR still attaches to the same task."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

import pytest

from issuebot.config import Connection
from issuebot.plugins.workspaces.git.workspace import (
    Git,
    _branch_name,
    _resolve_branch,
    _workspace_path,
    _workspace_path_for_branch,
)
from issuebot.process import Completed, RecordingProcess


def _connection(**overrides: object) -> Connection:
    """A connection whose only relevant setting is its branch prefix."""
    return Connection(name="p", board="b", **overrides)


@dataclass
class _BranchProbeProcess(RecordingProcess):
    """A `RecordingProcess` whose git/gh answers come from branch-name
    membership tests rather than `RecordingProcess`'s own substring-matched
    scripts.

    `_resolve_branch` constructs candidate branch names at runtime
    (`issuebot/PAR-12`, `-2`, `-3`, ...), and a plain substring match cannot
    tell `issuebot/PAR-12` apart from `issuebot/PAR-12-2` — the shorter name
    is a literal prefix of the longer one's command line. Parsing each
    command's actual branch argument and comparing it against these lists
    sidesteps that ambiguity entirely."""

    branches: list[str] = field(default_factory=list)
    remote: list[str] = field(default_factory=list)
    merged: list[str] = field(default_factory=list)
    pr_merged: list[str] = field(default_factory=list)

    def run(
        self, argv: list[str], *, cwd: str | None = None, env: dict[str, str] | None = None
    ) -> Completed:
        """Record the call, then answer from whichever list its branch belongs to."""
        self.calls.append(list(argv))
        self.cwds.append(cwd)
        self.envs.append(env)

        if argv[:4] == ["git", "rev-parse", "--verify", "--quiet"]:
            ref = argv[4]
            if ref.startswith("refs/heads/"):
                branch = ref.removeprefix("refs/heads/")
                return Completed(argv, 0 if branch in self.branches else 1)
            if ref.startswith("refs/remotes/origin/"):
                branch = ref.removeprefix("refs/remotes/origin/")
                return Completed(argv, 0 if branch in self.remote else 1)
            # A plain ref name: `_is_finished`'s divergence guard, not an
            # existence probe. There is no commit graph to simulate here, so
            # every distinct name is its own "sha" — the fake never mistakes
            # two different branches for the same commit, which is exactly
            # the real-git degenerate case this guard exists to catch.
            return Completed(argv, 0, out=ref)

        if argv[:3] == ["git", "branch", "--merged"]:
            branch = argv[-1]  # `branch --merged <default> --list <branch>`
            return Completed(argv, 0, out=branch if branch in self.merged else "")

        if argv[:3] == ["gh", "pr", "view"]:
            branch = argv[3]
            return Completed(argv, 0, out="MERGED" if branch in self.pr_merged else "OPEN")

        if argv[:2] == ["git", "fetch"]:
            return Completed(argv, 0)  # best-effort, always succeeds here

        return Completed(argv, 0)


class _FakeGit(Git):
    """A `Git` whose process is a `_BranchProbeProcess`, with a convenience to
    simulate a branch having just been cut — what every real caller of
    `_resolve_branch` does before its next call within the same run."""

    def add_branch(self, branch: str) -> None:
        """Make `branch` exist locally from here on."""
        assert isinstance(self.proc, _BranchProbeProcess)
        self.proc.branches.append(branch)


@pytest.fixture
def fake_git():
    """Builds a `_FakeGit` backed by a `_BranchProbeProcess` scripted from
    the given branch-name lists."""

    def build(
        *,
        branches: list[str] | None = None,
        remote: list[str] | None = None,
        merged: list[str] | None = None,
        pr_merged: list[str] | None = None,
    ) -> _FakeGit:
        proc = _BranchProbeProcess(
            branches=list(branches or []),
            remote=list(remote or []),
            merged=list(merged or []),
            pr_merged=list(pr_merged or []),
        )
        return _FakeGit("/tmp/fake-repo", proc)

    return build


def test_no_existing_branch_uses_the_base_name(fake_git):
    """The ordinary first attempt is unchanged — no suffix, no probing cost
    beyond one existence check."""
    g = fake_git(branches=[], remote=[], merged=[])

    assert _resolve_branch(g, _connection(), "PAR-12") == "issuebot/PAR-12"


def test_an_unmerged_branch_is_continued(fake_git):
    """The clarify-and-resume loop depends on a task coming back to its own
    branch."""
    g = fake_git(branches=["issuebot/PAR-12"], remote=[], merged=[])

    assert _resolve_branch(g, _connection(), "PAR-12") == "issuebot/PAR-12"


def test_a_merged_branch_yields_the_next_number(fake_git):
    """The headline behaviour."""
    g = fake_git(branches=["issuebot/PAR-12"], remote=[], merged=["issuebot/PAR-12"])

    assert _resolve_branch(g, _connection(), "PAR-12") == "issuebot/PAR-12-2"


def test_successive_merges_keep_counting(fake_git):
    """A third attempt must not collide with the second."""
    g = fake_git(
        branches=["issuebot/PAR-12", "issuebot/PAR-12-2"],
        remote=[],
        merged=["issuebot/PAR-12", "issuebot/PAR-12-2"],
    )

    assert _resolve_branch(g, _connection(), "PAR-12") == "issuebot/PAR-12-3"


def test_the_latest_unmerged_attempt_wins(fake_git):
    """-2 is open, so work continues there rather than starting a -3."""
    g = fake_git(
        branches=["issuebot/PAR-12", "issuebot/PAR-12-2"],
        remote=[],
        merged=["issuebot/PAR-12"],
    )

    assert _resolve_branch(g, _connection(), "PAR-12") == "issuebot/PAR-12-2"


def test_a_branch_only_on_the_remote_still_counts(fake_git):
    """A fresh clone has none of the task's branches locally. Ignoring the
    remote would re-cut a branch that already exists on origin and then fail
    to push."""
    g = fake_git(branches=[], remote=["issuebot/PAR-12"], merged=[])

    assert _resolve_branch(g, _connection(), "PAR-12") == "issuebot/PAR-12"


def test_a_squash_merged_branch_is_detected_by_the_pr(fake_git):
    """A squash merge leaves no ancestry, so `git branch --merged` says no.
    The PR is the source of truth, which is exactly why pr_merged exists."""
    g = fake_git(branches=["issuebot/PAR-12"], remote=[], merged=[], pr_merged=["issuebot/PAR-12"])

    assert _resolve_branch(g, _connection(), "PAR-12") == "issuebot/PAR-12-2"


def test_resolution_is_stable_when_asked_twice(fake_git):
    """_branch_name is consulted at three separate call sites. If two of them
    disagreed, a task would be prepared on one branch and reported on another."""
    g = fake_git(branches=["issuebot/PAR-12"], remote=[], merged=["issuebot/PAR-12"])

    first = _resolve_branch(g, _connection(), "PAR-12")
    # Once cut, the new branch exists and is unmerged — the same answer.
    g.add_branch(first)
    second = _resolve_branch(g, _connection(), "PAR-12")

    assert first == second == "issuebot/PAR-12-2"


def test_a_recut_branch_gets_its_own_worktree_directory(fake_git, tmp_path):
    """A worktree is bound to a branch, so a new branch needs a new directory.

    Reusing the ref-keyed directory hands the task the worktree still checked
    out on the merged branch — the exact bug the re-cut exists to fix, made
    invisible by the fact that the branch name was right."""
    g = fake_git(branches=["issuebot/PAR-12"], remote=[], merged=["issuebot/PAR-12"])
    connection = _connection()

    branch = _resolve_branch(g, connection, "PAR-12")
    path = _workspace_path_for_branch(connection, branch, tmp_path)

    assert path.name == "PAR-12-2"


def test_the_clone_directory_stays_keyed_on_the_ref(fake_git, tmp_path):
    """A clone is branch-agnostic — it fetches and checks out whichever branch
    was resolved. Re-cloning for a -2 would be pure waste."""
    connection = _connection()

    path = _workspace_path(connection, "PAR-12", tmp_path)

    assert path.name == "PAR-12"


def test_the_task_reference_survives_the_suffix():
    """Parade matches a PR to a task by the ref in the branch name. A suffix
    that broke that would silently detach every re-cut branch's PR."""
    name = _branch_name("issuebot/", "PAR-12", attempt=2)

    assert re.search(r"(?<![A-Za-z0-9])PAR-12(?![0-9])", name)
    # Not just "contains the substring": a broken suffix like "PAR-122" (attempt
    # run into the number) also contains "PAR-12" — the boundary is the point.
    assert not re.search(r"(?<![A-Za-z0-9])PAR-12(?![0-9])", "issuebot/PAR-122")
