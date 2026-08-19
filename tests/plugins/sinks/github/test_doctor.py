"""The GitHub sink's own doctor check: can this connection actually open a PR.

Calls the plugin's hook directly rather than driving `issuebot doctor`. That a
connection listing a sink gets that sink's hook run at all is core's wiring, and
core proves it generically in `tests/plugins/test_mounting.py`; what is left
here is the only half this plugin owns — which conditions it complains about.
"""

from __future__ import annotations

import shutil
import subprocess

import pytest

from conftest import connection
from issuebot.plugins.sinks.github.doctor import doctor

# The check asks the sink's own `origin`, not the git workspace plugin — this is
# a sink, and a sink that imports a workspace is not deletable independently.
ORIGIN = "issuebot.plugins.sinks.github.sink.origin"


def _findings(conn, monkeypatch: pytest.MonkeyPatch, *, gh: bool, origin: bool = True) -> list[str]:
    """Everything the check says about `conn`, with `gh` and the remote scripted.

    `gh auth status` is stubbed to succeed so the two failures under test stay
    separable — an unauthenticated `gh` is its own warning, checked below.
    """
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/gh" if gh else None)
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: subprocess.CompletedProcess(a, 0))
    monkeypatch.setattr(ORIGIN, lambda proc, folder: "git@x:o/r.git" if origin else "")

    said: list[str] = []
    doctor(conn, echo=said.append)
    return said


def test_it_warns_when_gh_is_not_installed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Nothing else can be checked without it, so this is the only finding."""
    said = _findings(connection(git_init="branch"), monkeypatch, gh=False)

    assert len(said) == 1
    assert "gh" in said[0]


def test_it_warns_when_gh_is_not_authenticated(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/gh")
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: subprocess.CompletedProcess(a, 1))
    monkeypatch.setattr(ORIGIN, lambda proc, folder: "git@x:o/r.git")

    said: list[str] = []
    doctor(connection(git_init="branch"), echo=said.append)

    assert any("authenticated" in line for line in said)


def test_it_warns_when_a_local_folder_has_no_origin(monkeypatch: pytest.MonkeyPatch) -> None:
    """A PR is opened against a remote, so a checkout with none cannot serve one."""
    said = _findings(connection(git_init="branch"), monkeypatch, gh=True, origin=False)

    assert any("origin" in line for line in said)


def test_a_clone_connection_is_not_asked_about_a_remote(monkeypatch: pytest.MonkeyPatch) -> None:
    """A clone-based connection's workspace does not exist yet at doctor time —
    there is nothing on disk to have an origin, so asking would always warn."""
    conn = connection(folder=None, repo="https://example.com/o/r.git", git_init="branch")

    said = _findings(conn, monkeypatch, gh=True, origin=False)

    assert said == []
