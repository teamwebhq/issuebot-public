"""Thin REST client for the Issuebear agent work contract + the few task
endpoints the supervisor needs, authenticated with the agent PAT.

Transient-failure classification (``is_transient``/``describe_transient``/
``log_poll_failure``) lives in :mod:`issuebot.transient` because none of it is
issuebear-specific — see that module's docstring."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import httpx

from issuebot.plugins.sources.base import ConnectionConflict

if TYPE_CHECKING:
    from issuebot.agent_state import ConnectionSnapshot

__all__ = ["AlreadyClaimed", "ApiError", "ConnectionConflict", "IssuebotClient", "project_repo"]

# Sentinel distinguishing "body wasn't valid JSON" from a legitimate ``None`` body.
_UNPARSEABLE = object()


def project_repo(project: dict[str, Any]) -> str | None:
    """The HTTPS clone URL of the repository a project is linked to, if any.

    ``clone_url`` only. Every run environment is given GitHub credentials for
    the ``gh`` CLI, which authenticates an HTTPS remote; none of them is given
    an SSH key or a known-hosts entry, so ``ssh_url`` names a remote that
    cannot be cloned. A Railway sandbox holds ``GH_TOKEN`` and nothing else.

    A board server that sends no ``clone_url`` reads the same as 'not linked':
    the connection keeps whatever repo it was configured with, and the wizard
    asks.
    """
    return (project.get("github_repo") or {}).get("clone_url") or None


def _now_iso() -> str:
    """The current UTC time in the ISO form the execution endpoint stores."""
    return datetime.now(UTC).isoformat()


class ApiError(Exception):
    """Raised on any unexpected 4xx/5xx response from the API."""

    def __init__(self, status: int, detail: str) -> None:
        self.status = status
        self.detail = detail
        super().__init__(f"{status}: {detail}")


class AlreadyClaimed(Exception):
    """Raised on a 409 from claim — another listener won the task.

    Stays here, unlike :class:`~issuebot.plugins.sources.base.ConnectionConflict`
    which moved beside the ABC: core never catches this one. `Source.claim`
    already promises ``None`` for a claim not taken, whatever the reason, so
    losing a race is answered inside this plugin and never named outside it.
    """


class IssuebotClient:
    """REST client for the Issuebear agent work contract.

    Wraps an ``httpx.Client`` configured with the agent PAT as a Bearer token.
    A transport can be injected so tests drive it with ``httpx.MockTransport``.
    """

    def __init__(
        self,
        *,
        api_url: str,
        pat: str,
        install_name: str | None = None,
        telemetry_interval: float = 15.0,
        transport: httpx.BaseTransport | None = None,
        timeout: float = 30.0,
    ) -> None:
        """Build the client bound to ``api_url`` and authenticated with ``pat``.

        ``install_name`` and ``telemetry_interval`` are this plugin's own
        settings, answering the two install-wide questions
        :class:`~issuebot.plugins.sources.base.SourceClient` asks of whichever
        source is installed. They default, so a caller that only wants to reach
        the API (``doctor``, the wizard) supplies neither."""
        self._install_name = install_name
        self.telemetry_interval = telemetry_interval
        self._http = httpx.Client(
            base_url=api_url.rstrip("/"),
            headers={"Authorization": f"Bearer {pat}"},
            transport=transport,
            timeout=timeout,
        )

    @classmethod
    def from_config(cls, cfg: Any) -> IssuebotClient:
        """A client bound to the configured board API and agent PAT.

        Reached through `Issuebear.client`, which is what core asks for.
        `issuebot.config` is imported inside the call because it imports the
        plugin registry, which imports this module — at call time both already
        exist."""
        from issuebot import plugins
        from issuebot.config import global_settings

        settings = global_settings(cfg, plugins.get("sources", "issuebear"))
        return cls(
            api_url=settings.api_url,
            pat=settings.pat,
            install_name=settings.install_name,
            telemetry_interval=settings.telemetry_interval_seconds,
        )

    def close(self) -> None:
        """Close the underlying HTTP connection pool."""
        self._http.close()

    def _json(self, resp: httpx.Response) -> Any:
        """Return the parsed JSON body, raising :class:`ApiError` on failure.

        Tolerates non-JSON bodies (gateway error pages, proxy timeouts): rather
        than letting ``json.JSONDecodeError`` escape, it raises a clean
        :class:`ApiError` carrying the raw text.
        """
        if resp.status_code >= 400:
            body = self._maybe_json(resp)
            detail = body.get("detail", resp.text) if isinstance(body, dict) else resp.text
            raise ApiError(resp.status_code, str(detail))

        if not resp.content:
            return None

        body = self._maybe_json(resp)
        if body is _UNPARSEABLE:
            raise ApiError(resp.status_code, f"non-JSON response body: {resp.text[:200]!r}")
        return body

    @staticmethod
    def _maybe_json(resp: httpx.Response) -> Any:
        """Parse the body as JSON, returning the ``_UNPARSEABLE`` sentinel if it isn't."""
        try:
            return resp.json()
        except ValueError:
            return _UNPARSEABLE

    # --- agent work contract -------------------------------------------------

    def connect(
        self, board_id: str, name: str | None = None, install_id: str | None = None
    ) -> dict[str, Any]:
        """Connect this agent to a board (POST /boards/{id}/agent-connection).

        Sends the connection's display ``name`` and ``install_id`` so the
        dashboard can label and group it. Raises :class:`ConnectionConflict`
        on a 409 — the agent is already connected to that board.

        The response echoes the calling agent's own identity under ``agent``
        (resolved by the board from the PAT), so the runner can learn its user id
        from this call without a separate ``GET /me``.
        """
        resp = self._http.post(
            f"/boards/{board_id}/agent-connection",
            json={"name": name, "install_id": install_id},
        )
        if resp.status_code == 409:
            raise ConnectionConflict(board_id, agent_id=resp.headers.get("X-Parade-Agent-Id"))
        return self._json(resp)

    def disconnect(self, board_id: str) -> None:
        """Disconnect this agent from a board (DELETE /boards/{id}/agent-connection)."""
        self._json(self._http.delete(f"/boards/{board_id}/agent-connection"))

    def get_my_work(self, *, board_id: str | None = None) -> list[dict[str, Any]]:
        """Return the list of tasks currently waiting for this agent.

        Pass ``board_id`` to restrict results to a single board.
        """
        params = {"board_id": board_id} if board_id is not None else None
        return self._json(self._http.get("/me/work", params=params))

    def wait_for_work(
        self, *, timeout: int = 25, board_id: str | None = None
    ) -> list[dict[str, Any]]:
        """Long-poll for work; return the task list, or ``[]`` on a 204.

        Pass ``board_id`` to restrict the poll to a single board.
        """
        params: dict[str, Any] = {"timeout": timeout}
        if board_id is not None:
            params["board_id"] = board_id

        resp = self._http.get("/me/work/wait", params=params, timeout=timeout + 10)
        if resp.status_code == 204:
            return []

        return self._json(resp)

    def claim(
        self, task_id: str, *, install_id: str | None = None, executor: str | None = None
    ) -> dict[str, Any]:
        """Claim ``task_id`` for this agent, returning ``{run_id, task_id}``.

        Optionally reports the owning install and executor kind so cloud runs
        are observable on the board. Raises :class:`AlreadyClaimed` if another
        listener already won it.
        """
        body: dict[str, str] = {}
        if install_id:
            body["install_id"] = install_id
        if executor:
            body["executor"] = executor
        resp = self._http.post(f"/tasks/{task_id}/claim", json=body or None)
        if resp.status_code == 409:
            raise AlreadyClaimed(task_id)

        return self._json(resp)

    def patch_execution(self, run_id: str, **fields: Any) -> None:
        """Patch a run's sandbox execution metadata (executor, sandbox_id,
        sandbox_session_name, sandbox_status, sandbox_created_at,
        sandbox_destroyed_at). Only non-None fields are sent."""
        body = {k: v for k, v in fields.items() if v is not None}
        self._json(self._http.post(f"/agent-runs/{run_id}/execution", json=body))

    # -- the SandboxLifecycle capability --------------------------------------
    #
    # `plugins.sources.base.SandboxLifecycle`, implemented by translation: the
    # controller speaks issuebot's own vocabulary, and the executor/sandbox_*
    # column names and the timestamps are this board's schema, spelled only
    # here.

    def sandbox_started(self, run_id: str, *, environment: str, sandbox_id: str) -> None:
        """Record that this run now executes in ``sandbox_id``."""
        self.patch_execution(
            run_id,
            executor=environment,
            sandbox_id=sandbox_id,
            sandbox_status="running",
            sandbox_created_at=_now_iso(),
        )

    def sandbox_destroyed(self, run_id: str) -> None:
        """Record that this run's sandbox has been torn down."""
        self.patch_execution(run_id, sandbox_status="destroyed", sandbox_destroyed_at=_now_iso())

    def heartbeat(self, run_id: str) -> None:
        """Send a liveness heartbeat for the given agent run."""
        self._json(self._http.post(f"/agent-runs/{run_id}/heartbeat"))

    def release(self, run_id: str, *, status: str = "done", note: str | None = None) -> None:
        """Release the agent run, reporting ``status`` and an optional ``note``."""
        self._json(
            self._http.post(
                f"/agent-runs/{run_id}/release",
                json={"status": status, "note": note},
            )
        )

    # --- telemetry + commands (clanker dashboard) ----------------------------

    def report_telemetry(
        self,
        *,
        version: str,
        install_id: str,
        hostname: str | None,
        connections: list[ConnectionSnapshot],
    ) -> None:
        """Report this install's live per-connection state to the dashboard.

        Translation happens here: the runner's snapshot vocabulary (``board``,
        ``phase``) becomes this board's wire schema (``board_id``,
        ``activity_phase``) — spelled only in this client, exactly like the
        sandbox lifecycle columns above.
        """
        self._json(
            self._http.post(
                "/me/telemetry",
                json={
                    "version": version,
                    "install_id": install_id,
                    "hostname": hostname,
                    "connections": [
                        {
                            "board_id": s.board,
                            "activity_phase": s.phase,
                            "log_tail": s.log_tail,
                            "links": s.links,
                        }
                        for s in connections
                    ],
                },
            )
        )

    def wait_for_commands(
        self, *, install_id: str | None = None, timeout: int = 25
    ) -> list[dict[str, Any]]:
        """Long-poll for queued control commands; return the list, or ``[]`` on a 204.

        Pass ``install_id`` to scope the poll to commands for this install.
        """
        params: dict[str, Any] = {"timeout": timeout}
        if install_id is not None:
            params["install_id"] = install_id
        resp = self._http.get("/me/commands/wait", params=params, timeout=timeout + 10)
        if resp.status_code == 204:
            return []
        return self._json(resp)

    def ack_command(self, command_id: str, *, status: str, result: str | None = None) -> None:
        """Report the outcome of executing a command (status in {done, failed})."""
        self._json(
            self._http.post(
                f"/me/commands/{command_id}/ack",
                json={"status": status, "result": result},
            )
        )

    # --- task operations -----------------------------------------------------

    def get_task(self, task_id: str) -> dict[str, Any]:
        """Return the full task record for ``task_id``."""
        return self._json(self._http.get(f"/tasks/{task_id}"))

    def add_comment(self, task_id: str, body: str) -> dict[str, Any]:
        """Add a comment with ``body`` to ``task_id`` and return the result."""
        return self._json(self._http.post(f"/tasks/{task_id}/comments", json={"body": body}))

    def update_task(self, task_id: str, **fields: Any) -> dict[str, Any]:
        """Patch ``task_id`` with the supplied partial ``fields``."""
        return self._json(self._http.patch(f"/tasks/{task_id}", json=fields))

    # --- install registration ------------------------------------------------

    def register_install(self, hostname: str | None) -> str:
        """Register this install with Parade (POST /me/installs); return the
        minted install id to persist locally.

        The name is this client's own configured ``install_name``, not one core
        passes down — see the ABC."""
        body = self._json(
            self._http.post("/me/installs", json={"hostname": hostname, "name": self._install_name})
        )
        return body["id"]

    # --- listing -------------------------------------------------------------

    def list_organisations(self) -> list[dict[str, Any]]:
        """Return the organisations visible to this agent."""
        return self._json(self._http.get("/organisations"))

    def list_projects(self, org_id: str) -> list[dict[str, Any]]:
        """Return the projects within ``org_id``."""
        return self._json(self._http.get(f"/organisations/{org_id}/projects"))

    def list_boards(self, project_id: str) -> list[dict[str, Any]]:
        """Return the boards within ``project_id``."""
        return self._json(self._http.get(f"/projects/{project_id}/boards"))

    def get_board(self, board_id: str) -> dict[str, Any]:
        """Return the board record for ``board_id``, including the id of the
        project it belongs to."""
        return self._json(self._http.get(f"/boards/{board_id}"))

    def list_board_members(self, board_id: str) -> list[dict[str, Any]]:
        """Return the members of ``board_id``: humans and agents alike, each
        carrying the display ``name`` and the ``user_id`` an assignment wants."""
        return self._json(self._http.get(f"/boards/{board_id}/members"))

    def get_project(self, project_id: str) -> dict[str, Any]:
        """Return the project record for ``project_id``, including its linked
        GitHub repository (``github_repo``), if any."""
        return self._json(self._http.get(f"/projects/{project_id}"))
