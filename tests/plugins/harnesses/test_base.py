"""Tests for the shapes every harness shares: LaunchSpec, LaunchResult."""

from __future__ import annotations

from issuebot.plugins.harnesses.base import LaunchResult, LaunchSpec


def test_launchspec_bootstrap_fields_default_empty():
    spec = LaunchSpec(prompt="p", folder="/w")
    assert spec.env == {}
    assert spec.mcp_servers == []
    assert spec.plugin_dirs == []


def test_launchspec_carries_bootstrap_fields():
    spec = LaunchSpec(
        prompt="p",
        folder="/w",
        env={"A": "1"},
        mcp_servers=[{"cd": {"command": "npx", "args": []}}],
        plugin_dirs=["/p"],
    )
    assert spec.env == {"A": "1"}
    assert spec.mcp_servers[0]["cd"]["command"] == "npx"
    assert spec.plugin_dirs == ["/p"]


def test_the_mcp_document_merges_every_server_the_launch_was_handed():
    spec = LaunchSpec(
        prompt="p",
        folder="/w",
        mcp_servers=[{"cd": {"command": "npx"}}, {"board": {"type": "http", "url": "https://b"}}],
    )
    assert spec.mcp_document() == {
        "mcpServers": {"cd": {"command": "npx"}, "board": {"type": "http", "url": "https://b"}}
    }


def test_a_later_server_wins_the_name():
    """The precedence `run.execute` relies on: it appends the source's servers
    after the repo's, so a repo declaring the board's own name cannot cut the
    agent off from its board."""
    spec = LaunchSpec(
        prompt="p",
        folder="/w",
        mcp_servers=[{"board": {"url": "https://repo-says"}}, {"board": {"url": "https://real"}}],
    )
    assert spec.mcp_document()["mcpServers"]["board"] == {"url": "https://real"}


def test_launchspec_disallowed_tools_defaults_empty():
    spec = LaunchSpec(prompt="p", folder="/tmp")
    assert spec.disallowed_tools == []


def test_launchresult_result_text_defaults_empty():
    assert LaunchResult(exit_code=0).result_text == ""


def test_launch_spec_and_result_default_session_fields_to_none() -> None:
    spec = LaunchSpec(prompt="p", folder="/w")
    assert spec.resume_session_id is None
    result = LaunchResult(exit_code=0)
    assert result.session_id is None
