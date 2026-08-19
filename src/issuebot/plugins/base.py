"""Plugin declarations: name, settings, what config keys it owns, and optional features."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel

if TYPE_CHECKING:
    from issuebot.config import Connection
    from issuebot.plugins.environments.base import ExecutionEnvironment
    from issuebot.plugins.harnesses.base import Harness
    from issuebot.plugins.sinks.base import Sink
    from issuebot.plugins.sources.base import Source
    from issuebot.plugins.workspaces.base import Workspace

Validator = Callable[["Connection"], Iterable[str]]


@dataclass(frozen=True, kw_only=True)
class Plugin:
    """A plugin: a tool, source, workspace, environment, harness, or sink.

    Args:
        name: The plugin's identifier in config.
        settings: The Pydantic model holding its per-connection config, if any.
        global_settings: The Pydantic model holding its global app config, if any.
        flat: If True, the plugin owns all fields of its settings model; if False,
            its config is a table under its name.
        validate: A function to validate connection-level config for this plugin.
        cli: A Typer app for this plugin's commands.
        doctor: A function to check the plugin's state.
        wizard: A function to configure this plugin interactively.
        hidden: If True, the plugin is never *offered* — it is left out of the
            wizard's questions and out of `--help`'s "Installed:" lines. It
            stays fully resolvable: a config may name it and `plugins.get`
            finds it. For a plugin that exists to be wired up deliberately
            (the `fake` harness, the `fake` sink) rather than chosen.
    """

    name: str
    settings: type[BaseModel] | None = None
    global_settings: type[BaseModel] | None = None
    flat: bool = False
    validate: Validator | None = None
    cli: Any = None
    doctor: Any = None
    wizard: Any = None
    hidden: bool = False

    @property
    def claimed_keys(self) -> frozenset[str]:
        """The config keys this plugin owns.

        If the plugin has no settings, it claims nothing. If flat=True, it
        claims all field names of its settings model. If flat=False, it claims
        only its own name (as a table).
        """
        if self.settings is None:
            return frozenset()
        if self.flat:
            # ponytail: all fields on a flat plugin are claimed keys.
            return frozenset(self.settings.model_fields.keys())
        return frozenset({self.name})


@dataclass(frozen=True, kw_only=True)
class SourcePlugin(Plugin):
    """A plugin that reads work from an external system.

    Args:
        source: The :class:`~issuebot.plugins.sources.base.Source` implementation.
        setup: Gathers this source's *global* settings for ``issuebot init`` —
            where the work lives and the credential to reach it with. Separate
            from ``wizard`` because the two happen at different times and
            produce different things: this one runs once per install and returns
            a global table, ``wizard`` runs per connection and returns
            connection settings. Keeping ``wizard`` meaning "per connection"
            here is what makes it mean the same thing as an environment
            plugin's.
        settings_wizard: Gathers the per-connection settings this source owns,
            for the connect wizard. Separate from ``wizard`` because the two
            run at different points in the flow: ``wizard`` identifies the
            work before the connection is named, this one asks how the work is
            approached once the environment is known. Returns the settings
            plus whether runs under them may report ``changes`` — the neutral
            fact the workspace hook needs, stated by the source instead of
            core reading any source's key by name.
    """

    source: type[Source]
    setup: Callable[[], dict[str, Any]] | None = None
    settings_wizard: Callable[..., tuple[dict[str, Any], bool]] | None = None


@dataclass(frozen=True, kw_only=True)
class WorkspacePlugin(Plugin):
    """A plugin that owns a workspace directory structure."""

    workspace: type[Workspace]


@dataclass(frozen=True, kw_only=True)
class EnvironmentPlugin(Plugin):
    """A plugin that sets up a code execution environment."""

    environment: type[ExecutionEnvironment]


@dataclass(frozen=True, kw_only=True)
class HarnessPlugin(Plugin):
    """A plugin that drives one coding-agent CLI."""

    harness: type[Harness]


@dataclass(frozen=True, kw_only=True)
class SinkPlugin(Plugin):
    """A plugin that writes results back to an external system."""

    sink: type[Sink]
