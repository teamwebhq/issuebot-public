"""Tests for rendering the launched-agent work-a-task prompt."""

from __future__ import annotations

from issuebot.plugins.sources.issuebear import prompts
from issuebot.plugins.sources.issuebear.prompts import (
    render_mention_prompt,
    render_work_prompt,
)
from issuebot.plugins.workspaces.base import WorkspaceProblem


def test_work_prompt_names_the_task_skills_and_first_move() -> None:
    prompt = render_work_prompt(reference="ISS-42", done="review")
    assert "ISS-42" in prompt
    assert "review" in prompt
    assert "board-brainstorming" in prompt
    assert "board-implementing" in prompt
    assert "board-planning" in prompt
    assert "get_task" in prompt


def test_work_prompt_states_done_mode() -> None:
    prompt = render_work_prompt(reference="ISS-9", done="complete")
    assert "complete" in prompt


def test_work_prompt_always_asks_for_a_plan() -> None:
    """Planning is not a mode any more — both settings plan, so both prompts
    have to say so."""
    for confirm in (True, False):
        prompt = render_work_prompt(reference="ISS-1", done="review", confirm=confirm)
        assert "set_plan" in prompt


def test_confirm_prompt_tells_the_agent_to_wait_for_approval() -> None:
    out = render_work_prompt(reference="ISS-1", done="review", confirm=True)
    assert "request_confirmation" in out
    assert "wait" in out.lower()


def test_no_confirm_prompt_tells_the_agent_not_to_ask_routinely() -> None:
    """`confirm: no` must not read as "confirmation is unavailable" — it stays
    for the irreversible step, which is why the instruction differs rather than
    disappearing."""
    out = render_work_prompt(reference="ISS-1", done="review", confirm=False)
    assert "request_confirmation" in out
    assert "undo" in out.lower()


def test_work_prompt_tells_agent_not_to_do_git():
    from issuebot.plugins.sources.issuebear.prompts import render_work_prompt

    out = render_work_prompt(reference="ISS-1", done="review")
    lowered = out.lower()
    assert "do not" in lowered or "don't" in lowered
    assert "branch" in lowered and "push" in lowered


def test_render_build_prompt_mentions_the_task_and_its_confirm_setting():
    out = render_work_prompt(reference="ISS-1", done="review", confirm=False, mode="build")
    assert "ISS-1" in out
    assert "confirm before building: **no**" in out


def test_render_respond_prompt_is_read_only():
    out = render_work_prompt(reference="ISS-9", done="review", mode="respond")
    assert "ISS-9" in out
    low = out.lower()
    assert "read-only" in low
    assert "comment" in low


def test_render_defaults_to_build():
    out = render_work_prompt(reference="ISS-2", done="complete")
    assert "do not create branches" in out.lower()  # the build template's git line


def test_the_work_prompt_asks_for_an_announcement_without_a_restatement():
    """Both halves matter and pull against each other: silence leaves the thread
    with no trace that the task moved, and a summary shows the reader the same
    plan or questions twice in two shapes."""
    out = render_work_prompt(reference="ISS-1", done="review")
    lowered = out.lower()

    # Say something.
    assert "i've posted some questions for you" in lowered
    # But not the contents.
    assert "do not restate" in lowered
    assert "never repeat" in lowered or "nothing about *what it said*" in lowered


def test_render_build_prompt_instructs_the_asking_tool():
    """The build prompt must tell the agent to call the board's asking tool
    rather than guess or mark the task done when it needs human input — see
    runner._finish_task's `paused` outcome."""
    out = render_work_prompt(reference="ISS-1", done="review")
    assert "ask_questions" in out
    assert "guessing" in out.lower() or "guess" in out.lower()


def test_the_work_prompt_asks_for_a_handoff_of_what_is_left() -> None:
    """A run ends and its session is gone. Whatever the agent knew about the
    work it did not finish only survives if the board holds it, so the prompt
    has to ask for the remainder in the final comment and on the board."""
    out = render_work_prompt(reference="ISS-1", done="review")
    lowered = out.lower()

    assert "what is left" in lowered
    assert "checklist" in lowered
    assert "task_graph" in out


def test_render_mention_prompt_contains_all_template_fields() -> None:
    """render_mention_prompt fills in reference, actor_name, comment_excerpt, and agent_id."""
    out = render_mention_prompt(
        reference="ISS-10",
        actor_name="Alice",
        comment_excerpt="Can you fix the login bug?",
        agent_id="u-agent-42",
    )
    assert "ISS-10" in out
    assert "Alice" in out
    assert "Can you fix the login bug?" in out
    assert "u-agent-42" in out


def test_render_mention_prompt_handles_empty_agent_id() -> None:
    """render_mention_prompt does not crash when agent_id is empty."""
    out = render_mention_prompt(
        reference="ISS-11",
        actor_name="Bob",
        comment_excerpt="What is the status?",
        agent_id="",
    )
    assert "ISS-11" in out
    assert "Bob" in out
    # Must not include a stale placeholder literal.
    assert "{agent_id}" not in out


def test_render_mention_prompt_instructs_read_only_session() -> None:
    """The mention template must clearly state the agent must not edit code."""
    out = render_mention_prompt(
        reference="ISS-12",
        actor_name="Carol",
        comment_excerpt="help?",
        agent_id="u-1",
    )
    low = out.lower()
    assert "do not" in low or "must not" in low or "cannot" in low or "don't" in low


def test_render_mention_prompt_includes_get_task_instruction() -> None:
    """The agent should call get_task to read the full task context."""
    out = render_mention_prompt(
        reference="ISS-13",
        actor_name="Dan",
        comment_excerpt="question here",
        agent_id="u-1",
    )
    assert "get_task" in out


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
    out = render_work_prompt(reference="ISS-1", done="review")
    assert "ISSUEBOT_RESPONSE" in out


def test_the_mention_prompt_names_the_response_env_var():
    out = render_mention_prompt(
        reference="ISS-1", actor_name="Ada", comment_excerpt="hi", agent_id="u-1"
    )
    assert "ISSUEBOT_RESPONSE" in out


def test_a_run_permitted_only_an_answer_is_not_told_it_may_hand_off():
    """job.permits is the latitude, not a suggestion: a run that cannot hand off
    or edit code must not be told those kinds exist."""
    out = render_work_prompt(reference="ISS-1", done="review", permits=frozenset({"answer"}))
    assert '"kind": "answer"' in out
    assert '"kind": "handoff"' not in out
    assert '"kind": "changes"' not in out
    assert '"kind": "needs_input"' not in out


def test_the_default_permits_lists_all_four_kinds():
    out = render_work_prompt(reference="ISS-1", done="review")
    for kind in ("changes", "answer", "needs_input", "handoff"):
        assert f'"kind": "{kind}"' in out
