"""``issuebot doctor``'s checks: is this install actually able to do the work.

Named `doctor_checks` rather than `doctor` so the CLI can import it alongside
its own `doctor` command without one name shadowing the other.


The checks that are core to every install live here — the board answers, the
harness executable exists — and everything else is *asked for*, not known: each
installed plugin that offers a `doctor` hook is given the thing it can check
(the config for the harness, the connection for everything a connection wires
together) and reports in its own words.

That is the whole point of the split. The version of this command before the
plugin boundary spelled one vendor's CLI name, both of its token environment
variables and its plan limits; ran `git ls-remote` itself; and knew that a PR
needs `gh`. None of that is knowledge a health check should hold — it belongs to
the environment, the workspace and the sink, which is where it now lives.
"""

from __future__ import annotations

import os
import shutil
from collections.abc import Callable
from typing import Any

from issuebot import plugins
from issuebot.config import Config, Connection, harness_name, harness_settings, plugins_in_play
from issuebot.plugins.base import Plugin

Echo = Callable[[str], None]


def _ask(plugin: Plugin, subject: Any, *, echo: Echo, warn: Echo) -> None:
    """Run one plugin's doctor hook, turning a raise into a finding.

    A check that raises is itself something the user needs told, and the least
    useful way to tell them is a traceback: this command's whole promise is to
    report on *everything*, so one broken plugin must not take the rest of the
    connection — or every connection after it — with it.
    """
    if plugin.doctor is None:
        return
    try:
        plugin.doctor(subject, echo=echo)
    except Exception as exc:  # noqa: BLE001 - one broken check must not end the report
        warn(f"Warning: plugin '{plugin.name}' check failed: {exc}")


def run_harness_doctor(cfg: Config, *, echo: Echo) -> None:
    """Run the configured harness plugin's own doctor hook, if it has one.

    Shared with `issuebot init`, which runs exactly this and nothing else: a
    harness whose hook registers the board MCP in the user's own install is
    doing as much setup as checking. A harness with nothing to do simply has
    `doctor=None`."""
    plugin = plugins.get("harnesses", harness_name(cfg))
    if plugin.doctor is not None:
        plugin.doctor(cfg, echo=echo)


def check_harness(cfg: Config, *, echo: Echo, warn: Echo) -> None:
    """Check the configured harness: its executable, then its own hook.

    An explicit `command` is checked as a path (it is one); otherwise the
    harness's name is resolved on PATH.

    A config this build cannot name a harness for — one that names a plugin
    since removed, or names none on an install with several — is a warning here
    rather than the exception it is everywhere else: `doctor`'s whole promise is
    to report on everything, and it is the command you reach for precisely when
    the install is in that state.
    """
    try:
        name = harness_name(cfg)
        plugin = plugins.get("harnesses", name)
    except plugins.UnknownPlugin as exc:
        warn(f"Warning: {exc}.")
        return

    command = harness_settings(cfg, name).get("command")
    if command:
        if not os.access(command, os.X_OK):
            warn(f"Warning: harness executable '{command}' is missing or not executable.")
    elif shutil.which(name) is None:
        warn(f"Warning: harness '{name}' is not on PATH.")

    # The harness's own hook, guarded — `doctor` reports, `init` may raise.
    _ask(plugin, cfg, echo=echo, warn=warn)


def check_connection(conn: Connection, *, warn: Echo) -> None:
    """Ask every plugin this connection resolves to for its own checks.

    `plugins_in_play` is the same resolution `validate_config` uses, minus the
    harness (a global choice, checked once by :func:`check_harness` rather than
    once per connection): the source it reads from, the workspace strategy its
    keys select, the environment it runs in, and each sink it publishes
    through. A plugin with nothing to check has no hook and is skipped.
    """
    for plugin in plugins_in_play(conn).values():
        _ask(plugin, conn, echo=warn, warn=warn)


def check(cfg: Config, *, echo: Echo, warn: Echo) -> None:
    """Run every check, reporting through `echo` (news) and `warn` (problems).

    The board/PAT check is the caller's: it needs a live client, and unlike
    everything here its failure is fatal — there is no point checking how the
    work would run when the work cannot be fetched.
    """
    check_harness(cfg, echo=echo, warn=warn)

    for conn in cfg.connections:
        check_connection(conn, warn=warn)
