"""Turning an agent's own words into the one-line forms git and a forge want.

A run's agent already writes what it did (``Changed.summary``). Two consumers
want that text shortened to a subject line — the commit the workspace makes and
the pull request title the github sink writes — and both want the work item's
reference in front of it exactly once. Central because they must agree: a
commit reading ``ISS-42`` while its PR reads ``ISS-42: add the widget`` is the
same run described twice, badly.
"""

from __future__ import annotations

import re
import textwrap
from typing import TYPE_CHECKING

from issuebot.contracts import Changed

if TYPE_CHECKING:
    from collections.abc import Iterable

    from issuebot.contracts import Output


# A commit message is a summary. Anything longer belongs in the pull request
# body, which the sink writes separately from its own copy of the agent's
# report, so nothing is lost by refusing to put it in the commit object too.
MESSAGE_LIMIT = 4096

# Control characters have no business in a commit object: git stores the
# message as raw bytes and a forge parses them back, so a NUL or a stray escape
# can make the object it built unreadable to the receiving end. Newline and tab
# are the two a real message uses; the rest, DEL included, go.
_STRIPPED = {c: None for c in [*range(32), 127] if c not in (9, 10)}


def _clean(text: str) -> str:
    """``text`` with what a commit message cannot carry taken out.

    The agent writes its own summary and it goes straight into a git object
    that is then pushed to a forge, so this is a trust boundary: the runner
    must not be able to build a message the forge can refuse. Applied both to
    the agent's text and to the composed message, which is the same text plus
    our own reference.
    """
    # Every line ending becomes the one git reads.
    text = text.replace("\r\n", "\n").replace("\r", "\n").translate(_STRIPPED)

    if len(text) > MESSAGE_LIMIT:
        head = text[:MESSAGE_LIMIT]

        # End on a whole line where there is a line to end on; a single
        # enormous line has no boundary to cut back to, so it keeps the cut.
        text = head.rpartition("\n")[0] or head

    # Git reads exactly one blank line as the subject/body separator, so a run
    # of them would push the body out of every tool that honours that shape.
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def without_ref(ref: str, text: str) -> str:
    """``text`` with a leading copy of ``ref`` (and its separator) removed.

    Shared so that the callers which re-prefix the ref and the caller which
    asks "did the subject keep the whole line?" agree on where the ref ends.
    """
    text = text.strip()
    if text.lower().startswith(ref.lower()):
        # Drop the ref, then whatever separated it from the real title.
        text = text[len(ref) :].lstrip(":- \t")

    return text


def titled(ref: str, title: str) -> str:
    """``"{ref}: {title}"``, with a ref the writer already put in front removed.

    Both paths into a PR title route through here. The model is told not to
    prefix the ref and slips anyway, and the mechanical path's text is the
    agent's own summary, whose first line normally *does* start with the ref —
    so prefixing unconditionally gives the reviewer ``ISS-42: ISS-42: …``. One
    helper on both paths means that cannot happen on either.
    """
    text = without_ref(ref, title)

    # Cap what is left, not the raw line: capping first spends the budget on a
    # ref that is about to be stripped, so the title lost its tail for nothing
    # ("…closes any live agen"). `shorten` cuts back to a whole word.
    budget = 72 - len(ref) - len(": ")
    if budget > 0 and len(text) > budget:
        text = textwrap.shorten(text, width=budget, placeholder="")

    return f"{ref}: {text}"


def commit_message(ref: str, outputs: Iterable[Output]) -> str:
    """The message for the one commit a run makes, from the agent's own report.

    The subject is the first line of the agent's ``Changed`` summary, titled
    with the reference; the rest of that summary, when there is any, follows
    after a blank line as the body — the shape ``git log --oneline`` and every
    review tool expect.

    ``titled`` caps the subject at a title's width, which a long first line does
    not fit. A commit that drops the tail of what the agent said puts those
    words nowhere in the repository's history, so when the subject does not
    carry the whole first line the *whole* summary goes in the body under it.
    The subject stays short for ``git log --oneline``; nothing is lost.

    A run that reported no ``Changed`` output falls back to the bare reference.
    Nothing said what the commit did, so there is nothing better to write — and
    a summary that `_clean` empties out lands in the same place, for the same
    reason: the subject a commit must have cannot come from nothing.
    """
    raw = next((o.summary for o in outputs if isinstance(o, Changed)), "")

    summary = _clean(raw)
    if not summary:
        return ref

    first, _, rest = summary.partition("\n")
    message = titled(ref, first)

    # The subject carries the first line whole only when it still ends with it;
    # `titled` shortens by cutting the tail, so a cut subject cannot.
    body = rest if message.endswith(without_ref(ref, first)) else summary

    message = f"{message}\n\n{body.strip()}" if body.strip() else message

    # Again on the whole thing: the body path above re-joins text, and the cap
    # is a cap on the message, not on the agent's share of it.
    return _clean(message)
