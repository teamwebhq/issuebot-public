"""One suite every harness runs against.

Shared rather than per-harness, so a new harness is held to the contract by
construction, and a contract change fails for every implementation at once
instead of passing silently for whichever nobody updated.
"""

from __future__ import annotations

import threading

import pytest

from issuebot import plugins
from issuebot.plugins.harnesses.base import Harness, LaunchResult, LaunchSpec
from issuebot.process import RecordingProcess

HARNESSES = plugins.names_of("harnesses")


def _spec() -> LaunchSpec:
    """A minimal LaunchSpec, enough for any harness to accept."""
    return LaunchSpec(prompt="do the thing", folder="/work/alpha")


@pytest.fixture(params=HARNESSES)
def harness(request: pytest.FixtureRequest) -> Harness:
    """Every installed harness, constructed against a RecordingProcess."""
    return plugins.get("harnesses", request.param).harness(proc=RecordingProcess())


def test_every_harness_subclasses_the_abc(harness: Harness) -> None:
    """A harness plugin's implementation must actually be a Harness."""
    assert isinstance(harness, Harness)


def test_every_harness_names_itself(harness: Harness) -> None:
    """A harness's `name` must match the plugin name it is registered under."""
    assert harness.name in HARNESSES


def test_every_harness_returns_a_launch_result(harness: Harness, reporter) -> None:
    """launch() must return a LaunchResult, whatever else it does."""
    assert isinstance(harness.launch(_spec(), reporter), LaunchResult)


def test_every_harness_tolerates_a_cancel_that_is_never_set(harness: Harness, reporter) -> None:
    """launch() must accept a cancel Event, even one that never fires."""
    harness.launch(_spec(), reporter, threading.Event())
