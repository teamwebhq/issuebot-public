"""Fake-harness-specific behaviour beyond the shared conformance suite: the
scripting knobs (on_launch, lines, session_id, summary) tests build on."""

from __future__ import annotations

from pathlib import Path

from issuebot.contracts import Answer, parse_outputs
from issuebot.plugins.harnesses.base import LaunchResult, LaunchSpec
from issuebot.plugins.harnesses.fake.harness import FakeHarness, write_response
from issuebot.run import RESPONSE_ENV


def _spec(**overrides) -> LaunchSpec:
    return LaunchSpec(
        prompt=overrides.get("prompt", "do the thing"),
        folder=overrides.get("folder", "/work/alpha"),
        env=overrides.get("env", {}),
    )


def test_fake_harness_records_spec_and_returns_exit_code(reporter):
    harness = FakeHarness(exit_code=3)
    spec = _spec()

    result = harness.launch(spec, reporter)

    assert isinstance(result, LaunchResult)
    assert result.exit_code == 3
    assert harness.calls == [spec]


def test_fake_harness_default_exit_code_is_zero(reporter):
    harness = FakeHarness()

    result = harness.launch(_spec(), reporter)

    assert result.exit_code == 0


def test_fake_harness_feeds_lines_to_reporter(reporter):
    harness = FakeHarness(exit_code=0, lines=["hello", "world"])

    result = harness.launch(_spec(), reporter)

    assert result.exit_code == 0
    assert reporter.raw_lines == ["hello", "world"]
    # Non-JSON lines surface as raw events so nothing is lost.
    assert [ev.summary for ev in reporter.events] == ["hello", "world"]


def test_fake_harness_on_launch_callback_fires(reporter):
    seen: list[LaunchSpec] = []
    harness = FakeHarness(on_launch=seen.append)
    spec = _spec()

    harness.launch(spec, reporter)

    assert seen == [spec]


def test_fake_harness_returns_configured_session_id(reporter):
    harness = FakeHarness(exit_code=0, session_id="sess-fake")
    result = harness.launch(_spec(), reporter)
    assert result.session_id == "sess-fake"
    assert result.exit_code == 0


def test_fake_harness_summarize_records_and_returns():
    harness = FakeHarness(summary="Title line\nThe body")
    out = harness.summarize("diff text", context="ctx", model="m", folder="/tmp/p")
    assert out == "Title line\nThe body"
    assert harness.summarize_calls == [("diff text", "ctx", "m", "/tmp/p")]


# ---------------------------------------------------------------------------
# The response document
# ---------------------------------------------------------------------------


def test_fake_harness_writes_an_empty_response_document_by_default(tmp_path, reporter):
    """No `outputs` given means the agent deliberately reported nothing — a
    valid, parseable document, not a missing one."""
    path = tmp_path / "response.json"
    harness = FakeHarness()

    harness.launch(_spec(env={RESPONSE_ENV: str(path)}), reporter)

    assert parse_outputs(path.read_text()) == []


def test_fake_harness_writes_the_given_outputs(tmp_path, reporter):
    path = tmp_path / "response.json"
    harness = FakeHarness(outputs=[Answer(text="the answer")])

    harness.launch(_spec(env={RESPONSE_ENV: str(path)}), reporter)

    assert [o.kind for o in parse_outputs(path.read_text())] == ["answer"]


def test_fake_harness_can_suppress_writing_a_response(tmp_path, reporter):
    """Simulates an agent that never finished: nothing is written at all."""
    path = tmp_path / "response.json"
    harness = FakeHarness(writes_response=False)

    harness.launch(_spec(env={RESPONSE_ENV: str(path)}), reporter)

    assert not path.exists()


def test_fake_harness_can_write_a_malformed_response(tmp_path, reporter):
    path = tmp_path / "response.json"
    harness = FakeHarness(response_raw="not json")

    harness.launch(_spec(env={RESPONSE_ENV: str(path)}), reporter)

    assert path.read_text() == "not json"


def test_fake_harness_skips_writing_without_an_env_entry(reporter):
    """A bare LaunchSpec (no RESPONSE_ENV set) is left alone — nothing to write to."""
    harness = FakeHarness()
    harness.launch(_spec(), reporter)  # must not raise


def test_write_response_is_a_noop_without_an_env_entry():
    """The shared helper other test doubles call directly is just as tolerant."""
    write_response(_spec())  # must not raise, and writes nothing


def test_write_response_writes_to_the_path_in_the_spec(tmp_path):
    path = tmp_path / "response.json"
    write_response(_spec(env={RESPONSE_ENV: str(path)}), [Answer(text="hi")])
    assert Path(path).read_text()
    assert [o.kind for o in parse_outputs(path.read_text())] == ["answer"]
