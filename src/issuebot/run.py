"""Running one Job to completion — the pipeline every execution environment shares.

Prepare the workspace, apply the repo's bootstrap, launch the harness (retrying
through a transient overload, and relaunching fresh once when a resumed session
never produced a result), derive what actually changed once the agent exits,
and report how the run ended. This is the local environment's whole body, and
the sandbox worker's too — one pipeline rather than two nearly-parallel copies.

Two things a caller hands in decide almost everything: ``job.permits`` (may
this run report ``changes``, and so must its workspace be sound before launch)
and the ``Workspace`` itself (where the run happens, and how to ask git, never
the agent, what moved). Board-facing messages are the source's job, driven by
the returned ``Response``, never a side effect of this module.

Any run permitted ``changes`` commits and pushes
(``workspace.commit_and_push``), gated only by git's own ``settings.push`` —
see ADR-0012.

``Response.outputs`` is filled in here: the agent writes its response document
to the path handed to it as ``$ISSUEBOT_RESPONSE``, outside the workspace so it
can never land in a commit, and ``_finish`` reads it back once the harness
exits. A missing or unparseable document fails the run outright — it means the
agent never finished reporting, a different state from a document that
deliberately says ``{"outputs": []}``.
"""

from __future__ import annotations

import logging
import shutil
import tempfile
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from issuebot import provision
from issuebot.agent_state import AgentState
from issuebot.config import conn_setting
from issuebot.contracts import Delivery, Response, SinkResult, parse_outputs
from issuebot.plugins.harnesses.base import Harness, LaunchResult, LaunchSpec
from issuebot.plugins.workspaces.base import Prepared, Workspace
from issuebot.process import REAL, Process
from issuebot.reporter import ConsoleReporter, Reporter
from issuebot.transient import describe_transient, is_transient

if TYPE_CHECKING:
    from pydantic import BaseModel

    from issuebot.config import Connection, SinkRef
    from issuebot.contracts import Job, WorkItem
    from issuebot.plugins.sinks.base import Sink
    from issuebot.plugins.sources.base import Source
    from issuebot.runner import Wiring

logger = logging.getLogger("issuebot")

# The environment variable naming the path the agent must write its structured
# response to. Set on every launch (not only ones permitted to make changes —
# "there is no read-only exception and no second output channel"), pointing
# outside any workspace so the document can never appear in a commit.
RESPONSE_ENV = "ISSUEBOT_RESPONSE"


def _overload_backoff(attempt: int) -> float:
    """Seconds to wait before the Nth overload retry: exponential from one
    minute, capped at ten (60, 120, 240, 480, 600, 600, ...)."""
    return min(60.0 * (2 ** (attempt - 1)), 600.0)


def heartbeat_loop(source: Source, run_id: str, interval: float, stop: threading.Event) -> None:
    """Heartbeat the run every ``interval`` seconds until ``stop`` is set,
    through :meth:`~issuebot.plugins.sources.base.Source.heartbeat` — the one
    thing `execute` asks of a source while a run is in flight. A failed
    heartbeat is logged and retried — it must never crash the supervisor."""
    while not stop.wait(interval):
        try:
            source.heartbeat(run_id)
        except Exception as exc:  # noqa: BLE001
            if is_transient(exc):
                logger.info("heartbeat for run %s deferred (%s)", run_id, describe_transient(exc))
            else:
                logger.warning("heartbeat failed for run %s", run_id, exc_info=True)


# ---------------------------------------------------------------------------
# Preparing the workspace
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Ready:
    """Everything :func:`_prepare` resolves: where the agent launches, and the
    repo's bootstrap result, ready to fold into the launch spec."""

    prepared: Prepared
    prov: provision.ProvisionResult


def _prepare(
    job: Job,
    workspace: Workspace,
    connection: Connection,
    settings: BaseModel,
    proc: Process,
    rep: Reporter,
) -> _Ready | Response:
    """Cut (or reuse) the task's working copy and apply the repo's bootstrap.

    Returns what the launch needs, or a failed :class:`Response` when the agent
    must not run. How hard a failure lands is ``job.permits``' call: work that
    may report ``changes`` must never launch against a broken workspace, while
    a run that can only answer degrades to the connection's own folder and
    still answers from there.

    That degrade needs somewhere to degrade *to*. A clone-based or sandboxed
    connection keeps no folder on this machine, and falling back to the
    process's own working directory would launch the agent — with its editing
    and shell tools and no permission prompts — in somebody's home directory
    or an unrelated checkout. So a folderless connection fails the run instead.

    A condition the run can proceed through — a diverged branch — is not a
    failure and never lands in the ``except`` below: the workspace reports it
    as ``Prepared.problem`` (:class:`~issuebot.plugins.workspaces.base.
    WorkspaceProblem`), and :func:`execute` routes it back through
    ``Source.prompt`` so the agent is told to reconcile before working. This
    function stays workspace-agnostic either way — the problem is data, not a
    git-specific branch here.
    """
    changes_permitted = "changes" in job.permits

    try:
        prepared = workspace.prepare(connection, job.work.ref, settings=settings, proc=proc)
    except Exception:  # noqa: BLE001 - any workspace failure is this run's prep failure
        logger.exception("workspace prep failed for %s", job.work.ref)
        if changes_permitted:
            return Response(status="failed", result_text="workspace prep failed")
        if not job.folder:
            logger.warning("no folder to fall back to for %s", job.work.ref)
            return Response(
                status="failed",
                result_text="workspace prep failed and this connection keeps no folder to run in",
            )
        logger.warning("falling back to the project folder for %s", job.work.ref)
        prepared = Prepared(folder=job.folder)

    try:
        prov = provision.provision(prepared.folder, reporter=rep)
    except Exception:  # noqa: BLE001 - surface any bootstrap failure
        logger.exception("bootstrap failed for %s", job.work.ref)
        if changes_permitted:
            return Response(status="failed", result_text="bootstrap failed")
        prov = provision.ProvisionResult()

    return _Ready(prepared=prepared, prov=prov)


# ---------------------------------------------------------------------------
# Launching
# ---------------------------------------------------------------------------


def _launch_with_retries(
    harness: Harness,
    spec: LaunchSpec,
    rep: Reporter,
    cancel: threading.Event,
    *,
    state: AgentState,
    reference: str,
    max_overload_retries: int,
    overload_backoff: Callable[[int], float],
) -> LaunchResult:
    """Launch the agent, retrying where a retry is the right answer.

    Two retry paths, both bounded so nothing can loop forever: a transient API
    overload backs off and resumes the same session, and a resume that failed
    without yielding a session id is dropped and relaunched fresh, once. An
    abort always wins over any retry decision.

    Session persistence is the caller's business (via ``Response.session_id``)
    — this loop only threads the id between attempts of the same run, it does
    not write it anywhere.
    """
    attempt = 0
    relaunched_fresh = False
    while True:
        result = harness.launch(spec, rep, cancel)

        if cancel.is_set():
            return result

        if result.retryable and result.exit_code != 0 and attempt < max_overload_retries:
            attempt += 1
            delay = overload_backoff(attempt)
            state.set_phase("blocked")
            logger.info(
                "overloaded on %s; resuming in %.0fs (retry %d/%d)",
                reference,
                delay,
                attempt,
                max_overload_retries,
            )
            spec = replace(spec, resume_session_id=result.session_id or spec.resume_session_id)
            if cancel.wait(delay):
                return result
            # The wait is over and the agent is about to run again: 'blocked'
            # described the backoff, not the retry.
            state.set_phase("working")
            continue

        if (
            not relaunched_fresh
            and spec.resume_session_id is not None
            and result.session_id is None
            and result.exit_code != 0
        ):
            logger.info(
                "resume of session %s for %s failed; relaunching fresh",
                spec.resume_session_id,
                reference,
            )
            relaunched_fresh = True
            spec = replace(spec, resume_session_id=None)
            continue

        return result


def _classify(
    result: LaunchResult,
    cancel: threading.Event,
    *,
    elapsed: float,
    timeout_minutes: int | None,
) -> Literal["done", "failed", "aborted", "timed out"]:
    """How a finished launch ended.

    An abort wins over the exit code, since a terminated child may exit
    non-zero anyway; a timeout is told apart from a Ctrl-C by whether the hard
    limit was actually reached."""
    if cancel.is_set():
        return "timed out" if (timeout_minutes and elapsed >= timeout_minutes * 60) else "aborted"
    return "failed" if result.exit_code != 0 else "done"


# ---------------------------------------------------------------------------
# Finishing
# ---------------------------------------------------------------------------


def _finish(
    job: Job,
    workspace: Workspace,
    prepared: Prepared,
    settings: BaseModel,
    proc: Process,
    result: LaunchResult,
    response_path: str,
) -> Response:
    """Finish a run that exited cleanly.

    Reads the agent's response document first: a missing or unparseable one
    fails the run regardless of what git or the harness reported, because it
    means the agent never finished telling us what it produced. Only past that
    gate does ``changes`` get derived from git — never from the agent — and
    only when ``job.permits`` actually allows this run to report them: a
    folder workspace cannot produce one at all, and discarding it for a
    throwaway or read-only run (a mention) is the point, not an oversight.

    Committing and pushing guarantees "an unexpected failure raises", not "a
    rejected push fails the run": ``GitWorkspace.commit_and_push`` itself
    reports a rejected push as ``Changes(pushed=False)`` rather than raising,
    so that case still ends ``done`` here.
    Only something ``commit_and_push`` did not already turn into data —
    ``proc`` erroring, git itself misbehaving — reaches this ``except`` and
    fails the run, because a ``done`` status with no ``Changes`` at all would
    be a worse answer than a plain failure.
    """
    try:
        raw = Path(response_path).read_text()
    except OSError:
        logger.warning("no response document at %s for %s", response_path, job.work.ref)
        return Response(
            status="failed",
            result_text="agent exited without writing a response",
            session_id=result.session_id,
        )

    try:
        outputs = parse_outputs(raw)
    except ValueError as exc:
        logger.warning("malformed response document for %s: %s", job.work.ref, exc)
        return Response(
            status="failed",
            result_text=f"malformed response document: {exc}",
            session_id=result.session_id,
        )

    if "changes" not in job.permits:
        return Response(status="done", outputs=outputs, session_id=result.session_id)

    try:
        changes = workspace.commit_and_push(prepared, job.work.ref, settings=settings, proc=proc)
    except Exception:  # noqa: BLE001 - surface any commit/push failure
        logger.exception("commit/push failed for %s", job.work.ref)
        return Response(
            status="failed", result_text="commit/push failed", session_id=result.session_id
        )

    return Response(status="done", changes=changes, outputs=outputs, session_id=result.session_id)


# ---------------------------------------------------------------------------
# Delivering
# ---------------------------------------------------------------------------


def deliver_all(
    work: WorkItem,
    response: Response,
    connection: Connection,
    *,
    sinks: Sequence[tuple[SinkRef, Sink]],
) -> list[SinkResult]:
    """Hand every deliverable output to every sink that accepts its kind, over
    every sink the connection declares, in order.

    Deliverables run before any decision is applied — a decision usually
    refers to a deliverable ("reassign to Sam" means "…now that the PR
    exists"), so a decision must never be applied until every sink has had its
    turn. This function
    only delivers; whether a decision then goes ahead is what
    :func:`required_failed` is for — the caller's to ask, since only the
    caller (the source's own ``apply``) knows what a decision even is.

    Every sink gets a turn regardless of an earlier one's failure: a
    best-effort sink failing must never silently skip a later required one,
    or vice versa — the caller decides what a failure means only once every
    sink has actually been asked.

    A sink that raises is caught here and turned into an ordinary failed
    :class:`~issuebot.contracts.SinkResult` rather than let escape: "a
    required sink failing cancels the decisions" must be total. Without
    this, a *crashing* required sink would produce no result at all —
    ``required_failed`` would never see it, the decisions would go ahead
    unguarded, and the run would release as ``done`` with no sink result to
    show for it, which is exactly backwards.
    """
    # Where this connection's code lives, told to every sink alike. Both are
    # facts about the delivery, and a sink needs whichever its own tools can
    # use: `folder` is empty for a clone-based or sandboxed connection (the
    # controller keeps no checkout for one), which is exactly why `repo` is
    # carried too.
    repo = conn_setting(connection, "repo") or ""
    folder = connection.folder or ""

    results: list[SinkResult] = []
    for _, sink in sinks:
        for output in response.deliverables:
            if output.kind not in sink.accepts:
                continue
            delivery = Delivery(
                work=work,
                output=output,
                changes=response.changes,
                repo=repo,
                folder=folder,
            )
            try:
                results.append(sink.deliver(delivery))
            except Exception as exc:  # noqa: BLE001 - the required-sink rule must be total; see docstring
                logger.exception("sink '%s' crashed delivering to %s", sink.name, work.ref)
                results.append(SinkResult(sink=sink.name, ok=False, summary=f"crashed: {exc}"))
    return results


def required_failed(results: list[SinkResult], sinks: Sequence[tuple[SinkRef, Sink]]) -> bool:
    """True when a *required* sink's delivery failed.

    This cancels the run's decisions and fails the run, where a best-effort
    sink's own failure is merely reported and the run continues."""
    required = {ref.name for ref, _ in sinks if ref.required}
    return any(not r.ok and r.sink in required for r in results)


# ---------------------------------------------------------------------------
# The run
# ---------------------------------------------------------------------------


def execute(
    job: Job,
    wiring: Wiring,
    *,
    reporter: Reporter | None = None,
    cancel: threading.Event | None = None,
    proc: Process = REAL,
    max_overload_retries: int = 5,
    overload_backoff: Callable[[int], float] = _overload_backoff,
) -> Response:
    """Run one :class:`Job` to completion and report how it went.

    ``wiring`` is the connection's assembled run machinery
    (:class:`~issuebot.runner.Wiring`): the harness that does the work, the
    workspace ``prepare``/``commit_and_push`` drive, the live ``Connection``
    and workspace settings those two need (``job.folder`` is only the
    answer-only fallback's location), and the source the heartbeat keeps the
    run alive on. The heartbeat interval and the live state come off
    ``wiring.ctx``; the run to heartbeat is ``job.run_id``.

    Every MCP server the launch gets arrives on the job or the repo: the
    source's own (``job.mcp_servers``, from `Source.agent_access` — the board
    channel included) and whatever the repo's bootstrap declares. They are
    merged here, source last, so a repo cannot displace the agent's channel to
    its board with a same-named server.

    Does NOT release the run, claim it, or touch a session store — the caller
    does, from the returned :class:`Response`, so one run-lock lifecycle wraps
    a local run and a sandbox run identically and session persistence is not
    duplicated between them.
    """
    harness = wiring.harness
    workspace = wiring.workspace
    connection = wiring.connection
    settings = wiring.workspace_settings
    source = wiring.source
    run_id = job.run_id
    heartbeat_interval = wiring.ctx.heartbeat_interval

    state = wiring.ctx.state or AgentState()
    rep = reporter or ConsoleReporter(ref=job.work.ref, show_prefix=False, agent_state=state)

    ready = _prepare(job, workspace, connection, settings, proc, rep)
    if isinstance(ready, Response):
        return ready
    prepared, prov = ready.prepared, ready.prov

    # A problem the workspace reported (a diverged branch) rides the prompt:
    # only the source knows how to phrase instructions to its agent, so the
    # prompt is re-rendered through the same `Source.prompt` that built
    # `job.prompt`, now with the problem to weave in. Only for a run that may
    # report `changes` — one that may not never commits or pushes, so there is
    # nothing for its agent to reconcile. Rendered before the response dir
    # below exists, so a prompt that raises cannot leak the directory.
    prompt = job.prompt
    if prepared.problem is not None and "changes" in job.permits:
        prompt = source.prompt(job.work, connection, permits=job.permits, problem=prepared.problem)

    # A fresh directory per run, outside any workspace (system temp, never the
    # prepared checkout), so the response document the agent writes can never
    # appear in a commit. Removed in the `finally` below once it has been read.
    response_dir = tempfile.mkdtemp(prefix="issuebot-response-")
    response_path = str(Path(response_dir) / "response.json")

    try:
        spec = LaunchSpec(
            prompt=prompt,
            folder=prepared.folder,
            resume_session_id=job.resume_session_id,
            env={**job.env, **prov.env, RESPONSE_ENV: response_path},
            mcp_servers=prov.mcp_servers + [s.to_fragment() for s in job.mcp_servers],
            plugin_dirs=prov.plugin_dirs,
            disallowed_tools=list(job.withheld_tools),
        )

        # ``cancel`` is the abort signal: the caller sets it on Ctrl-C, and the
        # optional timer below sets it when the hard timeout elapses.
        cancel = cancel or threading.Event()

        timer: threading.Timer | None = None
        if job.timeout_minutes:
            timer = threading.Timer(job.timeout_minutes * 60, cancel.set)
            timer.daemon = True
            timer.start()

        # Heartbeat for the whole launch so the board knows the run is alive even
        # while the agent is busy. Every kind of work gets one — a mention's
        # non-locking run needs it just as much as a claimed task's; an empty
        # run_id (nothing to heartbeat) simply skips it.
        stop = threading.Event()
        hb = None
        if run_id and heartbeat_interval > 0:
            hb = threading.Thread(
                target=heartbeat_loop,
                args=(source, run_id, heartbeat_interval, stop),
                daemon=True,
            )
            hb.start()

        rep.start(job.work.ref, prepared.folder)
        t0 = time.monotonic()

        try:
            # Inside the try so the finally always clears it — an early return must
            # not leave a stale branch link on the dashboard.
            if "changes" in job.permits and prepared.branch:
                state.set_links([{"branch": prepared.branch}])
            result = _launch_with_retries(
                harness,
                spec,
                rep,
                cancel,
                state=state,
                reference=job.work.ref,
                max_overload_retries=max_overload_retries,
                overload_backoff=overload_backoff,
            )
        except Exception:  # noqa: BLE001
            state.set_phase("error")
            logger.exception("harness launch crashed for %s", job.work.ref)
            rep.finish("failed", time.monotonic() - t0)
            return Response(status="failed", result_text="launch crashed")
        finally:
            stop.set()
            if hb is not None:
                hb.join(timeout=2)
            if timer is not None:
                timer.cancel()
            state.clear_links()

        elapsed = time.monotonic() - t0
        status = _classify(result, cancel, elapsed=elapsed, timeout_minutes=job.timeout_minutes)
        rep.finish(status, elapsed)

        if status == "done":
            return _finish(job, workspace, prepared, settings, proc, result, response_path)

        return Response(status=status, result_text=status, session_id=result.session_id)
    finally:
        shutil.rmtree(response_dir, ignore_errors=True)
