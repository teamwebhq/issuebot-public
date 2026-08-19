"""Global settings for the GitHub sink plugin."""

from __future__ import annotations

from pydantic import BaseModel


class GlobalSettings(BaseModel):
    """`[github]`: behaviour shared by every connection's PRs.

    No credential field: every ``gh`` call this sink makes runs through the
    ``gh`` CLI's own authentication (``gh auth login`` / ``GH_TOKEN`` in its
    environment) exactly as the doctor check
    (:func:`~issuebot.plugins.sinks.github.doctor.doctor`) already verifies —
    there is nothing of the credential's own for issuebot to hold or pass
    down, so adding a ``token`` field here would be a setting nothing reads.
    """

    # Model for the PR-description one-shot. None → the harness's small default.
    summary_model: str | None = None
