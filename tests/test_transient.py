"""Tests for transient-failure classification — generic over HTTP, not tied to
any one client's exception type. Moved here out of a source plugin's own client
tests alongside the functions themselves (Task 10's review round): nothing here
belongs to any one source, so `_StatusError` stands in for any client's own
"HTTP error with a status" exception rather than importing one from a plugin.
"""

from __future__ import annotations

import logging

import httpx
import pytest

from issuebot.transient import (
    TRANSIENT_ESCALATE_AFTER,
    describe_transient,
    is_transient,
    log_poll_failure,
    log_poll_recovered,
)


class _StatusError(Exception):
    """A minimal stand-in for any HTTP client's "error with a status code" —
    proves the classification is duck-typed on `.status`, not `isinstance`
    against one client's exception class."""

    def __init__(self, status: int, detail: str = "") -> None:
        self.status = status
        super().__init__(detail)


@pytest.mark.parametrize(
    "exc",
    [
        httpx.ConnectError("boom"),
        httpx.ConnectTimeout("boom"),
        httpx.ReadTimeout("boom"),
        httpx.RemoteProtocolError("boom"),
        _StatusError(502, "bad gateway"),
        _StatusError(503, "unavailable"),
        _StatusError(504, "gateway timeout"),
    ],
)
def test_is_transient_true(exc):
    assert is_transient(exc) is True


@pytest.mark.parametrize(
    "exc",
    [
        _StatusError(500, "server error"),
        _StatusError(404, "not found"),
        _StatusError(409, "conflict"),
        Exception(),
    ],
)
def test_is_transient_false(exc):
    assert is_transient(exc) is False


def test_describe_transient():
    assert describe_transient(_StatusError(502, "bad gateway")) == "502"
    assert describe_transient(httpx.ConnectError("x")) == "ConnectError"
    assert describe_transient(httpx.ReadTimeout("x")) == "ReadTimeout"


def test_log_poll_failure_transient_under_threshold(caplog):
    logger = logging.getLogger("issuebot.test")
    with caplog.at_level(logging.INFO, logger="issuebot.test"):
        result = log_poll_failure(logger, "Board API", httpx.ConnectError("x"), 0)

    assert result == 1
    assert len(caplog.records) == 1
    record = caplog.records[0]
    assert record.levelno == logging.INFO
    assert record.exc_info is None


def test_log_poll_failure_transient_at_threshold_escalates(caplog):
    logger = logging.getLogger("issuebot.test")
    with caplog.at_level(logging.INFO, logger="issuebot.test"):
        result = log_poll_failure(
            logger, "Board API", httpx.ConnectError("x"), TRANSIENT_ESCALATE_AFTER
        )

    assert result == 0
    assert len(caplog.records) == 1
    record = caplog.records[0]
    assert record.levelno == logging.WARNING
    assert record.exc_info is not None


def test_log_poll_failure_non_transient(caplog):
    logger = logging.getLogger("issuebot.test")
    with caplog.at_level(logging.INFO, logger="issuebot.test"):
        result = log_poll_failure(logger, "Board API", _StatusError(500, "boom"), 0)

    assert result == 0
    assert len(caplog.records) == 1
    record = caplog.records[0]
    assert record.levelno == logging.WARNING
    assert record.exc_info is not None


def test_log_poll_recovered_after_failures(caplog):
    logger = logging.getLogger("issuebot.test")
    with caplog.at_level(logging.INFO, logger="issuebot.test"):
        result = log_poll_recovered(logger, "Board API", 4)

    assert result == 0
    assert len(caplog.records) == 1
    assert caplog.records[0].levelno == logging.INFO
    assert "back after 4" in caplog.records[0].getMessage()


def test_log_poll_recovered_says_nothing_when_healthy(caplog):
    logger = logging.getLogger("issuebot.test")
    with caplog.at_level(logging.INFO, logger="issuebot.test"):
        result = log_poll_recovered(logger, "Board API", 0)

    assert result == 0
    assert caplog.records == []
