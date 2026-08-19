---
name: writing-pull-requests
description: Use when writing the title and description of a pull request for a finished change. Writes for the reviewer — what changed and why, what to look at first, what was verified, and what to check by hand.
---

# Writing a pull request

One reader: the person who has to review this. They did not do the work, they
are reading it between two other things, and they will decide how carefully to
look based on what you write. Make that decision easy for them, and make it
honest.

Use ASD-STE100 Simplified Technical English throughout. Avoid being overly wordy,
technical. Assume a tired engineer who just needs to get through this.

Only ever describe the branch as it is now. Not what it was in the past. Not what
it was throughout the development. The final state is the only thing a reader is
interested in.

## Title

One line, imperative, under 72 characters. It says what the change does, not
what the task asked for.

**Do not put the task reference in it.** The runner prefixes it already, so
writing it yourself gives the reviewer `ISS-42: ISS-42: …`.

- Good: `Retry a task run once when the API returns 529`
- Bad: `ISS-42: fixes`, `Various improvements`, `Update harness.py`

## Description

Order it by what the reviewer needs, not by what the diff contains:

```markdown
Two or three sentences: what this changes and why it was needed. The "why"
first — the reviewer can read the diff for the "what", but not for the reason.

## Changes

- One bullet per meaningful change, grouped by area of behaviour.
- Anything subtle, and why it is the way it is.

## Testing

What was run, and what it proves. Name new tests and what they cover.

## For the reviewer

Anything to check by hand, any judgement call worth a second opinion, and
anything deliberately left out or deferred.
```

Drop a section that has nothing real in it. An empty **For the reviewer** is
better absent than filled with "nothing to note". Keep the opening paragraph
always.

## What makes it useful

- **Lead with the risk.** The change most likely to be wrong goes first, not
  the largest one and not the one you did first.
- **Describe behaviour, not files.** "Two tasks on the same repository no longer
  overwrite each other's branch" tells the reviewer something. "Modified
  `workspace.py`" tells them what the diff already shows.
- **Do not narrate the diff.** If a bullet only repeats a file name and a verb,
  cut it. Group small mechanical edits into one line.
- **Say what you did not do.** Work left for later, a case knowingly unhandled,
  a shortcut with a known ceiling — the reviewer finding it themselves costs
  far more than you writing one line about it.
- **No filler.** No "as requested", no "this PR", no restating the task title,
  no summary of the summary at the end.

## Honesty

You are describing work for someone who will trust the description over the
diff, so it has to be exact.

- Claim only what was actually run. If the tests were not run, say the change
  is untested rather than implying it passed.
- If the diff you were given is truncated, describe what you can see and say
  plainly that the rest was not visible. Never fill the gap by guessing.
- If the change does something the task did not ask for, that is the first
  thing the reviewer needs to know, not a footnote.
