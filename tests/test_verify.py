"""Tests for the controller-side structural verification of an agent's
response — forge-agnostic, no network. See the design's "Verification, in two
places": this is the first half; the GitHub sink's substantive check (does the
branch really carry work) is the second, in a later task."""

from __future__ import annotations

from issuebot.contracts import Changed, Changes, Handoff, NeedsInput, Output, Response
from issuebot.verify import verify

ALL_PERMITS = frozenset({"changes", "answer", "needs_input", "handoff"})


def _changes(*, moved: bool = True) -> Changes:
    return Changes(
        branch="issuebot/ISS-1",
        base_sha="a",
        head_sha="b" if moved else "a",
        stat="1 file changed",
        files_changed=1,
        pushed=True,
    )


def test_a_clean_response_has_no_problems():
    response = Response(status="done", outputs=[Changed(summary="did stuff")], changes=_changes())
    assert verify(response, ALL_PERMITS) == []


def test_an_empty_response_has_no_problems():
    assert verify(Response(status="done"), ALL_PERMITS) == []


# ---------------------------------------------------------------------------
# 1. every output.kind is in permits
# ---------------------------------------------------------------------------


def test_a_kind_outside_permits_is_a_problem():
    response = Response(status="done", outputs=[Changed(summary="did stuff")], changes=_changes())
    problems = verify(response, frozenset({"answer"}))
    assert any("changes" in p and "not permitted" in p for p in problems)


def test_a_kind_inside_permits_is_fine():
    response = Response(status="done", outputs=[Changed(summary="did stuff")], changes=_changes())
    assert verify(response, frozenset({"changes"})) == []


# ---------------------------------------------------------------------------
# 2. each kind's required field is present and non-empty
# ---------------------------------------------------------------------------


def test_a_bare_output_missing_its_required_field_is_a_problem():
    """The base `Output` class is instantiable on its own — a `Changed`
    carries `summary` by construction, but a bare `Output(kind="changes")`
    does not, so this check is not fully redundant with pydantic's own field
    validation."""
    bare = Output(kind="changes")
    response = Response(status="done", outputs=[bare], changes=_changes())
    problems = verify(response, ALL_PERMITS)
    assert any("missing its required field" in p for p in problems)


def test_a_properly_typed_output_always_has_its_field():
    response = Response(status="done", outputs=[Changed(summary="did stuff")], changes=_changes())
    assert not any("missing its required field" in p for p in verify(response, ALL_PERMITS))


# ---------------------------------------------------------------------------
# 3. at most one decision
# ---------------------------------------------------------------------------


def test_two_decisions_is_a_problem():
    response = Response(
        status="done",
        outputs=[NeedsInput(question="which env?"), Handoff(assignee="sam")],
    )
    problems = verify(response, ALL_PERMITS)
    assert any("at most one decision" in p for p in problems)


def test_one_decision_is_fine():
    response = Response(status="done", outputs=[NeedsInput(question="which env?")])
    assert verify(response, ALL_PERMITS) == []


def test_any_number_of_deliverables_is_fine():
    response = Response(
        status="done",
        outputs=[Changed(summary="did stuff"), Changed(summary="did more stuff")],
        changes=_changes(),
    )
    assert verify(response, ALL_PERMITS) == []


def test_a_deliverable_and_a_decision_together_is_fine():
    response = Response(
        status="done",
        outputs=[Changed(summary="did stuff"), NeedsInput(question="which env?")],
        changes=_changes(),
    )
    assert verify(response, ALL_PERMITS) == []


# ---------------------------------------------------------------------------
# 4. for `changes`: head_sha != base_sha
# ---------------------------------------------------------------------------


def test_a_changes_output_with_no_actual_change_is_a_problem():
    response = Response(
        status="done", outputs=[Changed(summary="did stuff")], changes=_changes(moved=False)
    )
    problems = verify(response, ALL_PERMITS)
    assert any("no actual change" in p for p in problems)


def test_a_changes_output_with_no_changes_record_at_all_is_a_problem():
    response = Response(status="done", outputs=[Changed(summary="did stuff")], changes=None)
    problems = verify(response, ALL_PERMITS)
    assert any("no actual change" in p for p in problems)


def test_a_changes_output_that_really_moved_head_is_fine():
    response = Response(status="done", outputs=[Changed(summary="did stuff")], changes=_changes())
    assert verify(response, ALL_PERMITS) == []


# ---------------------------------------------------------------------------
# Every problem is reported, not just the first
# ---------------------------------------------------------------------------


def test_every_problem_is_reported_at_once():
    response = Response(
        status="done",
        outputs=[Changed(summary="did stuff"), NeedsInput(question="q"), Handoff(assignee="sam")],
        changes=_changes(moved=False),
    )
    problems = verify(response, frozenset({"answer"}))
    # kind-not-permitted x3, at-most-one-decision, and the empty-changes check.
    assert len(problems) >= 4
