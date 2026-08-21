"""The listen loop: poll a source for work, take the run lock, hand it to an
environment, release. The supervisor owns one listener per connection and
reconciles them when the config changes.

This module decides *whether* work runs, owns the run lock around it, and
builds the :class:`~issuebot.contracts.Job` that says what the run may do.
Where it runs is an
:class:`~issuebot.plugins.environments.base.ExecutionEnvironment`; how a kind of
work is treated is the :class:`~issuebot.plugins.sources.base.Source` itself —
its own judgement about its own work kinds; who does it is a
:class:`~issuebot.plugins.harnesses.base.Harness`. The listener asks none of
those questions.

The ``*_for`` factories below are the one place a connection's settings turn
into plugin instances, one per axis. Each resolves by name through the
registry and constructs with a fixed keyword shape, so adding a plugin is
writing a folder and registering it — never an edit here. :func:`wire` calls
them in the one right order and returns the result as a :class:`Wiring`, so
the listener here and the in-sandbox worker assemble a run identically by
construction.
"""

from __future__ import annotations

import concurrent.futures
import logging
import os
import socket
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from issuebot import install as install_store
from issuebot import plugins
from issuebot import run as run_pipeline
from issuebot.agent_state import AgentState, ConnectionSnapshot, LogTailHandler
from issuebot.commands import run_command_loop
from issuebot.config import (
    Config,
    Connection,
    SinkRef,
    conn_setting,
    executor_name,
    load_config,
    source_plugin,
    unconfigured_workspace,
    workspaces_claiming,
)
from issuebot.context import RunnerContext
from issuebot.contracts import Claim, Job, Response, SinkResult, WorkItem
from issuebot.plugins.base import EnvironmentPlugin, SinkPlugin, WorkspacePlugin
from issuebot.plugins.environments.base import ExecutionEnvironment
from issuebot.plugins.harnesses.base import Harness
from issuebot.plugins.sinks.base import Sink
from issuebot.plugins.sources.base import ConnectionConflict, Source, SourceClient
from issuebot.plugins.workspaces.base import Workspace
from issuebot.reporter import ConsoleReporter
from issuebot.sessions import SessionStore
from issuebot.status import StatusStore, build_payload, default_status_path
from issuebot.transient import log_poll_failure, log_poll_recovered
from issuebot.verify import verify

logger = logging.getLogger("issuebot")


class _ThreadFilter(logging.Filter):
    """Pass only log records emitted from a single named thread.

    Lets a per-listener ``LogTailHandler`` capture just that listener's runner
    log lines (it runs on a thread named ``listen-{name}``), routing them to its
    own state's tail rather than mixing every connection's logs together.
    """

    def __init__(self, thread_name: str) -> None:
        super().__init__()
        self._t = thread_name

    def filter(self, record: logging.LogRecord) -> bool:
        """True only when the record was logged from the watched thread."""
        return record.threadName == self._t


def source_for(
    api: SourceClient,
    project: Connection,
    ctx: RunnerContext,
    *,
    agent_id: str | None = None,
    install_id: str | None = None,
) -> Source:
    """Build the `Source` a connection's `source` setting selects.

    A connection naming no source gets the one this install has — resolved by
    :func:`~issuebot.config.source_plugin`, which is also what `init`/`doctor`
    ask, so a connection and an install-wide command can never disagree about
    which source that is.

    The board endpoints come from the source's *own* global settings table,
    validated against its own model and splatted as constructor keywords —
    exactly as `sinks_for` hands each sink its table, so core carries no
    source's field names.

    ponytail: calls every source plugin's implementation with the same fixed
    keyword shape (as `sinks_for` assumes for every sink). With exactly one
    source plugin today this is not yet a real constraint; a second one can
    adjust this factory when it exists.
    """
    plugin = source_plugin(project.source)
    kwargs: dict[str, Any] = {
        "client": api,
        "board": conn_setting(project, "board"),
        "connection": project,
        "install_id": install_id,
        "agent_id": agent_id,
    }
    if plugin.global_settings is not None:
        table = ctx.plugin_settings.get(plugin.name) or {}
        kwargs |= plugin.global_settings.model_validate(table).model_dump()

    return plugin.source(**kwargs)


def sinks_for(
    project: Connection, harness: Harness, settings: Mapping[str, Any]
) -> list[tuple[SinkRef, Sink]]:
    """Build every sink a connection declares, in the order it lists them.

    Each sink is constructed with the run's summarizer (`harness`) plus the
    fields of its *own* global settings table, validated against its own model —
    `settings` is every plugin's table by name (`ctx.plugin_settings`), and a
    sink is only ever handed the one it owns. So a sink's global setting is a
    constructor keyword it declares, not a field core carries.

    ponytail: still one fixed keyword shape for every sink plugin, as
    `source_for` assumes for every source. The `harness` half is the fixed part
    (a sink that needs no summarizer ignores it); the settings half is the
    plugin's own declaration.
    """
    resolved: list[tuple[SinkRef, Sink]] = []
    for ref in project.sinks:
        plugin = plugins.get("sinks", ref.name)
        if not isinstance(plugin, SinkPlugin):
            raise TypeError(f"sink plugin '{ref.name}' has no implementation")

        kwargs: dict[str, Any] = {"harness": harness}
        if plugin.global_settings is not None:
            table = settings.get(plugin.name) or {}
            kwargs |= plugin.global_settings.model_validate(table).model_dump()

        resolved.append((ref, plugin.sink(**kwargs)))
    return resolved


def workspace_for(project: Connection, ctx: RunnerContext) -> tuple[Workspace, BaseModel]:
    """Build the `Workspace` a connection's own settings select, with its settings.

    A workspace plugin is *flat* — its keys sit directly on the connection — so
    the selection is simply which of them the connection sets: each plugin's
    own claimed keys select it, and a connection setting none works in place —
    resolved by :func:`unconfigured_workspace`, which is the one thing here
    that is not the connection's own doing.

    This is the same rule `validate_config` uses to decide which plugins are in
    play, so config validation and the run agree about what a connection is
    wired to — *including* a keyless one: `plugins_in_play` calls the same
    fallback below for that case, so the rules the run will be held to are the
    rules load applies.

    ponytail: a connection setting keys of *two* workspace plugins is
    contradictory config nothing rejects yet; the first in name order wins and
    the mismatch is logged. A `validate` hook spanning workspace plugins is
    where that belongs, once one exists.
    """
    matched = workspaces_claiming((project.model_extra or {}).keys())
    if len(matched) > 1:
        logger.warning(
            "connection %s sets keys of several workspaces (%s); using '%s'",
            project.name,
            ", ".join(p.name for p in matched),
            matched[0].name,
        )

    plugin = matched[0] if matched else unconfigured_workspace()
    if not isinstance(plugin, WorkspacePlugin):
        raise TypeError(f"workspace plugin '{plugin.name}' has no implementation")

    assert plugin.settings is not None, f"workspace plugin '{plugin.name}' declares no settings"
    settings = plugin.settings.model_validate(project.settings_for(plugin))

    # The workspace's *own* global table, validated against its own model and
    # splatted as constructor keywords — the same thing `sinks_for` does per
    # sink and `source_for` does for the source.
    kwargs: dict[str, Any] = {}
    if plugin.global_settings is not None:
        table = ctx.plugin_settings.get(plugin.name) or {}
        kwargs |= plugin.global_settings.model_validate(table).model_dump()

    return plugin.workspace(**kwargs), settings


def in_process_environment() -> str:
    """The installed environment that runs a job in the calling process.

    Resolved by capability, never by name. The in-sandbox worker has already
    been placed inside its sandbox, so what it needs is "whatever runs the work
    right here" — which is
    :attr:`~issuebot.plugins.environments.base.ExecutionEnvironment.runs_in_process`,
    read off the class the registry holds, exactly as git's `validate` reads
    :attr:`~issuebot.plugins.sinks.base.Sink.needs_pushed_branch` off the sink
    classes it finds there — importing one environment class by name would
    make that plugin the only undeletable one on its axis.

    Exactly one has to claim it: with none there is nothing that can run the
    work here, and with two there is no non-arbitrary choice — and taking
    whichever sorted first is how a privileged default gets reinvented under a
    new name. Both raise :class:`~issuebot.plugins.UnknownPlugin` naming what is
    installed, so a missing capability reads as a sentence rather than a
    ``StopIteration`` five frames down.
    """
    running = sorted(
        name
        for name, plugin in plugins.all_of("environments").items()
        if isinstance(plugin, EnvironmentPlugin) and plugin.environment.runs_in_process
    )
    if len(running) != 1:
        raise plugins.UnknownPlugin(
            f"{len(running)} installed environments run work in this process, need "
            f"exactly one (installed: {', '.join(plugins.names_of('environments')) or 'none'})"
        )
    return running[0]


@dataclass(frozen=True)
class Wiring:
    """The assembled run machinery for one connection — what :func:`wire` returns.

    One value holding what the factories above produce, so the two places that
    run work — the listener on the controller, the worker inside a sandbox —
    hold one thing instead of five pieces, and neither can thread them together
    in the wrong order: the build order lives in :func:`wire`, stated once.
    """

    # The source's own client, as handed to `wire`. An environment that reports
    # a remote machine's lifecycle (a sandbox) talks to the board through it —
    # via the optional `plugins.sources.base.SandboxLifecycle` capability.
    api: SourceClient

    # The runner-wide settings every run shares (timeout, heartbeat, session
    # store, live state, each plugin's global table).
    ctx: RunnerContext

    # The run's own copy of the connection (see `wire`). The repo sync corrects
    # this instance, never the one the Supervisor compares configs against.
    connection: Connection

    # Who does the work.
    harness: Harness

    # This connection's whole work-item lifecycle: discover, claim, narrate,
    # apply decisions, finish.
    source: Source

    # Where work is prepared, with that plugin's own validated settings.
    workspace: Workspace
    workspace_settings: BaseModel

    # This connection's declared sinks, in the order it lists them.
    sinks: list[tuple[SinkRef, Sink]]

    # Where the work runs. Filled by `wire` after everything else is assembled:
    # the environment is built *over* the wiring, so it cannot be a constructor
    # argument of it. None only inside `environment_for` itself and in tests
    # that never run one — every `wire` caller gets a built environment.
    environment: ExecutionEnvironment | None = None


def environment_for(wiring: Wiring, *, name: str | None = None) -> ExecutionEnvironment:
    """Build the `ExecutionEnvironment` a connection's `executor` setting selects.

    The one place that maps a setting to an environment. Call sites hold an
    `ExecutionEnvironment` and never ask which kind it is.

    Every environment is constructed the same way — the wiring, plus optionally
    a `proc` double — and reads what it needs of it: a sandbox decides harness,
    workspace and source on the far side of the wire, so it reads only the
    client, the connection and the context; a local run reads the rest.

    `name` overrides the connection's choice for the one caller whose
    environment is not the connection's: the in-sandbox worker, which is
    already inside the sandbox the connection asked for and must run the work
    where it stands (:func:`in_process_environment`).
    """
    chosen = name or executor_name(wiring.connection)
    plugin = plugins.get("environments", chosen)
    if not isinstance(plugin, EnvironmentPlugin):
        raise TypeError(f"environment plugin '{chosen}' has no implementation")

    return plugin.environment(wiring)


def wire(
    api: SourceClient,
    harness: Harness,
    connection: Connection,
    ctx: RunnerContext,
    *,
    install_id: str | None = None,
    environment_name: str | None = None,
) -> Wiring:
    """Assemble one connection's run machinery, in the one right order.

    The order is the point, and it lives only here:

    1. The connection is copied, so nothing downstream can edit the instance
       the Supervisor stored to compare configs against.
    2. Source, then workspace and sinks, over that same copy.
    3. The environment is built last, over the assembled wiring, so it holds
       the same pieces every run through it reads.

    ``environment_name`` overrides the connection's own `executor` choice for
    the in-sandbox worker (see :func:`environment_for`).
    """
    connection = connection.model_copy()

    source = source_for(api, connection, ctx, agent_id=ctx.agent_id, install_id=install_id)
    workspace, workspace_settings = workspace_for(connection, ctx)
    sinks = sinks_for(connection, harness, ctx.plugin_settings)

    wiring = Wiring(
        api=api,
        ctx=ctx,
        connection=connection,
        harness=harness,
        source=source,
        workspace=workspace,
        workspace_settings=workspace_settings,
        sinks=sinks,
    )
    return replace(wiring, environment=environment_for(wiring, name=environment_name))


class RepoMismatch(RuntimeError):
    """This connection is configured for a different repository than the task's
    project is linked to."""


def check_repo(connection: Connection, work: WorkItem) -> None:
    """Refuse work that belongs to a different repository than this connection.

    The board says which repository a task's project is linked to; the config
    says which one this connection works in. When they disagree, one of the two
    is wrong and neither this runner nor the board can tell which — the project
    may have been relinked, or the config edited by hand. Doing the work anyway
    is the worst of the options: it produces a branch and a PR on the wrong
    repository, which never surface on the task, so it reads exactly like the
    agent silently doing nothing.

    Raising is the whole point. `job_for` runs this before there is a workspace
    to prepare, and the caller turns it into a failed run with this message on
    the task, so a person sees which two URLs disagree and fixes one of them.

    Two things are not a mismatch, and both mean "nothing to compare":

    * The board sent no repo — the project is unlinked, or the board could not
      confirm the link with GitHub just now. Neither says anything about this
      connection.
    * The connection is configured with a `folder` rather than a `repo`. It
      works in a checkout that is already on this machine, and the URL that
      checkout came from is git's business, not the config's.
    """
    configured = conn_setting(connection, "repo")
    if work.repo is None or configured is None:
        return

    if work.repo != configured:
        raise RepoMismatch(
            f"task {work.ref} belongs to {work.repo}, but connection "
            f"'{connection.name}' is configured for {configured}"
        )


def job_for(work: WorkItem, wiring: Wiring, *, run_id: str = "") -> Job:
    """Everything an environment needs to run one work item, decided here.

    ``permits`` is ``source.permits(work) ∩ workspace.produces`` (ADR-0011):
    what this source allows this kind of work to report, narrowed by what this
    workspace could physically produce. A folder workspace has no git to derive
    `Changes` from, so a run in one is never told it may report `changes` — the
    intersection is computed once, here, rather than trusted to each
    environment or discovered when a run fails.

    The workspace is asked with the connection's own settings
    (``produces_for``), not off its class: a git connection that cuts no task
    branch has nothing to derive `Changes` from either, and the class cannot
    know that.

    ponytail: ``withheld_tools`` stays empty. Its natural rule is "a run that
    may not report `changes` should not hold the tools that make them", but the
    only vocabulary for that today is one agent CLI's own tool names, and
    spelling those in the runner would be the same kind of leak ADR-0002 forbids
    for environments. It needs a harness-neutral capability name first.
    """
    ctx = wiring.ctx
    source = wiring.source

    check_repo(wiring.connection, work)

    permits = source.permits(work) & wiring.workspace.produces_for(wiring.workspace_settings)
    return Job(
        work=work,
        prompt=source.prompt(work, wiring.connection, permits=permits),
        folder=wiring.connection.folder,
        permits=permits,
        withheld_tools=(),
        timeout_minutes=ctx.timeout_minutes,
        mcp_servers=source.agent_access(work),
        env={},
        resume_session_id=ctx.store.get(work.task_id) if ctx.store else None,
        run_id=run_id,
    )


class ProjectListener:
    """Polls one board for the work outstanding against this agent and runs it."""

    def __init__(
        self,
        wiring: Wiring,
        *,
        wait_timeout: int = 25,
        max_concurrent: int = 1,
        slots: threading.BoundedSemaphore | None = None,
    ) -> None:
        """Hold one connection's assembled run machinery.

        ``wiring`` is what :func:`wire` returns; the listener adds only the
        loop around it — polling, claiming, dispatching, releasing. A test that
        wants a double anywhere in the machinery swaps it into the `Wiring`
        rather than through constructor keywords here.
        """
        self._wiring = wiring
        self._project = wiring.connection
        self._ctx = wiring.ctx

        # `board` is issuebear's per-connection setting, not a declared
        # Connection field — read once here rather than at each use below.
        self._board = conn_setting(wiring.connection, "board")

        self._wait_timeout = wait_timeout
        self._max_concurrent = max_concurrent

        # This connection's whole work-item lifecycle: discover, claim,
        # narrate, apply decisions, finish. The listener never asks which
        # source it holds, only ever `poll`/`claim`/`release`/`apply`/`finish`.
        self._source = wiring.source

        # Where this connection's work runs. `wire` always builds one — the
        # None default on the field exists only for the environment's own
        # construction (see `Wiring.environment`).
        assert wiring.environment is not None, "wire() builds the environment"
        self._environment = wiring.environment

        # This connection's declared sinks, delivered to by `_finish`.
        self._sinks = wiring.sinks

        self._state = self._ctx.state if self._ctx.state is not None else AgentState()
        self._stop = threading.Event()

        # Abort signals of every run currently in flight, keyed by run id, so
        # stop() can cancel all of them rather than just the most recent.
        self._active_cancels: dict[str, threading.Event] = {}
        self._cancels_lock = threading.Lock()

        # Bounded worker pool for concurrent dispatch, created in `run` when the
        # connection allows more than one at a time. None keeps today's exact
        # serial in-line processing on the poll thread.
        self._pool: concurrent.futures.ThreadPoolExecutor | None = None

        # `max_concurrent` counts the tasks *this runner* works at once, so the
        # cap is one object shared by every listener — the Supervisor's. A
        # listener built on its own (a test, a single-connection run) gets one
        # of its own, which for one connection is the same thing. Without a
        # shared one, each listener enforced the cap privately and a config with
        # three connections ran three times the number it asked for.
        self._slots = slots or threading.BoundedSemaphore(max_concurrent)

    # -- live state ---------------------------------------------------------

    def snapshot(self) -> ConnectionSnapshot:
        """This connection's published state: its static identity stamped onto
        the live state. The one snapshot every publisher receives, so `issuebot
        status` and server telemetry cannot disagree.

        The published "target" (folder, else the workspace's repo) is read per
        call rather than cached at construction, so a config reloaded under a
        live listener publishes what it now says. `_board` stays cached."""
        target = self._project.folder or conn_setting(self._project, "repo") or ""

        return self._state.snapshot(
            name=self._project.name or "",
            board=self._board,
            target=target,
        )

    @property
    def state(self) -> AgentState:
        """This listener's own live state (phase, log tail, links) for telemetry."""
        return self._state

    def _unregister_cancel(self, run_id: str) -> None:
        """Stop tracking a run's abort signal once it has finished."""
        with self._cancels_lock:
            self._active_cancels.pop(run_id, None)

    def _has_active_runs(self) -> bool:
        """True while any run is still in flight."""
        with self._cancels_lock:
            return bool(self._active_cancels)

    def _record_idle(self) -> None:
        """Report this connection idle — but only once nothing is left running.

        The status view is a single slot per connection while the pool can have
        several runs in flight, so the first worker to finish must not declare
        the whole connection idle underneath its still-working siblings.
        """
        if not self._has_active_runs():
            self._state.set_phase("idle")

    def _safe_release(self, claim: Claim, response: Response) -> None:
        """Best-effort release of a claim. Releasing must never crash the poll
        thread or a pool worker."""
        try:
            self._source.release(claim, response)
        except Exception:  # noqa: BLE001 — release is best-effort; never crash the caller
            logger.warning("failed to release run for %s", claim.work_id, exc_info=True)

    def stop(self) -> None:
        """Signal the run loop to stop, abort every run in flight, and shut the
        pool down without waiting.

        Ordering matters: ``_stop`` is set FIRST, and only then do we snapshot
        ``_active_cancels`` under the lock. ``_run_claimed`` takes that same lock
        to check ``_stop`` and register its cancel Event as one atomic step, so
        there is no window where a worker starts after this snapshot but still
        boots uncancelled: either it registered before the snapshot (and its
        Event is set below), or it sees ``_stop`` already set and bails without
        starting, releasing the run instead.
        """
        self._stop.set()
        with self._cancels_lock:
            cancels = list(self._active_cancels.values())
        for cancel in cancels:
            cancel.set()
        if self._pool is not None:
            self._pool.shutdown(wait=False)

    # -- the poll loop ------------------------------------------------------

    def run(self) -> None:
        """Poll for outstanding work and run each item until stopped.

        Every poll is a full reconciliation: the source answers with everything
        still waiting, so an item this listener could not take — the claim was
        lost, the pool was full, the board was unreachable — is simply on the
        next answer. Nothing has to be remembered between rounds.

        With ``max_concurrent > 1`` the work is dispatched to a bounded thread
        pool so several runs proceed at once; claiming itself always stays on
        this poll thread, so a claim genuinely lost still gates correctly.
        """
        target = self._project.folder or conn_setting(self._project, "repo")
        logger.info("listening on board %s → %s", self._board, target)
        if self._max_concurrent > 1:
            self._pool = concurrent.futures.ThreadPoolExecutor(max_workers=self._max_concurrent)
        transient_fails = 0

        while not self._stop.is_set():
            # Only report waiting when nothing is actually running: with a pool
            # the poll thread keeps looping while workers are mid-run, and must
            # not paint over their "working" state.
            if not self._has_active_runs():
                self._state.set_phase("waiting")

            try:
                items = self._source.poll(timeout=self._wait_timeout)
            except Exception as exc:  # noqa: BLE001
                transient_fails = log_poll_failure(logger, "Board API", exc, transient_fails)
                self._stop.wait(3)
                continue

            transient_fails = log_poll_recovered(logger, "Board API", transient_fails)

            for work in items:
                if self._stop.is_set():
                    return
                self._process(work)

    def _process(self, work: WorkItem) -> None:
        """Claim one work item and hand it to the run pipeline, if claiming
        succeeds.

        Whether a claim locks anything is the source's own business (a mention
        gets the board's non-locking "responding" run back instead of racing
        for one) — the listener only ever asks for a `Claim` or `None`, so a
        new kind of work routes correctly here without an edit.

        A free slot is taken *before* the claim, never after: claiming first
        and then waiting for capacity would hold a board lock on work nothing
        is running yet. And it is *waited for*, not tested, because pausing
        this listener's polling is the right answer anyway: there is no point
        fetching work faster than it can be run.
        """
        while not self._slots.acquire(timeout=0.5):
            if self._stop.is_set():
                return

        handed_off = False
        try:
            claim = self._source.claim(work)
            if claim is None:
                return  # lost the race, or the attempt failed — redelivered by the next poll

            # Claiming always happens on the poll thread so a lost race still
            # gates correctly even when the pool is dispatching concurrently. Only
            # the execute/release body moves to a worker.
            if self._pool is None:
                handed_off = True
                self._run_and_free(work, claim)
                return

            # claim() is a network round-trip, so stop() can shut the pool down in
            # the gap before this submit. Guard both halves of that race: bail if
            # already stopped, and tolerate submit() itself raising — either way
            # release the claim rather than crashing the poll thread.
            stopped = Response(status="failed", result_text="listener stopped")
            if self._stop.is_set():
                self._safe_release(claim, stopped)
                return
            try:
                self._pool.submit(self._run_and_free, work, claim)
                handed_off = True
            except RuntimeError:  # pool shut down concurrently
                self._safe_release(claim, stopped)
        finally:
            # Whoever ends up running the work gives the slot back when it is
            # done; every path that never got that far gives it back here.
            if not handed_off:
                self._slots.release()

    def _run_and_free(self, work: WorkItem, claim: Claim) -> None:
        """Run one claimed item, then give its concurrency slot back."""
        try:
            self._run_claimed(work, claim)
        finally:
            self._slots.release()

    # -- running ------------------------------------------------------------

    def _execute(self, job: Job, cancel: threading.Event) -> Response:
        """Hand the job to this connection's environment and report the outcome.

        Where a resumable session is remembered, too: ``job.resume_session_id``
        was read from the store when the job was built, and whatever session the
        run comes back with is what a later run for this task resumes into. The
        environment does not touch the store — a sandbox run's session id
        arrives over the wire exactly like a local one's, so persisting it in
        one place keeps the two identical.
        """
        reporter = ConsoleReporter(
            ref=job.work.ref, show_prefix=self._ctx.multi, agent_state=self._state
        )
        response = self._environment.run(job, reporter=reporter, cancel=cancel)

        if self._ctx.store is not None and response.session_id:
            self._ctx.store.set(job.work.task_id, response.session_id)

        return response

    def _finish(self, job: Job, response: Response) -> Response:
        """Verify what a run produced, deliver it, apply any decision, and
        tell the board what happened — deliverables before decisions (a
        hand-off usually refers to what was just delivered), and only for a
        run that actually finished: a crashed or aborted run has no outputs
        to check.

        A required sink's delivery failing cancels the decisions and fails
        the run (the task stays where it was); a best-effort sink's failure
        is only reported. Returns
        the response to release with — a structurally invalid one, or one a
        required sink refused, is downgraded to failed, so the board learns
        why rather than seeing a bare "done".

        ``apply``/``finish`` are board calls and so can raise (a 500, a
        dropped connection) — caught and logged rather than left to propagate,
        exactly like ``_safe_release`` below: this method's return value is
        an argument to that call, so letting an exception escape here would
        skip the release entirely, stranding the claim and (on the no-pool
        path) killing the poll thread. Reporting the outcome is best-effort;
        releasing the claim is not.
        """
        work = job.work

        # A run that ended badly has no outputs to verify and nothing to
        # deliver — but it still has something to say. The board is where a
        # person finds out what happened, and a run that failed before its
        # agent ever launched (a clone that could not authenticate, a
        # workspace that would not prepare) produced no agent comment at all,
        # so without this the task simply goes quiet.
        if response.status != "done":
            self._report(work, response, [])
            return response

        # Against the job's own permits, not the source's alone: the run was
        # launched being told it may report exactly these, so that is what it
        # is held to.
        problems = verify(response, job.permits)
        if problems:
            return replace(response, status="failed", result_text="; ".join(problems))

        try:
            results = run_pipeline.deliver_all(work, response, self._project, sinks=self._sinks)
            if run_pipeline.required_failed(results, self._sinks):
                response = replace(
                    response, status="failed", result_text="a required sink failed to deliver"
                )
            else:
                for decision in response.decisions:
                    self._source.apply(work, decision)
            self._report(work, response, results)
        except Exception:  # noqa: BLE001 — reporting is best-effort; releasing is not
            logger.warning("failed to deliver outcome for %s", work.ref, exc_info=True)
        return response

    def _report(self, work: WorkItem, response: Response, results: list[SinkResult]) -> None:
        """Tell the board how a run went.

        Best-effort, like every other board call on the way out (see
        :meth:`_finish`): reporting the outcome must never cost the release
        that frees the claim, so a board that refuses the comment is logged
        and no more.
        """
        try:
            self._source.finish(work, response, results)
        except Exception:  # noqa: BLE001 — reporting is best-effort; releasing is not
            logger.warning("failed to report outcome for %s", work.ref, exc_info=True)

    def _run_claimed(self, work: WorkItem, claim: Claim) -> None:
        """Run one claimed work item to completion and release its claim.

        Called synchronously on the poll thread when there is no pool, and on a
        pool worker otherwise. Works uniformly whether or not this source's
        claim locks anything — `release` itself decides whether there is
        anything to tell the board.

        The check-and-register below is one atomic step under ``_cancels_lock``
        — the same lock ``stop()`` holds while snapshotting — so work that
        reaches a worker only after ``stop()`` has run sees ``_stop`` set and
        bails without starting, releasing the run rather than stranding it.
        """
        run_id = claim.token
        cancel = threading.Event()
        # Fall back to a key that is still unique per run so it never collides
        # in the shared registry (a mention with no responding run has none).
        cancel_key = run_id or f"unclaimed-{id(cancel)}"
        with self._cancels_lock:
            already_stopped = self._stop.is_set()
            if not already_stopped:
                self._active_cancels[cancel_key] = cancel

        if already_stopped:
            self._safe_release(claim, Response(status="failed", result_text="listener stopped"))
            return

        self._state.set_phase("working", work.ref)
        try:
            try:
                # Raises `RepoMismatch` when the board's repository for this
                # task is not the one this connection is configured for — the
                # except below turns that into a failed run the board is told
                # about, before any workspace is prepared.
                job = job_for(work, self._wiring, run_id=run_id)
            except Exception as exc:  # noqa: BLE001 - a claim must never be stranded
                logger.exception("could not build a job for %s", work.ref)
                failed = Response(status="failed", result_text=f"could not prepare the run: {exc}")
                self._report(work, failed, [])
                self._safe_release(claim, failed)
                return
            response = self._execute(job, cancel)
            self._safe_release(claim, self._finish(job, response))
        finally:
            self._unregister_cancel(cancel_key)
            self._record_idle()


class Supervisor:
    """Owns per-connection listener threads and reconciles them when the config
    file changes — so ``issuebot connect`` while ``listen`` is running is picked
    up without a restart.

    The ``_watch`` daemon loop polls the config file's mtime every
    ``poll_interval`` seconds. On a change it calls ``load_config`` and
    ``_reconcile``, which starts a listener (and calls ``api.connect``) for each
    new connection and stops one (and calls ``api.disconnect``) for each removed
    connection. Unchanged connections keep running untouched.

    Publish and command-loop daemon threads are started once in ``start()``,
    parallel to the watch loop. The publish thread gathers one snapshot batch
    each tick via ``connection_snapshots()`` and hands the same batch to every
    publisher — the local status file and server telemetry. ``stop()`` signals
    all three loops and tears down every listener.
    """

    @classmethod
    def from_config(
        cls, cfg: Config, api: SourceClient, harness: Harness, *, names: list[str] | None = None
    ) -> Supervisor:
        """A Supervisor wired from a loaded config, watching the config's own path.

        Which of these settings live where — the update command on the config
        root, the session store behind the harness — is the config module's
        business and this class's, not the CLI's, so the assembly lives here.

        It reads no *plugin's* settings. What it needs of the source — the
        telemetry interval, the install's name — is declared on
        :class:`~issuebot.plugins.sources.base.SourceClient`: the client reads
        its own table and answers for itself, exactly as `plugin_settings`
        hands each sink and workspace its own.

        The identity reported is the distribution release version, the same
        value the remote execution protocol aligns and verifies.
        """
        from issuebot import __version__, sessions
        from issuebot.config import default_config_path

        return cls(
            api,
            harness,
            default_config_path(),
            store=sessions.store_for(cfg, harness),
            telemetry_interval=api.telemetry_interval,
            version=__version__,
            names=names or None,
            install_path=install_store.default_install_path(),
        )

    def __init__(
        self,
        api: SourceClient,
        harness: Harness,
        config_path: Path | str,
        *,
        store: SessionStore | None = None,
        poll_interval: float = 2.0,
        telemetry_interval: float = 15.0,
        version: str = "",
        names: list[str] | None = None,
        status_store: StatusStore | None = None,
        install_path: Path | None = None,
        agent_path: Path | None = None,
    ) -> None:
        self._api = api
        self._harness = harness
        self._path = Path(config_path)
        # The session store outlives any one config load, so it is passed in
        # rather than rebuilt per reload; everything else the runs need is
        # derived from the config in `_reconcile`.
        self._store = store
        self._poll = poll_interval
        self._telemetry_interval = telemetry_interval
        # The one in-flight telemetry POST (see `_publish`): a new one is only
        # started once the previous has returned, so a hung server drops
        # telemetry ticks instead of backing the publish loop up behind it.
        self._telemetry_thread: threading.Thread | None = None
        self._version = version
        self._status_store = status_store or StatusStore(default_status_path())
        # Resolved in start(); None until then (telemetry reports it as unknown).
        self._hostname: str | None = None

        # Optional name filter: when set, only connections with these names are
        # managed. Allows ``issuebot listen p1 p2`` to scope hot-reload too.
        self._names: set[str] | None = set(names) if names else None

        # Install id persistence: path where the source-minted id is stored.
        # What this install is *called* is the client's own (see
        # `SourceClient.register_install`).
        self._install_path: Path = install_path or install_store.default_install_path()
        # Populated in start() after registration (or reuse).
        self._install_id: str | None = None

        # Agent-id cache: the runner's own user id, learned from the connect()
        # response and persisted here so a restart knows who it is without a
        # GET /me round-trip. Loaded in start(); refreshed in _reconcile.
        self._agent_path: Path = agent_path or install_store.default_agent_path()

        # The runner-wide concurrency cap, shared by every listener so
        # `max_concurrent` counts tasks in flight across all of them rather than
        # per connection. Built on the first reconcile, where the config that
        # names the number is in hand.
        self._slots: threading.BoundedSemaphore | None = None
        self._slot_count = 0
        # How many of those slots `hold` is currently sitting on, so `resume`
        # gives back exactly what it took (a BoundedSemaphore raises otherwise).
        self._held = 0

        # Per-connection listener state, keyed by connection name.
        self._listeners: dict[str, ProjectListener] = {}
        # Board id per connection name — needed for api.disconnect on removal.
        self._boards: dict[str, str] = {}
        # The Connection each running listener was started with, so _reconcile can
        # tell an edited connection from an untouched one (a listener holds its
        # Connection by value; it can't pick up an edit in place).
        self._conns: dict[str, Connection] = {}
        # Per-connection log handler routing that listener's thread's runner log
        # lines into its own state tail; removed when the listener stops.
        self._log_handlers: dict[str, LogTailHandler] = {}
        # Guards concurrent mutations of _listeners / _boards from _reconcile
        # against reads in active_boards() and stop() (dict-changed-size race).
        self._lock = threading.Lock()

        self._stop = threading.Event()
        self._mtime = 0.0
        self._agent_id: str | None = None
        # The most recently loaded config, refreshed on every hot-reload in
        # `_watch`. `_reconcile` derives a RunnerContext from it per listener.
        self._cfg: Config | None = None

    # -------------------------------------------------------------------------
    # Public interface
    # -------------------------------------------------------------------------

    def active_boards(self) -> set[str]:
        """Return the board ids of all currently running listeners."""
        with self._lock:
            return {self._boards[name] for name in self._listeners}

    def connection_snapshots(self) -> list[ConnectionSnapshot]:
        """One :class:`~issuebot.agent_state.ConnectionSnapshot` per live
        listener — the batch every publisher receives each tick.

        Reads the listener set under the lock, then snapshots outside it so a
        slow state lock never blocks reconcile.
        """
        with self._lock:
            listeners = list(self._listeners.values())
        return [listener.snapshot() for listener in listeners]

    # -- publishing ----------------------------------------------------------

    def _publish_status(self, snapshots: list[ConnectionSnapshot]) -> None:
        """Mirror the batch to the local status file for ``issuebot status``.

        On shutdown the file is left in place; it simply ages into "stale".
        """
        payload = build_payload(
            snapshots,
            version=self._version,
            interval=self._telemetry_interval,
            now=datetime.now(UTC),
            pid=os.getpid(),
        )
        self._status_store.write(payload)

    def _publish_telemetry(self, snapshots: list[ConnectionSnapshot]) -> None:
        """Report the batch to the server, which translates it to its own wire
        format inside the source client.

        Skipped while ``install_id`` is None — the install is not yet
        registered with the dashboard, so there is nothing to report to.
        """
        if self._install_id is None:
            return
        self._api.report_telemetry(
            version=self._version,
            install_id=self._install_id,
            hostname=self._hostname,
            connections=snapshots,
        )

    def _safe_publish(
        self,
        publish: Callable[[list[ConnectionSnapshot]], None],
        snapshots: list[ConnectionSnapshot],
    ) -> None:
        """Run one publisher over the batch, logging instead of raising.

        Best-effort per publisher: a failure is logged and the others still
        receive the batch, so a flaky server never starves the local status
        file (or the other way round). Publishing never breaks the runner."""
        try:
            publish(snapshots)
        except Exception:  # noqa: BLE001 — publishing is best-effort
            name = getattr(publish, "__name__", "publisher")
            logger.warning("%s failed", name, exc_info=True)

    def _publish(self) -> None:
        """One tick: gather one snapshot batch and hand the same batch to every
        publisher.

        The status file is written first, on this thread: `issuebot status`
        calls the file stale at 45s while the board client allows 30s per HTTP
        phase, so local freshness must never wait on the network. Telemetry
        goes out on its own thread and is *skipped* while the previous POST is
        still in flight — the least machinery that bounds the coupling: a hung
        server then costs dropped telemetry ticks, never a stale status file
        (a queue or a second timed loop would buy nothing more).
        """
        snapshots = self.connection_snapshots()
        self._safe_publish(self._publish_status, snapshots)

        if self._telemetry_thread is None or not self._telemetry_thread.is_alive():
            self._telemetry_thread = threading.Thread(
                target=self._safe_publish,
                args=(self._publish_telemetry, snapshots),
                daemon=True,
                name="telemetry",
            )
            self._telemetry_thread.start()

    def _publish_loop(self) -> None:
        """Publish immediately, then on every telemetry interval until stopped.

        The whole tick is guarded: this daemon thread is started exactly once,
        so an exception anywhere in it — gathering the snapshots included, not
        just the per-publisher deliveries `_publish` already guards — would
        otherwise silence status.json and telemetry for the process lifetime.
        """
        while not self._stop.is_set():
            try:
                self._publish()
            except Exception:  # noqa: BLE001 — one bad tick must not kill the loop
                logger.warning("publish tick failed", exc_info=True)
            self._stop.wait(self._telemetry_interval)

    def hold(self, timeout: float) -> bool:
        """Stop new work being claimed, and wait for what is in flight to finish.

        Every run holds one of :attr:`_slots` from before its claim until after
        its release, so taking all of them is both halves of a drain at once:
        with every permit held, no listener can claim anything, and the last
        permit only comes free when the last run has released its claim. That
        is why this needs no cooperation from the listeners and — unlike
        stopping them — can be undone: :meth:`resume` hands the permits back and
        the poll loops, which never stopped, carry straight on.

        A listener already *waiting* on a slot for work in hand races this for
        each freed permit, and can win — extending the drain by that run. The
        timeout bounds it: a drain that keeps losing races simply times out and
        the update is refused, which is the safe answer either way.

        Returns True when everything came free inside ``timeout``. On a timeout
        it keeps whatever it did get (so the drain is still as complete as it
        could be) and answers False, leaving the caller to decide whether to go
        ahead — an update that waits forever is an update that never lands on a
        busy runner.

        Called from the command thread only, which is why ``_held`` needs no
        lock of its own.
        """
        if self._slots is None:
            return True

        deadline = time.monotonic() + timeout
        while self._held < self._slot_count:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not self._slots.acquire(timeout=remaining):
                return False
            self._held += 1

        return True

    def resume(self) -> None:
        """Give back every slot :meth:`hold` took, so work flows again."""
        if self._slots is None:
            return

        while self._held:
            self._held -= 1
            self._slots.release()

    def stop(self) -> None:
        """Signal the watch loop and all listener threads to stop."""
        self._stop.set()
        with self._lock:
            listeners = list(self._listeners.values())
            handlers = list(self._log_handlers.values())
            self._log_handlers.clear()
        # Detach each listener's log handler from the shared issuebot logger so it
        # doesn't leak across runs.
        issuebot_logger = logging.getLogger("issuebot")
        for handler in handlers:
            issuebot_logger.removeHandler(handler)
        for listener in listeners:
            listener.stop()

    def start(self) -> None:
        """Spawn the watch, telemetry, and command daemon threads.

        The watch thread handles the initial config load (mtime starts at 0.0,
        so the very first poll detects a change and calls ``_reconcile``).
        """
        # Lower the issuebot logger to INFO so runner activity (claiming,
        # listening, retries) reaches the dashboard log tail. The tail is fed
        # per-connection: each listener attaches its own LogTailHandler (with a
        # thread-name filter) in _reconcile, so logs route to the right board.
        logging.getLogger("issuebot").setLevel(logging.INFO)

        # Resolve this machine's hostname once so it can be sent with registration.
        self._hostname = socket.gethostname()

        # Register-or-reuse: if we have a persisted install id, use it; otherwise
        # mint a new one via the Parade server. A failure here is non-fatal — the
        # runner starts anyway, but install_id stays None for this run (telemetry
        # skips reporting and no per-install commands arrive). Registration is only
        # re-attempted on the next process start, not in-process.
        self._install_id = install_store.load_install_id(self._install_path)
        if self._install_id is None:
            try:
                self._install_id = self._api.register_install(self._hostname)
                install_store.save_install_id(self._install_path, self._install_id)
                logger.info("registered install %s", self._install_id)
            except Exception:  # noqa: BLE001
                logger.warning(
                    "install registration failed; telemetry disabled until restart",
                    exc_info=True,
                )

        # Load the agent's own user id from the local cache so mention sessions
        # can self-assign. The id is learned from the connect() response (which
        # the board resolves from the PAT) in _reconcile and persisted here, so a
        # restart already knows it without any GET /me round-trip. On the very
        # first run the cache is empty and the id is filled in when the first
        # board connects. If it never resolves, _agent_id stays None (degraded
        # mode: mentions can still reply, they just can't self-assign).
        self._agent_id = install_store.load_agent_id(self._agent_path)
        if self._agent_id is not None:
            logger.info("agent identity loaded from cache: %s", self._agent_id)

        # Watch the config file for connection additions and removals.
        threading.Thread(target=self._watch, daemon=True, name="supervisor").start()

        # One publish loop for both views of the live state: each tick gathers
        # one snapshot batch and hands it to the status file and to server
        # telemetry, so `issuebot status` and the dashboard cannot disagree.
        # Reads listeners live, so connections added by hot-reload show up on
        # the next tick without a restart.
        threading.Thread(target=self._publish_loop, daemon=True, name="publish").start()

        # The command loop runs for the whole session as a daemon thread.
        threading.Thread(
            target=run_command_loop,
            kwargs={
                "client": self._api,
                "stop": self._stop,
                # Pass the Supervisor itself as the stoppable — on a restart or
                # update command it calls stop() which tears down all listeners.
                "listeners": [self],
                "install_id": self._install_id,
            },
            daemon=True,
            name="commands",
        ).start()

    # -------------------------------------------------------------------------
    # Internal implementation
    # -------------------------------------------------------------------------

    def _remember_agent_id(self, connect_result: dict[str, Any] | None) -> None:
        """Cache and persist the agent's own user id from a connect() response.

        The board's connect endpoint resolves the calling agent from the PAT and
        echoes its identity, so the runner learns its user id from a call it
        already makes — no separate GET /me. Persisting it means a restart (where
        connect returns 409 with no body) still knows the id from the local cache.

        A no-op when the response carries no identity (e.g. an older server, or a
        409 caught upstream) or when the id is already known.
        """
        agent = (connect_result or {}).get("agent")
        agent_id = agent.get("id") if isinstance(agent, dict) else None
        if not agent_id or agent_id == self._agent_id:
            return

        self._agent_id = agent_id
        try:
            install_store.save_agent_id(self._agent_path, agent_id)
        except OSError:
            # Persistence is best-effort — a failed write just means the next run
            # re-learns the id from connect(). Don't disrupt startup.
            logger.warning("could not persist agent id", exc_info=True)
        logger.info("agent identity resolved from connect: %s", agent_id)

    def _resize_slots(self, wanted: int) -> None:
        """Make sure the shared concurrency cap exists, and say so if it moved.

        A semaphore's count cannot be changed while runs are holding permits
        against it, and swapping in a fresh one would let the in-flight runs and
        the new arrivals be capped by different objects — briefly running more
        at once than either number allows. So a changed ``max_concurrent`` is
        reported rather than applied: every other setting a hot reload picks up
        belongs to one connection, and this is the only one that belongs to the
        process.
        """
        if self._slots is None:
            self._slots = threading.BoundedSemaphore(wanted)
            self._slot_count = wanted
            return

        if wanted != self._slot_count:
            logger.warning(
                "max_concurrent is now %d but this runner is still capped at %d; "
                "restart `issuebot listen` to apply it",
                wanted,
                self._slot_count,
            )

    def _stop_departed(self, want: dict[str, Connection]) -> None:
        """Stop every listener whose connection has left the config or been edited.

        The listener set is snapshotted under the lock, then the slow stop/API
        calls run outside it so the lock is never held across network I/O. A
        removal also disconnects server-side; an edit does not, because the start
        loop reconnects it moments later and churning the board connection would
        be pure noise.
        """
        with self._lock:
            departed = []
            for name in list(self._listeners):
                wanted = want.get(name)
                if wanted is not None and wanted == self._conns.get(name):
                    continue  # untouched — leave it running
                self._conns.pop(name, None)
                departed.append(
                    (name, self._listeners.pop(name), self._boards.pop(name), wanted is None)
                )

        for name, listener, board, gone in departed:
            listener.stop()
            # Detach this listener's per-connection log handler.
            handler = self._log_handlers.pop(name, None)
            if handler is not None:
                logging.getLogger("issuebot").removeHandler(handler)
            if not gone:
                logger.info("connection %s changed; restarting its listener", name)
                continue
            try:
                self._api.disconnect(board)
            except Exception:  # noqa: BLE001 — server failure must not block local cleanup
                logger.warning("server disconnect failed for board %s", board, exc_info=True)

    def _server_connect(self, conn: Connection) -> None:
        """Register a connection server-side before its listener starts.

        Best-effort: a transient failure still starts the listener (the board may
        already be registered). A 409 means the state we want, and still conveys
        our identity — capture it, or a runner reconnecting to a durable board
        connection could never resolve its agent id.
        """
        board = conn_setting(conn, "board")
        try:
            self._remember_agent_id(
                self._api.connect(board, conn.name, install_id=self._install_id)
            )
        except ConnectionConflict as conflict:
            if conflict.agent_id:
                self._remember_agent_id({"agent": {"id": conflict.agent_id}})
        except Exception:  # noqa: BLE001 — board may already be registered
            logger.warning(
                "server connect failed for board %s; starting listener anyway",
                board,
                exc_info=True,
            )

    def _reconcile(self, cfg: Config) -> None:
        """Start new listeners, stop removed ones, restart edited ones; leave
        genuinely unchanged ones running.

        A listener holds its ``Connection`` by value, so an edited connection —
        which is what ``issuebot connect --name <existing>`` writes — can only
        take effect by restarting that listener; comparing names alone would let
        it keep serving the stale settings until the next manual restart. The
        stop drops it out of ``_listeners``, so the start loop below picks it
        straight back up with the new settings (and, like a removal, aborts
        whatever that listener was running).

        Best-effort on both sides: a server ``connect`` failure still starts the
        listener (the board may already be registered), and a server ``disconnect``
        failure still stops the listener locally.
        """
        # Apply the optional name filter.
        connections = cfg.connections
        if self._names is not None:
            connections = [c for c in connections if c.name in self._names]

        want = {c.key: c for c in connections}
        multi = len(want) > 1

        self._stop_departed(want)
        self._resize_slots(cfg.max_concurrent)

        # Start listeners for newly added connections.
        for name, conn in want.items():
            # Read under lock to check membership; add under lock after creation.
            with self._lock:
                already_running = name in self._listeners
            if already_running:
                continue

            self._server_connect(conn)

            # One context per listener: the config's runner settings, plus this
            # connection's own live state and whether it shares the console.
            ctx = RunnerContext.from_config(
                cfg,
                store=self._store,
                multi=multi,
                state=AgentState(),
                agent_id=self._agent_id,
            )

            # The connection's whole run machinery, assembled in one place.
            # `wire` copies the connection, so the instance stored in `_conns`
            # below stays exactly what the config file said — a repo sync
            # during a run must not make an unchanged file read as an edit.
            wiring = wire(self._api, self._harness, conn, ctx, install_id=self._install_id)

            listener = ProjectListener(
                wiring,
                max_concurrent=cfg.max_concurrent,
                slots=self._slots,
            )

            # Route this listener's thread's runner log lines into its own tail.
            thread_name = f"listen-{name}"
            handler = LogTailHandler(listener.state)
            handler.addFilter(_ThreadFilter(thread_name))
            logging.getLogger("issuebot").addHandler(handler)

            t = threading.Thread(target=listener.run, daemon=True, name=thread_name)
            with self._lock:
                self._listeners[name] = listener
                self._boards[name] = conn_setting(conn, "board")
                self._conns[name] = conn
                self._log_handlers[name] = handler
            t.start()
            logger.info("started listener for board %s (%s)", conn_setting(conn, "board"), name)

    def _watch(self) -> None:
        """Poll the config file's mtime and call ``_reconcile`` on each change.

        The initial mtime is 0.0 so the first iteration always triggers a load,
        starting the initial set of listeners without a separate init call.

        Nothing gets out of here. A config the user has just broken — a typo, a
        plugin this build does not have, TOML that no longer parses — is exactly
        what ``load_config`` raises over, and an escaping exception kills this
        daemon thread for the life of the process: hot reload would stop
        silently, and fixing the file would never bring it back. So a bad edit
        is reported and the connections that are already running keep running,
        which is the same "the file is wrong, go and look at it" answer every
        command that reads the config gives.
        """
        while not self._stop.is_set():
            try:
                mtime = self._path.stat().st_mtime
                if mtime != self._mtime:
                    self._mtime = mtime
                    cfg = load_config(self._path)
                    if cfg is not None:
                        self._cfg = cfg
                        self._reconcile(cfg)
            except FileNotFoundError:
                # Config file removed or not yet written — ignore and retry.
                pass
            except Exception as problem:  # noqa: BLE001 — see the docstring
                # Once per edit, not once per poll: the mtime is taken above
                # before the load, so a file left broken does not repeat this.
                logger.warning(
                    "config at %s could not be loaded, so the running connections are "
                    "unchanged:\n%s",
                    self._path,
                    problem,
                )
            self._stop.wait(self._poll)
