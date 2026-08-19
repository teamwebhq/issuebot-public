import logging
import re

from issuebot.agent_state import AgentState, LogTailHandler


def test_phase_and_links_round_trip():
    st = AgentState()
    assert st.snapshot().phase == "idle"  # default
    st.set_phase("working")
    st.set_links([{"branch": "issuebot/ISS-1"}])
    snap = st.snapshot()
    assert snap.phase == "working"
    assert snap.links == [{"branch": "issuebot/ISS-1"}]
    st.clear_links()
    assert st.snapshot().links == []


def test_snapshot_stamps_identity():
    """The one caller with an identity (the listener) stamps it onto the
    snapshot; observers of the live half alone leave it empty."""
    st = AgentState()
    st.set_phase("working", "ISS-7")
    snap = st.snapshot(name="p", board="b-1", target="/work/repo")
    assert (snap.name, snap.board, snap.target) == ("p", "b-1", "/work/repo")
    assert (snap.phase, snap.ref) == ("working", "ISS-7")


def test_log_tail_bounds_to_200_lines():
    st = AgentState()
    for i in range(250):
        st.append_log(f"line{i}", kind="tool")
    tail = st.snapshot().log_tail
    lines = tail.split("\n")
    assert len(lines) == 200
    # Each entry is tab-delimited: <iso-utc>\t<kind>\t<text>.
    first = lines[0].split("\t")
    assert first[1] == "tool"
    assert first[2] == "line50"  # oldest 50 dropped
    assert lines[-1].split("\t")[2] == "line249"


def test_log_tail_handler_appends_records():
    st = AgentState()
    logger = logging.getLogger("test-issuebot-tail")
    logger.handlers.clear()
    handler = LogTailHandler(st)
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)

    logger.info("hello tail")
    assert "hello tail" in st.snapshot().log_tail


def test_log_lines_have_distinct_microsecond_timestamps():
    """Two append_log calls in rapid succession must produce distinct iso timestamps.

    datetime.now(UTC).isoformat() carries sub-second precision — if the
    implementation ever regresses to timespec="seconds" the fractional part
    disappears, which this test catches by asserting each timestamp's fraction
    has microsecond (6-digit) resolution. (Asserting two now() calls *differ*
    would be probabilistically flaky, so we assert the format instead.)
    """
    st = AgentState()
    st.append_log("line1")
    st.append_log("line2")
    tail = st.snapshot().log_tail
    lines = tail.strip().split("\n")
    ts1 = lines[0].split("\t")[0]
    ts2 = lines[1].split("\t")[0]
    # Each timestamp must carry a microsecond fraction (e.g. 12:00:00.123456+00:00).
    micro = re.compile(r"\.\d{6}")
    assert micro.search(ts1), f"timestamp lacks microsecond precision: {ts1}"
    assert micro.search(ts2), f"timestamp lacks microsecond precision: {ts2}"
