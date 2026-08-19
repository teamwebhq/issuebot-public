"""Running a job in this process — the local execution environment.

The thinnest possible environment: it *is* :func:`issuebot.run.execute`, plus
the never-raise guarantee the ABC demands. Everything the pipeline needs —
which harness, which workspace, which source to keep the run alive on — is the
:class:`~issuebot.runner.Wiring` this environment is built over, so ``run``
takes only the job and the two things that vary per run (a reporter and an
abort signal).

That thinness is the point. The sandbox environment runs *this same pipeline*
on the far side of a wire, so "how a run works" exists once and "where it runs"
is the only difference between the two.
"""

from __future__ import annotations

import logging
import threading
from typing import TYPE_CHECKING, ClassVar

from issuebot import run as run_pipeline
from issuebot.contracts import Response
from issuebot.plugins.environments.base import ExecutionEnvironment
from issuebot.process import REAL, Process

if TYPE_CHECKING:
    from issuebot.contracts import Job
    from issuebot.reporter import Reporter
    from issuebot.runner import Wiring

logger = logging.getLogger("issuebot")


class LocalEnvironment(ExecutionEnvironment):
    """Launches the harness on this machine, in this process."""

    name: ClassVar[str] = "local"

    # This is the environment that runs the work here, in this interpreter —
    # which is what the in-sandbox worker resolves on, since inside a sandbox
    # there is no sandbox left to boot.
    runs_in_process: ClassVar[bool] = True

    def __init__(self, wiring: Wiring, proc: Process = REAL) -> None:
        """Hold the wiring every run through this connection shares.

        The whole `Wiring` rather than picked-out pieces: a local run drives
        the shared pipeline, which reads the harness, workspace, connection,
        settings and source off it — the same instance the job builder read.
        """
        self._wiring = wiring
        self._proc = proc

    def run(
        self,
        job: Job,
        *,
        reporter: Reporter,
        cancel: threading.Event | None = None,
    ) -> Response:
        """Run the job through the shared pipeline and report how it went.

        The guard is the ABC's never-raise rule, not defensive habit:
        ``.run()`` is called with the work already claimed, so an exception
        escaping here would skip the release entirely and kill the listener
        thread — a stranded claim and a dead listener, silently.
        """
        try:
            return run_pipeline.execute(
                job,
                self._wiring,
                reporter=reporter,
                cancel=cancel,
                proc=self._proc,
            )
        except Exception as exc:  # noqa: BLE001 - an environment must never crash the listener
            logger.exception("local run crashed for %s", job.work.ref)
            return Response(status="failed", result_text=f"local run crashed: {exc}")
