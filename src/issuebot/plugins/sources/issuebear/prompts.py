"""Render the launched-agent instruction prompt from the board's own document.

The document text is no longer this runner's to keep: an organisation writes
and edits it on the Parade board (`WorkItem.instructions`), and this module's
job shrinks to filling in the tags such a document may use. Parade validates
what an author writes against its own tag vocabulary, not against what any one
render call here happens to need, so every render fills the whole vocabulary
(see `_ALL_TAGS`) — `str.format` silently ignores whichever ones a particular
document doesn't reference.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import get_args

from issuebot.contracts import OutputKind, SkillRef
from issuebot.plugins.sources.issuebear.settings import DoneMode
from issuebot.plugins.workspaces.base import WorkspaceProblem
from issuebot.run import RESPONSE_ENV

# The complete tag vocabulary Parade validates an instruction document
# against. A document is authored by a person on the board, working from that
# validation — not from which of these any one render call below actually
# uses — so every tag gets a value (defaulting to "") on every render, and
# `str.format` is left to ignore whichever ones the document doesn't mention.
_ALL_TAGS = (
    "reference",
    "done",
    "confirm",
    "confirm_instruction",
    "identity",
    "response_instructions",
    "skills",
    "agent_instructions",
    "actor_name",
    "comment_excerpt",
    "self_assign_instruction",
)


class MissingDocument(RuntimeError):
    """The board sent no instruction document for this kind of work.

    The runner carries no copy of its own, so there is nothing to fall back to
    and nothing sensible to invent: an agent launched without its instructions
    would do something, and that something is worse than a failed run.
    """


def _render(document: str, **tags: str) -> str:
    """Fill `document` with Parade's whole eleven-tag vocabulary and return it.

    Raises :class:`MissingDocument` when `document` is blank — a board that
    sent no document for this kind of run, not a template this runner could
    guess at. Every caller passes only the tags it actually has a value for;
    the rest default to `""` here so a document that legitimately uses a tag
    this render path doesn't happen to fill (say, a `work_task` document that
    borrows `{actor_name}`) still renders instead of raising `KeyError`.
    """
    if not document:
        raise MissingDocument(
            "no instruction document for this run — the board sent none, and "
            "the runner keeps no copy of its own to fall back to"
        )
    filled = dict.fromkeys(_ALL_TAGS, "")
    filled.update(tags)
    return document.format(**filled)


# Every kind a run could possibly permit — the default for callers that
# haven't been taught about `permits` yet.
ALL_OUTPUT_KINDS: frozenset[OutputKind] = frozenset(get_args(OutputKind))

# One line per output kind, describing what it carries and when to use it.
_OUTPUT_KIND_LINES: dict[OutputKind, str] = {
    "changes": (
        '`{"kind": "changes", "summary": "..."}` — you changed the repository; summary says what.'
    ),
    "answer": (
        '`{"kind": "answer", "text": "..."}` — you produced an answer with no repository changes.'
    ),
    "needs_input": (
        '`{"kind": "needs_input", "question": "..."}` — you cannot proceed without '
        "a human answering."
    ),
    "handoff": (
        '`{"kind": "handoff", "assignee": "...", "note": "..."}` — you are handing the task '
        "to someone else; assignee is the name or id of a member of this board."
    ),
}

# Appended to every launch prompt. Only the kinds `permits` allows are listed —
# a run permitted only `answer` must not be told it may hand off, since the
# controller rejects a kind outside `permits` regardless of what was said here.
_RESPONSE_BLOCK = """\
When you are finished, write your response as JSON to the file path given in \
the `${env}` environment variable (create the file — it does not exist yet). \
The document is one object, `{{"outputs": [...]}}`, a list of zero or more of:

{kinds}

If you have nothing to report, write `{{"outputs": []}}`. This file is the only \
channel for your final answer, changes summary or hand-off decision — anything \
you only say elsewhere never reaches the controller."""


def render_response_instructions(permits: frozenset[OutputKind]) -> str:
    """The block telling the agent what to write, where, and which of the four
    output kinds this run permits. Order is fixed (``changes``, ``answer``,
    ``needs_input``, ``handoff``), filtered to ``permits`` regardless of the
    order callers pass it in."""
    kinds = "\n".join(
        f"- {_OUTPUT_KIND_LINES[kind]}" for kind in get_args(OutputKind) if kind in permits
    )
    return _RESPONSE_BLOCK.format(env=RESPONSE_ENV, kinds=kinds)


# The identity block, woven into every work prompt: who the agent is, and which
# person the work belongs to (the requester, or their owner when an agent asked).
# An agent that knows neither has to guess a name when it hands the task back,
# and a guessed name is not a user id.
_IDENTITY_SELF = "**You are {agent_name} on this board.** Your own board user id is `{agent_id}`."

_IDENTITY_REQUESTER = """\
**{requester_name} is the person behind this task.** Their board user id is `{requester_id}`.

- Handing the task back to them means `assignee_id="{requester_id}"` — that id, never a name.
- A follow-up task you create is requested by you, which is right: the board records that \
you raised it, and sends its questions and notifications to the person who owns you."""


def render_identity(
    *,
    agent_name: str = "",
    agent_id: str = "",
    requester_name: str = "",
    requester_id: str = "",
) -> str:
    """The block telling the agent who it is and who asked for this task.

    Each half is rendered only when its id is known, and an unknown display
    name falls back to the id — the id is what the board actually wants in an
    `assignee_id`/`requester_id` field, so a half with an id is still worth
    saying. Everything unknown gives an empty block, which is how a launch
    survives a board that would not answer either lookup.
    """
    lines: list[str] = []

    if agent_id:
        lines.append(_IDENTITY_SELF.format(agent_name=agent_name or agent_id, agent_id=agent_id))

    if requester_id:
        lines.append(
            _IDENTITY_REQUESTER.format(
                requester_name=requester_name or requester_id, requester_id=requester_id
            )
        )

    return "\n\n".join(lines)


# Injected when the agent id is known: tells the agent exactly how to self-assign.
_SELF_ASSIGN_WITH_ID = (
    'call `update_task("{reference}", assignee_id="{agent_id}")` to assign the task '
    "to yourself, then stop. The runner will detect the assignment and start a proper "
    "work session with the full toolset."
)

# Fallback when the runner could not resolve the agent user id at startup.
_SELF_ASSIGN_NO_ID = (
    "Note: the runner could not resolve your agent user id at startup, so "
    "self-assignment via `update_task` is not available in this session. "
    "Post a reply comment asking to be manually assigned instead."
)


def render_skills_line(skills: Sequence[SkillRef]) -> str:
    """The sentence naming this run's skills, or "" when there are none.

    Named rather than described: a headless `-p` run does not go looking
    through skill descriptions for something that might apply, which is the
    whole reason the prompt says which ones to use.
    """
    if not skills:
        return ""
    names = [f"**{s.slug}**" for s in skills]
    joined = names[0] if len(names) == 1 else ", ".join(names[:-1]) + f" and {names[-1]}"
    return f"Use your {joined} skills to do this well."


# What to do with `request_confirmation`, per the connection's `confirm` setting.
# Both are instructions, not permissions: an agent left to decide for itself when
# approval is "worth it" will decide differently every run.
_CONFIRM_INSTRUCTIONS = {
    True: (
        "Once you have set the plan, call this and wait — do not write any code "
        "until it comes back approved. Fold anything else you want agreed into "
        "the same summary."
    ),
    False: (
        "This connection does not want sign-off, so do not ask for it routinely: "
        "set the plan and get on with it. Keep this for a step you cannot undo — "
        "something destructive, or well outside what the task asked for."
    ),
}


def render_work_prompt(
    *,
    document: str,
    reference: str,
    done: DoneMode,
    confirm: bool = True,
    permits: frozenset[OutputKind] = ALL_OUTPUT_KINDS,
    agent_name: str = "",
    agent_id: str = "",
    requester_name: str = "",
    requester_id: str = "",
    skills: Sequence[SkillRef] = (),
    agent_instructions: str | None = None,
) -> str:
    """Render `document` — the board's `work_task` or `respond_task` document
    (`source.py` picks which by the connection's `mode`) — for one launch.

    Raises :class:`MissingDocument` when `document` is blank. There used to be
    a build/respond branch here to avoid handing an unused `{confirm}` to the
    read-only document's `.format()` call; it is gone because `str.format`
    already ignores extra keyword arguments, and now every document — whoever
    wrote it, however it uses the vocabulary — is filled the same way.

    The four identity arguments name the agent and the task's requester
    (:func:`render_identity`). They default to empty because only the source
    can read them off the board, and neither lookup is worth failing a launch
    over — a prompt with no identity block is a working prompt.
    """
    identity = render_identity(
        agent_name=agent_name,
        agent_id=agent_id,
        requester_name=requester_name,
        requester_id=requester_id,
    )
    # The document holds the slot on a line of its own, so the block carries the
    # blank lines that set it apart — and an empty block leaves none behind.
    if identity:
        identity = f"\n{identity}\n"
    return _render(
        document,
        reference=reference,
        done=done,
        confirm="yes" if confirm else "no",
        confirm_instruction=_CONFIRM_INSTRUCTIONS[bool(confirm)],
        identity=identity,
        response_instructions=render_response_instructions(permits),
        skills=render_skills_line(skills),
        agent_instructions=agent_instructions or "",
    )


# Prepended to a work prompt when the task's branch diverged from origin and the
# agent must reconcile it before starting. {reconcile_step} is the rebase or the
# merge wording; {base_line}/{base_step} are filled only for a base divergence
# ("diverged-base") and are empty strings otherwise.
_RECONCILE_PREAMBLE = """\
⚠️ Before you start this task, reconcile its branch with the remote.

The branch `{branch}` has diverged from origin and could not be auto-synced \
({detail}).{base_line}

Reconcile it **locally** first:
- Run `git fetch origin`.
- {reconcile_step}{base_step}
- Preserve commits that exist only on the remote — never drop others' work.
- Do NOT push. Leave the branch reconciled locally; the runner does the final push.

If you cannot reconcile the branch safely, do NOT guess: post a comment on the \
task explaining exactly what conflicts and why, then stop.

Once the branch is reconciled, carry on with the task below.

---

"""


def render_reconcile_preamble(problem: WorkspaceProblem) -> str:
    """Render the instruction block prepended to a work prompt when the task's
    branch diverged from origin and the agent must reconcile it before working.

    Called by :meth:`Issuebear.prompt` when ``run.execute`` hands the prompt
    render a :class:`~issuebot.plugins.workspaces.base.WorkspaceProblem` — the
    git workspace's ``prepare`` reporting a divergence as data.

    ``problem.kind`` is "diverged-branch" (origin gained commits the local
    branch lacks — the agent's rebase onto ``origin/<branch>`` makes the
    runner's plain final push a fast-forward) or "diverged-base" (updating from
    the base branch conflicted); for the latter the ``base`` branch name is
    woven in so the agent reconciles onto it too.

    ``problem.reconcile`` picks the wording: "rebase" or "merge". A connection
    whose ``update_base`` is "merge" asked for history never to be rewritten,
    so telling it to rebase would undo the setting from inside the prompt.

    ponytail: for a "diverged-base" rebase of a branch origin already has, the
    rewritten commits make the runner's plain push reject — the workspace
    retries that one case with ``--force-with-lease`` (ADR-0013), so the
    reconciled branch still reaches origin.
    """
    merging = problem.reconcile == "merge"

    # The one bullet that names the operation, plus the base sentences that a
    # base divergence adds. Same shape either way; only the verb changes.
    if merging:
        reconcile_step = (
            f"Merge `origin/{problem.branch}` into your local branch, resolving any conflicts."
        )
    else:
        reconcile_step = (
            f"Rebase your local branch onto `origin/{problem.branch}`, resolving any conflicts."
        )

    if problem.kind == "diverged-base" and problem.base:
        base = problem.base
        if merging:
            base_line = f" The base branch `{base}` moved and merging it conflicted."
            base_step = f"\n- Also merge `origin/{base}` so the branch includes the latest base."
        else:
            base_line = f" The base branch `{base}` moved and rebasing onto it conflicted."
            base_step = (
                f"\n- Also rebase onto `origin/{base}` so the branch sits on the latest base."
            )
    else:
        base_line = ""
        base_step = ""

    return _RECONCILE_PREAMBLE.format(
        branch=problem.branch,
        detail=problem.detail or "no detail given",
        reconcile_step=reconcile_step,
        base_line=base_line,
        base_step=base_step,
    )


def render_mention_prompt(
    *,
    document: str,
    reference: str,
    actor_name: str,
    comment_excerpt: str,
    agent_id: str,
    permits: frozenset[OutputKind] = ALL_OUTPUT_KINDS,
) -> str:
    """Render `document` — the board's `respond_mention` document — for one
    mention session.

    Raises :class:`MissingDocument` when `document` is blank. The prompt tells
    the agent to read the task, then either reply (question/discussion) or
    self-assign (asked to do work). When ``agent_id`` is non-empty the exact
    ``update_task`` call is embedded; when it is empty (runner could not call
    GET /me) the self-assign block is replaced with a note to reply instead.
    ``permits`` defaults to every kind — a mention-shaped ``Job`` (no
    ``changes``) should pass its own narrower set once a caller has one to give.
    """
    if agent_id:
        # Build the concrete self-assign instruction with the agent's own user id.
        self_assign_instruction = _SELF_ASSIGN_WITH_ID.format(
            reference=reference, agent_id=agent_id
        )
    else:
        # Degraded mode: runner couldn't resolve the id, so the agent can only reply.
        self_assign_instruction = _SELF_ASSIGN_NO_ID

    return _render(
        document,
        reference=reference,
        actor_name=actor_name,
        comment_excerpt=comment_excerpt,
        self_assign_instruction=self_assign_instruction,
        response_instructions=render_response_instructions(permits),
    )
