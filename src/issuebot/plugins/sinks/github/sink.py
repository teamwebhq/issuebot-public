"""The GitHub sink: opens (or reuses) a PR from a pushed branch.

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

Opening a PR — and the diff-driven PR-description generation (``_describe``,
``harness.summarize``) that writes its body — is this sink's own business, not
the workspace's or the run pipeline's (ADR-0012). A connection with no github
sink never pays for a description.
"""

from __future__ import annotations

import json
import logging
import re
import textwrap
from typing import TYPE_CHECKING, ClassVar

from issuebot.contracts import Changed, SinkResult
from issuebot.plugins.sinks.base import Sink
from issuebot.process import REAL, Process

if TYPE_CHECKING:
    from issuebot.contracts import Changes, Delivery, OutputKind
    from issuebot.plugins.harnesses.base import Harness

logger = logging.getLogger("issuebot")

# A diff too large to hand a summarizer model is truncated rather than sent
# whole.
_MAX_DIFF_BYTES = 20000

# Everything up to and including the host, in the two forms git writes a remote:
# `scheme://[user@]host/` and `user@host:`. What follows is the repository path.
_HOST = re.compile(r"^(?:[a-z][a-z0-9+.-]*://[^/]+/|[^/@]+@[^:/]+:)", re.IGNORECASE)


def _capped(text: str, limit: int = _MAX_DIFF_BYTES) -> str:
    """Truncate a diff too large to hand to a model, marking the cut."""
    return text if len(text) <= limit else text[:limit] + "\n…(diff truncated)…"


def _titled(ref: str, title: str) -> str:
    """``"{ref}: {title}"``, with a ref the writer already put in front removed.

    Both paths into a PR title route through here. The model is told not to
    prefix the ref and slips anyway, and the mechanical path's text is the
    agent's own summary, whose first line normally *does* start with the ref —
    so prefixing unconditionally gives the reviewer ``ISS-42: ISS-42: …``. One
    helper on both paths means that cannot happen on either.
    """
    text = title.strip()
    if text.lower().startswith(ref.lower()):
        # Drop the ref, then whatever separated it from the real title.
        text = text[len(ref) :].lstrip(":- \t")

    # Cap what is left, not the raw line: capping first spends the budget on a
    # ref that is about to be stripped, so the title lost its tail for nothing
    # ("…closes any live agen"). `shorten` cuts back to a whole word.
    budget = 72 - len(ref) - len(": ")
    if budget > 0 and len(text) > budget:
        text = textwrap.shorten(text, width=budget, placeholder="")

    return f"{ref}: {text}"


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


def _open_pr(proc: Process, repo: str, branch: str, title: str, body: str) -> str | None:
    """The branch's open PR url, opening one if there isn't already one.

    Scoped with ``pr list --state open`` rather than ``pr view <branch>``: the
    latter also matches a closed or merged PR, so a reused branch would report
    a stale PR from earlier work as this run's. Names the repo explicitly
    rather than relying on a cwd."""
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
        "url",
        "-q",
        ".[0].url",
    ]
    existing = proc.run(argv)
    if existing.ok and existing.out.strip():
        return existing.out.strip()

    created = proc.run(
        ["gh", "pr", "create", "-R", repo, "--head", branch, "--title", title, "--body", body]
    )
    return created.out.strip() or None if created.ok else None


def _describe(
    proc: Process,
    folder: str,
    changes: Changes,
    summary: str,
    *,
    harness: Harness | None,
    model: str | None,
    ref: str,
) -> tuple[str, str]:
    """The PR ``(title, body)``: ask the harness to turn the diff into one,
    falling back to the agent's own change summary (plus ``git diff --stat``)
    when there is no harness, no local checkout to read the diff from, the call
    fails, or it comes back empty — the same fallback ladder the old
    ``local_run._describe`` used, just built from ``Changes``/the agent's own
    ``summary`` output instead of a board fetch, since a sink has no source of
    its own to ask for a task's title.

    A connection with no checkout on this machine (a clone or a sandbox) simply
    starts one rung down that ladder: the diff is the *only* thing here that
    genuinely needs a working copy, and a mechanical description from what the
    agent reported is a better answer than no PR at all."""
    if harness is not None and folder:
        diff = proc.run(["git", "diff", f"{changes.base_sha}...{changes.head_sha}"], cwd=folder).out
        try:
            text = harness.summarize(
                _capped(diff), context=summary, model=model, folder=folder
            ).strip()
        except Exception:  # noqa: BLE001 - a summarizer failure falls back, never fails the PR
            logger.warning(
                "PR summary generation failed for %s; using a mechanical description",
                ref,
                exc_info=True,
            )
        else:
            title, _, body = text.partition("\n")
            if title.strip():
                return _titled(ref, title), (body.strip() or summary)

            # The call worked but gave back nothing usable. The mechanical
            # description below still opens the PR; it must not do so silently.
            logger.warning(
                "PR summary for %s came back unusable; using a mechanical description", ref
            )

    mechanical_title = summary.strip().splitlines()[0] if summary.strip() else ref

    # Markdown, so the diffstat renders as a diffstat rather than one mangled
    # line: the agent's summary as the opening paragraph, then the stat fenced.
    stat = changes.stat.strip()
    parts = [summary.strip(), f"## Changes\n\n```\n{stat}\n```" if stat else ""]
    mechanical_body = "\n\n".join(filter(None, parts))

    return _titled(ref, mechanical_title), (mechanical_body or summary)


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
        """Open (or reuse) a PR from ``delivery.changes``' pushed branch.

        Refuses before making any GitHub call that isn't the verification
        itself: no ``Changes`` at all, no repository it can name, or GitHub's
        own compare API saying the branch carries nothing — each comes back as
        an ordinary failed :class:`~issuebot.contracts.SinkResult` rather than
        opening a PR from nothing."""
        assert isinstance(delivery.output, Changed)
        proc = self._proc
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

        if not _carries_work(proc, repo, changes.base_sha, changes.head_sha):
            return SinkResult(
                sink=self.name, ok=False, summary="branch carries no verified changes"
            )

        title, body = _describe(
            proc,
            delivery.folder,
            changes,
            delivery.output.summary,
            harness=self._harness,
            model=self._summary_model,
            ref=delivery.work.ref,
        )
        url = _open_pr(proc, repo, changes.branch, title, body)
        if url is None:
            return SinkResult(sink=self.name, ok=False, summary="could not open a pull request")
        return SinkResult(sink=self.name, ok=True, summary="opened PR", url=url)
