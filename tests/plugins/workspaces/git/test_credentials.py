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

HELPER = "credential.https://github.com.helper=!gh auth git-credential"


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
    assert "-c" in clone
    assert clone[clone.index("-c") + 1] == HELPER


def test_an_existing_clone_is_corrected(tmp_path):
    """A workspace cut before issuebot set the helper — or cloned by hand — is
    given it on reuse rather than being left unable to fetch."""
    workspace = tmp_path / "p" / "PAR-12"
    (workspace / ".git").mkdir(parents=True)
    proc = RecordingProcess()

    _clone(proc, tmp_path)

    assert ["git", "config", "--local", *HELPER.split("=", 1)] in proc.calls
