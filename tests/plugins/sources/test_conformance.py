"""One suite every source plugin runs against.

Shared rather than per-source, so a new source is held to the contract by
construction — mirrors `tests/plugins/workspaces/test_conformance.py` and
`tests/plugins/harnesses/test_conformance.py`.

`work()`/`mention()` (from conftest) build a `WorkItem` whose `kind` is shared,
forge-agnostic vocabulary (`contracts.WorkKind`), not an issuebear concept —
so a rule about what a *mention* may report is a rule every source is held to,
not a peek at one implementation's internals.
"""

from __future__ import annotations

import pytest

from conftest import FakeApi, connection, ctx, mention, work
from issuebot import plugins, runner
from issuebot.contracts import McpServer, Response
from issuebot.plugins.sources.base import Source

SOURCES = plugins.names_of("sources")


@pytest.fixture(params=SOURCES)
def source(request: pytest.FixtureRequest) -> Source:
    """Every installed source, built over a fake board client the way the runner
    builds one.

    Through `runner.source_for` rather than by calling the class, because a
    source is handed its own global settings table there and one of the rules
    below is about what those settings let it hand the agent. A fixture that
    constructed the class bare would hold every source to a contract in a state
    production never builds one in."""
    return runner.source_for(FakeApi(), connection(source=request.param), ctx())


def test_every_source_subclasses_the_abc(source: Source) -> None:
    """A source plugin's implementation must actually be a Source."""
    assert isinstance(source, Source)


def test_every_source_names_itself(source: Source) -> None:
    """A source's `name` must match the plugin name it is registered under."""
    assert source.name in SOURCES


def test_permits_never_exceeds_the_four_known_kinds(source: Source) -> None:
    assert source.permits(work()) <= {"changes", "answer", "needs_input", "handoff"}


def test_an_assignment_permits_every_kind(source: Source) -> None:
    assert source.permits(work()) == {"changes", "answer", "needs_input", "handoff"}


def test_a_mention_cannot_produce_changes(source: Source) -> None:
    """Not because mentions are special-cased downstream, but because this
    source says so about its own work kinds."""
    assert "changes" not in source.permits(mention())


def test_a_source_that_does_not_lock_still_returns_a_claim(source: Source) -> None:
    """A mention is delivered with the board's own non-locking run — never a
    race to win, so `claim` never returns `None` for one, and `release` has
    whatever it takes to make releasing it a no-op."""
    claim = source.claim(mention(run_id=None))
    assert claim is not None
    source.release(claim, Response(status="done"))  # must not raise


def test_heartbeat_is_deliverable_through_every_source(source: Source) -> None:
    """`run.execute` keeps every claimed run alive through `Source.heartbeat`,
    so every source must accept one — its own board call or the ABC's no-op."""
    source.heartbeat("r-1")  # must not raise


class _Leaseless(Source):
    """A source implementing only the ABC's abstract methods — what a board
    with no run lease would ship."""

    name = "leaseless"

    @classmethod
    def client(cls, cfg):
        return FakeApi()

    def poll(self, *, timeout):
        return []

    def claim(self, work):
        return None

    def release(self, claim, response):
        return None

    def say(self, work, message):
        return None

    def apply(self, work, decision):
        return None

    def finish(self, work, response, results):
        return None

    def permits(self, work):
        return frozenset()

    def prompt(self, work, connection, *, permits, problem=None):
        return ""

    def agent_access(self, work):
        return ()


def test_a_source_with_no_lease_concept_inherits_a_noop_heartbeat() -> None:
    """The heartbeat is part of the ABC, concrete: a source whose board has no
    run lease implements nothing and the runner's heartbeat loop is a clean
    no-op, not an `AttributeError` logged every interval while the board
    silently expires the lock."""
    _Leaseless().heartbeat("r-1")  # must not raise


def test_agent_access_is_a_tuple_of_mcp_servers(source: Source) -> None:
    access = source.agent_access(work())
    assert isinstance(access, tuple)
    assert all(isinstance(server, McpServer) for server in access)


def test_prompt_carries_the_reference(source: Source) -> None:
    item = work(reference="ISS-1")
    assert "ISS-1" in source.prompt(item, connection(), permits=source.permits(item))


@pytest.mark.parametrize("withheld", ["changes", "answer", "needs_input", "handoff"])
def test_a_prompt_never_offers_an_output_kind_the_job_forbids(
    source: Source, withheld: str
) -> None:
    """`permits` is the *job's* latitude — the source's own judgement already
    narrowed by what the connection's workspace can produce — so a source that
    renders from its own answer instead tells a folder-workspace run it may
    report `changes`, which the controller then rejects it for. Offering a kind
    the run cannot deliver is an instruction to fail, so the rule is checked for
    every kind, not just the one that actually went wrong."""
    item = work()
    permits = source.permits(item) - {withheld}

    prompt = source.prompt(item, connection(), permits=permits)

    assert f'"kind": "{withheld}"' not in prompt
    for offered in permits:
        assert f'"kind": "{offered}"' in prompt
