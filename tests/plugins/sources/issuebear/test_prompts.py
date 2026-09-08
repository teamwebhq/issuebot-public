"""Tests for rendering the launched-agent work-a-task prompt."""

from __future__ import annotations

import pytest

from issuebot.contracts import SkillRef
from issuebot.plugins.sources.issuebear import prompts
from issuebot.plugins.sources.issuebear.prompts import (
    render_mention_prompt,
    render_work_prompt,
)
from issuebot.plugins.workspaces.base import WorkspaceProblem

# ---------------------------------------------------------------------------
# Rendering the board's own instruction document (Task 13)
# ---------------------------------------------------------------------------


def test_the_skills_line_names_each_skill():
    line = prompts.render_skills_line(
        (
            SkillRef(id="1", slug="board-planning", name="Board planning"),
            SkillRef(id="2", slug="board-implementing", name="Board implementing"),
        )
    )
    assert line == (
        "Use your **board-planning** and **board-implementing** skills to do this well."
    )


def test_no_skills_renders_nothing():
    assert prompts.render_skills_line(()) == ""


def test_the_document_is_rendered_with_every_tag_filled():
    """A document is authored on the board against Parade's whole ten-tag
    vocabulary, not against the subset any one render call happens to use —
    so a document may reference a tag this call never explicitly supplies
    (e.g. ``{actor_name}`` in a ``work_task`` document) and it must still
    resolve, to "" rather than raising ``KeyError`` or surviving as literal
    placeholder text.
    """
    document = "".join(f"<{tag}:{{{tag}}}>" for tag in prompts._ALL_TAGS)
    out = prompts.render_work_prompt(
        document=document,
        reference="ISS-9",
        done="review",
        confirm=True,
        skills=(SkillRef(id="1", slug="board-planning", name="Board planning"),),
        agent_instructions="Run the checks.",
    )
    assert "<reference:ISS-9>" in out
    assert "**board-planning**" in out
    assert "Run the checks." in out
    # Tags this call never mentions still resolved to "", not to a KeyError.
    assert "<actor_name:>" in out
    assert "<comment_excerpt:>" in out
    assert "<self_assign_instruction:>" in out


def test_a_missing_document_is_an_error_not_a_guess():
    with pytest.raises(prompts.MissingDocument):
        prompts.render_work_prompt(document="", reference="ISS-9", done="review")


# A generic document to exercise what `render_work_prompt`/`render_mention_prompt`
# themselves compute — confirm wording, response instructions, identity, skills —
# as opposed to what any particular board's document happens to say. Real
# document text is the board's business now (see `docs/superpowers` design);
# these tests only need the plainest document that references the tag under test.
_WORK_DOC = (
    "Task {reference} done={done} confirm={confirm}. {confirm_instruction}\n"
    "{identity}\n{skills}\n{agent_instructions}"
)
_MENTION_DOC = "{reference} {actor_name}: {comment_excerpt}\n{self_assign_instruction}"


def test_work_prompt_states_done_mode() -> None:
    prompt = render_work_prompt(document=_WORK_DOC, reference="ISS-9", done="complete")
    assert "complete" in prompt


def test_confirm_prompt_tells_the_agent_to_wait_for_approval() -> None:
    out = render_work_prompt(document=_WORK_DOC, reference="ISS-1", done="review", confirm=True)
    assert "wait" in out.lower()


def test_no_confirm_prompt_tells_the_agent_not_to_ask_routinely() -> None:
    """`confirm: no` must not read as "confirmation is unavailable" — it stays
    for the irreversible step, which is why the instruction differs rather than
    disappearing."""
    out = render_work_prompt(document=_WORK_DOC, reference="ISS-1", done="review", confirm=False)
    assert "undo" in out.lower()


def test_confirm_flows_through_as_yes_or_no() -> None:
    """The `{confirm}` tag is source.py's yes/no reading of the connection's
    `confirm` boolean, not the boolean's Python repr."""
    yes = render_work_prompt(document=_WORK_DOC, reference="ISS-1", done="review", confirm=True)
    no = render_work_prompt(document=_WORK_DOC, reference="ISS-1", done="review", confirm=False)
    assert "confirm=yes" in yes
    assert "confirm=no" in no


def test_render_mention_prompt_contains_all_template_fields() -> None:
    """render_mention_prompt fills in reference, actor_name, comment_excerpt, and agent_id."""
    out = render_mention_prompt(
        document=_MENTION_DOC,
        reference="ISS-10",
        actor_name="Alice",
        comment_excerpt="Can you fix the login bug?",
        agent_id="u-agent-42",
    )
    assert "ISS-10" in out
    assert "Alice" in out
    assert "Can you fix the login bug?" in out
    assert "u-agent-42" in out


def test_render_mention_prompt_fills_the_boards_own_instructions() -> None:
    """`{agent_instructions}` is one document tag, not one kind of run's: a
    board that writes its step instructions into its `respond_mention`
    document must get them, exactly as a `work_task` document does."""
    out = render_mention_prompt(
        document="{actor_name}: {comment_excerpt}\n{agent_instructions}",
        reference="ISS-10",
        actor_name="Alice",
        comment_excerpt="Can you fix the login bug?",
        agent_id="u-agent-42",
        agent_instructions="Answer in British English.",
    )
    assert "Answer in British English." in out


def test_render_mention_prompt_handles_empty_agent_id() -> None:
    """render_mention_prompt does not crash when agent_id is empty, and falls
    back to a note that self-assignment is unavailable rather than embedding a
    blank id."""
    out = render_mention_prompt(
        document=_MENTION_DOC,
        reference="ISS-11",
        actor_name="Bob",
        comment_excerpt="What is the status?",
        agent_id="",
    )
    assert "ISS-11" in out
    assert "Bob" in out
    # Must not include a stale placeholder literal.
    assert "{agent_id}" not in out
    assert "could not resolve" in out.lower()


def test_reconcile_preamble_branch_kind_instructs_local_rebase_no_push():
    out = prompts.render_reconcile_preamble(
        WorkspaceProblem(kind="diverged-branch", detail="ff-only failed", branch="issuebot/ISS-9")
    )
    assert "issuebot/ISS-9" in out
    assert "git fetch origin" in out
    assert "origin/issuebot/ISS-9" in out
    assert "Do NOT push" in out
    assert "never drop others' work" in out
    assert "comment on the task" in out.lower()


def test_reconcile_preamble_base_kind_weaves_in_base_branch():
    out = prompts.render_reconcile_preamble(
        WorkspaceProblem(
            kind="diverged-base", detail="rebase onto main conflicted", branch="b", base="main"
        )
    )
    assert "origin/main" in out
    assert "rebase onto main conflicted" in out


def test_reconcile_preamble_asks_for_a_merge_when_the_connection_merges_the_base():
    """A connection configured `update_base = "merge"` never wants history
    rewritten. The preamble must tell the agent to merge, not to rebase."""
    out = prompts.render_reconcile_preamble(
        WorkspaceProblem(
            kind="diverged-base",
            detail="merge of main conflicted",
            branch="issuebot/ISS-9",
            base="main",
            reconcile="merge",
        )
    )
    assert "git fetch origin" in out
    assert "origin/main" in out
    assert "Do NOT push" in out
    assert "ebase" not in out, "a merge connection was told to rebase"
    assert "erge" in out


# ---------------------------------------------------------------------------
# The response-file instructions
# ---------------------------------------------------------------------------


def test_the_work_prompt_names_the_response_env_var():
    out = render_work_prompt(document=_WORK_DOC, reference="ISS-1", done="review")
    assert "ISSUEBOT_RESPONSE" in out


def test_the_mention_prompt_names_the_response_env_var():
    out = render_mention_prompt(
        document=_MENTION_DOC,
        reference="ISS-1",
        actor_name="Ada",
        comment_excerpt="hi",
        agent_id="u-1",
    )
    assert "ISSUEBOT_RESPONSE" in out


def test_a_run_permitted_only_an_answer_is_not_told_it_may_hand_off():
    """job.permits is the latitude, not a suggestion: a run that cannot hand off
    or edit code must not be told those kinds exist."""
    out = render_work_prompt(
        document=_WORK_DOC, reference="ISS-1", done="review", permits=frozenset({"answer"})
    )
    assert '"kind": "answer"' in out
    assert '"kind": "handoff"' not in out
    assert '"kind": "changes"' not in out
    assert '"kind": "needs_input"' not in out


def test_the_default_permits_lists_all_four_kinds():
    out = render_work_prompt(document=_WORK_DOC, reference="ISS-1", done="review")
    for kind in ("changes", "answer", "needs_input", "handoff"):
        assert f'"kind": "{kind}"' in out


# ---------------------------------------------------------------------------
# The response block is appended after render, not substituted into the
# document. `render_work_prompt` renders both `work_task` and `respond_task`
# documents (source.py picks which text to hand it), so these two documents
# stand in for all three prompt kinds between them.
# ---------------------------------------------------------------------------

_MODERN_WORK_DOC = "Task {reference} done={done}. {agent_instructions}\nDo the work."
_MODERN_MENTION_DOC = "{reference} {actor_name}: {comment_excerpt}\n{self_assign_instruction}"


def test_the_response_block_is_appended_exactly_once_to_a_modern_work_prompt():
    out = render_work_prompt(document=_MODERN_WORK_DOC, reference="ISS-1", done="review")
    assert out.count("ISSUEBOT_RESPONSE") == 1
    assert out.count("---") == 1
    assert out.rstrip().endswith("never reaches the controller.")


def test_the_response_block_is_appended_exactly_once_to_a_modern_mention_prompt():
    out = render_mention_prompt(
        document=_MODERN_MENTION_DOC,
        reference="ISS-1",
        actor_name="Ada",
        comment_excerpt="hi",
        agent_id="u-1",
    )
    assert out.count("ISSUEBOT_RESPONSE") == 1
    assert out.count("---") == 1


def test_permits_filtering_still_works_on_the_appended_block():
    out = render_work_prompt(
        document=_MODERN_WORK_DOC,
        reference="ISS-1",
        done="review",
        permits=frozenset({"answer"}),
    )
    assert '"kind": "answer"' in out
    assert '"kind": "handoff"' not in out


def test_reconcile_preamble_does_not_duplicate_the_response_block():
    """The preamble is prepended before the whole rendered prompt (which
    already carries its own appended block), so a reconciling run must still
    see the block exactly once."""
    out = render_work_prompt(document=_MODERN_WORK_DOC, reference="ISS-1", done="review")
    preambled = (
        prompts.render_reconcile_preamble(
            WorkspaceProblem(kind="diverged-branch", detail="d", branch="b")
        )
        + out
    )
    assert preambled.count("ISSUEBOT_RESPONSE") == 1
