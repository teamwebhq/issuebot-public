"""issuebot — run a coding agent against tasks assigned to it on a board."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("issuebot")
except PackageNotFoundError:  # a source tree that was never installed
    __version__ = "0+unknown"


REPO_URL = "https://github.com/teamwebhq/issuebot"
