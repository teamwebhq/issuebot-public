"""The contract between the sandbox controller and the in-sandbox worker.

The controller (:mod:`issuebot.sandbox`) boots a sandbox, execs ``issuebot
run-one`` inside it, and parses what comes back. The worker
(:mod:`issuebot.worker`) is the other end of that same conversation. They speak
over three channels — argv, environment variables, and a sentinel line on
stdout.

Everything either end needs to know about the wire lives here, so the halves
cannot drift apart (ADR-0004). No source plugin's field names are spelled into
the wire: it carries *which* source and that source's own settings table,
opaque, so a second source rides it unchanged.

The wire is one value in each direction:

* :class:`WorkerEnv` — everything the controller tells the worker, encoded once
  and decoded once. Its :class:`BootMode` names the three ways a sandbox can
  start rather than leaving the third to be inferred from two absent flags, and
  its ``version`` names the released code the run must be: the sandbox runs
  *this* code, so the two ends being the same is a correctness property, not a
  tidiness one. :func:`version_argv`, :func:`parse_version` and
  :func:`update_argv` are the other half of that — how the controller asks a
  sandbox what it has, how it reads the answer, and how it fixes it.
* :class:`RunResult` — everything the worker tells the controller.

Nothing provider-specific belongs here. Secrets are the provider's business (see
:meth:`issuebot.sandbox.SandboxProvider.secret_env`); this module would otherwise
be spelling one vendor's template syntax on behalf of all of them.
"""

from __future__ import annotations

import json
import logging
import os
import re
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

import issuebot
from issuebot import release
from issuebot.config import Connection
from issuebot.context import RunnerContext
from issuebot.contracts import (
    Changes,
    Response,
    SkillRef,
    WorkItem,
    coerce_status,
    parse_outputs,
)
from issuebot.state import StateFile

logger = logging.getLogger("issuebot")

# The worker prints its result on stdout behind this marker, and also writes it
# to a file so a run cut short before the line is flushed can still be recovered.
RESULT_MARKER = "##ISSUEBOT-RESULT##"
RESULT_FILE = "/tmp/issuebot-result.json"  # noqa: S108 — ephemeral per-task container

# One stable release version, on a line of its own. What a probe's output is
# searched for.
_VERSION_RE = re.compile(r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)")


class BootMode(Enum):
    """What a sandbox woke up into.

    Three states, one value — each named rather than inferred from absent
    flags (ADR-0004).
    """

    # Nothing preinstalled: a fresh machine from the provider's base template.
    COLD = "cold"

    # The connection's shared project checkpoint: the repo is already cloned and
    # its dependencies installed, from whatever task populated it. The workspace
    # belongs to that other task and must be topped up for this one.
    WARM = "warm"

    # This task's own checkpoint, taken when it paused for human input. The
    # workspace is already this task's branch, mid-work.
    RESUME = "resume"


# --- the wire: controller → worker ----------------------------------------

_ENV_BOOT = "ISSUEBOT_BOOT"
_ENV_VERSION = "ISSUEBOT_VERSION"
_ENV_AGENT_ID = "ISSUEBOT_AGENT_ID"
_ENV_ACTOR_NAME = "ISSUEBOT_ACTOR_NAME"
_ENV_COMMENT_EXCERPT = "ISSUEBOT_COMMENT_EXCERPT"
# The whole config this run works under, narrowed to one connection by
# `config.sandbox_config`. The sandbox has no config file, so this is where its
# wiring comes from — not three named endpoints, which were one source plugin's
# field names spelled into the provider-neutral wire.
# Not `ISSUEBOT_CONFIG`: that name is already the config *path* override
# (`config.CONFIG_ENV`), and a sandbox that had both would point `load_config`
# at a JSON document where a file path belongs.
_ENV_CONFIG = "ISSUEBOT_WIRE_CONFIG"

# The board's skills and instruction documents, as one JSON object — like
# `_ENV_CONFIG`, structured data rather than a plain string, so it gets a
# channel of its own instead of being packed into one.
_ENV_WORK_CONTEXT = "ISSUEBOT_WORK_CONTEXT"

# The board's run preferences. Each is a plain string, so — like actor/excerpt
# above — they ride their own variables rather than joining the JSON blob.
_ENV_HARNESS = "ISSUEBOT_HARNESS"
_ENV_MODEL = "ISSUEBOT_MODEL"
_ENV_AGENT_INSTRUCTIONS = "ISSUEBOT_AGENT_INSTRUCTIONS"


@dataclass(frozen=True)
class WorkerEnv:
    """Everything the controller tells the worker, as one value.

    Encoded into the sandbox's environment at create time, because that is the
    only channel a sandbox has before it exists; decoded once inside. Board
    credentials are carried rather than assumed, so a sandbox image needs no
    config file of its own (ADR-0004).

    A whole config, narrowed to this run's connection — not three named
    endpoints. ``ISSUEBOT_API_URL``/``ISSUEBOT_MCP_URL``/``ISSUEBOT_PAT`` were
    one source plugin's field names spelled into the provider-neutral wire, so
    a second source could only ride it by having those three fields. The config
    travels whole and opaque, exactly as `RunnerContext.plugin_settings`
    carries plugin tables in process: what is in it is each plugin's own
    declaration, and this module reads none of it.
    """

    # The config the worker runs under: every plugin's global table plus this
    # run's one connection, built by `config.sandbox_config` — which is what
    # decides how narrow it is, and why. Empty means nobody sent one, which is
    # a hand-run `run-one` falling back to the config file on disk.
    config: Mapping[str, Any] = field(default_factory=dict)

    boot: BootMode = BootMode.COLD

    # Which released issuebot this run must be. The sandbox executes issuebot's
    # own code, so the controller states its version here and the worker refuses
    # to work as any other — the last check before the work, after the controller
    # has already brought the sandbox to this release. Empty means nobody said —
    # a hand-run `run-one` — and is left unchecked rather than refused.
    version: str = ""

    # The agent's own user id, so a mention session can self-assign.
    agent_id: str | None = None

    # Mention context. Not on the task record, so it cannot be re-fetched inside
    # the sandbox — it rides the wire or it is lost, which is exactly how it came
    # to be silently dropped on this path once before.
    actor_name: str | None = None
    comment_excerpt: str | None = None

    # The board's skills and prompt documents for this item. Not on the task
    # record either — like the mention context above, they only reach the
    # sandbox if this value carries them.
    skills: tuple[SkillRef, ...] = ()
    instructions: Mapping[str, str] = field(default_factory=dict)

    # The board's run preferences (see `WorkItem.harness`/`.model`) and its own
    # instructions for the agent. Requests, not settings this module acts on —
    # it only ever carries them through.
    harness: str | None = None
    model: str | None = None
    agent_instructions: str | None = None

    @classmethod
    def for_run(
        cls,
        ctx: RunnerContext,
        work: WorkItem,
        *,
        boot: BootMode,
        agent_id: str | None = None,
        config: Mapping[str, Any] | None = None,
    ) -> WorkerEnv:
        """What this run needs, drawn from the runner's own settings and item.

        ``config`` is what the worker will wire itself from — see
        :func:`~issuebot.config.sandbox_config`, which builds it and owns every
        decision about what a sandbox may and may not see. Nothing here reads
        inside it."""
        return cls(
            config=dict(config or {}),
            boot=boot,
            version=issuebot.__version__,
            agent_id=agent_id,
            actor_name=work.actor_name,
            comment_excerpt=work.comment_excerpt,
            skills=work.skills,
            instructions=work.instructions,
            harness=work.harness,
            model=work.model,
            agent_instructions=work.agent_instructions,
        )

    def encode(self) -> dict[str, str]:
        """The environment variables to bake into the sandbox.

        Optional values are omitted rather than sent empty, so the worker's own
        fallbacks stay in charge of defaults."""
        env = {
            _ENV_CONFIG: json.dumps(dict(self.config)),
            _ENV_BOOT: self.boot.value,
        }
        for key, value in (
            (_ENV_VERSION, self.version),
            (_ENV_AGENT_ID, self.agent_id),
            (_ENV_ACTOR_NAME, self.actor_name),
            (_ENV_COMMENT_EXCERPT, self.comment_excerpt),
            (_ENV_HARNESS, self.harness),
            (_ENV_MODEL, self.model),
            (_ENV_AGENT_INSTRUCTIONS, self.agent_instructions),
        ):
            if value:
                env[key] = str(value)
        if self.skills or self.instructions:
            env[_ENV_WORK_CONTEXT] = json.dumps(
                {
                    "skills": [asdict(s) for s in self.skills],
                    "instructions": dict(self.instructions),
                }
            )
        return env

    @classmethod
    def decode(cls, environ: dict[str, str] | None = None) -> WorkerEnv:
        """Read back what the controller sent. The exact inverse of :meth:`encode`.

        An unrecognised or missing boot mode reads as cold, which is the mode
        that assumes least about the machine; an unreadable config reads as
        empty, for the same reason — a hand-run ``run-one`` sends neither, and
        an empty config sends the worker to the file on disk."""
        env = os.environ if environ is None else environ
        try:
            boot = BootMode(env.get(_ENV_BOOT, BootMode.COLD.value))
        except ValueError:
            boot = BootMode.COLD

        try:
            config = json.loads(env.get(_ENV_CONFIG) or "{}")
        except json.JSONDecodeError:
            config = {}

        skills, instructions = _decode_work_context(env.get(_ENV_WORK_CONTEXT))

        return cls(
            config=config if isinstance(config, dict) else {},
            boot=boot,
            version=env.get(_ENV_VERSION, ""),
            agent_id=env.get(_ENV_AGENT_ID),
            actor_name=env.get(_ENV_ACTOR_NAME),
            comment_excerpt=env.get(_ENV_COMMENT_EXCERPT),
            skills=skills,
            instructions=instructions,
            harness=env.get(_ENV_HARNESS),
            model=env.get(_ENV_MODEL),
            agent_instructions=env.get(_ENV_AGENT_INSTRUCTIONS),
        )

    def work_item(self, *, task_id: str, reference: str | None, kind: str) -> WorkItem:
        """Rebuild the work item inside the sandbox.

        The task id comes from argv and the reference from the task record;
        everything that is on neither comes from this value."""
        return WorkItem(
            task_id=task_id,
            reference=reference,
            kind="mention" if kind == "mention" else "assigned",
            actor_name=self.actor_name,
            comment_excerpt=self.comment_excerpt,
            skills=self.skills,
            instructions=self.instructions,
            harness=self.harness,
            model=self.model,
            agent_instructions=self.agent_instructions,
        )


def _decode_work_context(raw: str | None) -> tuple[tuple[SkillRef, ...], Mapping[str, str]]:
    """Parse `_ENV_WORK_CONTEXT`, degrading to empty on anything unreadable.

    Absent, malformed JSON, or a shape that isn't the one `encode` writes all
    read the same way: the sandbox rebuilds what it was told and no more,
    matching how an unreadable `_ENV_SOURCE_SETTINGS` degrades above."""
    try:
        context = json.loads(raw or "{}")
        skills = tuple(
            SkillRef(
                id=str(s["id"]),
                slug=str(s["slug"]),
                name=str(s.get("name") or ""),
                updated_at=str(s.get("updated_at") or ""),
            )
            for s in context.get("skills") or []
        )
        instructions = dict(context.get("instructions") or {})
    except (json.JSONDecodeError, TypeError, KeyError, AttributeError):
        return (), {}
    return skills, instructions


def worker_argv(work: WorkItem, *, run_id: str, connection: Connection) -> list[str]:
    """The ``issuebot run-one`` invocation for this work item.

    ``--kind`` carries the work kind through verbatim, so the worker resolves
    the same policy the controller did. A new kind of work needs no change
    here."""
    return [
        "issuebot",
        "run-one",
        "--task",
        work.task_id,
        "--run-id",
        run_id,
        "--connection",
        connection.key,
        "--kind",
        work.kind,
    ]


def version_argv() -> list[str]:
    """Ask a sandbox which released issuebot it has installed."""
    return ["issuebot", "version"]


def parse_version(lines: Iterable[str]) -> str:
    """Find one stable version on a line of its own in provider output."""
    return next((line.strip() for line in lines if _VERSION_RE.fullmatch(line.strip())), "")


def update_argv(version: str) -> list[str]:
    """Install the controller's exact GitHub Release in a sandbox."""
    return release.installer_argv(version)


@dataclass(frozen=True)
class RunResult:
    """What the worker reports back, and what the controller parses.

    A whole :class:`~issuebot.contracts.Response`, encoded: the worker runs the
    same pipeline a local run does, so what it produced is exactly what a local
    run would have — including the ``Changes`` its workspace derived and the
    ``outputs`` the agent authored. Carrying only the status (all this could
    hold while nothing inside a sandbox produced either) would have meant a
    sandboxed hand-off, answer or PR silently evaporating at the wire.

    ``changes``/``outputs`` stay plain JSON here rather than their real types:
    this value is what crosses a process boundary, so it is untrusted until
    :meth:`to_response` narrows it.
    """

    status: str
    result_text: str = ""
    session_id: str | None = None
    changes: dict[str, Any] | None = None
    outputs: list[dict[str, Any]] = field(default_factory=list)

    # `Response.guidance` -- the board's PR-writing skill, resolved inside the
    # sandbox where the skill bundle is warm. It has to ride this wire: the
    # controller's own delivery step runs on a different machine, with no
    # warm cache of its own to resolve it from instead.
    guidance: str = ""

    @classmethod
    def from_response(cls, response: Response) -> RunResult:
        """Wrap a response for the trip back to the controller."""
        return cls(
            status=response.status,
            result_text=response.result_text,
            session_id=response.session_id,
            changes=asdict(response.changes) if response.changes is not None else None,
            outputs=[output.model_dump(mode="json") for output in response.outputs],
            guidance=response.guidance,
        )

    def _derived_changes(self) -> Changes | None:
        """The changes the far side derived, or None when none survived the trip.

        A shape we cannot rebuild is dropped rather than raised on: a sink then
        refuses with "no pushed changes to open a PR from", a visible and
        ordinary outcome, where a raise would take the listener down with it."""
        if not isinstance(self.changes, dict):
            return None
        try:
            return Changes(**self.changes)
        except TypeError:
            logger.warning("unreadable changes from the sandbox worker: %r", self.changes)
            return None

    def to_response(self) -> Response:
        """Unwrap into a response, narrowing everything that crossed the wire.

        ``status`` is a plain string until it is checked: it arrived as JSON
        over a pipe, and an older or malformed worker could otherwise land an
        unknown value in a ``Literal``-typed field.

        Unreadable ``outputs`` fail the run outright rather than degrading to
        none. The two states mean opposite things — "the agent deliberately
        reported nothing" and "we lost what it reported" — and a ``done``
        response whose hand-off quietly vanished would release as a success
        that dropped a decision.

        The failure carries the parse error and says whether a branch was
        pushed, because it is `result_text` — not the log — that reaches
        someone watching ``issuebot listen``, and skew between a controller and
        a sandbox is still the likeliest thing behind it: the controller aligns
        the sandbox to its own release before any work starts
        (:meth:`issuebot.sandbox.SandboxEnvironment._align_version`), which makes
        this unreachable *when both ends can name themselves* — and a build that
        cannot is exactly where it would fire. A pushed
        branch is named rather than delivered: the run is failed, so `_finish`
        returns before any sink sees it, and nothing else would ever mention
        the work sitting on the remote."""
        try:
            outputs = parse_outputs(json.dumps({"outputs": self.outputs}))
        except ValueError as exc:
            logger.warning("unreadable outputs from the sandbox worker: %s", exc)
            changes = self._derived_changes()
            orphaned = (
                f"; branch '{changes.branch}' was pushed and is not being delivered"
                if changes is not None and not changes.empty
                else ""
            )
            return Response(
                status="failed",
                result_text=f"could not read the sandbox worker's outputs: {exc}{orphaned}",
                session_id=self.session_id,
            )

        return Response(
            status=coerce_status(self.status),
            changes=self._derived_changes(),
            outputs=outputs,
            result_text=self.result_text,
            session_id=self.session_id,
            guidance=self.guidance,
        )

    def to_json(self) -> str:
        return json.dumps(asdict(self))

    def sentinel_line(self) -> str:
        """The stdout line the controller watches for."""
        return f"{RESULT_MARKER} {self.to_json()}"

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> RunResult:
        """Build from a parsed payload, tolerating missing and unknown fields."""
        changes = payload.get("changes")
        outputs = payload.get("outputs")
        return cls(
            status=str(payload.get("status") or "failed"),
            result_text=payload.get("result_text") or "",
            session_id=payload.get("session_id"),
            changes=changes if isinstance(changes, dict) else None,
            outputs=outputs if isinstance(outputs, list) else [],
            guidance=payload.get("guidance") or "",
        )

    @classmethod
    def parse_json(cls, text: str) -> RunResult | None:
        """Parse the result file's contents, or None if it isn't usable."""
        try:
            payload = json.loads(text)
        except ValueError:
            return None
        return cls.from_payload(payload) if isinstance(payload, dict) else None


def parse_sentinel(line: str) -> RunResult | None:
    """The result carried by a stdout line, or None if it isn't the sentinel.

    A malformed sentinel reads as "no result", which falls the controller
    through to the result-file recovery path rather than inventing an outcome.
    """
    if not line.startswith(RESULT_MARKER):
        return None
    return RunResult.parse_json(line[len(RESULT_MARKER) :].strip())


def write_result_file(result: RunResult, path: str = RESULT_FILE) -> None:
    """Leave the result on disk as the controller's fallback recovery path.

    Written atomically: the controller reads this file after the worker's
    process has gone, and a half-written one would parse as no result at all."""
    StateFile(Path(path)).write_text(result.to_json())
