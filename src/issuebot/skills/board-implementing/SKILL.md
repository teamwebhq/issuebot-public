---
name: board-implementing
description: Use when implementing an Issuebear board task whose requirements are clear. Works test-first following the repo's conventions and keeps the board updated, recording its plan with set_plan and honouring the task's done-mode and confirm setting.
---

# Implementing a board task

You are implementing a task in a real local repository, talking to the board only
through the board's own **MCP server** (`set_plan`, `add_comment`,
`update_task`). Keep the board live.

1. Read the repo's `CLAUDE.md` / `AGENTS.md` and follow its conventions.
2. Post a short kickoff comment ("on it — <one line of intent>").
3. Record your plan with `set_plan("<ref>", plan)` — always, even when the task
   is obvious. The task holds one canonical plan, so call it again whenever the
   plan changes rather than posting a revision as a comment. Setting a plan asks
   for nothing and pauses nothing, so follow it with one short comment saying it
   landed and what you are doing next — "I've updated the plan based on our
   discussion. I've started working on it." Never paste or summarise the plan
   itself into that comment: it is on the task already, and a prose copy beside
   it just makes the reader work out which version is current.
4. Honour the task's **confirm** setting:
   - **confirm: yes** — after setting the plan, call
     `request_confirmation("<ref>", summary)` and wait, then say so in one line
     ("I've updated the plan, please review and let me know if I should
     continue."). Write no code until it comes back approved. If it comes back
     rejected, the note says what to change: revise with `set_plan` and ask
     again.
   - **confirm: no** — get on with it. Keep `request_confirmation` for something
     you cannot undo, or well outside what the task asked for.
5. Work **test-first**: write the failing test, make it pass, keep changes small.
   Run the repo's tests.
6. Post a concise final comment summarising what you did. Do NOT run git
   yourself — no branches, commits, pushes, or PRs; the runner handles version
   control.
7. Honour the **done-mode**:
   - `review` — reassign the task to its requester for review; do NOT mark it complete.
   - `complete` — mark it complete with `update_task("<ref>", completed=true)`.

If you get genuinely blocked mid-implementation, ask via `ask_questions` rather
than guessing — it posts your questions as a form and hands the task back in one
step. Keep the three apart: `set_plan` says what you intend, `ask_questions`
asks what you cannot work out, `request_confirmation` asks whether to proceed.

# Execution model

Break the task into a plan of small, testable subtasks. Each subtask should be a single, clear action that can be implemented and tested in isolation.

Execute subtasks by dispatching a fresh implementer subagent per subtask, a subtask review (spec compliance + code quality) after each, and a broad whole-branch review at the end.

Why subagents: You delegate tasks to specialized agents with isolated context. By precisely crafting their instructions and context, you ensure they stay focused and succeed at their task. They should never inherit your session's context or history — you construct exactly what they need. This also preserves your own context for coordination work.

Core principle: Fresh subagent per subtask + subtask review (spec + quality) + broad final review = high quality, fast iteration

Continuous execution: Do not pause to check in with your human partner between subtasks. Execute all subtasks from the plan without stopping. The only reasons to stop are: BLOCKED status you cannot resolve, ambiguity that genuinely prevents progress, or all subtasks complete. "Should I continue?" prompts and progress summaries waste their time — they asked you to execute the plan, so execute it!

## Model Selection

Use the least powerful model that can handle each role to conserve cost and increase speed.

**Mechanical implementation tasks** (isolated functions, clear specs, 1-2 files): use a fast, cheap model. Most implementation tasks are mechanical when the plan is well-specified.

**Integration and judgment tasks** (multi-file coordination, pattern matching, debugging): use a standard model.

**Architecture and design tasks**: use the most capable available model.
The final whole-branch review is one of these — dispatch it on the most
capable available model, not the session default.

**Review tasks**: choose the model with the same judgment, scaled to the
diff's size, complexity, and risk. A small mechanical diff does not need the
most capable model; a subtle concurrency change does.

**Always specify the model explicitly when dispatching a subagent.** An
omitted model inherits your session's model — often the most capable and
most expensive — which silently defeats this section.

**Turn count beats token price.** Wall-clock and context cost scale with how
many turns a subagent takes, and the cheapest models routinely take 2-3× the
turns on multi-step work — costing more overall. Use a mid-tier model as the
floor for reviewers and for implementers working from prose descriptions.
When the task's plan text contains the complete code to write, the
implementation is transcription plus testing: use the cheapest tier for
that implementer. Single-file mechanical fixes also take the cheapest tier.

**Task complexity signals (implementation tasks):**
- Touches 1-2 files with a complete spec → cheap model
- Touches multiple files with integration concerns → standard model
- Requires design judgment or broad codebase understanding → most capable model
