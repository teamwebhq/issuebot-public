"""What this source asks `issuebot init`, now that core no longer asks it.

The URL/PAT questions used to live in `issuebot.wizard.setup`, and were covered
only incidentally by two CLI tests. They are this plugin's own surface now, so
the fallback that makes them worth having — one URL when discovery works, two
when it doesn't — is tested here, where it survives core changing its mind about
what `init` looks like.
"""

from __future__ import annotations

import typer
from typer.testing import CliRunner

from issuebot.plugins.sources.issuebear import wizard
from issuebot.plugins.sources.issuebear.discovery import DiscoveryError

runner = CliRunner()


def _ask(monkeypatch, discovered, keys: str) -> dict:
    """Run the hook under a scripted stdin, returning the table it gathered.

    Wrapped in a throwaway Typer command because `typer.prompt` reads a real
    stream: `CliRunner` gives it one, exactly as the CLI tests drive the rest of
    the wizard."""
    monkeypatch.setattr(wizard, "discover", discovered)

    gathered: dict = {}
    app = typer.Typer()
    app.command()(lambda: gathered.update(wizard.setup()))

    result = runner.invoke(app, [], input=keys)
    assert result.exit_code == 0, result.output
    return gathered


def test_one_url_is_enough_when_the_server_answers(monkeypatch) -> None:
    """The happy path is a single question: the base URL discovers both
    endpoints, and only the PAT is left to ask for."""
    asked: list[str] = []

    def discovered(base: str) -> dict[str, str]:
        asked.append(base)
        return {"api_url": "https://x/api", "mcp_url": "https://x/mcp"}

    table = _ask(monkeypatch, discovered, "https://x\npat-abc\n")

    assert asked == ["https://x"]
    assert table == {"api_url": "https://x/api", "mcp_url": "https://x/mcp", "pat": "pat-abc"}


def test_both_urls_are_asked_for_when_discovery_fails(monkeypatch) -> None:
    """A server too old to serve the well-known document must not be a dead
    end — the two URLs it would have carried are asked for directly."""

    def discovered(base: str) -> dict[str, str]:
        raise DiscoveryError("404")

    table = _ask(
        monkeypatch, discovered, "https://x\nhttps://direct/api\nhttps://direct/mcp\npat-abc\n"
    )

    assert table == {
        "api_url": "https://direct/api",
        "mcp_url": "https://direct/mcp",
        "pat": "pat-abc",
    }


# ---------------------------------------------------------------------------
# The connect walk: this source's own hierarchy
# ---------------------------------------------------------------------------
#
# These three moved here with the code, from `tests/test_wizard.py`. Core used
# to walk organisation → project → board itself, which made one board server's
# data model an obligation on the axis; it now asks the source and takes back
# whatever identifies the connection.


class _Client:
    """The three listings this source's hierarchy is walked over."""

    def __init__(self, orgs=None, projects=None, boards=None) -> None:
        self.orgs = orgs or [{"id": "o1", "name": "Acme"}]
        self.projects = projects or [{"id": "p1", "name": "Web"}]
        self.boards = boards or [{"id": "b1", "name": "Frontend"}]
        self.asked: list[str] = []

    def list_organisations(self) -> list[dict]:
        self.asked.append("organisations")
        return self.orgs

    def list_projects(self, org_id: str) -> list[dict]:
        self.asked.append(f"projects of {org_id}")
        return self.projects

    def list_boards(self, project_id: str) -> list[dict]:
        self.asked.append(f"boards of {project_id}")
        return self.boards


def _first(label, options, *, to_label, default_index=0):
    """A chooser that always takes the first option, standing in for core's."""
    return options[default_index]


def test_the_walk_descends_this_sources_own_three_levels() -> None:
    """Organisation → project → board, each scoped by the one above it — this
    board server's hierarchy, asked for by the source rather than by core."""
    client = _Client()

    identity = wizard.connection(client, choose=_first)

    assert client.asked == ["organisations", "projects of o1", "boards of p1"]
    assert identity == {"board": "b1", "name": "frontend", "repo": None}


def test_the_walk_suggests_a_name_from_what_was_picked() -> None:
    """The connection name core offers to edit comes from the board that was
    actually chosen, not from a fixed default."""
    client = _Client(boards=[{"id": "b9", "name": "Payments API"}])

    assert wizard.connection(client, choose=_first)["name"] == "payments-api"


def test_suggest_name_slugifies_board_name() -> None:
    """The board name is lowercased and non-alphanumerics become hyphens."""
    assert wizard.suggest_name("Frontend Web!", "Acme") == "frontend-web"


def test_suggest_name_falls_back_to_project_then_default() -> None:
    """An empty board name falls back to the project, then a safe default."""
    assert wizard.suggest_name("", "Payments API") == "payments-api"
    assert wizard.suggest_name("", "") == "connection"


def test_an_entity_is_labelled_by_name_then_id() -> None:
    """A record is shown by its name, or its id when nameless."""
    assert wizard._label({"id": "x", "name": "Acme"}) == "Acme"
    assert wizard._label({"id": "x"}) == "x"


# ---------------------------------------------------------------------------
# The per-connection settings hook (`settings_wizard`)
# ---------------------------------------------------------------------------


def _pick_default(label, values, default, *, help_for=None):
    """A choose_literal that presses Enter on everything."""
    return default


def test_settings_asks_for_this_sources_own_keys_with_their_defaults() -> None:
    """Mode, plan confirmation and done-mode are this source's settings, so
    this hook asks for them — and its defaults are the settings models' own."""
    settings, changes = wizard.settings(choose_literal=_pick_default, sandboxed=False)

    assert settings == {"mode": "build", "confirm": True, "done": "review"}
    assert changes is True


def test_a_respond_connection_reports_that_changes_are_barred() -> None:
    """The second return value is the neutral fact the workspace hook needs:
    respond bars `changes` (mirroring `Issuebear.permits`), stated by this
    source instead of core reading `mode` by name."""

    def pick(label, values, default, *, help_for=None):
        return "respond" if label == "Mode" else default

    settings, changes = wizard.settings(choose_literal=pick, sandboxed=False)

    assert settings["mode"] == "respond"
    assert changes is False


def test_a_sandboxed_connection_is_not_offered_respond() -> None:
    """A machine booted per task exists to build in — mode is forced, not
    asked, so the Mode menu must never render."""
    asked: list[str] = []

    def pick(label, values, default, *, help_for=None):
        asked.append(label)
        return default

    settings, changes = wizard.settings(choose_literal=pick, sandboxed=True)

    assert settings["mode"] == "build"
    assert changes is True
    assert "Mode" not in asked


def test_the_confirm_menu_answers_as_the_settings_bool() -> None:
    """ "yes"/"no" is flag-and-menu vocabulary; the saved setting is the bool."""

    def pick(label, values, default, *, help_for=None):
        return "no" if label == "Confirm the plan" else default

    settings, _ = wizard.settings(choose_literal=pick, sandboxed=False)

    assert settings["confirm"] is False
