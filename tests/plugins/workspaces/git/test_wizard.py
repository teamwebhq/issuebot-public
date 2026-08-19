"""The git workspace's own wizard hook.

Its questions used to sit in the generic ``issuebot.wizard``, which spelled
this plugin's whole vocabulary as core's own. These drive the hook directly,
with the numbered picker handed in exactly as the wizard hands it in.
"""

from __future__ import annotations

from typing import Any

import pytest

from issuebot.plugins.workspaces.git import wizard as git_wizard
from issuebot.wizard import _choose_literal


def _run(
    monkeypatch: pytest.MonkeyPatch,
    answers: list[str],
    *,
    sandboxed: bool = False,
    changes: bool = True,
) -> dict[str, Any]:
    """Drive the hook with scripted menu answers.

    ``prompt_repo``/``prompt_folder`` are canned, as the generic wizard cans
    them when the source (or the user, earlier) already answered.
    """
    replies = iter(answers)

    def fake_prompt(*a: Any, **kw: Any) -> str:
        # An empty scripted answer means what an empty stdin line means to
        # typer.prompt: take the default.
        reply = next(replies, "")
        return reply or str(kw.get("default", ""))

    monkeypatch.setattr("typer.prompt", fake_prompt)
    return git_wizard.wizard(
        choose_literal=_choose_literal,
        prompt_repo=lambda: "https://example.com/r.git",
        prompt_folder=lambda settings: "/srv/work",
        sandboxed=sandboxed,
        changes=changes,
    )


def test_in_place_work_returns_no_strategy_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Working directly is `git_init`'s *absence* — a present-but-None key
    would select this plugin for a connection with no git strategy at all.
    Update-base is not asked: there is no branch to update, and `validate`
    would refuse the answer at the very end of the wizard."""
    # working copy=<enter> (folder), isolation=<enter> (none).
    params = _run(monkeypatch, ["", ""])

    assert params == {"folder": "/srv/work"}


def test_a_strategy_brings_the_update_base_question(monkeypatch: pytest.MonkeyPatch) -> None:
    # working copy=1 (folder), isolation=2 (branch), update base=2 (rebase).
    params = _run(monkeypatch, ["1", "2", "2"])

    assert params == {"folder": "/srv/work", "git_init": "branch", "update_base": "rebase"}


def test_a_clone_can_be_worked_in_directly(monkeypatch: pytest.MonkeyPatch) -> None:
    """The pairing the old four-valued question could not express: a fresh
    clone per task that cuts no branch."""
    # working copy=2 (clone), isolation=1 (none).
    params = _run(monkeypatch, ["2", "1"])

    assert params == {"repo": "https://example.com/r.git"}


def test_a_sandboxed_connection_always_clones_onto_a_task_branch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`sandboxed` is the wizard's neutral fact; the consequences are this
    plugin's to draw: no folder on this machine means a fresh clone, and the
    work is cut onto a task branch — a clone that committed to the checked-out
    default branch would push straight to it. Neither is asked."""
    # Only update base is a question: <enter> (none).
    params = _run(monkeypatch, [""], sandboxed=True)

    assert params == {
        "repo": "https://example.com/r.git",
        "git_init": "branch",
        "update_base": "none",
    }


def test_a_sandboxed_changeless_connection_asks_only_for_the_repo(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sandboxed + `changes=False`: no branch can ever report changes, so no
    `git_init` is forced and the update-base question is not asked — only the
    repo, which a sandboxed connection always needs."""

    def no_questions(*a: Any, **kw: Any) -> str:
        raise AssertionError("no menu question may be asked")

    monkeypatch.setattr("typer.prompt", no_questions)

    params = git_wizard.wizard(
        choose_literal=_choose_literal,
        prompt_repo=lambda: "https://example.com/r.git",
        prompt_folder=lambda settings: "/srv/work",
        sandboxed=True,
        changes=False,
    )

    assert params == {"repo": "https://example.com/r.git"}


def test_a_changeless_connection_is_not_offered_a_branch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`changes=False` (the source said runs may not report changes): no
    strategy or update-base question, but where the copy comes from is still
    the user's choice."""
    # working copy=2 (clone) — and nothing else may be consumed.
    params = _run(monkeypatch, ["2"], changes=False)

    assert params == {"repo": "https://example.com/r.git"}


def test_every_menu_answer_is_shown_with_its_line_of_help(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """ "worktree" and "clone" are both git, and the words do not say which is
    which — so each answer is shown with what it actually does, and every one
    of them is (a menu that explains three of four is the same problem)."""
    # working copy=<enter>, isolation=2 (branch), update base=<enter> — so all
    # three menus render.
    _run(monkeypatch, ["", "2", ""])

    out = capsys.readouterr().out
    for help_lines in git_wizard._HELP.values():
        for value, line in help_lines.items():
            assert line in out, f"no help shown for {value!r}"
