"""What a config is not allowed to say.

The governing rule: a key that cannot apply is an error, not something ignored.
A config saying something impossible is a config whose author believed something
false, and the sooner they hear about it the better.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from conftest import config, connection
from issuebot import plugins
from issuebot.config import (
    Config,
    ConfigError,
    Connection,
    load_config,
    source_plugin,
    validate_config,
)
from issuebot.plugins.base import Plugin


def _cfg(**conn_overrides: Any) -> Config:
    """A config carrying one connection, with the given overrides applied on
    top of the simplest working shape (`conftest.connection`'s defaults)."""
    return config(connections=[connection(**conn_overrides)])


# --- the brief's own cases ----------------------------------------------------


def test_an_unknown_key_is_rejected_with_a_suggestion():
    problems = validate_config(_cfg(folder="/tmp/p", fodler="/tmp/p"))
    assert any("unknown key 'fodler'" in p and "folder" in p for p in problems)


def test_an_unknown_executor_names_the_ones_that_exist():
    """The message lists what is installed rather than saying nothing is.

    Which names those are comes from the registry, so this keeps holding when
    an environment plugin is added or deleted — the point is that the user is
    told their options, not which options this install happens to ship."""
    problems = validate_config(_cfg(executor="aws"))
    reported = "\n".join(p for p in problems if "unknown environment 'aws'" in p)

    assert reported, problems
    for name in plugins.names_of("environments"):
        assert name in reported


def test_settings_for_an_unused_plugin_are_rejected():
    """Keeping them would be convenient when switching executors, but it is
    exactly the case where a user believes a setting is in effect and it is not.

    Asked of whichever table plugin is installed rather than a named one: what
    is under test is that an unselected plugin's table is refused, not any one
    plugin's settings."""
    tabled = sorted(
        (p for p in plugins.every() if p.settings is not None and not p.flat),
        key=lambda p: p.name,
    )
    unused = tabled[0] if tabled else None
    if unused is None:
        pytest.skip("no installed plugin keeps its settings in a table")

    problems = validate_config(_cfg(**{unused.name: {}}))

    assert any(unused.name in p and "does not use" in p for p in problems)


def test_an_environments_table_is_not_refused_before_the_executor_resolves():
    """A connection that names no environment has not said it does not use one.

    It has said nothing, and the honest complaint is the one about saying
    nothing. Reporting "does not use <plugin>" as well sent the user to delete a
    table when what they had to do was name where their tasks run — a false
    accusation on top of a true error, which is worse than either alone.

    Needs an install where naming none *cannot* resolve — with a single
    environment there is nothing to be ambiguous about and no error to be false
    on top of, so the premise is skipped rather than faked."""
    if len(plugins.names_of("environments")) < 2:
        pytest.skip("one environment installed, so naming none is not ambiguous")

    tabled = next(
        (p for p in plugins.all_of("environments").values() if p.settings and not p.flat), None
    )
    if tabled is None:
        pytest.skip("no installed environment keeps its settings in a table")

    problems = validate_config(_cfg(executor=None, **{tabled.name: {}}))

    assert not [p for p in problems if "does not use" in p], problems
    assert any("no environment named" in p for p in problems), problems


def test_an_unknown_sink_is_rejected():
    problems = validate_config(_cfg(sinks=["netlify"]))
    assert any("unknown sink 'netlify'" in p for p in problems)


def test_a_typo_inside_a_sink_table_is_rejected():
    """`sinks = [{...}]` is a table with no plugin of its own to claim a typo
    inside it (`requird` for `required`) — nothing but SinkRef itself can
    catch it, so it must not silently default `required` to True."""
    with pytest.raises(ValidationError, match="requird"):
        Connection.model_validate({"name": "p", "sinks": [{"name": "fake", "requird": False}]})


def test_every_problem_is_reported_not_just_the_first():
    """A hand-edited file usually has more than one, and fixing them a
    round-trip at a time is miserable."""
    problems = validate_config(_cfg(fodler="/x", executor="aws"))
    assert len(problems) >= 2


def test_loading_a_bad_config_raises_with_all_of_them(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('[[connections]]\nname = "w"\nfodler = "/x"\nexecutor = "aws"\n')
    with pytest.raises(ConfigError) as exc:
        load_config(path)
    assert "fodler" in str(exc.value) and "aws" in str(exc.value)


# --- the mechanism itself, and the edges the brief flags -----------------------


def test_a_valid_config_has_no_problems():
    """The everyday case: nothing here should ever be flagged."""
    assert validate_config(_cfg(sinks=["fake"])) == []


def test_load_config_returns_the_config_when_valid(tmp_path):
    from issuebot.config import save_config

    path = tmp_path / "config.toml"
    save_config(_cfg(sinks=["fake"]), path)
    assert load_config(path) is not None


def test_a_table_named_after_an_installed_plugin_with_no_model_is_accepted():
    """`git` is still registered name-only (Task 7 gives it a real settings
    model) — its table is accepted as written, not field-checked, because the
    plugin exists even though nothing yet knows what its table should contain."""
    cfg = config(git={"clone_root": "/x"})
    assert validate_config(cfg) == []


def test_a_bad_global_settings_table_is_reported_once_not_once_per_connection():
    """`plugins_in_play` re-resolves a plugin for every connection when
    collecting per-connection settings/validate hooks, but a *global* table is
    checked once in the top-level loop — a typo in one must not multiply with
    the number of connections that use the plugin."""
    source = source_plugin()
    label = f"[{source.name}]"

    def problems(count: int) -> list[str]:
        cfg = Config.model_validate(
            {
                "harness": "fake",
                source.name: {},  # missing whatever the source declares it needs
                "connections": [connection(name=f"c{i}") for i in range(count)],
            }
        )
        return [p for p in validate_config(cfg) if p.startswith(label)]

    assert problems(3) == problems(1)
    assert problems(1)  # ...and there was something to report in the first place


def test_a_missing_source_table_is_a_config_error_not_a_traceback():
    """The source's global settings are the one table a config cannot omit —
    every command that reaches the board reads them.

    Absent, they used to sail through load (the loop above only walks keys the
    file has) and come back as a raw pydantic `ValidationError` out of the first
    API call: `issuebot doctor` on a config with no source table printed a
    traceback. Pre-existing, and exactly the class Task 15 set out to remove."""
    source = source_plugin()
    problems = validate_config(Config(harness="fake"))

    assert any(f"[{source.name}]" in p for p in problems)


def test_an_unclaimed_top_level_table_is_rejected():
    problems = validate_config(Config.model_validate({"bogus": {"x": 1}}))
    assert any("unknown key 'bogus'" in p for p in problems)


def test_a_global_settings_table_is_field_validated():
    """A source with a real `global_settings` model has its table checked
    against that model's own fields — here, an empty one is missing whatever
    the source declares it needs.

    The plugin comes from the registry rather than being named: what is under
    test is that a modelled table is validated at all, not one source's schema.
    """
    source = source_plugin()
    cfg = Config.model_validate({source.name: {}})
    problems = validate_config(cfg)
    assert any(f"[{source.name}]" in p for p in problems)


def test_a_flat_plugins_own_field_validation_is_enforced():
    """`board` is the source's required field; setting one of its siblings
    (`done`) without it is still a settings error, ordinary field validation.

    Which plugin owns those keys is read off the registry, so this asserts the
    mechanism rather than one source's schema."""
    source = source_plugin()
    conn = Connection.model_validate({"name": "p", "done": "review"})
    problems = validate_config(Config(connections=[conn]))
    assert any(source.name in p and "board" in p for p in problems)


def test_a_plugins_validate_hook_runs_against_the_whole_connection(monkeypatch):
    """The mechanism Task 7 (git) and others build on: `plugin.validate` is
    called with the whole connection, and whatever it returns is reported like
    any other problem. A stub stands in — no real plugin has this hook yet."""
    stub = Plugin(name="stub-sink", flat=False, validate=lambda c: [f"stub objects to {c.name}"])
    fake_registry = {
        "sources": {},
        "workspaces": {},
        "environments": {"local": Plugin(name="local")},
        "harnesses": {"fake": Plugin(name="fake")},
        "sinks": {"stub-sink": stub},
    }
    monkeypatch.setattr(plugins, "discover", lambda: fake_registry)

    problems = validate_config(Config(connections=[Connection(name="p", sinks=["stub-sink"])]))

    assert any("stub objects to p" in p for p in problems)


def test_a_keyless_connection_is_validated_against_the_workspace_the_run_would_use(monkeypatch):
    """A connection claiming no workspace's keys still runs in one, and that
    one's rules must be applied at load like any other plugin's.

    They were not. `plugins_in_play` resolved flat plugins purely on the keys a
    connection sets, so a keyless connection put *no* workspace in play while
    `runner.workspace_for` resolved one for it regardless — its settings model
    and `validate` hook were never checked. Always possible in principle; it
    became reachable when the fallback stopped being a fixed plugin, because on
    an install whose only workspace has a strategy (git alone, the shape below)
    the run resolves *that* for a config nothing ever held to its rules. The
    connection here is the case it costs: no folder and no repo, which git's
    own validator calls "nowhere to work" and which used to load cleanly and
    fail at run time instead.
    """
    from issuebot.plugins.base import WorkspacePlugin
    from issuebot.plugins.workspaces.base import Workspace

    class _Versioned(Workspace):
        produces = frozenset({"changes", "answer"})

    stub = WorkspacePlugin(
        name="versioned",
        workspace=_Versioned,
        validate=lambda c: [] if c.folder else ["nowhere to work"],
    )
    monkeypatch.setattr(
        plugins,
        "discover",
        lambda: {
            "sources": {},
            "workspaces": {"versioned": stub},
            "environments": {"local": Plugin(name="local")},
            "harnesses": {"fake": Plugin(name="fake")},
            "sinks": {},
        },
    )

    def problems(**fields: Any) -> list[str]:
        return validate_config(Config(connections=[Connection(name="p", **fields)]))

    assert any("nowhere to work" in p for p in problems())
    assert not any("nowhere to work" in p for p in problems(folder="/tmp/p"))
