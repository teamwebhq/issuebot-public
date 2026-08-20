"""The git workspace plugin: cut a task's working copy, keep it fresh, and
derive what changed once the agent is done.

This is the run path's view of git. Listing and reclaiming workspaces after the
fact is :mod:`issuebot.plugins.workspaces.git.inventory`, which is only ever
driven from the CLI.

The process seam is :class:`~issuebot.process.Process`, passed once per call
(ADR-0003). A failed git step raises :class:`GitError` — named, so callers can
catch it rather than a bare ``Exception``.

:class:`GitWorkspace` is the plugin's :class:`~issuebot.plugins.workspaces.
base.Workspace` implementation, and the ABC (plus the settings models) is this
module's whole external surface: the underscore-prefixed functions below are
its internals, called only by the class and each other, and nothing outside
this plugin imports them. The handful of public module-level names that remain
(`Git`, `is_merged`, `pr_merged`, the root resolvers, `source_repo_folder`,
`is_git_worktree`) are public because the plugin's *own* sibling modules
(`inventory`, `doctor`) import them.

Publishing a pushed branch (a PR) is a sink's business, not this plugin's
(ADR-0012). What this workspace owns is the push itself, and a sink that needs
one says so with `needs_pushed_branch` rather than being named here.

One `gh` call does live here: `Git.gh` is shelled out to by `pr_merged`, to
ask whether a task's branch already has a merged PR. That is a *read* about
the state of a branch this plugin owns, on the same footing as `git branch
--merged` — but it is `gh`, so the docstring says so rather than reading as a
boundary the module does not keep.

`is_merged`/`pr_merged` themselves live here rather than in `inventory` even
though `inventory`'s prune path is their older, original caller: `inventory`
already imports `Git` from this module, so the reverse import for
`_resolve_branch`'s own merge check (below) would be circular. `inventory`
imports both names back from here, so its prune path is unchanged.
"""

from __future__ import annotations

import hashlib
import logging
import re
from collections.abc import Iterator
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar, Literal

from issuebot import provision
from issuebot.config import Connection, conn_setting
from issuebot.contracts import Changes, OutputKind
from issuebot.plugins.workspaces.base import Prepared, Workspace, WorkspaceProblem
from issuebot.plugins.workspaces.git.settings import Settings
from issuebot.process import REAL, Completed, Process
from issuebot.state import StateFile, private_dir, state_dir

logger = logging.getLogger("issuebot")

if TYPE_CHECKING:
    from pydantic import BaseModel

    from issuebot.reporter import Reporter

DivergenceKind = Literal["branch", "base"]

# How a workspace authenticates an HTTPS GitHub remote: through the ``gh`` CLI,
# which every run environment already holds a credential for (a Railway sandbox
# is given ``GH_TOKEN`` and nothing else). git reads no such variable of its
# own, so without this a clone of a private repo asks for a password nobody is
# there to type.
#
# Set on each clone rather than in the user's global git config — issuebot owns
# the workspaces it cuts, not the machine they sit on — and scoped to
# github.com, so a remote on another host keeps whatever it already uses.
GH_CREDENTIAL_CONFIG = "credential.https://github.com.helper=!gh auth git-credential"


class GitError(RuntimeError):
    """A git or ``gh`` step failed."""


class BranchDiverged(GitError):
    """The local task branch and its remote (or base) have diverged and cannot be
    fast-forwarded.

    Carries the launchable workspace ``folder`` and the divergence ``kind``
    ("branch" = origin gained commits we lack; "base" = rebasing the base branch
    conflicted). Never escapes this plugin: `GitWorkspace.prepare` catches it
    and reports it as `Prepared.problem` — data the runner routes to the agent
    to reconcile in-workspace. ``base`` is the base branch name for a base
    divergence.
    """

    def __init__(
        self,
        branch: str,
        detail: str = "",
        *,
        folder: str = "",
        kind: DivergenceKind = "branch",
        base: str | None = None,
    ) -> None:
        self.branch = branch
        self.detail = detail
        self.folder = folder
        self.kind = kind
        self.base = base
        super().__init__(f"branch {branch} diverged from remote{f': {detail}' if detail else ''}")


class Git:
    """Git and ``gh``, bound to one folder and one process adapter.

    The internal working unit for both this module and
    :mod:`issuebot.plugins.workspaces.git.inventory`. It exists so the folder
    and the adapter are threaded once rather than through every helper's
    parameter list.
    """

    def __init__(self, folder: str | Path, proc: Process = REAL) -> None:
        self.folder = str(folder)
        self.proc = proc

    def at(self, folder: str | Path) -> Git:
        """The same adapter, pointed at a different folder."""
        return Git(folder, self.proc)

    def git(self, *args: str) -> Completed:
        """Run a git command here, tolerating failure."""
        return self.proc.run(["git", *args], cwd=self.folder)

    def gh(self, *args: str) -> Completed:
        """Run a ``gh`` command here, tolerating failure."""
        return self.proc.run(["gh", *args], cwd=self.folder)

    def check(self, what: str, *args: str) -> Completed:
        """Run a git command here and raise :class:`GitError` if it fails."""
        r = self.git(*args)
        if not r.ok:
            raise GitError(f"git {what} failed: {r.message}")
        return r

    # -- queries every caller needs -----------------------------------------

    def branch_exists(self, branch: str) -> bool:
        """True when the branch exists locally."""
        return self.git("rev-parse", "--verify", "--quiet", f"refs/heads/{branch}").ok

    def remote_branch_exists(self, branch: str) -> bool:
        """True when origin has the branch, whether or not this copy does.

        The two are not the same question and the difference is a task's work:
        a fresh clone has origin's every branch as a remote-tracking ref and
        none of them as a local one, so a task branch pushed by an earlier run
        answers False to :meth:`branch_exists` while its commits are sitting
        safely on the remote."""
        return self.git("rev-parse", "--verify", "--quiet", f"refs/remotes/origin/{branch}").ok

    def current_branch(self) -> str:
        """The branch currently checked out here.

        Used for in-place work (`git_init=None`), where the agent's branch is
        whatever was already checked out rather than a task branch this plugin
        cut itself."""
        return self.git("rev-parse", "--abbrev-ref", "HEAD").out.strip()

    def head_sha(self) -> str:
        """The commit this folder's HEAD currently points at."""
        return self.git("rev-parse", "HEAD").out.strip()

    def has_origin(self) -> bool:
        """True when an ``origin`` remote is configured."""
        return "origin" in self.git("remote").out.split()

    def default_branch(self) -> str:
        """The repo's base branch: origin's HEAD, else main/master, else "main"."""
        r = self.git("symbolic-ref", "refs/remotes/origin/HEAD")
        if r.ok and r.out.strip():
            return r.out.strip().removeprefix("refs/remotes/origin/")
        for candidate in ("main", "master"):
            if self.branch_exists(candidate):
                return candidate
        return "main"

    def is_dirty(self) -> bool:
        """True when the working tree has uncommitted changes."""
        return bool(self.git("status", "--porcelain").out.strip())

    def unpushed(self) -> bool:
        """True when the branch holds commits that removing it would lose.

        Commits not on its upstream, or — when no upstream is configured —
        commits not on the repo's default branch. A fresh branch identical to its
        base is not unpushed."""
        r = self.git("rev-list", "@{u}..HEAD")
        if r.ok:
            return bool(r.out.strip())
        return bool(self.git("rev-list", f"{self.default_branch()}..HEAD").out.strip())


def is_merged(folder: str, branch: str, *, proc: Process = REAL) -> bool:
    """True when the branch has been merged into the repo's base branch.

    Commit-ancestry only — see :func:`pr_merged` for the squash/rebase case.
    Lives here rather than in `inventory` so :func:`_resolve_branch` below can
    call it without `inventory` importing back from this module."""
    g = Git(folder, proc)
    return bool(g.git("branch", "--merged", g.default_branch(), "--list", branch).out.strip())


def pr_merged(folder: str, branch: str, *, proc: Process = REAL) -> bool:
    """True when the branch's GitHub PR is merged, per ``gh``.

    The source of truth for a PR: correct for squash and rebase merges, which
    :func:`is_merged`'s ancestry check misses entirely. Lives beside
    :func:`is_merged` for the same import-cycle reason (module docstring)."""
    r = Git(folder, proc).gh("pr", "view", branch, "--json", "state", "-q", ".state")
    if not r.ok:
        # A failed call and "asked, and the PR isn't merged" both read as
        # False below, but they are not the same thing: no `gh`, no `gh auth`,
        # a non-GitHub remote, no remote, or being offline all land here too
        # — and on a machine missing any of those, this call silently reverts
        # every squash/rebase-merged branch to reading unmerged forever, with
        # nothing to say why. One line so that at least it's visible; whether
        # it was a missing binary, a missing PR, or a missing login is in
        # `r.message`, which is whatever `gh` itself said.
        logger.warning("gh pr view %s could not be answered: %s", branch, r.message)
        return False
    return r.out.strip() == "MERGED"


def _safe_ref(ref: str) -> str:
    """A filesystem- and branch-safe rendering of a task ref.

    Keeps word characters, dots and hyphens; collapses everything else (and any
    '..') to '-'; never starts with a '-' or '.', which git would reject as a
    branch name and a shell could read as a flag."""
    ref = re.sub(r"[^\w.\-]", "-", ref.strip())
    ref = re.sub(r"\.\.+", "-", ref)
    ref = re.sub(r"-{2,}", "-", ref)
    return ref.lstrip("-.")


# How many previous attempts :func:`_branch_candidates` probes before giving up
# and reusing the last one. A task producing more than this many merged PRs is
# a human problem, not a branch-naming one — and each attempt beyond the first
# costs a round trip (a `fetch`, a `branch --merged`, sometimes a `gh pr view`).
_MAX_BRANCH_ATTEMPTS = 20


def _branch_name(prefix: str, ref: str, *, attempt: int = 1) -> str:
    """The per-task branch name for one attempt.

    ``attempt`` 1 is the plain, original name; later attempts append ``-N``.
    The task ref stays intact and delimited either way, because the board
    server matches a pull request to its task by finding that ref in the
    branch name — a suffix that ran into the number (``PAR-122`` for attempt
    12 of ``PAR-12``) would silently detach every re-cut branch's PR from its
    task.

    Takes the resolved ``prefix``, not the connection: callers walk up to
    :data:`_MAX_BRANCH_ATTEMPTS` names per resolution, and the prefix is
    loop-invariant — reading it back through the settings model each time was
    a full validate per candidate."""
    suffix = "" if attempt <= 1 else f"-{attempt}"
    return f"{prefix}{_safe_ref(ref)}{suffix}"


def _branch_candidates(prefix: str, ref: str) -> Iterator[str]:
    """Every branch name this task might have used, oldest attempt first,
    bounded at :data:`_MAX_BRANCH_ATTEMPTS`."""
    for attempt in range(1, _MAX_BRANCH_ATTEMPTS + 1):
        yield _branch_name(prefix, ref, attempt=attempt)


def _is_finished(g: Git, branch: str) -> bool:
    """True when this branch's work has already landed, and continuing on it
    would produce an empty diff nobody can review.

    Two questions because one is not enough: ancestry (``git branch
    --merged``) misses a squash or rebase merge entirely, and the PR (via
    ``gh``) is the only thing that knows about those. Either one saying yes
    retires the branch.

    Ancestry is guarded by one cheap check first, and the guard is narrower
    than it sounds: ``git branch --merged`` is true whenever ``branch`` is an
    ancestor of the base, and "ancestor" is reflexive — a branch that was just
    cut and never touched is trivially its own base's ancestor, and would
    otherwise read as "merged" before any work happened at all. Comparing tips
    rules out exactly that reflexive case (``branch``'s tip identical to the
    base's). It does **not** establish "is not an ancestor of base" in
    general: a branch cut from a point strictly *behind* the current base — a
    stale local checkout, not a fresh clone or a remote-tracking ref — is a
    real, non-reflexive ancestor with zero commits of its own, and this guard
    does not catch it; it still reads as merged.

    That residual gap is accepted here, not closed: what turns a false
    positive into lost work is resolving a branch more than once after
    cutting it, which is why callers of :func:`_resolve_branch` each resolve
    exactly once per run rather than re-checking after mutating the
    repository. Paid for that way, a false positive here costs an odd branch
    name, not a commit landing on the wrong one. Closing it properly needs
    issuebot to record each branch's own start point, which nothing here does
    yet."""
    base_sha = g.git("rev-parse", "--verify", "--quiet", g.default_branch()).out.strip()
    branch_sha = g.git("rev-parse", "--verify", "--quiet", branch).out.strip()
    diverged = bool(branch_sha) and branch_sha != base_sha

    ancestor_merged = diverged and is_merged(g.folder, branch, proc=g.proc)
    return ancestor_merged or pr_merged(g.folder, branch, proc=g.proc)


def _resolve_branch(g: Git, project: Connection, ref: str) -> str:
    """Which branch this task should work on now.

    Walks the task's attempts in order (:func:`_branch_candidates`) and stops
    at the first one that is usable:

    * a branch that exists (locally **or** on origin) and is **not** finished
      — the task comes back to its own work, which is what makes the
      clarify-and-resume loop cheap;
    * otherwise the first name that does not exist at all — a brand new task,
      or the next attempt after every earlier one merged.

    Deliberately stable under repetition: this is consulted at three call
    sites within a single run (`_prepare_workspace`, `_refresh_workspace`,
    `GitWorkspace.prepare`), and once an attempt's branch is cut it exists and
    is unfinished, so every later call in the same run selects that same
    branch.

    Existence is checked against the remote as well as locally because a
    fresh clone has none of the task's branches locally while origin has all
    of them — re-cutting one that origin already has produces a push that is
    rejected, a wasted run reported as a sink failure."""
    # Loop-invariant: one settings-model read for the whole walk, not one per
    # candidate (see `_branch_name`).
    prefix = conn_setting(project, "branch_prefix", "issuebot/")

    for branch in _branch_candidates(prefix, ref):
        # Ask origin too, so a fresh clone sees the task's earlier attempts.
        g.git("fetch", "origin", branch)  # best-effort; an absent ref is fine
        exists = g.branch_exists(branch) or g.remote_branch_exists(branch)

        if not exists or not _is_finished(g, branch):
            return branch

    # Every candidate `_branch_candidates` probed came back finished. Reusing
    # the last of them would hand back a branch the loop just proved is
    # merged — guaranteeing the empty-diff PR this task exists to prevent.
    # The next name past the probed range is, by construction, one nothing
    # here has checked, let alone proved finished.
    return _branch_name(prefix, ref, attempt=_MAX_BRANCH_ATTEMPTS + 1)


def resolve_worktree_root(cfg_root: str | None) -> Path:
    """Where worktrees live: the config override, else under the state dir."""
    return Path(cfg_root) if cfg_root is not None else state_dir() / "worktrees"


def resolve_clone_root(cfg_root: str | None) -> Path:
    """Where per-task clones live: the config override, else under the state dir."""
    return Path(cfg_root) if cfg_root is not None else state_dir() / "clones"


def _workspace_path(project: Connection, ref: str, root: Path) -> Path:
    """This task's workspace directory under a worktree/clone root."""
    return root / project.key / _safe_ref(ref)


def _workspace_path_for_branch(project: Connection, branch: str, root: Path) -> Path:
    """This attempt's worktree directory, named for the branch it holds.

    A worktree is bound to one branch, so its directory has to follow the
    branch and not the task ref: a task whose first branch merged gets a new
    branch, and handing it the ref-named directory would hand it the worktree
    still sitting on the merged one.

    The prefix is stripped so the directory reads ``PAR-12-2`` rather than
    ``issuebot/PAR-12-2`` — a branch prefix contains a slash, which would
    otherwise silently create a nested directory."""
    prefix = conn_setting(project, "branch_prefix", "issuebot/")
    leaf = branch.removeprefix(prefix)
    return root / project.key / _safe_ref(leaf)


# ---------------------------------------------------------------------------
# Preparing the workspace
# ---------------------------------------------------------------------------


def _sync_branch(g: Git, branch: str) -> None:
    """Fetch and fast-forward ``branch`` to origin's copy.

    A no-op when there is no origin or no such remote branch. Raises
    :class:`BranchDiverged` when a fast-forward is impossible."""
    if not g.has_origin():
        return

    g.git("fetch", "origin", branch)  # best-effort; an absent ref is fine

    remote = g.git("rev-parse", "--verify", "--quiet", f"refs/remotes/origin/{branch}")
    if not remote.ok or not remote.out.strip():
        return  # not on origin yet — nothing to sync

    ff = g.git("merge", "--ff-only", f"origin/{branch}")
    if not ff.ok:
        raise BranchDiverged(branch, ff.message, folder=g.folder, kind="branch")


def _update_base(g: Git, branch: str, mode: str) -> None:
    """Rebase or merge origin's base branch into the task branch.

    A conflict aborts cleanly and raises :class:`BranchDiverged`, so the
    workspace is always left in a state the agent could be handed. No-op when
    the connection's ``update_base`` is "none"."""
    if mode == "none" or not g.has_origin():
        return

    base = g.default_branch()
    g.git("fetch", "origin", base)

    if mode == "rebase":
        if not g.git("rebase", f"origin/{base}").ok:
            g.git("rebase", "--abort")
            raise BranchDiverged(
                branch, f"rebase onto {base} conflicted", folder=g.folder, kind="base", base=base
            )
        return

    if not g.git("merge", "--no-edit", f"origin/{base}").ok:
        g.git("merge", "--abort")
        raise BranchDiverged(
            branch, f"merge of {base} conflicted", folder=g.folder, kind="base", base=base
        )


def _shared_clone_path(project: Connection, clone_root: str | None) -> Path:
    """Where a repo+worktree connection's one shared clone lives.

    ``.shared`` cannot collide with a task's own clone: :func:`_safe_ref`
    strips leading dots, so no task ref ever names a dot-directory. The
    housekeeping side skips dot-directories for the same reason — this clone
    is the worktree strategy's infrastructure, not a per-task workspace."""
    return resolve_clone_root(clone_root) / project.key / ".shared"


def source_repo_folder(project: Connection, clone_root: str | None) -> str:
    """The repository this connection's worktrees are cut from and listed by.

    The connection's own folder when it has one, else the shared clone —
    the one place `git worktree list`/`add`/`remove` can be asked."""
    if project.folder is not None:
        return project.local_folder
    return str(_shared_clone_path(project, clone_root))


def _sync_origin(g: Git, repo: str) -> None:
    """Repoint ``origin`` at ``repo`` when this clone's own remote has drifted.

    A connection's `repo` can change under a clone that already exists — the
    config was edited, or the connection was reconnected to a project that had
    been relinked. The physical clone on disk, keyed by `project.key` under a
    persistent state directory, keeps whatever remote it was created with until
    something tells git otherwise: left alone, every fetch, push and pull
    request would keep hitting the old repository while the config says
    otherwise. Every caller of `_working_copy` that reuses an existing clone —
    including the worktree strategy's one shared clone — routes through here
    before its fetch, so a drifted repo is corrected first.

    ``git remote set-url`` only rewrites a remote that already exists — it
    errors on one with none, and a checkout can be missing ``origin``
    entirely (removed by hand, or a clone from before this plugin always
    added one). That case reads identically to "drifted" here (an empty
    current URL), so it falls through to ``remote add`` instead of a
    ``set-url`` that would turn a merely-unusual checkout into a failed run.
    """
    current = g.git("remote", "get-url", "origin")
    if current.ok and current.out.strip() == repo:
        return

    logger.info("clone origin corrected to %s (was %s)", repo, current.out.strip() or "<unset>")
    verb = "set-url" if current.ok else "add"
    g.check("repoint origin", "remote", verb, "origin", repo)


def _working_copy(
    project: Connection, ref: str, root: str | None, proc: Process, *, shared: bool = False
) -> str:
    """Where this task's working copy is, cloning one if that is what it is.

    The first of the two axes: a connection with a `repo` clones it, and one
    without works from the folder it was given. Nothing here cuts a branch —
    what happens *inside* the copy is the other axis, and keeping them apart
    is what makes "a fresh clone, worked in directly" a thing that can be
    asked for.

    ``shared`` is the worktree strategy's shape: one clone per connection,
    with the per-task copies cut from it as worktrees. Cloning per task *and*
    cutting a worktree from each clone would be two copies of the repository
    for every task, the second of which is the only one used.

    Idempotent either way: an existing clone is fetched, not re-cloned, which
    is what makes the clarify-and-resume loop cheap. Reused, not just
    idempotent: an existing clone whose `repo` has since diverged from the
    connection is repointed (:func:`_sync_origin`) before it is fetched, since
    otherwise "reuse the clone" would silently mean "reuse the wrong remote".
    """
    repo = conn_setting(project, "repo")
    if not repo:
        return project.local_folder

    if shared:
        path = _shared_clone_path(project, root)
    else:
        path = _workspace_path(project, ref, resolve_clone_root(root))
    here = Git(path, proc)

    if path.exists():
        if not (path / ".git").exists():
            raise GitError(f"workspace path exists but is not a git clone: {path}")
        _sync_origin(here, repo)
        _use_gh_credentials(here)
        here.check("fetch", "fetch", "origin")
    else:
        private_dir(path.parent)
        Git(path.parent, proc).check("clone", "clone", "-c", GH_CREDENTIAL_CONFIG, repo, str(path))

    return str(path)


def _use_gh_credentials(g: Git) -> None:
    """Point this clone's git at ``gh``'s credential store.

    The reuse branch's half of what cloning does with
    :data:`GH_CREDENTIAL_CONFIG`: a workspace has to authenticate the same way
    whether this run cut it or an earlier one did. Idempotent, and failure is
    not fatal — a clone that already authenticates some other way keeps
    working.
    """
    g.git("config", "--local", *GH_CREDENTIAL_CONFIG.split("=", 1))


def _start_point(g: Git, project: Connection, branch: str) -> str | None:
    """Where to cut this task's branch when the copy has no local one yet.

    A task can come back long after the working copy that held it went away —
    the clone was pruned, the machine changed, the sandbox was destroyed. The
    branch itself did not: a run permitted `changes` always pushes, so the work
    is on origin under this task's own name. So the remote is asked first, and
    a task branch it already has is where the next turn continues from. Cutting
    from the default branch instead silently reverted the task to nothing and
    then had its push rejected, which is a wasted run reported as a sink
    failure.

    Failing that: a fresh clone starts from ``origin/<default>``, so every new
    task starts from current code. A connection working in its own folder gets
    None and cuts from whatever the user has checked out — their working copy,
    their starting point.
    """
    if not g.has_origin():
        return None

    # Best-effort, exactly as `_sync_branch` fetches: a ref origin does not
    # have is not an error, it is the ordinary case of a brand new task.
    g.git("fetch", "origin", branch)

    if g.remote_branch_exists(branch):
        return f"origin/{branch}"

    return f"origin/{g.default_branch()}" if conn_setting(project, "repo") else None


def _prepare_worktree(
    g: Git,
    project: Connection,
    branch: str,
    root: str | None,
    *,
    start: str | None = None,
) -> str:
    """Ensure this task's own git worktree exists on its task branch, cut from
    the working copy ``g`` names.

    Idempotent: an existing worktree is synced, not re-added. Keyed on the
    branch (:func:`_workspace_path_for_branch`), not the task ref: a re-cut
    branch after a merge must land in its own worktree rather than the one
    still checked out on the branch that merged."""
    path = _workspace_path_for_branch(project, branch, resolve_worktree_root(root))

    if path.exists():
        if not (path / ".git").exists():
            raise GitError(f"workspace path exists but is not a git worktree: {path}")
        _sync_branch(g.at(path), branch)
        return str(path)

    private_dir(path.parent)
    if g.branch_exists(branch):
        g.check("add worktree", "worktree", "add", str(path), branch)
    elif start:
        g.check("add worktree", "worktree", "add", "-b", branch, str(path), start)
    else:
        g.check("add worktree", "worktree", "add", "-b", branch, str(path))
    return str(path)


def _prepare_branch(g: Git, branch: str, *, start: str | None = None) -> str:
    """Check out (or create) the task branch in the working copy ``g`` names.

    ``start`` is where a *new* branch is cut from — :func:`_start_point`'s
    answer, which is the task's own branch on the remote when it has one.
    """
    if g.branch_exists(branch):
        g.check("checkout branch", "checkout", branch)
        _sync_branch(g, branch)
    elif start:
        g.check("create branch", "checkout", "-b", branch, start)
    else:
        g.check("create branch", "checkout", "-b", branch)
    return g.folder


def _prepare_workspace(
    project: Connection,
    ref: str,
    *,
    worktree_root: str | None,
    clone_root: str | None = None,
    proc: Process = REAL,
) -> str:
    """Ensure the agent's workspace exists and return the folder to launch in.

    Two decisions, taken in order and independent of each other: where the
    working copy comes from (:func:`_working_copy` — a fresh clone per task, or
    the connection's own folder), and what is cut inside it (``git_init`` — a
    task branch, a worktree, or neither).

    Idempotent: a second call for the same ref reuses the existing
    branch/worktree/clone, which is what makes the clarify-and-resume loop cheap.
    Raises :class:`GitError` if a git step fails, or :class:`BranchDiverged` when
    the branch needs a human or the agent to reconcile it.

    ``git_init`` absent (None) is working directly in the copy, on whatever
    branch is checked out — not a third strategy alongside worktree/branch, but
    its absence.
    Such a run derives no ``Changes`` (``GitWorkspace.produces_for``), so
    nothing is committed there and the branch is left exactly as found.
    """
    git_init = conn_setting(project, "git_init", None)
    copy = _working_copy(project, ref, clone_root, proc, shared=git_init == "worktree")
    if git_init is None:
        return copy

    g = Git(copy, proc)
    branch = _resolve_branch(g, project, ref)

    # Only consulted when the copy has no local branch of this name yet — a
    # fresh clone, or a folder that never worked this task. Both strategies ask
    # the same question, so both get the same answer.
    start = _start_point(g, project, branch)

    if git_init == "branch":
        folder = _prepare_branch(g, branch, start=start)
    else:
        folder = _prepare_worktree(g, project, branch, worktree_root, start=start)

    # Either strategy keeps the task branch fresh against its base the same way.
    _update_base(Git(folder, proc), branch, conn_setting(project, "update_base", "none"))
    return folder


def _the_existing_clone(root: Path) -> Path | None:
    """The one clone directly under ``root``, or None when there isn't one.

    :func:`_refresh_workspace` uses this to find whichever per-ref clone a project
    checkpoint happened to capture — the checkpoint was taken mid-task, so it
    carries that task's ref-named folder, not the new task's.

    Raises :class:`GitError` if more than one exists: a checkpoint is meant to
    carry exactly one clone (one ephemeral sandbox, one task's workspace), so a
    second one means the warming assumption is broken. Picking one silently would
    be a misfire, not a fix."""
    if not root.is_dir():
        return None

    # Dot-directories are infrastructure (the worktree strategy's shared
    # clone), not a task's workspace — and no task ref names one (`_safe_ref`).
    candidates = [
        child
        for child in sorted(root.iterdir())
        if (child / ".git").exists() and not child.name.startswith(".")
    ]
    if len(candidates) > 1:
        raise GitError(
            f"multiple existing clones under {root}, expected at most one: "
            f"{[str(c) for c in candidates]}"
        )
    return candidates[0] if candidates else None


def _refresh_workspace(
    project: Connection,
    ref: str,
    *,
    base: str | None = None,
    clone_root: str | None = None,
    proc: Process = REAL,
) -> str:
    """Top up an already-cloned workspace instead of cutting a fresh one.

    The warm-boot counterpart to :func:`_prepare_workspace`, for a sandbox booted
    from a populated project checkpoint: the repo is already cloned under the
    project's clone root, from whatever task cold-populated the checkpoint.

    Renames that clone in place to this task's own ref path — so the ordinary
    :func:`_prepare_workspace` call that follows finds an up-to-date, already
    checked-out workspace and does cheap no-op git calls rather than a fresh
    clone — then fetches, hard-resets to ``origin/<base>``, and checks out (or
    creates) the task branch. ``base`` defaults to the repo's own default branch.

    A task that has run before continues from its own branch on the remote
    (:func:`_start_point`), not from the base this checkpoint happens to hold:
    the checkpoint was populated by somebody else's task, and the reset above
    would otherwise put this one back to before it started.

    Raises :class:`GitError` if the checkpoint carries no clone at all, which
    means the warming assumption is broken rather than that there is nothing to
    do."""
    root = resolve_clone_root(clone_root) / project.key
    new_path = _workspace_path(project, ref, resolve_clone_root(clone_root))

    old_path = _the_existing_clone(root)
    if old_path is None:
        raise GitError(f"no existing clone under {root} to warm from")
    if old_path != new_path:
        private_dir(new_path.parent)
        old_path.rename(new_path)

    g = Git(new_path, proc)
    g.check("fetch", "fetch", "origin")
    g.check("reset", "reset", "--hard", f"origin/{base or g.default_branch()}")

    branch = _resolve_branch(g, project, ref)
    _prepare_branch(g, branch, start=_start_point(g, project, branch))
    return str(new_path)


# ---------------------------------------------------------------------------
# Whether the workspace still needs provisioning
# ---------------------------------------------------------------------------

# Dependency-manifest files that decide whether a warm workspace's
# .issuebear.toml provisioning needs to re-run: the bootstrap declaration itself
# plus every lockfile flavour we know about. Deliberately broader than any one
# ecosystem, since a connection's repo could use any of them.
_MANIFEST_FILES = (
    provision.FILENAME,
    "uv.lock",
    "package-lock.json",
    "yarn.lock",
    "pnpm-lock.yaml",
    "poetry.lock",
    "Cargo.lock",
    "Gemfile.lock",
)
_MANIFEST_HASH_FILE = ".issuebot-manifest-hash"


def _manifest_hash(folder: str) -> str:
    """A stable hash over whichever manifest files are present in ``folder``."""
    digest = hashlib.sha256()
    for name in _MANIFEST_FILES:
        path = Path(folder) / name
        if path.exists():
            digest.update(name.encode())
            digest.update(path.read_bytes())
    return digest.hexdigest()


def _manifest_marker(g: Git) -> Path:
    """Where the manifest hash is stored — ``<git-dir>/.issuebot-manifest-hash``.

    Deliberately NOT in the working tree: a marker there is swept up by
    :func:`commit`'s ``git add -A`` and committed onto the task branch, and its
    mere presence makes an otherwise-untouched workspace look dirty. Mirrors
    :func:`issuebot.provision._marker_path`, which keeps its own marker in the git
    dir for exactly this reason. Falls back to the folder for a non-git directory,
    where there is no commit to pollute."""
    r = g.git("rev-parse", "--absolute-git-dir")
    if r.ok and r.out.strip():
        return Path(r.out.strip()) / _MANIFEST_HASH_FILE
    return Path(g.folder) / _MANIFEST_HASH_FILE


def is_git_worktree(folder: str, *, proc: Process = REAL) -> bool:
    """True when ``folder`` is inside a git working tree.

    Lives here rather than in `inventory` (which is about the workspaces this
    plugin has *cut*) because it is the prerequisite of cutting one at all: it
    is what `GitWorkspace.folder_problem` answers with, and `Git` — the only
    thing it needs — is defined in this module."""
    r = Git(folder, proc).git("rev-parse", "--is-inside-work-tree")
    return r.ok and r.out.strip() == "true"


def _needs_provision(folder: str, *, proc: Process = REAL) -> bool:
    """True when this workspace's dependency manifest changed since it was last
    provisioned — or has never been provisioned at all.

    Records the new hash as a side effect, so repeated warm boots of the same
    checkpoint answer False without re-running setup."""
    marker = StateFile(_manifest_marker(Git(folder, proc)))
    current = _manifest_hash(folder)

    stored = marker.read_text()
    if stored is not None and stored.strip() == current:
        return False

    marker.write_text(current)
    return True


# ---------------------------------------------------------------------------
# Committing
# ---------------------------------------------------------------------------


def _commit(folder: str, message: str, *, proc: Process = REAL) -> bool:
    """Commit everything in the workspace, if there is anything to commit.

    Returns True when a commit was made. Used on its own for the work-in-progress
    commit of a paused run, where nothing is published."""
    g = Git(folder, proc)
    if not g.is_dirty():
        return False

    g.check("add", "add", "-A")
    g.check("commit", "commit", "-m", message)
    return True


def _effective_base(g: Git, prepared: Prepared) -> str:
    """The sha the agent's work is diffed (and pushed) against.

    Normally `prepared.base_sha`, the branch tip `prepare` recorded. But a run
    that started with a divergence problem told the agent to *rebase* (the
    reconcile preamble), and a rebase rewrites history: the recorded sha is
    then no longer an ancestor of HEAD, and a diff from it spans the commits
    other contributors pushed — claiming their work as the agent's.

    So when a problem was reported and the recorded sha no longer bounds the
    branch, the base is re-established against origin's copy of the ref that
    gained the commits we lacked: the task branch for "diverged-branch", the
    base branch for "diverged-base". `merge-base` with HEAD lands exactly on
    what the agent rebased onto, so the diff covers only what sits on top of
    everyone else's work. Falls back to the recorded sha if the ref cannot be
    resolved — an honest-but-wide diff beats no Changes at all."""
    base = prepared.base_sha
    problem = prepared.problem
    if problem is None or not base:
        return base

    if g.git("merge-base", "--is-ancestor", base, "HEAD").ok:
        return base  # nothing was rewritten; the recorded sha still bounds the work

    # These kinds are this plugin's own vocabulary — `prepare` minted them.
    ref = problem.base if problem.kind == "diverged-base" else problem.branch
    r = g.git("merge-base", f"origin/{ref}", "HEAD")
    return r.out.strip() if r.ok and r.out.strip() else base


def _push(g: Git, branch: str) -> bool:
    """Push the branch to origin, returning True when it lands.

    Never forced: a rejected push is data (``Changes(pushed=False)``), not a
    retry with ``--force-with-lease`` (deleted, ADR-0012). A reconciled branch
    divergence fast-forwards here, so the plain push suffices; a reconciled
    *base* rebase of an already-pushed branch is the one case that still
    rejects, and it lands as ``pushed=False`` rather than a force."""
    return g.git("push", "-u", "origin", branch).ok


# ---------------------------------------------------------------------------
# The Workspace ABC implementation
# ---------------------------------------------------------------------------


class GitWorkspace(Workspace):
    """A working copy — the connection's folder, or a fresh clone per task —
    with a task branch, a worktree, or nothing cut inside it.

    Git is the workspace that *can* derive `Changes`, which is what `produces`
    says. Whether a given connection does is `produces_for`: that needs a task
    branch, and one that cuts none has nothing to derive them from.
    """

    name = "git"
    produces: ClassVar[frozenset[OutputKind]] = frozenset(
        {"changes", "answer", "needs_input", "handoff"}
    )

    def __init__(self, *, worktree_root: str | None = None, clone_root: str | None = None) -> None:
        # Global roots (`[git]`'s worktree_root/clone_root), not per-connection
        # settings — resolved once by whoever constructs this plugin instance,
        # the same way a harness plugin is constructed with its `command`.
        self._worktree_root = worktree_root
        self._clone_root = clone_root

    def produces_for(self, settings: BaseModel) -> frozenset[OutputKind]:
        """Everything, unless this connection cuts no branch.

        Working directly means the agent is in a copy whose branch is somebody
        else's — the folder the user has open, or the default branch of a fresh
        clone. Committing there is not a smaller version of what a task branch
        does, it is the thing a task branch exists to avoid, so a run that cuts
        no branch reports no `changes` and nothing is committed at all.
        """
        assert isinstance(settings, Settings)
        if settings.git_init is None:
            return self.produces - {"changes"}
        return self.produces

    @classmethod
    def folder_problem(cls, folder: str) -> str | None:
        """Git needs a git repository to cut a branch, worktree or clone from.

        One `rev-parse` — cheap enough for the wizard to call on every keystroke
        of a folder path. Only asked of connections whose keys select this
        workspace, so in-place work in a plain directory is never rejected for
        wanting a repository it does not use."""
        if is_git_worktree(folder):
            return None

        return f"a git workspace requires a git repo: {folder}"

    def refresh(
        self, connection: Connection, ref: str, *, reporter: Reporter, proc: Process = REAL
    ) -> None:
        """Rename the checkpoint's inherited clone onto this task's ref and reset it.

        The warm-boot top-up: :func:`_refresh_workspace` moves and resets the
        clone so the :meth:`prepare` that follows finds an up-to-date checkout,
        and the repo's bootstrap re-runs only when the dependency manifest moved
        since the checkpoint was taken — the run's own provisioning call then
        finds nothing left to do.

        A diverged branch is not this hook's to report: the worker calls it
        bare, so raising would kill the sandbox run with no result at all. The
        branch is left as it stands and :meth:`prepare` — which runs next in
        the pipeline — re-detects the divergence and reports it as
        `Prepared.problem`, the same path a local run takes."""
        try:
            folder = _refresh_workspace(connection, ref, clone_root=self._clone_root, proc=proc)
        except BranchDiverged as exc:
            if not exc.folder:
                raise  # no launchable workspace: the warming assumption broke
            logger.info("refresh met a diverged branch %s; deferring to prepare", exc.branch)
            folder = exc.folder

        if _needs_provision(folder, proc=proc):
            provision.provision(folder, reporter=reporter)

    def prepare(
        self, connection: Connection, ref: str, *, settings: BaseModel, proc: Process = REAL
    ) -> Prepared:
        """Cut (or reuse) the task's working copy, and record its starting sha
        so `commit_and_push` can later diff against exactly what the agent
        started from, not whatever the base branch has become since.

        A diverged branch is not a failure here: `_sync_branch`/`_update_base`
        both leave the workspace clean and checked out when they raise, so the
        divergence comes back as `Prepared.problem` — data the runner routes to
        the agent to reconcile in-workspace — rather than an exception the
        runner would read as an ordinary prep failure."""
        assert isinstance(settings, Settings)
        problem = None
        try:
            folder = _prepare_workspace(
                connection,
                ref,
                worktree_root=self._worktree_root,
                clone_root=self._clone_root,
                proc=proc,
            )
        except BranchDiverged as exc:
            if not exc.folder:
                raise  # no launchable workspace to hand the agent
            folder = exc.folder
            problem = WorkspaceProblem(
                kind=f"diverged-{exc.kind}",
                detail=exc.detail,
                branch=exc.branch,
                base=exc.base or "",
            )
        g = Git(folder, proc)
        # `_prepare_workspace` already resolved and checked out the task branch
        # under every strategy that cuts one (`branch`, `worktree`) — or, for
        # `git_init=None`, left whatever was already current alone. Reading it
        # back here, rather than calling `_resolve_branch` again, is not just
        # avoiding a redundant call: `_resolve_branch` reasons about repository
        # state that cutting the branch just changed, so a second call can
        # legitimately disagree with the first — on a real (non-clone) repo
        # whose task branch was cut from a point behind the base rather than a
        # freshly reset tip, that second look can read the branch it was just
        # given as already merged and hand back a `-2` that nothing is checked
        # out on, while the agent's commits land on the branch `folder` is
        # actually sitting on. One resolution per run, consumed immediately, is
        # what `_refresh_workspace` already does; this mirrors it rather than
        # resolving twice.
        return Prepared(
            folder=folder, branch=g.current_branch(), base_sha=g.head_sha(), problem=problem
        )

    def commit_and_push(
        self, prepared: Prepared, message: str, *, settings: BaseModel, proc: Process = REAL
    ) -> Changes:
        """Commit whatever the agent left, then push unless `settings.push`
        says not to or there is no remote to push to.

        Derives `Changes` from `prepared.base_sha` rather than the base
        branch, so a base rebase mid-run does not get counted as the agent's
        own diff — re-established through :func:`_effective_base` when the run
        started with a divergence the agent was told to rebase away."""
        assert isinstance(settings, Settings)
        g = Git(prepared.folder, proc)
        _commit(prepared.folder, message, proc=proc)

        head_sha = g.head_sha()
        base_sha = _effective_base(g, prepared)
        stat = g.git("diff", "--stat", base_sha, head_sha).out
        # One path per line, so count lines: splitting on whitespace counted a
        # path with a space in it twice.
        names = g.git("diff", "--name-only", base_sha, head_sha).out
        files_changed = len(names.strip().splitlines()) if names.strip() else 0

        pushed = False
        if settings.push and g.has_origin():
            # A reconcile rebase can leave zero net commits (the agent's work
            # was already squash-merged upstream): HEAD then equals the
            # recomputed base, yet the branch is fully on origin. The remote
            # tip says so — report pushed without a needless push.
            remote = g.git(
                "rev-parse", "--verify", "--quiet", f"refs/remotes/origin/{prepared.branch}"
            )

            if remote.ok and remote.out.strip() == head_sha:
                pushed = True
            elif head_sha != base_sha:
                pushed = _push(g, prepared.branch)

        return Changes(
            branch=prepared.branch,
            base_sha=base_sha,
            head_sha=head_sha,
            stat=stat,
            files_changed=files_changed,
            pushed=pushed,
        )
