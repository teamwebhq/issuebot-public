"""Tests for the listen loop: ProjectListener long-polls one board's work queue
and processes its claimable tasks serially, skipping work for other boards."""

from __future__ import annotations

import concurrent.futures
import dataclasses
import threading
import time
from pathlib import Path
from typing import Any

from conftest import config, connection, ctx, in_process_environment, wiring
from issuebot.config import Config, Connection, SinkRef, save_config
from issuebot.contracts import Changed, Claim, Handoff, Response, SinkResult
from issuebot.plugins.environments.base import ExecutionEnvironment
from issuebot.plugins.harnesses.fake.harness import FakeHarness
from issuebot.plugins.sources.base import ConnectionConflict
from issuebot.runner import ProjectListener

_PROJECT = connection()

# A connection whose workspace can actually derive `Changes` — `_PROJECT` works
# in place with no git strategy, so its jobs are never permitted to report any
# and a response claiming them is (correctly) refused before delivery.
_GIT_PROJECT = connection(git_init="branch")
_TASK = {"id": "t1", "reference": "ISS-1", "requester_id": "u-req"}


class ScriptedApi:
    """Serves one claimable work item on the first poll, then [] forever.

    Records claims and releases. A ``threading.Event`` signals once a task has
    been released, so the test can wait deterministically instead of sleeping.
    """

    def __init__(self, work_item: Any) -> None:
        self._work_item = _as_payload(work_item)
        self._served = threading.Event()
        self.released = threading.Event()
        self.claims: list[str] = []
        self.releases: list[dict[str, Any]] = []
        self.comments: list[tuple[str, str]] = []
        self.updates: list[tuple[str, dict[str, Any]]] = []
        # The order the board was called in, which the per-method lists above
        # cannot show: "decision applied *before* the claim is released" is an
        # ordering claim, and two separate lists both being non-empty is not.
        self.calls: list[str] = []
        # Records the board_id kwarg passed to each task read.
        self.wait_board_ids: list[str | None] = []
        self.execution_patches: list[dict[str, Any]] = []
        # Records the attribution kwargs passed to each claim call.
        self.claim_kwargs: list[dict[str, Any]] = []
        # Mention claims, and the responding run the board opens for one. Set
        # `mention_run_id` to None for the board that opens no separate run.
        self.mention_claims: list[str] = []
        self.mention_run_id: str | None = "r-resp"

    # --- agent work contract -------------------------------------------------

    def get_tasks(self, *, board_id: str | None = None, wait: int = 0) -> list[dict[str, Any]]:
        self.wait_board_ids.append(board_id)
        if not self._served.is_set():
            self._served.set()
            return [self._work_item]
        # Block for ~wait so the loop doesn't busy-spin, returning nothing.
        time.sleep(min(wait, 0.05))
        return []

    def get_mentions(self, *, board_id: str | None = None, wait: int = 0) -> list[dict[str, Any]]:
        """No mentions outstanding: these doubles script tasks."""
        return []

    def claim(self, task_id: str, **kwargs: Any) -> dict[str, Any]:
        self.claims.append(task_id)
        self.claim_kwargs.append(kwargs)
        return {"run_id": "r1", "task_id": task_id}

    def claim_mention(self, notification_id: str) -> dict[str, Any]:
        self.mention_claims.append(notification_id)
        return {
            "notification_id": notification_id,
            "task_id": "t1",
            "run_id": self.mention_run_id,
        }

    # --- task operations used by process_task --------------------------------

    def heartbeat(self, run_id: str) -> None:
        pass

    def release(self, run_id: str, *, status: str = "done", note: str | None = None) -> None:
        self.releases.append({"run_id": run_id, "status": status, "note": note})
        self.calls.append("release")
        self.released.set()

    def get_task(self, task_id: str) -> dict[str, Any]:
        return _TASK

    def add_comment(self, task_id: str, body: str) -> dict[str, Any]:
        self.comments.append((task_id, body))
        return {"id": "c1"}

    def list_board_members(self, board_id: str) -> list[dict[str, Any]]:
        # One member, whose display name doubles as their user id, so a handoff
        # to "sam" resolves to "sam" and these tests stay about the listener.
        return [{"name": "sam", "user_id": "sam"}]

    def update_task(self, task_id: str, **fields: Any) -> dict[str, Any]:
        self.updates.append((task_id, fields))
        self.calls.append("update_task")
        return {"id": task_id}

    def patch_execution(self, run_id: str, **fields: Any) -> None:
        self.execution_patches.append({"run_id": run_id, **fields})


class AlreadyClaimedApi(ScriptedApi):
    """A ScriptedApi whose claim always fails, as a lost race does.

    *Which* failure is the source's own vocabulary, and the listener is
    deliberately blind to it: `Source.claim` promises a `Claim` or `None`, so
    every reason a claim did not happen looks the same from here."""

    def claim(self, task_id: str, **kwargs: Any) -> dict[str, Any]:
        self.claims.append(task_id)
        raise RuntimeError("another listener won it")


def _as_payload(item: Any) -> dict[str, Any]:
    """A work item as the board sends it — the listener parses these itself.

    The board's wire key is `board_id`; the contract's field is `source_ref`,
    named for the axis rather than for one source's noun. `asdict` gives the
    field name, so it is mapped back here — this helper stands in for the wire,
    and the wire has not changed."""
    from dataclasses import asdict

    from issuebot.contracts import WorkItem

    if not isinstance(item, WorkItem):
        return item
    payload = asdict(item)
    payload["board_id"] = payload.pop("source_ref")
    return payload


def _work_item(task_id: str = "t1", reference: str = "ISS-1", **kw):
    """A work item as the board delivers it."""
    from conftest import work as _w

    return _w(task_id, reference, **kw)


class StubEnvironment(ExecutionEnvironment):
    """Stands in for whichever environment a connection uses.

    The listener's job is claim → build a job → run → release; which
    environment it holds is `environment_for`'s decision and is covered in
    test_environments.py.
    """

    name = "stub"

    def __init__(self, outcome: Response | None = None, on_run=None) -> None:
        self.outcome = outcome or Response(status="done")
        self.on_run = on_run
        self.runs: list[dict[str, Any]] = []

    def run(self, job, *, reporter, cancel=None):
        self.runs.append({"job": job, "work": job.work, "run_id": job.run_id, "cancel": cancel})
        if self.on_run is not None:
            return self.on_run(job.work, job.run_id, cancel) or self.outcome
        return self.outcome


def _run_listener(listener: ProjectListener) -> threading.Thread:
    """Start the listener on a daemon thread and return it."""
    thread = threading.Thread(target=listener.run, daemon=True)
    thread.start()
    return thread


def test_claims_and_processes_a_matching_board_task() -> None:
    # Which environment a connection runs in is environment_for's decision,
    # covered in test_environments.py — a StubEnvironment stands in so this test
    # is only about the poll/claim/run/release loop, not any one environment's
    # internals.
    work = _work_item()
    api = ScriptedApi(work)
    listener = ProjectListener(
        wiring(_PROJECT, api=api, environment=StubEnvironment()),
        wait_timeout=1,
    )

    thread = _run_listener(listener)
    # Wait for the task to be released (deterministic), then stop.
    assert api.released.wait(timeout=2), "task was never processed"
    listener.stop()
    thread.join(timeout=2)
    assert not thread.is_alive()

    assert api.claims == ["t1"]
    assert api.releases == [{"run_id": "r1", "status": "done", "note": None}]


def test_listener_releases_after_process_task() -> None:
    """A run doesn't release itself — it returns a Response and the listener
    performs the release from it. A synchronous call to _process (no thread)
    proves the wiring directly."""
    work = _work_item()
    api = ScriptedApi(work)
    listener = ProjectListener(wiring(_PROJECT, api=api, environment=StubEnvironment()))

    listener._process(work)

    assert api.claims == ["t1"]
    assert api.releases == [{"run_id": "r1", "status": "done", "note": None}]


def test_listener_maps_non_done_outcome_status_to_failed_release() -> None:
    """Any non-done response releases as 'failed', while the response's own
    result text (carrying the real classification) is forwarded unchanged."""
    ex = StubEnvironment(Response(status="aborted", result_text="aborted"))
    item = _work_item()
    api = ScriptedApi(item)
    listener = ProjectListener(wiring(_PROJECT, api=api, environment=ex))

    listener._process(item)

    assert api.releases == [{"run_id": "r1", "status": "failed", "note": "aborted"}]


def test_a_failed_run_tells_the_board_why() -> None:
    """A run that fails before its agent launches — a clone that cannot
    authenticate, a workspace that will not prepare — produces no agent
    comment, because no agent ever ran. The runner has to say so itself, or
    the task goes silent and the person who filed it is left watching an
    assignment that never comes back."""
    ex = StubEnvironment(Response(status="failed", result_text="workspace prep failed"))
    item = _work_item()
    api = ScriptedApi(item)
    listener = ProjectListener(wiring(_PROJECT, api=api, environment=ex))

    listener._process(item)

    assert [body for _, body in api.comments if "workspace prep failed" in body]
    assert api.releases[0]["status"] == "failed"


def test_work_for_another_repository_is_refused_and_said_so() -> None:
    """The connection is configured for one repository; the board hands it a
    task belonging to another. Running it anyway would commit to a branch on
    the wrong repository and open a PR that never appears on this task — so the
    run fails before a workspace is prepared, and the task is told which two
    URLs disagree."""
    item = _work_item()
    item = dataclasses.replace(item, repo="https://github.com/acme/other.git")
    api = ScriptedApi(item)
    conn = connection(folder=None, repo="https://github.com/acme/web.git", git_init="branch")
    listener = ProjectListener(wiring(conn, api=api, environment=StubEnvironment()))

    listener._process(item)

    said = " ".join(body for _, body in api.comments)
    assert "acme/other.git" in said
    assert "acme/web.git" in said
    assert api.releases[0]["status"] == "failed"


def test_a_handoff_decision_is_applied_before_the_claim_is_released() -> None:
    """The listener applies decisions itself now — Source.apply, driven by the
    response the executor returned, not a side effect the agent had to cause."""
    from issuebot.contracts import Handoff

    ex = StubEnvironment(Response(status="done", outputs=[Handoff(assignee="sam")]))
    item = _work_item()
    api = ScriptedApi(item)
    listener = ProjectListener(wiring(_PROJECT, api=api, environment=ex))

    listener._process(item)

    assert api.updates == [("t1", {"assignee_id": "sam"})]
    assert api.releases == [{"run_id": "r1", "status": "done", "note": None}]
    # ...and *before*, which is the whole claim: a release the handoff hadn't
    # landed for would free the task while it still pointed at this agent.
    assert api.calls == ["update_task", "release"]


def test_a_structurally_invalid_response_is_released_as_failed() -> None:
    """Controller-side verification runs before a decision is ever applied or
    the board is told what happened — a run that claims a kind it wasn't
    permitted, or two decisions at once, downgrades to a failed release."""
    from issuebot.contracts import Handoff, NeedsInput

    ex = StubEnvironment(
        Response(status="done", outputs=[Handoff(assignee="sam"), NeedsInput(question="q")])
    )
    item = _work_item()
    api = ScriptedApi(item)
    listener = ProjectListener(wiring(_PROJECT, api=api, environment=ex))

    listener._process(item)

    assert api.updates == []  # neither decision was applied
    assert api.releases[0]["status"] == "failed"
    assert "at most one decision" in api.releases[0]["note"]


class FakeSink:
    """Records every delivery it's handed and hands back a scripted result —
    the listener-level counterpart to `tests/plugins/sinks/test_conformance.
    py`'s `_FakeSink`, used here to exercise `ProjectListener._finish`'s own
    ordering/failure wiring rather than `run.deliver_all` in isolation."""

    def __init__(self, name: str, *, ok: bool = True) -> None:
        self.name = name
        self.accepts = frozenset({"changes", "answer"})
        self._ok = ok
        self.deliveries: list = []

    def deliver(self, delivery):
        self.deliveries.append(delivery)
        return SinkResult(sink=self.name, ok=self._ok, summary="ok" if self._ok else "down")


def _changed_and_handoff() -> Response:
    """A done response reporting real changes plus a hand-off — one deliverable
    for a sink to see, one decision for the source to apply. `changes` must be
    a genuine, non-empty `Changes` or `verify` downgrades the response to
    `failed` before `_finish` ever reaches delivery."""
    from issuebot.contracts import Changes

    return Response(
        status="done",
        changes=Changes(branch="b", base_sha="a", head_sha="b2", stat="1 file", files_changed=1),
        outputs=[Changed(summary="did stuff"), Handoff(assignee="sam")],
    )


def test_a_required_sink_failing_cancels_the_decisions() -> None:
    """A required sink's own failure fails the run and skips every decision —
    the task stays where it was rather than being reassigned over a PR that
    was never opened."""
    sink = FakeSink("required", ok=False)
    ex = StubEnvironment(_changed_and_handoff())
    item = _work_item()
    api = ScriptedApi(item)
    listener = ProjectListener(
        wiring(
            _GIT_PROJECT,
            api=api,
            environment=ex,
            sinks=[(SinkRef(name="required", required=True), sink)],
        )
    )

    listener._process(item)

    assert len(sink.deliveries) == 1  # the sink still got its turn
    assert api.updates == []  # the decision was never applied
    assert api.releases[0]["status"] == "failed"


def test_a_best_effort_sink_failing_does_not_cancel_the_decisions() -> None:
    """A best-effort sink's own failure is only reported — the run and its
    decision go ahead exactly as if the sink had never been configured."""
    sink = FakeSink("optional", ok=False)
    ex = StubEnvironment(_changed_and_handoff())
    item = _work_item()
    api = ScriptedApi(item)
    listener = ProjectListener(
        wiring(
            _GIT_PROJECT,
            api=api,
            environment=ex,
            sinks=[(SinkRef(name="optional", required=False), sink)],
        )
    )

    listener._process(item)

    assert len(sink.deliveries) == 1
    assert api.updates == [("t1", {"assignee_id": "sam"})]
    assert api.releases[0]["status"] == "done"


def test_deliverables_run_before_decisions() -> None:
    """A decision usually refers to a deliverable — reassigning first hands
    the reviewer a task with nothing to look at."""
    order: list[str] = []

    class OrderTrackingSink:
        name = "tracker"
        accepts = frozenset({"changes"})

        def deliver(self, delivery):
            order.append("deliver")
            return SinkResult(sink="tracker", ok=True, summary="delivered")

    class TrackingApi(ScriptedApi):
        def update_task(self, task_id, **fields):
            order.append("decide")
            return super().update_task(task_id, **fields)

    item = _work_item()
    api = TrackingApi(item)
    ex = StubEnvironment(_changed_and_handoff())
    listener = ProjectListener(
        wiring(
            _GIT_PROJECT,
            api=api,
            environment=ex,
            sinks=[(SinkRef(name="tracker", required=True), OrderTrackingSink())],
        )
    )

    listener._process(item)

    assert order == ["deliver", "decide"]


def test_the_listener_runs_work_through_its_environment(monkeypatch) -> None:
    """Claim, hand to the environment, release from the response — whatever the
    environment. Which one a connection gets is environment_for's job."""
    ex = StubEnvironment(Response(status="done"))
    item = _work_item()
    api = ScriptedApi(item)
    listener = ProjectListener(wiring(_PROJECT, api=api, environment=ex))

    listener._process(item)

    assert [r["work"].task_id for r in ex.runs] == ["t1"]
    assert ex.runs[0]["run_id"] == "r1"
    assert api.claims == ["t1"]
    assert api.releases == [{"run_id": "r1", "status": "done", "note": None}]


def test_unclaimable_work_runs_without_taking_a_run_lock(monkeypatch) -> None:
    """A mention's claim takes no run lock: it acknowledges the notification and
    opens the board's own non-locking run, which is released from the outcome."""
    ex = StubEnvironment()
    item = _work_item("t1", "ISS-1", kind="mention", notification_id="n1")
    api = ScriptedApi(item)
    listener = ProjectListener(wiring(_PROJECT, api=api, environment=ex))

    listener._process(item)

    assert api.claims == []
    assert api.mention_claims == ["n1"]
    assert [r["work"].kind for r in ex.runs] == ["mention"]
    assert api.releases == [{"run_id": "r-resp", "status": "done", "note": None}]


def test_a_failed_unclaimed_run_is_released_as_failed() -> None:
    """The mention path used to release a hardcoded 'done', so a failed sandbox
    reported success."""
    ex = StubEnvironment(Response(status="failed", result_text="sandbox run crashed"))
    item = _work_item("t1", "ISS-1", kind="mention", notification_id="n1")
    api = ScriptedApi(item)
    listener = ProjectListener(wiring(_PROJECT, api=api, environment=ex))

    listener._process(item)

    assert api.releases == [{"run_id": "r-resp", "status": "failed", "note": "sandbox run crashed"}]


def test_a_mention_claim_with_no_responding_run_releases_nothing() -> None:
    """The board opens no separate run when this agent already holds a working
    claim on the task; the mention still runs, and there is nothing to release."""
    ex = StubEnvironment()
    item = _work_item("t1", "ISS-1", kind="mention", notification_id="n1")
    api = ScriptedApi(item)
    api.mention_run_id = None
    listener = ProjectListener(wiring(_PROJECT, api=api, environment=ex))

    listener._process(item)

    assert api.releases == []
    assert len(ex.runs) == 1


def test_the_agent_id_reaches_the_run() -> None:
    """Self-assign-by-mention needs the agent's own user id, and it has to reach
    the thing that acts on it: the source renders it into the mention prompt,
    and a sandbox puts it on the wire. It travels on the runner context because
    both need it, so a listener told one explicitly folds it in there — but what
    matters is that it comes out the far end, in the prompt the run launches
    with."""
    item = _work_item("t1", "ISS-1", kind="mention", notification_id="n1")
    ex = StubEnvironment()
    api = ScriptedApi(item)
    listener = ProjectListener(
        wiring(_PROJECT, api=api, environment=ex, context=ctx(agent_id="agent-9"))
    )

    listener._process(item)

    assert "agent-9" in ex.runs[0]["job"].prompt


def test_skips_work_for_a_different_board() -> None:
    work = {"task_id": "t1", "reference": "ISS-1", "board_id": "OTHER"}
    api = ScriptedApi(work)
    listener = ProjectListener(wiring(_PROJECT, api=api), wait_timeout=1)

    thread = _run_listener(listener)
    time.sleep(0.3)
    listener.stop()
    thread.join(timeout=2)
    assert not thread.is_alive()

    # The board guard means the OTHER-board item is never claimed or processed.
    assert api.claims == []
    assert api.releases == []


def test_already_claimed_task_is_not_processed() -> None:
    work = _work_item()
    api = AlreadyClaimedApi(work)
    listener = ProjectListener(wiring(_PROJECT, api=api), wait_timeout=1)

    thread = _run_listener(listener)
    time.sleep(0.3)
    listener.stop()
    thread.join(timeout=2)
    assert not thread.is_alive()

    # Claim was attempted but lost; process_task never ran, so no release.
    assert api.claims == ["t1"]
    assert api.releases == []


def test_phase_transitions_waiting_to_working() -> None:
    from issuebot.agent_state import AgentState

    work = _work_item()
    api = ScriptedApi(work)
    state = AgentState()
    seen_phases: list[str] = []

    # Wrap set_phase to record the transition order.
    orig = state.set_phase
    state.set_phase = lambda p, ref=None: (  # ty: ignore[invalid-assignment]
        seen_phases.append(p),
        orig(p, ref),
    )[1]

    listener = ProjectListener(
        wiring(_PROJECT, api=api, context=ctx(state=state)),
        wait_timeout=1,
    )
    thread = _run_listener(listener)
    assert api.released.wait(timeout=2), "task was never processed"
    listener.stop()
    thread.join(timeout=2)

    assert "waiting" in seen_phases
    assert "working" in seen_phases
    assert seen_phases.index("waiting") < seen_phases.index("working")


def test_listener_snapshot_records_ref_while_working() -> None:
    """`issuebot status` and server telemetry receive the same snapshot, so the
    phase and ref they report cannot disagree."""
    item = _work_item()
    api = ScriptedApi(item)
    listener = ProjectListener(wiring(_PROJECT, api=api, environment=StubEnvironment()))

    snap = listener.snapshot()
    assert (snap.name, snap.board, snap.target) == ("p", "b", "/tmp/p")
    assert (snap.phase, snap.ref) == ("idle", None)

    seen: list[Any] = []
    ex = StubEnvironment(on_run=lambda w, r, c: seen.append(listener.snapshot()))
    listener._environment = ex
    listener._process(item)

    # While the work ran the snapshot showed it; afterwards it went idle.
    assert seen and seen[0].phase == "working"
    assert seen[0].ref == "ISS-1"
    assert listener.snapshot().phase == "idle"
    assert listener.snapshot().ref is None


def test_supervisor_writes_status_file(tmp_path: Path) -> None:
    """The Supervisor mirrors each live listener's per-connection runtime to the
    local status file that ``issuebot status`` reads (name, version, connections)."""
    from issuebot.runner import Supervisor
    from issuebot.status import StatusStore

    cfg_path = tmp_path / "config.toml"
    save_config(_config_with([_conn("p", "b-a")]), cfg_path)

    store = StatusStore(tmp_path / "status.json")
    api = RecordingApi()
    sup = Supervisor(
        api,
        FakeHarness(0),
        cfg_path,
        poll_interval=0.05,
        telemetry_interval=0.01,
        version="9.9.9",
        status_store=store,
    )
    sup.start()
    try:
        assert _wait(lambda: bool((p := store.read()) and p.get("connections")), 2.0), (
            "status file was never written"
        )
        payload = store.read()
        assert payload is not None
        assert payload["version"] == "9.9.9"
        names = [c["name"] for c in payload["connections"]]
        assert "p" in names
    finally:
        sup.stop()


def test_stop_aborts_the_in_flight_run() -> None:
    api = ScriptedApi(_work_item("t1", "ISS-1"))
    listener = ProjectListener(wiring(_PROJECT, api=api))

    # Simulate a run in flight by registering its abort signal, then stop().
    active = threading.Event()
    listener._active_cancels["r1"] = active

    listener.stop()

    assert active.is_set()


def test_stop_aborts_every_in_flight_run() -> None:
    """stop() must set EVERY tracked cancel Event, not just one — a pooled
    sandbox environment can have several runs in flight at once."""
    api = ScriptedApi(_work_item("t1", "ISS-1"))
    listener = ProjectListener(wiring(_PROJECT, api=api))

    first, second = threading.Event(), threading.Event()
    listener._active_cancels["r1"] = first
    listener._active_cancels["r2"] = second

    listener.stop()

    assert first.is_set()
    assert second.is_set()


class _ConcurrentApi(ScriptedApi):
    """Serves several claimable work items on the first poll (instead of
    ScriptedApi's single item), each claimed with its own run_id, and exposes
    an Event set once every one of them has been released."""

    def __init__(self, work_items: list[dict[str, Any]]) -> None:
        super().__init__(work_items[0])
        self._work_items = [_as_payload(w) for w in work_items]
        self.all_released = threading.Event()

    def get_tasks(self, *, board_id: str | None = None, wait: int = 0) -> list[dict[str, Any]]:
        self.wait_board_ids.append(board_id)
        if not self._served.is_set():
            self._served.set()
            return list(self._work_items)
        time.sleep(min(wait, 0.05))
        return []

    def serve(self, *work_items: Any) -> None:
        """Offer a fresh batch of work on the next poll, replacing the last."""
        self._work_items = [_as_payload(w) for w in work_items]
        self._served.clear()

    def claim(self, task_id: str, **kwargs: Any) -> dict[str, Any]:
        self.claims.append(task_id)
        return {"run_id": f"r-{task_id}", "task_id": task_id}

    def release(self, run_id: str, *, status: str = "done", note: str | None = None) -> None:
        self.releases.append({"run_id": run_id, "status": status, "note": note})
        if len(self.releases) >= len(self._work_items):
            self.all_released.set()


def test_listener_dispatches_up_to_max_concurrent() -> None:
    """max_concurrent=2 runs two claimed items at once and holds a third back
    until a worker frees up. stop() cancels every run actually in flight."""
    started = threading.Semaphore(0)
    gate = threading.Event()
    lock = threading.Lock()
    active_cancels: list[threading.Event] = []

    def on_run(work, run_id, cancel):
        with lock:
            active_cancels.append(cancel)
        started.release()
        gate.wait(timeout=5)
        return Response(status="done")

    items = [_work_item(f"t{i}", f"ISS-{i}") for i in (1, 2, 3)]
    api = _ConcurrentApi(items)
    listener = ProjectListener(
        wiring(_PROJECT, api=api, environment=StubEnvironment(on_run=on_run)),
        wait_timeout=1,
        max_concurrent=2,
    )

    thread = threading.Thread(target=listener.run, daemon=True)
    thread.start()
    try:
        assert started.acquire(timeout=5)
        assert started.acquire(timeout=5)
        # The third is queued behind the pool, not running.
        assert not started.acquire(timeout=0.3)
        with lock:
            assert len(active_cancels) == 2

        listener.stop()
        with lock:
            assert all(c.is_set() for c in active_cancels)
    finally:
        gate.set()
        listener.stop()
        thread.join(timeout=5)


def test_stop_cancels_pool_queued_work_before_it_starts() -> None:
    """Work that reaches a pool worker only after stop() has run must release its
    claim rather than starting — and never strand the run."""
    gate = threading.Event()
    running = threading.Event()

    def on_run(work, run_id, cancel):
        running.set()
        gate.wait(timeout=5)
        return Response(status="done")

    items = [_work_item(f"t{i}", f"ISS-{i}") for i in (1, 2)]
    api = _ConcurrentApi(items)
    listener = ProjectListener(
        wiring(_PROJECT, api=api, environment=StubEnvironment(on_run=on_run)),
        wait_timeout=1,
        max_concurrent=1,
    )

    thread = threading.Thread(target=listener.run, daemon=True)
    thread.start()
    try:
        assert running.wait(timeout=5)
        listener.stop()
        gate.set()
        thread.join(timeout=5)
    finally:
        gate.set()
        listener.stop()

    # Every claim was released, including the one that never got to run.
    assert len(api.releases) == len(api.claims)


def test_process_after_pool_shutdown_does_not_crash_the_listener() -> None:
    """RACE 2 regression: ThreadPoolExecutor.submit() raises RuntimeError on a
    shut-down pool. If stop() races ahead of a submit (claim() is a network
    round-trip in real life), _process must not let that RuntimeError escape
    and kill the poll thread — it must release the claim and return quietly."""
    conn = connection(
        folder=None,
        repo="https://example.com/r.git",
        git_init="branch",
    )
    api = ScriptedApi(_work_item("t1", "ISS-1"))

    listener = ProjectListener(wiring(conn, api=api))
    listener._pool = concurrent.futures.ThreadPoolExecutor(max_workers=2)
    listener.stop()  # shuts the pool down and sets _stop

    # Drive the exact code path _process takes for a claimed task after stop()
    # raced ahead of the submit — must not raise.
    listener._process(_work_item("t1", "ISS-1"))

    assert api.claims == ["t1"]
    assert api.releases == [{"run_id": "r1", "status": "failed", "note": "listener stopped"}]


def _listener(conn: Connection) -> ProjectListener:
    """Build a ProjectListener for ``conn`` with its own (default) AgentState."""
    return ProjectListener(wiring(conn, api=ScriptedApi({"task_id": "t1", "board_id": conn.board})))


def test_connection_snapshots_reports_per_listener_state() -> None:
    """Each ProjectListener owns a distinct AgentState; connection_snapshots()
    reports one ConnectionSnapshot per live listener with the right board and
    that listener's own activity phase."""
    from issuebot.runner import Supervisor

    la = _listener(_conn("a", "b-a"))
    lb = _listener(_conn("b", "b-b"))

    # Distinct state objects, not a shared one.
    assert la.state is not lb.state

    la.state.set_phase("working")
    lb.state.set_phase("waiting")

    sup = Supervisor(RecordingApi(), FakeHarness(0), "/tmp/none.toml")
    # Inject the listeners directly (no threads) to exercise the snapshot.
    sup._listeners = {"a": la, "b": lb}
    sup._boards = {"a": "b-a", "b": "b-b"}

    snaps = {s.board: s for s in sup.connection_snapshots()}
    assert set(snaps) == {"b-a", "b-b"}
    assert snaps["b-a"].phase == "working"
    assert snaps["b-a"].name == "a"
    assert snaps["b-b"].phase == "waiting"


def test_supervisor_reports_telemetry_batches(tmp_path: Path) -> None:
    """The publish loop hands the snapshot batch to server telemetry: one entry
    per live listener, in the runner's own vocabulary."""
    from issuebot.runner import Supervisor
    from issuebot.status import StatusStore

    cfg_path = tmp_path / "config.toml"
    save_config(_config_with([_conn("p", "b-a")]), cfg_path)

    api = RecordingApi()
    sup = Supervisor(
        api,
        FakeHarness(0),
        cfg_path,
        poll_interval=0.05,
        telemetry_interval=0.01,
        version="9.9.9",
        status_store=StatusStore(tmp_path / "status.json"),
    )
    sup.start()
    try:
        assert _wait(lambda: any(t["connections"] for t in api.telemetry), 2.0), (
            "telemetry never reported a live connection"
        )
    finally:
        sup.stop()

    batch = next(t for t in api.telemetry if t["connections"])
    assert batch["version"] == "9.9.9"
    assert batch["install_id"] == "inst-1"
    snap = batch["connections"][0]
    assert snap.board == "b-a"
    assert snap.name == "p"


def test_one_failing_publisher_does_not_starve_the_other() -> None:
    """A failing status-file write must not stop server telemetry receiving the
    same batch — each publisher fails alone."""
    from issuebot.runner import Supervisor

    class ExplodingStore:
        def write(self, payload: dict[str, Any]) -> None:
            raise OSError("disk full")

    api = RecordingApi()
    sup = Supervisor(
        api,
        FakeHarness(0),
        "/tmp/none.toml",
        status_store=ExplodingStore(),  # type: ignore[arg-type]
    )
    sup._install_id = "inst-1"
    sup._listeners = {"a": _listener(_conn("a", "b-a"))}
    sup._boards = {"a": "b-a"}

    sup._publish()

    # Telemetry is delivered off the publish thread (so a hung server can
    # never stale the status file); give the delivery a moment to land.
    assert _wait(lambda: api.telemetry, 2.0), "telemetry never received the batch"
    assert api.telemetry[0]["connections"][0].board == "b-a"


def test_a_hung_telemetry_post_does_not_stall_status_writes(tmp_path: Path) -> None:
    """The board client allows 30s per HTTP phase while `issuebot status` calls
    the file stale at 45s — one slow POST per tick and a healthy runner reads
    as dead. Status-file writes must stay on the tick schedule with the server
    hung: the write lands before (and unblocked by) the telemetry delivery."""
    from issuebot.runner import Supervisor
    from issuebot.status import StatusStore

    hang = threading.Event()

    class HangingApi(RecordingApi):
        def report_telemetry(self, **kwargs: Any) -> None:
            hang.wait(timeout=5)
            super().report_telemetry(**kwargs)

    store = StatusStore(tmp_path / "status.json")
    sup = Supervisor(HangingApi(), FakeHarness(0), "/tmp/none.toml", status_store=store)
    sup._install_id = "inst-1"

    started = time.monotonic()
    sup._publish()
    first = store.read()
    sup._publish()
    second = store.read()
    elapsed = time.monotonic() - started
    hang.set()

    assert first is not None and second is not None
    assert elapsed < 2.0, f"status writes waited {elapsed:.1f}s on a hung telemetry POST"


def test_a_bad_snapshot_tick_does_not_kill_the_publish_loop(tmp_path: Path) -> None:
    """The publish daemon thread is started exactly once. If gathering the
    snapshot batch raises, the loop must log and carry on — a single bad tick
    otherwise silences status.json and telemetry for the process lifetime."""
    from issuebot.runner import Supervisor
    from issuebot.status import StatusStore

    store = StatusStore(tmp_path / "status.json")
    sup = Supervisor(
        RecordingApi(),
        FakeHarness(0),
        "/tmp/none.toml",
        telemetry_interval=0.01,
        status_store=store,
    )

    calls = {"n": 0}
    real_snapshots = sup.connection_snapshots

    def flaky() -> list:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("boom")
        return real_snapshots()

    sup.connection_snapshots = flaky  # type: ignore[method-assign]

    thread = threading.Thread(target=sup._publish_loop, daemon=True)
    thread.start()
    try:
        assert _wait(lambda: store.read() is not None, 2.0), (
            "the publish loop never recovered from one bad tick"
        )
    finally:
        sup._stop.set()
        thread.join(timeout=2)


def test_publish_skips_telemetry_until_registered(tmp_path: Path) -> None:
    """With no install id there is nothing to report to: the server call is
    skipped, but the local status file is still written."""
    from issuebot.runner import Supervisor
    from issuebot.status import StatusStore

    api = RecordingApi()
    store = StatusStore(tmp_path / "status.json")
    sup = Supervisor(api, FakeHarness(0), "/tmp/none.toml", version="9.9.9", status_store=store)

    sup._publish()

    assert api.telemetry == []
    payload = store.read()
    assert payload is not None
    assert payload["version"] == "9.9.9"


def test_listener_polls_scoped_to_its_board() -> None:
    """The listener passes its connection's board_id into every work read."""
    work = _work_item()
    api = ScriptedApi(work)
    listener = ProjectListener(wiring(_PROJECT, api=api), wait_timeout=1)

    thread = _run_listener(listener)
    # Wait until the task has been released (i.e., one full work cycle completed).
    assert api.released.wait(timeout=2), "task was never processed"
    listener.stop()
    thread.join(timeout=2)

    # The board_id passed to the task read must match the connection's board.
    assert api.wait_board_ids, "the board was never read"
    assert api.wait_board_ids[0] == "b"


def test_supervisor_enables_info_logging_for_the_tail(tmp_path: Path) -> None:
    """Supervisor.start() lowers the issuebot logger to INFO so the runner's own
    activity (claiming/listening/errors) reaches the dashboard log tail."""
    import logging as _logging

    from issuebot.runner import Supervisor

    logger = _logging.getLogger("issuebot")
    saved_level, saved_handlers = logger.level, list(logger.handlers)

    cfg_path = tmp_path / "config.toml"
    save_config(_config_with([_conn("a", "b-a")]), cfg_path)

    sup = Supervisor(RecordingApi(), FakeHarness(0), cfg_path, poll_interval=0.05)
    sup.start()
    try:
        assert logger.level == _logging.INFO
    finally:
        sup.stop()
        logger.setLevel(saved_level)
        logger.handlers[:] = saved_handlers


# ---------------------------------------------------------------------------
# Supervisor hot-reload tests
# ---------------------------------------------------------------------------


def _conn(name: str, board: str) -> Connection:
    """Build a minimal Connection for testing (folder=/tmp is always present).

    Through `conftest.connection` so it names the environment that runs work in
    this process, which is what a listener under test is about to do."""
    return connection(name=name, board=board, folder="/tmp")


def _config_with(connections: list[Connection]) -> Config:
    """Build a minimal Config containing the given connections."""
    return config(connections=connections)


def _wait(condition: Any, timeout: float) -> bool:
    """Poll ``condition`` every 10 ms until it returns True or the timeout elapses."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.01)
    return False


class RecordingApi:
    """Minimal fake API that records ``connect``/``disconnect`` calls.

    All other methods return harmless stubs so the Supervisor's telemetry and
    command daemon threads can start without crashing.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.telemetry: list[dict[str, Any]] = []

    def connect(
        self, board_id: str, name: str | None = None, install_id: str | None = None
    ) -> dict[str, Any]:
        """Record and acknowledge a board connect."""
        self.calls.append(("connect", board_id))
        return {}

    def disconnect(self, board_id: str) -> None:
        """Record a board disconnect."""
        self.calls.append(("disconnect", board_id))

    def get_tasks(self, *, board_id: str | None = None, wait: int = 0) -> list:
        """Return no work; sleep briefly so listener threads don't spin."""
        time.sleep(min(wait, 0.01))
        return []

    def get_mentions(self, *, board_id: str | None = None, wait: int = 0) -> list:
        """Return no mentions."""
        return []

    def wait_for_commands(self, *, install_id: str | None = None, timeout: int = 25) -> list:
        """No control commands; sleep briefly so the command thread doesn't spin."""
        time.sleep(min(timeout, 0.01))
        return []

    def ack_command(self, command_id: str, *, status: str, result: str | None = None) -> None:
        """No-op: there are no commands to acknowledge."""

    def register_install(self, hostname: str | None) -> str:
        """Mint a fixed install id, so the publish loop reports telemetry."""
        return "inst-1"

    def report_telemetry(self, **kwargs: Any) -> None:
        """Record each telemetry report from the publish loop."""
        self.telemetry.append(kwargs)


def test_listen_picks_up_new_connection_without_restart(tmp_path: Path) -> None:
    """Adding a connection to the config file starts a new listener without restarting.

    This is the hot-reload regression: the Supervisor polls the config file and
    reconciles — spinning up a new ProjectListener + calling api.connect — when a
    new connection appears, without any manual restart.
    """
    from issuebot.runner import Supervisor

    cfg_path = tmp_path / "config.toml"
    save_config(_config_with([_conn("a", "b-a")]), cfg_path)

    api = RecordingApi()
    sup = Supervisor(api, FakeHarness(0), cfg_path, poll_interval=0.05)
    sup.start()
    try:
        # Write a second connection into the config file while the supervisor runs.
        save_config(_config_with([_conn("a", "b-a"), _conn("b", "b-b")]), cfg_path)

        # The supervisor must detect the change within a short timeout and start a
        # listener for the new board.
        assert _wait(lambda: "b-b" in sup.active_boards(), 2.0), (
            "second board was never picked up by the hot-reload supervisor"
        )
        assert ("connect", "b-b") in api.calls, (
            f"api.connect was not called for 'b-b'; calls were: {api.calls}"
        )
    finally:
        sup.stop()


def test_supervisor_stops_removed_connection(tmp_path: Path) -> None:
    """Removing a connection from the config file stops its listener and calls disconnect."""
    from issuebot.runner import Supervisor

    cfg_path = tmp_path / "config.toml"
    save_config(_config_with([_conn("a", "b-a"), _conn("b", "b-b")]), cfg_path)

    api = RecordingApi()
    sup = Supervisor(api, FakeHarness(0), cfg_path, poll_interval=0.05)
    sup.start()
    try:
        # Wait for both listeners to start.
        assert _wait(lambda: sup.active_boards() == {"b-a", "b-b"}, 2.0), (
            f"both boards should be active; got {sup.active_boards()}"
        )

        # Remove one connection from the config.
        save_config(_config_with([_conn("a", "b-a")]), cfg_path)

        # The supervisor must stop the removed listener and disconnect server-side.
        assert _wait(lambda: "b-b" not in sup.active_boards(), 2.0), (
            "removed board was still active after config change"
        )
        assert ("disconnect", "b-b") in api.calls, (
            f"api.disconnect was not called for 'b-b'; calls were: {api.calls}"
        )
    finally:
        sup.stop()


# ---------------------------------------------------------------------------
# Agent-id resolution from the connect() response (no GET /me)
# ---------------------------------------------------------------------------


class _ConnectIdentityApi(RecordingApi):
    """RecordingApi whose connect() echoes the agent's identity, mirroring the
    board's connect endpoint (which resolves the agent from the PAT)."""

    def __init__(self, user_id: str = "a1") -> None:
        super().__init__()
        self._user_id = user_id

    def connect(
        self, board_id: str, name: str | None = None, install_id: str | None = None
    ) -> dict[str, Any]:
        self.calls.append(("connect", board_id))
        return {"connected": True, "agent": {"id": self._user_id}}


class _ConflictApi(RecordingApi):
    """RecordingApi whose connect() always raises ConnectionConflict — the agent
    is already connected (a restart). ``agent_id`` mimics the board conveying the
    identity on the 409 (via the X-Parade-Agent-Id header); None mimics an older
    board that doesn't."""

    def __init__(self, agent_id: str | None = None) -> None:
        super().__init__()
        self._agent_id = agent_id

    def connect(
        self, board_id: str, name: str | None = None, install_id: str | None = None
    ) -> dict[str, Any]:
        self.calls.append(("connect", board_id))
        raise ConnectionConflict(board_id, agent_id=self._agent_id)


def test_agent_id_resolved_from_connect_response(tmp_path: Path) -> None:
    """The Supervisor learns its agent id from the connect() response — no GET /me —
    and persists it locally."""
    from issuebot.runner import Supervisor

    cfg_path = tmp_path / "config.toml"
    save_config(_config_with([_conn("a", "b-a")]), cfg_path)
    agent_path = tmp_path / "agent_id"

    api = _ConnectIdentityApi(user_id="a1")
    sup = Supervisor(api, FakeHarness(0), cfg_path, poll_interval=0.05, agent_path=agent_path)
    sup.start()
    try:
        assert _wait(lambda: sup._agent_id == "a1", 2.0), (
            f"_agent_id should be 'a1' from connect, got {sup._agent_id!r}"
        )
        # Persisted so a later run doesn't need to ask again.
        assert _wait(lambda: agent_path.exists(), 2.0)
        assert agent_path.read_text(encoding="utf-8").strip() == "a1"
    finally:
        sup.stop()


def test_agent_id_persists_across_restart(tmp_path: Path) -> None:
    """On restart the board connection already exists (connect → 409, no identity in
    the body); the agent id is still available, loaded from the local cache."""
    from issuebot.runner import Supervisor

    agent_path = tmp_path / "agent_id"
    agent_path.write_text("a1", encoding="utf-8")  # persisted by a prior run

    cfg_path = tmp_path / "config.toml"
    save_config(_config_with([_conn("a", "b-a")]), cfg_path)

    api = _ConflictApi()  # connect 409s and returns no identity
    sup = Supervisor(api, FakeHarness(0), cfg_path, poll_interval=0.05, agent_path=agent_path)
    sup.start()
    try:
        # Loaded synchronously in start(), before any connect.
        assert sup._agent_id == "a1", (
            f"_agent_id should load from cache on restart, got {sup._agent_id!r}"
        )
    finally:
        sup.stop()


def test_agent_id_resolved_from_conflict_on_durable_connection(tmp_path: Path) -> None:
    """The live-bug case: the agent is already connected to the board (connect → 409)
    AND the local cache is empty (first run of the new runner). The board conveys
    the identity on the 409, so the runner still resolves and persists it —
    otherwise it could never self-assign on that board."""
    from issuebot.runner import Supervisor

    cfg_path = tmp_path / "config.toml"
    save_config(_config_with([_conn("a", "b-a")]), cfg_path)
    agent_path = tmp_path / "agent_id"  # empty: nothing cached yet

    api = _ConflictApi(agent_id="a1")  # already connected, but 409 carries the id
    sup = Supervisor(api, FakeHarness(0), cfg_path, poll_interval=0.05, agent_path=agent_path)
    sup.start()
    try:
        assert _wait(lambda: sup._agent_id == "a1", 2.0), (
            f"_agent_id should resolve from the 409, got {sup._agent_id!r}"
        )
        assert _wait(lambda: agent_path.exists(), 2.0)
        assert agent_path.read_text(encoding="utf-8").strip() == "a1"
    finally:
        sup.stop()


def test_supervisor_restarts_a_connection_whose_settings_changed(tmp_path: Path) -> None:
    """`issuebot connect --name <existing>` rewrites the connection in place —
    the listener must be restarted with the new settings, not left running the
    stale ones until the next manual restart."""
    from issuebot.runner import Supervisor

    cfg_path = tmp_path / "config.toml"
    save_config(_config_with([_conn("a", "b-a")]), cfg_path)

    api = RecordingApi()
    sup = Supervisor(api, FakeHarness(0), cfg_path, poll_interval=0.05)
    sup.start()
    try:
        assert _wait(lambda: sup.active_boards() == {"b-a"}, 2.0)

        changed = connection(name="a", board="b-a", folder="/tmp", sinks=["fake"])
        save_config(_config_with([changed]), cfg_path)

        assert _wait(
            lambda: any(
                any(s.name == "fake" for s in lis._project.sinks)  # noqa: SLF001
                for lis in sup._listeners.values()  # noqa: SLF001
            ),
            2.0,
        ), "listener kept running the stale connection settings"
        # The board is still served throughout — this is a restart, not a removal.
        assert sup.active_boards() == {"b-a"}
    finally:
        sup.stop()


def test_supervisor_leaves_an_unchanged_connection_alone(tmp_path: Path) -> None:
    """An unrelated config edit must not churn listeners that didn't change:
    restarting one aborts whatever it is running."""
    from issuebot.runner import Supervisor

    cfg_path = tmp_path / "config.toml"
    save_config(_config_with([_conn("a", "b-a")]), cfg_path)

    api = RecordingApi()
    sup = Supervisor(api, FakeHarness(0), cfg_path, poll_interval=0.05)
    sup.start()
    try:
        assert _wait(lambda: sup.active_boards() == {"b-a"}, 2.0)
        listener = sup._listeners["a"]  # noqa: SLF001 - identity check

        save_config(_config_with([_conn("a", "b-a"), _conn("b", "b-b")]), cfg_path)
        assert _wait(lambda: sup.active_boards() == {"b-a", "b-b"}, 2.0)

        assert sup._listeners["a"] is listener  # noqa: SLF001 - same object, never restarted
        assert ("disconnect", "b-a") not in api.calls
    finally:
        sup.stop()


# ---------------------------------------------------------------------------
# Claim attribution + the single-slot status view under concurrency
# ---------------------------------------------------------------------------


def test_claim_reports_the_install_and_executor(tmp_path: Path) -> None:
    """The board can only show who is running a cloud task if the claim says so."""
    api = ScriptedApi(_work_item("t1", "ISS-1"))
    listener = ProjectListener(
        wiring(_PROJECT, api=api, install_id="inst-9"),
        wait_timeout=1,
    )

    thread = _run_listener(listener)
    try:
        assert api.released.wait(timeout=5)
    finally:
        listener.stop()
        thread.join(timeout=2)

    # The environment resolved off the registry, never spelled: this is the same
    # class of coupling the deletion suite exists to catch, and the matcher
    # cannot see this one.
    assert api.claim_kwargs == [{"install_id": "inst-9", "executor": in_process_environment()}]


def test_status_stays_working_until_the_last_concurrent_run_finishes() -> None:
    """With several runs in flight the per-connection status is one slot: the
    first to finish must not report the whole connection idle underneath its
    still-working sibling."""
    hold = threading.Event()
    quick_done = threading.Event()

    def on_run(work, run_id, cancel):
        if run_id == "slow":
            hold.wait(timeout=5)
        return Response(status="done")

    listener = ProjectListener(
        wiring(_PROJECT, api=ScriptedApi(_work_item()), environment=StubEnvironment(on_run=on_run))
    )

    slow = threading.Thread(
        target=listener._run_claimed,
        args=(_work_item("t1"), Claim(work_id="t1", token="slow")),
        daemon=True,
    )
    slow.start()

    def run_quick() -> None:
        listener._run_claimed(_work_item("t2", "ISS-2"), Claim(work_id="t2", token="quick"))
        quick_done.set()

    threading.Thread(target=run_quick, daemon=True).start()
    try:
        assert quick_done.wait(timeout=5)
        assert listener.snapshot().phase == "working"
    finally:
        hold.set()
        slow.join(timeout=5)

    assert listener.snapshot().phase == "idle"


# ---------------------------------------------------------------------------
# Session resume
# ---------------------------------------------------------------------------


def test_a_runs_session_is_remembered_and_offered_back(tmp_path: Path) -> None:
    """`Response.session_id` is the environment's whole say in session
    persistence — it hands one back and the listener stores it, so the next run
    for the same task is built with it to resume into. A sandbox run's id
    arrives over the wire exactly like a local one's, which is why this lives
    here and not in either environment."""
    from issuebot.sessions import SessionStore

    store = SessionStore(tmp_path / "sessions.json")
    ex = StubEnvironment(Response(status="done", session_id="sess-1"))
    api = ScriptedApi(_work_item())
    listener = ProjectListener(wiring(_PROJECT, api=api, environment=ex, context=ctx(store=store)))

    listener._process(_work_item())
    assert store.get("t1") == "sess-1"

    listener._process(_work_item())
    assert ex.runs[-1]["job"].resume_session_id == "sess-1"


def test_the_supervisor_asks_the_client_how_often_to_report(tmp_path: Path) -> None:
    """`from_config` reads *no* plugin's settings model.

    It used to read `telemetry_interval_seconds` and `install_name` off the
    installed source's settings by attribute name, so a second source that
    declared neither raised an `AttributeError` at startup — `summary_model`
    verbatim. Both facts now belong to the client, which reads its own table,
    and the fake below has no settings model at all: if core ever reaches for
    one again, this is what fails.
    """
    from issuebot.runner import Supervisor

    class BareClient:
        """A source client with the two install-wide facts and nothing else."""

        telemetry_interval = 99.0

    sup = Supervisor.from_config(
        config(connections=[]),
        BareClient(),  # ty: ignore[invalid-argument-type]
        FakeHarness(0),
    )

    assert sup._telemetry_interval == 99.0


# ---------------------------------------------------------------------------
# A broken config, and the runner-wide concurrency cap
# ---------------------------------------------------------------------------


def test_a_broken_config_edit_does_not_end_hot_reload(tmp_path: Path) -> None:
    """`load_config` raises on a config this install cannot honour — a typo, a
    plugin that isn't here, TOML that no longer parses. That reached the watch
    thread and killed it, so hot reload stopped for the life of the process and
    repairing the file never brought it back."""
    from issuebot.runner import Supervisor

    cfg_path = tmp_path / "config.toml"
    save_config(_config_with([_conn("a", "b-a")]), cfg_path)

    sup = Supervisor(RecordingApi(), FakeHarness(0), cfg_path, poll_interval=0.05)
    sup.start()
    try:
        assert _wait(lambda: "b-a" in sup.active_boards(), 2.0)

        cfg_path.write_text('harness = "fake"\nnonsense_key = 1\n')
        time.sleep(0.3)

        # The repair is the point: a watcher that died would never see it.
        save_config(_config_with([_conn("a", "b-a"), _conn("b", "b-b")]), cfg_path)
        assert _wait(lambda: "b-b" in sup.active_boards(), 2.0), (
            "hot reload stopped at the broken edit and never recovered"
        )
    finally:
        sup.stop()


def test_max_concurrent_caps_the_runner_not_each_connection() -> None:
    """`max_concurrent` counts the tasks this runner works at once. Each listener
    used to enforce it privately, so two connections at 1 ran two at a time."""
    started = threading.Semaphore(0)
    gate = threading.Event()

    def on_run(work, run_id, cancel):
        started.release()
        gate.wait(timeout=5)
        return Response(status="done")

    slots = threading.BoundedSemaphore(1)
    listeners = [
        ProjectListener(
            wiring(
                _PROJECT,
                api=_ConcurrentApi([_work_item(f"t{i}", f"ISS-{i}")]),
                environment=StubEnvironment(on_run=on_run),
            ),
            wait_timeout=1,
            slots=slots,
        )
        for i in (1, 2)
    ]

    threads = [threading.Thread(target=lis.run, daemon=True) for lis in listeners]
    for thread in threads:
        thread.start()
    try:
        assert started.acquire(timeout=5)
        # The other connection's item waits for the one slot this runner has.
        assert not started.acquire(timeout=0.3)
    finally:
        gate.set()
        for lis in listeners:
            lis.stop()
        for thread in threads:
            thread.join(timeout=5)


def test_a_slot_is_freed_when_the_claim_is_lost() -> None:
    """A slot is taken before claiming, so the path where nothing was claimed
    has to give it back — or the runner quietly loses capacity per lost race."""

    class LostRace:
        """A source that never wins a claim — what `poll` delivered is already
        somebody else's."""

        def claim(self, work):
            return None

    slots = threading.BoundedSemaphore(1)
    listener = ProjectListener(
        wiring(
            _PROJECT,
            api=ScriptedApi(_work_item()),
            environment=StubEnvironment(),
            source=LostRace(),
        ),
        slots=slots,
    )

    listener._process(_work_item())

    assert slots.acquire(blocking=False), "the slot was never given back"


def test_holding_the_runner_waits_for_a_run_and_stops_new_claims() -> None:
    """What an update needs before it replaces the files this process runs from:
    nothing new gets claimed, and the hold does not come back until the task
    already running has released its claim."""
    from issuebot.runner import Supervisor

    running = threading.Event()
    finish = threading.Event()

    def on_run(work, run_id, cancel):
        running.set()
        finish.wait(timeout=5)
        return Response(status="done")

    api = _ConcurrentApi([_work_item("t1", "ISS-1")])
    slots = threading.BoundedSemaphore(1)
    listener = ProjectListener(
        wiring(_PROJECT, api=api, environment=StubEnvironment(on_run=on_run)),
        wait_timeout=1,
        slots=slots,
    )

    sup = Supervisor(RecordingApi(), FakeHarness(0), "/tmp/none.toml")
    sup._slots, sup._slot_count = slots, 1

    thread = threading.Thread(target=listener.run, daemon=True)
    thread.start()
    try:
        assert running.wait(timeout=5)
        assert sup.hold(0.2) is False, "held while a run was still in flight"

        finish.set()
        assert sup.hold(5) is True

        # Held means held: the poll loop is still running, and cannot claim.
        claims_when_held = len(api.claims)
        api.serve(_work_item("t2", "ISS-2"))
        time.sleep(0.3)
        assert len(api.claims) == claims_when_held

        # Unclaimed work comes round again on the next poll, which is what makes
        # skipping it safe; resume, and that redelivery is claimed.
        sup.resume()
        api.serve(_work_item("t2", "ISS-2"))
        assert _wait(lambda: len(api.claims) > claims_when_held, 3.0), (
            "resume did not let the runner claim again"
        )
    finally:
        finish.set()
        listener.stop()
        thread.join(timeout=5)


def test_work_arriving_at_the_limit_is_held_until_a_slot_frees() -> None:
    """The board does not redeliver: a task assigned to this agent is simply
    assigned, and coming to get it is issuebot's job. Skipping at the limit
    silently lost the task until a human touched it."""
    slots = threading.BoundedSemaphore(1)
    assert slots.acquire(blocking=False)  # the runner is busy elsewhere

    api = ScriptedApi(_work_item())
    listener = ProjectListener(
        wiring(_PROJECT, api=api, environment=StubEnvironment()),
        slots=slots,
    )

    worker = threading.Thread(target=listener._process, args=(_work_item(),), daemon=True)
    worker.start()
    try:
        time.sleep(0.3)
        assert api.claims == []  # held, not claimed — the slot is not ours yet

        slots.release()  # the other run finishes
        assert _wait(lambda: api.claims == ["t1"], 3.0), "the held item was never run"
        assert api.released.wait(timeout=3)
    finally:
        listener.stop()
        worker.join(timeout=5)


def test_stop_releases_a_listener_waiting_for_a_slot() -> None:
    """The wait must notice stop(), or shutdown hangs behind a busy runner."""
    slots = threading.BoundedSemaphore(1)
    assert slots.acquire(blocking=False)

    api = ScriptedApi(_work_item())
    listener = ProjectListener(
        wiring(_PROJECT, api=api, environment=StubEnvironment()),
        slots=slots,
    )

    worker = threading.Thread(target=listener._process, args=(_work_item(),), daemon=True)
    worker.start()
    listener.stop()
    worker.join(timeout=3)

    assert not worker.is_alive()
    assert api.claims == []  # never claimed: nothing to strand


class OutstandingWorkApi(ScriptedApi):
    """A board that lists its item on every read until a claim takes it, and
    refuses the first claim.

    The level-triggered contract in one double: reading changes nothing, so the
    first poll's answer going unrun costs nothing — the item is on the next
    one."""

    def __init__(self, work_item: Any) -> None:
        super().__init__(work_item)
        self.taken = False

    def get_tasks(self, *, board_id: str | None = None, wait: int = 0) -> list[dict[str, Any]]:
        self.wait_board_ids.append(board_id)
        if self.taken:
            time.sleep(min(wait, 0.05))
            return []

        return [self._work_item]

    def claim(self, task_id: str, **kwargs: Any) -> dict[str, Any]:
        self.claims.append(task_id)
        self.claim_kwargs.append(kwargs)
        if len(self.claims) == 1:
            raise RuntimeError("board is down")

        self.taken = True
        return {"run_id": "r1", "task_id": task_id}


def test_work_the_listener_could_not_run_is_offered_again() -> None:
    """The property the periodic sweep used to provide: a poll is a pure read,
    so an item nothing managed to claim is simply on the next answer."""
    api = OutstandingWorkApi(_work_item())
    listener = ProjectListener(
        wiring(_PROJECT, api=api, environment=StubEnvironment()),
        wait_timeout=1,
    )

    thread = _run_listener(listener)
    assert api.released.wait(timeout=5), "the item was never offered a second time"
    listener.stop()
    thread.join(timeout=2)

    assert api.claims == ["t1", "t1"]
    assert api.releases == [{"run_id": "r1", "status": "done", "note": None}]
