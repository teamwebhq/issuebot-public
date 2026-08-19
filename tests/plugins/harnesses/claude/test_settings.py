"""This harness's own `[claude]` table, field-checked like every other plugin's.

Lives here rather than in core's config-validation suite: what the table is
*called* and what fields it takes are the plugin's declaration, and the general
claim — a modelled global table is validated, once, whatever plugin owns it — is
made next to the mechanism in `tests/test_config_validation.py`.
"""

from __future__ import annotations

from conftest import source_table
from issuebot.config import Config, harness_settings, validate_config


def test_a_typo_in_the_table_is_caught() -> None:
    cfg = Config.model_validate({"harness": "claude", "claude": {"resume_sessions": "not-a-bool"}})

    problems = validate_config(cfg)

    assert any("[claude]" in p and "resume_sessions" in p for p in problems)


def test_a_well_formed_table_is_accepted() -> None:
    cfg = Config.model_validate(
        {
            "harness": "claude",
            "claude": {"command": "/opt/claude", "resume_sessions": True},
            **source_table(),  # every valid config carries the source's own table
        }
    )

    assert validate_config(cfg) == []


def test_the_table_is_read_back_under_this_harnesss_name() -> None:
    """The `command` override reaches `harness_settings`, which is what
    `harness_for` and `doctor` both read it through."""
    cfg = Config.model_validate({"harness": "claude", "claude": {"command": "/opt/claude"}})

    assert harness_settings(cfg) == {"command": "/opt/claude"}
