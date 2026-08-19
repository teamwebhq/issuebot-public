"""Tests for ensure_claude_mcp: idempotent registration of the board MCP in the
user's own Claude Code, with graceful skips."""

from __future__ import annotations

from conftest import config
from issuebot.config import Config, source_plugin
from issuebot.plugins.harnesses.claude.mcp_setup import ensure_claude_mcp


def _cfg(harness: str = "claude", command_path: str | None = None) -> Config:
    """A config whose source table is whichever source is installed.

    What this harness registers is the source's own answer (`Source.user_mcp`),
    so a test that spelled one source's table would be asserting on the wrong
    plugin's settings."""
    command = {harness: {"command": command_path}} if command_path else {}
    return config(harness=harness, **command)


class _Result:
    """Minimal subprocess.CompletedProcess stand-in (only returncode is read)."""

    def __init__(self, returncode: int) -> None:
        self.returncode = returncode


class _Recorder:
    """Stand-in for subprocess.run that records argv. `mcp get` probes return
    ``get_code``; every other command returns 0 (success)."""

    def __init__(self, get_code: int = 0) -> None:
        self.calls: list[list[str]] = []
        self._get_code = get_code

    def __call__(self, argv, **kwargs):  # noqa: ANN001 - test double
        self.calls.append(list(argv))
        return _Result(self._get_code if "get" in argv else 0)


def test_another_harness_gets_no_claude_wiring() -> None:
    """This registers the board MCP in the *user's own* Claude Code, which is
    meaningless for an install driving some other agent — so it does nothing at
    all rather than shelling out to a `claude` that may not be there."""
    run = _Recorder()
    msgs: list[str] = []

    ensure_claude_mcp(_cfg(harness="some-other-harness"), run=run, echo=msgs.append)

    assert run.calls == []


def test_claude_missing_on_path_prints_manual_command(monkeypatch) -> None:
    import issuebot.plugins.harnesses.claude.mcp_setup as m

    monkeypatch.setattr(m.shutil, "which", lambda _name: None)
    run = _Recorder()
    msgs: list[str] = []
    ensure_claude_mcp(_cfg(), run=run, echo=msgs.append)

    assert run.calls == []  # never shelled out
    joined = "\n".join(msgs)
    assert "claude mcp add" in joined
    assert source_plugin().source.user_mcp(_cfg()).url in joined


def test_already_registered_skips_add(monkeypatch) -> None:
    import issuebot.plugins.harnesses.claude.mcp_setup as m

    monkeypatch.setattr(m.shutil, "which", lambda _name: "/usr/bin/claude")
    run = _Recorder(get_code=0)  # `mcp get` exits 0 → already present
    msgs: list[str] = []
    ensure_claude_mcp(_cfg(), run=run, echo=msgs.append)

    # Only the `get` probe ran; no `add`.
    assert any("get" in c for c in run.calls)
    assert not any("add" in c for c in run.calls)


def test_absent_triggers_add_with_expected_argv(monkeypatch) -> None:
    import issuebot.plugins.harnesses.claude.mcp_setup as m

    monkeypatch.setattr(m.shutil, "which", lambda _name: "/usr/bin/claude")
    run = _Recorder(get_code=1)  # `mcp get` non-zero → not present
    msgs: list[str] = []
    ensure_claude_mcp(_cfg(command_path="/opt/claude"), run=run, echo=msgs.append)

    # Every value in the command is the source's own answer, read back the same
    # way `mcp_setup` reads it — so this asserts the *rendering*, which is this
    # harness's job, and not one source's endpoint, which is not.
    cfg = _cfg(command_path="/opt/claude")
    server = source_plugin().source.user_mcp(cfg)

    add = next(c for c in run.calls if "add" in c)
    assert add == [
        "/opt/claude",
        "mcp",
        "add",
        "--scope",
        "user",
        "--transport",
        "http",
        server.name,
        server.url,
        *[arg for k, v in server.headers.items() for arg in ("--header", f"{k}: {v}")],
    ]


def test_oserror_on_run_is_swallowed(monkeypatch) -> None:
    import issuebot.plugins.harnesses.claude.mcp_setup as m

    monkeypatch.setattr(m.shutil, "which", lambda _name: "/usr/bin/claude")

    def bad_run(*args, **kwargs):  # noqa: ANN002, ANN003 - test double
        raise OSError("Permission denied")

    msgs: list[str] = []
    # Must not propagate the OSError, and should echo a manual-command fallback.
    ensure_claude_mcp(_cfg(), run=bad_run, echo=msgs.append)
    assert any("claude mcp add" in msg for msg in msgs)
