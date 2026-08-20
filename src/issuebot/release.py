"""Immutable GitHub Release wheel policy and canonical asset locations."""

from __future__ import annotations

import os
import re
import shlex
import sys
from importlib.metadata import PackageNotFoundError, distribution
from pathlib import Path

from issuebot import REPO_URL

_STABLE_VERSION = re.compile(r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)")
RELEASES_URL = f"{REPO_URL}/releases"


def stable_version(value: str) -> str:
    """Return a stable release version, or reject unsupported syntax."""
    if not _STABLE_VERSION.fullmatch(value):
        raise ValueError(f"{value!r} is not a stable X.Y.Z version")
    return value


def installer_url(version: str | None = None) -> str:
    """Return the latest or exact release installer URL."""
    if version is None:
        return f"{RELEASES_URL}/latest/download/install.sh"
    return f"{RELEASES_URL}/download/v{stable_version(version)}/install.sh"


def installer_command(version: str | None = None) -> str:
    """Download then run the latest or exact released installer safely."""
    checked = stable_version(version) if version is not None else None
    url = shlex.quote(installer_url(checked))
    version_arg = f" {shlex.quote(checked)}" if checked is not None else ""
    return (
        "installer=$(mktemp) || exit 1; "
        "trap 'rm -f \"$installer\"' 0; "
        f'curl -fsSL {url} -o "$installer" && sh "$installer"{version_arg}'
    )


INSTALL_COMMAND = installer_command()


def installer_argv(version: str | None = None) -> list[str]:
    """Argv that installs the latest (or one exact) release, without a shell.

    The one definition of how an issuebot install updates itself — a self-update
    on the local machine and an install inside a sandbox run the same program.
    It is built when the update happens, never stored, so a config or an image
    written months ago cannot pin an install to an installer URL that has moved.

    ``sh -c`` because every caller hands this argv straight to an executor that
    uses no shell, so the download-then-install shell program must be one
    argument rather than three.
    """
    return ["sh", "-c", installer_command(version)]


def wheel_url(version: str) -> str:
    """Return the canonical wheel asset URL for one release."""
    checked = stable_version(version)
    return f"{RELEASES_URL}/download/v{checked}/issuebot-{checked}-py3-none-any.whl"


def is_installed_wheel() -> bool:
    """Whether this package is the installed, non-editable distribution copy."""
    try:
        installed = Path(str(distribution("issuebot").locate_file("issuebot"))).resolve()
    except PackageNotFoundError:
        return False
    imported = Path(__file__).resolve().parent
    return imported == installed


def install_bin_dir() -> Path | None:
    """Directory the running issuebot console script was launched from.

    ``uv tool install`` writes the ``issuebot`` console script into the bin
    directory, so ``sys.argv[0]`` names where this install actually lives — the
    one place a self-update may write. Returns ``None`` when the process did not
    start from that script (``python -m issuebot``, pytest, an embedded call),
    because then there is no install location to pin.

    The path is made absolute but not resolved: a bin directory of symlinks is
    the normal layout, and following them leads out of the bin directory.
    """
    argv0 = Path(sys.argv[0])
    if argv0.name != "issuebot":
        return None
    return Path(os.path.abspath(argv0)).parent
