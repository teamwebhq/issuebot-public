"""The bundled skills are portable markdown, shipped harness-neutral in
`issuebot/skills/`; only Claude needs the `.claude-plugin` wrapper, which the
Claude harness synthesises at launch rather than shipping in the package."""

import json
from importlib.resources import files
from pathlib import Path

from issuebot.plugins.harnesses.claude.skills import body, plugin_dir


def test_the_skills_ship_as_plain_content():
    """Only the manifest was ever Claude-shaped; the SKILL.md files are portable,
    so a second harness loads the same content its own way."""
    root = Path(str(files("issuebot").joinpath("skills")))
    assert (root / "board-implementing" / "SKILL.md").is_file()
    assert not list(root.rglob(".claude-plugin"))


def test_claude_gets_a_directory_it_can_load():
    """Claude Code only loads skills via `--plugin-dir`, which needs a manifest
    next to them -- plugin_dir() synthesises that wrapper around the portable
    skill content."""
    directory = plugin_dir()
    assert directory is not None
    manifest = Path(directory) / ".claude-plugin" / "plugin.json"
    assert json.loads(manifest.read_text())["name"] == "issuebot-board"
    assert (Path(directory) / "skills" / "board-implementing" / "SKILL.md").is_file()


def test_skill_body_is_readable_without_its_frontmatter():
    """The PR-writing guidance is inlined into a tools-free `claude -p` prompt,
    which has no skill loader to strip the YAML header for it."""
    text = body("writing-pull-requests")
    assert text
    assert not text.startswith("---")
    assert "name: writing-pull-requests" not in text


def test_an_unknown_skill_body_is_empty_rather_than_an_error():
    """A broken install degrades the prompt, never fails the run -- the same
    bargain plugin_dir() makes when it cannot find the skills at all."""
    assert body("no-such-skill") == ""
