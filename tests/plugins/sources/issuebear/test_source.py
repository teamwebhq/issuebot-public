"""Tests for `Issuebear`'s own behaviour as a `Source` — the board-specific
half the conformance suite (`tests/plugins/sources/test_conformance.py`)
deliberately stays generic about.
"""

from __future__ import annotations

from conftest import FakeApi, config, connection, ctx, mention, work
from issuebot import runner
from issuebot.contracts import Answer, Changed, Claim, Handoff, NeedsInput, Response, SinkResult
from issuebot.plugins.sources.issuebear import messages
from issuebot.plugins.sources.issuebear.client import AlreadyClaimed
from issuebot.plugins.sources.issuebear.source import Issuebear
from issuebot.plugins.workspaces.base import WorkspaceProblem


def _source(
    client: FakeApi | None = None, *, agent_id: str | None = None, **overrides
) -> Issuebear:
    """This source over a fake board, with a board endpoint and PAT it can hand
    an agent. ``overrides`` are the *connection's*; `agent_id` is the source's own."""
    return Issuebear(
        client or FakeApi(),
        board="b",
        connection=connection(**overrides),
        mcp_url="https://board/mcp",
        pat="pat-123",
        agent_id=agent_id,
    )


# ---------------------------------------------------------------------------
# poll
# ---------------------------------------------------------------------------


def test_poll_scopes_to_this_connections_board():
    api = FakeApi(work_items=[{"task_id": "t1", "board_id": "b"}])
    items = _source(api).poll(timeout=1)
    assert [i.task_id for i in items] == ["t1"]
    assert api.wait_board_ids == ["b"]


def test_poll_filters_out_items_for_another_board():
    """/me/work is agent-wide; belt-and-braces even though board_id already
    scoped the request server-side."""
    api = FakeApi(work_items=[{"task_id": "t1", "board_id": "OTHER"}])
    assert _source(api).poll(timeout=1) == []


# ---------------------------------------------------------------------------
# claim / release
# ---------------------------------------------------------------------------


def test_claiming_an_assignment_locks_it_on_the_board():
    api = FakeApi(run_id="r1")
    claim = _source(api).claim(work())
    assert claim == Claim(work_id="t1", token="r1")
    assert api.claims == ["t1"]


def test_claiming_a_mention_never_calls_the_board():
    """The board already opened the responding run; claiming one is not a
    race, so this never touches the network."""
    api = FakeApi()
    claim = _source(api).claim(mention(run_id="r-resp"))
    assert claim == Claim(work_id="t1", token="r-resp")
    assert api.claims == []


def test_claiming_a_mention_with_no_run_id_still_returns_a_claim():
    """Older servers omit the responding run — the claim carries no token,
    and release() treats that as nothing to release."""
    claim = _source().claim(mention(run_id=None))
    assert claim == Claim(work_id="t1", token=None)


def test_losing_the_claim_race_returns_none():
    api = FakeApi(claim_error=AlreadyClaimed("t1"))
    assert _source(api).claim(work()) is None


def test_a_transient_claim_failure_returns_none_rather_than_raising():
    api = FakeApi(claim_error=RuntimeError("connection reset"))
    assert _source(api).claim(work()) is None


def test_release_reports_done_or_failed_with_the_result_text():
    api = FakeApi()
    source = _source(api)

    source.release(Claim(work_id="t1", token="r1"), Response(status="done", result_text="ok"))
    source.release(Claim(work_id="t1", token="r1"), Response(status="failed", result_text="broke"))

    assert api.releases == [
        {"run_id": "r1", "status": "done", "note": "ok"},
        {"run_id": "r1", "status": "failed", "note": "broke"},
    ]


def test_release_with_no_token_does_nothing():
    api = FakeApi()
    _source(api).release(Claim(work_id="t1", token=None), Response(status="done"))
    assert api.releases == []


# ---------------------------------------------------------------------------
# say / apply / finish
# ---------------------------------------------------------------------------


def test_say_posts_a_prefixed_comment():
    api = FakeApi()
    _source(api).say(work(), "hello")
    assert api.comments == [("t1", "issuebot: hello")]


def test_a_handoff_decision_reassigns_the_task_without_narrating_it():
    """The agent's hand-off note is already on the thread — it posted it. What
    the board needs from the runner is the assignee, not a second telling."""
    api = FakeApi()

    _source(api).apply(work(), Handoff(assignee="sam", note="over to you"))

    assert api.updates == [("t1", {"assignee_id": "sam"})]
    assert api.comments == []


def test_a_needs_input_decision_marks_the_task_awaiting_input_only():
    """The question reached the thread as the agent's own comment. Posting it
    again in the runner's voice is why one question arrived three times."""
    api = FakeApi()

    _source(api).apply(work(), NeedsInput(question="which environment?"))

    assert api.updates == [("t1", {"status": "needs_input"})]
    assert api.comments == []


def test_finishing_reports_what_the_sinks_did():
    """So the source can say "PR opened: …" without knowing what a PR is."""
    api = FakeApi()
    response = Response(status="done", outputs=[Changed(summary="fixed the bug")])
    result = SinkResult(sink="pr-forge", ok=True, summary="opened PR", url="https://x/pull/1")

    _source(api).finish(work(), response, [result])

    assert len(api.comments) == 1
    text = api.comments[0][1]
    assert "opened PR" in text
    assert "https://x/pull/1" in text
    # Once, in the runner's voice — this used to read "issuebot: issuebot: …".
    assert text.count(messages.PREFIX) == 1


def test_finishing_a_run_the_agent_narrated_posts_nothing():
    """One run, one response on the board. The agent answered in its own
    comment; a clean run with no sinks leaves the runner nothing to add."""
    api = FakeApi()

    _source(api).finish(work(), Response(status="done", outputs=[Answer(text="42")]), [])

    assert api.comments == []


# ---------------------------------------------------------------------------
# permits / prompt / agent_access
# ---------------------------------------------------------------------------


def test_an_assignment_permits_every_kind():
    assert _source().permits(work()) == {"changes", "answer", "needs_input", "handoff"}


def test_a_mention_cannot_produce_changes():
    """Not because mentions are special-cased downstream, but because this
    source says so about its own work kinds."""
    assert "changes" not in _source().permits(mention())


def test_a_respond_mode_assignment_cannot_produce_changes_either():
    """The design's "respond inversion": a `mode="respond"` connection must
    not be told (via the read-only template) that it must not touch the
    workspace while still being permitted to report `changes`. `permits`
    reads the connection, not only the work kind, to close that gap."""
    source = _source(mode="respond")
    assert source.permits(work()) == {"answer", "needs_input", "handoff"}


def test_a_respond_mode_mention_is_unaffected():
    """Already excluded `changes` on its own; folding `mode` in must not add
    a second, contradictory restriction."""
    source = _source(mode="respond")
    assert source.permits(mention()) == {"answer", "needs_input", "handoff"}


def test_an_assignment_prompt_carries_the_connections_done_setting():
    source = _source()
    item = work(reference="ISS-9")
    prompt = source.prompt(item, connection(done="complete"), permits=source.permits(item))
    assert "ISS-9" in prompt
    # The setting the test is named for: the agent is told how to finish.
    assert "complete" in prompt


def test_an_assignment_prompt_carries_the_connections_confirm_setting():
    """A connection that wants no sign-off must not get a prompt telling the
    agent to wait for one — the whole point of the setting."""
    source = _source()
    item = work(reference="ISS-9")

    waits = source.prompt(item, connection(confirm=True), permits=source.permits(item))
    straight_on = source.prompt(item, connection(confirm=False), permits=source.permits(item))

    assert "confirm before building: **yes**" in waits
    assert "confirm before building: **no**" in straight_on
    # Both plan, whatever they do about approval.
    assert "set_plan" in waits and "set_plan" in straight_on


def test_a_workspace_problem_prepends_the_reconcile_preamble():
    """When `run.execute` re-renders the prompt with a divergence the workspace
    reported, the reconcile instructions come first and the work prompt is
    intact underneath."""
    source = _source()
    item = work(reference="ISS-9")
    problem = WorkspaceProblem(
        kind="diverged-branch", detail="ff-only failed", branch="issuebot/ISS-9"
    )

    prompt = source.prompt(item, connection(), permits=source.permits(item), problem=problem)

    assert prompt.index("reconcile its branch") < prompt.index("Task: **ISS-9**")
    assert "origin/issuebot/ISS-9" in prompt


def test_a_mention_prompt_carries_who_said_what():
    source = _source(agent_id="u-agent")
    item = mention(actor_name="Ada", comment_excerpt="what do you think?")
    prompt = source.prompt(item, connection(), permits=source.permits(item))
    assert "Ada" in prompt
    assert "what do you think?" in prompt
    assert "u-agent" in prompt


def test_agent_access_hands_the_agent_this_boards_own_mcp_server():
    (server,) = _source().agent_access(work())

    # The name is the agent's tool prefix (`mcp__<name>__get_task`) and the key
    # `--strict-mcp-config` isolates on, so it is user-visible surface, not an
    # internal label. This plugin's own registered name, the same one `user_mcp`
    # registers install-wide — the two must not drift.
    assert server.name == Issuebear.name
    assert server.type == "http"
    assert server.url == "https://board/mcp"
    assert server.headers["Authorization"] == "Bearer pat-123"


def test_the_configured_endpoint_and_credential_reach_the_agent():
    """This plugin's `[issuebear]` table is what an agent's board channel is
    built from — end to end, through the factory that splats it in.

    Built with `source_for` rather than by calling the class, because the hop
    under test is the table becoming constructor keywords: `mcp_url` and `pat`
    were a write-only field and an unread setting for exactly as long as nothing
    asserted an agent ever saw them.
    """
    table = {
        Issuebear.name: {
            "api_url": "https://api",
            "mcp_url": "https://configured/mcp",
            "pat": "pat-from-config",
        }
    }
    source = runner.source_for(FakeApi(), connection(), ctx(plugin_settings=table))

    (server,) = source.agent_access(work())

    assert server.url == "https://configured/mcp"
    assert server.headers["Authorization"] == "Bearer pat-from-config"


def test_the_board_server_an_agent_gets_is_the_one_the_user_registers():
    """`user_mcp` (install-wide, from a `Config`) and `agent_access` (this run)
    describe the same server — one board, two lifetimes — so a harness's global
    registration and an autonomous launch cannot disagree about how to reach it.

    Neither side is derived from the other here: one goes settings table →
    constructor keywords → `agent_access`, the other `Config` → `global_settings`
    → `user_mcp`, from the same configured board."""
    source = runner.source_for(FakeApi(), connection(), ctx())

    (per_run,) = source.agent_access(work())

    assert per_run == Issuebear.user_mcp(config())


# ---------------------------------------------------------------------------
# heartbeat: not part of the ABC, but run.execute's own narrow protocol
# ---------------------------------------------------------------------------


def test_heartbeat_delegates_to_the_client():
    api = FakeApi()
    _source(api).heartbeat("r1")
    assert api.heartbeats == ["r1"]
