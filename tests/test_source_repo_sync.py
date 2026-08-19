"""The project's repository is the connection's repository.

The wizard sets it at connect time; this keeps it true afterwards, for a
hand-edited config or a project relinked in Parade. There is no override — a
connection working a different repo than the project it reads tasks from
produces PRs that never surface on those tasks, which looks like the feature
silently not working."""

from __future__ import annotations

from typing import Any

import pytest

from issuebot.config import Connection
from issuebot.plugins.sources.issuebear.source import Issuebear


class _FakeClient:
    """The slice of `IssuebotClient` `sync_repo` calls: `get_board` then
    `get_project`. `fail=True` makes both raise, standing in for an
    unreachable (or too-old) board server."""

    def __init__(self, *, project: dict[str, Any], fail: bool = False) -> None:
        self._project = project
        self._fail = fail

    def get_board(self, board_id: str) -> dict[str, Any]:
        if self._fail:
            raise ConnectionError("board server unreachable")
        return {"id": board_id, "project_id": "proj-1"}

    def get_project(self, project_id: str) -> dict[str, Any]:
        if self._fail:
            raise ConnectionError("board server unreachable")
        return self._project


@pytest.fixture
def source_with_project():
    """Build an `Issuebear` bound to a `Connection` with the given `repo`
    setting, and a fake client whose `get_board`/`get_project` answer with a
    project carrying (or not carrying) a linked GitHub repo.

    ``source.saves`` counts how many times the connection's `repo` was
    actually corrected — `Issuebear` persists nothing to disk itself (see
    `_set_connection_repo`'s docstring), so this is the fixture's stand-in for
    "was the connection rewritten", the same thing a config file rewrite would
    prove once a caller with a whole `Config` and a path chooses to persist
    the correction."""

    def _make(
        *, project_repo: str | None, connection_repo: str | None, fail: bool = False
    ) -> Issuebear:
        project = {
            "id": "proj-1",
            "github_repo": {"ssh_url": project_repo} if project_repo else None,
        }
        connection = Connection(name="p", repo=connection_repo)
        client = _FakeClient(project=project, fail=fail)

        source = Issuebear(
            client,
            board="board-1",
            connection=connection,
            mcp_url="https://board.example/mcp",
            pat="pat-secret",
        )

        source.saves = 0
        original = source._set_connection_repo

        def _counting_set(repo: str) -> None:
            source.saves += 1
            original(repo)

        source._set_connection_repo = _counting_set
        return source

    return _make


def test_sync_corrects_a_drifted_repo(source_with_project):
    """A config edited by hand is put back, not obeyed."""
    source = source_with_project(
        project_repo="git@github.com:acme/web.git",
        connection_repo="git@github.com:acme/OLD.git",
    )

    assert source.sync_repo() == "git@github.com:acme/web.git"
    assert source.connection_repo() == "git@github.com:acme/web.git"


def test_sync_leaves_a_matching_repo_alone(source_with_project):
    """The common case must not rewrite the config file on every run."""
    source = source_with_project(
        project_repo="git@github.com:acme/web.git",
        connection_repo="git@github.com:acme/web.git",
    )

    assert source.sync_repo() == "git@github.com:acme/web.git"
    assert source.saves == 0


def test_sync_leaves_the_connection_alone_when_the_project_has_no_repo(source_with_project):
    """An unlinked project says nothing about the repo — it does not clear a
    connection that was configured by hand before linking existed."""
    source = source_with_project(
        project_repo=None,
        connection_repo="git@github.com:acme/local.git",
    )

    assert source.sync_repo() is None
    assert source.connection_repo() == "git@github.com:acme/local.git"


def test_sync_survives_an_unreachable_board_server(source_with_project):
    """A run must not die because the repo check could not be made."""
    source = source_with_project(
        project_repo="git@github.com:acme/web.git",
        connection_repo="git@github.com:acme/web.git",
        fail=True,
    )

    assert source.sync_repo() is None
