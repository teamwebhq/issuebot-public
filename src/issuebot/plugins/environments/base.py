"""The ExecutionEnvironment ABC: where a :class:`~issuebot.contracts.Job` runs.

An ABC rather than a Protocol, matching :class:`~issuebot.plugins.sources.base.
Source`, :class:`~issuebot.plugins.workspaces.base.Workspace` and
:class:`~issuebot.plugins.sinks.base.Sink`: every environment must actually
subclass this (checked by the conformance suite), not merely happen to match
its shape.

Two environments satisfy it today: the local one, which launches the harness in
this process, and a sandbox one, which boots a throwaway machine and has the
same pipeline (:func:`issuebot.run.execute`) run inside it. Adding a third (an
AWS Lambda sandbox, a container host) means writing one folder and registering
it; nothing in the runner changes — ADR-0002.

Three axes meet here and stay independent of one another:

* **what** arrived — the :class:`~issuebot.contracts.Job` the runner built
* **how it is treated** — ``job.permits``, which is the source's own judgement
  about its work kinds intersected with what the workspace can produce, so a
  new kind of work is a source's decision rather than a branch in every
  environment
* **where it runs** — this interface
* **who does the work** — :class:`~issuebot.plugins.harnesses.base.Harness`

An environment therefore has one method, not one per kind of work — and one
return type both environments must produce, so a caller can never be handed an
outcome it does not read (ADR-0008).
"""

from __future__ import annotations

import threading
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, ClassVar

if TYPE_CHECKING:
    from issuebot.contracts import Job, Response
    from issuebot.reporter import Reporter
    from issuebot.runner import Wiring


class ExecutionEnvironment(ABC):
    """Somewhere one :class:`~issuebot.contracts.Job` can be run to completion.

    Does not claim or release the run, and does not touch the session store —
    it returns the outcome and the caller owns the lifecycle. That is the seam
    which lets one run-lock lifecycle wrap any environment.
    """

    # Set by each subclass; also the name it is registered under in the plugin
    # registry (`plugins.get("environments", environment.name)`), and what a
    # claim reports to the board as the executor that ran the work.
    name: ClassVar[str]

    # Whether this environment runs the job in the calling process, rather than
    # handing it to a machine somewhere else. A capability, declared like
    # `Workspace.produces` and `Sink.needs_pushed_branch`, and read off the
    # *class* the registry holds — see `runner.in_process_environment`, which is
    # how the in-sandbox worker asks for "the environment that runs the work
    # right here" without naming one. It has to: that worker is already inside
    # its sandbox, so there is no sandbox left to boot and the run is in-process
    # by definition, but which plugin that *is* is not core's to know.
    runs_in_process: ClassVar[bool] = False

    @abstractmethod
    def __init__(self, wiring: Wiring, *args: object) -> None:
        """Every environment is built over one connection's assembled
        :class:`~issuebot.runner.Wiring` — the constructor contract
        ``environment_for`` holds every plugin to.

        Subclasses override this and read what they need of the wiring: a
        sandbox decides harness, workspace and source on the far side of the
        wire, so it reads only the client, the connection and the context; a
        local run reads the rest. They also accept a ``proc`` keyword — the
        conformance suite requires it, so no environment test can reach a real
        machine.
        """

    @abstractmethod
    def run(
        self,
        job: Job,
        *,
        reporter: Reporter,
        cancel: threading.Event | None = None,
    ) -> Response:
        """Run this job, whatever kind of work it carries, and report how it went.

        Must not raise: an environment that let an exception escape would kill
        the listener thread and strand a claimed run, so a crash is reported as
        a failed :class:`~issuebot.contracts.Response` instead. The conformance
        suite breaks every environment at a point none of them can avoid to
        check that.
        """
