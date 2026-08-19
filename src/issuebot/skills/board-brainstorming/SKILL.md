---
name: board-brainstorming
description: Use when working an Issuebear board task that has ANY ambiguity in scope, requirements, or approach. Surfaces clarifying questions through the board's ask_questions tool (instead of guessing), which posts them as a form and hands the task back to its requester to fill in.
---

# Brainstorming a board task

You are working a board task through the board's own **MCP server**.
There is no interactive human in your session — you reach the requester only
through the board's tools. So brainstorming happens asynchronously, and every
round trip costs them an interruption. Make each one count.

Start by understanding the current project context.

**Before you change any files**, if the task is non-trivial and anything about the
goal, scope, constraints, or approach is ambiguous:

1. Decide the few questions whose answers would actually change what you build.
   Anything you can settle from the repo, the task, or its existing comments is
   not a question — settle it. Note the assumptions you are making instead.
2. Where you can name the realistic answers, name them: pass `options` for that
   question and the requester picks one instead of composing a reply. Competing
   approaches are exactly this — one question, one option per approach, with the
   trade-off in the wording.
3. Call `ask_questions("<ref>", questions)` ONCE with the whole set. It puts a
   form on the task and hands it back for you, so do NOT reassign with
   `update_task`.
4. Say so in one short comment — "I've posted some questions for you." — so the
   conversation records that the task moved. Do NOT repeat the questions there:
   they are on the task as a form, and a prose copy beside it means the reader
   sees the same questions twice in two shapes and has to reconcile them.
5. Stop. Do not implement on guesses.

```
ask_questions("ISS-42", [
  {"q": "Which store should this write to?", "options": ["Postgres", "Redis"]},
  {"q": "Anything about the migration I should know?"},
])
```

When the task comes back to you, the answers are in the comment history — read it
with `get_task`, treat them as decisions, and proceed to implementation.

Questions are not the tool for "shall I go ahead?" — that is
`request_confirmation`, and whether to use it is the task's **confirm** setting
(see board-implementing). Nor are they the place to put your plan: that is
`set_plan`.
