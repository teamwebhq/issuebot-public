"""Tests for mention routing in ProjectListener._process and the Supervisor.

Used to also cover ``process_mention``'s own behaviour (prompt building,
disallowed tools, session resume, ...) by calling ``local_run.run_work``
directly with a mention work item. That half moved to ``test_run.py``, which
tests the same pipeline (``run.execute``) generically — a mention is just a
`Job` whose `permits` excludes `changes`, not a second code path any more, so
there is nothing mention-specific left to test at this level once routing
(claimed vs. not, released from its outcome) is covered. What remains here is
genuinely about the listener and the supervisor, not about a run's internals.
"""

from __future__ import annotations

from typing import Any

from conftest import config, connection, mention, wiring, work
from issuebot.contracts import Response, WorkItem
from issuebot.plugins.environments.base import ExecutionEnvironment
from issuebot.plugins.harnesses.fake.harness import FakeHarness
from issuebot.runner import ProjectListener

_PROJECT = connection()

# A mention as the board lists it, carrying the notification its claim names.
_MENTION = mention(actor_name="Alice", comment_excerpt="Can you look into the login bug?")


class MentionApi:
    """Minimal API fake for mention tests — tracks every call the runner makes."""

    def __init__(self) -> None:
        self.claims: list[str] = []
        self.mention_claims: list[str] = []
        self.releases: list[dict[str, Any]] = []
        self.comments: list[tuple[str, str]] = []
        self.updates: list[tuple[str, dict[str, Any]]] = []
        self.heartbeats: list[str] = []

    def get_tasks(self, *, board_id: str | None = None, wait: int = 0) -> list[dict[str, Any]]:
        """Return empty — mention tests drive _process directly."""
        return []

    def get_mentions(self, *, board_id: str | None = None, wait: int = 0) -> list[dict[str, Any]]:
        """Return empty — mention tests drive _process directly."""
        return []

    def claim(self, task_id: str, **kwargs: Any) -> dict[str, Any]:
        """Record the claim attempt."""
        self.claims.append(task_id)
        return {"run_id": "r1", "task_id": task_id}

    def claim_mention(self, notification_id: str) -> dict[str, Any]:
        """Record the mention claim and open its responding run."""
        self.mention_claims.append(notification_id)
        return {"notification_id": notification_id, "task_id": "t1", "run_id": "r1"}

    def get_task(self, task_id: str) -> dict[str, Any]:
        return {"id": task_id, "requester_id": "u-req"}

    def heartbeat(self, run_id: str) -> None:
        """Record the heartbeat."""
        self.heartbeats.append(run_id)

    def release(self, run_id: str, *, status: str = "done", note: str | None = None) -> None:
        """Record the release."""
        self.releases.append({"run_id": run_id, "status": status, "note": note})

    def add_comment(self, task_id: str, body: str) -> dict[str, Any]:
        """Record the comment."""
        self.comments.append((task_id, body))
        return {"id": "c1"}

    def update_task(self, task_id: str, **fields: Any) -> dict[str, Any]:
        """Record the update."""
        self.updates.append((task_id, fields))
        return {"id": task_id}


# ---------------------------------------------------------------------------
# _process routing: mention vs. assigned/no-kind
# ---------------------------------------------------------------------------


class _StubEnvironment(ExecutionEnvironment):
    """Records what the listener handed it."""

    name = "stub"

    def __init__(self, outcome: Response | None = None) -> None:
        self.outcome = outcome or Response(status="done")
        self.ran: list[WorkItem] = []

    def run(self, job, *, reporter, cancel=None):
        self.ran.append(job.work)
        return self.outcome


def test_an_assigned_item_is_claimed_before_it_runs() -> None:
    """Whether work is claimed comes from its policy, not from a check on the
    kind — so a new kind of work routes correctly without an edit here."""
    ex = _StubEnvironment()
    api = MentionApi()
    listener = ProjectListener(wiring(_PROJECT, api=api, environment=ex))

    listener._process(work("t1", "ISS-1"))

    assert api.claims == ["t1"]
    assert [w.kind for w in ex.ran] == ["assigned"]


def test_an_item_with_no_kind_is_treated_as_assigned() -> None:
    """Older servers only ever sent assigned work and omitted the field."""
    ex = _StubEnvironment()
    api = MentionApi()
    listener = ProjectListener(wiring(_PROJECT, api=api, environment=ex))

    listener._process(WorkItem.from_api({"task_id": "t1", "reference": "ISS-1", "board_id": "b"}))

    assert api.claims == ["t1"]
    assert [w.kind for w in ex.ran] == ["assigned"]


def test_a_mention_run_is_released_from_its_outcome() -> None:
    """A mention is claimed by its notification, which opens the board's own
    non-locking run — and that run is released with what the run produced."""
    ex = _StubEnvironment()
    api = MentionApi()
    listener = ProjectListener(wiring(_PROJECT, api=api, environment=ex))

    listener._process(_MENTION)

    assert api.claims == []  # no run lock is taken for a mention
    assert api.mention_claims == ["n1"]
    assert api.releases == [{"run_id": "r1", "status": "done", "note": None}]


# ---------------------------------------------------------------------------
# Supervisor: agent-id resolution from the connect() response
# ---------------------------------------------------------------------------


class _SupervisorApi(MentionApi):
    """MentionApi variant that satisfies the Supervisor's connect / telemetry /
    command daemon threads and board-scoped work polling.

    connect() echoes the agent's identity, mirroring the board endpoint (which
    resolves the agent from the PAT)."""

    def connect(
        self, board_id: str, name: str | None = None, install_id: str | None = None
    ) -> dict[str, Any]:
        return {"agent": {"id": "u-agent-1", "email": "bot@example.com"}}

    def disconnect(self, board_id: str) -> None:
        pass

    def get_tasks(self, *, board_id: str | None = None, wait: int = 0) -> list[dict[str, Any]]:
        import time as _time

        _time.sleep(min(wait, 0.02))
        return []

    def report_telemetry(self, **kwargs: Any) -> None:
        pass

    def wait_for_commands(self, *, timeout: int = 25) -> list[Any]:
        import time as _time

        _time.sleep(min(timeout, 0.02))
        return []

    def ack_command(self, command_id: str, *, status: str, result: str | None = None) -> None:
        pass


def _wait_for_listener(sup: Any, name: str, timeout: float = 2.0):
    """Poll until the Supervisor has a listener registered under ``name``."""
    import time as _time

    deadline = _time.monotonic() + timeout
    while _time.monotonic() < deadline:
        listener = sup._listeners.get(name)
        if listener is not None:
            return listener
        _time.sleep(0.01)
    return None


def test_supervisor_resolves_agent_id_and_passes_to_listeners(tmp_path) -> None:
    """Supervisor learns the agent id from the connect() response (no GET /me) and
    stores it on each listener."""
    from issuebot.config import save_config
    from issuebot.runner import Supervisor

    cfg_path = tmp_path / "config.toml"
    save_config(config(connections=[_PROJECT]), cfg_path)

    api = _SupervisorApi()
    sup = Supervisor(
        api, FakeHarness(0), cfg_path, poll_interval=0.05, agent_path=tmp_path / "agent_id"
    )
    sup.start()
    try:
        listener = _wait_for_listener(sup, "p")
        assert listener is not None, "listener was never started"
        assert listener._ctx.agent_id == "u-agent-1", "the agent id must reach the listener"
    finally:
        sup.stop()


def test_supervisor_connect_without_identity_starts_listeners_with_none_agent_id(tmp_path) -> None:
    """If connect() yields no identity and nothing is cached, listeners start with
    agent_id=None — a degraded but non-fatal mode (mentions can still reply)."""
    from issuebot.config import save_config
    from issuebot.runner import Supervisor

    class NoIdentityApi(_SupervisorApi):
        def connect(
            self, board_id: str, name: str | None = None, install_id: str | None = None
        ) -> dict[str, Any]:
            return {}  # older server / no agent echoed

    cfg_path = tmp_path / "config.toml"
    save_config(config(connections=[_PROJECT]), cfg_path)

    api = NoIdentityApi()
    sup = Supervisor(
        api, FakeHarness(0), cfg_path, poll_interval=0.05, agent_path=tmp_path / "agent_id"
    )
    sup.start()
    try:
        listener = _wait_for_listener(sup, "p")
        assert listener is not None, "listener was never started"
        assert listener._ctx.agent_id is None, "no identity echoed means no agent id"
    finally:
        sup.stop()
