"""Finding plugins and asking what they own.

Discovery is a scan of each type package for subpackages exposing a `PLUGIN`.
Internal only: nothing here reads entry points, so a plugin cannot yet ship in a
separate distribution. Adding that later changes this module and no plugin.
"""

from __future__ import annotations

import pkgutil
from collections.abc import Iterable
from functools import cache
from importlib import import_module
from typing import Any, Literal, get_args, get_origin

from pydantic import TypeAdapter, ValidationError
from pydantic.fields import FieldInfo

from issuebot.plugins.base import Plugin

KINDS = ("sources", "workspaces", "environments", "harnesses", "sinks")

_SINGULAR = {
    "sources": "source",
    "workspaces": "workspace",
    "environments": "environment",
    "harnesses": "harness",
    "sinks": "sink",
}


class UnknownPlugin(ValueError):
    """A config named a plugin that is not installed."""


class SettingError(ValueError):
    """A ``--set`` assignment names something no installed plugin can honour."""


class PluginConflict(RuntimeError):
    """Two plugins claim the same config key, so a config using it is ambiguous."""


@cache
def discover(
    root: str = "issuebot.plugins", kinds: tuple[str, ...] = KINDS
) -> dict[str, dict[str, Plugin]]:
    """Every installed plugin, keyed by type then name.

    Cached: the set cannot change within a process, and discovery imports every
    plugin module. `root` and `kinds` are parameters so tests can discover a
    throwaway tree rather than the shipped one."""
    found: dict[str, dict[str, Plugin]] = {}
    for kind in kinds:
        found[kind] = {}
        try:
            package = import_module(f"{root}.{kind}")
        except ModuleNotFoundError:
            continue
        for entry in pkgutil.iter_modules(package.__path__):
            if not entry.ispkg:
                continue
            plugin = getattr(import_module(f"{root}.{kind}.{entry.name}"), "PLUGIN", None)
            if plugin is not None:
                found[kind][plugin.name] = plugin
    _check_no_key_collisions(found)
    return found


def _check_no_key_collisions(found: dict[str, dict[str, Plugin]]) -> None:
    """Refuse a plugin set where two plugins claim one config key."""
    owner: dict[str, str] = {}
    for of_kind in found.values():
        for plugin in of_kind.values():
            for key in plugin.claimed_keys:
                if key in owner:
                    raise PluginConflict(
                        f"'{plugin.name}' and '{owner[key]}' both claim '{key}'; a plugin "
                        f"with generic field names must use a table, not flat keys"
                    )
                owner[key] = plugin.name


def all_of(kind: str) -> dict[str, Plugin]:
    """Every installed plugin of one type, keyed by name.

    An unknown *type* raises :class:`UnknownPlugin` too, and here rather than in
    each caller: `get`, `names_of` and `offered` all route through this one
    lookup, so the check cannot be repeated while building an error message
    about itself (`names_of(kind)` comes back through here).
    """
    try:
        return discover()[kind]
    except KeyError:
        raise UnknownPlugin(f"unknown plugin type '{kind}' (known: {', '.join(KINDS)})") from None


def names_of(kind: str) -> list[str]:
    """The names of every installed plugin of one type, sorted."""
    return sorted(all_of(kind))


def offered(kind: str) -> list[str]:
    """The names of one type's plugins that a user should be *offered*.

    :func:`names_of` minus the hidden ones. The distinction is between
    resolving a plugin and proposing it: `fake` is a real sink, a config may
    name it and `plugins.get` must find it, but putting it in a wizard question
    makes it look like one of the answers — and since these lists are sorted,
    `fake` sorted *first*, so it was the first sink every `issuebot connect`
    asked about. `names_of` stays the honest full list, which is why
    `UnknownPlugin`'s "known:" still uses it: a config naming a hidden plugin
    is valid, so a typo of one deserves to be told what it missed.
    """
    return sorted(name for name, plugin in all_of(kind).items() if not plugin.hidden)


def get(kind: str, name: str) -> Plugin:
    """One plugin by type and name, or raise naming the ones that exist."""
    try:
        return all_of(kind)[name]
    except KeyError:
        known = ", ".join(names_of(kind)) or "none installed"
        raise UnknownPlugin(f"unknown {_SINGULAR[kind]} '{name}' (known: {known})") from None


def every() -> list[Plugin]:
    """Every installed plugin, across every type, flattened."""
    return [plugin for of_kind in discover().values() for plugin in of_kind.values()]


def named(name: str) -> Plugin | None:
    """The installed plugin called `name`, whatever type it is, or None.

    Types are a taxonomy for *us*; a user naming a plugin on the command line or
    in a config table names it the way it names itself, so the lookup they get
    has to be type-blind."""
    return next((plugin for plugin in every() if plugin.name == name), None)


def claimant(key: str) -> Plugin | None:
    """The plugin that owns a per-connection config key, if any."""
    for of_kind in discover().values():
        for plugin in of_kind.values():
            if key in plugin.claimed_keys:
                return plugin
    return None


def every_name() -> frozenset[str]:
    """Every installed plugin's name, across all types — for top-level tables."""
    return frozenset(name for of_kind in discover().values() for name in of_kind)


def mount_cli(app, root: str = "issuebot.plugins", kinds: tuple[str, ...] = KINDS) -> None:
    """Add every installed plugin's command group to the CLI, sorted for stable help.

    `root`/`kinds` mirror :func:`discover`'s, so a test can mount a throwaway
    tree instead of the shipped one."""
    found = discover(root, kinds)
    for kind in kinds:
        for name, plugin in sorted(found.get(kind, {}).items()):
            if plugin.cli is not None:
                app.add_typer(plugin.cli, name=name)


# ---------------------------------------------------------------------------
# Plugin settings from the command line
# ---------------------------------------------------------------------------
#
# A generic command (`issuebot connect`) must not carry any one plugin's flags —
# that is the leak the whole plugin boundary exists to prevent, and a second
# environment could not be added by writing one folder if it had to grow flags
# on someone else's command. So a plugin's own settings arrive through one
# generic, repeatable `--set <plugin>.<key>=<value>`, whose vocabulary is
# whatever the registry answers with at the moment you ask.


def _field_help(name: str, field: FieldInfo) -> str:
    """One `key (choices) — description` line, with whatever the field offers.

    Both halves come off the field itself: a `Literal` annotation is rendered as
    its own choices — only a Literal, since every other annotation's `get_args`
    is type machinery (`str | None` is not a menu) — and `description=` (when
    the plugin bothered to write one) as the prose. A plugin that writes neither
    gets a bare key: the same generic rendering, just with less to render."""
    choices = get_args(field.annotation) if get_origin(field.annotation) is Literal else ()
    line = f"{name} ({'|'.join(str(c) for c in choices)})" if choices else name
    return f"{line} — {field.description}" if field.description else line


def settings_help(exclude: Iterable[str] = ()) -> str:
    """Every per-connection setting `--set` will accept, for the flag's help text.

    Built from the installed plugins' settings models, so `issuebot connect
    --help` names exactly what this install can honour — no hand-maintained list
    to drift, and a newly installed plugin documents itself. This is the only
    place a `--set`-only setting can be explained — no plugin has flags of its
    own to carry help text.

    Blank-line separated: that is the one break Typer's help renderer keeps, so
    each setting lands on its own line instead of running into the next.

    `exclude` drops flat keys the *caller* owns — a command whose own flag
    writes a key should not advertise a second way to write it that it will
    then refuse. Which keys those are is the command's knowledge, not this
    module's, so it is passed in rather than known here."""
    owned = frozenset(exclude)
    lines = sorted(
        _field_help(f"{plugin.name}.{name}", field)
        for plugin in every()
        if plugin.settings is not None
        for name, field in plugin.settings.model_fields.items()
        if not (plugin.flat and name in owned)
    )
    return "\n\n".join(lines) or "no installed plugin takes settings"


def _parse_setting(assignment: str) -> tuple[Plugin, str, Any]:
    """One `plugin.key=value` assignment, resolved against the installed plugins.

    Every part is checked against the registry rather than accepted on trust:
    the plugin must be installed and take settings, the key must be a field of
    its settings model, and the value must parse as that field's own type. So a
    typo is refused here, by name, instead of being written to config for a
    later load to reject — or worse, silently ignored by a model that drops
    unknown keys."""
    target, sep, raw = assignment.partition("=")
    if not sep:
        raise SettingError(f"--set expects <plugin>.<key>=<value>, got '{assignment}'")

    name, dot, key = target.strip().partition(".")
    if not dot:
        raise SettingError(f"--set expects <plugin>.<key>=<value>, got '{assignment}'")

    plugin = named(name)
    if plugin is None or plugin.settings is None:
        raise SettingError(f"no installed plugin takes settings named '{name}'")

    field = plugin.settings.model_fields.get(key)
    if field is None:
        known = ", ".join(sorted(plugin.settings.model_fields))
        raise SettingError(f"'{name}' has no setting '{key}' (it takes: {known})")

    # Parse the string against the field's own annotation, so the value's domain
    # is reported in the plugin's words ("Input should be 'isolated' or
    # 'private'") and the config is written with the type it will be read as.
    try:
        value = TypeAdapter(field.annotation).validate_strings(raw)
    except ValidationError as exc:
        reason = "; ".join(error["msg"] for error in exc.errors())
        raise SettingError(f"--set {target}: {reason}") from None

    return plugin, key, value


def settings_from(assignments: Iterable[str]) -> dict[str, Any]:
    """Turn `--set plugin.key=value` assignments into connection settings.

    A flat plugin's key lands on the connection itself, a table plugin's under
    the plugin's name — exactly the shape `Connection.settings_for` reads back,
    so what you set is what that plugin gets."""
    gathered: dict[str, Any] = {}
    for assignment in assignments:
        plugin, key, value = _parse_setting(assignment)
        if plugin.flat:
            gathered[key] = value
        else:
            gathered.setdefault(plugin.name, {})[key] = value
    return gathered
