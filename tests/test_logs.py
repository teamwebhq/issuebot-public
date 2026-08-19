"""Tests for the per-run log reader/renderer (issuebot logs)."""

from __future__ import annotations

import io
import threading
from pathlib import Path

from issuebot import logs as logs_mod
from issuebot.events import AgentEvent, raw_event

# A run log holds whatever its harness printed, and this module never learns any
# harness's wire format — it is handed a `parse`. So the lines below are made up,
# and `_parse` is the stand-in for a harness that understands them: enough shape
# to cover the four kinds the renderer treats differently. What a real harness's
# own parser does with its own format is tested beside that plugin.
_TOOL = "tool Edit: src/foo.py"
_TEXT = "text thinking it through"
_RESULT = "result all done"
_INIT = "init sess-1"


def _parse(line: str) -> AgentEvent | None:
    """Read one of the made-up lines above, as a harness would read its own."""
    kind, _, rest = line.strip().partition(" ")
    if kind not in ("tool", "text", "result", "init"):
        return None
    return AgentEvent(
        "tool_use" if kind == "tool" else kind,  # type: ignore[arg-type]
        rest,
        detail=rest,
        session_id=rest if kind == "init" else None,
    )


def _write_run(log_dir: Path, name: str, lines: list[str]) -> Path:
    log_dir.mkdir(parents=True, exist_ok=True)
    path = log_dir / name
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


# --- run discovery -----------------------------------------------------------


def test_list_runs_parses_ref_with_dash_newest_first(tmp_path: Path):
    _write_run(tmp_path, "ISS-42-20260629-200000.jsonl", ["{}"])
    _write_run(tmp_path, "ISS-42-20260629-210000.jsonl", ["{}"])
    _write_run(tmp_path, "ISS-7-20260628-120000.jsonl", ["{}"])

    runs = logs_mod.list_runs(tmp_path)
    assert [r.ref for r in runs] == ["ISS-42", "ISS-42", "ISS-7"]
    # Newest first: 21:00 before 20:00 for the same ref.
    assert runs[0].started == "20260629-210000"
    assert runs[1].started == "20260629-200000"


def test_list_runs_ignores_non_log_files(tmp_path: Path):
    _write_run(tmp_path, "ISS-1-20260629-200000.jsonl", ["{}"])
    (tmp_path / "notes.txt").write_text("hi", encoding="utf-8")
    (tmp_path / "weird.jsonl").write_text("{}", encoding="utf-8")  # no timestamp
    runs = logs_mod.list_runs(tmp_path)
    assert [r.ref for r in runs] == ["ISS-1"]


def test_list_runs_missing_dir_is_empty(tmp_path: Path):
    assert logs_mod.list_runs(tmp_path / "nope") == []


def test_latest_run_and_for_ref(tmp_path: Path):
    _write_run(tmp_path, "ISS-1-20260629-200000.jsonl", ["{}"])
    _write_run(tmp_path, "ISS-2-20260629-210000.jsonl", ["{}"])
    latest = logs_mod.latest_run(tmp_path)
    assert latest is not None and latest.ref == "ISS-2"
    for_ref = logs_mod.latest_run_for_ref("ISS-1", tmp_path)
    assert for_ref is not None and for_ref.started == "20260629-200000"
    assert logs_mod.latest_run_for_ref("ISS-9", tmp_path) is None


# --- rendering ---------------------------------------------------------------


def test_render_concise_skips_init_and_uses_summaries():
    out = logs_mod.render_lines([_INIT, _TOOL, _TEXT, _RESULT, "", "  "], raw=False, parse=_parse)
    text = "\n".join(out)
    assert "Edit: src/foo.py" in text
    assert "thinking it through" in text
    assert "all done" in text
    # The init/session line carries nothing feed-worthy and is dropped.
    assert "sess-1" not in text
    assert len(out) == 3


def test_render_raw_passes_lines_through():
    out = logs_mod.render_lines([_TOOL, "", _RESULT], raw=True)
    assert out == [_TOOL, _RESULT]


def test_render_raw_handles_a_line_no_parser_claims():
    out = logs_mod.render_lines(["plain text line"], raw=True)
    assert out == ["plain text line"]


def test_a_line_nobody_parses_keeps_its_whole_text():
    """`raw_event` is the ABC's default reading and this module's default
    `parse`, so it is core's — and it has to keep the full line in `detail`
    while the summary is only the first, truncated line, or a plain harness's
    output arrives at the reporter already lossy."""
    ev = raw_event("  one\ntwo  ")

    assert ev is not None
    assert ev.kind == "raw"
    assert ev.summary == "one"
    assert ev.detail == "  one\ntwo  "
    assert raw_event("   ") is None


def test_render_without_a_parser_shows_every_line_verbatim():
    """`issuebot logs` runs before `init`, and on a build whose harness is gone.

    With nobody to say what a line means, the default reading is that each one
    is opaque text — so the run is still readable rather than blank."""
    out = logs_mod.render_lines([_TOOL, "", _RESULT], raw=False)

    assert [line for line in out if _TOOL in line]
    assert [line for line in out if _RESULT in line]
    assert len(out) == 2  # the blank line carries nothing


def test_read_lines_missing(tmp_path: Path):
    assert logs_mod.read_lines(tmp_path / "absent.jsonl") == []


def test_tail():
    assert logs_mod.tail(["a", "b", "c"], 2) == ["b", "c"]
    assert logs_mod.tail(["a", "b", "c"], 0) == ["a", "b", "c"]
    assert logs_mod.tail(["a", "b", "c"], None) == ["a", "b", "c"]


# --- follow ------------------------------------------------------------------


def test_drain_new_emits_only_appended(tmp_path: Path):
    path = _write_run(tmp_path, "ISS-1-20260629-200000.jsonl", [_TOOL, _TEXT, _RESULT])
    out = io.StringIO()
    seen = logs_mod.drain_new(path, 2, out=out, raw=False, parse=_parse)
    assert seen == 3
    assert "all done" in out.getvalue()
    # The first two lines were already shown — only line 3 is emitted.
    assert "Edit: src/foo.py" not in out.getvalue()


def test_follow_prints_tail_then_stops_when_set(tmp_path: Path):
    path = _write_run(tmp_path, "ISS-1-20260629-200000.jsonl", [_TOOL, _RESULT])
    out = io.StringIO()
    stop = threading.Event()
    stop.set()  # already stopped → render the tail, then return without polling
    seen = logs_mod.follow_log(path, out=out, raw=False, n=10, stop=stop, poll=0.01, parse=_parse)
    assert seen == 2
    assert "Edit: src/foo.py" in out.getvalue()
    assert "all done" in out.getvalue()


def test_follow_respects_tail_count(tmp_path: Path):
    path = _write_run(tmp_path, "ISS-1-20260629-200000.jsonl", [_TOOL, _TEXT, _RESULT])
    out = io.StringIO()
    stop = threading.Event()
    stop.set()
    logs_mod.follow_log(path, out=out, raw=True, n=1, stop=stop, poll=0.01)
    # Only the final line is in the initial tail.
    assert out.getvalue().strip() == _RESULT


# --- active run selection ----------------------------------------------------


def test_active_run_prefers_working_connection(tmp_path: Path):
    _write_run(tmp_path, "ISS-1-20260629-200000.jsonl", ["{}"])
    _write_run(tmp_path, "ISS-2-20260629-210000.jsonl", ["{}"])  # newer on disk
    payload = {"connections": [{"name": "p1", "phase": "working", "ref": "ISS-1"}]}
    run = logs_mod.active_run(tmp_path, payload, is_fresh=lambda p: True)
    assert run is not None
    assert run.ref == "ISS-1"  # the working ref wins over the newer file


def test_active_run_falls_back_to_latest_when_stale(tmp_path: Path):
    _write_run(tmp_path, "ISS-1-20260629-200000.jsonl", ["{}"])
    _write_run(tmp_path, "ISS-2-20260629-210000.jsonl", ["{}"])
    payload = {"connections": [{"name": "p1", "phase": "working", "ref": "ISS-1"}]}
    run = logs_mod.active_run(tmp_path, payload, is_fresh=lambda p: False)
    assert run is not None
    assert run.ref == "ISS-2"


def test_active_run_no_payload_uses_latest(tmp_path: Path):
    _write_run(tmp_path, "ISS-2-20260629-210000.jsonl", ["{}"])
    run = logs_mod.active_run(tmp_path, None, is_fresh=lambda p: True)
    assert run is not None
    assert run.ref == "ISS-2"
