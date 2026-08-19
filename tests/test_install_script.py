"""``install.sh`` — the one way issuebot gets installed, by people and sandboxes.

Exercised with stubs on PATH rather than by installing anything: the script's
job is to work out *what* to install and *where*, and both halves have been
silently wrong before. ``ISSUEBOT_BIN_DIR`` is set in every case here so the
answer to "where" is the test's decision and not the host's — whether the
machine running the suite happens to have a writable ``/usr/local/bin`` is not
something these tests may depend on.

The curl stub represents GitHub's latest-release redirect, so these tests cover
release selection without fetching an external asset.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from issuebot.sandbox_protocol import update_argv

SCRIPT = Path(__file__).resolve().parent.parent / "install.sh"
LATEST_VERSION = "2.3.4"
RESOLVED = "5c5c" + "b" * 36  # what the stub `git ls-remote` answers


class Install:
    """One run of the installer against stubbed tools.

    The stubs record what they were called with *and the environment that
    mattered*, so a test can assert where the binary was aimed rather than only
    that something was installed.
    """

    def __init__(
        self,
        tmp_path: Path,
        *args: str,
        bin_on_path: bool = True,
        uv_exit: int = 0,
        reported_version: str | None = None,
    ):
        self.bin_dir = tmp_path / "bin"
        self.bin_dir.mkdir()
        tool_dir = tmp_path / "tools"
        tool_dir.mkdir()
        log = tmp_path / "calls.log"

        record = f'echo "uv UV_TOOL_BIN_DIR=$UV_TOOL_BIN_DIR $*" >> {log}'
        self._stub(tool_dir, "uv", f"{record}\nexit {uv_exit}\n")
        self._stub(
            tool_dir, "git", f'echo "git $*" >> {log}\nprintf "{RESOLVED}\\trefs/heads/main\\n"\n'
        )
        self._stub(
            tool_dir,
            "curl",
            f'echo "curl $*" >> {log}\n'
            'case "$*" in\n'
            '    *"/releases/latest"*) printf "https://github.com/teamwebhq/issuebot-public/'
            f'releases/tag/v{LATEST_VERSION}" ;;\n'
            "esac\n",
        )
        expected_version = args[0] if args else LATEST_VERSION
        self._stub(self.bin_dir, "issuebot", f'echo "{reported_version or expected_version}"\n')

        # The stub dir is where the binary is aimed; PATH is what the caller's
        # environment would have had. Separating them is what lets a test say
        # "installed somewhere unreachable" without touching the host.
        path = (
            f"{tool_dir}:{self.bin_dir}:/usr/bin:/bin"
            if bin_on_path
            else f"{tool_dir}:/usr/bin:/bin"
        )
        self.done = subprocess.run(
            ["sh", str(SCRIPT), *args],
            capture_output=True,
            text=True,
            env={"PATH": path, "HOME": str(tmp_path), "ISSUEBOT_BIN_DIR": str(self.bin_dir)},
        )
        self.calls = log.read_text().splitlines() if log.exists() else []

    def _stub(self, directory: Path, name: str, body: str) -> None:
        path = directory / name
        path.write_text(f"#!/bin/sh\n{body}")
        path.chmod(0o755)

    @property
    def install(self) -> str:
        """The `uv tool install` the script ran, if it got that far."""
        return next((call for call in self.calls if call.startswith("uv ")), "")


def test_the_script_is_valid_shell():
    """It is fetched and piped into `sh` on a machine nobody is watching."""
    assert subprocess.run(["sh", "-n", str(SCRIPT)]).returncode == 0


def test_it_installs_the_exact_release_wheel(tmp_path: Path):
    """An exact release request must name its immutable wheel asset."""
    run = Install(tmp_path, "1.2.3")

    assert run.done.returncode == 0
    assert (
        "https://github.com/teamwebhq/issuebot-public/releases/download/"
        "v1.2.3/issuebot-1.2.3-py3-none-any.whl"
    ) in run.install


def test_it_installs_where_it_says_it_does(tmp_path: Path):
    """uv's own default (~/.local/bin) is not on the PATH of a non-interactive
    exec, which is how a sandbox's issuebot came to be unreachable."""
    run = Install(tmp_path, "1.2.3")

    assert f"UV_TOOL_BIN_DIR={run.bin_dir}" in run.install


def test_a_hand_install_resolves_the_latest_release(tmp_path: Path):
    """A hand install follows GitHub's latest-release redirect to a wheel."""
    run = Install(tmp_path)

    assert run.done.returncode == 0
    assert any(call.endswith("/releases/latest") for call in run.calls)
    assert "/v2.3.4/issuebot-2.3.4-py3-none-any.whl" in run.install


@pytest.mark.parametrize("version", ["main", "v1.2.3", "1.2", "01.2.3"])
def test_it_rejects_anything_that_is_not_a_stable_version(tmp_path: Path, version: str):
    """Only canonical stable versions can identify immutable release assets."""
    run = Install(tmp_path, version)

    assert run.done.returncode != 0
    assert run.install == ""


def test_it_rejects_an_installed_version_other_than_the_request(tmp_path: Path):
    """A successful tool install is insufficient if it installed another wheel."""
    run = Install(tmp_path, "1.2.3", reported_version="9.8.7")

    assert run.done.returncode != 0
    assert "reports 9.8.7" in run.done.stderr


def test_an_unreachable_install_fails_rather_than_reporting_success(tmp_path: Path):
    """Checked against the PATH we were *called* with: this script prepends to
    its own, and a binary findable only because of that is not installed as far
    as the next process is concerned."""
    run = Install(tmp_path, "1.2.3", bin_on_path=False)

    assert run.done.returncode != 0
    assert "PATH" in run.done.stderr
    assert run.install == ""  # and it said so before spending the minutes


def test_a_failed_install_is_loud(tmp_path: Path):
    """Half-installed is the outcome that surfaces later as a confusing failure
    in somebody's task, so it exits non-zero instead."""
    assert Install(tmp_path, "1.2.3", uv_exit=1).done.returncode != 0


@pytest.mark.parametrize("version", ["1.2.3", "9.8.7"])
def test_the_sandbox_fetches_the_installer_for_the_exact_release(version):
    """The release URL and installer argument identify the same exact version."""
    command = " ".join(update_argv(version))

    assert f"/releases/download/v{version}/{SCRIPT.name}" in command
    assert version in command.split("|")[-1]
    assert SCRIPT.is_file()
