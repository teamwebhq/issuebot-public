"""The Source ABC: a work item's whole lifecycle, from discovery to finish.

An ABC rather than a Protocol, matching :class:`~issuebot.plugins.workspaces.base.
Workspace` and :class:`~issuebot.plugins.harnesses.base.Harness`: every source
must actually subclass this (checked by the conformance suite), not merely
happen to match its shape.

A source owns the whole lifecycle: discover, claim, narrate, apply decisions,
finish.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any, ClassVar, Protocol, runtime_checkable

if TYPE_CHECKING:
    from issuebot.agent_state import ConnectionSnapshot
    from issuebot.config import Config, Connection
    from issuebot.contracts import (
        Claim,
        McpServer,
        Output,
        OutputKind,
        Response,
        SinkResult,
        WorkItem,
    )
    from issuebot.plugins.workspaces.base import WorkspaceProblem


class ConnectionConflict(Exception):
    """This agent is already connected to that one of a source's streams, said
    by the source itself.

    Beside the ABC rather than inside an implementation because core *catches*
    it: `intake.finalize` turns it into "disconnect it first" and refuses to
    write the config, and `Supervisor._server_connect` reads its `agent_id` so a
    runner reconnecting to a durable connection still learns who it is. An
    exception core must handle is part of the axis contract, so a second source
    raises this one rather than its own — and core stops importing a class out
    of one plugin's directory to catch it.

    ``agent_id`` carries the agent's own user id when the source conveyed it on
    the conflict; None when it did not.
    """

    def __init__(self, source_ref: str, agent_id: str | None = None) -> None:
        super().__init__(source_ref)
        self.source_ref = source_ref
        self.agent_id = agent_id


class SourceClient(Protocol):
    """Everything core drives on the handle :meth:`Source.client` hands back.

    Written down because core does not merely *hold* this object and pass it to
    plugin code — it calls it, with specific signatures, from six modules. That
    is a contract whether or not anyone types it, and an untyped one is a
    contract a second source discovers by crashing.

    The set here is disjoint from :class:`Source`'s own methods: what an
    *install* does either side of a work item, none of which belongs to a
    single work item's lifecycle.

    A Protocol rather than methods on :class:`Source` because a `Source` is
    built per connection, over a board and a `Connection`, while most of this is
    install-wide — registration, telemetry and the command loop all run once per
    process, before and after any listener exists. Folding them onto the ABC
    would mean a sourceless `Source`.
    """

    # -- proving and closing the connection to the source --------------------

    def get_my_work(self, *, board_id: str | None = ...) -> list[dict[str, Any]]:
        """Work waiting for this agent. `init` and `doctor` call it to prove the
        credentials: answering at all is the check."""
        ...

    def close(self) -> None:
        """Release whatever the handle holds open."""
        ...

    # -- the agent's registration with the source ----------------------------

    def connect(
        self, board_id: str, name: str | None = ..., install_id: str | None = ...
    ) -> dict[str, Any]:
        """Register this agent against one board. Raises
        :class:`ConnectionConflict` when it is already connected. The response
        may carry the agent's own identity under ``agent``, which is how the
        runner learns its user id without a separate round-trip."""
        ...

    def disconnect(self, board_id: str) -> None:
        """Drop this agent's registration for one board."""
        ...

    def register_install(self, hostname: str | None) -> str:
        """Register this machine and return the id to persist locally.

        Core says which machine; *what it is called* is the source's own — a
        client already reads its own settings table (`Source.client`), so it
        needs nothing from core to answer this."""
        ...

    # -- one work item, outside a run ----------------------------------------

    def get_task(self, task_id: str) -> dict[str, Any]:
        """One work item's record, by id."""
        ...

    # -- the install's background loops --------------------------------------

    #: Seconds between telemetry reports. Declared here, on the contract core
    #: drives, for the same reason as `register_install`'s name: how often an
    #: install reports is a fact about the system being reported *to*. A
    #: source with no opinion answers a constant.
    telemetry_interval: float

    def report_telemetry(
        self,
        *,
        version: str,
        install_id: str,
        hostname: str | None,
        connections: list[ConnectionSnapshot],
    ) -> None:
        """Report this install's live per-connection state.

        ``connections`` is the runner's own vocabulary
        (:class:`~issuebot.agent_state.ConnectionSnapshot`); a client translates
        it to its board's wire schema on its side of the seam, exactly as the
        sandbox lifecycle columns are."""
        ...

    def wait_for_commands(
        self, *, install_id: str | None = ..., timeout: int = ...
    ) -> list[dict[str, Any]]:
        """Long-poll for control commands queued for this install."""
        ...

    def ack_command(self, command_id: str, *, status: str, result: str | None = ...) -> None:
        """Report the outcome of executing one control command."""
        ...


@runtime_checkable
class SandboxLifecycle(Protocol):
    """A source client that can record where a run executes — an optional
    capability, not part of :class:`SourceClient`.

    The sandbox controller reports its lifecycle through this: guarded by
    ``isinstance``, like ``runner._RepoSyncable``, so a client without it (a
    board with no execution metadata, a bare test double) is silently skipped
    rather than crashed into. The vocabulary here is issuebot's own
    (:mod:`issuebot.sandbox`); a client translates it to its board's schema —
    and stamps its own timestamps — on its side of the seam.
    """

    def sandbox_started(self, run_id: str, *, environment: str, sandbox_id: str) -> None:
        """Record that this run now executes in ``sandbox_id``, created by the
        named execution environment."""
        ...

    def sandbox_destroyed(self, run_id: str) -> None:
        """Record that this run's sandbox has been torn down."""
        ...


class Source(ABC):
    """Owns one work item's whole lifecycle: discover, claim, narrate, apply
    decisions, finish.

    The runner owns the loop, calling :meth:`poll`. Claim/release stay on the
    ABC even though not every source locks — a source with no locking returns
    a :class:`~issuebot.contracts.Claim` whose release does nothing, so the
    run-lock lifecycle is written once in the listener and "no lock" is an
    implementation detail rather than a second code path.
    """

    # Set by each subclass; also the name it is registered under in the
    # plugin registry (`plugins.get("sources", source.name)`).
    name: ClassVar[str]

    # -- the install-wide handle ---------------------------------------------

    @classmethod
    @abstractmethod
    def client(cls, cfg: Config) -> SourceClient:
        """This source's own API handle, built from its global settings.

        A classmethod because it is needed *before* any instance exists: the
        thing `__init__` takes as ``client`` is this, and everything an install
        does outside a single work item — `issuebot init`'s credential check,
        `doctor`, `connect`/`disconnect`, the supervisor registering the install
        and running its telemetry and command loops — needs one too.

        The return type is the point. Core does not just carry this object
        around; it calls nine methods on it across six modules, so what a source
        must implement to be usable is :class:`SourceClient`, written down there
        rather than discovered by a second source crashing. An implementation is
        free to return something with far more on it — `__init__` takes back
        whatever this returns, so the per-connection half never has to squeeze
        through the same door.
        """

    @classmethod
    def user_mcp(cls, cfg: Config) -> McpServer | None:
        """The MCP server that wires this source into the user's *own* agent
        tooling, or None when it has nothing to offer.

        Distinct from :meth:`agent_access`, which is per work item and per run:
        this is the one-time, install-wide registration a harness performs
        against the human's own interactive setup — the equivalent of the user
        typing that agent CLI's own "add this MCP server, for my whole account"
        command — so it is asked of the class and takes only the config.
        Concrete with a None default, so a source with no such channel
        implements nothing and the harness simply skips the step.
        """
        return None

    # -- one work item's lifecycle -------------------------------------------

    @abstractmethod
    def poll(self, *, timeout: int) -> list[WorkItem]:
        """Long-poll for work items waiting on this source."""

    def sweep(self) -> list[WorkItem]:
        """The standing list of work waiting for this agent, for a source that
        can answer one.

        Beside :meth:`poll` rather than inside it: a delivery channel says what
        arrived, this says what is *still* assigned. The runner sweeps on an
        interval so work a one-shot delivery missed — delivered while the poll
        loop was erroring, or never delivered per item at all — is found anyway.
        Concrete with an empty default: a source with only a delivery channel
        implements nothing and keeps working unchanged."""
        return []

    @abstractmethod
    def claim(self, work: WorkItem) -> Claim | None:
        """Take this work item's lock, or ``None`` if it could not be taken —
        another runner won the race, or the attempt itself failed. Either way
        the item will be offered again by a later :meth:`poll`, so the caller
        need not tell the two apart."""

    @abstractmethod
    def release(self, claim: Claim, response: Response) -> None:
        """Release a claim once its run has finished, reporting how it went."""

    def heartbeat(self, run_id: str) -> None:  # noqa: B027 — deliberately concrete, see docstring
        """Keep a claimed run's lock alive while it is in flight.

        The one thing ``run.execute`` asks of a source *during* a run — called
        on an interval for the whole launch, so the board does not expire the
        lock under a busy agent. Concrete with a no-op default: a source whose
        board has no lease concept implements nothing, and the runner's
        heartbeat loop is then a clean no-op rather than an error every
        interval."""

    @abstractmethod
    def say(self, work: WorkItem, message: str) -> None:
        """Post one message to the work item, in the runner's own voice."""

    @abstractmethod
    def apply(self, work: WorkItem, decision: Output) -> None:
        """Apply a decision output (``needs_input`` or ``handoff``) to the
        work item — a fact about where it goes next in this source's own
        system."""

    @abstractmethod
    def finish(self, work: WorkItem, response: Response, results: list[SinkResult]) -> None:
        """Report a run's outcome, including what any sinks did with its
        deliverables — so the source can say "PR opened: …" without knowing
        what a PR is."""

    @abstractmethod
    def permits(self, work: WorkItem) -> frozenset[OutputKind]:
        """Which output kinds this work item may report — this source's own
        judgement about its own work kinds, not a property of the kind alone."""

    @abstractmethod
    def prompt(
        self,
        work: WorkItem,
        connection: Connection,
        *,
        permits: frozenset[OutputKind],
        problem: WorkspaceProblem | None = None,
    ) -> str:
        """Render the launch prompt for this work item.

        ``permits`` is handed in rather than read back from :meth:`permits`
        because it is the *job's* latitude, already narrowed by what the
        connection's workspace can produce. A source asked for its own answer
        instead would tell a folder-workspace run it may report ``changes`` —
        an instruction to do something the run will then be rejected for.

        ``problem`` is a condition the workspace's ``prepare`` reported
        (:class:`~issuebot.plugins.workspaces.base.WorkspaceProblem`, a
        diverged branch today): the run proceeds, and the source weaves its
        own instruction for resolving it into the prompt. Only ``run.execute``
        passes one — ``runner.job_for`` renders before the workspace is
        prepared, so its call carries the default None."""

    @abstractmethod
    def agent_access(self, work: WorkItem) -> tuple[McpServer, ...]:
        """MCP servers giving the agent its own channel to this source, beyond
        whatever the environment already wires in — empty when there is none."""
