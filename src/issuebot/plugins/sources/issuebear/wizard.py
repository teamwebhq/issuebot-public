"""Everything this source asks a user, at both the moments it gets asked.

:func:`setup` is ``issuebot init``'s half — where the board server is and the
PAT to reach it with — mounted as the plugin's ``setup`` hook.

:func:`connection` is ``issuebot connect``'s half: the walk down this board
server's own hierarchy, organisation → project → board, mounted as the plugin's
``wizard`` hook. :func:`settings` is the other per-connection half — the
settings this source owns (``mode``/``confirm``/``done``), mounted as the
plugin's ``settings_wizard`` hook so their vocabulary never has to be core's.
The walk lives here and not in core because none of it generalises: GitHub
Issues is organisation → repository and has no board at all, Linear is team →
project or team → cycle. A walk in core would be a hierarchy every source had
to invent levels to fill.

So core asks a source to identify the connection and takes back settings plus a
name to suggest; how many questions that took, and what they were called, stays
here. The numbered picker itself is handed in — the same way an environment
plugin is handed ``choose_literal`` — so this source's questions look like every
other plugin's and this module stays a leaf.

Numbered prompts and ``typer.prompt`` for the same reason as the core wizard:
they read plain lines from stdin, so the whole flow stays exercisable under
``CliRunner``.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any, get_args

import typer

from issuebot.plugins.sources.issuebear.client import project_repo
from issuebot.plugins.sources.issuebear.discovery import DiscoveryError, discover
from issuebot.plugins.sources.issuebear.settings import ConfirmChoice, DoneMode, Mode

# Picks one of a list by number, rendering each with `to_label`. Injected by the
# core wizard (`issuebot.wizard.choose`) rather than imported, so every plugin's
# questions are asked and defaulted identically.
Chooser = Callable[..., Any]

# One line per answer, shown beside it in the menu — this source's values are a
# vocabulary the user has never met, so the bare word is not a question they
# can answer.
_HELP: dict[str, dict[str, str]] = {
    "Mode": {
        "build": "the agent may change code and report the changes",
        "respond": "the agent answers and comments only, never reporting changes",
    },
    # The agent always plans, and always asks about genuine ambiguity — neither
    # is worth offering as a choice. Whether it waits for you to sign the plan
    # off before writing code is the one thing that is.
    "Confirm the plan": {
        "yes": "post the plan and wait for your approval before writing any code",
        "no": "post the plan and get straight on with it",
    },
    "Done mode": {
        "review": "post a summary and hand the task back for review",
        "complete": "post a summary and mark the task complete",
    },
}


def setup() -> dict[str, Any]:
    """Ask for the board server and PAT; return the ``[issuebear]`` table.

    One base URL is asked for and :mod:`.discovery` turns it into the API and
    MCP URLs; only when that fails are both asked for directly, so the happy
    path is a single question.

    Returns the settings rather than a Config: the caller assembles the file
    (harness, this table, whatever else it gathered) and proves the PAT against
    the server just described before anything is written.
    """
    base = typer.prompt("Issuebear URL")

    try:
        doc = discover(base)
        api_url = doc["api_url"]
        mcp_url = doc["mcp_url"]
    except DiscoveryError as exc:
        typer.echo(f"Could not auto-discover the URLs ({exc}).", err=True)
        typer.echo("Enter the API and MCP URLs directly instead.")
        api_url = typer.prompt("API URL")
        mcp_url = typer.prompt("MCP URL")

    pat = typer.prompt("Agent PAT", hide_input=True)

    return {"api_url": api_url, "mcp_url": mcp_url, "pat": pat}


def _label(entity: dict[str, Any]) -> str:
    """Human label for an org/project/board record: its name, else its id."""
    return str(entity.get("name") or entity.get("id") or "?")


def suggest_name(board_name: str, project_name: str = "") -> str:
    """Auto-suggest a connection name from the picked board (or project) name.

    Lowercases and turns any run of non-alphanumerics into a single hyphen, so
    "Frontend Web!" becomes "frontend-web". Falls back to the project name, then
    to a safe constant, so the suggestion is never empty.
    """
    base = board_name or project_name or "connection"
    slug = re.sub(r"[^a-z0-9]+", "-", base.lower()).strip("-")
    return slug or "connection"


def connection(client: Any, *, choose: Chooser) -> dict[str, Any]:
    """Walk organisation → project → board and return what identifies the
    connection: the ``board`` it reads, a ``name`` to suggest for it, and the
    ``repo`` the project is linked to (``None`` to let core ask instead).

    Three levels because that is this board server's hierarchy, not because
    three is how many levels a source has. A single option at any level is
    auto-selected and announced rather than asked — `choose`'s own rule, which
    is why it is handed in rather than reimplemented here.
    """
    org = choose("Organisation", client.list_organisations(), to_label=_label)
    project = choose("Project", client.list_projects(org["id"]), to_label=_label)
    board = choose("Board", client.list_boards(project["id"]), to_label=_label)

    return {
        "board": str(board.get("id")),
        "name": suggest_name(_label(board), _label(project)),
        # The repository the project is attached to, or None to let core ask.
        # The project's answer is final: a connection pointed at a different
        # repo would raise PRs that never appear on the tasks it works.
        "repo": project_repo(project),
    }


def settings(*, choose_literal: Any, sandboxed: bool) -> tuple[dict[str, Any], bool]:
    """The per-connection settings this source owns: mode, plan confirmation
    and done-mode — the plugin's ``settings_wizard`` hook.

    Returns the settings plus whether runs under them may report ``changes``
    (core's own output-kind vocabulary). The wizard hands that on to the
    workspace hook, which then skips the branch questions a read-only
    connection has no use for — without core ever reading ``mode`` by name.
    The judgement mirrors :meth:`Issuebear.permits`, where ``respond`` bars
    ``changes`` whatever kind of work arrived.

    ``sandboxed`` says the chosen environment boots a fresh machine per task.
    Mode is then forced to "build" rather than asked: a machine booted for one
    task exists to build in, and respond work needs no machine of its own.

    ``choose_literal`` is the generic wizard's own numbered picker, handed in
    rather than imported, so this source's questions look identical to every
    other plugin's and this module stays a leaf.
    """
    mode = (
        "build"
        if sandboxed
        else choose_literal("Mode", get_args(Mode), "build", help_for=_HELP["Mode"])
    )

    # A two-value menu rather than a y/n prompt so it reads like every other
    # question in the wizard, and so both answers get their line of help —
    # "no" does not mean "no plan", and a bare y/n would let you think it did.
    confirm = (
        choose_literal(
            "Confirm the plan", get_args(ConfirmChoice), "yes", help_for=_HELP["Confirm the plan"]
        )
        == "yes"
    )

    done = choose_literal("Done mode", get_args(DoneMode), "review", help_for=_HELP["Done mode"])

    return {"mode": mode, "confirm": confirm, "done": done}, mode == "build"
