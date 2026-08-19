"""The git workspace's cross-field rejection table (design spec, "Validation").

Two independent axes, so the table has two independent halves: where the working
copy comes from (`repo` against `folder` — exactly one), and what is cut inside
it (`git_init`, which `branch_prefix`/`update_base` need). Nothing relates the
two, which is the point: every pairing is a connection somebody could want.

Plus the rule that reaches outside git's own fields: a sink declaring
`needs_pushed_branch` cannot be served by a workspace configured never to push.
"""

from __future__ import annotations

from typing import ClassVar

import pytest

from issuebot import plugins
from issuebot.config import Connection
from issuebot.plugins.base import SinkPlugin
from issuebot.plugins.sinks.fake.sink import FakeSink
from issuebot.plugins.workspaces.git.validate import validate


def _conn(**extra) -> Connection:
    """A minimal connection carrying whatever git-relevant fields a case needs."""
    return Connection.model_validate({"name": "p", "board": "b", **extra})


# --- where the working copy comes from ---------------------------------------

INVALID = [
    pytest.param(
        {"folder": "/repo", "repo": "https://x/r.git"},
        "not both",
        id="folder+repo",
    ),
    pytest.param(
        {"folder": "/repo", "repo": "https://x/r.git", "git_init": "branch"},
        "not both",
        id="folder+repo+branch",
    ),
    pytest.param({}, "nowhere to work", id="neither"),
    pytest.param({"git_init": "branch"}, "nowhere to work", id="branch-with-neither"),
    pytest.param(
        {"folder": "/repo", "branch_prefix": "bot/"},
        "cuts no branch",
        id="branch_prefix-without-git_init",
    ),
    pytest.param(
        {"folder": "/repo", "update_base": "rebase"},
        "cuts no branch to update",
        id="update_base-without-git_init",
    ),
]


@pytest.mark.parametrize("fields, message", INVALID)
def test_rejects_invalid_combinations(fields: dict, message: str) -> None:
    problems = list(validate(_conn(**fields)))
    assert any(message in p for p in problems), problems


# --- every pairing of the two axes is valid ----------------------------------

VALID = [
    pytest.param({"folder": "/repo"}, id="folder+direct"),
    pytest.param({"folder": "/repo", "git_init": "branch"}, id="folder+branch"),
    pytest.param({"folder": "/repo", "git_init": "worktree"}, id="folder+worktree"),
    pytest.param({"repo": "https://x/r.git"}, id="clone+direct"),
    pytest.param({"repo": "https://x/r.git", "git_init": "branch"}, id="clone+branch"),
    pytest.param({"repo": "https://x/r.git", "git_init": "worktree"}, id="clone+worktree"),
]


@pytest.mark.parametrize("fields", VALID)
def test_accepts_every_pairing_of_the_two_axes(fields: dict) -> None:
    """Where the copy comes from and what is cut in it are independent. The
    four-valued `git_init` could not say "a fresh clone, worked in directly" at
    all, and this is the row that proves it now can."""
    assert list(validate(_conn(**fields))) == []


# --- the rule that reaches outside git's own fields --------------------------


def test_the_workspace_strategy_says_nothing_about_read_only_work() -> None:
    """`mode` is not git's business either way.

    A rule here used to require a `git_init` strategy for `mode='respond'`. It
    is deleted: the strategy decides *where* work happens and `permits` decides
    *what may come back*, and deriving one from the other made respond mode
    unreachable — the wizard forces `isolation='none'` for it, this forced the
    opposite."""
    assert list(validate(_conn(folder="/repo", mode="respond"))) == []
    assert list(validate(_conn(folder="/repo", mode="respond", git_init="branch"))) == []


class _PublishingSink(FakeSink):
    """A sink that publishes from a branch the remote can already see."""

    name: ClassVar[str] = "publisher"
    needs_pushed_branch: ClassVar[bool] = True


@pytest.fixture
def two_sinks(monkeypatch: pytest.MonkeyPatch) -> None:
    """A registry holding one sink that needs a push and one that does not.

    Stubs rather than the shipped sinks: the rule is about what a sink
    *declares*, so a test naming a real one would be testing that plugin's
    declaration rather than git's rule — and would go red or vacuous the day it
    is deleted. Patching `all_of` is enough to move the whole registry:
    `plugins.get` and `plugins.names_of` both read through it.
    """
    sinks = {
        "publisher": SinkPlugin(name="publisher", sink=_PublishingSink),
        "quiet": SinkPlugin(name="quiet", sink=FakeSink),
    }
    monkeypatch.setattr(plugins, "all_of", lambda kind: sinks if kind == "sinks" else {})


def test_a_sink_that_publishes_from_a_branch_needs_push_enabled(two_sinks: None) -> None:
    """`push=False` leaves a sink that publishes from a pushed branch nothing to
    publish, and the message names the sink that objected."""
    problems = list(
        validate(_conn(folder="/repo", git_init="branch", push=False, sinks=["publisher"]))
    )

    assert any("needs 'push' enabled" in p for p in problems)
    assert any("publisher" in p for p in problems)


def test_push_is_only_required_by_a_sink_that_says_so(two_sinks: None) -> None:
    """The rule is the sink's declaration, not "this connection has sinks" —
    a sink that publishes some other way is fine without a push."""
    assert (
        list(validate(_conn(folder="/repo", git_init="branch", push=False, sinks=["quiet"]))) == []
    )


def test_push_enabled_satisfies_every_sink(two_sinks: None) -> None:
    """`push` defaults to true, which is what the rule is asking for."""
    assert list(validate(_conn(folder="/repo", git_init="branch", sinks=["publisher"]))) == []


def test_an_unknown_sink_is_left_to_the_check_that_names_it(two_sinks: None) -> None:
    """`validate_config` already reports a sink nothing answers to, by name.
    Saying it twice, in git's words, would be noise."""
    assert (
        list(validate(_conn(folder="/repo", git_init="branch", push=False, sinks=["nope"]))) == []
    )
