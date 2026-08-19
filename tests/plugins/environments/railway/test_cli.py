"""``issuebot railway ...``: the plugin's own project-wide commands.

They reach the CLI through ``plugins.mount_cli``, so these drive the real
top-level app — the same way ``issuebot git worktree`` is exercised — rather
than the plugin's Typer object in isolation. What is asserted is which Railway
calls each command makes, never a live CLI.
"""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from issuebot import cli
from issuebot.plugins.environments.railway import cli as railway_cli
from issuebot.plugins.environments.railway.environment import RailwayProvider
from issuebot.process import RecordingProcess

runner = CliRunner()


def _provider_deleting(on_delete):
    """A `RailwayProvider` replacement whose delete_checkpoint calls ``on_delete``.

    The prune sweep's contract is which names it deletes and that one failure
    does not stop it — neither needs a real Railway CLI."""

    class _Provider:
        def __init__(self, **kwargs):
            pass

        def delete_checkpoint(self, name: str) -> None:
            on_delete(name)

    return _Provider


def test_railway_build_template_builds_the_shared_template(monkeypatch: pytest.MonkeyPatch):
    proc = RecordingProcess()
    monkeypatch.setattr(
        railway_cli, "RailwayProvider", lambda **kw: RailwayProvider(proc=proc, **kw)
    )

    result = runner.invoke(cli.app, ["railway", "build-template"])

    assert result.exit_code == 0, result.output
    argv = proc.calls[0]
    assert argv[:4] == ["railway", "sandbox", "template", "build"]
    for package in ("git", "gh", "nodejs"):
        assert package in argv


def test_railway_prune_checkpoints_deletes_only_aged_task_checkpoints(
    monkeypatch: pytest.MonkeyPatch,
):
    """`issuebot railway prune-checkpoints` finds task-* checkpoints older than
    the TTL (via task_checkpoints.aged) and deletes each, then echoes the count."""
    captured: dict = {}
    deleted: list[str] = []
    forgotten: list[str] = []

    def fake_aged(ttl_seconds: float) -> list[str]:
        captured["ttl_seconds"] = ttl_seconds
        return ["t1", "t2"]

    monkeypatch.setattr(railway_cli.task_checkpoints, "aged", fake_aged)
    monkeypatch.setattr(railway_cli, "RailwayProvider", _provider_deleting(deleted.append))
    monkeypatch.setattr(railway_cli.task_checkpoints, "forget", lambda tid: forgotten.append(tid))

    result = runner.invoke(cli.app, ["railway", "prune-checkpoints"])

    assert result.exit_code == 0, result.output
    assert captured["ttl_seconds"] == 168 * 3600  # default TTL: 7 days
    assert deleted == ["task-t1", "task-t2"]
    # Bookkeeping is dropped too, so the next sweep doesn't retry a deleted name.
    assert forgotten == ["t1", "t2"]
    assert "Pruned 2 task checkpoint(s)." in result.output


def test_railway_prune_checkpoints_respects_custom_ttl(monkeypatch: pytest.MonkeyPatch):
    captured: dict = {}

    def fake_aged(ttl_seconds: float) -> list[str]:
        captured["ttl_seconds"] = ttl_seconds
        return []

    monkeypatch.setattr(railway_cli.task_checkpoints, "aged", fake_aged)
    monkeypatch.setattr(railway_cli, "RailwayProvider", _provider_deleting(lambda n: None))

    result = runner.invoke(cli.app, ["railway", "prune-checkpoints", "--ttl-hours", "24"])

    assert result.exit_code == 0, result.output
    assert captured["ttl_seconds"] == 24 * 3600
    assert "Pruned 0 task checkpoint(s)." in result.output


def test_railway_prune_checkpoints_deletes_none_when_nothing_aged(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(railway_cli.task_checkpoints, "aged", lambda ttl_seconds: [])
    deleted: list[str] = []
    monkeypatch.setattr(railway_cli, "RailwayProvider", _provider_deleting(deleted.append))

    result = runner.invoke(cli.app, ["railway", "prune-checkpoints"])

    assert result.exit_code == 0, result.output
    assert deleted == []


def test_railway_prune_checkpoints_survives_a_failed_delete(monkeypatch: pytest.MonkeyPatch):
    """A checkpoint that is already gone (or a transient CLI failure) must not
    kill the sweep — and the entry is still forgotten, so it isn't retried
    forever."""
    forgotten: list[str] = []

    def boom(name: str) -> None:
        raise RuntimeError(f"no such checkpoint: {name}")

    monkeypatch.setattr(railway_cli.task_checkpoints, "aged", lambda ttl_seconds: ["t1"])
    monkeypatch.setattr(railway_cli, "RailwayProvider", _provider_deleting(boom))
    monkeypatch.setattr(railway_cli.task_checkpoints, "forget", lambda tid: forgotten.append(tid))

    result = runner.invoke(cli.app, ["railway", "prune-checkpoints"])

    assert result.exit_code == 0, result.output
    assert forgotten == ["t1"]
