"""Running one already-claimed task inside a sandbox — the other end of the wire.

The counterpart to the controller in :mod:`issuebot.sandbox`: it booted a
throwaway machine and exec'd ``issuebot run-one`` in it; this is what runs
there.

What it runs is the environment that runs work *in this process*. That is not a
curiosity — inside the sandbox there is no sandbox, and a run here must produce
exactly what the same work would have produced on the developer's own machine.
So this module rebuilds, from the wire and the board, the same
:class:`~issuebot.runner.Wiring` the controller's listener holds — through the
same :func:`~issuebot.runner.wire` call — and then lets that environment run it.

Which environment that is, this module does not know: it asks
:func:`~issuebot.runner.in_process_environment` for whichever installed plugin
declares ``runs_in_process``.

The ``run-one`` command itself only parses arguments and sets an exit code
(ADR-0006).

This module does NOT claim or release the run: the controller claimed it before
the sandbox existed and holds the lock until the outcome comes back. Nothing here
outlives the sandbox.
"""

from __future__ import annotations

import logging
from typing import get_args

import issuebot
from issuebot import runner
from issuebot.config import Config, Connection, harness_for, source_plugin
from issuebot.context import RunnerContext
from issuebot.contracts import Response, WorkItem, WorkKind
from issuebot.plugins.harnesses.base import Harness
from issuebot.plugins.sources.base import SourceClient
from issuebot.reporter import ConsoleReporter
from issuebot.sandbox_protocol import BootMode, RunResult, WorkerEnv, write_result_file
from issuebot.sessions import SessionStore, default_state_path

# Every kind of work `run_one` will accept — mirrors `contracts.WorkKind`.
_KNOWN_KINDS: tuple[str, ...] = get_args(WorkKind)

logger = logging.getLogger("issuebot")


class UnknownWork(ValueError):
    """The worker was asked for a connection or a kind of work it does not have."""


def run_work(
    client: SourceClient,
    harness: Harness,
    connection: Connection,
    work: WorkItem,
    *,
    run_id: str,
    ctx: RunnerContext,
    reporter: ConsoleReporter,
) -> Response:
    """Run one work item here, exactly as the controller would have run it locally.

    The same :func:`runner.wire` the controller's listener uses, so "what may
    this run report", "which prompt does it launch with" and "where does its
    working copy come from" are answered by the same assembly on both sides of
    the wire. The one difference is *which* environment: the connection's own
    choice is the sandbox this process is already inside, so
    :func:`runner.in_process_environment` overrides it.
    """
    # `wire` makes the repo sync itself, before it selects the workspace — so
    # the sandbox, which rebuilds its wiring fresh from the config it booted
    # with, makes that check here rather than trust the controller made it.
    wiring = runner.wire(
        client,
        harness,
        connection,
        ctx,
        environment_name=runner.in_process_environment(),
    )

    job = runner.job_for(work, wiring, run_id=run_id)

    environment = wiring.environment
    assert environment is not None, "wire() builds the environment"
    return environment.run(job, reporter=reporter)


def session_store(harness: Harness) -> SessionStore | None:
    """The per-task session store a sandbox run always gets, for a harness that
    can reopen a conversation.

    Deliberately not gated on the harness's ``resume_sessions`` setting, which
    is a knob for the local case. Inside a sandbox, session capture is always
    wanted: the pause-and-resume ladder only reopens the prior conversation if
    the run that paused captured its session id into the checkpointed filesystem
    for the resuming run to load. None for a harness with no session concept at
    all (``resumes_sessions`` unset), which degrades to a workspace-only restore
    rather than crashing.
    """
    return SessionStore(default_state_path()) if harness.resumes_sessions else None


def run_one(
    cfg: Config,
    *,
    task_id: str,
    run_id: str,
    connection_name: str,
    kind: str,
    env: WorkerEnv | None = None,
) -> Response:
    """Run one already-claimed task to completion in this process.

    Raises :class:`UnknownWork` when the connection or the kind of work is not
    one this runner has — the caller turns that into a usage error. Everything
    after that point comes back as a :class:`~issuebot.contracts.Response`,
    because the controller on the other side of the wire needs an answer either
    way.
    """
    if kind not in _KNOWN_KINDS:
        raise UnknownWork(f"unknown work kind '{kind}' (known: {', '.join(_KNOWN_KINDS)})")

    connection = cfg.connection(connection_name)
    if connection is None:
        raise UnknownWork(f"unknown connection: {connection_name}")

    wire = env if env is not None else WorkerEnv.decode()

    # The last check before the work, and the only one made by the process that
    # will actually do it: the controller already aligned this sandbox, but it
    # did that by asking `issuebot` on the PATH, and *this* is what the PATH
    # resolved to. A mismatch means the alignment did not take, and running
    # anyway produces a wrong answer nobody can see.
    #
    # An empty wire is nobody having asked — a hand-run `run-one` — and is left
    # unchecked. Remote runs always carry the controller's released version.
    if wire.version and wire.version != issuebot.__version__:
        return Response(
            status="failed",
            result_text=(
                f"this sandbox is issuebot {issuebot.__version__} but the controller asked for "
                f"{wire.version}; refusing to work as the wrong version"
            ),
        )

    # The same handle the controller built on the other side of the wire, from
    # the same config — asked of the source rather than constructed here, so the
    # sandbox knows no more about which source this is than the controller does.
    client = source_plugin(connection.source).source.client(cfg)
    harness = harness_for(cfg)

    task = client.get_task(task_id)
    work = wire.work_item(task_id=task_id, reference=task.get("reference"), kind=kind)

    reporter = ConsoleReporter(ref=work.ref)
    ctx = RunnerContext.from_config(cfg, store=session_store(harness), agent_id=wire.agent_id)

    # A warm boot inherits a working copy some earlier task left in the project
    # checkpoint, and only the workspace that placed it knows how to bring it to
    # this task's ref — so it is asked, through the same factory `run_work` uses
    # a moment later. A workspace with nothing to top up (or a cold boot, or a
    # resume, whose copy is already this task's own branch mid-work) does
    # nothing: the ordinary prep is idempotent over both.
    if wire.boot is BootMode.WARM:
        workspace, _ = runner.workspace_for(connection, ctx)
        workspace.refresh(connection, work.ref, reporter=reporter)

    return run_work(client, harness, connection, work, run_id=run_id, ctx=ctx, reporter=reporter)


def report(response: Response) -> RunResult:
    """Publish the response on both channels the controller reads.

    A sentinel line on stdout, which the controller parses as it streams, and a
    file, which it falls back to when a run was cut short before the line was
    flushed. Returns the result so the caller can print it — writing to stdout is
    the command's job, not this module's."""
    result = RunResult.from_response(response)
    write_result_file(result)
    return result
