"""Running work in a throwaway remote machine, independent of whose machine it is.

Everything here is the *pattern*: boot a sandbox, tell the board where the run
lives, exec ``issuebot run-one`` inside it, stream the output back, recover the
result, tear the sandbox down. None of that is specific to a provider.

What is specific — how you create a machine, how you exec in it, whether it can
snapshot a filesystem, how it names the secrets it will inject — is
:class:`SandboxProvider`. A second provider (an AWS sandbox, a container host, a
bare SSH box) implements those and inherits this whole controller, the wire
protocol, the reporter lifecycle, the checkpoint policy and the teardown
guarantees.

Provider-neutrality is a property of what is *absent* from this module
(ADR-0002): secrets are :meth:`SandboxProvider.secret_env`, asked of the
provider at boot, never spelled here in any provider's own syntax.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar, Protocol

import issuebot
from issuebot import release
from issuebot.config import Connection, source_plugin
from issuebot.contracts import Job, NeedsInput, Response, WorkItem
from issuebot.events import AgentEvent
from issuebot.plugins.environments.base import ExecutionEnvironment
from issuebot.plugins.sources.base import SandboxLifecycle
from issuebot.reporter import Reporter
from issuebot.sandbox_protocol import (
    RESULT_FILE,
    RESULT_MARKER,
    BootMode,
    RunResult,
    WorkerEnv,
    parse_sentinel,
    parse_version,
    update_argv,
    version_argv,
    worker_argv,
)

if TYPE_CHECKING:
    from issuebot.runner import Wiring

logger = logging.getLogger("issuebot")


class SandboxProvider(Protocol):
    """Somewhere throwaway machines can be created, driven and destroyed.

    Implement this to add an execution environment. Auth, region, credentials
    and CLI mechanics are the implementation's business — none of it appears in
    the controller.
    """

    name: str

    # Whether this provider can snapshot a sandbox's filesystem and boot from
    # the snapshot. Providers that cannot simply always boot cold; the
    # checkpoint methods below are then never called.
    supports_checkpoints: bool

    # The command a user runs to rebuild this provider's sandbox template. The
    # controller only quotes it — when it finds a stale template it has had to
    # update at boot, the person watching the run is the one who can stop that
    # happening again, and only the provider knows what its own template is
    # built with.
    rebuild_command: str

    def secret_env(self) -> dict[str, str]:
        """Infrastructure secrets the agent needs, in whatever form this provider
        injects them.

        The model API key and a git forge token, at minimum. A provider whose
        platform resolves references at boot returns references; one that must
        pass real values returns real values; one whose image already carries
        them returns ``{}``. The controller merges the answer into the sandbox's
        environment and never looks at it.
        """
        ...

    def create(self, *, env: dict[str, str], checkpoint: str | None = None) -> str:
        """Create a sandbox with ``env`` baked in and return its id."""
        ...

    def exec_stream(
        self,
        sandbox_id: str,
        argv: list[str],
        *,
        on_line: Callable[[str], None],
        cancel: threading.Event | None = None,
    ) -> int:
        """Run a command in the sandbox, calling ``on_line`` per output line."""
        ...

    def read_file(self, sandbox_id: str, path: str) -> str:
        """Read a file out of the sandbox filesystem."""
        ...

    def destroy(self, sandbox_id: str) -> None:
        """Destroy a sandbox. May raise; the controller treats it best-effort."""
        ...

    def list_checkpoints(self) -> list[str]:
        """Existing checkpoint names. Only called when ``supports_checkpoints``."""
        ...

    def create_checkpoint(self, sandbox_id: str, name: str) -> None:
        """Snapshot a running sandbox. Only called when ``supports_checkpoints``."""
        ...

    def delete_checkpoint(self, name: str) -> None:
        """Delete a checkpoint, tolerating one that is already gone."""
        ...


@dataclass(frozen=True)
class Boot:
    """How a sandbox came up, and what that implies for the end of the run."""

    sandbox_id: str
    mode: BootMode = BootMode.COLD

    @property
    def resuming(self) -> bool:
        """True when this sandbox restored one paused task's own state."""
        return self.mode is BootMode.RESUME


def project_checkpoint(connection: Connection) -> str:
    """The checkpoint holding this connection's warm workspace."""
    return f"project-{connection.key}"


def task_checkpoint(task_id: str) -> str:
    """The checkpoint holding one paused task's own state."""
    from issuebot import task_checkpoints

    return task_checkpoints.checkpoint_name(task_id)


class SandboxEnvironment(ExecutionEnvironment):
    """Runs a job in a throwaway machine from any :class:`SandboxProvider`.

    The pattern only — every provider inherits this whole controller. A
    provider adapter supplies the six verbs above and nothing else; a second
    one is a new folder beside the first, not an edit here.
    """

    # Each provider's own subclass overrides this with the name it is
    # registered under, which is what a claim reports to the board as the
    # executor that ran the work. The generic answer is only ever seen by the
    # controller's own tests, which register nothing.
    name: ClassVar[str] = "sandbox"

    def __init__(self, wiring: Wiring, provider: SandboxProvider) -> None:
        """Bind the controller to one connection's wiring and one provider.

        Of the wiring, only the client, the connection and the context are
        read: which harness runs, which workspace is cut and which source
        narrates are all decided *inside* the sandbox, by the worker running
        the same ``run.execute`` a local run does.
        """
        self._api = wiring.api
        self._project = wiring.connection
        self._ctx = wiring.ctx
        self._provider = provider

    # -- boot ---------------------------------------------------------------

    def _boot(self, job: Job) -> Boot:
        """Bring a sandbox up on the best rung available.

        Three rungs, each conditional on a capability rather than on the kind of
        work: this task's own checkpoint (only for work that could have paused
        with something worth resuming into — read-only work's sandbox is thrown
        away), then the connection's warm project checkpoint, then a cold boot.
        A provider without checkpoint support skips straight to cold.

        "Could this run have left something worth resuming" is ``"changes" in
        job.permits`` — the source's own judgement, intersected with the
        workspace's, decided once by the controller. It replaces the inline
        ``work.kind == "assigned"`` check that stood in for it while nothing
        built a `Job`.
        """
        work = job.work
        resumable = "changes" in job.permits

        mode = BootMode.COLD
        checkpoint: str | None = None

        if self._provider.supports_checkpoints:
            existing = self._provider.list_checkpoints()
            own = task_checkpoint(work.task_id)
            shared = project_checkpoint(self._project)
            if resumable and own in existing:
                mode, checkpoint = BootMode.RESUME, own
            elif shared in existing:
                mode, checkpoint = BootMode.WARM, shared

        # The provider names its own secrets; the wire carries everything else.
        env = dict(self._provider.secret_env())
        env.update(
            WorkerEnv.for_run(
                self._ctx,
                work,
                boot=mode,
                agent_id=self._ctx.agent_id,
                # The wire carries the source's own settings table, so the
                # controller has to say *whose* — resolved the same way the
                # listener resolved it when it built the source for this run.
                source=source_plugin(self._project.source).name,
            ).encode()
        )

        return Boot(self._provider.create(env=env, checkpoint=checkpoint), mode)

    # -- version skew -------------------------------------------------------

    def _installed_version(self, sandbox_id: str) -> str:
        """Which issuebot the sandbox has, asked of the sandbox itself.

        Not of a label baked in beside it: a recorded version is one more thing
        that can be forgotten while the code moves on, and a checkpoint's
        filesystem outlives the template it descended from. A sandbox that
        cannot answer — no issuebot at all, a broken install, a provider that
        could not exec — reports ``""``, which reads as a mismatch. "I don't
        know what is in there" and "the right thing is in there" must not be the
        same answer.
        """
        lines: list[str] = []
        try:
            code = self._provider.exec_stream(sandbox_id, version_argv(), on_line=lines.append)
        except Exception:  # noqa: BLE001 - an unanswerable probe is a mismatch, not a crash
            logger.warning("could not ask sandbox %s what it is", sandbox_id, exc_info=True)
            return ""

        return parse_version(lines) if code == 0 else ""

    def _align_version(self, sandbox_id: str, reporter: Reporter) -> None:
        """Bring the sandbox to this controller's release before any work runs.

        The sandbox executes issuebot's own code, so a run on a different build
        is a wrong answer rather than a slow one. A sandbox behind its controller
        is therefore only ever a *performance* problem: it updates itself here,
        in the controller's own thread, and the work that follows is correct
        either way. Ahead is skew too — it takes this same path back down,
        because the version the controller asked for is the only one that is
        right.

        The user hears about it, not just the log: until the template is rebuilt
        this costs them an install on every cold boot, and they are the only one
        who can stop it.

        An update that fails raises, which :meth:`run` turns into a failed
        response. Loud beats working on code we already know is the wrong code.
        """
        mine = issuebot.__version__
        installed = self._installed_version(sandbox_id)
        if installed == mine:
            return

        reporter.event(
            AgentEvent(
                "text",
                f"sandbox is running issuebot {installed or '(none)'}, this "
                f"controller is {mine} — updating it for this run. "
                f"Rebuild the template with: {self._provider.rebuild_command}",
            )
        )

        output: list[str] = []
        code = self._provider.exec_stream(sandbox_id, update_argv(mine), on_line=output.append)
        if code != 0:
            tail = " / ".join(line.strip() for line in output[-3:] if line.strip())
            raise RuntimeError(
                f"could not update the sandbox to issuebot {mine} (exit {code})"
                f"{': ' + tail if tail else ''}"
            )

        refreshed = self._installed_version(sandbox_id)
        if refreshed != mine:
            raise RuntimeError(
                f"updated the sandbox to issuebot {mine}, but it still reports "
                f"{refreshed or '(none)'}"
            )

    # -- teardown -----------------------------------------------------------

    def _keep_for_resume(self, boot: Boot, work: WorkItem) -> None:
        """Snapshot this task's own state so the next run resumes straight into it.

        The other half of the boot ladder's top rung: a run that ended waiting
        on a human keeps the sandbox it was working in, under this task's own
        checkpoint name, and records when — the TTL sweep (a provider plugin's
        own `prune-checkpoints` command) reclaims the ones nobody ever came back
        to answer.

        The trigger is the agent's own conclusion rather than a way the run
        terminated: any :class:`~issuebot.contracts.NeedsInput` output
        (ADR-0011).
        """
        from issuebot import task_checkpoints

        try:
            self._provider.create_checkpoint(boot.sandbox_id, task_checkpoint(work.task_id))
        except Exception:  # noqa: BLE001 - a failed snapshot only costs a cold resume
            logger.warning("task checkpoint failed for %s", boot.sandbox_id, exc_info=True)
            return
        task_checkpoints.record(work.task_id)

    def _checkpoint_decision(self, boot: Boot, job: Job, response: Response) -> None:
        """End-of-run checkpoint bookkeeping, deterministic on the response.

        Work that ended waiting on a human keeps its own checkpoint (see
        :meth:`_keep_for_resume`) and stops there — the sandbox holds one task's
        half-finished branch, which must not become anyone else's warm boot.

        Otherwise this clears the task's checkpoint (there is nothing left to
        resume into) and, on a genuinely cold run that could have changed the
        workspace, populates the shared project checkpoint so the next run boots
        warm. A resumed sandbox is never folded into it, for the same
        one-task's-state reason; neither is read-only work, which leaves nothing
        behind worth caching.
        """
        if not self._provider.supports_checkpoints:
            return

        work = job.work

        if any(isinstance(output, NeedsInput) for output in response.outputs):
            self._keep_for_resume(boot, work)
            return

        from issuebot import task_checkpoints

        try:
            self._provider.delete_checkpoint(task_checkpoint(work.task_id))
        except Exception:  # noqa: BLE001 - deletion is best-effort
            logger.warning("task checkpoint delete failed", exc_info=True)
        task_checkpoints.forget(work.task_id)

        if (
            boot.mode is BootMode.COLD
            and "changes" in job.permits
            and response.status in ("done", "failed")
        ):
            try:
                self._provider.create_checkpoint(boot.sandbox_id, project_checkpoint(self._project))
            except Exception:  # noqa: BLE001 - warming is best-effort
                logger.warning("project checkpoint failed for %s", boot.sandbox_id, exc_info=True)

    def _destroy(self, sandbox_id: str, run_id: str) -> None:
        """Best-effort teardown. Never raises — it runs in a ``finally``, where a
        raise would discard the outcome and kill the listener thread instead of
        letting it release the run."""
        try:
            self._provider.destroy(sandbox_id)
        except Exception:  # noqa: BLE001 — must not block cleanup or the return
            logger.warning("sandbox destroy failed for %s", sandbox_id, exc_info=True)
        if isinstance(self._api, SandboxLifecycle):
            try:
                self._api.sandbox_destroyed(run_id)
            except Exception:  # noqa: BLE001 — metadata is not worth failing a run over
                logger.warning("sandbox_destroyed report failed for %s", run_id, exc_info=True)

    # -- the run ------------------------------------------------------------

    def _collect(
        self,
        sandbox_id: str,
        argv: list[str],
        *,
        ref: str,
        reporter: Reporter,
        cancel: threading.Event | None,
    ) -> tuple[RunResult | None, int]:
        """Exec the worker and recover its result.

        Streams stdout to the reporter while watching for the sentinel line,
        falling back to the result file when the sentinel never arrived (the
        worker was cut off mid-flush).

        The reporter is driven through its full ``start`` → ``event``/``raw`` →
        ``finish`` lifecycle, exactly like a local run: a ``ConsoleReporter``
        only opens its per-run log in ``start()``, so a sandbox run that skipped
        it left no ``issuebot logs`` transcript, no console feed and an empty
        dashboard tail. The worker's lines are already the rendered feed its own
        reporter produced, so they surface as ``raw`` events; the machine-only
        sentinel is logged but kept out of the feed.
        """
        found: list[RunResult] = []

        def on_line(line: str) -> None:
            reporter.raw(line)
            if line.startswith(RESULT_MARKER):
                parsed = parse_sentinel(line)
                if parsed is not None:
                    found.append(parsed)
                return
            reporter.event(AgentEvent("raw", line))

        reporter.start(ref, f"sandbox {sandbox_id}")
        t0 = time.monotonic()
        try:
            exit_code = self._provider.exec_stream(sandbox_id, argv, on_line=on_line, cancel=cancel)
            if not found:
                try:
                    recovered = RunResult.parse_json(
                        self._provider.read_file(sandbox_id, RESULT_FILE)
                    )
                except Exception:  # noqa: BLE001 - no file is a valid outcome, not a crash
                    recovered = None
                if recovered is not None:
                    found.append(recovered)
        except Exception:
            # A transport crash still closes the run out on the reporter (and its
            # log file) before propagating to the caller's failed-outcome guard.
            reporter.finish("failed", time.monotonic() - t0)
            raise

        result = found[0] if found else None
        # One classification, so the narrated status and the returned outcome
        # cannot disagree. No result is a failure whatever the exit code said —
        # the same verdict `run` reaches — so the feed must not close with a ✓
        # on a run the board is about to be told failed.
        status = result.status if result else "failed"
        reporter.finish(status, time.monotonic() - t0)
        return result, exit_code

    def run(
        self,
        job: Job,
        *,
        reporter: Reporter,
        cancel: threading.Event | None = None,
    ) -> Response:
        """Refuse-or: boot → report → align → exec → recover → checkpoint → destroy.

        ``align`` is the version step: the sandbox is brought to this
        controller's own release *before* the worker starts, never alongside it,
        because the worker is the thing whose version has to be right.

        The refusal in front of it ensures a remote controller always comes from
        the released wheel whose exact artifact the sandbox can install. Local
        runs are unaffected: they are already running exactly this code.

        A worker that said nothing fails the run, whatever its exit code. Both
        channels it reports on — the sentinel line and the result file — are
        gone, so what the run produced is not "nothing", it is unknown: the same
        distinction :func:`issuebot.run._finish` makes about a missing response
        document, and for the same reason. Reading a clean exit as ``done``
        released the task as finished with no outputs, no delivery and no
        decision, which is the one answer that is certainly wrong.

        The job itself does not cross the wire: the worker re-reads the task
        from the board and rebuilds it on the far side (see
        :mod:`issuebot.sandbox_protocol`), so what travels is the task id, the
        run id and the little the board cannot tell it. What comes back is the
        same :class:`~issuebot.contracts.Response` a local run would have
        produced, derived by the same pipeline inside the sandbox.

        Nothing propagates out: a raise here would kill the listener thread and
        strand the run, so the whole sequence is wrapped and any exception
        becomes a failed response. The sandbox is destroyed whenever one was
        actually created — and never when ``create`` itself failed, there being
        nothing to destroy.
        """
        work, run_id = job.work, job.run_id

        if not release.is_installed_wheel():
            return Response(
                status="failed",
                result_text=(
                    "remote execution requires a released issuebot wheel; install it with: "
                    f"{release.INSTALL_COMMAND}"
                ),
            )

        booted: Boot | None = None
        try:
            booted = self._boot(job)
            # An optional capability of the source's client, not part of
            # `SourceClient`: a client that cannot record where a run executes
            # is skipped, and the run proceeds without the telemetry.
            if isinstance(self._api, SandboxLifecycle):
                self._api.sandbox_started(
                    run_id, environment=self.name, sandbox_id=booted.sandbox_id
                )
            self._align_version(booted.sandbox_id, reporter)
            result, exit_code = self._collect(
                booted.sandbox_id,
                worker_argv(work, run_id=run_id, connection=self._project),
                ref=work.ref,
                reporter=reporter,
                cancel=cancel,
            )
            response = (
                result.to_response()
                if result is not None
                else Response(status="failed", result_text="no result from sandbox worker")
            )
            self._checkpoint_decision(booted, job, response)
            return response
        except Exception as exc:  # noqa: BLE001 - an environment must never crash the listener
            logger.exception("sandbox run crashed for %s", work.ref)
            return Response(status="failed", result_text=f"sandbox run crashed: {exc}")
        finally:
            if booted is not None:
                self._destroy(booted.sandbox_id, run_id)
