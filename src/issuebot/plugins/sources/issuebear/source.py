"""The Issuebear board as a :class:`~issuebot.plugins.sources.base.Source`:
discover, claim, narrate, apply decisions, finish — the whole lifecycle of one
work item, built on top of the thin REST client in ``client.py``.
"""

from __future__ import annotations

import logging
from typing import Any, ClassVar, Protocol

from issuebot import plugins
from issuebot.config import (
    Config,
    Connection,
    conn_setting,
    global_settings,
    maybe_executor_name,
)
from issuebot.contracts import (
    Claim,
    Handoff,
    McpServer,
    NeedsInput,
    Output,
    OutputKind,
    Response,
    SinkResult,
    WorkItem,
)
from issuebot.plugins.sources.base import Source
from issuebot.plugins.sources.issuebear import messages, prompts
from issuebot.plugins.sources.issuebear.client import AlreadyClaimed, IssuebotClient
from issuebot.plugins.workspaces.base import WorkspaceProblem
from issuebot.transient import describe_transient, is_transient

logger = logging.getLogger("issuebot")

# What each kind of work may report back — this source's own judgement about
# its work kinds, replacing `WorkPolicy`'s `_ASSIGNMENT_PERMITS`/
# `_MENTION_PERMITS` table. An assignment may report anything; a mention's
# session never touches the workspace, so it cannot produce `changes`.
_ASSIGNMENT_PERMITS: frozenset[OutputKind] = frozenset(
    {"changes", "answer", "needs_input", "handoff"}
)
_MENTION_PERMITS: frozenset[OutputKind] = frozenset({"answer", "needs_input", "handoff"})


class _Client(Protocol):
    """The slice of `IssuebotClient` this source actually calls.

    A structural type rather than `IssuebotClient` itself, so a test double
    needs only these methods."""

    def wait_for_work(
        self, *, timeout: int = ..., board_id: str | None = ...
    ) -> list[dict[str, Any]]: ...
    def claim(
        self, task_id: str, *, install_id: str | None = ..., executor: str | None = ...
    ) -> dict[str, Any]: ...
    def release(self, run_id: str, *, status: str = ..., note: str | None = ...) -> None: ...
    def add_comment(self, task_id: str, body: str) -> dict[str, Any]: ...
    def update_task(self, task_id: str, **fields: Any) -> dict[str, Any]: ...
    def heartbeat(self, run_id: str) -> None: ...


class Issuebear(Source):
    """One board connection's whole work-item lifecycle, over `IssuebotClient`."""

    name: ClassVar[str] = "issuebear"

    def __init__(
        self,
        client: _Client,
        *,
        board: str,
        connection: Connection,
        mcp_url: str,
        pat: str,
        install_id: str | None = None,
        agent_id: str | None = None,
        **_settings: object,
    ) -> None:
        """Bind this source to one board connection.

        ``client`` is duck-typed against `IssuebotClient` rather than typed
        against it directly, so a test double needs only the methods actually
        called. ``agent_id``/``install_id`` are resolved once per listener
        (from the ``connect()`` response and install registration
        respectively) and stay fixed for this source's lifetime, exactly like
        ``connection`` itself.

        ``mcp_url``/``pat`` are named out of this plugin's own `[issuebear]`
        table because :meth:`agent_access` builds the launch's board MCP from
        them — the endpoint and credential a *run* needs, as opposed to the
        install-wide handle's (:meth:`client`, :meth:`user_mcp`, which read the
        same table through `global_settings`). Required, not defaulted: a source
        that cannot describe its own board to an agent would launch one with no
        way to read the task, and would do it silently.

        ``**_settings`` absorbs the rest of the table `runner.source_for` splats
        in (`api_url`, `install_name`, `telemetry_interval_seconds`) — the same
        accommodation every sink makes for its own table.
        """
        self._client = client
        self._board = board
        self._connection = connection
        self._mcp_url = mcp_url
        self._pat = pat
        self._install_id = install_id
        self._agent_id = agent_id

    # -- the install-wide handle ---------------------------------------------

    @classmethod
    def client(cls, cfg: Config) -> IssuebotClient:
        """A REST client bound to the configured board API and agent PAT.

        The one place a `Config` becomes a board client, so every command — and
        every listener, which is handed this same object as ``client`` — reaches
        the board the same way."""
        return IssuebotClient.from_config(cfg)

    @classmethod
    def board_mcp(cls, *, mcp_url: str, pat: str) -> McpServer:
        """This board as an MCP server the agent calls as the agent PAT.

        The one place the board's wire format lives: an http transport, this
        plugin's own name, and a bearer header. Two callers, two lifetimes —
        :meth:`user_mcp` registers it once against the human's own tooling,
        :meth:`agent_access` hands it to a single launch — and they describe the
        same server, so they describe it once."""
        return McpServer(
            name=cls.name,
            type="http",
            url=mcp_url,
            headers={"Authorization": f"Bearer {pat}"},
        )

    @classmethod
    def user_mcp(cls, cfg: Config) -> McpServer:
        """The board as an MCP server for the user's own agent tooling.

        The same endpoint and bearer token an autonomous launch is given
        per-run, described once for a harness to register globally — so "how do
        you reach this board over MCP" is answered here rather than by whichever
        harness happens to offer such a registration."""
        settings = global_settings(cfg, plugins.get("sources", cls.name))
        return cls.board_mcp(mcp_url=settings.mcp_url, pat=settings.pat)

    # -- discover / claim / release -----------------------------------------

    def poll(self, *, timeout: int) -> list[WorkItem]:
        """Long-poll for work on this connection's board, already scoped to it.

        ``/me/work`` is agent-wide, so items are filtered again here even
        though ``wait_for_work`` is also asked to scope server-side — belt
        and braces against a server that doesn't."""
        payloads = self._client.wait_for_work(timeout=timeout, board_id=self._board)
        items = [WorkItem.from_api(p) for p in payloads]
        return [item for item in items if item.for_source_ref(self._board)]

    def claim(self, work: WorkItem) -> Claim | None:
        """Take this task's run lock, or hand back the board's own non-locking
        "responding" run for a mention.

        A mention is never a race to win — the board already delivered its
        run — so this never returns ``None`` for one; its claim simply carries
        no token when the board sent no responding run (an older server), and
        ``release`` treats that as nothing to release. A locking claim
        genuinely lost (``AlreadyClaimed``) and a transient failure (network,
        gateway) both return ``None``: the item is redelivered by the next
        ``poll`` either way, so the caller need not tell them apart.
        """
        if work.kind == "mention":
            return Claim(work_id=work.task_id, token=work.run_id)

        try:
            result = self._client.claim(
                work.task_id,
                install_id=self._install_id,
                # The resolved name, so the board is told which environment ran
                # the work even when the config left it to the one installed.
                executor=maybe_executor_name(self._connection),
            )
        except AlreadyClaimed:
            return None
        except Exception as exc:  # noqa: BLE001 - claim failures are retried, not raised
            if is_transient(exc):
                logger.info(
                    "claim deferred for %s (%s); will retry", work.ref, describe_transient(exc)
                )
            else:
                logger.warning("claim failed for %s", work.ref, exc_info=True)
            return None

        return Claim(work_id=work.task_id, token=result["run_id"])

    def release(self, claim: Claim, response: Response) -> None:
        """Release the run lock, reporting how the run went.

        A no-op when this claim carries no token — a mention the board never
        opened a responding run for has nothing to release."""
        if not claim.token:
            return
        status = "done" if response.status == "done" else "failed"
        self._client.release(claim.token, status=status, note=response.result_text or None)

    # -- narration / decisions / finish --------------------------------------

    def say(self, work: WorkItem, message: str) -> None:
        """Post one message to the task, in the runner's own voice."""
        self._client.add_comment(work.task_id, messages.say(message))

    def apply(self, work: WorkItem, decision: Output) -> None:
        """Apply a decision to the task: hand it off, or mark it awaiting input.

        The state change only. Announcing it as well put the agent's own
        question or hand-off note on the thread a second time, in the runner's
        voice, directly under the comment the agent had just written — the
        agent is told that comments are its only channel to people, and it uses
        it. Where the task went next is what `status`/`assignee` are for, and
        the board shows those without anybody narrating them.
        """
        if isinstance(decision, Handoff):
            self._client.update_task(work.task_id, assignee_id=decision.assignee)
        elif isinstance(decision, NeedsInput):
            self._client.update_task(work.task_id, status="needs_input")

    def finish(self, work: WorkItem, response: Response, results: list[SinkResult]) -> None:
        """Report anything about the run the agent could not have reported
        itself — and stay quiet when there is nothing (see
        :func:`messages.summarize`)."""
        message = messages.summarize(response, results)
        if message is not None:
            self.say(work, message)

    # -- what a run may do, and how it launches ------------------------------

    def permits(self, work: WorkItem) -> frozenset[OutputKind]:
        """This source's judgement about its own work kinds: an assignment
        may report anything; a mention's session never touches the workspace,
        so it cannot produce `changes` — and neither can a connection whose
        `mode` is `"respond"`, whatever kind of work arrived.

        `mode` folds in here rather than as a second field on the ABC:
        `permits` is this source's judgement, and mode is part of that
        judgement, not a second axis (ADR-0011).
        """
        kind_permits = _MENTION_PERMITS if work.kind == "mention" else _ASSIGNMENT_PERMITS
        if conn_setting(self._connection, "mode", "build") == "respond":
            return kind_permits & _MENTION_PERMITS  # bars `changes`, same restriction as a mention
        return kind_permits

    def prompt(
        self,
        work: WorkItem,
        connection: Connection,
        *,
        permits: frozenset[OutputKind],
        problem: WorkspaceProblem | None = None,
    ) -> str:
        """Render the launch prompt for this work item.

        ``permits`` is the job's own latitude — this source's judgement already
        intersected with what the connection's workspace can produce — so the
        agent is only ever offered output kinds the run can actually deliver.
        Required, with no fallback to :meth:`permits`: that fallback *is* the
        bug this argument exists to close, and leaving it available as a default
        leaves it one forgotten keyword away.

        A workspace ``problem`` (a diverged branch) prepends the reconcile
        preamble: the agent settles the divergence in-workspace before the
        task, and the runner's final push stays plain (never forced)."""
        if work.kind == "mention":
            rendered = prompts.render_mention_prompt(
                reference=work.ref,
                actor_name=work.actor_name or "someone",
                comment_excerpt=work.comment_excerpt or "",
                agent_id=self._agent_id or "",
                permits=permits,
            )
        else:
            rendered = prompts.render_work_prompt(
                reference=work.ref,
                done=conn_setting(connection, "done", "review"),
                confirm=conn_setting(connection, "confirm", True),
                mode=conn_setting(connection, "mode", "build"),
                permits=permits,
            )

        if problem is not None:
            rendered = prompts.render_reconcile_preamble(problem) + rendered
        return rendered

    def agent_access(self, work: WorkItem) -> tuple[McpServer, ...]:
        """The board itself, so the agent can read the task and narrate.

        This source's server, described by this source, carried by the hook
        that exists for it — the same one a second source would use without
        core learning anything new."""
        return (self.board_mcp(mcp_url=self._mcp_url, pat=self._pat),)

    def heartbeat(self, run_id: str) -> None:
        """Keep a run's lock alive while it is in flight.

        Overrides the ABC's no-op: this board leases the run lock, so a run
        that stops heartbeating is a run the board hands to someone else."""
        self._client.heartbeat(run_id)
