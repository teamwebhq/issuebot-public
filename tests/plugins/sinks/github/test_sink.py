"""Tests for the GitHub sink: opening (or reusing) a PR from a pushed branch."""

from __future__ import annotations

from pathlib import Path

import pytest

from conftest import completed, sandbox_connection, work
from issuebot.contracts import Changed, Changes, Delivery, Response, SinkResult
from issuebot.plugins.harnesses.fake.harness import FakeHarness
from issuebot.plugins.sinks.github.sink import GitHubSink, _slug
from issuebot.process import RecordingProcess


def _changes(**overrides: object) -> Changes:
    base: dict[str, object] = {
        "branch": "issuebot/ISS-1",
        "base_sha": "base-sha",
        "head_sha": "head-sha",
        "stat": "1 file changed, 2 insertions(+)",
        "files_changed": 1,
        "pushed": True,
    }
    base.update(overrides)
    return Changes(**base)  # type: ignore[arg-type]


# A sentinel distinct from `None`, which is itself a real value under test
# (`changes=None` — no `Changes` at all, as opposed to "use the default one").
_DEFAULT_CHANGES = object()


def _delivery(
    *,
    changes: Changes | None | object = _DEFAULT_CHANGES,
    summary: str = "did the thing",
    repo: str = "https://github.com/o/r.git",
    folder: str = "/repo",
    ref: str = "ISS-1",
) -> Delivery:
    return Delivery(
        work=work(reference=ref),
        output=Changed(summary=summary),
        changes=_changes() if changes is _DEFAULT_CHANGES else changes,  # type: ignore[arg-type]
        repo=repo,
        folder=folder,
    )


def _happy(**replies: object) -> RecordingProcess:
    """A process that answers every call this sink makes on the happy path.

    A test's own patterns go in first, because `RecordingProcess` matches in
    insertion order: scripting the compare endpoint asked for a diff must not be
    swallowed by the broader `gh api` default sitting in front of it.
    """
    scripted: dict[str, object] = dict(replies)
    for pattern, reply in {
        "gh api": completed(out='{"ahead_by": 3}'),
        "gh pr list": completed(out=""),
        "gh pr create": completed(out="https://github.com/o/r/pull/9\n"),
    }.items():
        scripted.setdefault(pattern, reply)
    return RecordingProcess(replies=scripted)  # type: ignore[arg-type]


def test_the_github_sink_opens_a_pull_request() -> None:
    proc = _happy()

    result = GitHubSink(proc=proc).deliver(_delivery())

    assert result.ok
    assert result.sink == "github"
    assert result.url == "https://github.com/o/r/pull/9"
    assert any(c[:3] == ["gh", "pr", "create"] for c in proc.calls)


def test_it_verifies_against_the_forge_before_opening_anything() -> None:
    """Asking gh is stronger than trusting the environment's report: a push that
    failed while reporting success is caught here."""
    proc = _happy(**{"gh api": completed(out='{"ahead_by": 0}')})

    result = GitHubSink(proc=proc).deliver(_delivery())

    assert not result.ok
    assert not any(c[:3] == ["gh", "pr", "create"] for c in proc.calls)


def test_a_compare_the_forge_cannot_answer_is_not_treated_as_work() -> None:
    """`gh api compare` failing is not the same as it saying "no commits", and
    the sink must not read one as the other. It fails when the branch never
    reached the forge at all (a push that silently did not happen: 404 on the
    head sha), and that is precisely the case where opening a PR would be
    wrong — so a failed call means "no work here", not "assume there is"."""
    proc = _happy(**{"gh api": completed(code=1, err="gh: Not Found (HTTP 404)")})

    result = GitHubSink(proc=proc).deliver(_delivery())

    assert not result.ok
    assert not any(c[:3] == ["gh", "pr", "create"] for c in proc.calls)


def test_a_failed_compare_is_not_believed_even_when_it_printed_a_body() -> None:
    """The exit code is authoritative, not the body: `gh` can fail *and* leave
    something parseable on stdout (a cached page, a proxy's error envelope, a
    partial write). Reading that body would turn a call that did not answer into
    a confident "yes, three commits ahead".

    The 404 case above cannot pin this on its own — its stdout is empty, so the
    JSON decode fails and the *next* guard returns False for a different reason.
    Only a failed call carrying a body that says otherwise proves the exit code
    is what decided."""
    proc = _happy(**{"gh api": completed(code=1, out='{"ahead_by": 3}', err="gh: server error")})

    result = GitHubSink(proc=proc).deliver(_delivery())

    assert not result.ok
    assert not any(c[:3] == ["gh", "pr", "create"] for c in proc.calls)


# ---------------------------------------------------------------------------
# Naming the repository rather than inferring it from a cwd
# ---------------------------------------------------------------------------


def test_every_github_call_names_the_repository() -> None:
    """`gh` would happily infer the repo from the git remote of the directory it
    runs in — and that ties this sink to a local checkout it cannot count on
    having. Naming the repo explicitly is what makes the clone and sandbox
    connections work at all."""
    proc = _happy()

    GitHubSink(proc=proc).deliver(_delivery())

    api_call = next(c for c in proc.calls if c[:2] == ["gh", "api"])
    assert api_call[2] == "repos/o/r/compare/base-sha...head-sha"
    for call in (c for c in proc.calls if c[0] == "gh"):
        assert "-R" not in call or call[call.index("-R") + 1] == "o/r"


def test_a_connection_with_no_checkout_delivers_the_same_way() -> None:
    """The cwd hole: a clone-based or sandboxed connection's `Changes` are as
    real as a local one's — the environment really did push — but no checkout
    for `gh` to run in exists on this machine. Same code path, same result."""
    proc = _happy()

    result = GitHubSink(proc=proc).deliver(_delivery(folder=""))

    assert result.ok
    assert result.url == "https://github.com/o/r/pull/9"


def test_a_connection_with_no_repo_url_reads_its_checkouts_remote() -> None:
    """An in-place or worktree connection configures no clone URL — there is
    nothing to clone — so the repository is whatever its checkout points at."""
    proc = _happy(**{"git remote get-url": completed(out="git@github.com:o/r.git\n")})

    result = GitHubSink(proc=proc).deliver(_delivery(repo=""))

    assert result.ok
    api_call = next(c for c in proc.calls if c[:2] == ["gh", "api"])
    assert api_call[2].startswith("repos/o/r/")


def test_it_refuses_when_it_cannot_name_a_repository() -> None:
    """Better an honest failed delivery than a `gh` call that silently acts on
    whatever repository the runner happens to be sitting in."""
    proc = _happy()

    result = GitHubSink(proc=proc).deliver(_delivery(repo="", folder=""))

    assert not result.ok
    assert "repository" in result.summary
    assert not any(c[0] == "gh" for c in proc.calls)


@pytest.mark.parametrize(
    ("url", "slug"),
    [
        ("https://github.com/o/r.git", "o/r"),
        ("https://github.com/o/r", "o/r"),
        ("https://github.com/o/r/", "o/r"),
        ("git@github.com:o/r.git", "o/r"),
        ("ssh://git@github.com/o/r.git", "o/r"),
        # Deeper than owner/name: `gh -R` wants the repository, not the group.
        ("https://gitlab.com/grp/sub/r.git", "sub/r"),
    ],
)
def test_it_reads_the_repository_out_of_the_forms_git_writes(url: str, slug: str) -> None:
    assert _slug(url) == slug


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/r.git",  # a host and one segment — no owner
        "/tmp/repo.git",  # a local path
        "file:///tmp/repo.git",  # a local path with a scheme
        "not a url",
        "",
    ],
)
def test_a_url_that_names_no_repository_reads_as_none(url: str) -> None:
    """Matching two segments off the *end* would back into the hostname here and
    answer `example.com/r` — truthy, so the sink would skip its own refusal and
    hand the user a raw 404 from `gh` instead of a message they can act on."""
    assert _slug(url) == ""


def test_a_repo_url_naming_no_repository_is_refused_not_guessed_at() -> None:
    """The end-to-end shape of the case above: a connection whose `repo` has no
    owner segment gets the honest refusal, not a `gh` call against its host."""
    proc = _happy()

    result = GitHubSink(proc=proc).deliver(_delivery(repo="https://example.com/r.git", folder=""))

    assert not result.ok
    assert "repository" in result.summary
    assert not any(c[0] == "gh" for c in proc.calls)


# ---------------------------------------------------------------------------
# Opening, reusing and refusing
# ---------------------------------------------------------------------------


def test_reuses_an_already_open_pr_instead_of_opening_a_second_one() -> None:
    proc = _happy(
        **{
            "gh api": completed(out='{"ahead_by": 1}'),
            "gh pr list": completed(out="https://github.com/o/r/pull/4\n"),
        }
    )

    result = GitHubSink(proc=proc).deliver(_delivery())

    assert result.ok
    assert result.url == "https://github.com/o/r/pull/4"
    assert not any(c[:3] == ["gh", "pr", "create"] for c in proc.calls)


def test_refuses_when_there_are_no_changes_at_all() -> None:
    proc = RecordingProcess()
    result = GitHubSink(proc=proc).deliver(_delivery(changes=None))
    assert not result.ok
    assert proc.calls == []


def test_refuses_when_the_branch_is_empty() -> None:
    """`base_sha == head_sha` means the agent produced nothing, whatever the
    output claims — refused before any `gh` call, exactly like the no-`Changes`
    case above."""
    proc = RecordingProcess()
    empty = _changes(head_sha="base-sha")
    result = GitHubSink(proc=proc).deliver(_delivery(changes=empty))
    assert not result.ok
    assert proc.calls == []


def test_refuses_an_unpushed_branch_by_saying_it_was_never_pushed() -> None:
    """The branch carries the work — it just never left the runner, so GitHub
    has never heard of its head sha. The refusal has to say that, not blame the
    branch for carrying nothing."""
    proc = _happy()
    result = GitHubSink(proc=proc).deliver(_delivery(changes=_changes(pushed=False)))
    assert not result.ok
    assert "not pushed" in result.summary
    assert not any(c[:2] == ["gh", "api"] for c in proc.calls)


# ---------------------------------------------------------------------------
# The PR description
# ---------------------------------------------------------------------------


def test_uses_a_mechanical_description_with_no_harness() -> None:
    """No harness configured (the plugin conformance suite's bare
    `plugin.sink()`, or a summarizer nobody wired up) still opens a PR, titled
    and bodied from the agent's own change summary."""
    proc = _happy(**{"gh api": completed(out='{"ahead_by": 2}')})

    result = GitHubSink(proc=proc).deliver(_delivery(summary="fixed the thing"))

    assert result.ok
    create = next(c for c in proc.calls if c[:3] == ["gh", "pr", "create"])
    title = create[create.index("--title") + 1]
    body = create[create.index("--body") + 1]
    assert "fixed the thing" in title
    assert "fixed the thing" in body
    # No harness means no diff was even fetched.
    assert not any(c[:2] == ["git", "diff"] for c in proc.calls)


def test_uses_the_harness_summary_when_one_is_available() -> None:
    proc = _happy(**{"git diff": completed(out="--- a\n+++ b\n")})
    harness = FakeHarness(summary="Add the widget\n\nBecause it was missing.")

    result = GitHubSink(harness=harness, summary_model="haiku", proc=proc).deliver(_delivery())

    assert result.ok
    assert result.summary == "opened PR"
    assert len(harness.summarize_calls) == 1
    diff, context, model, folder = harness.summarize_calls[0]
    assert diff == "--- a\n+++ b\n"
    assert context == "did the thing"  # the agent's own Changed.summary
    assert model == "haiku"
    assert folder == "/repo"

    create = next(c for c in proc.calls if c[:3] == ["gh", "pr", "create"])
    assert create[create.index("--title") + 1].endswith("Add the widget")
    assert "Because it was missing." in create[create.index("--body") + 1]


def test_a_checkout_reads_its_diff_locally_and_asks_the_forge_for_nothing() -> None:
    """With a working copy there is no reason to spend an API call on the diff."""
    proc = _happy(**{"git diff": completed(out="--- a\n+++ b\n")})
    harness = FakeHarness(summary="Add the widget")

    result = GitHubSink(harness=harness, proc=proc).deliver(_delivery())

    assert result.ok
    assert any(c[:2] == ["git", "diff"] for c in proc.calls)
    assert not any("vnd.github.v3.diff" in " ".join(c) for c in proc.calls)


class _CwdWatchingHarness(FakeHarness):
    """A FakeHarness that also notes whether its cwd existed when it was called.

    `summarize` runs a child process, so the folder it is handed has to be a
    real directory *at the time of the call* — which nothing can tell from the
    recorded arguments afterwards, since a scratch directory is gone by then."""

    folder_existed = False

    def summarize(self, diff: str, *, context: str, model: str | None, folder: str) -> str:
        """Note the cwd's existence, then answer as FakeHarness does."""
        self.folder_existed = bool(folder) and Path(folder).is_dir()
        return super().summarize(diff, context=context, model=model, folder=folder)


def test_no_checkout_still_gets_the_model_written_description() -> None:
    """A clone-based or sandboxed connection keeps no working copy here, so the
    diff comes from the forge — the description is the model's either way."""
    proc = _happy(**{"vnd.github.v3.diff": completed(out="--- a\n+++ b\n")})
    harness = _CwdWatchingHarness(summary="Add the widget\n\nBecause it was missing.")

    result = GitHubSink(harness=harness, proc=proc).deliver(
        _delivery(folder="", summary="fixed the thing")
    )

    assert result.ok
    assert result.summary == "opened PR"

    diff, _, _, _ = harness.summarize_calls[0]
    assert diff == "--- a\n+++ b\n"
    assert harness.folder_existed  # a real cwd, never the listener's own

    create = next(c for c in proc.calls if c[:3] == ["gh", "pr", "create"])
    assert create[create.index("--title") + 1].endswith("Add the widget")
    assert "Because it was missing." in create[create.index("--body") + 1]


def test_a_diff_nobody_can_supply_says_so_in_the_delivery_summary() -> None:
    """The PR still opens with the mechanical description, and the person
    reading the task comment is told why it reads that way."""
    proc = _happy(**{"vnd.github.v3.diff": completed(code=1, err="not found")})
    harness = FakeHarness(summary="Add the widget")

    result = GitHubSink(harness=harness, proc=proc).deliver(
        _delivery(folder="", summary="fixed the thing")
    )

    assert result.ok
    assert harness.summarize_calls == []
    assert "no diff available" in result.summary

    create = next(c for c in proc.calls if c[:3] == ["gh", "pr", "create"])
    assert "fixed the thing" in create[create.index("--title") + 1]


def test_falls_back_to_a_mechanical_description_when_the_harness_says_nothing() -> None:
    """An empty summarizer reply (or one that fails) must not crash the sink,
    nor open a PR with a blank title — the mechanical fallback still applies."""
    proc = _happy(**{"git diff": completed(out="")})
    harness = FakeHarness(summary="")

    result = GitHubSink(harness=harness, proc=proc).deliver(_delivery(summary="the fallback text"))

    assert result.ok
    create = next(c for c in proc.calls if c[:3] == ["gh", "pr", "create"])
    assert "the fallback text" in create[create.index("--title") + 1]


def test_a_mechanical_title_does_not_repeat_a_ref_the_agent_already_wrote() -> None:
    """The agent's own change summary normally opens with the reference, so
    prefixing it again hands the reviewer `ISS-152: ISS-152: ...`."""
    proc = _happy()

    result = GitHubSink(proc=proc).deliver(
        _delivery(ref="ISS-152", summary="ISS-152: remove_member now closes live agents")
    )

    assert result.ok
    create = next(c for c in proc.calls if c[:3] == ["gh", "pr", "create"])
    title = create[create.index("--title") + 1]
    assert title.count("ISS-152") == 1
    assert title == "ISS-152: remove_member now closes live agents"


def test_a_long_mechanical_title_is_cut_back_to_a_whole_word() -> None:
    """The 72-character budget is measured after the duplicate ref is gone —
    spending it on a ref that is then stripped is what truncated the reported
    PR mid-word ("...closes any live agen")."""
    proc = _happy()
    summary = (
        "ISS-152: remove_member (api/routers/members.py) now closes any live "
        "agent sessions belonging to the removed member"
    )

    result = GitHubSink(proc=proc).deliver(_delivery(ref="ISS-152", summary=summary))

    assert result.ok
    create = next(c for c in proc.calls if c[:3] == ["gh", "pr", "create"])
    title = create[create.index("--title") + 1]
    assert len(title) <= 72
    assert title.count("ISS-152") == 1
    # Every word kept is a whole word from the summary, so nothing ends mid-word.
    words = summary.removeprefix("ISS-152:").split()
    assert title.removeprefix("ISS-152: ").split() == words[: len(title.split()) - 1]


def test_a_model_title_that_repeats_the_ref_is_not_prefixed_twice() -> None:
    """The summarizer is told not to write the ref and sometimes does anyway."""
    proc = _happy(**{"git diff": completed(out="--- a\n+++ b\n")})
    harness = FakeHarness(summary="ISS-152 - Add the widget\n\nBecause it was missing.")

    result = GitHubSink(harness=harness, proc=proc).deliver(_delivery(ref="ISS-152"))

    assert result.ok
    create = next(c for c in proc.calls if c[:3] == ["gh", "pr", "create"])
    title = create[create.index("--title") + 1]
    assert title.count("ISS-152") == 1
    assert title == "ISS-152: Add the widget"


def test_the_mechanical_body_fences_the_diffstat_under_a_changes_heading() -> None:
    """A bare stat renders as one mangled line; fenced, it reads as a diffstat."""
    proc = _happy()
    changes = _changes(stat="api/routers/members.py | 12 ++++---\n1 file changed")

    result = GitHubSink(proc=proc).deliver(_delivery(changes=changes, summary="fixed the thing"))

    assert result.ok
    create = next(c for c in proc.calls if c[:3] == ["gh", "pr", "create"])
    body = create[create.index("--body") + 1]
    assert body.startswith("fixed the thing")
    assert "## Changes\n\n```\napi/routers/members.py | 12 ++++---\n1 file changed\n```" in body


def test_it_declares_that_it_needs_a_pushed_branch() -> None:
    """A PR is opened from a branch the forge can already see. The declaration
    is what git's workspace validation reads to reject `push = false` on a
    connection wired to this sink — the rule is stated here, not there."""
    assert GitHubSink.needs_pushed_branch


# ---------------------------------------------------------------------------
# Delivering for a connection with no local checkout
# ---------------------------------------------------------------------------


def test_a_required_sink_does_not_fail_a_successful_clone_connection():
    """The cwd hole, end to end. A clone-based or sandboxed connection's
    `Changes` are as real as a local one's — the environment really did push —
    but no checkout for a sink's own tools to run in exists on this machine. A
    sink that inferred its repository from a cwd failed such a run for a purely
    local reason and blamed the branch, silently dropping whatever decision came
    with it."""
    from issuebot import run as run_pipeline
    from issuebot.config import SinkRef
    from issuebot.process import Completed

    conn = sandbox_connection(repo="https://github.com/o/r.git")
    assert conn.folder is None  # the workspace lives only in the sandbox/clone

    # `gh` answers from the forge, not from a directory — which is the point.
    proc = RecordingProcess(
        replies={
            "gh api": Completed([], 0, '{"ahead_by": 3}'),
            "gh pr list": Completed([], 0, ""),
            "gh pr create": Completed([], 0, "https://github.com/o/r/pull/9"),
            # Anything needing a working copy must never be reached.
            "git ": Completed([], 128, "", "not a git repository"),
        }
    )

    changes = Changes(
        branch="issuebot/ISS-1",
        base_sha="a",
        head_sha="b",
        stat="1 file",
        files_changed=1,
        pushed=True,
    )
    response = Response(status="done", changes=changes, outputs=[Changed(summary="did stuff")])
    sinks = [(SinkRef(name="github", required=True), GitHubSink(proc=proc))]

    results = run_pipeline.deliver_all(work(), response, conn, sinks=sinks)

    assert not run_pipeline.required_failed(results, sinks), (
        f"a genuinely successful run was reported as a required-sink failure: {results}"
    )
    # No summarizer is wired up here, so the description is the mechanical one
    # and the delivery summary says so.
    assert results == [
        SinkResult(
            sink="github",
            ok=True,
            summary="opened PR (mechanical description: no summarizer harness configured)",
            url="https://github.com/o/r/pull/9",
        )
    ]
