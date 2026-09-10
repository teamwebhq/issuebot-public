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
exits. A missing document earns one resumed retry — the agent is asked again,
in the same conversation, to write it — and only then fails the run; an
unparseable one fails outright. Either failure means the agent never finished
reporting, a different state from a document that deliberately says
``{"outputs": []}``.

One output is not the agent's: when git says the run committed and the agent
reported no ``changes`` output, ``_finish`` appends one of its own. ``Changes``
is derived from git because the agent's word is not trusted, so whether that
work reaches a sink cannot hinge on the agent's word either — see
:func:`_derived_changes_output`.
"""

from __future__ import annotations

import logging
import shutil
import tempfile
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from issuebot import plugins, provision
from issuebot.agent_state import AgentState
from issuebot.config import conn_setting, harness_named
from issuebot.contracts import (
    Answer,
    Changed,
    Changes,
    Delivery,
    Output,
    Response,
    SinkResult,
    parse_outputs,
)
from issuebot.plugins.harnesses.base import Harness, LaunchResult, LaunchSpec
from issuebot.plugins.sources.base import ForgeAuth
from issuebot.plugins.workspaces.base import Prepared, Workspace
from issuebot.process import REAL, Process, with_env
from issuebot.reporter import ConsoleReporter, Reporter
from issuebot.sandbox_protocol import READY_MARKER, in_sandbox
from issuebot.summaries import commit_message
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

# What the agent is told when it exits cleanly having never written its response
# document. Deliberately narrow: the conversation is resumed with all its work
# already done, so the only thing left to ask for is the file itself.
RESPONSE_NUDGE = (
    "You exited without writing your response document. Do no further work — just write "
    f"it now, as JSON, to the path in the ${RESPONSE_ENV} environment variable. That file "
    "is the only channel for your answer: without it this run is reported as a failure and "
    "everything you did is discarded."
)

# The board's skill that carries PR-writing guidance, when it sends one. Named
# once here rather than at each of `execute`'s and the GitHub sink's call
# sites -- both need the same slug, and only one of them ever changes it.
#
# This is a contract with the board's seeded skill set, not a lookup issuebot
# validates: a Parade org that renames or deletes the `writing-pull-requests`
# skill does not break anything here, it just means `bundle.body()` finds no
# match and `guidance` renders empty -- the PR description silently loses that
# input with no error to trace it back to.
PR_GUIDANCE_SLUG = "writing-pull-requests"


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
        prepared = workspace.prepare(
            connection, job.work.ref, settings=settings, base=job.work.base_branch, proc=proc
        )
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
    else:
        # A sandbox controller is watching this stream for the one moment the
        # workspace is worth keeping for every later task: prepared and
        # bootstrapped, and nothing done in it yet. Said only on the success
        # path — a half-applied bootstrap must not become anyone's warm boot —
        # and only from a sandbox, a local run having no controller to tell.
        if in_sandbox():
            rep.raw(READY_MARKER)

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


def _retry_response(
    harness: Harness,
    spec: LaunchSpec,
    result: LaunchResult,
    response_path: str,
    rep: Reporter,
    cancel: threading.Event,
    *,
    reference: str,
) -> None:
    """Give an agent that exited cleanly without its response document one more
    turn to write it.

    A run that did every bit of its work and only missed the final file would
    otherwise be failed by :func:`_finish` and have all of it discarded, so the
    same conversation is reopened with nothing but a nudge to write the
    document. Bounded to a single extra launch: an agent that still writes
    nothing fails the run exactly as before.

    Only for a harness that resumes — a fresh launch would carry no memory of
    the work and would simply redo it — and never after an abort or timeout,
    where more agent work is the last thing wanted.

    Runs purely for its side effect on ``response_path``. The original
    ``result`` stays the one the caller finishes with: its ``session_id`` is
    the conversation to store, and its ``result_text`` is the PR-body fallback,
    neither of which this content-free turn can improve on.
    """
    session = result.session_id or spec.resume_session_id
    if cancel.is_set() or not harness.resumes_sessions or session is None:
        return

    if Path(response_path).exists():
        return

    logger.info("no response document from %s; asking again in the same session", reference)
    harness.launch(replace(spec, prompt=RESPONSE_NUDGE, resume_session_id=session), rep, cancel)


# ---------------------------------------------------------------------------
# Finishing
# ---------------------------------------------------------------------------


# What a synthesized `Changed` says when the agent wrote no answer to borrow a
# summary from. Deliberately plain about where it came from: nothing the agent
# said describes this commit, and the GitHub sink reads the diff anyway.
DERIVED_SUMMARY = "committed changes the agent did not summarise"


def _derived_changes_output(outputs: list[Output], changes: Changes) -> list[Output]:
    """``outputs`` with a git-derived ``changes`` output appended when one is missing.

    Exists because the two halves of "changes" have different trust levels.
    :class:`~issuebot.contracts.Changes` is derived from git precisely because
    the agent's word is not trusted — an agent claiming a refactor cannot move
    ``head_sha``. But sinks are only offered the outputs the *agent* wrote, so a
    run that committed and pushed while reporting only an ``answer``, only a
    ``handoff``, or nothing at all would leave a branch on the forge that no
    sink ever sees: no pull request opened, no sink result, nothing on the board
    saying the work is sitting there. Deriving the changes from git is pointless
    if delivering them still hinges on the agent's word, so it does not.

    The agent's first ``answer`` is the closest thing to its own account of the
    run, so that text becomes the summary; failing that, the plainly-labelled
    fallback. Appended last, leaving the agent's own outputs in the order it
    wrote them.
    """
    if changes.empty or any(o.kind == "changes" for o in outputs):
        return outputs

    answer = next((o for o in outputs if isinstance(o, Answer)), None)

    return [*outputs, Changed(summary=answer.text if answer is not None else DERIVED_SUMMARY)]


def _finish(
    job: Job,
    workspace: Workspace,
    prepared: Prepared,
    settings: BaseModel,
    proc: Process,
    result: LaunchResult,
    response_path: str,
    *,
    guidance: str = "",
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

    A commit that produced real commits is then made deliverable whatever the
    agent reported: :func:`_derived_changes_output` appends a ``changes`` output
    when the agent wrote none, so git's own account of the run reaches a sink
    without depending on the agent having mentioned it.

    ``guidance`` only ever reaches a caller past this point: it rides
    unconditionally on a ``done`` response (see ``Response.guidance``), a
    failure never delivers so never needs it.
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
        return Response(
            status="done", outputs=outputs, session_id=result.session_id, guidance=guidance
        )

    try:
        # The commit says what the run did, in the agent's own words: `outputs`
        # is already in hand, and the bare ref alone made every commit in a
        # repository read the same.
        message = commit_message(job.work.ref, outputs)
        changes = workspace.commit_and_push(prepared, message, settings=settings, proc=proc)
    except Exception:  # noqa: BLE001 - surface any commit/push failure
        logger.exception("commit/push failed for %s", job.work.ref)
        return Response(
            status="failed", result_text="commit/push failed", session_id=result.session_id
        )

    return Response(
        status="done",
        changes=changes,
        outputs=_derived_changes_output(outputs, changes),
        session_id=result.session_id,
        guidance=guidance,
    )


# ---------------------------------------------------------------------------
# Delivering
# ---------------------------------------------------------------------------


def deliver_all(
    work: WorkItem,
    response: Response,
    connection: Connection,
    *,
    sinks: Sequence[tuple[SinkRef, Sink]],
    forge_env: Mapping[str, str] | None = None,
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
                forge_env=forge_env or {},
                guidance=response.guidance,
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
# Forge credentials
# ---------------------------------------------------------------------------


def forge_env(source: Source, work: WorkItem) -> Mapping[str, str]:
    """What this item's git/`gh` calls should authenticate and identify as.

    A capability, not part of the axis: a source that holds credentials of its
    own (a board with a GitHub App installed) lends a short-lived one for this
    item, so the work is attributed to that app rather than to whichever
    person's personal token is on the machine. One that does not — or that has
    nothing to lend for this item — leaves the run using the machine's own
    credential.

    Asked once as the job is built, so the controller and a sandbox worker
    (which rebuilds the job through this same function) each borrow their own,
    and asked again through :func:`refreshed_forge_env` before the steps that
    need a live token.
    """
    return source.forge_env(work) if isinstance(source, ForgeAuth) else {}


def harness_for_work(work: WorkItem, wiring: Wiring, *, proc: Process = REAL) -> Harness:
    """Which harness runs this item: the one the board asked for, else the one
    this install configured.

    The install's harness is the default, not the verdict — a board (or one of
    its columns) that names a different installed harness gets it, built here
    from that harness's own settings table (`ctx.plugin_settings`, where its
    `command` override lives). Resolved at launch rather than at wiring time
    because the choice belongs to the item, and no item exists when a
    connection is wired.

    A request this install cannot honour — an unknown harness, one this build
    refuses (`codex`), one whose plugin has no implementation — never fails the
    run: a preference set on a machine the board cannot see falls back to the
    install's own harness, logged once naming both and why.

    Lives here rather than in :mod:`issuebot.runner` because `runner` imports
    this module, exactly as :func:`forge_env` does.
    """
    installed = wiring.harness
    wanted = work.harness

    # Nothing asked for, or asked for what is already running: the wiring's own
    # instance answers. Never rebuilt — a test injects its harness there, and a
    # copy would silently drop whatever that instance was set up to do.
    if wanted is None or wanted == installed.name:
        return installed

    try:
        return harness_named(wanted, wiring.ctx.plugin_settings, proc=proc)
    except (plugins.UnknownPlugin, TypeError) as exc:
        logger.warning(
            "%s requested harness '%s', but this install cannot run it (%s); using '%s'",
            work.ref,
            wanted,
            exc,
            installed.name,
        )
        return installed


def refreshed_forge_env(source: Source, job: Job) -> Mapping[str, str]:
    """The job's forge credentials, borrowed again for a step about to run.

    A lent token lives about an hour, and an agent can work for longer — so the
    one borrowed as the job was built may already be dead by the time there is
    something to push or a pull request to open. The board caches per
    (installation, repo) and renews shortly before expiry, so asking again is
    nearly free and never hands back a nearly-dead token.

    A failed borrow falls back to what the run started with. That fallback is
    worth something only while the original token is still in date, which on a
    run longer than the token's hour it is not — the fallback is then worthless
    and the next git or ``gh`` call is refused by GitHub. Behaviour is the same
    either way (there is nothing better to offer), but the certain failure is
    logged where it happens instead of surfacing as GitHub's "Invalid username
    or token" much later.

    Nothing lent at the start means there is nothing to renew, and no board
    call worth making.
    """
    if not job.forge_env:
        return {}

    borrowed = forge_env(source, job.work)
    if borrowed:
        return borrowed

    _warn_if_fallback_expired(job)

    return job.forge_env


def _warn_if_fallback_expired(job: Job) -> None:
    """Say whether the token the run started with is already dead.

    Reads the board's stated expiry from the job's own forge environment (see
    :meth:`issuebot.plugins.sources.issuebear.source.Issuebear.forge_env`). An
    absent or unparseable value means the age cannot be told, so nothing is
    said. Never raises: this only makes a failure legible, and must not cause
    one.
    """
    stated = job.forge_env.get("ISSUEBOT_FORGE_TOKEN_EXPIRES_AT")
    if not stated:
        return

    try:
        expires_at = datetime.fromisoformat(stated.replace("Z", "+00:00"))
    except ValueError:
        logger.debug("could not read the lent token's expiry %r for %s", stated, job.work.ref)
        return

    now = datetime.now(expires_at.tzinfo)

    # A minute of slack: a token that dies while the push is in flight is as
    # dead as one that died an hour ago.
    if expires_at - now <= timedelta(minutes=1):
        logger.warning(
            "could not borrow git credentials again for %s and the token this run started with "
            "expired at %s; the next git or gh call will be refused by GitHub",
            job.work.ref,
            stated,
        )
    else:
        logger.debug(
            "could not borrow git credentials again for %s; the token this run started with "
            "is in date until %s",
            job.work.ref,
            stated,
        )


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
    workspace = wiring.workspace
    connection = wiring.connection
    settings = wiring.workspace_settings
    source = wiring.source
    run_id = job.run_id
    heartbeat_interval = wiring.ctx.heartbeat_interval

    state = wiring.ctx.state or AgentState()
    rep = reporter or ConsoleReporter(ref=job.work.ref, show_prefix=False, agent_state=state)

    # Everything this run shells out to — the clone, the branch, the push —
    # authenticates as whatever the source lent (`Job.forge_env`), not as the
    # machine's own credential. Applied once here rather than at each git call
    # site: one missed call site would silently fall back to the credential
    # this exists to stop using. `with_env` returns `proc` untouched when
    # nothing was lent.
    proc = with_env(proc, job.forge_env)

    # Which harness does the work is the item's call, not the install's — and
    # it is decided after the credentials above, so a harness spawned here runs
    # with the same lent identity as everything else in the run.
    harness = harness_for_work(job.work, wiring, proc=proc)

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

    # The skills the board sent with this item, materialised on disk (cached by
    # content, so only the first task on a board actually downloads anything).
    # Appended after the repo's own `prov.plugin_dirs` rather than before, so a
    # repository can add to what the board gives without displacing it.
    bundle = source.agent_skills(job.work)

    # Read here, alongside the launch, while the bundle is warm — carried on
    # the `Response` (see its docstring) rather than left for a later,
    # possibly colder-cached, delivery step to resolve.
    guidance = bundle.body(PR_GUIDANCE_SLUG)

    try:
        spec = LaunchSpec(
            prompt=prompt,
            folder=prepared.folder,
            # A stored session belongs to the harness that started it: the
            # store keys by task id alone, so a session id means nothing to a
            # harness the board named over this install's own. An overridden
            # run therefore starts fresh.
            # ponytail: dropped rather than kept per harness — key the session
            # store by (task, harness) when a board switches harness mid-task
            # often enough for the lost context to hurt.
            resume_session_id=job.resume_session_id if harness is wiring.harness else None,
            env={**job.env, **job.forge_env, **prov.env, RESPONSE_ENV: response_path},
            mcp_servers=prov.mcp_servers + [s.to_fragment() for s in job.mcp_servers],
            plugin_dirs=prov.plugin_dirs + ([bundle.plugin_dir] if bundle.plugin_dir else []),
            disallowed_tools=list(job.withheld_tools),
            model=job.work.model,
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
            # A clean exit with no response document is worth one more ask
            # before `_finish` throws the whole run away over the missing file.
            _retry_response(
                harness, spec, result, response_path, rep, cancel, reference=job.work.ref
            )

            # The commit and push happen after the agent, which may have run
            # for longer than a lent token lives — so borrow again and layer
            # the fresh answer over the one applied above (an outer overlay
            # wins over an inner one; an empty one adds no layer at all).
            pushing = with_env(proc, refreshed_forge_env(source, job))

            return _finish(
                job,
                workspace,
                prepared,
                settings,
                pushing,
                result,
                response_path,
                guidance=guidance,
            )

        return Response(status=status, result_text=status, session_id=result.session_id)
    finally:
        shutil.rmtree(response_dir, ignore_errors=True)
