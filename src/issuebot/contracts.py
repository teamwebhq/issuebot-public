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
class SkillRef:
    """One skill this item is worked with: what to fetch, and how to cache it.

    The board sends identity and freshness, never content — the folder is
    downloaded once per `updated_at` and reused for every task after it.
    """

    id: str
    slug: str
    name: str = ""
    updated_at: str = ""


@dataclass(frozen=True)
class PrPolicy:
    """What the board's step wants done with this run's pushed branch.

    One step of a board's pipeline opens a draft for a later step to finish;
    another finishes the work and opens it for review; another produces a branch
    on purpose and no pull request at all. That is the board's decision, not the
    runner's, so it arrives with the work item and a sink obeys it.

    The default is what every run did before a step could ask for anything: open
    a pull request, not a draft, request nobody.
    """

    create: bool = True
    draft: bool = False

    # GitHub logins, already resolved by the board. Reviewer identity is the
    # board's to map — it holds the link between a board member and their forge
    # account — so issuebot never turns a board user into a GitHub login itself,
    # exactly as it never picks the harness in `harness`/`model` below.
    reviewers: tuple[str, ...] = ()


def _pr_policy(payload: object) -> PrPolicy:
    """One step's pull-request policy from its wire object, or the default.

    Read leniently, like the rest of a work item: a step that sent no ``pr`` at
    all, and one that sent only part of it, both get the defaults for whatever
    they left out rather than failing the item."""
    if not isinstance(payload, Mapping):
        return PrPolicy()

    # A step that sent something other than a list of logins is read as having
    # named nobody, the same as one that named nobody.
    reviewers = payload.get("reviewers")
    logins = tuple(str(login) for login in reviewers) if isinstance(reviewers, list | tuple) else ()

    return PrPolicy(
        create=bool(payload.get("create", True)),
        draft=bool(payload.get("draft", False)),
        reviewers=logins,
    )


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

    # The board's queue entry for a mention, which is what its claim names.
    # Only a mention carries one.
    notification_id: str | None = None

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

    # The skills the board says this item is worked with, in the order they
    # should be offered. Empty means the board selected none — the runner has
    # none of its own to fall back on, and that is the intended answer.
    skills: tuple[SkillRef, ...] = ()

    # The prompt documents this run is built from, keyed by `work_task`,
    # `respond_task` or `respond_mention`. The board owns these outright; a
    # missing one fails the run rather than being guessed at.
    instructions: Mapping[str, str] = field(default_factory=dict)

    # Which harness the board wants this worked with. It overrides the
    # harness the install configured, as long as this install has that one
    # (`run.harness_for_work`); if it cannot run the named harness, it uses its
    # own rather than failing a run over a preference set on a machine the
    # board cannot see. None leaves the install's own harness in charge.
    harness: str | None = None
    model: str | None = None

    # The prompt the board's column composed for this item — the whole run
    # prompt, built from the same instruction documents and using the same tag
    # vocabulary as `instructions` above — and what that composition lets the
    # run do ("edit_code" or "research"). Both are None together, which is the
    # board saying it selected nothing: a runner reading these falls back to
    # `instructions`, which is exactly how it behaved before either existed.
    prompt: str | None = None
    mode: str | None = None

    # The board's or column's own instructions for the agent, filled into the
    # instruction document's {agent_instructions} tag.
    agent_instructions: str | None = None

    # What this step wants done with the branch the run pushes. A step that
    # said nothing gets the default policy, which is what every run did before
    # steps could ask.
    pr: PrPolicy = field(default_factory=PrPolicy)

    @classmethod
    def from_api(cls, payload: dict[str, Any]) -> WorkItem:
        """Build from a work-list payload, ignoring fields we don't model."""
        kind = payload.get("kind")
        return cls(
            task_id=str(payload["task_id"]),
            reference=payload.get("reference"),
            source_ref=None if payload.get("board_id") is None else str(payload["board_id"]),
            kind="mention" if kind == "mention" else "assigned",
            notification_id=payload.get("notification_id"),
            actor_name=payload.get("actor_name"),
            comment_excerpt=payload.get("comment_excerpt"),
            repo=payload.get("repo"),
            skills=tuple(
                SkillRef(
                    id=str(s["id"]),
                    slug=str(s["slug"]),
                    name=str(s.get("name") or ""),
                    updated_at=str(s.get("updated_at") or ""),
                )
                for s in payload.get("skills") or []
            ),
            instructions=dict(payload.get("instructions") or {}),
            harness=payload.get("harness"),
            model=payload.get("model"),
            prompt=payload.get("prompt"),
            mode=payload.get("mode"),
            agent_instructions=payload.get("agent_instructions"),
            pr=_pr_policy(payload.get("pr")),
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

    # Why the branch is not on origin, in git's own words — or the runner's
    # when it never asked git (a connection configured not to push, a working
    # copy with no origin). Empty when it was pushed. Carried because
    # `pushed=False` alone tells whoever reads the board that the work is
    # stuck, and nothing tells them what to do about it.
    push_detail: str = ""

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

    # What this run's forge tools authenticate and identify themselves as:
    # environment variables applied to the agent's own launch AND to every git
    # and `gh` command the run makes, so a push, a pull request and a comment
    # all come from the same identity. Lent by the source (a board that holds
    # app credentials of its own); empty when it has none to lend, which leaves
    # the run using whatever credential the machine already holds.
    #
    # Environment variables rather than a typed credential because the shape is
    # a forge's own, not the runner's: the source names what its forge's tools
    # read, and core carries it without knowing which forge that is.
    forge_env: Mapping[str, str] = field(default_factory=dict)


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

    # The board's PR-writing guidance, resolved once here (`run.execute`,
    # alongside the launch itself) and carried on the response rather than
    # looked up again wherever it is next needed. Delivery happens
    # controller-side (`run.deliver_all`), which for a sandboxed run is a
    # different machine from the one that ran the agent and materialised the
    # skill bundle — so by the time a sink wants this, the cache that would
    # answer the lookup may not even exist there. Carrying the resolved prose
    # is what makes it work on both paths; see `sandbox_protocol.RunResult`
    # for the wire crossing. Empty when the board sent no such skill.
    guidance: str = ""

    # What the sinks did with this run's deliverables, attached by the
    # controller (`runner.Listener._finish`) once they have all run. Carried
    # here for the same reason `guidance` is: it is resolved after the run and
    # the release needs it, so rather than making `Source.release` take a
    # second argument every source would have to accept, the one value that
    # already travels from the run to the release carries it. Empty until
    # delivery has happened, and on any run that never got that far.
    sink_results: tuple[SinkResult, ...] = ()

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

    # The run's forge credentials/identity, as environment variables (see
    # `Job.forge_env`). A sink runs controller-side and shells out to its own
    # forge's tools, so it needs the same identity the run itself used —
    # otherwise the branch is pushed by one actor and the pull request opened
    # by another. Empty when the source lent none.
    forge_env: Mapping[str, str] = field(default_factory=dict)

    # The board's PR-writing guidance, already resolved for this run. Carried
    # rather than looked up: a sink runs controller-side and may be on a
    # different machine from the run (the sandboxed execution path), where the
    # skill cache is cold. Carrying the resolved prose is what makes it work
    # on both paths — see `Response.guidance`, where this comes from.
    guidance: str = ""


@dataclass(frozen=True)
class PullRequestRef:
    """The pull request a delivery ended at, as the forge itself names it.

    The sink that opened or found it fills this in, because it is the one layer
    that knows what a pull request on its forge looks like. Everything above it
    reads the fields.
    """

    repo: str
    number: int
    url: str
    draft: bool = False

    # The GitHub logins actually requested for review — not the ones asked for.
    # A review request is best effort (a login that is not a collaborator is
    # refused), so this lists what took.
    reviewers: tuple[str, ...] = ()


@dataclass(frozen=True)
class SinkResult:
    """What a sink did, in terms the source can report without understanding it.

    ``pull_request`` names one forge concept in an otherwise sink-neutral
    contract, and it earns the exception: the source already understands pull
    requests specifically — it reports them to the board as a repository and a
    number — it just used to get there by pattern-matching ``url``. That could
    not say whether the pull request was a draft or who was asked to review it,
    and it read any other sink's URL that happened to look like one as a pull
    request. Being explicit is honest where being implicit was fragile.
    """

    sink: str
    ok: bool
    summary: str  # "opened PR", "deployed", "could not reach Netlify"
    url: str | None = None

    # The pull request this delivery ended at, when it ended at one. None for a
    # sink that opens none, and for a delivery whose step asked for none.
    pull_request: PullRequestRef | None = None


@dataclass(frozen=True)
class Claim:
    """A source's lock on one work item.

    A source that does not lock returns one whose release does nothing, so the
    run-lock lifecycle can be written once regardless of whether the source
    backing it locks at all.
    """

    work_id: str
    # Whatever the source needs to release it. Empty when the claim holds
    # nothing to release — a mention the board opened no responding run for.
    token: str = ""
