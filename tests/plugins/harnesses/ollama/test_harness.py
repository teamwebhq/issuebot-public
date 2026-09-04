"""Ollama-specific harness behaviour: the `ollama launch` wrapper around the
Claude Code command line, and the model that belongs to the wrapper rather than
to the agent.

Everything else this harness does is Claude Code's, inherited rather than
rewritten, so only a few of those behaviours are checked here — enough to show
the wrapper did not break the stream it wraps."""

from __future__ import annotations

import json

from conftest import SpawnRecorder
from issuebot.plugins.harnesses.base import LaunchSpec
from issuebot.plugins.harnesses.ollama.harness import OllamaClaudeHarness

# One server fragment, in the shape a source hands one over.
_BOARD = {"board": {"type": "http", "url": "https://board.example/mcp"}}

# What Claude Code says when a tool call reaches an MCP server it is no longer
# attached to. Repeated from the Claude tests on purpose: this asserts the
# wrapper still ends the run, not that the two files agree.
_MCP_LOST = 'MCP server "issuebear" is not connected'


def _spec(**kw) -> LaunchSpec:
    return LaunchSpec(prompt="do the thing", folder="/work/alpha", mcp_servers=[_BOARD], **kw)


def _halves(argv: list[str]) -> tuple[list[str], list[str]]:
    """The argv either side of the `--` separator: what `ollama launch` reads,
    and what it hands on to Claude Code."""
    cut = argv.index("--")
    return argv[:cut], argv[cut + 1 :]


def test_ollama_launches_claude_through_ollama(reporter):
    """`ollama launch claude` is the whole point: Claude Code runs against an
    Ollama model, so Ollama is the program and Claude Code is its argument."""
    spawn = SpawnRecorder()

    OllamaClaudeHarness(proc=spawn).launch(_spec(), reporter)

    assert spawn.argv is not None
    assert spawn.argv[:3] == ["ollama", "launch", "claude"]
    assert spawn.cwd == "/work/alpha"


def test_ollama_answers_the_model_chooser_before_it_is_asked(reporter):
    """`ollama launch` offers an interactive model menu, which an unattended run
    can never answer."""
    spawn = SpawnRecorder()

    OllamaClaudeHarness(proc=spawn).launch(_spec(), reporter)

    launch, _ = _halves(spawn.argv)
    assert "--yes" in launch


def test_ollama_gives_the_boards_model_to_ollama_not_to_claude(reporter):
    """The board names an *Ollama* model, so it is an argument of the wrapper.
    Passed on to Claude Code as well it would name a model Anthropic never had."""
    spawn = SpawnRecorder()

    OllamaClaudeHarness(proc=spawn).launch(_spec(model="glm-4.7-flash"), reporter)

    launch, claude = _halves(spawn.argv)
    assert launch[launch.index("--model") + 1] == "glm-4.7-flash"
    assert "--model" not in claude


def test_ollama_omits_the_model_when_the_board_names_none(reporter):
    """With no model to pass, `ollama launch` chooses one itself."""
    spawn = SpawnRecorder()

    OllamaClaudeHarness(proc=spawn).launch(_spec(), reporter)

    assert "--model" not in spawn.argv
    assert "--yes" in spawn.argv


def test_ollama_hands_the_whole_claude_command_line_across_the_separator(reporter):
    """The wrapper adds a prefix and takes nothing away: every flag the Claude
    harness builds still reaches Claude Code."""
    spawn = SpawnRecorder()
    spec = _spec(resume_session_id="sess-prior", plugin_dirs=["/repo/.claude/plugins/board"])

    OllamaClaudeHarness(proc=spawn).launch(spec, reporter)

    _, claude = _halves(spawn.argv)
    assert claude[:2] == ["-p", "do the thing"]
    assert "--mcp-config" in claude
    assert "--strict-mcp-config" in claude
    assert "--dangerously-skip-permissions" in claude
    assert claude[claude.index("--output-format") + 1] == "stream-json"
    assert claude[claude.index("--resume") + 1] == "sess-prior"
    assert claude[claude.index("--plugin-dir") + 1] == "/repo/.claude/plugins/board"
    # The launch's own servers still reach the agent, written where Claude reads them.
    assert spawn.mcp_json == {"mcpServers": _BOARD}


def test_ollama_custom_command(reporter):
    """`[ollama] command` names the Ollama executable, not the Claude one."""
    spawn = SpawnRecorder()

    OllamaClaudeHarness(command="/opt/bin/ollama", proc=spawn).launch(_spec(), reporter)

    assert spawn.argv[:3] == ["/opt/bin/ollama", "launch", "claude"]


def test_ollama_summarize_runs_through_launch_and_stays_read_only(reporter):
    """The PR description is written by the same local model, so it goes through
    the same wrapper — and keeps the read-only, MCP-free surface it has for
    Claude Code."""
    spawn = SpawnRecorder(lines=["Add widget", "Implements the widget."])
    harness = OllamaClaudeHarness(proc=spawn)

    out = harness.summarize(
        change="Read `git diff a...b`.",
        context="ISS-1: Add widget",
        model="glm-4.7-flash",
        folder="/repo",
    )

    assert out == "Add widget\nImplements the widget."
    launch, claude = _halves(spawn.argv)
    assert launch[:3] == ["ollama", "launch", "claude"]
    assert launch[launch.index("--model") + 1] == "glm-4.7-flash"
    assert "--model" not in claude
    assert "--mcp-config" not in claude
    assert "--strict-mcp-config" in claude
    assert "--dangerously-skip-permissions" not in claude
    assert "Read" in claude[claude.index("--allowedTools") + 1].split(",")


def test_ollama_still_reads_the_stream_it_wraps(reporter):
    """Claude Code's stream-json arrives unchanged through the wrapper, so the
    session id is still there to resume from."""
    result_line = json.dumps({"type": "result", "result": "done", "session_id": "sess-new"})
    spawn = SpawnRecorder(lines=[result_line])

    result = OllamaClaudeHarness(proc=spawn).launch(_spec(), reporter)

    assert result.session_id == "sess-new"
    assert result.result_text == "done"


def test_ollama_ends_the_run_when_the_board_mcp_drops_out(reporter):
    """A dropped MCP server is Claude Code's condition, and it is no less final
    for running under Ollama."""
    spawn = SpawnRecorder(exit_code=1, lines=[_MCP_LOST, "later"])

    result = OllamaClaudeHarness(proc=spawn).launch(_spec(), reporter)

    assert result.retryable is True
    assert "later" not in reporter.raw_lines  # the run stopped at the disconnect
