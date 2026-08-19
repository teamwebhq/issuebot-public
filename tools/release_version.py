"""Validate the immutable release version selected for a Git revision."""

from __future__ import annotations

import argparse
import re
import subprocess
import tomllib
from collections.abc import Collection, Sequence
from pathlib import Path

_STABLE = re.compile(r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)")


def read_version(text: str) -> str:
    """Read ``[project].version`` from TOML text."""
    data = tomllib.loads(text)
    try:
        value = data["project"]["version"]
    except (KeyError, TypeError) as exc:
        raise ValueError("pyproject.toml has no [project].version") from exc
    if not isinstance(value, str):
        raise ValueError("[project].version must be a string")
    return value


def validate(
    version: str,
    *,
    base_version: str | None,
    tags: Collection[str],
) -> str:
    """Validate one immutable stable release identity."""
    if not _STABLE.fullmatch(version):
        raise ValueError(f"{version!r} is not a stable X.Y.Z version")
    if base_version == version:
        raise ValueError(f"version must change from {base_version}")
    tag = f"v{version}"
    if tag in tags:
        raise ValueError(f"release tag {tag} already exists")
    return version


def main(argv: Sequence[str] | None = None) -> None:
    """Print the validated project version for the current Git checkout."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--base-ref",
        help="Git ref that supplies the previous project version",
    )
    args = parser.parse_args(argv)

    try:
        version = read_version(Path("pyproject.toml").read_text())
        base_version = _base_version(args.base_ref) if args.base_ref else None
        tags = set(_git_output("tag", "--list").splitlines())
        print(validate(version, base_version=base_version, tags=tags))
    except ValueError as exc:
        parser.error(str(exc))


def _base_version(base_ref: str) -> str:
    """Return the project version from a supplied Git base ref."""
    return read_version(_git_output("show", f"{base_ref}:pyproject.toml"))


def _git_output(*args: str) -> str:
    """Run one fixed Git command and return its decoded standard output."""
    completed = subprocess.run(
        ["git", *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout


if __name__ == "__main__":
    main()
