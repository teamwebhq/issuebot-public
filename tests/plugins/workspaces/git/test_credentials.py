"""How a workspace authenticates its GitHub remote.

git reads no token variable of its own, so a workspace has to be told where the
password comes from, or cloning a private repo over HTTPS stops for input
nobody is there to give.

Two answers, in this order: the token the run already holds in ``GH_TOKEN``
(the short-lived one the board lends, else whatever the environment was given),
then ``gh``'s own store for a machine where a person ran `gh auth login`. The
token has to come first — a sandbox image may ship a wrapper around `gh` that
refuses `gh auth git-credential`, and a clone with only that helper then fails
with git asking for a username.
"""

from __future__ import annotations

import os
import subprocess

from issuebot.config import Connection
from issuebot.forge import (
    GH_CREDENTIAL_CONFIG,
    GH_CREDENTIAL_HELPER,
    TOKEN_CREDENTIAL_HELPER,
)
from issuebot.forge import (
    GH_CREDENTIAL_KEY as KEY,
)
from issuebot.plugins.workspaces.git.workspace import _working_copy
from issuebot.process import RecordingProcess

# What git must end up with, in reading order: the empty value that resets the
# helper list, then the run's own token, then ``gh``. Without the reset git
# keeps every helper the machine already has and takes the first answer — a
# stale personal credential on a developer's laptop or a CI runner.
WANTED = ["", TOKEN_CREDENTIAL_HELPER, GH_CREDENTIAL_HELPER]


def _clone(proc: RecordingProcess, root) -> None:
    """Cut a working copy for one task from an HTTPS GitHub remote."""
    project = Connection(name="p", repo="https://github.com/acme/web.git")
    _working_copy(project, "PAR-12", str(root), proc)


def test_a_fresh_clone_authenticates_with_the_runs_own_token(tmp_path):
    """The credential helper is set by the clone itself, so it covers the
    clone's own fetch as well as every later one."""
    proc = RecordingProcess()

    _clone(proc, tmp_path)

    clone = next(c for c in proc.calls if c[:2] == ["git", "clone"])
    set_here = [a.split("=", 1)[1] for a in clone if a.startswith(f"{KEY}=")]

    assert set_here == WANTED


def test_a_fresh_clone_keeps_the_helpers_for_the_agents_own_git(tmp_path):
    """`-c` authenticates the clone and is not written to the copy it makes.
    The agent works inside that copy and runs git of its own, so the list is
    persisted too — without it the agent's first push authenticates as whatever
    the machine holds."""
    proc = RecordingProcess()

    _clone(proc, tmp_path)

    configured = [c[-1] for c in proc.calls if c[:3] == ["git", "config", "--local"] and KEY in c]

    assert configured == WANTED


def test_an_existing_clone_is_corrected(tmp_path):
    """A workspace cut before issuebot set the helpers — or cloned by hand — is
    given them on reuse rather than being left unable to fetch."""
    workspace = tmp_path / "p" / "PAR-12"
    (workspace / ".git").mkdir(parents=True)
    proc = RecordingProcess()

    _clone(proc, tmp_path)

    configured = [c[-1] for c in proc.calls if c[:3] == ["git", "config", "--local"] and KEY in c]

    assert configured == WANTED


# --- the helper as git actually runs it ---------------------------------------
#
# The list above is configuration; these run it. A shell helper that is subtly
# wrong reads the same in an assertion on argv and fails only against real git.


def _fill(**env: str) -> subprocess.CompletedProcess[str]:
    """Ask git for github.com credentials under this environment.

    Returns the whole result: a run where no helper answers is a *failure* for
    git (it falls through to prompting a terminal that is not there), and that
    is the expected outcome of one of the tests below."""
    child = {k: v for k, v in os.environ.items() if k != "GH_TOKEN"}
    child.update(env)
    # Only the token helper, so `gh` on the developer's machine cannot answer
    # for it and make a broken helper look like it worked. Rebuilt as `-c`
    # pairs: dropping an entry alone would leave its `-c` to swallow the
    # subcommand.
    keep = [e for e in GH_CREDENTIAL_CONFIG if not e.endswith(GH_CREDENTIAL_HELPER)]
    args = [arg for entry in keep for arg in ("-c", entry)]
    done = subprocess.run(  # noqa: S603 - argv is this module's own constants
        ["git", *args, "credential", "fill"],
        input="protocol=https\nhost=github.com\n\n",
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
        env=child,
    )
    return done


def test_the_token_helper_answers_git_with_the_token_in_the_environment():
    done = _fill(GH_TOKEN="ghs_notarealtoken")

    assert done.returncode == 0, done.stderr
    assert "username=x-access-token" in done.stdout
    assert "password=ghs_notarealtoken" in done.stdout


def test_the_token_helper_stays_silent_without_a_token():
    """Silence is what lets the next helper in the list have its turn. A helper
    that answered with an empty password would instead be git's first answer,
    and `gh` would never be asked."""
    done = _fill(GH_TOKEN="")

    # Nothing answered, so git went looking for a terminal and found none.
    # That is the fall-through this test is about: with `gh` in the list it is
    # `gh`'s turn next, not a wrong answer already given.
    assert "x-access-token" not in done.stdout
    assert done.returncode != 0


def test_the_helper_survives_git_config_parameters_packing():
    """The same string is packed into `GIT_CONFIG_PARAMETERS`, whose format
    single-quotes each entry — so the helper must contain no single quote."""
    assert "'" not in TOKEN_CREDENTIAL_HELPER
    assert all("'" not in entry for entry in GH_CREDENTIAL_CONFIG)
