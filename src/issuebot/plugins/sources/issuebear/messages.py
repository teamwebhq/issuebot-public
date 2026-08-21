"""What issuebot says on the board — the runner's own voice, told apart from
the agent's by one shared prefix.

There are no per-stage sentences here: ``run.execute`` reports one
:class:`~issuebot.contracts.Response`, so what there is to say is one board
message summarising what a run produced and what its sinks did with it, built
from that ``Response`` alone. There is no reconcile-branch wording either —
reconciliation rides the launch prompt (``prompts.render_reconcile_preamble``,
woven in by ``Issuebear.prompt`` when prepare reports a divergence), not a
board message.
"""

from __future__ import annotations

from issuebot.contracts import Response, SinkResult

# How every message issuebot posts identifies itself.
PREFIX = "issuebot:"


def say(text: str) -> str:
    """One board message in the runner's own voice."""
    return f"{PREFIX} {text}"


def summarize(response: Response, results: list[SinkResult]) -> str | None:
    """What the runner has to add about a finished run — or None when it has
    nothing to add, which is the common case.

    **Only what the agent could not have said itself.** Every launch template
    tells the agent that task comments are its one channel to people, and to
    post its answer, its questions and a summary of what it did as comments —
    so it does. Repeating its `answer` text or its `changes` summary here put a
    second copy of the agent's own words on the thread underneath the first,
    and a `needs_input` question arrived three times over: the agent's comment,
    the board's own Ask (`ask_questions`), and ours.

    What is genuinely ours to report is what happened *after* the agent exited:
    what each sink did (a PR URL the agent cannot know, a sink that refused),
    and a run that ended badly, since a failed run may have produced no comment
    at all. With neither, the agent has already told the whole story and this
    says nothing rather than saying it again.

    Returns the bare text: the caller adds the runner's own voice
    (:func:`say`), so it is not prefixed twice.
    """
    parts: list[str] = []

    for result in results:
        line = result.summary if result.ok else f"{result.sink} failed: {result.summary}"
        if result.url:
            line = f"{line} — {result.url}"
        parts.append(line)

    # A run that did not finish cleanly: the agent may never have got as far as
    # saying anything, so this is the one status worth stating outright.
    if response.status != "done":
        parts.append(response.result_text or f"run {response.status}")

    return " · ".join(parts) if parts else None


def unresolved_assignee(assignee: str, names: list[str]) -> str:
    """What the runner says when a hand-off names somebody the board does not have.

    The task keeps its current assignee, so this has to give a person enough to
    finish the hand-off by hand: the value the agent wrote, and who is actually
    on the board.

    Returns the bare text: the caller adds the runner's own voice (:func:`say`).
    """
    roster = ", ".join(names) if names else "nobody"
    return (
        f'could not hand this task off: "{assignee}" is not a member of this board, '
        f"so the assignee is unchanged. Members are: {roster}."
    )


def self_handoff(requester: str | None) -> str:
    """What the runner says when a hand-off names the agent itself.

    An agent cannot hand work to itself: the only session that would pick the
    task up is the one that just ended, so the task would sit assigned and
    still. ``requester`` is who the task went back to instead — the person who
    asked for the work — or ``None`` when the board records nobody to send it
    to and the assignee is unchanged.

    Returns the bare text: the caller adds the runner's own voice (:func:`say`).
    """
    if requester is not None:
        return (
            f"this hand-off named me, and I cannot hand work to myself, "
            f"so the task goes back to {requester}, who asked for it."
        )

    return (
        "this hand-off named me, and I cannot hand work to myself. This task records "
        "nobody who asked for it, so the assignee is unchanged — please assign it to "
        "the person who should pick it up."
    )
