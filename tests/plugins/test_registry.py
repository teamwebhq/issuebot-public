"""The registry: what exists, who owns which config key, what is rejected."""

from __future__ import annotations

import pytest
from pydantic import BaseModel

from issuebot import plugins
from issuebot.plugins.base import Plugin

_PLAIN = """
    from issuebot.plugins.base import Plugin
    PLUGIN = Plugin(name="alpha")
"""

_FLAT = """
    from pydantic import BaseModel
    from issuebot.plugins.base import Plugin

    class Settings(BaseModel):
        widget_size: int = 1

    PLUGIN = Plugin(name="beta", settings=Settings, flat=True)
"""

_COLLIDES = """
    from pydantic import BaseModel
    from issuebot.plugins.base import Plugin

    class Settings(BaseModel):
        widget_size: int = 2

    PLUGIN = Plugin(name="gamma", settings=Settings, flat=True)
"""


def test_discovery_finds_every_plugin_in_a_type_package(plugin_tree):
    found = plugins.discover(root=plugin_tree(alpha=_PLAIN, beta=_FLAT), kinds=("widgets",))
    assert sorted(found["widgets"]) == ["alpha", "beta"]


def test_a_directory_without_a_declaration_is_ignored(plugin_tree):
    found = plugins.discover(root=plugin_tree(alpha=_PLAIN, nope="X = 1"), kinds=("widgets",))
    assert list(found["widgets"]) == ["alpha"]


def test_two_plugins_cannot_claim_the_same_flat_key(plugin_tree):
    """Checked at discovery because a collision is unresolvable at read time —
    there is no way to know whose `widget_size` a config file meant."""
    root = plugin_tree(beta=_FLAT, gamma=_COLLIDES)
    with pytest.raises(plugins.PluginConflict, match="widget_size"):
        plugins.discover(root=root, kinds=("widgets",))


def test_a_plugin_with_no_settings_claims_nothing():
    assert Plugin(name="local").claimed_keys == frozenset()


def test_a_table_plugin_claims_its_own_name():
    class Settings(BaseModel):
        token: str = ""

    assert Plugin(name="widget", settings=Settings).claimed_keys == frozenset({"widget"})


def test_a_flat_plugin_claims_its_field_names():
    class Settings(BaseModel):
        repo: str | None = None
        git_init: str | None = None

    assert Plugin(name="git", settings=Settings, flat=True).claimed_keys == frozenset(
        {"repo", "git_init"}
    )


def test_looking_up_a_missing_plugin_names_the_ones_that_exist():
    with pytest.raises(plugins.UnknownPlugin, match="unknown environment 'aws'"):
        plugins.get("environments", "aws")


def test_looking_up_a_missing_plugin_type_says_so_rather_than_dying_of_a_key_error():
    """`get` builds its message by asking `names_of` for the type's plugins —
    which is the same failing lookup. So an unknown *type* used to surface as a
    bare `KeyError('nope')` raised while handling `KeyError('nope')`, and the
    helpful sentence was never reached."""
    with pytest.raises(plugins.UnknownPlugin, match="unknown plugin type 'nope'"):
        plugins.get("nope", "x")
