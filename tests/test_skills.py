"""Tests for the bundled skills' own content contract.

This is the skills' own frontmatter, not any one agent CLI's -- every harness
that loads a skill needs its `name:` and `description:` fields, so this lives
here with issuebot's other bundled content rather than beside whichever harness
happens to package it for its own CLI."""

from __future__ import annotations

from importlib.resources import files
from pathlib import Path

import pytest


def _slugs() -> list[str]:
    """Every bundled skill's directory slug.

    Discovered rather than listed, so a skill added to the package is covered by
    the frontmatter contract below without anyone remembering to add it here."""
    root = Path(str(files("issuebot").joinpath("skills")))
    return sorted(d.name for d in root.iterdir() if (d / "SKILL.md").is_file())


@pytest.mark.parametrize("slug", _slugs())
def test_every_skill_carries_the_frontmatter_a_loader_needs(slug: str):
    """Each skill's SKILL.md carries the YAML frontmatter a loader needs to
    register and select it: a `name:` matching its own directory slug, and a
    `description:`. A typo in either would pass every other test in the suite
    and silently break skill selection."""
    root = Path(str(files("issuebot").joinpath("skills")))
    head = (root / slug / "SKILL.md").read_text()

    assert head.startswith("---")
    assert f"name: {slug}" in head
    assert "description:" in head


def test_the_expected_skills_are_all_bundled():
    """The discovery above cannot catch a skill that went *missing* -- an empty
    directory would parametrize to nothing and pass. Name the set explicitly."""
    assert set(_slugs()) == {
        "board-brainstorming",
        "board-implementing",
        "board-planning",
        "writing-pull-requests",
    }
