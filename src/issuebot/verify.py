"""Controller-side structural verification of an agent's response.

The design's "Verification, in two places": this is the first, forge-agnostic
and network-free half — it checks only what the response document itself can
say. The second half is substantive and forge-specific (the GitHub sink asking
the remote whether a branch really carries anything), and lives with that sink
in a later task.

A standalone function over ``(response, permits)`` rather than a method on
:class:`~issuebot.plugins.sources.base.Source`: every source needs the same
four checks, and none of them touch a network or a forge, so there is nothing
for a source implementation to override.
"""

from __future__ import annotations

from issuebot.contracts import Changed, Output, OutputKind, Response

# The field each output kind must carry, non-empty. Mirrors the four `Output`
# subclasses in contracts.py — kept here as data because `verify` checks it as
# a data invariant, not by importing and isinstance-checking every subclass.
_REQUIRED_FIELD: dict[str, str] = {
    "changes": "summary",
    "answer": "text",
    "needs_input": "question",
    "handoff": "assignee",
}


def _has_required_field(output: Output) -> bool:
    """True when ``output`` carries its kind's required field, non-empty.

    Guards against a bare ``Output(kind=...)`` — the base class is
    instantiable on its own, so this is not fully redundant with the
    pydantic validation the four real subclasses already enforce."""
    field = _REQUIRED_FIELD.get(output.kind)
    return field is not None and bool(getattr(output, field, None))


def verify(response: Response, permits: frozenset[OutputKind]) -> list[str]:
    """Every structural problem with what the agent said it produced, or
    ``[]`` if there are none.

    Four checks, independent of each other so every problem is reported at
    once rather than stopping at the first:

    1. every ``output.kind`` is in ``permits``
    2. each kind's required field is present and non-empty
    3. at most one decision (``needs_input`` and ``handoff`` are contradictory
       claims about where the task goes next; any number of deliverables is
       fine)
    4. for a ``changes`` output: ``head_sha != base_sha`` — an agent claiming
       it changed the repository cannot move that story past what git itself
       recorded
    """
    problems: list[str] = []

    for output in response.outputs:
        if output.kind not in permits:
            problems.append(f"'{output.kind}' output not permitted for this work")
        if not _has_required_field(output):
            problems.append(f"'{output.kind}' output is missing its required field")

    if len(response.decisions) > 1:
        kinds = ", ".join(d.kind for d in response.decisions)
        problems.append(f"at most one decision is allowed, got: {kinds}")

    if any(isinstance(o, Changed) for o in response.outputs) and (
        response.changes is None or response.changes.empty
    ):
        problems.append("'changes' output reported but the branch carries no actual change")

    return problems
