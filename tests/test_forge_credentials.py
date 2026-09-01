"""Working as the board's GitHub App, not as whoever's token is on the machine.

A runner pushes branches, opens pull requests and comments on them. Doing all
of that with a personal access token puts one human's name on every agent's
work. So the board is asked to lend the app's own short-lived, repo-scoped
credential for the task being worked, and everything that talks to GitHub — the
agent itself, git, and the sink — uses it.

The fallback is the whole point of the shape: a board with nothing to lend (an
unlinked project, no app installed, an older server) or a board that cannot be
reached leaves the run using whatever credential the machine already holds,
exactly as before. Nothing here may fail a run.
"""

from __future__ import annotations

from typing import Any

import httpx

from conftest import FakeApi, FakeWorkspace, RecordingReporter, connection, ctx, wiring, work
from issuebot import runner
from issuebot.config import SinkRef
from issuebot.contracts import Changed, Changes, Delivery, Job, Response, SinkResult
from issuebot.plugins.environments.base import ExecutionEnvironment
from issuebot.plugins.harnesses.fake.harness import FakeHarness
from issuebot.plugins.sinks.github.sink import GitHubSink
from issuebot.plugins.sources.issuebear.client import IssuebotClient
from issuebot.plugins.sources.issuebear.source import Issuebear
from issuebot.process import RecordingProcess
from issuebot.run import RESPONSE_ENV, execute

LENT = {
    "token": "ghs_lent",
    "expires_at": "2026-08-22T13:00:00Z",
    "repo_full_name": "acme/web",
    "clone_url": "https://github.com/acme/web.git",
    "app_login": "issuebear[bot]",
    "author_name": "PushBot",
    "author_email": "pushbot@agents.invalid",
}


class _Board(FakeApi):
    """A board that lends credentials, lends nothing, or cannot answer."""

    def __init__(self, credentials: dict[str, Any] | None = LENT, error: Exception | None = None):
        super().__init__()
        self._credentials = credentials
        self._error = error
        self.asked: list[str] = []

    def git_credentials(self, task_id: str) -> dict[str, Any] | None:
        self.asked.append(task_id)
        if self._error is not None:
            raise self._error
        return self._credentials


def _source(board: _Board) -> Issuebear:
    return Issuebear(
        board, board="b", connection=connection(), mcp_url="https://board/mcp", pat="pat-123"
    )


# ---------------------------------------------------------------------------
# What the board lends, and what it becomes
# ---------------------------------------------------------------------------


def test_the_app_token_and_the_clankers_authorship_travel_together():
    """The app is the actor GitHub shows; the clanker is the author on the
    commits — the only two identities there are to set."""
    env = _source(_Board()).forge_env(work(task_id="t1"))

    assert env["GH_TOKEN"] == "ghs_lent"
    assert env["GIT_AUTHOR_NAME"] == "PushBot"
    assert env["GIT_AUTHOR_EMAIL"] == "pushbot@agents.invalid"
    assert env["GIT_COMMITTER_NAME"] == "PushBot"


def test_git_is_pointed_at_the_lent_token_even_in_a_checkout_we_did_not_clone():
    """A worktree cut from the developer's own repository never saw issuebot's
    clone-time credential helper, so its push would authenticate with the
    machine's keychain — the very credential this exists to stop using."""
    env = _source(_Board()).forge_env(work())

    assert env["GIT_CONFIG_COUNT"] == "1"
    assert env["GIT_CONFIG_KEY_0"] == "credential.https://github.com.helper"
    assert env["GIT_CONFIG_VALUE_0"] == "!gh auth git-credential"


def test_a_board_with_nothing_to_lend_leaves_the_machines_own_credential():
    assert _source(_Board(credentials=None)).forge_env(work()) == {}


def test_a_board_that_cannot_answer_never_fails_the_run():
    board = _Board(error=RuntimeError("board unreachable"))

    assert _source(board).forge_env(work()) == {}


def test_the_client_reads_a_404_as_nothing_to_lend():
    """The board answers 404 for every "no credentials here" — unlinked, no
    installation, app not configured — so the runner has one case to handle."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"detail": "no GitHub credentials for this task"})

    client = IssuebotClient(
        api_url="https://board", pat="p", transport=httpx.MockTransport(handler)
    )

    assert client.git_credentials("t1") is None


# ---------------------------------------------------------------------------
# Where they reach
# ---------------------------------------------------------------------------


def test_the_job_carries_what_the_board_lent():
    board = _Board()
    w = wiring(connection(), api=board, source=_source(board), context=ctx())

    job = runner.job_for(work(), w)

    assert job.forge_env["GH_TOKEN"] == "ghs_lent"


class _RecordingWorkspace(FakeWorkspace):
    """A workspace that keeps the process adapter it was handed."""

    def __init__(self) -> None:
        super().__init__()
        self.procs: list[Any] = []

    def commit_and_push(self, prepared, message, *, settings, proc=None):
        self.procs.append(proc)
        return super().commit_and_push(prepared, message, settings=settings, proc=proc)


def test_the_run_pushes_and_works_with_the_lent_token():
    """Both halves of a run touch GitHub: git, when the workspace pushes, and
    the agent itself, which comments on the PR with `gh`."""
    launched: list[dict[str, str]] = []
    workspace = _RecordingWorkspace()
    proc = RecordingProcess()

    job = Job(
        work=work(),
        prompt="do it",
        folder="/tmp/p",
        permits=frozenset({"changes"}),
        withheld_tools=(),
        timeout_minutes=None,
        mcp_servers=(),
        env={},
        resume_session_id=None,
        forge_env={"GH_TOKEN": "ghs_lent"},
    )
    w = wiring(
        connection(),
        harness=FakeHarness(on_launch=lambda spec: launched.append(dict(spec.env))),
        workspace=workspace,
        source=_source(FakeApi()),
        context=ctx(),
    )

    execute(job, w, reporter=RecordingReporter(), proc=proc)

    assert launched[0]["GH_TOKEN"] == "ghs_lent"
    assert RESPONSE_ENV in launched[0]

    # The workspace's own git calls carry it too, whatever adapter it was given.
    workspace.procs[0].run(["git", "push"], cwd="/tmp/p")
    assert proc.envs[-1]["GH_TOKEN"] == "ghs_lent"


def test_the_pull_request_is_opened_as_the_app_and_signed_by_the_clanker():
    """`gh` authenticates with the lent token, so the PR's actor is the app —
    and the body says which clanker did the work, since GitHub offers no way to
    name an app's token per agent."""
    proc = RecordingProcess(
        replies={
            "gh api": _completed('{"ahead_by": 2}'),
            "gh pr list": _completed(""),
            "gh pr create": _completed("https://github.com/acme/web/pull/9\n"),
        }
    )
    delivery = Delivery(
        work=work(reference="ISS-1"),
        output=Changed(summary="did the thing"),
        changes=Changes(
            branch="issuebot/ISS-1",
            base_sha="base",
            head_sha="head",
            stat="1 file changed",
            files_changed=1,
            pushed=True,
        ),
        repo="https://github.com/acme/web.git",
        folder="",
        forge_env={"GH_TOKEN": "ghs_lent", "GIT_AUTHOR_NAME": "PushBot"},
    )

    result = GitHubSink(proc=proc).deliver(delivery)

    assert result.ok
    assert all(env and env["GH_TOKEN"] == "ghs_lent" for env in proc.envs)
    create = next(c for c in proc.calls if c[:3] == ["gh", "pr", "create"])
    assert "PushBot" in create[create.index("--body") + 1]


def _completed(out: str):
    from issuebot.process import Completed

    return Completed(["gh"], 0, out)


# ---------------------------------------------------------------------------
# Borrowing again, late in the run
# ---------------------------------------------------------------------------


class _RenewingBoard(_Board):
    """A board that lends a different token on every ask, so a test can tell
    which ask a run's credential came from."""

    def git_credentials(self, task_id: str) -> dict[str, Any] | None:
        super().git_credentials(task_id)
        return {**LENT, "token": f"ghs_{len(self.asked)}"}


class _OneShotBoard(_Board):
    """A board that lends once and cannot answer after that — the late
    re-borrow fails, as a board that went away mid-run would."""

    def git_credentials(self, task_id: str) -> dict[str, Any] | None:
        super().git_credentials(task_id)
        if len(self.asked) > 1:
            raise RuntimeError("board unreachable")
        return {**LENT, "token": "ghs_1"}


def _job(**overrides: Any) -> Job:
    """A job permitted to make changes, holding whatever was lent at the start
    of the run."""
    fields: dict[str, Any] = {
        "work": work(),
        "prompt": "do it",
        "folder": "/tmp/p",
        "permits": frozenset({"changes"}),
        "withheld_tools": (),
        "timeout_minutes": None,
        "mcp_servers": (),
        "env": {},
        "resume_session_id": None,
        "forge_env": {"GH_TOKEN": "ghs_start"},
    }
    fields.update(overrides)
    return Job(**fields)


def test_the_push_uses_a_token_borrowed_after_the_agent_finished():
    """A lent token lives an hour and the agent may work for longer, so the one
    on the Job can be dead by the time there is anything to push."""
    workspace = _RecordingWorkspace()
    proc = RecordingProcess()
    board = _RenewingBoard()
    w = wiring(connection(), workspace=workspace, source=_source(board), context=ctx())

    execute(_job(), w, reporter=RecordingReporter(), proc=proc)

    workspace.procs[0].run(["git", "push"], cwd="/tmp/p")
    assert proc.envs[-1]["GH_TOKEN"] == "ghs_1"


def test_a_board_that_cannot_answer_late_leaves_the_run_on_the_token_it_started_with():
    """A possibly-still-valid token beats no token at all: a failed re-borrow
    must never cost the run its push."""
    workspace = _RecordingWorkspace()
    proc = RecordingProcess()
    board = _Board(error=RuntimeError("board unreachable"))
    w = wiring(connection(), workspace=workspace, source=_source(board), context=ctx())

    execute(_job(), w, reporter=RecordingReporter(), proc=proc)

    workspace.procs[0].run(["git", "push"], cwd="/tmp/p")
    assert proc.envs[-1]["GH_TOKEN"] == "ghs_start"


def test_a_run_that_was_lent_nothing_asks_the_board_for_nothing_late():
    """Nothing was lent, so there is nothing to renew — and no board call to
    make for a run that authenticates with the machine's own credential."""
    board = _Board()
    w = wiring(connection(), workspace=_RecordingWorkspace(), source=_source(board), context=ctx())

    execute(_job(forge_env={}), w, reporter=RecordingReporter(), proc=RecordingProcess())

    assert board.asked == []


class _RecordingSink:
    """A sink that keeps the deliveries it was handed."""

    name = "pr"
    accepts = frozenset({"changes"})

    def __init__(self) -> None:
        self.deliveries: list[Delivery] = []

    def deliver(self, delivery: Delivery) -> SinkResult:
        self.deliveries.append(delivery)
        return SinkResult(sink=self.name, ok=True, summary="opened PR")


class _StubEnvironment(ExecutionEnvironment):
    """Stands in for the environment: reports a run that changed something,
    without running one."""

    name = "stub"

    def __init__(self) -> None:
        pass

    def run(self, job, *, reporter, cancel=None) -> Response:
        return Response(
            status="done",
            changes=Changes(
                branch="b", base_sha="a", head_sha="b2", stat="1 file", files_changed=1
            ),
            outputs=[Changed(summary="did stuff")],
        )


def _delivering_listener(board: _Board, sink: _RecordingSink) -> runner.ProjectListener:
    """A listener whose runs produce changes for ``sink`` to deliver."""
    return runner.ProjectListener(
        wiring(
            connection(git_init="branch"),
            api=board,
            source=_source(board),
            context=ctx(),
            environment=_StubEnvironment(),
            sinks=[(SinkRef(name="pr", required=True), sink)],
        )
    )


def test_the_pull_request_is_opened_with_a_token_borrowed_after_the_run():
    """`gh pr create` is the last thing a run does, and the furthest from the
    borrow that started it."""
    board = _RenewingBoard()
    sink = _RecordingSink()

    _delivering_listener(board, sink)._process(work())

    assert sink.deliveries[0].forge_env["GH_TOKEN"] == "ghs_2"


def test_delivery_falls_back_to_the_runs_own_token_when_the_board_cannot_answer():
    board = _OneShotBoard()
    sink = _RecordingSink()

    _delivering_listener(board, sink)._process(work())

    assert sink.deliveries[0].forge_env["GH_TOKEN"] == "ghs_1"
