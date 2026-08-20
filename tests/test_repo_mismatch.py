"""A connection only does work that belongs to the repository it is configured
for.

The board says which repository a task's project is linked to; the config says
which one this connection works in. When the two disagree one of them is wrong,
and the runner cannot tell which — so it refuses the run and says so, rather
than committing to a branch and opening a PR on a repository nobody is watching
for this task."""

from __future__ import annotations

import pytest

from issuebot.config import Connection
from issuebot.contracts import WorkItem
from issuebot.runner import RepoMismatch, check_repo

REPO = "https://github.com/acme/web.git"


def _work(repo: str | None) -> WorkItem:
    """One assigned task, carrying the repository its project is linked to."""
    return WorkItem(task_id="t1", reference="ISS-1", repo=repo)


def test_a_task_from_another_repository_is_refused():
    """The whole point: the mismatch stops the run before a workspace exists."""
    conn = Connection(name="web", repo=REPO)

    with pytest.raises(RepoMismatch) as caught:
        check_repo(conn, _work("https://github.com/acme/other.git"))

    # Both URLs, so the person reading it on the task can tell which to fix.
    assert "acme/other.git" in str(caught.value)
    assert REPO in str(caught.value)


def test_a_matching_repository_runs():
    conn = Connection(name="web", repo=REPO)

    check_repo(conn, _work(REPO))


def test_a_task_whose_project_names_no_repository_is_not_a_mismatch():
    """An unlinked project — or a board that could not reach GitHub just now —
    says nothing about this connection, so there is nothing to disagree with."""
    conn = Connection(name="web", repo=REPO)

    check_repo(conn, _work(None))


def test_a_folder_connection_is_not_a_mismatch():
    """A connection working a checkout already on this machine has no repo URL
    in its config to compare. Where that checkout came from is git's business."""
    conn = Connection(name="web", folder="/srv/web")

    check_repo(conn, _work(REPO))
