"""Claude-specific harness behaviour: argv shape, session resume, retry
detection — everything the shared conformance suite doesn't cover because it
is Claude-only."""

from __future__ import annotations

import json
from pathlib import Path

from conftest import SpawnRecorder
from issuebot.plugins.harnesses.base import LaunchSpec
from issuebot.plugins.harnesses.claude.harness import ClaudeHarness


class PluginDirRecorder(SpawnRecorder):
    """A SpawnRecorder that also reads the `--plugin-dir` this harness passes.

    Here rather than in `conftest`: the flag and the manifest layout inside the
    directory are this agent CLI's own, and no other harness's tests can use
    them. Read during spawn because the directory is a subdir of the launch's
    own temp dir and is gone by the time `launch()` returns."""

    def __init__(self, exit_code: int = 0, lines: list[str] | None = None):
        super().__init__(exit_code=exit_code, lines=lines)
        self.plugin_manifest: dict | None = None
        self.plugin_has_board_skill = False

    def spawn(self, argv, *, on_line, cwd=None, env=None, cancel=None) -> int:
        if "--plugin-dir" in argv:
            plugin_dir = Path(argv[argv.index("--plugin-dir") + 1])
            manifest = plugin_dir / ".claude-plugin" / "plugin.json"
            if manifest.is_file():
                self.plugin_manifest = json.loads(manifest.read_text())
            self.plugin_has_board_skill = (
                plugin_dir / "skills" / "board-implementing" / "SKILL.md"
            ).is_file()
        return super().spawn(argv, on_line=on_line, cwd=cwd, env=env, cancel=cancel)


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


def test_claude_loads_bundled_plugin_dir(reporter):
    spawn = PluginDirRecorder()
    harness = ClaudeHarness(command="claude", proc=spawn)

    harness.launch(_spec(), reporter)

    assert spawn.argv is not None
    assert "--plugin-dir" in spawn.argv
    # PluginDirRecorder reads the manifest while the directory still exists
    # (during spawn); see below for why it can't be read after launch() returns.
    assert spawn.plugin_manifest is not None
    assert spawn.plugin_manifest["name"] == "issuebot-board"
    assert spawn.plugin_has_board_skill

    # Regression: the plugin dir is a subdir of the launch's own temp dir, so
    # it must be gone once launch() returns -- not leaked per launch/retry.
    plugin = spawn.argv[spawn.argv.index("--plugin-dir") + 1]
    assert not Path(plugin).exists()


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


def test_summarize_builds_toolless_argv_and_returns_text():
    spawn = SpawnRecorder(lines=["Add widget", "Implements the widget per ISS-1."])
    harness = ClaudeHarness(command="claude", proc=spawn)
    out = harness.summarize(
        "DIFF", context="ISS-1: Add widget", model="claude-haiku-4-5", folder="/repo"
    )
    assert out == "Add widget\nImplements the widget per ISS-1."
    argv = spawn.argv
    assert "--mcp-config" not in argv
    assert "--model" in argv and argv[argv.index("--model") + 1] == "claude-haiku-4-5"
    assert spawn.cwd == "/repo"


def test_summarize_prompt_carries_the_pr_writing_guidance():
    """The summarizer runs tools-free, so it never loads the skill itself --
    the guidance has to travel in the prompt or it does not reach the model."""
    spawn = SpawnRecorder(lines=["Add widget", "body"])
    harness = ClaudeHarness(command="claude", proc=spawn)
    harness.summarize("DIFF", context="ISS-1", model=None, folder="/repo")

    prompt = spawn.argv[spawn.argv.index("-p") + 1]
    assert "DIFF" in prompt
    assert "reviewer" in prompt.lower()
