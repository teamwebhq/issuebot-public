"""Tests for the local runner status mirror (issuebot status)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from issuebot.agent_state import ConnectionSnapshot
from issuebot.config import Connection
from issuebot.status import (
    StatusStore,
    build_payload,
    default_status_path,
    is_stale,
    render_status,
    status_age,
)

_NOW = datetime(2026, 6, 29, 20, 0, 0, tzinfo=UTC)


def _conn(name: str, board: str, folder: str = "/work/repo") -> Connection:
    return Connection(name=name, board=board, folder=folder)


def _snap(name: str, phase: str = "idle", ref: str | None = None) -> ConnectionSnapshot:
    return ConnectionSnapshot(name=name, board="b1", target="/work/repo", phase=phase, ref=ref)


def _fresh_payload(connections: list[ConnectionSnapshot], *, updated: datetime = _NOW) -> dict:
    return build_payload(connections, version="0.1.0", interval=15.0, now=updated, pid=4242)


# --- StatusStore -------------------------------------------------------------


def test_store_roundtrip(tmp_path: Path):
    store = StatusStore(tmp_path / "status.json")
    payload = _fresh_payload([_snap("p1", "working", "ISS-1")])
    store.write(payload)
    assert store.read() == payload


def test_store_missing_reads_none(tmp_path: Path):
    assert StatusStore(tmp_path / "absent.json").read() is None


def test_store_corrupt_reads_none(tmp_path: Path):
    path = tmp_path / "status.json"
    path.write_text("{not json", encoding="utf-8")
    assert StatusStore(path).read() is None


def test_store_write_is_atomic_no_tmp_left(tmp_path: Path):
    store = StatusStore(tmp_path / "status.json")
    store.write(_fresh_payload([]))
    # The temp file used for the atomic replace must not linger.
    assert list(tmp_path.glob("*.tmp")) == []


def test_store_clear(tmp_path: Path):
    path = tmp_path / "status.json"
    store = StatusStore(path)
    store.write(_fresh_payload([]))
    store.clear()
    assert not path.exists()
    store.clear()  # idempotent, no raise


def test_default_status_path_honours_xdg(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    assert default_status_path() == tmp_path / "issuebot" / "status.json"


# --- build_payload -----------------------------------------------------------


def test_payload_keeps_identity_half_of_each_snapshot():
    """The file carries {name, board, target, phase, ref} per connection — the
    log tail and links stay out: `issuebot status` does not show them."""
    payload = _fresh_payload(
        [
            ConnectionSnapshot(
                name="p1",
                board="b1",
                target="/work/repo",
                phase="working",
                ref="ISS-1",
                log_tail="secret\nlines",
                links=[{"branch": "issuebot/ISS-1"}],
            )
        ]
    )
    assert payload["connections"] == [
        {"name": "p1", "board": "b1", "target": "/work/repo", "phase": "working", "ref": "ISS-1"}
    ]


# --- freshness ---------------------------------------------------------------


def test_status_age(tmp_path: Path):
    payload = _fresh_payload([], updated=_NOW - timedelta(seconds=12))
    assert status_age(payload, now=_NOW) == 12.0


def test_fresh_payload_not_stale():
    payload = _fresh_payload([])
    assert is_stale(payload, now=_NOW) is False


def test_old_payload_is_stale():
    payload = _fresh_payload([], updated=_NOW - timedelta(minutes=5))
    assert is_stale(payload, now=_NOW) is True


def test_missing_timestamp_is_stale():
    assert is_stale({"connections": []}, now=_NOW) is True


# --- render_status -----------------------------------------------------------


def test_render_no_status_file_lists_connections():
    out = render_status([_conn("p1", "b1")], None, now=_NOW)
    assert "no status file" in out
    assert "p1" in out and "b1" in out


def test_render_stale_reports_stale():
    payload = _fresh_payload(
        [_snap("p1", "working", "ISS-1")],
        updated=_NOW - timedelta(minutes=10),
    )
    out = render_status([_conn("p1", "b1")], payload, now=_NOW)
    assert "stale" in out
    # A stale runtime must not masquerade as a live phase.
    assert "working" not in out


def test_render_active_shows_phase_and_ref():
    payload = _fresh_payload(
        [
            _snap("p1", "working", "ISS-42"),
            _snap("p2", "waiting", None),
        ]
    )
    out = render_status([_conn("p1", "b1"), _conn("p2", "b2")], payload, now=_NOW)
    assert "active" in out
    assert "pid 4242" in out
    assert "working" in out and "ISS-42" in out
    # p2 is idle/waiting with no ref → an em-dash placeholder, not a crash.
    assert "waiting" in out


def test_render_connection_without_runtime_shows_placeholder():
    # A configured connection with no live entry (runner hasn't started its
    # listener yet) still appears, with placeholders rather than stale data.
    payload = _fresh_payload([_snap("p1", "working", "ISS-1")])
    out = render_status([_conn("p1", "b1"), _conn("p2", "b2")], payload, now=_NOW)
    assert "p2" in out


def test_render_resolve_log_appends_path():
    payload = _fresh_payload([_snap("p1", "working", "ISS-42")])
    out = render_status(
        [_conn("p1", "b1")],
        payload,
        now=_NOW,
        resolve_log=lambda ref: f"/logs/{ref}-x.jsonl",
    )
    assert "/logs/ISS-42-x.jsonl" in out


def test_render_no_connections():
    out = render_status([], _fresh_payload([]), now=_NOW)
    assert "No connections configured" in out


# --- round trip: snapshot → file → issuebot status ---------------------------


def test_snapshot_round_trips_through_file_to_render(tmp_path: Path):
    """What the runner writes is what `issuebot status` shows offline: a
    snapshot goes in, and the connection's phase and ref come out on screen."""
    store = StatusStore(tmp_path / "status.json")
    store.write(_fresh_payload([_snap("p1", "working", "ISS-42")]))

    out = render_status([_conn("p1", "b1")], store.read(), now=_NOW)

    assert "active" in out
    assert "working" in out and "ISS-42" in out
