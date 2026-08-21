"""Tests for issuebot.install: persist and retrieve the Parade-minted install id."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from conftest import config
from issuebot import install

# ---------------------------------------------------------------------------
# Store: load_install_id / save_install_id
# ---------------------------------------------------------------------------


def test_install_id_round_trips(tmp_path: Path) -> None:
    """save_install_id persists the id; load_install_id reads it back."""
    p = tmp_path / "install_id"

    # Not yet registered — should return None.
    assert install.load_install_id(p) is None

    install.save_install_id(p, "abc-123")
    assert install.load_install_id(p) == "abc-123"


def test_load_returns_none_when_file_absent(tmp_path: Path) -> None:
    """load_install_id returns None when the path does not exist."""
    p = tmp_path / "nested" / "install_id"
    assert install.load_install_id(p) is None


def test_save_creates_parent_dirs(tmp_path: Path) -> None:
    """save_install_id creates intermediate directories."""
    p = tmp_path / "a" / "b" / "install_id"
    install.save_install_id(p, "srv-42")
    assert p.read_text(encoding="utf-8") == "srv-42"


def test_default_install_path_uses_xdg_state_home(tmp_path: Path, monkeypatch) -> None:
    """default_install_path() honours $XDG_STATE_HOME when set."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    p = install.default_install_path()
    assert p == tmp_path / "issuebot" / "install_id"


# ---------------------------------------------------------------------------
# Supervisor: register-or-reuse flow
# ---------------------------------------------------------------------------


class _RegisteringApi:
    """Minimal fake API that records register_install calls.

    All other methods return harmless stubs so the Supervisor's daemon threads
    can start without crashing.
    """

    def __init__(self, install_id: str = "srv-reg-1") -> None:
        self._install_id = install_id
        self.register_calls: list[tuple[str | None, str | None]] = []

    def register_install(self, hostname: str | None) -> str:
        """Record the call and return a fixed minted id."""
        self.register_calls.append(hostname)
        return self._install_id

    def connect(
        self, board_id: str, name: str | None = None, install_id: str | None = None
    ) -> dict[str, Any]:
        return {}

    def disconnect(self, board_id: str) -> None:
        pass

    def get_tasks(self, *, board_id: str | None = None, wait: int = 0) -> list:
        return []

    def get_mentions(self, *, board_id: str | None = None, wait: int = 0) -> list:
        return []

    def wait_for_commands(self, *, install_id: str | None = None, timeout: int = 25) -> list:
        return []

    def ack_command(self, command_id: str, *, status: str, result: str | None = None) -> None:
        pass

    def report_telemetry(self, **kwargs: Any) -> None:
        pass


def test_supervisor_registers_install_on_first_start(tmp_path: Path) -> None:
    """First start() with no persisted id calls register_install once and persists it."""
    from issuebot.config import save_config
    from issuebot.plugins.harnesses.fake.harness import FakeHarness
    from issuebot.runner import Supervisor

    cfg_path = tmp_path / "config.toml"
    install_path = tmp_path / "install_id"

    cfg = config(connections=[])
    save_config(cfg, cfg_path)

    api = _RegisteringApi(install_id="srv-fresh")
    sup = Supervisor(
        api,
        FakeHarness(0),
        cfg_path,
        poll_interval=0.05,
        install_path=install_path,
    )
    sup.start()
    try:
        # Give the start() logic time to complete.
        import time

        time.sleep(0.1)

        assert len(api.register_calls) == 1
        assert install.load_install_id(install_path) == "srv-fresh"
        assert sup._install_id == "srv-fresh"
    finally:
        sup.stop()


def test_supervisor_reuses_install_id_on_second_start(tmp_path: Path) -> None:
    """Second start() with a persisted id reuses it (no second register call)."""
    from issuebot.config import save_config
    from issuebot.plugins.harnesses.fake.harness import FakeHarness
    from issuebot.runner import Supervisor

    cfg_path = tmp_path / "config.toml"
    install_path = tmp_path / "install_id"

    # Pre-seed the install id as if a prior run had registered it.
    install.save_install_id(install_path, "srv-existing")

    cfg = config(connections=[])
    save_config(cfg, cfg_path)

    api = _RegisteringApi(install_id="srv-new")
    sup = Supervisor(
        api,
        FakeHarness(0),
        cfg_path,
        poll_interval=0.05,
        install_path=install_path,
    )
    sup.start()
    try:
        import time

        time.sleep(0.1)

        # register_install must NOT have been called — id was already persisted.
        assert api.register_calls == []
        assert sup._install_id == "srv-existing"
    finally:
        sup.stop()


def test_supervisor_survives_registration_failure(tmp_path: Path) -> None:
    """A failing register_install call is logged but does not crash start()."""
    from issuebot.config import save_config
    from issuebot.plugins.harnesses.fake.harness import FakeHarness
    from issuebot.runner import Supervisor

    cfg_path = tmp_path / "config.toml"
    install_path = tmp_path / "install_id"

    cfg = config(connections=[])
    save_config(cfg, cfg_path)

    class FailingApi(_RegisteringApi):
        def register_install(self, hostname: str | None) -> str:
            self.register_calls.append(hostname)
            raise RuntimeError("network error")

    api = FailingApi()
    sup = Supervisor(
        api,
        FakeHarness(0),
        cfg_path,
        poll_interval=0.05,
        install_path=install_path,
    )
    # Must not raise.
    sup.start()
    try:
        import time

        time.sleep(0.1)

        # install_id is None; no file written.
        assert sup._install_id is None
        assert install.load_install_id(install_path) is None
    finally:
        sup.stop()
