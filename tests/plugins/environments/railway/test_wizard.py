"""The railway plugin's own wizard hook.

Its questions used to sit in the generic ``issuebot.wizard``, which had no
business knowing about environment ids, network modes or token kinds. The
generic wizard now asks whichever environment the user picked for its own hook;
these drive that hook directly, with the numbered picker and repo prompt handed
in exactly as the wizard hands them in.
"""

from __future__ import annotations

import pytest

from issuebot.plugins.environments.railway import wizard as railway_wizard
from issuebot.wizard import _choose_literal


def _run(monkeypatch: pytest.MonkeyPatch, answers: list[str]) -> dict:
    """Drive the hook with scripted answers and no console output."""
    replies = iter(answers)
    monkeypatch.setattr(railway_wizard.typer, "prompt", lambda *a, **kw: next(replies))
    monkeypatch.setattr(railway_wizard.typer, "echo", lambda *a, **kw: None)
    monkeypatch.setattr(railway_wizard, "_warn_prereqs", lambda has_token=False: None)
    return railway_wizard.wizard(choose_literal=_choose_literal)


def test_it_collects_a_per_connection_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """A Railway project token only reaches one project, so the wizard asks for
    the connection's own credential instead of relying on one process-wide env
    var — otherwise a second railway connection can't authenticate at all."""
    params = _run(
        monkeypatch,
        [
            "env_123",  # railway environment id
            "1",  # network: isolated
            "tok-a",  # railway token
            "1",  # token kind: project
        ],
    )

    assert params["railway"].token == "tok-a"
    assert params["railway"].token_kind == "project"
    assert params["railway"].environment_id == "env_123"


def test_a_blank_token_inherits_the_runners_own(monkeypatch: pytest.MonkeyPatch) -> None:
    """Blank keeps today's behaviour: inherit whatever the listen process has —
    and the token-kind question is not asked at all, having nothing to describe."""
    params = _run(monkeypatch, ["env_123", "1", ""])

    assert params["railway"].token is None


def test_it_answers_only_for_its_own_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """This hook owns `[connections.railway]` and nothing else. The clone/task-
    branch/build consequences of a sandbox are the workspace's and the source's
    own hooks to draw from the wizard's neutral `sandboxed` fact — a repo or a
    strategy key answered here would be one plugin writing another's settings
    (ADR-0002)."""
    params = _run(monkeypatch, ["env_123", "1", ""])

    assert set(params) == {"railway"}
