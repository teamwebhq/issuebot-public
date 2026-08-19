"""Apply a repo's `.issuebear.toml` workspace bootstrap before the agent launches.

The file is repo-authored and committed at the repo root. It declares setup
commands (run once per workspace, re-run when the [bootstrap] table changes), env
vars, extra MCP servers, and extra plugin dirs. Pure functions over an injected
command runner, mirroring the git workspace plugin; an absent file is a no-op. No new trust
surface: the agent already runs with `--dangerously-skip-permissions` in this
same workspace, so setup runs at the trust level the agent already has."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tomllib
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import BaseModel, Field, ValidationError

from issuebot.contracts import McpServer
from issuebot.reporter import Reporter
from issuebot.state import StateFile, state_dir

# A runner executes argv in a working directory (optionally with extra env) and
# returns the completed process. The default shells out to the real command.
Runner = Callable[..., "subprocess.CompletedProcess[str]"]

# The bootstrap declaration a repo carries. Public because the git workspace
# hashes it too, deciding whether a warm workspace needs provisioning again —
# one spelling, so the file that triggers a re-provision cannot drift from the
# file that is read.
FILENAME = ".issuebear.toml"
_MARKER = "issuebot-bootstrap.json"


def _run(
    args: list[str], cwd: str, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    full = {**os.environ, **(env or {})}
    try:
        return subprocess.run(args, cwd=cwd, text=True, capture_output=True, env=full)
    except (FileNotFoundError, NotADirectoryError) as exc:
        return subprocess.CompletedProcess(args, returncode=127, stdout="", stderr=str(exc))


class Plugins(BaseModel):
    dirs: list[str] = Field(default_factory=list)


class BootstrapConfig(BaseModel):
    setup: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    mcp: list[McpServer] = Field(default_factory=list)
    plugins: Plugins = Field(default_factory=Plugins)


@dataclass(frozen=True)
class ProvisionResult:
    env: dict[str, str] = field(default_factory=dict)
    mcp_servers: list[dict] = field(default_factory=list)
    plugin_dirs: list[str] = field(default_factory=list)


def load_bootstrap(folder: str) -> BootstrapConfig | None:
    """Read and validate `<folder>/.issuebear.toml`. None when the file is absent;
    raises RuntimeError on malformed TOML or a schema violation."""
    path = Path(folder) / FILENAME
    if not path.exists():
        return None
    try:
        with path.open("rb") as fh:
            data = tomllib.load(fh)
    except tomllib.TOMLDecodeError as exc:
        raise RuntimeError(f"{FILENAME} is not valid TOML: {exc}") from exc
    try:
        return BootstrapConfig.model_validate(data.get("bootstrap", {}))
    except ValidationError as exc:
        raise RuntimeError(f"{FILENAME} is invalid: {exc}") from exc


def _hash(cfg: BootstrapConfig) -> str:
    """A stable hash of the bootstrap config; the marker stores it so setup re-runs
    only when the [bootstrap] table actually changes."""
    return hashlib.sha256(json.dumps(cfg.model_dump(), sort_keys=True).encode()).hexdigest()


def _marker_path(folder: str, run: Runner) -> Path:
    """`<git-dir>/issuebot-bootstrap.json` for a git workspace (per-clone and
    per-worktree, and never in the working tree), else a state path keyed by the
    folder path for a non-git folder."""
    r = run(["git", "rev-parse", "--absolute-git-dir"], folder)
    if r.returncode == 0 and r.stdout.strip():
        return Path(r.stdout.strip()) / _MARKER
    digest = hashlib.sha256(str(Path(folder).resolve()).encode()).hexdigest()[:16]
    return state_dir() / "bootstrap" / f"{digest}.json"


def _marker_hash(marker: Path) -> str | None:
    """The hash the last successful bootstrap recorded, or None if it never ran."""
    stored = StateFile(marker).read_json().get("hash")
    return stored if isinstance(stored, str) else None


def _write_marker(marker: Path, hash_: str) -> None:
    """Record that this bootstrap config has been applied to this workspace."""
    StateFile(marker).write_json({"hash": hash_})


def _run_setup(cfg: BootstrapConfig, folder: str, reporter: Reporter, run: Runner) -> None:
    """Run each setup command in order via `sh -c`, with the declared env, in the
    workspace. Output is surfaced to the reporter; a non-zero exit raises."""
    for cmd in cfg.setup:
        reporter.raw(f"issuebot bootstrap: {cmd}")
        r = run(["sh", "-c", cmd], folder, env=cfg.env)
        if r.stdout:
            reporter.raw(r.stdout.rstrip())
        if r.returncode != 0:
            detail = r.stderr.strip() or r.stdout.strip()
            raise RuntimeError(f"setup command failed (exit {r.returncode}): {cmd}\n{detail}")


def provision(folder: str, *, reporter: Reporter, run: Runner = _run) -> ProvisionResult:
    """Apply `<folder>/.issuebear.toml`. Runs setup when the marker is absent or its
    hash differs from the current [bootstrap] table, then returns the env / MCP /
    plugin data to thread into the launch. No-op (empty result) when the file is
    absent. Raises RuntimeError on a malformed file or a failed setup command."""
    cfg = load_bootstrap(folder)
    if cfg is None:
        return ProvisionResult()

    if cfg.setup:
        marker = _marker_path(folder, run)
        want = _hash(cfg)
        if _marker_hash(marker) != want:
            _run_setup(cfg, folder, reporter, run)
            _write_marker(marker, want)

    plugin_dirs = [str((Path(folder) / d).resolve()) for d in cfg.plugins.dirs]
    mcp_servers = [s.to_fragment() for s in cfg.mcp]
    return ProvisionResult(env=dict(cfg.env), mcp_servers=mcp_servers, plugin_dirs=plugin_dirs)
