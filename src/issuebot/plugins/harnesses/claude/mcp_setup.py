"""Register the source's MCP server in the user's own Claude Code.

Autonomous launches inject the MCP per-run with ``--strict-mcp-config`` (isolated
from global config). This is the separate, one-time step that wires the work
source into the human's interactive Claude Code so they can chat with it
directly. Idempotent and best-effort: it never fails ``init``/``doctor`` over a
missing ``claude`` or a failed add.

*What* to register is asked of the installed source (``Source.user_mcp``), which
answers with an :class:`~issuebot.contracts.McpServer` — name, transport, URL and
headers. This module only knows how to say it in Claude Code's own words. A
source with no such channel answers ``None`` and the step is skipped, which is
also what happens when an install has no source at all.
"""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Callable

from issuebot import plugins
from issuebot.config import Config, harness_name, harness_settings, source_plugin
from issuebot.contracts import McpServer
from issuebot.plugins.base import HarnessPlugin
from issuebot.plugins.harnesses.claude.harness import ClaudeHarness

# The harness whose executable does the registering. Always Claude Code's own,
# whichever harness the install runs: a wrapper starts Claude Code, it does not
# replace it, and `ollama mcp add` is not a command.
_CLAUDE = "claude"

# Process-callable shape compatible with subprocess.run (we only read returncode).
Run = Callable[..., "subprocess.CompletedProcess[str]"]
Echo = Callable[[str], None]


def _server(cfg: Config) -> McpServer | None:
    """The MCP server the installed source offers the user, or None.

    An install with no source installed is not an error here — there is simply
    nothing to register — so the registry's `UnknownPlugin` becomes a None."""
    try:
        return source_plugin().source.user_mcp(cfg)
    except plugins.UnknownPlugin:
        return None


def _drives_claude_code(cfg: Config) -> bool:
    """True when the install's harness runs Claude Code, wrapper or not.

    Asked of the registered harness *class*, not of the harness's name: the
    ollama harness runs Claude Code through `ollama launch`, so the user's own
    Claude Code wants the board registered exactly as much as it does for the
    claude harness — and a later wrapper gets the same answer with no edit here.

    A config this build cannot name a harness for answers False rather than
    raising. This step is best-effort and must never fail `init`/`doctor`, and
    an install driving something else has nothing to register anyway.
    """
    try:
        plugin = plugins.get("harnesses", harness_name(cfg))
    except plugins.UnknownPlugin:
        return False

    return isinstance(plugin, HarnessPlugin) and issubclass(plugin.harness, ClaudeHarness)


def _add_argv(claude: str, server: McpServer) -> list[str]:
    """The ``claude mcp add`` command that registers one MCP server globally.

    Only the http transport is spelled out: a `Source.user_mcp` is a remote
    endpoint by nature (it is how the *agent* reaches the source), so a stdio
    answer has no meaning to register in the user's own tooling."""
    headers = [
        arg for name, value in server.headers.items() for arg in ("--header", f"{name}: {value}")
    ]
    return [
        claude,
        "mcp",
        "add",
        "--scope",
        "user",
        "--transport",
        "http",
        server.name,
        server.url or "",
        *headers,
    ]


def ensure_claude_mcp(
    cfg: Config,
    *,
    run: Run = subprocess.run,
    echo: Echo = print,
) -> None:
    """Ensure the source's MCP server is registered in the user's Claude Code.

    No-op for an install whose harness does not run Claude Code, and for a
    source with nothing to register. If ``claude`` is not on PATH, print the
    manual command and return. If the MCP is already registered, do nothing.
    Otherwise add it globally (``--scope user``)."""
    if not _drives_claude_code(cfg):
        return

    server = _server(cfg)
    if server is None:
        return

    # The `[claude]` table names Claude Code's executable whatever the install
    # runs, so it is read by name rather than from the configured harness —
    # whose `command` is the wrapper's own program on an ollama install.
    claude = harness_settings(cfg, _CLAUDE).get("command") or _CLAUDE
    if shutil.which(claude) is None:
        manual = " ".join(_add_argv(claude, server))
        echo(
            "Claude Code not found on PATH; skipping board MCP setup. "
            f"To wire it up yourself, run:\n  {manual}"
        )
        return

    # Probe whether it's already registered; `mcp get <name>` exits non-zero when
    # absent. Suppress its output — we only care about the exit code. Any OSError
    # (e.g. the resolved executable can't actually be run) is swallowed: this step
    # is best-effort and must never fail init/doctor.
    try:
        probe = run(
            [claude, "mcp", "get", server.name],
            capture_output=True,
            text=True,
        )
        if probe.returncode == 0:
            echo("Board MCP already configured in Claude Code.")
            return

        result = run(_add_argv(claude, server), capture_output=True, text=True)
    except OSError as exc:
        echo(
            "Could not run Claude Code to configure the board MCP "
            f"({exc}). Add it manually with:\n  " + " ".join(_add_argv(claude, server))
        )
        return

    if result.returncode == 0:
        echo("Wired the board into Claude Code (claude mcp add, scope user).")
    else:
        echo(
            "Could not auto-add the board to Claude Code "
            f"(exit {result.returncode}). Add it manually with:\n  "
            + " ".join(_add_argv(claude, server))
        )
