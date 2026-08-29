"""The GitHub sink: opens (or updates) a PR from a pushed branch.

``deliver`` verifies against the forge itself before opening anything —
verification lives in two places: the controller's own check
(:func:`issuebot.verify.verify`) is structural and forge-agnostic (did the
agent's claimed ``head_sha`` move at all), while this is the substantive half,
asking GitHub's own compare API whether the branch really carries a commit
ahead of its base. That is stronger than trusting :class:`~issuebot.contracts.
Changes` — a push that failed while the environment still reported success is
caught here, not upstream.

**Every ``gh`` call names its repository explicitly** (``gh -R owner/name``).
The obvious alternative — letting ``gh`` infer the repo from the git remote of
whatever directory it runs in — quietly ties this sink to a local checkout, and
a sink runs controller-side: for a clone-based connection there is no persistent
checkout, and for a sandboxed one the clone only ever existed inside the
sandbox. There is no cwd to infer from, so the repo is resolved once, up front,
and every call is scoped by it. That resolution is a ladder (the connection's
configured repo URL, else the ``origin`` of a checkout it does keep) but what
comes out is one value feeding one code path — a connection with a checkout and
one without take exactly the same route through this module.

Opening a PR — and the PR-description generation (``_describe``,
``harness.summarize``) that writes its body — is this sink's own business, not
the workspace's or the run pipeline's (ADR-0012). A connection with no github
sink never pays for a description.

**Every run rewrites the whole description.** A second run on the same branch
does not append to the pull request it finds; it describes the pull request as
it now stands and replaces the title and the body with that. The alternative —
keeping the first run's text — leaves the description telling a reviewer about
half the work in front of them. The cost is that an edit a person made to the
body does not survive the next run, so put such notes in a review comment.
"""

from __future__ import annotations

import json
import logging
import re
import tempfile
from typing import TYPE_CHECKING, ClassVar

from issuebot.contracts import Changed, SinkResult
from issuebot.plugins.sinks.base import Sink
from issuebot.process import REAL, Process, with_env
from issuebot.summaries import titled

if TYPE_CHECKING:
    from collections.abc import Mapping

    from issuebot.contracts import Changes, Delivery, OutputKind
    from issuebot.plugins.harnesses.base import Harness

logger = logging.getLogger("issuebot")

# Everything up to and including the host, in the two forms git writes a remote:
# `scheme://[user@]host/` and `user@host:`. What follows is the repository path.
_HOST = re.compile(r"^(?:[a-z][a-z0-9+.-]*://[^/]+/|[^/@]+@[^:/]+:)", re.IGNORECASE)


def _slug(url: str) -> str:
    """``owner/name`` from a git remote URL, or ``""`` when it carries neither.

    The host is stripped first, deliberately. Matching two path segments off the
    *end* of the string looks equivalent and is not: given a URL with only one
    path segment it backs into the hostname and answers ``example.com/repo`` —
    truthy, so the caller skips its own "cannot name a repository" refusal and
    the user gets a raw 404 from ``gh`` instead. A local path (``/tmp/repo.git``,
    ``file:///tmp/repo.git``) has no host at all and is refused outright: it is
    not a repository this sink can name.
    """
    trimmed = url.strip().removesuffix("/").removesuffix(".git")
    path = _HOST.sub("", trimmed, count=1)
    if path == trimmed:
        return ""  # no host matched, so nothing here names a forge

    segments = [segment for segment in path.split("/") if segment]
    # The last two: a forge nests deeper than owner/name in places, and `gh -R`
    # wants the repository, not the group path above it.
    return "/".join(segments[-2:]) if len(segments) >= 2 else ""


def origin(proc: Process, folder: str) -> str:
    """The ``origin`` remote URL of a local checkout, or ``""``.

    Public because this sink's ``doctor`` asks the same question. Asked with
    ``git`` directly rather than of a workspace plugin — a sink importing a
    workspace is the one direction the plugin boundary rules out.
    """
    result = proc.run(["git", "remote", "get-url", "origin"], cwd=folder)
    return result.out.strip() if result.ok else ""


def repo_of(proc: Process, delivery: Delivery) -> str:
    """The GitHub repository this delivery belongs to, as ``owner/name``.

    The connection's own repo URL when it has one — the only answer available
    to a clone-based or sandboxed connection, which keeps no checkout on this
    machine — else the ``origin`` of the checkout it does keep. Empty when
    neither names a repository, which the caller reports as an ordinary failed
    delivery rather than guessing.
    """
    return _slug(delivery.repo) or (_slug(origin(proc, delivery.folder)) if delivery.folder else "")


def _carries_work(proc: Process, repo: str, base_sha: str, head_sha: str) -> bool:
    """True when GitHub's own compare API says ``head_sha`` is actually ahead
    of ``base_sha`` — the substantive check described in this module's
    docstring."""
    result = proc.run(["gh", "api", f"repos/{repo}/compare/{base_sha}...{head_sha}"])
    if not result.ok:
        return False
    try:
        payload = json.loads(result.out)
    except (json.JSONDecodeError, TypeError):
        return False
    return bool(payload.get("ahead_by"))


def _signed(body: str, delivery: Delivery) -> str:
    """The PR body, with the clanker that did the work named at the foot of it.

    The pull request's *actor* is the app whose token opened it — GitHub has no
    per-token display name, so an app cannot post as a named agent. The commits
    carry the clanker as their author, and this puts the same name where a
    reviewer reading the description will see it.
    """
    author = delivery.forge_env.get("GIT_AUTHOR_NAME", "")
    return f"{body}\n\n---\n\nOpened by **{author}**." if author else body


def _existing_pr(proc: Process, repo: str, branch: str) -> tuple[int, str] | None:
    """The branch's open pull request as ``(number, url)``, or ``None``.

    Scoped with ``pr list --state open`` rather than ``pr view <branch>``: the
    latter also matches a closed or merged PR, so a reused branch would report
    a stale PR from earlier work as this run's. Names the repo explicitly
    rather than relying on a cwd.

    The number is kept beside the url because a second run rewrites the pull
    request it finds, and ``gh pr edit`` is addressed by number.
    """
    argv = [
        "gh",
        "pr",
        "list",
        "-R",
        repo,
        "--head",
        branch,
        "--state",
        "open",
        "--json",
        "number,url",
    ]
    result = proc.run(argv)
    if not result.ok:
        return None

    # An empty list, a body that is not JSON, a row missing either field: all
    # of them mean the same thing here — nothing open to write to.
    try:
        row = json.loads(result.out)[0]
        return int(row["number"]), str(row["url"])
    except (json.JSONDecodeError, TypeError, KeyError, IndexError, ValueError):
        return None


def _change(repo: str, folder: str, changes: Changes, number: int | None) -> str:
    """Prose telling the summarizer where the change it must describe is.

    Handed to the harness whole: the harness carries it into its prompt and
    never reads it, so knowing how a GitHub change is looked at stays here with
    the rest of this sink's forge knowledge.

    An existing pull request is described from the *whole* pull request, not
    from this run's slice of it. On a second run ``changes.base_sha`` is the
    first run's tip, so the range below would describe only the increment while
    the reviewer reads the lot.

    A checkout answers without the network; a clone-based or sandboxed
    connection keeps none on this machine, so it is named a ``gh`` command
    instead and gets the same model-written description.
    """
    if number is not None:
        commands = [f"`gh pr diff {number} -R {repo}`"]
        if folder:
            commands.append(f"`git log {changes.branch}` in the current directory")
        return (
            f"The change is the whole of pull request #{number} of `{repo}`, "
            f"which this run has just added commits to. Read it with "
            f"{' and '.join(commands)}."
        )

    span = f"{changes.base_sha}...{changes.head_sha}"
    if folder:
        return (
            f"The change is the git range `{span}` in the current directory. "
            f"Read it with `git diff {span}`, `git log {span}` and `git show`."
        )

    return (
        f"The change is the git range `{span}` of `{repo}`. Read it with "
        f'`gh api -H "Accept: application/vnd.github.v3.diff" '
        f"repos/{repo}/compare/{span}`."
    )


def _create_pr(proc: Process, repo: str, branch: str, body: str, *, title: str) -> str | None:
    """Open a pull request from ``branch`` and return its url, or ``None``."""
    created = proc.run(
        ["gh", "pr", "create", "-R", repo, "--head", branch, "--title", title, "--body", body]
    )
    return created.out.strip() or None if created.ok else None


def _rewrite_pr(proc: Process, repo: str, number: int, body: str, *, title: str) -> bool:
    """Replace pull request ``number``'s title and body. True when it took.

    The whole description, not an addition to it — see this module's docstring
    for why, and for what that costs a person who edited the body by hand.
    """
    edited = proc.run(
        ["gh", "pr", "edit", str(number), "-R", repo, "--title", title, "--body", body]
    )
    return edited.ok


def _describe(
    folder: str,
    changes: Changes,
    summary: str,
    *,
    change: str,
    harness: Harness | None,
    model: str | None,
    guidance: str,
    ref: str,
    env: Mapping[str, str],
) -> tuple[str, str, str]:
    """The PR ``(title, body, fallback_reason)``: ask the harness to read the
    change ``change`` names and write one, falling back to the agent's own
    change summary (plus ``git diff --stat``) when there is no harness, the
    call fails, or it comes back empty.

    The harness is told where to look rather than handed a diff: a change too
    large for one prompt used to be described from a truncated copy of itself,
    which is how a big pull request got a description of half of it.

    ``guidance`` is ``Delivery.guidance`` (the board's own PR-writing skill,
    already resolved), forwarded to the harness untouched — this function does
    not read it itself, only carries it to where it is used. ``env`` is the
    run's forge credentials, carried the same way, so a ``gh`` call the agent
    makes while reading authenticates as whoever pushed the branch.

    ``fallback_reason`` is empty when the model wrote the description and a
    short phrase naming the rung that was taken when it did not. The caller puts
    it in the delivery summary: a mechanical description is a visible downgrade,
    and a log line on a runner is not where the person who reads the PR looks.
    """
    reason = "no summarizer harness configured"

    if harness is not None:
        try:
            # `summarize` runs a child process, so it needs a cwd that exists.
            # A connection with no checkout has none — a scratch directory
            # keeps the child out of whatever directory the listener itself
            # happens to be sitting in.
            with tempfile.TemporaryDirectory() as scratch:
                text = harness.summarize(
                    context=summary,
                    change=change,
                    model=model,
                    folder=folder or scratch,
                    guidance=guidance,
                    env=env,
                ).strip()

        except Exception as exc:  # noqa: BLE001 - a summarizer failure falls back, never fails the PR
            reason = f"summarizer failed ({type(exc).__name__})"
            logger.warning(
                "PR summary generation failed for %s; using a mechanical description",
                ref,
                exc_info=True,
            )

        else:
            title, _, body = text.partition("\n")
            if title.strip():
                return titled(ref, title), (body.strip() or summary), ""

            # The call worked but gave back nothing usable. The mechanical
            # description below still opens the PR; it must not do so silently.
            reason = "summary came back unusable"
            logger.warning(
                "PR summary for %s came back unusable; using a mechanical description", ref
            )

    mechanical_title = summary.strip().splitlines()[0] if summary.strip() else ref

    # Markdown, so the diffstat renders as a diffstat rather than one mangled
    # line: the agent's summary as the opening paragraph, then the stat fenced.
    stat = changes.stat.strip()
    parts = [summary.strip(), f"## Changes\n\n```\n{stat}\n```" if stat else ""]
    mechanical_body = "\n\n".join(filter(None, parts))

    return titled(ref, mechanical_title), (mechanical_body or summary), reason


class GitHubSink(Sink):
    """Opens a PR from a pushed task branch, once GitHub itself confirms it
    carries work."""

    name: ClassVar[str] = "github"
    accepts: ClassVar[frozenset[OutputKind]] = frozenset({"changes"})

    # A PR is opened from a branch GitHub can already see, so a workspace that
    # commits without pushing leaves this sink nothing to open one from. The
    # git workspace's validation reads this and rejects that combination.
    needs_pushed_branch: ClassVar[bool] = True

    def __init__(
        self,
        *,
        harness: Harness | None = None,
        summary_model: str | None = None,
        proc: Process = REAL,
    ) -> None:
        """``harness``/``summary_model``/``proc`` are resolved once by whoever
        constructs this plugin instance (mirrors ``GitWorkspace``'s own
        ``worktree_root``/``clone_root``) — all default so the conformance
        suite's bare ``plugin.sink()`` construction still works, falling back
        to a mechanical PR description with no summarizer call.
        """
        self._harness = harness
        self._summary_model = summary_model
        self._proc = proc

    def deliver(self, delivery: Delivery) -> SinkResult:
        """Open a PR from ``delivery.changes``' pushed branch, or rewrite the
        description of the one that branch already has.

        Refuses before making any GitHub call that isn't the verification
        itself: no ``Changes`` at all, no repository it can name, a branch that
        never reached origin, or GitHub's own compare API saying the branch
        carries nothing — each comes back as an ordinary failed
        :class:`~issuebot.contracts.SinkResult` rather than opening a PR from
        nothing."""
        assert isinstance(delivery.output, Changed)
        # Every `gh` call below authenticates as whatever the source lent for
        # this run (`Delivery.forge_env`) — the same identity that pushed the
        # branch, so it is not pushed by one actor and opened by another.
        # Untouched when nothing was lent: the machine's own `gh` credential.
        proc = with_env(self._proc, delivery.forge_env)
        changes = delivery.changes

        if changes is None or changes.empty:
            return SinkResult(
                sink=self.name, ok=False, summary="no pushed changes to open a PR from"
            )

        repo = repo_of(proc, delivery)
        if not repo:
            return SinkResult(
                sink=self.name,
                ok=False,
                summary="could not tell which GitHub repository this connection uses",
            )

        # An unpushed branch is asked about before the compare API, because
        # GitHub has never seen its head sha: the compare call would fail for a
        # purely local reason and read back as "the branch carries nothing",
        # which is the opposite of what happened.
        if not changes.pushed:
            return SinkResult(sink=self.name, ok=False, summary="branch was not pushed to origin")

        if not _carries_work(proc, repo, changes.base_sha, changes.head_sha):
            return SinkResult(
                sink=self.name, ok=False, summary="branch carries no verified changes"
            )

        # The pull request being written to is found before anything is
        # written, because what the description describes depends on it: a
        # branch with no PR yet is the whole change, while a branch that has
        # one is described from the whole of that PR.
        existing = _existing_pr(proc, repo, changes.branch)

        title, body, fallback = _describe(
            delivery.folder,
            changes,
            delivery.output.summary,
            change=_change(repo, delivery.folder, changes, existing[0] if existing else None),
            harness=self._harness,
            model=self._summary_model,
            guidance=delivery.guidance,
            ref=delivery.work.ref,
            env=delivery.forge_env,
        )
        signed = _signed(body, delivery)

        notes = [f"mechanical description: {fallback}"] if fallback else []

        if existing is None:
            url = _create_pr(proc, repo, changes.branch, signed, title=title)
            if url is None:
                return SinkResult(sink=self.name, ok=False, summary="could not open a pull request")
            verb = "opened PR"

        else:
            number, url = existing
            # A failed rewrite is not a failed delivery: the branch is pushed
            # and the pull request is there to read. It is reported, not
            # raised, so the reviewer knows the description is the older one.
            if _rewrite_pr(proc, repo, number, signed, title=title):
                verb = "updated PR"
            else:
                verb = "reused PR"
                notes.append("description not updated")

        # A mechanical or unwritten description says so where the person
        # reading the task comment will see it, not only in the runner's log.
        note = f"{verb} ({'; '.join(notes)})" if notes else verb

        return SinkResult(sink=self.name, ok=True, summary=note, url=url)
