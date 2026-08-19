"""One suite every workspace plugin runs against.

Shared rather than per-workspace, so a new workspace is held to the contract
by construction — mirrors `tests/plugins/harnesses/test_conformance.py`.
"""

from __future__ import annotations

from typing import get_args

import pytest

from issuebot import plugins
from issuebot.contracts import OutputKind
from issuebot.plugins.base import WorkspacePlugin
from issuebot.plugins.workspaces.base import Prepared, Workspace, WorkspaceProblem
from issuebot.runner import unconfigured_workspace

WORKSPACES = plugins.names_of("workspaces")


def test_every_workspace_declares_what_it_can_produce():
    """`permits` is intersected with this at load, so asking a workspace for an
    output it cannot derive is rejected in config rather than failing inside a
    run. Against `OutputKind` itself, not a copy of it: a restated list drifts.

    *Which* kinds a given workspace claims is that plugin's own business and is
    asserted in that plugin's own test directory — a name here is a name outside
    the plugin, which is the whole thing this axis is trying not to have."""
    for name in WORKSPACES:
        produces = plugins.get("workspaces", name).workspace.produces
        assert produces and produces <= set(get_args(OutputKind))


@pytest.fixture(params=WORKSPACES)
def workspace(request: pytest.FixtureRequest) -> Workspace:
    """Every installed workspace, constructed with no arguments."""
    return plugins.get("workspaces", request.param).workspace()


def test_every_workspace_subclasses_the_abc(workspace: Workspace) -> None:
    """A workspace plugin's implementation must actually be a Workspace."""
    assert isinstance(workspace, Workspace)


def test_every_workspace_names_itself(workspace: Workspace) -> None:
    """A workspace's `name` must match the plugin name it is registered under."""
    assert workspace.name in WORKSPACES


def test_prepared_is_a_frozen_dataclass_with_a_folder() -> None:
    """The one thing every `Workspace.prepare` call must hand back."""
    prepared = Prepared(folder="/work/alpha")
    assert prepared.folder == "/work/alpha"
    assert prepared.branch == ""
    assert prepared.base_sha == ""
    with pytest.raises(AttributeError):
        prepared.folder = "/work/beta"  # type: ignore[misc]


def test_prepared_carries_an_optional_problem() -> None:
    """`prepare` can report a condition the run proceeds through only if the
    agent is told — divergence today — as data on `Prepared`, never as an
    exception the runner would read as an ordinary prep failure. Absent by
    default: most prepares have nothing to say."""
    assert Prepared(folder="/work/alpha").problem is None

    problem = WorkspaceProblem(kind="diverged-branch", detail="ff-only failed", branch="b")
    prepared = Prepared(folder="/work/alpha", problem=problem)
    assert prepared.problem is not None
    assert prepared.problem.kind == "diverged-branch"

    with pytest.raises(AttributeError):
        problem.kind = "other"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# What a connection that claims no workspace keys resolves to
# ---------------------------------------------------------------------------


def _stub(name: str, produces: frozenset[str]) -> WorkspacePlugin:
    """A workspace plugin that exists only to be resolved between."""

    class _Stub(Workspace):
        pass

    _Stub.name = name  # ty: ignore[invalid-assignment]
    _Stub.produces = produces  # ty: ignore[invalid-assignment]
    return WorkspacePlugin(name=name, workspace=_Stub)


def _registry(monkeypatch: pytest.MonkeyPatch, *stubs: WorkspacePlugin) -> None:
    """Make `stubs` the entire workspace axis.

    `all_of` is the whole registry as far as `plugins.get` and
    `plugins.names_of` are concerned, so patching it alone leaves no shipped
    plugin able to answer instead — patching `names_of` on its own would make
    every assertion below tautological.
    """
    monkeypatch.setattr(
        plugins,
        "all_of",
        lambda kind: {p.name: p for p in stubs} if kind == "workspaces" else {},
    )


def test_the_shipped_tree_resolves_a_keyless_connection_without_being_told_a_name():
    """The live check, against whatever is installed: asking for no strategy
    resolves to a real installed workspace rather than a refusal.

    Only that. *Which* one, and why, is pinned by the stubbed tests below, which
    own the registry and so can state the rule without stating the tree — the
    property this one would most like to assert ("it derives no `changes`") is
    true of the tree that ships and false of a tree whose only workspace has a
    version-control strategy, which is a legitimate install."""
    assert unconfigured_workspace().name in WORKSPACES


def test_the_workspace_that_derives_nothing_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    """Resolved on the capability each workspace declares, never on a name —
    which is what the hard-coded `plugins.get("workspaces", "folder")` was."""
    _registry(
        monkeypatch,
        _stub("versioned", frozenset({"changes", "answer"})),
        _stub("plain", frozenset({"answer"})),
    )
    assert unconfigured_workspace().name == "plain"


def test_the_only_workspace_wins_even_when_it_derives_changes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The rung that stops the capability becoming a monopoly: delete the
    workspace that declares it and a keyless connection must still run, because
    with one installed there is no choice to get wrong."""
    _registry(monkeypatch, _stub("versioned", frozenset({"changes", "answer"})))
    assert unconfigured_workspace().name == "versioned"


def test_two_workspaces_deriving_nothing_are_refused_with_the_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No non-arbitrary answer, so it says so and names what is there rather
    than taking whichever sorted first."""
    _registry(
        monkeypatch,
        _stub("plain", frozenset({"answer"})),
        _stub("scratch", frozenset({"answer"})),
    )
    with pytest.raises(plugins.UnknownPlugin, match="2 of them do: plain, scratch"):
        unconfigured_workspace()


def test_several_workspaces_that_all_derive_changes_are_refused_in_their_own_words(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rung 1 finds nothing and rung 2 cannot choose — the branch the first
    version of this message got wrong.

    It derived its count from the candidates it was about to refuse, which on
    this shape are rung *2*'s, so it announced "2 of them derive no changes"
    when the true number is zero and sent the reader hunting for two workspaces
    that do not exist. Each rung now reports its own count."""
    _registry(
        monkeypatch,
        _stub("alpha", frozenset({"changes", "answer"})),
        _stub("beta", frozenset({"changes", "answer"})),
    )
    with pytest.raises(plugins.UnknownPlugin, match="none does, and the 2 installed"):
        unconfigured_workspace()


def test_no_workspace_at_all_is_refused_with_the_list(monkeypatch: pytest.MonkeyPatch) -> None:
    """The axis emptied entirely — a sentence naming what is installed, not a
    `StopIteration` or an `ImportError` five frames down."""
    _registry(monkeypatch)
    with pytest.raises(plugins.UnknownPlugin, match="none does.*installed: none"):
        unconfigured_workspace()


def test_a_refusal_names_the_two_remedies_that_exist(monkeypatch: pytest.MonkeyPatch) -> None:
    """There is no `workspace = "..."` key, so a reader sent to `config.toml`
    would look for one forever. That is the price of the design decision, and
    the message is the only place a user ever meets it."""
    _registry(
        monkeypatch,
        _stub("plain", frozenset({"answer"})),
        _stub("scratch", frozenset({"answer"})),
    )
    with pytest.raises(plugins.UnknownPlugin, match="uninstalling one or by giving"):
        unconfigured_workspace()
