"""Shared test doubles and builders.

One double per interface, shared. Before this file existed there was no
``conftest.py`` at all, so every double was file-private and got rewritten: the
board API was implemented from scratch in three files and doubled nineteen ways
across eight, ``NullReporter`` was re-implemented twice despite shipping in
production, and two unrelated classes were each called ``FakeApi``. A signature
change cost four to eight files.

The one interface that already had a single shared double — ``Harness``, whose
fake lives in production at ``issuebot/plugins/harnesses/fake/harness.py`` — is
the one whose signature can change by editing one file. This is that pattern,
applied to the rest.
"""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap
import threading
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel
from typer.testing import CliRunner

import issuebot
from issuebot import plugins, release, runner
from issuebot.config import Config, Connection, source_plugin
from issuebot.context import RunnerContext
from issuebot.contracts import Changes, McpServer, WorkItem
from issuebot.plugins.sources.base import Source
from issuebot.plugins.workspaces.base import Prepared, Workspace, WorkspaceProblem
from issuebot.process import REAL, Completed, Process, RecordingProcess

# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def work(
    task_id: str = "t1",
    reference: str | None = "ISS-1",
    *,
    board_id: str | None = "b",
    kind: str = "assigned",
    notification_id: str | None = None,
    actor_name: str | None = None,
    comment_excerpt: str | None = None,
) -> WorkItem:
    """A work item, with the fields a test cares about and defaults for the rest."""
    return WorkItem(
        task_id=task_id,
        reference=reference,
        source_ref=board_id,
        kind="mention" if kind == "mention" else "assigned",
        notification_id=notification_id,
        actor_name=actor_name,
        comment_excerpt=comment_excerpt,
    )


def mention(task_id: str = "t1", reference: str | None = "ISS-1", **kw: Any) -> WorkItem:
    """A mention work item."""
    kw.setdefault("actor_name", "Ada")
    kw.setdefault("comment_excerpt", "what do you think?")
    # A mention is claimed by its notification, so every one carries an id.
    kw.setdefault("notification_id", "n1")
    return work(task_id, reference, kind="mention", **kw)


def ctx(**overrides: Any) -> RunnerContext:
    """A RunnerContext with test-friendly defaults (no heartbeat, no timeout).

    The board endpoints are no longer context *fields*: they ride
    `plugin_settings` as the installed source's own table, which the factories
    hand to the plugin that owns it. So the default here is
    :func:`source_table`, the same registry-keyed table `config` builds — a test
    that needs different endpoints overrides `plugin_settings` whole."""
    base: dict[str, Any] = {
        "plugin_settings": source_table(),
        "heartbeat_interval": 0,
    }
    base.update(overrides)
    return RunnerContext(**base)


def in_process_environment() -> str | None:
    """The environment that runs work in this process, or None if none is installed.

    Tolerant where :func:`issuebot.runner.in_process_environment` refuses,
    because this is a *fixture's* question: a builder that raised would take the
    whole suite down with that plugin instead of only the tests that need it,
    and a connection naming no environment is still perfectly valid on an
    install that has one left (see ``config.executor_name``). A test that
    genuinely cannot run without one says so with :func:`needs_in_process`.

    Registry-derived rather than spelled, like `source_table` below: a core test
    needs *the* environment that runs work here, not a particular one.
    """
    try:
        return runner.in_process_environment()
    except plugins.UnknownPlugin:
        return None


def needs_in_process() -> str:
    """The in-process environment's name, skipping the test when none is installed.

    For a test that is *about* running work in this process — with that plugin
    deleted the behaviour is gone rather than broken, and a skip says so."""
    name = in_process_environment()
    if name is None:
        pytest.skip("no installed environment runs work in this process")
    return name


def connection(**overrides: Any) -> Connection:
    """A connection that runs its work in this process, defaulting to the
    simplest working shape.

    ``board``, ``done`` and the rest are no longer declared Connection fields —
    they are plugin-owned extras — so this builds through ``model_validate``
    (a plain kwarg call would reject them statically).

    The executor is named — resolved from the registry, never spelled — because
    with more than one environment installed a connection that names none is a
    config error (``config.executor_name``), and because a test that runs work
    means running it *here*. It comes out None on an install with no in-process
    environment, which is the same as leaving it out."""
    base: dict[str, Any] = {
        "name": "p",
        "board": "b",
        "folder": "/tmp/p",
        "done": "review",
        "executor": in_process_environment(),
    }
    base.update(overrides)
    return Connection.model_validate(base)


def sandbox_connection(**overrides: Any) -> Connection:
    """A connection whose work runs in a sandbox: a fresh clone per task.

    Deliberately does NOT name an execution environment — the sandbox controller
    never reads ``executor``, and a test that had to name one provider to
    exercise the neutral controller would be the leak ADR-0002 exists to
    prevent."""
    base: dict[str, Any] = {
        "name": "p",
        "board": "b",
        "repo": "https://example.com/r.git",
        "git_init": "branch",
    }
    base.update(overrides)
    return Connection.model_validate(base)


def sink_answers(**picks: str) -> str:
    """The input lines the connect wizard's per-sink questions consume.

    The wizard asks about every *offered* sink, in name order, so how many
    lines a scripted run must supply is whatever the registry answers with —
    same trick as `test_cli._executor_answer`, and the reason a sink plugin can
    be added or deleted without re-counting the newlines in every wizard test.
    Hidden sinks are not asked about, so they cost no line.

    `picks` answers a named sink ("2" required, "3" best-effort); every other
    sink gets an empty line, which is its default of "no".
    """
    return "".join(f"{picks.get(name, '')}\n" for name in plugins.offered("sinks"))


def source_table() -> dict[str, Any]:
    """A valid global settings table for whichever source is installed.

    Keyed off the registry rather than spelled: a core test needs *a* source
    configured, not a particular one, and naming one here is exactly the
    coupling the plugin boundary exists to prevent.

    The *keys* are still one source's, and there is no way round that — a table
    has to satisfy whichever source's `global_settings` model is installed, and
    only that plugin knows what is in it. They used to be defensible as "what
    `RunnerContext` declares as core fields"; it declares none of them any more,
    which is the whole of task 22. So this is now honestly "the shape issuebear's
    table has", and a second source shipping means a `setup`-style hook here
    rather than a longer literal."""
    return {
        source_plugin().name: {
            "api_url": "https://api",
            "mcp_url": "https://mcp",
            "pat": "pat-123",
        }
    }


class NoSettings(BaseModel):
    """A workspace-settings stand-in, for doubles that never read them."""


def wiring(
    conn: Connection | None = None,
    *,
    api: Any = None,
    harness: Any = None,
    context: RunnerContext | None = None,
    source: Any = None,
    workspace: Any = None,
    workspace_settings: BaseModel | None = None,
    environment: Any = None,
    sinks: list | None = None,
    install_id: str | None = None,
    environment_name: str | None = None,
) -> runner.Wiring:
    """A `Wiring` the way `runner.wire` assembles one, with any piece swapped
    for a double.

    This is the test-injection seam the old `ProjectListener` keyword
    arguments used to be. Doubles land *before* the environment is built, so a
    real environment holds the same pieces the job builder reads; a stubbed
    `environment` skips the registry entirely, which also lets a wiring be
    built for a connection whose executor could not resolve here.
    """
    from issuebot.plugins.harnesses.fake.harness import FakeHarness

    conn = conn if conn is not None else connection()
    context = context if context is not None else ctx()
    api = api if api is not None else FakeApi()
    harness = harness if harness is not None else FakeHarness(exit_code=0)

    if source is None:
        source = runner.source_for(
            api, conn, context, agent_id=context.agent_id, install_id=install_id
        )

    if workspace is None:
        workspace, resolved = runner.workspace_for(conn, context)
        if workspace_settings is None:
            workspace_settings = resolved
    if workspace_settings is None:
        workspace_settings = NoSettings()

    built = runner.Wiring(
        api=api,
        ctx=context,
        connection=conn,
        harness=harness,
        source=source,
        workspace=workspace,
        workspace_settings=workspace_settings,
        sinks=sinks if sinks is not None else [],
    )

    if environment is None:
        environment = runner.environment_for(built, name=environment_name)
    return replace(built, environment=environment)


def config(**overrides: Any) -> Config:
    """A config carrying a valid source table and a harness, defaulting to the
    simplest working shape.

    A harness has to be named: there is no privileged default any more, and this
    install has three to choose from, so a config that named none would not
    validate. `fake` is the one a core test means whichever harness ships —
    it records launches instead of spawning a CLI — and it is production code
    (`plugins/harnesses/fake`), not a double defined here."""
    base: dict[str, Any] = {"harness": "fake", **source_table()}
    base.update(overrides)
    return Config.model_validate(base)


# ---------------------------------------------------------------------------
# The board API
# ---------------------------------------------------------------------------


class FakeApi:
    """The board, as far as the runner is concerned.

    Covers every method the runner, the listener and the supervisor call, and
    records each one. Behaviour is set through the constructor rather than by
    subclassing, so a test that wants a claim to fail says so instead of
    defining a class.
    """

    def __init__(
        self,
        *,
        task: dict[str, Any] | None = None,
        work_items: list[dict[str, Any]] | None = None,
        claim_error: Exception | None = None,
        connect_error: Exception | None = None,
        run_id: str | None = "r1",
        agent_id: str | None = None,
        members: list[dict[str, Any]] | None = None,
    ) -> None:
        self._task = task or {
            "id": "t1",
            "reference": "ISS-1",
            "requester_id": "u-req",
            "assignee_id": "u-req",
        }
        self._work_items = list(work_items or [])
        self._claim_error = claim_error
        self._connect_error = connect_error
        self._run_id = run_id
        self._agent_id = agent_id
        self._members = list(members or [])
        self._served = threading.Event()

        # Everything that happened, for assertions.
        self.calls: list[tuple[str, Any]] = []
        self.claims: list[str] = []
        self.claim_kwargs: list[dict[str, Any]] = []
        self.releases: list[dict[str, Any]] = []
        self.comments: list[tuple[str, str]] = []
        self.updates: list[tuple[str, dict[str, Any]]] = []
        self.heartbeats: list[str] = []
        self.sandbox_reports: list[dict[str, Any]] = []
        self.wait_board_ids: list[str | None] = []
        self.mention_board_ids: list[str | None] = []
        self.mention_claims: list[str] = []
        self.member_lookups: list[str] = []
        self.telemetry: list[dict[str, Any]] = []
        self.released = threading.Event()

    # -- the two work lists --------------------------------------------------
    #
    # The board lists work until it is claimed. These serve the scripted items
    # on the first read and nothing after, which is what a listener that claims
    # everything it is offered sees.

    def _scripted(self, *, mentions: bool) -> list[dict[str, Any]]:
        """The scripted items of one kind, served once."""
        wanted = [item for item in self._work_items if (item.get("kind") == "mention") is mentions]
        return wanted if not self._served.is_set() else []

    def get_tasks(self, *, board_id: str | None = None, wait: int = 0) -> list[dict[str, Any]]:
        """The outstanding tasks, then nothing — blocking briefly once served so
        the poll loop doesn't spin."""
        self.wait_board_ids.append(board_id)
        tasks = self._scripted(mentions=False)

        # Set after both reads have been served, so one poll sees both lists.
        if self._served.is_set():
            time.sleep(min(wait, 0.05))

        return tasks

    def get_mentions(self, *, board_id: str | None = None, wait: int = 0) -> list[dict[str, Any]]:
        """The outstanding mentions, then nothing."""
        self.mention_board_ids.append(board_id)
        mentions = self._scripted(mentions=True)
        self._served.set()
        return mentions

    # -- the run lock -------------------------------------------------------

    def claim_mention(self, notification_id: str) -> dict[str, Any]:
        """Acknowledge a mention and open its responding run."""
        self.mention_claims.append(notification_id)
        self.calls.append(("claim_mention", notification_id))
        return {"notification_id": notification_id, "task_id": "t1", "run_id": self._run_id}

    def claim(self, task_id: str, **kwargs: Any) -> dict[str, Any]:
        self.claims.append(task_id)
        self.claim_kwargs.append(kwargs)
        self.calls.append(("claim", task_id))
        if self._claim_error is not None:
            raise self._claim_error
        return {"run_id": self._run_id, "task_id": task_id}

    def heartbeat(self, run_id: str) -> None:
        self.heartbeats.append(run_id)

    def release(self, run_id: str, *, status: str = "done", note: str | None = None) -> None:
        self.releases.append({"run_id": run_id, "status": status, "note": note})
        self.calls.append(("release", status))
        self.released.set()

    # The optional `SandboxLifecycle` capability, recorded for assertions.

    def sandbox_started(self, run_id: str, *, environment: str, sandbox_id: str) -> None:
        self.sandbox_reports.append(
            {
                "run_id": run_id,
                "event": "started",
                "environment": environment,
                "sandbox_id": sandbox_id,
            }
        )

    def sandbox_destroyed(self, run_id: str) -> None:
        self.sandbox_reports.append({"run_id": run_id, "event": "destroyed"})

    # -- tasks --------------------------------------------------------------

    def get_task(self, task_id: str) -> dict[str, Any]:
        return self._task

    def add_comment(self, task_id: str, body: str) -> dict[str, Any]:
        self.comments.append((task_id, body))
        self.calls.append(("comment", body))
        return {"id": "c1"}

    def update_task(self, task_id: str, **fields: Any) -> dict[str, Any]:
        self.updates.append((task_id, fields))
        self.calls.append(("update", fields))
        return {"id": task_id}

    def list_board_members(self, board_id: str) -> list[dict[str, Any]]:
        self.member_lookups.append(board_id)
        return list(self._members)

    # -- connection lifecycle ----------------------------------------------

    def connect(
        self, board_id: str, name: str | None = None, install_id: str | None = None
    ) -> dict[str, Any]:
        self.calls.append(("connect", board_id))
        if self._connect_error is not None:
            raise self._connect_error
        return {"agent": {"id": self._agent_id}} if self._agent_id else {}

    def disconnect(self, board_id: str) -> None:
        self.calls.append(("disconnect", board_id))

    def close(self) -> None:
        self.calls.append(("close", None))

    def register_install(self, hostname: str | None) -> str:
        self.calls.append(("register_install", hostname))
        return "inst-1"

    # -- background loops ---------------------------------------------------

    def wait_for_commands(
        self, *, install_id: str | None = None, timeout: int = 25
    ) -> list[dict[str, Any]]:
        time.sleep(min(timeout, 0.01))
        return []

    def ack_command(self, command_id: str, *, status: str, result: str | None = None) -> None:
        self.calls.append(("ack", command_id))

    def report_telemetry(self, **kwargs: Any) -> None:
        self.telemetry.append(kwargs)


# ---------------------------------------------------------------------------
# The board API, as the CLI talks to it
# ---------------------------------------------------------------------------
#
# `issuebot connect`/`disconnect`/`doctor` reach the board through the client
# their `cli.Session` builds, so a test drives them with a runner whose invokes
# carry a session holding a double — `cli_runner(StubClient())`. These three
# doubles are what goes in it. They live here rather than in `test_cli.py`
# because a plugin's own end-to-end tests — "does `issuebot connect --executor
# <me>` write a working connection" — need exactly the same doubles, and those
# tests belong in the plugin's test directory, not in core's.


class _SessionRunner(CliRunner):
    """A CliRunner that hands every invoke the same ``cli.Session``."""

    def __init__(self, obj: Any) -> None:
        super().__init__()
        self._obj = obj

    def invoke(self, *args: Any, **kwargs: Any) -> Any:
        kwargs.setdefault("obj", self._obj)
        return super().invoke(*args, **kwargs)


def cli_runner(client: Any) -> CliRunner:
    """A CliRunner whose invokes carry a ``cli.Session`` built on the given
    client double.

    The command under test then builds its board client through the session
    seam — the swap is on the interface, never on a module attribute
    (ADR-0006)."""
    from issuebot import cli

    return _SessionRunner(cli.Session(make_client=lambda cfg: client))


class OkClient:
    """A source handle for which everything works and nothing is waiting.

    `init` and `doctor` both prove the credentials by asking for the agent's
    work, so "the source answers" is exactly an empty list back. A stub rather
    than a real client over a mock transport: these tests hand the session a
    whole fake client, so what they exercise is the command's handling of the
    answer —
    that a real client builds the right request is the source plugin's own test
    (`tests/plugins/sources/<name>/`), and asserting it here would make every
    CLI test a test of one plugin's REST layer."""

    def get_tasks(self, *, board_id: str | None = None, wait: int = 0) -> list[Any]:
        """No tasks outstanding, reported successfully."""
        return []

    def close(self) -> None:
        """No-op for the stub."""


def ok_client() -> Any:
    """A source handle whose every call succeeds (see :class:`OkClient`)."""
    return OkClient()


class StubClient:
    """The board client for scripted `connect`/`disconnect`, recording each call."""

    def __init__(self, calls: list | None = None, *, raises: Exception | None = None) -> None:
        self.calls = calls if calls is not None else []
        self._raises = raises

    def connect(self, board_id: str, name: str | None = None, install_id: str | None = None):
        """Record the connect call and return a success response (or raise)."""
        self.calls.append(("connect", board_id, name))
        if self._raises is not None:
            raise self._raises
        return {"connected": True, "warning": None}

    def disconnect(self, board_id: str) -> None:
        """Record the disconnect call."""
        self.calls.append(("disconnect", board_id))

    def close(self) -> None:
        """No-op for the stub."""


class WizardStubClient(StubClient):
    """`StubClient` plus the org/project/board listings the connect wizard walks."""

    def __init__(
        self,
        calls: list | None = None,
        *,
        orgs: list[dict] | None = None,
        projects: list[dict] | None = None,
        boards: list[dict] | None = None,
    ) -> None:
        super().__init__(calls)
        self._orgs = orgs if orgs is not None else [{"id": "o1", "name": "Acme"}]
        self._projects = projects if projects is not None else [{"id": "p1", "name": "Web"}]
        self._boards = boards if boards is not None else [{"id": "b1", "name": "Frontend"}]

    def list_organisations(self) -> list[dict]:
        self.calls.append(("list_organisations",))
        return self._orgs

    def list_projects(self, org_id: str) -> list[dict]:
        self.calls.append(("list_projects", org_id))
        return self._projects

    def list_boards(self, project_id: str) -> list[dict]:
        self.calls.append(("list_boards", project_id))
        return self._boards


# ---------------------------------------------------------------------------
# A source
# ---------------------------------------------------------------------------


class FakeSource(Source):
    """A source with just enough behaviour to build a job and run it.

    Shared, for the same reason every other double here is: three files had
    grown their own near-identical `Source` subclass (two stubs plus a third
    that reached for a real plugin's implementation), so every change to the ABC
    cost three edits and a core test failed when a *plugin* changed.

    Behaviour is constructor arguments rather than subclasses — what a job may
    report, what prompt it launches with, and what MCP servers the source hands
    the agent are the only three things a test of the run pipeline ever varies.
    """

    name = "fake"

    def __init__(
        self,
        *,
        permits: frozenset[str] | None = None,
        prompt: str = "do the thing",
        access: tuple[McpServer, ...] = (),
    ) -> None:
        self._permits = permits or frozenset({"changes", "answer", "needs_input", "handoff"})
        self._prompt = prompt
        self._access = access
        self.said: list[tuple[str, str]] = []
        self.applied: list[Any] = []
        self.finished: list[Any] = []
        self.heartbeats: list[str] = []

    @classmethod
    def client(cls, cfg: Any) -> Any:
        """No install-wide handle: a test that needs one passes `FakeApi`."""
        return FakeApi()

    def poll(self, *, timeout: int) -> list[WorkItem]:
        return []

    def claim(self, work):
        return None

    def release(self, claim, response) -> None:
        return None

    def say(self, work, message: str) -> None:
        self.said.append((work.task_id, message))

    def apply(self, work, decision) -> None:
        self.applied.append(decision)

    def finish(self, work, response, results) -> None:
        self.finished.append((response, results))

    def permits(self, work):
        return self._permits

    def prompt(self, work, connection, *, permits, problem=None) -> str:
        """The scripted prompt, plus the kinds this job was told it may report.

        Rendered in the same `"kind": "x"` shape a real source uses, because
        what a launch was *offered* is the visible half of `job_for`'s
        `permits ∩ produces` — a test asserting a forbidden kind never reaches
        the agent needs a double that would have offered it. A workspace
        `problem` is prefixed as a marker, so a test can assert `run.execute`
        re-rendered the prompt with it."""
        offered = "".join(f'\n  {{"kind": "{kind}"}}' for kind in sorted(permits))
        prefix = f"[problem:{problem.kind}] " if problem is not None else ""
        return f"{prefix}{self._prompt}{offered}"

    def agent_access(self, work) -> tuple[McpServer, ...]:
        return self._access

    def heartbeat(self, run_id: str) -> None:
        """Recorded, so a test can assert the pipeline kept the run alive."""
        self.heartbeats.append(run_id)


# ---------------------------------------------------------------------------
# The reporter
# ---------------------------------------------------------------------------


class RecordingReporter:
    """A reporter that remembers its whole lifecycle."""

    def __init__(self) -> None:
        self.started: tuple[str, str] | None = None
        self.finished: tuple[str, float] | None = None
        self.events: list[Any] = []
        self.raw_lines: list[str] = []

    def start(self, ref: str, folder: str) -> None:
        self.started = (ref, folder)

    def event(self, ev: Any) -> None:
        self.events.append(ev)

    def raw(self, line: str) -> None:
        self.raw_lines.append(line)

    def finish(self, status: str, elapsed: float) -> None:
        self.finished = (status, elapsed)

    @property
    def summaries(self) -> list[str]:
        return [e.summary for e in self.events]


# ---------------------------------------------------------------------------
# A workspace
# ---------------------------------------------------------------------------


class FakeWorkspace(Workspace):
    """A workspace that records what it was asked to do and hands back
    scripted results, instead of touching git or the filesystem.

    Failure injection is a constructor argument (``prepare_error``), matching
    ``FakeProvider`` — a test that wants a broken workspace says so instead of
    subclassing.
    """

    name = "fake"
    produces = frozenset({"changes", "answer", "needs_input", "handoff"})

    def __init__(
        self,
        *,
        folder: str = "/tmp/w",
        branch: str = "issuebot/ISS-1",
        base_sha: str = "base-sha",
        changes: Changes | None = None,
        prepare_error: Exception | None = None,
        problem: WorkspaceProblem | None = None,
    ) -> None:
        self._folder = folder
        self._branch = branch
        self._base_sha = base_sha
        self._changes = changes
        self._prepare_error = prepare_error
        self._problem = problem
        self.prepare_calls: list[tuple[Any, str]] = []
        self.commit_calls: list[tuple[Prepared, str]] = []

    def prepare(self, connection, ref, *, settings, proc: Process = REAL) -> Prepared:
        self.prepare_calls.append((connection, ref))
        if self._prepare_error is not None:
            raise self._prepare_error
        return Prepared(
            folder=self._folder,
            branch=self._branch,
            base_sha=self._base_sha,
            problem=self._problem,
        )

    def commit_and_push(self, prepared, message, *, settings, proc: Process = REAL) -> Changes:
        self.commit_calls.append((prepared, message))
        if self._changes is not None:
            return self._changes
        return Changes(
            branch=prepared.branch,
            base_sha=prepared.base_sha,
            head_sha=f"{prepared.base_sha}-head",
            stat="1 file changed",
            files_changed=1,
            pushed=True,
        )


# ---------------------------------------------------------------------------
# A sandbox provider
# ---------------------------------------------------------------------------


class FakeProvider:
    """A sandbox provider that records what it was asked to do.

    Failure injection is a constructor argument rather than a subclass: the
    provider-specific double it replaces had nine subclasses, each overriding
    one method to raise.
    """

    name = "fake"
    rebuild_command = "rebuild the fake template"

    def __init__(
        self,
        *,
        result: dict[str, Any] | str | None = None,
        exit_code: int | None = None,
        checkpoints: list[str] | None = None,
        supports_checkpoints: bool = True,
        raises: dict[str, Exception] | None = None,
        lines: list[str] | None = None,
        emit_sentinel: bool = True,
        result_file: str | None = None,
        installed_version: str | None = None,
        update_exit: int = 0,
        update_applies: bool = True,
    ) -> None:
        self.supports_checkpoints = supports_checkpoints
        self._result = result if result is not None else {"status": "done"}
        self._exit_code = exit_code
        self._raises = raises or {}
        self._lines = lines or []
        self._emit_sentinel = emit_sentinel
        self._result_file = result_file

        # Which issuebot this sandbox is, as the probe would answer. Defaults to
        # the controller's own, so a test that says nothing about versions gets
        # the ordinary aligned boot; "" is a sandbox with no issuebot at all,
        # which cannot answer the probe.
        self._installed_version = (
            issuebot.__version__ if installed_version is None else installed_version
        )
        self._update_exit = update_exit
        self._update_applies = update_applies

        self.checkpoints = list(checkpoints or [])
        self.created: dict[str, Any] | None = None
        self.destroyed: str | None = None
        self.exec_argv: list[str] | None = None
        self.exec_calls: list[list[str]] = []
        self.checkpoint_creates: list[tuple[str, str]] = []
        self.checkpoint_deletes: list[str] = []

    def _maybe_raise(self, name: str) -> None:
        if name in self._raises:
            raise self._raises[name]

    def secret_env(self) -> dict[str, str]:
        self._maybe_raise("secret_env")
        return {"FAKE_SECRET": "s3cret"}

    def create(self, *, env: dict[str, str], checkpoint: str | None = None) -> str:
        self._maybe_raise("create")
        self.created = {"env": env, "checkpoint": checkpoint}
        return "sbx_1"

    def exec_stream(self, sandbox_id, argv, *, on_line, cancel=None) -> int:
        """Answer the version probe, the self-update, or run the worker.

        Three different commands reach one verb, so the double dispatches on
        which was asked for. ``raises={"exec_stream": ...}`` stays aimed at the
        *worker* exec — a transport crash mid-run — because that is the failure
        the tests using it are about; a failing self-update is ``update_exit``.

        The probe's answer arrives the way a real one does: wrapped in the
        sandbox CLI's own chatter, on both sides, with whitespace on it. A
        controller that reads the answer by position rather than by shape gets
        the postamble, and every boot then reinstalls.
        """
        from issuebot.sandbox_protocol import update_argv, version_argv

        self.exec_calls.append(list(argv))

        if argv == version_argv():
            if not self._installed_version:
                return 127  # no issuebot in this sandbox at all
            on_line("Connecting to sandbox...")
            on_line(f"  {self._installed_version}\r")
            on_line("Connection to sandbox closed.")
            return 0

        if argv == update_argv(issuebot.__version__):
            if self._update_exit == 0 and self._update_applies:
                self._installed_version = issuebot.__version__
            return self._update_exit

        self._maybe_raise("exec_stream")
        self.exec_argv = argv
        for line in self._lines:
            on_line(line)
        if self._emit_sentinel:
            from issuebot.sandbox_protocol import RunResult

            on_line(RunResult.from_payload(dict(self._result)).sentinel_line())
        if self._exit_code is not None:
            return self._exit_code
        return 0 if dict(self._result).get("status") == "done" else 1

    def read_file(self, sandbox_id: str, path: str) -> str:
        self._maybe_raise("read_file")
        if self._result_file is None:
            raise FileNotFoundError(path)
        return self._result_file

    def destroy(self, sandbox_id: str) -> None:
        self._maybe_raise("destroy")
        self.destroyed = sandbox_id

    def list_checkpoints(self) -> list[str]:
        self._maybe_raise("list_checkpoints")
        return list(self.checkpoints)

    def create_checkpoint(self, sandbox_id: str, name: str) -> None:
        self._maybe_raise("create_checkpoint")
        self.checkpoint_creates.append((sandbox_id, name))

    def delete_checkpoint(self, name: str) -> None:
        self._maybe_raise("delete_checkpoint")
        self.checkpoint_deletes.append(name)


# ---------------------------------------------------------------------------
# Running other programs
# ---------------------------------------------------------------------------
#
# There is no double here: `issuebot.process.RecordingProcess` ships beside the
# real adapter, for the same reason `FakeHarness` does. `completed` is only a
# shorthand for scripting one reply.


def completed(code: int = 0, out: str = "", err: str = "") -> Completed:
    """One scripted command result, for `RecordingProcess(replies=...)`."""
    return Completed([], code, out, err)


class SpawnRecorder(RecordingProcess):
    """A RecordingProcess that also reads the --mcp-config file it was handed.

    Shared by the harness plugins' tests, which each build one. `mcp_config` is
    the one thing every harness writes and every harness's tests want to see;
    the file lives in a TemporaryDirectory that only exists while spawn is
    running (cleaned up the moment it returns, so nothing leaks per launch or
    retry), so a test that wants to assert on its contents has to read it here.

    Anything else on the command line is that harness's own vocabulary and is
    recorded by a subclass in its own test directory, not here."""

    def __init__(self, exit_code: int = 0, lines: list[str] | None = None):
        super().__init__(lines=lines or [], exit_code=exit_code)
        self.mcp_json: dict | None = None
        self.cancel: threading.Event | None = None

    def spawn(self, argv, *, on_line, cwd=None, env=None, cancel=None) -> int:
        """Record the mcp-config contents (if any), then spawn as usual."""
        self.cancel = cancel
        if "--mcp-config" in argv:
            mcp_path = argv[argv.index("--mcp-config") + 1]
            self.mcp_json = json.loads(Path(mcp_path).read_text())
        return super().spawn(argv, on_line=on_line, cwd=cwd, env=env, cancel=cancel)

    @property
    def argv(self) -> list[str] | None:
        """The last command spawned."""
        return self.calls[-1] if self.calls else None

    @property
    def cwd(self) -> str | None:
        """The folder the last command ran in."""
        return self.cwds[-1] if self.cwds else None

    @property
    def env(self) -> dict | None:
        """The environment overlay the last command ran with."""
        return self.envs[-1] if self.envs else None


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


VERSION = "1.2.3"


@pytest.fixture(autouse=True)
def known_release(monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest) -> str:
    monkeypatch.setattr(issuebot, "__version__", VERSION)
    if request.module.__name__ != "test_release":
        monkeypatch.setattr(release, "is_installed_wheel", lambda: True)
    return VERSION


@pytest.fixture(autouse=True)
def isolated_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point every state and config path at a temp dir, for every test.

    Autouse because the alternative is opt-in, and a test that forgets reads and
    writes the developer's real ~/.local/state/issuebot — which is how a suite
    ends up depending on the machine it runs on."""
    root = tmp_path / "xdg"
    monkeypatch.setenv("XDG_STATE_HOME", str(root / "state"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(root / "config"))
    monkeypatch.delenv("ISSUEBOT_STATE", raising=False)
    monkeypatch.delenv("ISSUEBOT_CONFIG", raising=False)
    return root


@pytest.fixture
def config_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point ISSUEBOT_CONFIG at a per-test temp file, for CLI tests."""
    path = tmp_path / "config.toml"
    monkeypatch.setenv("ISSUEBOT_CONFIG", str(path))
    return path


@pytest.fixture
def api() -> FakeApi:
    return FakeApi()


@pytest.fixture
def reporter() -> RecordingReporter:
    return RecordingReporter()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """An initialised git repo with one commit on branch 'main'."""

    def git(*args: str) -> None:
        subprocess.run(["git", *args], cwd=repo_dir, check=True, capture_output=True, text=True)

    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    git("init", "-b", "main")
    git("config", "user.email", "t@example.com")
    git("config", "user.name", "Test")
    (repo_dir / "README.md").write_text("hi\n")
    git("add", "-A")
    git("commit", "-m", "init")
    return repo_dir


@pytest.fixture(autouse=True)
def clear_plugin_cache() -> None:
    """Clear the discovery cache before each test."""
    from issuebot import plugins

    plugins.discover.cache_clear()


@pytest.fixture
def plugin_tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Build an importable package of fake plugins and point discovery at it."""

    def build(**modules: str) -> str:
        root = tmp_path / "fakeplugins"
        (root / "widgets").mkdir(parents=True)
        (root / "__init__.py").write_text("")
        (root / "widgets" / "__init__.py").write_text("")
        for name, body in modules.items():
            pkg = root / "widgets" / name
            pkg.mkdir()
            (pkg / "__init__.py").write_text(textwrap.dedent(body))
        monkeypatch.syspath_prepend(str(tmp_path))
        for mod in [m for m in sys.modules if m.startswith("fakeplugins")]:
            del sys.modules[mod]
        return "fakeplugins"

    return build


__all__ = [
    "VERSION",
    "FakeApi",
    "FakeSource",
    "FakeProvider",
    "FakeWorkspace",
    "RecordingProcess",
    "RecordingReporter",
    "SpawnRecorder",
    "OkClient",
    "StubClient",
    "WizardStubClient",
    "NoSettings",
    "cli_runner",
    "completed",
    "config",
    "connection",
    "ctx",
    "mention",
    "ok_client",
    "sandbox_connection",
    "source_table",
    "wiring",
    "work",
]
