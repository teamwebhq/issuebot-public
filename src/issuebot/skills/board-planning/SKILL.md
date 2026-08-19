---
name: board-planning
description: Use when work you discover on an Issuebear board task does not belong in the task you are working — a follow-up, a bug found in passing, a second deliverable. Decides whether it becomes a plan step, a checklist item, a subtask or a new task, and writes the task so a non-technical reader understands it.
---

# Putting work on the board

You are working one task, and you keep finding more work. Most of it is not a
new task. This skill is about telling the difference, and about writing the ones
that are so somebody who was not in your session can act on them.

The board is shared with people who do not read code. Every task you write is
read by them first and by an engineer second.

## Where work goes

Four places, and only one of them is a task. Pick by asking **who reviews this
and when**, not by how big it feels.

| Put it in | When |
|---|---|
| **the plan** — `set_plan` | It is how *you* will do the task you are on. Steps, order, files. Yours, revised in place. |
| **a checklist** — `create_checklist` | It is a visible step *inside* this deliverable. Same branch, same pull request, same reviewer. People want to watch it tick along. |
| **a subtask** — `create_task(..., parent="<ref>")` | It is a *separate deliverable* of the same overall goal: its own branch, its own pull request, its own review, and it could be handed to someone else. |
| **a new task** + `link_tasks` | It is not part of this deliverable at all. A bug you found in passing, a follow-up, a refactor the task does not need. |

Two failure modes, both common:

- **A plan step promoted to a subtask.** "Write the migration", "add the tests",
  "update the docs" are not deliverables. They ship together in one pull request
  and one review. They belong in the plan, or in a checklist if the requester
  wants to see them move.
- **Real work demoted to a comment.** A bug you noticed and mentioned in a
  comment is lost the moment the task closes. If it needs doing later, it needs a
  task now.

When you cannot decide between a checklist item and a subtask, ask whether it
would get its own pull request. No pull request, no subtask.

## Do not widen the task you were given

Work you discover outside the scope of the task is a **new task**, never extra
commits on this branch. Create it, link it, mention it in one line in your final
comment, and carry on with what you were asked to do.

The one exception is work the task cannot be correct without. Do that, and say
in your comment that you did it and why.

## Before you create anything, look

`search_tasks(organisation_id, "<keywords>")` searches names, descriptions and
comments across the organisation, understands quoted phrases and `-exclusions`,
and matches a bare ref like `ISS-42`. Run it with the words a person would have
used, not the words you would have used.

If a task already covers it, add a comment to that one instead. A near-duplicate
costs somebody a triage decision every time they look at the board.

## Writing the task

`name` is one line, under 200 characters, and names the **outcome**, not the
activity. "Password reset emails arrive within a minute", not "Look at email
queue".

`description` is markdown, and always has these three parts, in this order:

```markdown
## Why

What is wrong or missing today, and who it affects. Written for someone who
does not read code. No file names, no function names, no stack traces.

## Expected outcome

What is true once this is done, in terms a non-technical reader can check
themselves. Where you can, write it as things somebody could tick off.

## Technical detail

For whoever picks it up. Where in the code it lives, the approach you would
take, what you already ruled out and why, the constraints. Error messages,
log lines and refs go here.
```

Keep the first two sections free of jargon and the third section free of
repetition. Somebody who reads only **Why** should understand whether this
matters. Somebody who reads only **Expected outcome** should know when it is
finished.

Write down what you already know. You have just read the code — the assumption
you would have to rediscover in three weeks is exactly what belongs in
**Technical detail**.

## Creating it

```python
search_tasks(org_id, "password reset email")          # look first

create_task(
    board_id,                                          # UUID, not a board name
    "Password reset emails arrive within a minute",
    step_id="Backlog",                                 # column name is fine
    description="## Why\n…\n\n## Expected outcome\n…\n\n## Technical detail\n…",
    labels=["bug"],
)

link_tasks("<new ref>", "<ref>", kind="relates")       # or "blocks"
```

Things to know before you call it:

- `board_id` must be the board's **UUID**. Get it from `list_boards`; do not
  guess, and do not pass its name. `step_id` is the exception — a column name
  works there, and omitting it puts the task in the first column.
- `labels` must already exist on the board. Check with `list_labels`; an unknown
  name is rejected.
- `assignee_id` must be a board member's user id, from `list_board_members`.
  Leave it unset unless the task clearly belongs to one person.
- There is no priority or estimate to set from here. Do not put one in the
  description instead — that is the requester's call, not yours.

## Subtasks

Same call, plus `parent`:

```python
create_task(board_id, "<name>", parent="ISS-42", description="…")
```

Three rules the board enforces, so plan around them rather than discovering
them:

- **One level.** A subtask cannot have subtasks of its own.
- **Write-once.** A parent can never be changed afterwards. It can only be
  cleared, permanently, with `update_task(ref, detach_from_parent=True)`.
- **Same project** as the parent, though it may sit on a different board.

Because the link cannot be moved, do not reach for a subtask when you are
unsure. A separate task plus `link_tasks(..., kind="relates")` says nearly the
same thing and can be corrected later.

Give the parent a checklist or a plan, never a subtask per step. A parent whose
subtasks are "design", "build", "test" is a plan that escaped onto the board.
