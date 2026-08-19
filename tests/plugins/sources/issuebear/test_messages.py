"""Tests for what issuebot says on the board — and, mostly, what it doesn't.

`summarize` reports only what the agent could not have reported itself. Every
launch template tells the agent that task comments are its one channel to
people and to post its answer, its questions and a summary of what it did; a
runner that then restates those puts a second copy of the agent's own words
underneath the first. So the tests here are as much about silence as about
wording.
"""

from __future__ import annotations

from issuebot.contracts import Answer, Changed, Response, SinkResult
from issuebot.plugins.sources.issuebear import messages


def test_say_speaks_in_the_runners_own_voice():
    assert messages.say("hello").startswith(messages.PREFIX)


def test_a_clean_run_the_agent_already_narrated_says_nothing():
    """The agent posted its own summary comment — that is what it was told to
    do. A second one from the runner is the same information twice."""
    response = Response(status="done", outputs=[Changed(summary="fixed the login bug")])

    assert messages.summarize(response, []) is None


def test_an_answer_is_not_repeated_either():
    response = Response(status="done", outputs=[Answer(text="the answer is 42")])

    assert messages.summarize(response, []) is None


def test_it_reports_what_a_sink_did():
    """The half the agent cannot know: the sink ran after it exited, and the
    URL did not exist while it was working."""
    response = Response(status="done", outputs=[Changed(summary="did the thing")])
    result = SinkResult(sink="pr-forge", ok=True, summary="opened PR", url="https://x/pull/1")

    text = messages.summarize(response, [result])

    assert text is not None
    assert "opened PR" in text
    assert "https://x/pull/1" in text
    assert "did the thing" not in text  # the agent's own words, already posted


def test_it_reports_a_failed_sink_without_its_url():
    response = Response(status="done", outputs=[Changed(summary="did the thing")])
    result = SinkResult(sink="pr-forge", ok=False, summary="could not reach the forge")

    text = messages.summarize(response, [result])

    assert text is not None
    assert "pr-forge failed" in text
    assert "could not reach the forge" in text


def test_a_run_that_ended_badly_is_stated_outright():
    """A failed run may have produced no comment at all, so this is the one
    status worth saying without being asked."""
    response = Response(status="failed", result_text="workspace prep failed")

    assert "workspace prep failed" in (messages.summarize(response, []) or "")


def test_a_bare_failure_still_names_its_status():
    response = Response(status="aborted")

    assert "aborted" in (messages.summarize(response, []) or "")


def test_the_text_carries_no_prefix_of_its_own():
    """The caller adds the runner's voice. Prefixing here too produced
    'issuebot: issuebot: …' on every comment it posted."""
    response = Response(status="failed", result_text="nope")

    assert not (messages.summarize(response, []) or "").startswith(messages.PREFIX)
