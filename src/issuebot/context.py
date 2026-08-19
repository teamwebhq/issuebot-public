"""The runner-wide settings a run needs, resolved once and passed as one value.

Every field here comes from :class:`~issuebot.config.Config`. Carrying the
bundle means adding a knob touches one definition rather than every interface
between the config file and the code that reads it.

Deliberately settings only — the API client, the harness, and the reporter stay
explicit parameters. A value that also carried its collaborators would be a
service locator, and callers could no longer tell what a function actually
touches. Build one with :meth:`RunnerContext.from_config`; it is frozen, so a
variant is ``dataclasses.replace``.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from issuebot.agent_state import AgentState
from issuebot.config import Config, plugin_tables
from issuebot.sessions import SessionStore


@dataclass(frozen=True)
class RunnerContext:
    """Resolved runner settings, shared by every executor and every run."""

    # Hard wall-clock limit per task; None means no timeout.
    timeout_minutes: int | None = None

    # How often a claimed run heartbeats. 0 disables (tests).
    heartbeat_interval: float = 60.0

    # The agent's own user id on the source, learned once from `connect()` and
    # fixed for the process. An environment needs it to tell a sandbox who the
    # agent is (a mention session self-assigns with it); None means it could
    # not be resolved, which degrades rather than fails.
    agent_id: str | None = None

    # Per-task agent session store, or None when session resume is off (either
    # the harness cannot resume, or the install did not ask for it).
    store: SessionStore | None = None

    # True when several connections run side by side, so reporters prefix output.
    multi: bool = False

    # This connection's live state (phase, log tail, links), which telemetry
    # reports and the status file mirrors. A handle rather than a setting, but
    # every executor needs it and it is resolved per connection exactly like
    # `store`, so it travels with them.
    state: AgentState | None = None

    # Every plugin's global settings table, by plugin name, exactly as the
    # config holds them (:func:`~issuebot.config.plugin_tables`). Core reads
    # none of it: a factory that builds a plugin — `runner.sinks_for`,
    # `source_for`, `workspace_for` — hands that plugin its own table. It rides
    # here because those factories run deep inside a listener, long after the
    # `Config` they came from went out of scope, and the alternative was a field
    # per plugin setting that happened to be needed down there.
    #
    # This is the *only* way a plugin's settings reach a run. A named field per
    # plugin setting would mean a second plugin on the same axis had to declare
    # exactly those field names or `from_config` raised.
    plugin_settings: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def from_config(
        cls,
        cfg: Config,
        *,
        store: SessionStore | None = None,
        multi: bool = False,
        heartbeat_interval: float = 60.0,
        state: AgentState | None = None,
        agent_id: str | None = None,
    ) -> RunnerContext:
        """Build the context from a loaded config.

        The single place that knows which ``Config`` field feeds which runner
        setting.

        It knows no *plugin's* fields at all: every plugin table travels whole,
        by name, and is validated by the factory that builds that plugin. So
        this resolves no source or workspace just to build a context, and
        deleting either plugin cannot break a run that merely wanted a timeout.
        """
        return cls(
            plugin_settings=plugin_tables(cfg),
            timeout_minutes=cfg.task_timeout_minutes,
            heartbeat_interval=heartbeat_interval,
            store=store,
            multi=multi,
            state=state,
            agent_id=agent_id,
        )
