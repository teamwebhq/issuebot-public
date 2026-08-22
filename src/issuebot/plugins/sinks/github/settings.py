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

    A run may be *lent* one for the duration (``Delivery.forge_env``, the
    board's own GitHub App token), which the sink applies to its ``gh`` calls
    so the pull request is opened by the same actor that pushed the branch.
    That is a fact about the run, not a setting either.
    """

    # Model for the PR-description one-shot. None → the harness's small default.
    summary_model: str | None = None
