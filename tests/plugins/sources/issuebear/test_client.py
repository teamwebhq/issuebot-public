from __future__ import annotations

import json

import httpx
import pytest

from issuebot.agent_state import ConnectionSnapshot
from issuebot.plugins.sources.issuebear.client import (
    AlreadyClaimed,
    ApiError,
    ConnectionConflict,
    IssuebotClient,
)


def _client(handler, **settings) -> IssuebotClient:
    """Build a client backed by a MockTransport using the given handler."""
    transport = httpx.MockTransport(handler)
    return IssuebotClient(
        api_url="https://issuebear.example/api",
        pat="secret-token",
        transport=transport,
        **settings,
    )


# The two work reads are the same shape over different resources, so their
# shared promises — path, params, 204 — are proved once for both.
WORK_READS = [("get_tasks", "/api/me/work/tasks"), ("get_mentions", "/api/me/work/mentions")]


@pytest.mark.parametrize(("method", "path"), WORK_READS)
def test_a_work_read_asks_its_own_path_with_board_id_and_wait(method: str, path: str):
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("authorization", "")
        seen["path"] = request.url.path
        seen["board_id"] = request.url.params.get("board_id", "")
        seen["wait"] = request.url.params.get("wait", "")
        return httpx.Response(200, json=[{"task_id": "t1"}])

    client = _client(handler)
    try:
        work = getattr(client, method)(board_id="b-5", wait=5)
    finally:
        client.close()

    assert seen["auth"] == "Bearer secret-token"
    assert seen["path"] == path
    assert seen["board_id"] == "b-5"
    assert seen["wait"] == "5"
    assert work == [{"task_id": "t1"}]


@pytest.mark.parametrize(("method", "path"), WORK_READS)
def test_a_work_read_treats_204_as_nothing_outstanding(method: str, path: str):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == path
        return httpx.Response(204)

    client = _client(handler)
    try:
        assert getattr(client, method)(wait=5) == []
    finally:
        client.close()


@pytest.mark.parametrize(("method", "path"), WORK_READS)
def test_a_work_read_without_board_id_omits_the_param(method: str, path: str):
    seen: dict[str, dict[str, str]] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["params"] = dict(request.url.params)
        return httpx.Response(204)

    client = _client(handler)
    try:
        getattr(client, method)(wait=1)
    finally:
        client.close()

    assert "board_id" not in seen["params"]


@pytest.mark.parametrize(("method", "path"), WORK_READS)
def test_a_work_read_with_a_non_json_body_raises_api_error(method: str, path: str):
    # A gateway/proxy can answer a parked read with a non-JSON error page; we
    # must surface a clean ApiError, not let json.JSONDecodeError escape.
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>502 Bad Gateway</html>")

    client = _client(handler)
    try:
        with pytest.raises(ApiError) as exc_info:
            getattr(client, method)(wait=5)
    finally:
        client.close()

    assert exc_info.value.status == 200
    assert "non-JSON" in exc_info.value.detail


def test_claim_mention_posts_to_the_notifications_claim_path():
    """The claim is what acknowledges a mention, so it must reach the board."""
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["path"] = request.url.path
        return httpx.Response(200, json={"notification_id": "n-1", "task_id": "t1", "run_id": "r1"})

    client = _client(handler)
    try:
        result = client.claim_mention("n-1")
    finally:
        client.close()

    assert seen["method"] == "POST"
    assert seen["path"] == "/api/me/work/mentions/n-1/claim"
    assert result["run_id"] == "r1"


def test_claim_mention_can_answer_with_no_responding_run():
    """The board sends no run when this agent already holds a working claim on
    the task; the client passes that through rather than inventing one."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"notification_id": "n-1", "task_id": "t1", "run_id": None})

    client = _client(handler)
    try:
        assert client.claim_mention("n-1")["run_id"] is None
    finally:
        client.close()


def test_claim_heartbeat_release_post_expected_paths():
    calls: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        if request.url.path.endswith("/claim"):
            return httpx.Response(200, json={"run_id": "r1", "task_id": "t1"})
        if request.url.path.endswith("/release"):
            body = json.loads(request.content)
            assert body["status"] == "done"
            return httpx.Response(200, json={})
        return httpx.Response(200, json={})

    client = _client(handler)
    try:
        claimed = client.claim("t1")
        assert claimed == {"run_id": "r1", "task_id": "t1"}

        client.heartbeat("r1")
        client.release("r1", status="done", note="finished")
    finally:
        client.close()

    assert calls == [
        ("POST", "/api/tasks/t1/claim"),
        ("POST", "/api/agent-runs/r1/heartbeat"),
        ("POST", "/api/agent-runs/r1/release"),
    ]


def test_claim_409_raises_already_claimed():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(409, json={"detail": "already claimed"})

    client = _client(handler)
    try:
        with pytest.raises(AlreadyClaimed):
            client.claim("t1")
    finally:
        client.close()


def test_claim_sends_install_and_executor_and_patch_execution():
    seen = []

    def handler(request):
        body = json.loads(request.content) if request.content else None
        seen.append((request.method, request.url.path, body))
        if request.url.path.endswith("/claim"):
            return httpx.Response(200, json={"run_id": "R1", "task_id": "T1"})
        return httpx.Response(200, json={"ok": True})

    client = IssuebotClient(api_url="https://b", pat="p", transport=httpx.MockTransport(handler))

    client.claim("T1", install_id="I1", executor="somewhere")
    client.patch_execution("R1", sandbox_id="sbx", sandbox_status="running")

    assert ("POST", "/tasks/T1/claim", {"install_id": "I1", "executor": "somewhere"}) in seen
    assert (
        "POST",
        "/agent-runs/R1/execution",
        {"sandbox_id": "sbx", "sandbox_status": "running"},
    ) in seen


def test_sandbox_lifecycle_translates_to_execution_metadata():
    """`sandbox_started`/`sandbox_destroyed` are the neutral capability
    (`plugins.sources.base.SandboxLifecycle`); the executor/sandbox_* column
    vocabulary and the timestamps are this client's own translation."""
    seen = []

    def handler(request):
        seen.append((request.url.path, json.loads(request.content)))
        return httpx.Response(200, json={"ok": True})

    client = IssuebotClient(api_url="https://b", pat="p", transport=httpx.MockTransport(handler))

    client.sandbox_started("R1", environment="railway", sandbox_id="sbx")
    client.sandbox_destroyed("R1")

    path, started = seen[0]
    assert path == "/agent-runs/R1/execution"
    assert started["executor"] == "railway"
    assert started["sandbox_id"] == "sbx"
    assert started["sandbox_status"] == "running"
    assert "sandbox_created_at" in started

    path, destroyed = seen[1]
    assert path == "/agent-runs/R1/execution"
    assert destroyed["sandbox_status"] == "destroyed"
    assert "sandbox_destroyed_at" in destroyed


def test_claim_without_extras_sends_no_body():
    seen = []

    def handler(request):
        seen.append(json.loads(request.content) if request.content else None)
        return httpx.Response(200, json={"run_id": "R1", "task_id": "T1"})

    client = IssuebotClient(api_url="https://b", pat="p", transport=httpx.MockTransport(handler))
    client.claim("T1")
    assert seen == [None] or seen == [{}]  # no install/executor → empty/no body


def test_4xx_raises_api_error_with_status():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"detail": "not found"})

    client = _client(handler)
    try:
        with pytest.raises(ApiError) as exc_info:
            client.get_task("missing")
    finally:
        client.close()

    assert exc_info.value.status == 404
    assert exc_info.value.detail == "not found"


def test_4xx_non_json_body_raises_api_error_with_text():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(502, text="Bad Gateway")

    client = _client(handler)
    try:
        with pytest.raises(ApiError) as exc_info:
            client.get_task("t1")
    finally:
        client.close()

    assert exc_info.value.status == 502
    assert exc_info.value.detail == "Bad Gateway"


def test_report_telemetry_posts_body_and_auth():
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["auth"] = request.headers.get("authorization", "")
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"status": "ok"})

    client = _client(handler)
    try:
        client.report_telemetry(
            version="0.1.0",
            install_id="srv-x",
            hostname="host-a",
            connections=[
                ConnectionSnapshot(
                    name="p",
                    board="b-1",
                    target="/work/repo",
                    phase="working",
                    ref="ISS-1",
                    log_tail="line1\nline2",
                    links=[{"branch": "issuebot/ISS-1"}],
                )
            ],
        )
    finally:
        client.close()

    # The wire schema is this board's own (board_id, activity_phase) — the
    # runner's snapshot vocabulary is translated inside the client.
    assert seen["path"] == "/api/me/telemetry"
    assert seen["auth"] == "Bearer secret-token"
    assert seen["body"] == {
        "version": "0.1.0",
        "install_id": "srv-x",
        "hostname": "host-a",
        "connections": [
            {
                "board_id": "b-1",
                "activity_phase": "working",
                "log_tail": "line1\nline2",
                "links": [{"branch": "issuebot/ISS-1"}],
            }
        ],
    }


def test_report_telemetry_includes_install_id():
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"status": "ok"})

    client = _client(handler)
    try:
        client.report_telemetry(version="0.2.0", install_id="srv-1", hostname="h", connections=[])
    finally:
        client.close()

    assert seen["body"] == {
        "version": "0.2.0",
        "install_id": "srv-1",
        "hostname": "h",
        "connections": [],
    }


def test_wait_for_commands_204_returns_empty_list():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(204)

    client = _client(handler)
    try:
        assert client.wait_for_commands(timeout=0) == []
    finally:
        client.close()


def test_wait_for_commands_returns_list():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/me/commands/wait"
        return httpx.Response(200, json=[{"id": "c1", "kind": "restart"}])

    client = _client(handler)
    try:
        assert client.wait_for_commands(timeout=0) == [{"id": "c1", "kind": "restart"}]
    finally:
        client.close()


def test_wait_for_commands_sends_install_id():
    seen: dict[str, dict[str, str]] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["params"] = dict(request.url.params)
        return httpx.Response(204)

    client = _client(handler)
    try:
        client.wait_for_commands(install_id="inst-42", timeout=0)
    finally:
        client.close()

    assert seen["params"].get("install_id") == "inst-42"


def test_wait_for_commands_omits_install_id_when_none():
    seen: dict[str, dict[str, str]] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["params"] = dict(request.url.params)
        return httpx.Response(204)

    client = _client(handler)
    try:
        client.wait_for_commands(timeout=0)
    finally:
        client.close()

    assert "install_id" not in seen["params"]


def test_ack_command_posts_status_and_result():
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"status": "done"})

    client = _client(handler)
    try:
        client.ack_command("c1", status="done", result="restarting")
    finally:
        client.close()

    assert seen["path"] == "/api/me/commands/c1/ack"
    assert seen["body"] == {"status": "done", "result": "restarting"}


def test_connect_posts_name_and_returns_body():
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/api/boards/b-1/agent-connection"
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"connected": True, "warning": "not a member"})

    client = _client(handler)
    try:
        assert client.connect("b-1", "frontend")["warning"] == "not a member"
    finally:
        client.close()

    assert seen["body"] == {"name": "frontend", "install_id": None}


def test_connect_sends_install_id():
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={})

    client = _client(handler)
    try:
        client.connect("b-2", "myconn", install_id="inst-99")
    finally:
        client.close()

    assert seen["body"] == {"name": "myconn", "install_id": "inst-99"}


def test_connect_409_raises_connection_conflict():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(409, json={"detail": "already connected"})

    client = _client(handler)
    try:
        with pytest.raises(ConnectionConflict):
            client.connect("b-1", "frontend")
    finally:
        client.close()


def test_disconnect_sends_delete():
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["path"] = request.url.path
        return httpx.Response(200, json={})

    client = _client(handler)
    try:
        client.disconnect("b-1")
    finally:
        client.close()

    assert seen["method"] == "DELETE"
    assert seen["path"] == "/api/boards/b-1/agent-connection"


def test_register_install_posts_and_returns_id() -> None:
    """register_install POSTs to /me/installs and returns the minted id."""
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["path"] = request.url.path
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"id": "srv-1", "hostname": "h", "name": None})

    client = _client(handler)
    try:
        result = client.register_install("h")
    finally:
        client.close()

    assert result == "srv-1"
    assert seen["method"] == "POST"
    assert seen["path"] == "/api/me/installs"
    assert seen["body"] == {"hostname": "h", "name": None}


def test_register_install_sends_its_own_configured_name() -> None:
    """The install's name is this plugin's own `[issuebear] install_name`, read
    by the client at construction — core says which machine is registering and
    nothing about what it is called, so a source that has no such setting is
    not obliged to invent one."""
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"id": "srv-2", "hostname": "myhost", "name": "my-agent"})

    client = _client(handler, install_name="my-agent")
    try:
        result = client.register_install("myhost")
    finally:
        client.close()

    assert result == "srv-2"
    assert seen["body"] == {"hostname": "myhost", "name": "my-agent"}
