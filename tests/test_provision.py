"""Tests for provision: .issuebear.toml loading and the bootstrap step."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from pydantic import ValidationError

from issuebot import provision


def _write(folder: Path, body: str) -> None:
    (folder / ".issuebear.toml").write_text(body)


def test_load_bootstrap_absent_returns_none(tmp_path: Path):
    assert provision.load_bootstrap(str(tmp_path)) is None


def test_load_bootstrap_parses_all_sections(tmp_path: Path):
    _write(
        tmp_path,
        """
[bootstrap]
setup = ["uv sync", "npm ci"]

[bootstrap.env]
NODE_ENV = "test"

[[bootstrap.mcp]]
name = "chrome-devtools"
command = "npx"
args = ["-y", "chrome-devtools-mcp@latest"]

[bootstrap.plugins]
dirs = ["tools/agent-plugins/browser"]
""",
    )
    cfg = provision.load_bootstrap(str(tmp_path))
    assert cfg is not None
    assert cfg.setup == ["uv sync", "npm ci"]
    assert cfg.env == {"NODE_ENV": "test"}
    assert cfg.mcp[0].name == "chrome-devtools"
    assert cfg.mcp[0].command == "npx"
    assert cfg.plugins.dirs == ["tools/agent-plugins/browser"]


def test_load_bootstrap_empty_file_is_all_defaults(tmp_path: Path):
    _write(tmp_path, "")
    cfg = provision.load_bootstrap(str(tmp_path))
    assert cfg is not None
    assert cfg.setup == []
    assert cfg.env == {}
    assert cfg.mcp == []
    assert cfg.plugins.dirs == []


def test_load_bootstrap_malformed_toml_raises(tmp_path: Path):
    _write(tmp_path, "this is = = not toml")
    with pytest.raises(RuntimeError, match="not valid TOML"):
        provision.load_bootstrap(str(tmp_path))


def test_load_bootstrap_wrong_type_raises(tmp_path: Path):
    _write(tmp_path, '[bootstrap]\nsetup = "should be a list"\n')
    with pytest.raises(RuntimeError, match="invalid"):
        provision.load_bootstrap(str(tmp_path))


def test_mcp_server_http_fragment():
    s = provision.McpServer(
        name="board", type="http", url="https://x/mcp", headers={"Authorization": "Bearer t"}
    )
    assert s.to_fragment() == {
        "board": {"type": "http", "url": "https://x/mcp", "headers": {"Authorization": "Bearer t"}}
    }


def test_mcp_server_url_only_is_http_fragment():
    s = provision.McpServer(name="board", url="https://x/mcp")
    assert s.to_fragment() == {"board": {"type": "http", "url": "https://x/mcp", "headers": {}}}


def test_mcp_server_stdio_fragment():
    s = provision.McpServer(name="cd", command="npx", args=["-y", "pkg"])
    assert s.to_fragment() == {"cd": {"command": "npx", "args": ["-y", "pkg"]}}


def test_mcp_server_http_without_url_rejected():
    with pytest.raises(ValidationError):
        provision.McpServer(name="board", type="http")


def test_mcp_server_stdio_without_command_rejected():
    with pytest.raises(ValidationError):
        provision.McpServer(name="cd")


class FakeRunner:
    """Records (argv, cwd, env) and returns scripted CompletedProcess results.

    `git rev-parse --absolute-git-dir` is answered from `git_dir` (None → a
    non-zero exit, exercising the XDG fallback). Every other call returns
    `setup_rc` with `setup_out`/`setup_err`."""

    def __init__(
        self, git_dir: str | None, *, setup_rc: int = 0, setup_out: str = "", setup_err: str = ""
    ):
        self.git_dir = git_dir
        self.setup_rc = setup_rc
        self.setup_out = setup_out
        self.setup_err = setup_err
        self.calls: list[tuple[list[str], str, dict | None]] = []

    def __call__(self, argv, cwd, env=None) -> subprocess.CompletedProcess:
        self.calls.append((argv, cwd, env))
        if argv[:2] == ["git", "rev-parse"]:
            if self.git_dir is None:
                return subprocess.CompletedProcess(argv, 128, "", "not a git repo")
            return subprocess.CompletedProcess(argv, 0, self.git_dir + "\n", "")
        return subprocess.CompletedProcess(argv, self.setup_rc, self.setup_out, self.setup_err)


class NullReporter:
    def start(self, ref, folder):
        pass

    def event(self, ev):
        pass

    def raw(self, line):
        pass

    def finish(self, status, elapsed):
        pass


def _git_dir(tmp_path: Path) -> str:
    d = tmp_path / ".git"
    d.mkdir()
    return str(d)


def test_provision_absent_file_returns_empty(tmp_path: Path):
    run = FakeRunner(_git_dir(tmp_path))
    res = provision.provision(str(tmp_path), reporter=NullReporter(), run=run)
    assert res == provision.ProvisionResult()
    assert run.calls == []  # never touches git when there is no file


def test_provision_runs_setup_when_marker_absent(tmp_path: Path):
    gd = _git_dir(tmp_path)
    _write(tmp_path, '[bootstrap]\nsetup = ["uv sync"]\n')
    run = FakeRunner(gd)
    provision.provision(str(tmp_path), reporter=NullReporter(), run=run)
    setup_calls = [c for c in run.calls if c[0][:2] != ["git", "rev-parse"]]
    assert setup_calls and setup_calls[0][0] == ["sh", "-c", "uv sync"]
    assert (Path(gd) / "issuebot-bootstrap.json").exists()


def test_provision_skips_setup_when_hash_unchanged(tmp_path: Path):
    gd = _git_dir(tmp_path)
    _write(tmp_path, '[bootstrap]\nsetup = ["uv sync"]\n')
    provision.provision(str(tmp_path), reporter=NullReporter(), run=FakeRunner(gd))
    run2 = FakeRunner(gd)
    provision.provision(str(tmp_path), reporter=NullReporter(), run=run2)
    assert [c for c in run2.calls if c[0][:2] != ["git", "rev-parse"]] == []


def test_provision_reruns_setup_when_config_changes(tmp_path: Path):
    gd = _git_dir(tmp_path)
    _write(tmp_path, '[bootstrap]\nsetup = ["uv sync"]\n')
    provision.provision(str(tmp_path), reporter=NullReporter(), run=FakeRunner(gd))
    _write(tmp_path, '[bootstrap]\nsetup = ["uv sync", "npm ci"]\n')
    run2 = FakeRunner(gd)
    provision.provision(str(tmp_path), reporter=NullReporter(), run=run2)
    setup_calls = [c for c in run2.calls if c[0][:2] != ["git", "rev-parse"]]
    assert len(setup_calls) == 2


def test_provision_setup_failure_raises(tmp_path: Path):
    gd = _git_dir(tmp_path)
    _write(tmp_path, '[bootstrap]\nsetup = ["false"]\n')
    run = FakeRunner(gd, setup_rc=1, setup_err="boom")
    with pytest.raises(RuntimeError, match="setup command failed"):
        provision.provision(str(tmp_path), reporter=NullReporter(), run=run)
    assert not (Path(gd) / "issuebot-bootstrap.json").exists()  # marker only on success


def test_provision_setup_runs_with_declared_env(tmp_path: Path):
    gd = _git_dir(tmp_path)
    _write(tmp_path, '[bootstrap]\nsetup = ["env"]\n[bootstrap.env]\nFOO = "bar"\n')
    run = FakeRunner(gd)
    provision.provision(str(tmp_path), reporter=NullReporter(), run=run)
    setup_call = [c for c in run.calls if c[0][:2] != ["git", "rev-parse"]][0]
    assert setup_call[2] == {"FOO": "bar"}


def test_provision_returns_env_mcp_plugins_even_when_setup_skipped(tmp_path: Path):
    gd = _git_dir(tmp_path)
    _write(
        tmp_path,
        """
[bootstrap]
setup = []
[bootstrap.env]
A = "1"
[[bootstrap.mcp]]
name = "cd"
command = "npx"
args = ["pkg"]
[bootstrap.plugins]
dirs = ["sub/plug"]
""",
    )
    res = provision.provision(str(tmp_path), reporter=NullReporter(), run=FakeRunner(gd))
    assert res.env == {"A": "1"}
    assert res.mcp_servers == [{"cd": {"command": "npx", "args": ["pkg"]}}]
    assert res.plugin_dirs == [str((tmp_path / "sub/plug").resolve())]


def test_provision_marker_falls_back_to_state_dir_when_not_git(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    _write(tmp_path, '[bootstrap]\nsetup = ["uv sync"]\n')
    run = FakeRunner(None)  # git rev-parse fails
    provision.provision(str(tmp_path), reporter=NullReporter(), run=run)
    markers = list((tmp_path / "state" / "issuebot" / "bootstrap").glob("*.json"))
    assert len(markers) == 1
