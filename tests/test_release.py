import importlib
import os
import shlex
import subprocess
from importlib.metadata import PackageNotFoundError
from pathlib import Path

import pytest

from issuebot.config import DEFAULT_UPDATE_COMMAND
from issuebot.sandbox_protocol import update_argv


def _release():
    return importlib.import_module("issuebot.release")


class _Distribution:
    def __init__(self, root: Path) -> None:
        self.root = root

    def locate_file(self, path: str) -> Path:
        return self.root / path


def _stub(directory: Path, name: str, body: str) -> None:
    path = directory / name
    path.write_text(f"#!/bin/sh\n{body}")
    path.chmod(0o755)


def _execute_installer_command(
    tmp_path: Path, command: str, *, curl_exit: int
) -> tuple[subprocess.CompletedProcess[str], list[str], list[str], Path]:
    tools = tmp_path / "tools"
    tools.mkdir()
    curl_args = tmp_path / "curl-args"
    shell_args = tmp_path / "shell-args"
    download_path = tmp_path / "download-path"
    _stub(
        tools,
        "curl",
        """printf '%s\\n' "$@" > "$ISSUEBOT_TEST_CURL_ARGS"
output=''
while [ "$#" -gt 0 ]; do
    if [ "$1" = '-o' ]; then
        shift
        output=$1
    fi
    shift
done
printf '%s' "$output" > "$ISSUEBOT_TEST_DOWNLOAD_PATH"
if [ "$ISSUEBOT_TEST_CURL_EXIT" -ne 0 ]; then
    exit "$ISSUEBOT_TEST_CURL_EXIT"
fi
printf '# downloaded installer\\n' > "$output"
""",
    )
    _stub(tools, "sh", 'printf \'%s\\n\' "$@" > "$ISSUEBOT_TEST_SHELL_ARGS"\n')
    env = os.environ | {
        "PATH": f"{tools}:{os.environ['PATH']}",
        "ISSUEBOT_TEST_CURL_ARGS": str(curl_args),
        "ISSUEBOT_TEST_CURL_EXIT": str(curl_exit),
        "ISSUEBOT_TEST_DOWNLOAD_PATH": str(download_path),
        "ISSUEBOT_TEST_SHELL_ARGS": str(shell_args),
    }
    result = subprocess.run(["/bin/sh", "-c", command], capture_output=True, text=True, env=env)
    downloaded = Path(download_path.read_text())
    return (
        result,
        curl_args.read_text().splitlines(),
        shell_args.read_text().splitlines() if shell_args.exists() else [],
        downloaded,
    )


def test_release_urls_name_the_exact_versioned_assets() -> None:
    release = _release()
    assert release.wheel_url("1.2.3") == (
        "https://github.com/teamwebhq/issuebot-public/releases/download/"
        "v1.2.3/issuebot-1.2.3-py3-none-any.whl"
    )
    assert release.installer_url("1.2.3").endswith("/v1.2.3/install.sh")
    assert release.installer_url().endswith("/latest/download/install.sh")


@pytest.mark.parametrize(
    ("command", "url"),
    [
        (
            DEFAULT_UPDATE_COMMAND,
            "https://github.com/teamwebhq/issuebot-public/releases/latest/download/install.sh",
        ),
        (
            update_argv("1.2.3"),
            "https://github.com/teamwebhq/issuebot-public/releases/download/v1.2.3/install.sh",
        ),
    ],
)
def test_a_failed_installer_download_is_nonzero_and_never_runs_sh(
    tmp_path: Path, command: str | list[str], url: str
) -> None:
    script = command[2] if isinstance(command, list) else shlex.split(command)[2]
    result, curl, installer, downloaded = _execute_installer_command(tmp_path, script, curl_exit=23)

    assert result.returncode != 0
    assert url in curl
    assert installer == []
    assert not downloaded.exists()


@pytest.mark.parametrize(
    ("command", "expected_installer_args"),
    [
        (DEFAULT_UPDATE_COMMAND, []),
        (update_argv("1.2.3"), ["1.2.3"]),
    ],
)
def test_a_successful_installer_download_runs_the_file_then_cleans_it_up(
    tmp_path: Path, command: str | list[str], expected_installer_args: list[str]
) -> None:
    script = command[2] if isinstance(command, list) else shlex.split(command)[2]
    result, _curl, installer, downloaded = _execute_installer_command(tmp_path, script, curl_exit=0)

    assert result.returncode == 0
    assert installer == [str(downloaded), *expected_installer_args]
    assert not downloaded.exists()


@pytest.mark.parametrize("value", ["", "v1.2.3", "1.2", "1.2.3rc1", "01.2.3"])
def test_release_urls_reject_non_stable_versions(value: str) -> None:
    release = _release()
    with pytest.raises(ValueError, match="stable X.Y.Z"):
        release.wheel_url(value)


def test_the_imported_installed_package_is_a_release_wheel(monkeypatch) -> None:
    release = _release()
    package = Path(release.__file__).resolve().parent
    monkeypatch.setattr(release, "distribution", lambda name: _Distribution(package.parent))
    assert release.is_installed_wheel() is True


def test_an_editable_or_source_package_is_not_a_release_wheel(tmp_path, monkeypatch) -> None:
    release = _release()
    monkeypatch.setattr(release, "distribution", lambda name: _Distribution(tmp_path))
    assert release.is_installed_wheel() is False


def test_a_package_without_distribution_metadata_is_not_a_release_wheel(monkeypatch) -> None:
    release = _release()

    def missing(name: str):
        raise PackageNotFoundError(name)

    monkeypatch.setattr(release, "distribution", missing)
    assert release.is_installed_wheel() is False
