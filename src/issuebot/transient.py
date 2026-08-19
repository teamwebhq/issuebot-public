"""Transient-failure classification for any poll/retry loop.

Generic over HTTP, not over any one client: a connectivity error (httpx's own
exceptions — every source ultimately sits on top of some HTTP client) or a
502/503/504-shaped status is worth retrying quietly; anything else is a real
error worth a traceback. The status check is duck-typed on a bare ``.status``
attribute rather than an ``isinstance`` check against one client's own error
type, so this module needs no import from any plugin.

Used by every poll loop (board work, board commands) and by ``run.execute``'s
heartbeat retry — none of which are issuebear-specific, so this lives in core
rather than behind the issuebear plugin boundary the board's own
``ApiError``/``IssuebotClient`` live behind: core importing retry logic from a
plugin would mean deleting that plugin takes core's retry logic with it.
"""

from __future__ import annotations

import logging

import httpx

_TRANSIENT_HTTPX = (
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.ReadTimeout,
    httpx.RemoteProtocolError,
)
_TRANSIENT_STATUSES = frozenset({502, 503, 504})


def is_transient(exc: BaseException) -> bool:
    """True if ``exc`` is a transient connectivity/gateway failure (server
    restart, proxy hiccup) rather than a genuine error worth a traceback."""
    if isinstance(exc, _TRANSIENT_HTTPX):
        return True
    return getattr(exc, "status", None) in _TRANSIENT_STATUSES


def describe_transient(exc: BaseException) -> str:
    """Short label for a transient failure, e.g. ``'502'`` or ``'ConnectError'``."""
    status = getattr(exc, "status", None)
    return str(status) if status is not None else type(exc).__name__


TRANSIENT_ESCALATE_AFTER = 20
"""Consecutive transient failures (~1 min at a 3 s backoff) before we escalate
to a loud WARNING so a genuinely prolonged outage stays visible."""


def log_poll_failure(
    logger: logging.Logger, label: str, exc: BaseException, consecutive: int
) -> int:
    """Log a poll-loop failure calmly for transient outages, loudly otherwise.

    Returns the updated consecutive-transient count. A transient failure under
    the escalation threshold gets one calm INFO line with no traceback; a
    genuine error — or a transient outage that has persisted past the
    threshold — gets the loud WARNING + traceback (and resets the count, so a
    prolonged outage re-surfaces a loud line roughly once per threshold window).
    """
    if is_transient(exc) and consecutive < TRANSIENT_ESCALATE_AFTER:
        logger.info("%s unavailable (%s); reconnecting…", label, describe_transient(exc))
        return consecutive + 1
    logger.warning("%s poll failed; backing off", label, exc_info=True)
    return 0
