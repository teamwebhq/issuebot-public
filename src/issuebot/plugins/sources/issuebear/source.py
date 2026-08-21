"""The Issuebear board as a :class:`~issuebot.plugins.sources.base.Source`:
discover, claim, narrate, apply decisions, finish — the whole lifecycle of one
work item, built on top of the thin REST client in ``client.py``.
"""

from __future__ import annotations

import logging
import uuid
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


def _match_member(assignee: str, members: list[dict[str, Any]]) -> str | None:
    """The ``user_id`` of the one board member ``assignee`` names, or ``None``.

    ``assignee`` is whatever the agent wrote, so it is matched leniently —
    trimmed and case-folded — against each member's display ``name`` and
    against its ``user_id``. Exactly one member has to match: a value naming
    nobody, and a name two members share, both resolve to ``None`` so the
    caller leaves the task alone rather than guessing which person was meant.
    """
    wanted = assignee.strip().casefold()

    def names(member: dict[str, Any]) -> set[str]:
        """Everything one roster entry answers to, matched the same lenient way."""
        return {str(member.get(field, "")).strip().casefold() for field in ("name", "user_id")}

    matched = {str(member["user_id"]) for member in members if wanted in names(member)}

    return matched.pop() if len(matched) == 1 else None


def _display_name(user_id: str, members: list[dict[str, Any]]) -> str:
    """The display name of the board member with ``user_id``, or ``""``.

    The mirror of :func:`_match_member`: that turns what an agent wrote into
    an id the board accepts, this turns an id the board gave us back into
    something worth showing a person. An id the roster does not carry — a
    member who left, a roster the board would not serve — answers ``""``, and
    every caller falls back to the id itself.
    """
    for member in members:
        if str(member.get("user_id", "")) == user_id:
            return str(member.get("name") or "")

    return ""


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
    def list_board_members(self, board_id: str) -> list[dict[str, Any]]: ...
    def get_task(self, task_id: str) -> dict[str, Any]: ...


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
            assignee_id = self._assignee_id(work, decision.assignee)

            # A hand-off to ourselves parks the task where nothing moves it, so
            # it goes back to whoever asked for the work instead.
            if assignee_id is not None and assignee_id == self._agent_id:
                assignee_id = self._redirect_self_handoff(work)

            # An unresolved hand-off has already been reported on the task; the
            # assignee stays as it is rather than being patched with a guess.
            if assignee_id is not None:
                self._client.update_task(work.task_id, assignee_id=assignee_id)

        elif isinstance(decision, NeedsInput):
            self._client.update_task(work.task_id, status="needs_input")

    def _assignee_id(self, work: WorkItem, assignee: str) -> str | None:
        """The board user id a hand-off's ``assignee`` names, or ``None``.

        ``PATCH /tasks/{id}`` wants a user id, but an agent writes whatever it
        knows the person by — usually their display name. A value that already
        parses as a UUID is the id itself and passes straight through, so a
        hand-off that carries one costs no extra board call; anything else
        sends for the board roster once and looks it up there
        (:func:`_match_member`).

        A value that matches nobody, or more than one person, is reported on
        the task and answered with ``None``. Raising here would also cost the
        run its closing report, because `runner._finish` calls `apply` before
        it reports.
        """
        try:
            uuid.UUID(assignee)
        except ValueError:
            pass
        else:
            return assignee

        members = self._roster()
        matched = _match_member(assignee, members)
        if matched is not None:
            return matched

        logger.warning("hand-off of %s names no board member: %r", work.ref, assignee)
        names = [str(member.get("name") or "?") for member in members]
        self.say(work, messages.unresolved_assignee(assignee, names))

        return None

    def _roster(self) -> list[dict[str, Any]]:
        """This board's members, or an empty roster when the board cannot say.

        Every caller reads the roster to *improve* what it does — put an id to
        a name, a name to an id — and none of them is worth failing a run
        over, so a board that will not answer is logged and answered with
        nothing. Each caller already handles an id it cannot resolve, because
        a roster that answers may still not carry the person asked about.
        """
        try:
            return self._client.list_board_members(self._board)
        except Exception:  # noqa: BLE001 - a roster we cannot read degrades the run, never ends it
            logger.warning("could not read the members of board %s", self._board, exc_info=True)
            return []

    def _requester(
        self, work: WorkItem, members: list[dict[str, Any]] | None = None
    ) -> tuple[str, str]:
        """Who asked for this task: their board user id and display name, or
        ``("", "")`` when the board cannot say.

        One `get_task`, because the work item the board delivers carries no
        requester. Both features that need one come through here — the launch
        prompt's identity block, and a hand-off the agent aimed at itself — so
        a task is read once per use and "what does `requester_id` mean" is
        answered in one place.

        Pass ``members`` when the caller already holds the roster (a launch
        reads it for the agent's own name as well), so the launch costs one
        roster fetch rather than two.
        """
        try:
            task = self._client.get_task(work.task_id)
        except Exception:  # noqa: BLE001 - a task we cannot read degrades the run, never ends it
            logger.warning("could not read task %s to find its requester", work.ref, exc_info=True)
            return "", ""

        requester_id = str(task.get("requester_id") or "")
        if not requester_id:
            return "", ""

        roster = self._roster() if members is None else members

        return requester_id, _display_name(requester_id, roster)

    def _redirect_self_handoff(self, work: WorkItem) -> str | None:
        """Where a hand-off the agent aimed at itself goes instead: the task's
        requester, or nowhere.

        The agent is the one thing on the board that cannot receive a hand-off
        from itself — the session that would pick the task up is the one
        ending — so the person who asked for the work gets it back. With
        nobody to send it to (a task the board records no requester for, or
        one this agent requested itself) the task keeps the assignee it has.

        Either way one comment says what happened, and nothing raises:
        `runner._finish` calls `apply` before it reports, so an exception here
        would cost the run its closing report.
        """
        requester_id, requester_name = self._requester(work)

        if requester_id and requester_id != self._agent_id:
            logger.info("hand-off of %s named this agent; redirecting to its requester", work.ref)
            self.say(work, messages.self_handoff(requester_name or requester_id))
            return requester_id

        logger.warning("hand-off of %s named this agent and it has no requester", work.ref)
        self.say(work, messages.self_handoff(None))

        return None

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
            # One roster read serves both halves of the identity block: the
            # agent's own display name, and the requester's.
            members = self._roster()
            requester_id, requester_name = self._requester(work, members)

            rendered = prompts.render_work_prompt(
                reference=work.ref,
                done=conn_setting(connection, "done", "review"),
                confirm=conn_setting(connection, "confirm", True),
                mode=conn_setting(connection, "mode", "build"),
                permits=permits,
                agent_name=_display_name(self._agent_id, members) if self._agent_id else "",
                agent_id=self._agent_id or "",
                requester_name=requester_name,
                requester_id=requester_id,
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
