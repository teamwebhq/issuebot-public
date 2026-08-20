"""The vocabulary every layer shares.

Owned by no plugin type: a :class:`Response` is produced by an environment and
consumed by the controller, the source and every sink, so it cannot live inside
any one of them without the others importing across a boundary that shouldn't
exist.

Two payloads come back from a run and they have different trust levels:
:class:`Changes` is derived from git by the environment, :class:`Output` is
authored by the agent from its response file. That distinction is the one
thing here most likely to be eroded by a later convenience, so it is kept as
two separate types rather than one permissive one.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Annotated, Any, Literal, get_args

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

# ---------------------------------------------------------------------------
# In: what a source delivers
# ---------------------------------------------------------------------------

WorkKind = Literal["assigned", "mention"]


@dataclass(frozen=True)
class WorkItem:
    """A task assigned to this agent, or a mention of it on a task.

    A source is free to send fields this type doesn't model; unknown keys are
    ignored rather than rejected, so a newer server can add to the payload
    without breaking an older runner.
    """

    task_id: str

    # The human-facing ref (e.g. "ISS-42"). The board always sends one, but the
    # in-sandbox worker reads it from a task record where it may be absent.
    reference: str | None = None

    # Which of the source's streams delivered it — a board, a repo, a team,
    # whatever that source divides work into. The agent-wide poll returns every
    # connection's work together, so a listener uses this to ignore items that
    # are not its own. Named for the axis rather than for one source's noun:
    # the wire key below is issuebear's, this field is the contract's.
    source_ref: str | None = None

    # "mention" is not claimable and runs a lighter, respond-only session. A
    # missing kind means an older server that only ever sent assigned tasks.
    kind: WorkKind = "assigned"

    # The server's non-locking "responding" run for a mention, heartbeated and
    # released while the session works. Older servers omit it.
    run_id: str | None = None

    # Mention context: who mentioned the agent, and what they said.
    actor_name: str | None = None
    comment_excerpt: str | None = None

    # The repository the item's project is linked to. A connection configured
    # for a different one is not the connection that should do this work, and
    # `runner.job_for` refuses the run rather than opening a PR that never
    # appears on the task. None when the project is unlinked, or when the board
    # cannot confirm the link — neither says anything about the connection, so
    # neither is a mismatch.
    repo: str | None = None

    @classmethod
    def from_api(cls, payload: dict[str, Any]) -> WorkItem:
        """Build from a ``/me/work`` payload, ignoring fields we don't model."""
        kind = payload.get("kind")
        return cls(
            task_id=str(payload["task_id"]),
            reference=payload.get("reference"),
            source_ref=None if payload.get("board_id") is None else str(payload["board_id"]),
            kind="mention" if kind == "mention" else "assigned",
            run_id=payload.get("run_id"),
            actor_name=payload.get("actor_name"),
            comment_excerpt=payload.get("comment_excerpt"),
            repo=payload.get("repo"),
        )

    @property
    def ref(self) -> str:
        """The ref to show and to name branches, logs and workspaces after.

        Falls back to the task id when the source sent no reference."""
        return self.reference or self.task_id

    def for_source_ref(self, ref: str) -> bool:
        """True when this item belongs to the given one of a source's streams.

        An unattributed item belongs to none of them: the agent-wide poll is
        not scoped to a connection, so running such an item against an
        arbitrary one would run it in the wrong workspace."""
        return self.source_ref is not None and str(self.source_ref) == str(ref)


class McpServer(BaseModel):
    """One MCP server to make available to the agent, stdio or http.

    Shared vocabulary because two axes speak it: a source declares board
    access (the agent's own channel to read the task and narrate), and a
    repo's ``.issuebear.toml`` bootstrap adds more of the same kind.
    """

    name: str
    type: str | None = None
    command: str | None = None
    args: list[str] = Field(default_factory=list)
    url: str | None = None
    headers: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _check_transport(self) -> McpServer:
        """An http server needs a url; a stdio server needs a command. We treat the
        server as http when `type == "http"` or a `url` is given (matching
        `to_fragment`), and require the field its transport depends on."""
        if self.type == "http" or self.url:
            if not self.url:
                raise ValueError("http MCP server requires a url")
        elif not self.command:
            raise ValueError("stdio MCP server requires a command")
        return self

    def to_fragment(self) -> dict[str, dict]:
        """This server as an `mcpServers` entry: `{name: {...}}`. The http form is
        chosen when `type == "http"` or a `url` is given; otherwise stdio."""
        if self.type == "http" or self.url:
            body: dict = {"type": "http", "url": self.url, "headers": self.headers}
        else:
            body = {"command": self.command, "args": self.args}
        return {self.name: body}


# ---------------------------------------------------------------------------
# How a run ended
# ---------------------------------------------------------------------------

RunStatus = Literal["done", "failed", "aborted", "timed out"]
"""How the process ended — not what the agent decided.

"Waiting on a human" is a `NeedsInput` output, a thing the agent concluded
rather than a way the run terminated: a run can end `done` and still return
`NeedsInput` (ADR-0011)."""


def coerce_status(value: object, *, default: RunStatus = "failed") -> RunStatus:
    """Narrow an untrusted string to a :data:`RunStatus`.

    The sandbox worker's status arrives as JSON over a pipe, so it is a plain
    ``str`` until something checks it. Without this, an older or malformed
    worker could put an unknown string into a ``Literal``-typed field and only
    be noticed much later, wherever that value was next compared."""
    for known in get_args(RunStatus):
        if value == known:
            return known
    return default


@dataclass(frozen=True)
class Changes:
    """What the environment actually did to the repository.

    Derived from git by the environment, never reported by the agent — an agent
    claiming it refactored three files cannot move `head_sha`."""

    branch: str
    base_sha: str
    head_sha: str
    stat: str
    files_changed: int
    pushed: bool = False

    @property
    def empty(self) -> bool:
        """True when the agent produced nothing, whatever it claims."""
        return self.head_sha == self.base_sha


# ---------------------------------------------------------------------------
# Out: what the agent says it produced
# ---------------------------------------------------------------------------


class Output(BaseModel):
    """One thing the agent says it produced. A run may return several."""

    kind: str

    @property
    def is_deliverable(self) -> bool:
        """Deliverables go to sinks; decisions go to the source."""
        return self.kind in ("changes", "answer")


class Changed(Output):
    """The agent made changes to the repository; `summary` describes them."""

    kind: Literal["changes"] = "changes"
    summary: str = Field(min_length=1)


class Answer(Output):
    """The agent produced an answer with no repository changes."""

    kind: Literal["answer"] = "answer"
    text: str = Field(min_length=1)


class NeedsInput(Output):
    """The agent cannot proceed without a human answering `question`."""

    kind: Literal["needs_input"] = "needs_input"
    question: str = Field(min_length=1)


class Handoff(Output):
    """The agent is handing the work item to `assignee`, with an optional `note`."""

    kind: Literal["handoff"] = "handoff"
    assignee: str = Field(min_length=1)
    note: str = ""


AnyOutput = Annotated[Changed | Answer | NeedsInput | Handoff, Field(discriminator="kind")]
OutputKind = Literal["changes", "answer", "needs_input", "handoff"]


@dataclass(frozen=True)
class Job:
    """Everything an environment needs to run one piece of work.

    Built by the controller (``runner.job_for``) and handed to an
    :class:`~issuebot.plugins.environments.base.ExecutionEnvironment` whole, so
    the questions "what may this run report" and "what prompt does it launch
    with" are answered once, in one place, rather than per environment.
    """

    work: WorkItem
    prompt: str

    # The connection's own folder, or None when it keeps none (a clone-based or
    # sandboxed connection). Only ever a *fallback* location: the workspace
    # plugin decides where the run actually happens, and this is what a run that
    # may not report `changes` degrades to when that preparation fails.
    #
    # No workspace plugin's vocabulary rides here: a plugin's strategy reaches
    # that plugin through its own settings model (`runner.workspace_for`),
    # never through the shared contracts.
    folder: str | None

    # The latitude: what the agent MAY return. Permission, not obligation.
    # Already `source.permits(work) & workspace.produces` — an environment
    # never has to intersect anything itself.
    permits: frozenset[OutputKind]
    withheld_tools: tuple[str, ...]
    timeout_minutes: int | None

    # Board read + narration, from the source.
    mcp_servers: tuple[McpServer, ...]

    # From the repo's own provisioning.
    env: Mapping[str, str]
    resume_session_id: str | None

    # The source's own id for this run, to heartbeat and to report sandbox
    # metadata under. Empty when the work carries no run to keep alive (a
    # mention an older server opened no responding run for).
    run_id: str = ""


class _Document(BaseModel):
    """The agent's response file, as written."""

    model_config = ConfigDict(extra="forbid")

    outputs: list[AnyOutput]


def parse_outputs(raw: str) -> list[Output]:
    """Parse the agent's response document, raising on anything malformed.

    Strict on purpose: a response we cannot read is a failed run, not a run with
    no outputs. Those two states mean very different things and must not be
    conflated by a tolerant parser."""
    try:
        return list(_Document.model_validate_json(raw).outputs)
    except ValidationError as exc:
        raise ValueError(f"malformed response document: {exc}") from exc


@dataclass(frozen=True)
class Response:
    """What an environment hands back."""

    status: RunStatus
    changes: Changes | None = None
    outputs: list[Output] = field(default_factory=list)
    session_id: str | None = None
    result_text: str = ""

    @property
    def deliverables(self) -> list[Output]:
        """Outputs that go to sinks."""
        return [o for o in self.outputs if o.is_deliverable]

    @property
    def decisions(self) -> list[Output]:
        """Outputs that mutate the source."""
        return [o for o in self.outputs if not o.is_deliverable]


# ---------------------------------------------------------------------------
# The supporting values
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Delivery:
    """What a sink is handed: one deliverable, and where it belongs.

    ``repo``/``folder`` are facts about the delivery, told to every sink alike
    — not settings anyone configures. A sink runs controller-side, and for a
    clone-based or sandboxed connection the controller has no local checkout at
    all: the workspace only ever existed inside the sandbox. So ``repo`` (the
    connection's own repository URL) is the answer that works for every
    connection shape, and ``folder`` is the extra a connection that *does* keep
    a checkout can offer on top.
    """

    work: WorkItem
    output: Output  # the deliverable this sink accepts
    changes: Changes | None  # present when the run produced any

    # The connection's repository, as configured (a clone URL). Empty for a
    # connection that works in place from `folder`.
    repo: str = ""

    # The connection's own local working copy, when it has one. Empty for a
    # clone-based or sandboxed connection — see the class docstring.
    folder: str = ""


@dataclass(frozen=True)
class SinkResult:
    """What a sink did, in terms the source can report without understanding it."""

    sink: str
    ok: bool
    summary: str  # "opened PR", "deployed", "could not reach Netlify"
    url: str | None = None


@dataclass(frozen=True)
class Claim:
    """A source's lock on one work item.

    A source that does not lock returns one whose release does nothing, so the
    run-lock lifecycle can be written once regardless of whether the source
    backing it locks at all.
    """

    work_id: str
    token: str | None = None  # whatever the source needs to release it
