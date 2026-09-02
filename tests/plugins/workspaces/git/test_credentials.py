"""How a workspace authenticates its GitHub remote.

Every run environment holds a credential for the ``gh`` CLI and nothing else —
a Railway sandbox is given ``GH_TOKEN``, no SSH key and no known-hosts entry.
git reads no such variable of its own, so a workspace has to be told to ask
``gh`` for the password, or cloning a private repo over HTTPS stops for input
nobody is there to give."""

from __future__ import annotations

from issuebot.config import Connection
from issuebot.plugins.workspaces.git.workspace import _working_copy
from issuebot.process import RecordingProcess

KEY = "credential.https://github.com.helper"
HELPER = "!gh auth git-credential"

# What git must end up with, in reading order: the empty value that resets the
# helper list, then ``gh``. Without the reset git keeps every helper the
# machine already has and takes the first answer — a stale personal credential
# on a developer's laptop or a CI runner.
WANTED = ["", HELPER]


def _clone(proc: RecordingProcess, root) -> None:
    """Cut a working copy for one task from an HTTPS GitHub remote."""
    project = Connection(name="p", repo="https://github.com/acme/web.git")
    _working_copy(project, "PAR-12", str(root), proc)


def test_a_fresh_clone_authenticates_through_gh(tmp_path):
    """The credential helper is set by the clone itself, so it covers the
    clone's own fetch as well as every later one."""
    proc = RecordingProcess()

    _clone(proc, tmp_path)

    clone = next(c for c in proc.calls if c[:2] == ["git", "clone"])
    set_here = [a.split("=", 1)[1] for a in clone if a.startswith(f"{KEY}=")]

    assert set_here == WANTED


def test_an_existing_clone_is_corrected(tmp_path):
    """A workspace cut before issuebot set the helper — or cloned by hand — is
    given it on reuse rather than being left unable to fetch."""
    workspace = tmp_path / "p" / "PAR-12"
    (workspace / ".git").mkdir(parents=True)
    proc = RecordingProcess()

    _clone(proc, tmp_path)

    configured = [c[-1] for c in proc.calls if c[:3] == ["git", "config", "--local"] and KEY in c]

    assert configured == WANTED
