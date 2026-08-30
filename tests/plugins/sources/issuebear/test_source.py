"""Tests for `Issuebear`'s own behaviour as a `Source` — the board-specific
half the conformance suite (`tests/plugins/sources/test_conformance.py`)
deliberately stays generic about.
"""

from __future__ import annotations

import io
import zipfile

from conftest import FakeApi, config, connection, ctx, mention, work
from issuebot import runner
from issuebot.contracts import (
    Answer,
    Changed,
    Changes,
    Claim,
    Handoff,
    NeedsInput,
    PullRequestRef,
    Response,
    SinkResult,
    SkillRef,
)
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
    assert api.mention_board_ids == ["b"]


def test_poll_returns_tasks_and_mentions_together():
    """Two board resources, one answer: the listener is handed everything
    outstanding without knowing there were two reads."""
    api = FakeApi(
        work_items=[
            {"task_id": "t1", "board_id": "b"},
            {"task_id": "t2", "board_id": "b", "kind": "mention", "notification_id": "n1"},
        ]
    )

    items = _source(api).poll(timeout=1)

    assert [(i.task_id, i.kind) for i in items] == [("t1", "assigned"), ("t2", "mention")]


def test_poll_carries_the_notification_a_mention_is_claimed_by():
    api = FakeApi(
        work_items=[{"task_id": "t2", "board_id": "b", "kind": "mention", "notification_id": "n1"}]
    )

    assert [i.notification_id for i in _source(api).poll(timeout=1)] == ["n1"]


def test_poll_filters_out_items_for_another_board():
    """Both work lists are agent-wide; belt-and-braces even though board_id
    already scoped each request server-side."""
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


def test_claiming_a_mention_acknowledges_its_notification():
    """The claim is what takes a mention off the board's list, and the run it
    opens is what the claim then carries."""
    api = FakeApi(run_id="r-resp")

    claim = _source(api).claim(mention(notification_id="n1"))

    assert claim == Claim(work_id="t1", token="r-resp")
    assert api.mention_claims == ["n1"]
    assert api.claims == []  # no run lock: a mention is not a race


def test_a_mention_claim_with_no_responding_run_carries_no_token():
    """The board opens no responding run when this agent already holds a
    working claim on the task, and a tokenless claim releases nothing."""
    api = FakeApi(run_id=None)
    source = _source(api)

    claim = source.claim(mention(notification_id="n1"))
    assert claim == Claim(work_id="t1", token="")

    source.release(claim, Response(status="done"))
    assert api.releases == []


def test_a_mention_with_no_notification_is_left_alone():
    """It cannot be claimed, so running it would never acknowledge it."""
    api = FakeApi()
    assert _source(api).claim(mention(notification_id=None)) is None
    assert api.mention_claims == []


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


def test_the_claim_says_what_the_run_will_actually_do():
    """The board can only show what is about to happen if the claim says so —
    and what is about to happen is the connection's answer, not the column's."""
    deferring = FakeApi()
    _source(deferring).claim(work(mode="research"))

    overriding = FakeApi()
    _source(overriding, mode="build").claim(work(mode="research"))

    assert deferring.claim_kwargs[0]["mode"] == "research"
    assert overriding.claim_kwargs[0]["mode"] == "edit_code"


def _pushed(**overrides) -> Changes:
    """A pushed branch with one changed file, as the environment derived it."""
    fields = {
        "branch": "issuebot/ISS-1",
        "base_sha": "aaa",
        "head_sha": "bbb",
        "stat": " one.py | 2 +-",
        "files_changed": 1,
        "pushed": True,
    }
    fields.update(overrides)
    return Changes(**fields)


def test_releasing_reports_what_the_run_did():
    api = FakeApi()
    response = Response(
        status="done",
        changes=_pushed(),
        outputs=[Changed(summary="added the endpoint")],
        sink_results=(
            SinkResult(
                sink="github",
                ok=True,
                summary="opened PR",
                url="https://github.com/acme/web/pull/7",
                pull_request=PullRequestRef(
                    repo="acme/web",
                    number=7,
                    url="https://github.com/acme/web/pull/7",
                    draft=True,
                ),
            ),
        ),
    )

    _source(api).release(Claim(work_id="t1", token="r1"), response)

    (result,) = api.release_results
    assert result["summary"] == "added the endpoint"
    assert result["changed_code"] is True
    assert result["pushed"] is True
    assert result["branches"] == [{"branch": "issuebot/ISS-1", "files_changed": 1}]
    assert result["pull_requests"] == [
        {
            "repo": "acme/web",
            "number": 7,
            "url": "https://github.com/acme/web/pull/7",
            "state": "open",
            "draft": True,
        }
    ]


def test_a_url_that_merely_looks_like_a_pull_request_is_not_reported_as_one():
    """The board is told about a pull request when a sink says it ended at one,
    never because the URL it reported has the shape of one. A sink is free to
    report any URL — a deployment, a preview, a comment — and only the sink that
    opened a pull request knows it opened one."""
    api = FakeApi()
    response = Response(
        status="done",
        changes=_pushed(),
        outputs=[Changed(summary="deployed it")],
        sink_results=(
            SinkResult(
                sink="mirror",
                ok=True,
                summary="mirrored the branch",
                url="https://mirror.example.com/acme/web/pull/7",
            ),
        ),
    )

    _source(api).release(Claim(work_id="t1", token="r1"), response)

    (result,) = api.release_results
    assert result["pull_requests"] == []


def test_a_branch_that_never_reached_origin_is_reported_as_unpushed():
    """The board is told the branch exists, and told separately that the work
    never left this machine — the runner did the pushing, so it knows."""
    api = FakeApi()
    response = Response(status="done", changes=_pushed(pushed=False))

    _source(api).release(Claim(work_id="t1", token="r1"), response)

    (result,) = api.release_results
    assert result["pushed"] is False
    assert result["branches"] == [{"branch": "issuebot/ISS-1", "files_changed": 1}]


def test_a_report_of_unpushed_work_says_why_the_branch_stayed_here():
    """The run's own report is where somebody looks next, so the reason the
    workspace recorded goes in it, after the agent's own words."""
    api = FakeApi()
    response = Response(
        status="done",
        outputs=[Changed(summary="added the endpoint")],
        changes=_pushed(pushed=False, push_detail="! [remote rejected] protected branch hook"),
    )

    _source(api).release(Claim(work_id="t1", token="r1"), response)

    (result,) = api.release_results
    assert "added the endpoint" in result["summary"]
    assert "protected branch hook" in result["summary"]


def test_a_run_that_answered_reports_the_answer_and_no_code():
    api = FakeApi()
    response = Response(status="done", outputs=[Answer(text="the cache is cold on boot")])

    _source(api).release(Claim(work_id="t1", token="r1"), response)

    (result,) = api.release_results
    assert result["summary"] == "the cache is cold on boot"
    assert result["changed_code"] is False
    assert result["pull_requests"] == []


# ---------------------------------------------------------------------------
# say / apply / finish
# ---------------------------------------------------------------------------


def test_say_posts_a_prefixed_comment():
    api = FakeApi()
    _source(api).say(work(), "hello")
    assert api.comments == [("t1", "issuebot: hello")]


# The board roster the hand-off tests resolve against.
_ROSTER = [
    {"name": "Sam Vimes", "user_id": "u-sam", "kind": "human"},
    # Agents carry their owner on the roster; people do not.
    {"name": "Hetzner", "user_id": "u-hetzner", "kind": "claude", "owner_id": "u-sam"},
]

_SAM_ID = "8f1d0b6e-2c47-4a1e-9c3b-0a5d6e7f8a90"


def test_a_handoff_decision_reassigns_the_task_without_narrating_it():
    """The agent's hand-off note is already on the thread — it posted it. What
    the board needs from the runner is the assignee, not a second telling.

    An assignee that is already a user id is what the board wants, so it goes
    through untouched and the roster is never fetched."""
    api = FakeApi(members=_ROSTER)

    _source(api).apply(work(), Handoff(assignee=_SAM_ID, note="over to you"))

    assert api.updates == [("t1", {"assignee_id": _SAM_ID})]
    assert api.comments == []
    assert api.member_lookups == []


def test_a_handoff_naming_a_member_assigns_that_members_user_id():
    """Agents write the name they know a person by, in whatever case and
    spacing they wrote it in; the board wants the user id."""
    api = FakeApi(members=_ROSTER)

    _source(api).apply(work(), Handoff(assignee="  hetzner ", note="over to you"))

    assert api.updates == [("t1", {"assignee_id": "u-hetzner"})]
    assert api.comments == []


def test_a_handoff_naming_nobody_leaves_the_task_alone_and_says_so():
    """An invented assignee used to 422 the patch, which also cost the run its
    closing report. Now the task keeps its assignee and a person is told what
    to fix by hand."""
    api = FakeApi(members=_ROSTER)

    _source(api).apply(work(), Handoff(assignee="Nobody At All", note="over to you"))

    assert api.updates == []
    assert len(api.comments) == 1
    text = api.comments[0][1]
    assert "Nobody At All" in text
    assert "Sam Vimes" in text and "Hetzner" in text


def test_an_ambiguous_handoff_name_leaves_the_task_alone():
    """Two members share the name, so there is no way to tell which was meant."""
    api = FakeApi(
        members=[
            {"name": "Sam", "user_id": "u-sam-1"},
            {"name": "sam", "user_id": "u-sam-2"},
        ]
    )

    _source(api).apply(work(), Handoff(assignee="Sam", note="over to you"))

    assert api.updates == []
    assert len(api.comments) == 1
    assert "Sam" in api.comments[0][1]


def test_a_handoff_to_the_agent_itself_goes_to_the_requester_instead():
    """An agent handing work to itself parks the task where nothing can move
    it: the only session that would pick it up is the one just ending. It goes
    back to whoever asked for the work, and a person is told why."""
    api = FakeApi(members=_ROSTER, task={"id": "t1", "reference": "ISS-1", "requester_id": "u-sam"})

    _source(api, agent_id="u-hetzner").apply(work(), Handoff(assignee="Hetzner", note="over to me"))

    assert api.updates == [("t1", {"assignee_id": "u-sam"})]
    assert len(api.comments) == 1
    assert "Sam Vimes" in api.comments[0][1]


def test_a_mention_handing_the_task_to_the_agent_itself_self_assigns():
    """A mention session ends where a work session cannot: taking the task on.
    Nothing is ending that would have picked it up — the assignment is what
    starts the work session — so it stands, and nobody is told otherwise."""
    api = FakeApi(members=_ROSTER, task={"id": "t1", "reference": "ISS-1", "requester_id": "u-sam"})

    _source(api, agent_id="u-hetzner").apply(
        mention(), Handoff(assignee="Hetzner", note="I will take this")
    )

    assert api.updates == [("t1", {"assignee_id": "u-hetzner"})]
    assert api.comments == []


def test_a_handoff_on_the_agents_own_task_goes_to_the_agents_owner():
    """An agent stays the requester of the follow-ups it raises, so the task it
    hands back names itself — the person to hand it to is the human who owns
    the agent, which only the roster can say."""
    api = FakeApi(
        members=_ROSTER, task={"id": "t1", "reference": "ISS-1", "requester_id": "u-hetzner"}
    )

    _source(api, agent_id="u-hetzner").apply(work(), Handoff(assignee="Hetzner", note="over to me"))

    assert api.updates == [("t1", {"assignee_id": "u-sam"})]
    assert "Sam Vimes" in api.comments[0][1]


def test_a_handoff_to_the_agent_itself_with_no_requester_changes_nothing():
    """With nobody to redirect to, the task keeps the assignee it has and the
    comment is the whole of the runner's answer — never an exception, which
    would cost the run its closing report."""
    api = FakeApi(members=_ROSTER, task={"id": "t1", "reference": "ISS-1"})

    _source(api, agent_id="u-hetzner").apply(work(), Handoff(assignee="Hetzner", note="over to me"))

    assert api.updates == []
    assert len(api.comments) == 1


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


def test_a_column_asking_for_research_bars_changes_on_a_board_deciding_connection():
    """The default connection does what the item's column asked for, so a
    research column's run cannot report `changes` any more than a mention can."""
    source = _source()
    assert "changes" not in source.permits(work(mode="research"))


def test_a_column_asking_for_nothing_still_builds():
    """The whole backwards-compatibility promise: an older board, or a column
    with nothing selected, leaves the run exactly as it was."""
    source = _source()
    assert "changes" in source.permits(work())


def test_an_overriding_connection_ignores_what_the_column_asked_for():
    """`build` and `respond` are overrides, not preferences: the item is not
    consulted at all."""
    assert "changes" in _source(mode="build").permits(work(mode="research"))
    assert "changes" not in _source(mode="respond").permits(work(mode="edit_code"))


def test_a_board_deciding_run_launches_with_the_columns_own_prompt():
    """The column composed the whole document, so that is what the agent is
    launched with — filled with the same tags the board's own documents use."""
    source = _source()
    item = work(reference="ISS-9", prompt="Research {reference} and report back.", mode="research")

    prompt = source.prompt(item, connection(), permits=source.permits(item))

    assert "Research ISS-9 and report back." in prompt


def test_a_run_whose_column_composed_nothing_uses_the_boards_own_document():
    source = _source()
    item = work(reference="ISS-9")

    prompt = source.prompt(item, connection(), permits=source.permits(item))

    # `work_task` is the only stub document that carries the confirm setting.
    assert "confirm:" in prompt


def test_an_overriding_connection_keeps_the_boards_document():
    """A connection told to respond responds, whatever the column composed —
    which is why the item carries both task documents as well as the prompt."""
    source = _source(mode="respond")
    item = work(reference="ISS-9", prompt="Change everything.", mode="edit_code")

    prompt = source.prompt(item, connection(mode="respond"), permits=source.permits(item))

    assert "Change everything." not in prompt
    assert "confirm:" not in prompt  # the board's `respond_task` document


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

    assert "confirm: yes" in waits
    assert "confirm: no" in straight_on


def test_a_work_prompt_names_the_agent_and_who_asked_for_the_task():
    """Without this the agent guesses a name when it hands work back — which is
    how a board display name ended up in an `assignee_id` field."""
    api = FakeApi(members=_ROSTER, task={"id": "t1", "reference": "ISS-9", "requester_id": "u-sam"})
    source = _source(api, agent_id="u-hetzner")
    item = work(reference="ISS-9")

    prompt = source.prompt(item, connection(), permits=source.permits(item))

    assert "u-hetzner" in prompt  # who the agent is
    assert "Sam Vimes" in prompt and "u-sam" in prompt  # who asked for the work
    # One task read and one roster read, however many facts came out of them.
    assert api.member_lookups == ["b"]


def test_a_work_prompt_renders_without_a_requester_the_board_cannot_name():
    """A board that will not answer must not cost the run its launch: the block
    loses the requester, the prompt keeps everything else."""
    api = FakeApi(members=_ROSTER, task={"id": "t1", "reference": "ISS-9"})
    source = _source(api, agent_id="u-hetzner")
    item = work(reference="ISS-9")

    prompt = source.prompt(item, connection(), permits=source.permits(item))

    assert "ISS-9" in prompt
    assert "u-hetzner" in prompt


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

    assert prompt.index("reconcile its branch") < prompt.index("Task ISS-9")
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


class _SkillClient(FakeApi):
    """A board that serves real skill content rather than `FakeApi`'s default
    empty bytes -- everything else about `download_skill` (tracking what was
    asked for, in ``skill_downloads``) it inherits unchanged."""

    def download_skill(self, skill_id: str) -> bytes:
        self.skill_downloads.append(skill_id)
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("board-planning/SKILL.md", "---\nname: board-planning\n---\nPlan well.")
        return buf.getvalue()


def test_agent_skills_materialises_what_the_item_carries(monkeypatch, tmp_path):
    """The board's own skill set, fetched and unpacked into a plugin directory
    -- `board_skills.build` does the unpacking, this source's own job is only
    to hand it the item's refs and the client's `download_skill`."""
    monkeypatch.setenv("HOME", str(tmp_path))  # keeps the on-disk cache out of ~/.issuebot
    client = _SkillClient()
    ref = SkillRef(id="s1", slug="board-planning", updated_at="v1")

    bundle = _source(client).agent_skills(work(skills=(ref,)))

    assert bundle.plugin_dir is not None
    assert client.skill_downloads == ["s1"]


def test_agent_skills_is_empty_when_the_item_carries_none():
    """A board that selected no skills for this item gets no plugin dir --
    the intended answer, not a degraded one."""
    assert _source().agent_skills(work()).plugin_dir is None


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


def test_losing_the_claim_race_is_reported(caplog):
    """A stale lock from a crashed run stops every poll silently otherwise."""
    api = FakeApi(claim_error=AlreadyClaimed("t1"))

    with caplog.at_level("INFO", logger="issuebot"):
        assert _source(api).claim(work()) is None

    assert "run lock held elsewhere" in caplog.text
