"""Codex-specific harness behaviour beyond the shared conformance suite."""

from __future__ import annotations

import pytest

from conftest import SpawnRecorder
from issuebot.plugins.harnesses.base import LaunchSpec
from issuebot.plugins.harnesses.codex.harness import CodexHarness

# One server fragment, in the shape a source hands one over. Codex is told
# nothing about where it came from, which is the point of asserting on it.
_BOARD = {"board": {"type": "http", "url": "https://board.example/mcp"}}


def _spec() -> LaunchSpec:
    return LaunchSpec(prompt="do the thing", folder="/work/alpha", mcp_servers=[_BOARD])


def test_codex_builds_argv_and_passes_cwd(reporter):
    spawn = SpawnRecorder(exit_code=2)
    harness = CodexHarness(command="codex", proc=spawn)

    result = harness.launch(_spec(), reporter)

    assert spawn.argv is not None
    assert spawn.argv[0] == "codex"
    assert "exec" in spawn.argv
    assert "do the thing" in spawn.argv
    assert spawn.cwd == "/work/alpha"
    assert result.exit_code == 2


def test_codex_passes_env_to_spawn(reporter):
    """A real gap the shared conformance suite can't catch (it never spawns
    anything for the fake harness): codex used to drop `spec.env` entirely,
    so neither a repo's bootstrap variables nor `RESPONSE_ENV` ever reached
    the child process."""
    spawn = SpawnRecorder()
    harness = CodexHarness(command="codex", proc=spawn)

    spec = LaunchSpec(
        prompt="do the thing",
        folder="/work/alpha",
        env={"ISSUEBOT_RESPONSE": "/tmp/response.json"},
    )
    harness.launch(spec, reporter)

    assert spawn.env == {"ISSUEBOT_RESPONSE": "/tmp/response.json"}


def test_codex_streams_lines_to_reporter(reporter):
    spawn = SpawnRecorder(lines=["line-one", "line-two"])
    harness = CodexHarness(command="codex", proc=spawn)

    harness.launch(_spec(), reporter)

    assert reporter.raw_lines == ["line-one", "line-two"]
    # Codex output is not stream-json: every line surfaces as a raw event.
    assert [ev.kind for ev in reporter.events] == ["raw", "raw"]
    assert reporter.events[0].summary == "line-one"


def test_codex_writes_the_launchs_own_mcp_servers_to_its_config_file(reporter):
    """Whatever the launch was handed, written out verbatim: codex has no
    server name, transport or credential of its own to contribute."""
    spawn = SpawnRecorder()
    harness = CodexHarness(command="codex", proc=spawn)

    harness.launch(_spec(), reporter)

    assert spawn.mcp_json == {"mcpServers": _BOARD}


def test_codex_summarize_not_supported():
    """Codex has no tools-free one-shot mode wired up yet; callers fall back to
    the mechanical PR description."""
    with pytest.raises(NotImplementedError):
        CodexHarness().summarize("diff", context="ctx", model=None, folder="/tmp")
