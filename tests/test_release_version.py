"""Tests for the release-version pull-request policy."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "tools" / "release_version.py"


@pytest.fixture
def release_version() -> ModuleType:
    """Load the policy tool after proving that it was created."""
    assert TOOL.is_file(), "tools/release_version.py is absent"
    spec = importlib.util.spec_from_file_location("release_version", TOOL)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_a_new_stable_version_is_accepted(release_version: ModuleType) -> None:
    """A changed, unused stable release identity remains valid."""
    assert release_version.validate("1.2.3", base_version="1.2.2", tags=set()) == "1.2.3"


def test_an_unchanged_pull_request_version_is_rejected(release_version: ModuleType) -> None:
    """Removing the base-version guard would accept an unpublishable merge."""
    with pytest.raises(ValueError, match="must change from 1.2.3"):
        release_version.validate("1.2.3", base_version="1.2.3", tags=set())


@pytest.mark.parametrize("version", ["", "v1.2.3", "1.2", "1.2.3rc1", "01.2.3"])
def test_non_stable_versions_are_rejected(release_version: ModuleType, version: str) -> None:
    """Relaxing stable syntax would allow identities releases do not support."""
    with pytest.raises(ValueError, match="stable X.Y.Z"):
        release_version.validate(version, base_version="1.2.2", tags=set())


def test_an_existing_release_tag_is_rejected(release_version: ModuleType) -> None:
    """Removing the tag guard would let CI attempt to replace an immutable release."""
    with pytest.raises(ValueError, match="v1.2.3 already exists"):
        release_version.validate("1.2.3", base_version="1.2.2", tags={"v1.2.3"})


def test_read_version_reads_the_project_version(release_version: ModuleType) -> None:
    """The policy obtains its identity from the project metadata."""
    assert release_version.read_version('[project]\nversion = "1.2.3"\n') == "1.2.3"


def test_read_version_rejects_missing_project_version(release_version: ModuleType) -> None:
    """A missing version must fail before release policy can make a decision."""
    with pytest.raises(ValueError, match=r"no \[project\]\.version"):
        release_version.read_version('[project]\nname = "issuebot"\n')


def test_cli_rejects_a_version_matching_an_optional_base_ref(tmp_path: Path) -> None:
    """The optional base ref is the source of the prior project version."""
    _git(tmp_path, "init", "-b", "main")
    _git(tmp_path, "config", "user.email", "test@example.com")
    _git(tmp_path, "config", "user.name", "Test User")
    _write_pyproject(tmp_path, "1.2.2")
    _git(tmp_path, "add", "pyproject.toml")
    _git(tmp_path, "commit", "-m", "base version")

    _git(tmp_path, "switch", "-c", "feature")
    _write_pyproject(tmp_path, "1.2.3")
    _git(tmp_path, "commit", "-am", "feature version")

    _git(tmp_path, "switch", "main")
    _write_pyproject(tmp_path, "1.2.3")
    _git(tmp_path, "commit", "-am", "main version")
    _git(tmp_path, "switch", "feature")

    completed = subprocess.run(
        [sys.executable, str(TOOL), "--base-ref", "main"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 2
    assert completed.stdout == ""
    assert "version must change from 1.2.3" in completed.stderr


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def _write_pyproject(path: Path, version: str) -> None:
    (path / "pyproject.toml").write_text(f'[project]\nversion = "{version}"\n')
