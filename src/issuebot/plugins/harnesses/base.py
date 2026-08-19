"""Harness abstraction: given a LaunchSpec, run an agent process in a folder with
the MCP servers it was handed wired in, and report how it exited."""

from __future__ import annotations

import threading
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, ClassVar

from issuebot.events import AgentEvent, raw_event

if TYPE_CHECKING:
    from issuebot.reporter import Reporter

# A spawn runs a child process, teeing each output line to ``on_line`` as it
# arrives, and terminates the child when ``cancel`` is set (for Ctrl-C abort /
# timeout). It returns the child's exit code.
Spawn = Callable[..., int]


@dataclass(frozen=True)
class LaunchSpec:
    """Everything a harness needs to run one task: the prompt, where to run it,
    and which MCP servers to wire in."""

    prompt: str
    folder: str
    # When set, reopen this prior conversation instead of starting fresh. Only
    # ever set for a harness that declares `resumes_sessions`; the id is that
    # harness's own token, opaque to everything that carries it.
    resume_session_id: str | None = None
    # Workspace bootstrap from the repo's own config (see provision.py), applied
    # by the harness. All default empty so harnesses/tests that ignore them are
    # unaffected.
    env: dict[str, str] = field(default_factory=dict)
    plugin_dirs: list[str] = field(default_factory=list)
    # Every MCP server this launch gets, already as `mcpServers` fragments: the
    # source's own (`Source.agent_access`) and the repo's bootstrap ones, merged
    # by `run.execute` in that precedence. A harness writes them out in whatever
    # its CLI reads (`mcp_document` below) and knows nothing of where each came
    # from — no transport, no credential, no server name is spelled here.
    mcp_servers: list[dict[str, Any]] = field(default_factory=list)
    # Tools removed from the agent's surface, by the names the agent knows them
    # by; each harness turns them into whatever its own CLI calls that. The
    # runner passes none today (`Job.withheld_tools` is always empty; a respond
    # run's read-only-ness is enforced by `Source.permits` barring `changes`,
    # not by removing tools — ADR-0011).
    disallowed_tools: list[str] = field(default_factory=list)

    def mcp_document(self) -> dict[str, Any]:
        """This launch's servers as one `{"mcpServers": {...}}` document.

        The file format every agent CLI reads, built once here because both
        harnesses write the same document to a temp file. Later fragments win,
        which is why `run.execute` appends the source's servers last: a repo's
        bootstrap config cannot displace the agent's channel to the source that
        gave it the work by declaring a server of the same name."""
        servers: dict[str, Any] = {}
        for fragment in self.mcp_servers:
            servers.update(fragment)

        return {"mcpServers": servers}


@dataclass(frozen=True)
class LaunchResult:
    """How a launch ended: exit code, captured output, and whatever the harness
    could recover from it (a resumable session, a retry signal, summary text)."""

    exit_code: int
    stdout: str = ""
    stderr: str = ""
    # The id of the conversation this run opened, when the harness resumes and
    # its output named one. Stored per task and handed back as
    # `LaunchSpec.resume_session_id` on the next launch.
    session_id: str | None = None
    # True when the run failed on a transient, retryable API condition (e.g. a
    # 529 overloaded). The supervisor backs off and resumes rather than failing
    # the task outright.
    retryable: bool = False
    # The agent's final result text, when its output carried one, used as the
    # mechanical PR-body fallback when the summarizer is unavailable.
    result_text: str = ""


class Harness(ABC):
    """Drives one coding-agent CLI against a task.

    An ABC rather than a Protocol: every harness must actually subclass this
    (checked by the conformance suite), not merely happen to match its shape —
    the whole point is that a new harness cannot pass by accident.
    """

    # Set by each subclass; also the name it is registered under in the plugin
    # registry (`plugins.get("harnesses", harness.name)`).
    name: ClassVar[str]

    # Whether this harness can reopen a prior conversation from a stored id.
    #
    # Declared, never inferred from the harness's name — core comparing a name
    # against a literal would make one plugin's identity compile-time knowledge
    # everywhere. Setting this gets the harness a `SessionStore` kept for it
    # (`issuebot.sessions.store_for`) and its own ids handed back as
    # `LaunchSpec.resume_session_id`; leaving it False starts every task fresh,
    # which is the safe default.
    resumes_sessions: ClassVar[bool] = False

    def parse_line(self, line: str) -> AgentEvent | None:
        """One line of this harness's output as a feed event, or None if it
        carries nothing worth showing.

        The default treats every line as opaque text, which is the honest
        reading for a harness that just prints. A harness whose output is a
        structured stream overrides this — and because reading a *recorded* run
        back goes through the same method (`issuebot.logs`), the wire format is
        named in exactly one place per harness rather than once per consumer.
        """
        return raw_event(line)

    @abstractmethod
    def launch(
        self,
        spec: LaunchSpec,
        reporter: Reporter,
        cancel: threading.Event | None = None,
    ) -> LaunchResult:
        """Run the agent to completion on one task, streaming output to
        ``reporter`` as it arrives; ``cancel`` (when set) aborts the run.
        Return its exit info."""

    @abstractmethod
    def summarize(self, diff: str, *, context: str, model: str | None, folder: str) -> str:
        """Generate PR title+body text from a diff. Tools-free and MCP-free; the
        first output line is the title, the rest is the body. ``folder`` is the
        cwd to run in. May raise; callers fall back to a mechanical description."""
