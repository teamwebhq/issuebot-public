"""One suite every environment runs against.

Nothing here names a platform: no provider adapter, no platform setting, no
vendor syntax. A test that needs one is the leak ADR-0002 exists to catch.

Every environment is built the same way — one :class:`~issuebot.runner.Wiring`
of doubles, the constructor contract ``environment_for`` holds every plugin to
— plus a :class:`~issuebot.process.Process` double, so nothing here can reach
a real machine however an environment happens to be implemented.
"""

from __future__ import annotations

import inspect

import pytest

from conftest import (
    FakeApi,
    FakeSource,
    FakeWorkspace,
    NoSettings,
    RecordingReporter,
    connection,
    ctx,
    work,
)
from issuebot import plugins
from issuebot.contracts import Job, Response
from issuebot.plugins.base import EnvironmentPlugin
from issuebot.plugins.environments.base import ExecutionEnvironment
from issuebot.plugins.harnesses.fake.harness import FakeHarness
from issuebot.process import RecordingProcess
from issuebot.runner import Wiring

ENVIRONMENTS = plugins.names_of("environments")


class _ExplodingReporter(RecordingReporter):
    """A reporter whose very first call raises.

    Every environment narrates a run before it can finish one, so this breaks
    each of them at a point none of them can avoid — without naming anything
    platform-specific to break.
    """

    def start(self, ref: str, folder: str) -> None:
        raise RuntimeError("the reporter exploded")


def _build(name: str) -> ExecutionEnvironment:
    """One environment, built the way ``environment_for`` builds it — one
    wiring of doubles — plus a ``proc`` double.

    ``environment_for`` itself never passes ``proc`` — a real run wants the real
    adapter — so that is the one thing here that is not production wiring. It is
    what stops the suite reaching a live machine, and
    :func:`test_every_environment_can_have_its_process_substituted` is what makes
    that a contract rather than a hope: an environment that quietly ignored
    ``proc`` would shell out for real from these tests.
    """
    plugin = plugins.get("environments", name)
    assert isinstance(plugin, EnvironmentPlugin), f"'{name}' has no implementation"

    wiring = Wiring(
        api=FakeApi(),
        ctx=ctx(),
        connection=connection(),
        harness=FakeHarness(),
        source=FakeSource(),
        workspace=FakeWorkspace(),
        workspace_settings=NoSettings(),
        sinks=[],
    )
    return plugin.environment(wiring, proc=RecordingProcess())


def _job() -> Job:
    """A job every environment can be asked to run."""
    return Job(
        work=work(),
        prompt="do the thing",
        folder="/tmp/p",
        permits=frozenset({"answer"}),
        withheld_tools=(),
        timeout_minutes=None,
        mcp_servers=(),
        env={},
        resume_session_id=None,
        run_id="R1",
    )


@pytest.fixture(params=ENVIRONMENTS)
def environment(request: pytest.FixtureRequest) -> ExecutionEnvironment:
    """Every installed environment, built from the same neutral wiring."""
    return _build(request.param)


def test_every_environment_subclasses_the_abc(environment: ExecutionEnvironment) -> None:
    """An environment plugin's implementation must actually be one."""
    assert isinstance(environment, ExecutionEnvironment)


@pytest.mark.parametrize("name", ENVIRONMENTS)
def test_every_environment_names_itself(name: str) -> None:
    """An environment's `name` must match the plugin name it is registered
    under — the two are compared, not merely both members of the same set: a
    claim reports `name` to the source as the executor that ran the work, so a
    plugin registered as one thing and calling itself another misattributes
    every run it does."""
    assert _build(name).name == name


@pytest.mark.parametrize("name", ENVIRONMENTS)
def test_every_environment_can_have_its_process_substituted(name: str) -> None:
    """`proc` must be a declared parameter, not something absorbed silently.

    An environment reaches the outside world by running programs; if it cannot
    be handed a :class:`~issuebot.process.Process` double then nothing can test
    it without a live machine — and, more immediately, every test in this file
    that thinks it is safe would actually be creating sandboxes."""
    accepted = inspect.signature(plugins.get("environments", name).environment).parameters
    assert "proc" in accepted, f"'{name}' silently ignores the process it is handed"


@pytest.mark.parametrize("name", ENVIRONMENTS)
def test_an_environment_never_raises_out_of_run(name: str) -> None:
    """A raise would kill the listener thread with the work still claimed, so a
    crash is reported as a failed Response instead."""
    response = _build(name).run(_job(), reporter=_ExplodingReporter())

    assert isinstance(response, Response)
    assert response.status == "failed"


def test_every_environment_returns_a_response(environment: ExecutionEnvironment) -> None:
    """However a run ends, the caller gets one type back — it is what releases
    the claim, so "no answer" is not an option."""
    assert isinstance(environment.run(_job(), reporter=RecordingReporter()), Response)
