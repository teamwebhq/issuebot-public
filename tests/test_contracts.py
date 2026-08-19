"""The vocabulary every layer shares.

Two payloads come back from a run and they have different trust levels: `changes`
is derived from git by infrastructure, `outputs` is authored by the agent. Tests
here pin that distinction, because it is the thing most likely to be eroded by a
convenience later.
"""

from __future__ import annotations

import pytest

from issuebot.contracts import (
    Changes,
    Handoff,
    NeedsInput,
    Response,
    WorkItem,
    parse_outputs,
)


def test_a_work_item_is_built_from_a_source_payload():
    item = WorkItem.from_api({"task_id": "t1", "reference": "ISS-1", "kind": "mention"})
    assert item.task_id == "t1"
    assert item.ref == "ISS-1"


def test_the_ref_falls_back_to_the_task_id():
    assert WorkItem(task_id="t1").ref == "t1"


def test_changes_with_an_unmoved_head_produced_nothing():
    assert Changes(branch="b", base_sha="a1", head_sha="a1", stat="", files_changed=0).empty


def test_changes_with_a_moved_head_produced_something():
    assert not Changes(branch="b", base_sha="a1", head_sha="b2", stat="", files_changed=1).empty


def test_outputs_parse_from_the_agents_document():
    outputs = parse_outputs(
        '{"outputs": [{"kind": "changes", "summary": "did it"},'
        ' {"kind": "handoff", "assignee": "u-1", "note": "please review"}]}'
    )
    assert [o.kind for o in outputs] == ["changes", "handoff"]
    assert outputs[1].assignee == "u-1"


def test_an_empty_document_parses_to_no_outputs():
    """A run that deliberately reports nothing is different from a missing file."""
    assert parse_outputs('{"outputs": []}') == []


@pytest.mark.parametrize("raw", ["not json", "{}", '{"outputs": "nope"}', '{"outputs": [{}]}'])
def test_a_malformed_document_is_rejected(raw):
    with pytest.raises(ValueError):
        parse_outputs(raw)


def test_an_unknown_kind_is_rejected_rather_than_guessed():
    with pytest.raises(ValueError):
        parse_outputs('{"outputs": [{"kind": "banana"}]}')


def test_deliverables_and_decisions_are_distinguishable():
    assert not NeedsInput(question="which?").is_deliverable
    assert not Handoff(assignee="u-1").is_deliverable


def test_a_response_carries_both_trust_levels():
    response = Response(
        status="done",
        changes=Changes(branch="b", base_sha="a", head_sha="c", stat="", files_changed=1),
        outputs=[NeedsInput(question="which?")],
    )
    assert response.changes.branch == "b"
    assert response.decisions == response.outputs
    assert response.deliverables == []
