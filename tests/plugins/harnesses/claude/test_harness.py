"""Claude-specific harness behaviour: argv shape, session resume, retry
detection — everything the shared conformance suite doesn't cover because it
is Claude-only."""

from __future__ import annotations

import json
import sys
import threading
import time

from conftest import SpawnRecorder
from issuebot.plugins.harnesses.base import LaunchSpec
from issuebot.plugins.harnesses.claude.harness import ClaudeHarness
from issuebot.process import RealProcess, RecordingProcess

# One server fragment, in the shape a source hands one over. This harness is
# told nothing about where it came from, which is what makes it worth asserting.
_BOARD = {"board": {"type": "http", "url": "https://board.example/mcp"}}


def _spec() -> LaunchSpec:
    return LaunchSpec(prompt="do the thing", folder="/work/alpha", mcp_servers=[_BOARD])


def test_claude_builds_argv_and_passes_cwd(reporter):
    spawn = SpawnRecorder(exit_code=7)
    harness = ClaudeHarness(command="claude", proc=spawn)

    result = harness.launch(_spec(), reporter)

    assert spawn.argv is not None
    assert spawn.argv[0] == "claude"
    assert "-p" in spawn.argv
    assert "do the thing" in spawn.argv
    assert spawn.cwd == "/work/alpha"
    assert result.exit_code == 7


def test_claude_uses_strict_mcp_config(reporter):
    spawn = SpawnRecorder()
    harness = ClaudeHarness(command="claude", proc=spawn)

    harness.launch(_spec(), reporter)

    assert spawn.argv is not None
    assert "--strict-mcp-config" in spawn.argv


def test_claude_skips_permissions_for_headless_autonomy(reporter):
    spawn = SpawnRecorder()
    harness = ClaudeHarness(command="claude", proc=spawn)

    harness.launch(_spec(), reporter)

    assert spawn.argv is not None
    assert "--dangerously-skip-permissions" in spawn.argv
    assert "--permission-mode" not in spawn.argv


def test_claude_uses_stream_json_output(reporter):
    spawn = SpawnRecorder()
    harness = ClaudeHarness(command="claude", proc=spawn)

    harness.launch(_spec(), reporter)

    assert spawn.argv is not None
    assert "--output-format" in spawn.argv
    assert "stream-json" in spawn.argv
    assert "--verbose" in spawn.argv


def test_claude_streams_lines_to_reporter(reporter):
    tool_line = json.dumps(
        {
            "type": "assistant",
            "message": {
                "content": [{"type": "tool_use", "name": "Read", "input": {"file_path": "x.py"}}]
            },
        }
    )
    spawn = SpawnRecorder(lines=[tool_line, "plain"])
    harness = ClaudeHarness(command="claude", proc=spawn)

    harness.launch(_spec(), reporter)

    # Every line is tee'd raw...
    assert tool_line in reporter.raw_lines
    assert "plain" in reporter.raw_lines
    # ...and the stream-json tool_use line is parsed into an event.
    assert any(ev.kind == "tool_use" for ev in reporter.events)
    # ...and the plain line still reaches the reporter as a raw event.
    assert any(ev.kind == "raw" for ev in reporter.events)


def test_claude_writes_the_launchs_own_mcp_servers_to_its_config_file(reporter):
    """Whatever the launch was handed, written out verbatim: this harness has no
    server name, transport or credential of its own to contribute."""
    spawn = SpawnRecorder()
    harness = ClaudeHarness(command="claude", proc=spawn)

    harness.launch(_spec(), reporter)

    assert spawn.mcp_json == {"mcpServers": _BOARD}


def test_claude_custom_command(reporter):
    spawn = SpawnRecorder()
    harness = ClaudeHarness(command="/usr/local/bin/claude", proc=spawn)

    harness.launch(_spec(), reporter)

    assert spawn.argv is not None
    assert spawn.argv[0] == "/usr/local/bin/claude"


def test_claude_adds_resume_when_session_id_present(reporter):
    spawn = SpawnRecorder()
    harness = ClaudeHarness(command="claude", proc=spawn)
    spec = LaunchSpec(
        prompt="do the thing",
        folder="/work/alpha",
        resume_session_id="sess-prior",
    )

    harness.launch(spec, reporter)

    assert spawn.argv is not None
    assert "--resume" in spawn.argv
    assert spawn.argv[spawn.argv.index("--resume") + 1] == "sess-prior"


def test_claude_omits_resume_without_session_id(reporter):
    spawn = SpawnRecorder()
    harness = ClaudeHarness(command="claude", proc=spawn)

    harness.launch(_spec(), reporter)

    assert spawn.argv is not None
    assert "--resume" not in spawn.argv


def test_claude_captures_session_id_from_result_event(reporter):
    result_line = json.dumps({"type": "result", "result": "ok", "session_id": "sess-new"})
    spawn = SpawnRecorder(lines=[result_line])
    harness = ClaudeHarness(command="claude", proc=spawn)

    result = harness.launch(_spec(), reporter)

    assert result.session_id == "sess-new"


def test_claude_session_id_none_when_no_result_event(reporter):
    spawn = SpawnRecorder(lines=["plain output"])
    harness = ClaudeHarness(command="claude", proc=spawn)

    result = harness.launch(_spec(), reporter)

    assert result.session_id is None


def test_claude_captures_session_id_from_init_event_without_result(reporter):
    # Simulates a 529 mid-turn: the init line arrives first (carrying the
    # session id), then the run dies non-zero before any result event. The id
    # must still be captured so the task can be resumed.
    init_line = json.dumps({"type": "system", "subtype": "init", "session_id": "sess-init"})
    spawn = SpawnRecorder(exit_code=1, lines=[init_line])
    harness = ClaudeHarness(command="claude", proc=spawn)

    result = harness.launch(_spec(), reporter)

    assert result.exit_code == 1
    assert result.session_id == "sess-init"


def test_claude_does_not_render_init_events_to_feed(reporter):
    # The init line carries the session id but no activity; Claude emits it
    # repeatedly, so it must be captured without spamming the live feed.
    init_line = json.dumps({"type": "system", "subtype": "init", "session_id": "sess-init"})
    spawn = SpawnRecorder(lines=[init_line, init_line])
    harness = ClaudeHarness(command="claude", proc=spawn)

    result = harness.launch(_spec(), reporter)

    assert result.session_id == "sess-init"
    assert [e.kind for e in reporter.events] == []  # nothing rendered


def test_claude_flags_retryable_on_overload(reporter):
    overload_line = (
        'API Error: 529 {"type":"error","error":{"type":"overloaded_error","message":"Overloaded"}}'
    )
    spawn = SpawnRecorder(exit_code=1, lines=[overload_line])
    harness = ClaudeHarness(command="claude", proc=spawn)

    result = harness.launch(_spec(), reporter)

    assert result.exit_code == 1
    assert result.retryable is True


def test_claude_not_retryable_on_ordinary_failure(reporter):
    spawn = SpawnRecorder(exit_code=2, lines=["Error: something the agent broke"])
    harness = ClaudeHarness(command="claude", proc=spawn)

    result = harness.launch(_spec(), reporter)

    assert result.exit_code == 2
    assert result.retryable is False


def test_claude_merges_every_server_and_keeps_the_surface_isolated(reporter):
    """Several servers land in one document, and `--strict-mcp-config` still
    goes with it — the agent's MCP surface is exactly this launch's, never the
    machine's globally configured one.

    Which server wins a shared name is not decided here: `run.execute` orders
    them and `LaunchSpec.mcp_document` merges them, so the rule lives with
    those and not in each harness."""
    spawn = SpawnRecorder()
    spec = LaunchSpec(
        prompt="p",
        folder="/w",
        mcp_servers=[_BOARD, {"chrome-devtools": {"command": "npx", "args": ["-y", "pkg"]}}],
    )

    ClaudeHarness(proc=spawn).launch(spec, reporter)

    assert spawn.mcp_json is not None and spawn.argv is not None
    servers = spawn.mcp_json["mcpServers"]
    assert servers["board"] == _BOARD["board"]
    assert servers["chrome-devtools"] == {"command": "npx", "args": ["-y", "pkg"]}
    assert "--strict-mcp-config" in spawn.argv


def test_claude_appends_plugin_dirs(reporter):
    spawn = SpawnRecorder()
    spec = LaunchSpec(
        prompt="p",
        folder="/w",
        plugin_dirs=["/repo/.claude/plugins/browser"],
    )
    ClaudeHarness(proc=spawn).launch(spec, reporter)
    assert spawn.argv is not None
    assert "/repo/.claude/plugins/browser" in spawn.argv
    assert spawn.argv[spawn.argv.index("/repo/.claude/plugins/browser") - 1] == "--plugin-dir"


def test_claude_passes_env_to_spawn(reporter):
    spawn = SpawnRecorder()
    spec = LaunchSpec(
        prompt="p",
        folder="/w",
        env={"NODE_ENV": "test"},
    )
    ClaudeHarness(proc=spawn).launch(spec, reporter)
    assert spawn.env == {"NODE_ENV": "test"}


def test_disallowed_tools_passed_when_set(reporter):
    spawn = SpawnRecorder()
    harness = ClaudeHarness(command="claude", proc=spawn)
    spec = LaunchSpec(
        prompt="p",
        folder="/tmp",
        disallowed_tools=["Write", "Edit", "NotebookEdit", "Bash"],
    )
    harness.launch(spec, reporter)
    argv = spawn.argv
    assert "--disallowedTools" in argv
    i = argv.index("--disallowedTools")
    assert argv[i + 1] == "Write,Edit,NotebookEdit,Bash"


def test_no_disallowed_flag_when_empty(reporter):
    spawn = SpawnRecorder()
    harness = ClaudeHarness(command="claude", proc=spawn)
    harness.launch(LaunchSpec(prompt="p", folder="/tmp"), reporter)
    assert "--disallowedTools" not in spawn.argv


def test_result_text_captured_from_result_event(reporter):
    line = '{"type":"result","result":"I investigated and found X.","session_id":"s1"}'
    harness = ClaudeHarness(command="claude", proc=SpawnRecorder(lines=[line]))
    res = harness.launch(LaunchSpec(prompt="p", folder="/tmp"), reporter)
    assert res.result_text == "I investigated and found X."


def test_summarize_builds_a_read_only_argv_and_returns_text():
    spawn = SpawnRecorder(lines=["Add widget", "Implements the widget per ISS-1."])
    harness = ClaudeHarness(command="claude", proc=spawn)
    out = harness.summarize(
        change="Read `git diff a...b`.",
        context="ISS-1: Add widget",
        model="claude-haiku-4-5",
        folder="/repo",
    )
    assert out == "Add widget\nImplements the widget per ISS-1."
    argv = spawn.argv
    assert "--mcp-config" not in argv
    assert "--model" in argv and argv[argv.index("--model") + 1] == "claude-haiku-4-5"
    assert spawn.cwd == "/repo"


def test_summarize_may_read_the_change_but_never_write():
    """The call has to look at the repository, so it gets tools — an allow-list
    of readers. Skipping permissions instead would hand a description-writing
    call write access to somebody's checkout; a denied tool only costs it the
    written description."""
    spawn = SpawnRecorder(lines=["Add widget", "body"])
    harness = ClaudeHarness(command="claude", proc=spawn)
    harness.summarize(change="Read `git diff a...b`.", context="ISS-1", model=None, folder="/repo")

    argv = spawn.argv
    assert "--dangerously-skip-permissions" not in argv
    allowed = argv[argv.index("--allowedTools") + 1].split(",")
    assert "Read" in allowed
    assert "Bash(git diff:*)" in allowed
    assert all("Write" not in tool and "Edit" not in tool for tool in allowed)


def test_summarize_is_told_where_the_change_is_rather_than_handed_it():
    """The whole point of the read-only tools: a change larger than one prompt
    is described from all of itself, so what travels is where to look."""
    spawn = SpawnRecorder(lines=["Add widget", "body"])
    harness = ClaudeHarness(command="claude", proc=spawn)
    harness.summarize(
        change="Read `gh pr diff 7 -R o/r`.", context="ISS-1", model=None, folder="/repo"
    )

    assert "gh pr diff 7 -R o/r" in (spawn.stdin or "")


def test_summarize_carries_the_runs_forge_credentials():
    """The agent reads the change with `gh`, which must authenticate as the same
    identity that pushed the branch."""
    spawn = SpawnRecorder(lines=["Add widget", "body"])
    harness = ClaudeHarness(command="claude", proc=spawn)
    harness.summarize(
        change="Read `gh pr diff 7 -R o/r`.",
        context="ISS-1",
        model=None,
        folder="/repo",
        env={"GH_TOKEN": "t"},
    )

    assert spawn.envs[-1] == {"GH_TOKEN": "t"}


def test_summarize_weaves_the_boards_guidance_into_the_prompt():
    """`guidance` is `Delivery.guidance` -- the board's own `writing-pull-requests`
    skill, already resolved -- and reaches this plugin-free call the only way it
    can: inlined into the prompt on stdin."""
    spawn = SpawnRecorder(lines=["Add widget", "body"])
    harness = ClaudeHarness(command="claude", proc=spawn)
    harness.summarize(
        change="Read `git diff a...b`.",
        context="ISS-1",
        model=None,
        folder="/repo",
        guidance="Title in the imperative.",
    )

    assert "Title in the imperative." in (spawn.stdin or "")


# ---------------------------------------------------------------------------
# A board MCP server that drops out mid-run
# ---------------------------------------------------------------------------

# What Claude Code itself says when a tool call reaches an MCP server it is no
# longer attached to. The client says this, not the board: the call never left
# the machine.
_MCP_LOST = 'MCP server "issuebear" is not connected'


class _RealChild(RecordingProcess):
    """Runs one real program, whatever argv it is handed.

    The harness builds `claude` command lines, so a real `claude` cannot stand
    in for the child here — but the cancel ladder is the kernel's, and a double
    would only prove the double works. This runs a python child instead, and
    ignores the working directory the launch asks for because that folder
    belongs to a real run and does not exist in a test.
    """

    def __init__(self, script: str) -> None:
        super().__init__()
        self._argv = [sys.executable, "-c", script]

    def spawn(self, argv, *, on_line, cwd=None, env=None, cancel=None, stdin=None) -> int:
        """Run the fixed script, streaming and cancelling exactly as the real
        adapter does."""
        self.calls.append(list(argv))
        return RealProcess().spawn(self._argv, on_line=on_line, cancel=cancel)


def test_claude_ends_the_run_when_the_board_mcp_drops_out(reporter):
    """Claude Code never re-attaches a dropped MCP server inside one process, so
    every later board tool call fails against a connection that cannot recover.
    The run ends instead, non-zero and retryable, and the ladder in
    `run._launch_with_retries` resumes it in a fresh process that can attach."""
    script = f"import time\nprint({_MCP_LOST!r}, flush=True)\ntime.sleep(60)\n"
    harness = ClaudeHarness(command="claude", proc=_RealChild(script))
    cancel = threading.Event()

    started = time.monotonic()
    result = harness.launch(_spec(), reporter, cancel)
    elapsed = time.monotonic() - started

    assert elapsed < 30, "the child outlived the dropped MCP server"
    assert result.exit_code != 0  # the ladder only retries a non-zero exit
    assert result.retryable is True
    assert not cancel.is_set(), "ending the run to reconnect is not the caller's abort"


def test_claude_still_honours_the_callers_cancel_on_a_silent_child(reporter):
    """The run has its own reason to end a launch now, and the caller's abort
    must still reach a child that has stopped printing — the whole point of a
    Ctrl-C is a child that says nothing more."""
    harness = ClaudeHarness(command="claude", proc=_RealChild("import time; time.sleep(60)"))
    cancel = threading.Event()
    threading.Timer(0.2, cancel.set).start()

    started = time.monotonic()
    result = harness.launch(_spec(), reporter, cancel)
    elapsed = time.monotonic() - started

    assert elapsed < 30, "the child outlived its cancellation"
    assert result.exit_code != 0


def test_claude_reads_the_disconnect_off_the_real_stream_json_line(reporter):
    """The error arrives inside a stream-json tool result, where the server name
    is escaped (`MCP server \\"issuebear\\" is not connected`). The match has to
    survive that escaping, which is why it is not one contiguous substring."""
    tool_result = json.dumps(
        {
            "type": "user",
            "message": {
                "content": [{"type": "tool_result", "is_error": True, "content": _MCP_LOST}]
            },
        }
    )
    spawn = SpawnRecorder(exit_code=1, lines=[tool_result, "later"])
    harness = ClaudeHarness(command="claude", proc=spawn)

    result = harness.launch(_spec(), reporter)

    assert result.retryable is True
    assert "later" not in reporter.raw_lines  # the run stopped at the disconnect


def test_claude_is_not_fooled_by_ordinary_talk_about_connections(reporter):
    """ "is not connected" on its own is ordinary agent output. Only a line that
    names an MCP server ends the run."""
    spawn = SpawnRecorder(exit_code=0, lines=["the database is not connected yet"])
    harness = ClaudeHarness(command="claude", proc=spawn)

    result = harness.launch(_spec(), reporter)

    assert result.retryable is False
