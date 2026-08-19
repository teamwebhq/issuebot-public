"""Test/dry-run harness that records launches without spawning a real CLI."""

from __future__ import annotations

import json
import threading
from collections.abc import Callable
from pathlib import Path

from issuebot.contracts import Output
from issuebot.plugins.harnesses.base import Harness, LaunchResult, LaunchSpec
from issuebot.process import REAL, Process
from issuebot.reporter import Reporter
from issuebot.run import RESPONSE_ENV


def write_response(
    spec: LaunchSpec, outputs: list[Output] | None = None, *, raw: str | None = None
) -> None:
    """Write a response document for ``spec``'s launch, exactly as a real agent
    would, to the path in ``spec.env[RESPONSE_ENV]`` (a no-op if that key is
    absent, e.g. a bare `LaunchSpec` a test built by hand).

    A shared helper rather than something only `FakeHarness.launch` does, so a
    test double that builds its own `LaunchResult` without going through it
    (`run.execute`'s overload-retry and resume tests do) can still satisfy
    "every cleanly-exited run leaves a response document" by calling this
    directly. ``raw`` overrides the encoded document entirely, for a test that
    wants to write something `parse_outputs` rejects.
    """
    path = spec.env.get(RESPONSE_ENV)
    if not path:
        return
    if raw is None:
        raw = json.dumps({"outputs": [o.model_dump(mode="json") for o in outputs or []]})
    Path(path).write_text(raw)


class FakeHarness(Harness):
    """Test/dry-run harness: records launches, optionally replays canned output
    lines to the reporter, and returns a configured exit code."""

    name = "fake"

    def __init__(
        self,
        exit_code: int = 0,
        on_launch: Callable[[LaunchSpec], None] | None = None,
        lines: list[str] | None = None,
        session_id: str | None = None,
        result_text: str = "",
        summary: str = "Fake title\nFake body",
        *,
        # Accepted only so every harness can be constructed uniformly
        # (`plugin.harness(proc=...)`, see the conformance suite); this harness
        # never spawns anything, so it is stored but never read.
        proc: Process = REAL,
        # What the agent "wrote" to its response document. Defaults to an empty
        # list — a real run that deliberately reported nothing, not a missing
        # document — so a test that doesn't care about outputs still gets a
        # `done` run rather than tripping `run.execute`'s missing-response gate.
        outputs: list[Output] | None = None,
        # False simulates the agent never finishing: nothing is written at
        # RESPONSE_ENV, and `run.execute` must fail the run over it.
        writes_response: bool = True,
        # Overrides the encoded document with literal text `parse_outputs`
        # rejects, for exercising the malformed-document path.
        response_raw: str | None = None,
    ) -> None:
        self.exit_code = exit_code
        self._on_launch = on_launch
        self._lines = lines
        self._session_id = session_id
        self._result_text = result_text
        self._summary = summary
        self._proc = proc
        self._outputs = outputs
        self._writes_response = writes_response
        self._response_raw = response_raw
        self.calls: list[LaunchSpec] = []
        self.summarize_calls: list[tuple[str, str, str | None, str]] = []

    def launch(
        self,
        spec: LaunchSpec,
        reporter: Reporter,
        cancel: threading.Event | None = None,
    ) -> LaunchResult:
        """Record the launch, replay any canned lines to the reporter, write the
        scripted response document (unless suppressed), and return the
        configured exit code. ``cancel`` is accepted and ignored."""
        self.calls.append(spec)
        if self._on_launch is not None:
            self._on_launch(spec)

        for line in self._lines or []:
            reporter.raw(line)
            ev = self.parse_line(line)
            if ev is not None:
                reporter.event(ev)

        if self._writes_response:
            write_response(spec, self._outputs, raw=self._response_raw)

        return LaunchResult(
            exit_code=self.exit_code,
            session_id=self._session_id,
            result_text=self._result_text,
        )

    def summarize(self, diff: str, *, context: str, model: str | None, folder: str) -> str:
        """Record the call and return the configured canned summary."""
        self.summarize_calls.append((diff, context, model, folder))
        return self._summary
