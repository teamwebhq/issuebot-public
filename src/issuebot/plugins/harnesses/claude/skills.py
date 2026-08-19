"""Synthesise the Claude plugin directory that wraps issuebot's bundled skills.

The skills themselves (`issuebot/skills/`) are plain markdown, portable to any
harness. Claude Code only loads skills via `--plugin-dir`, which requires a
`.claude-plugin/plugin.json` manifest sitting next to them, so this module
copies the bundled skills into a temp dir alongside a generated manifest --
the same trick the Claude harness already uses to write a temporary
`mcp.json` per launch, and (when `dest` is passed) the *same* temp dir,
cleaned up on the same schedule.
"""

from __future__ import annotations

import json
import shutil
import tempfile
from importlib.resources import files
from pathlib import Path

# The manifest is the only Claude-specific artifact in this whole bundle; the
# skill content underneath is harness-neutral.
_MANIFEST = {
    "name": "issuebot-board",
    "version": "0.1.0",
    "description": (
        "Board-native agent skills for the issuebot runner: brainstorm and "
        "implement Issuebear board tasks through task comments and updates."
    ),
}


def _root() -> Path | None:
    """The bundled skills directory, or None on an install that cannot expose it
    as a real path (a broken or zipimported one)."""
    try:
        root = Path(str(files("issuebot").joinpath("skills")))
    except (ModuleNotFoundError, TypeError):
        return None
    return root if root.is_dir() else None


def body(slug: str) -> str:
    """One bundled skill's prose, with its YAML frontmatter stripped, or ``""``.

    For the callers that cannot go through a skill *loader* at all: the PR
    description is written by a tools-free, MCP-free `claude -p` (see
    ``ClaudeHarness.summarize``), which loads no plugin and so would never reach
    `writing-pull-requests` on its own. Inlining the body into that prompt keeps
    the SKILL.md the single place the guidance is written, editable by anyone who
    edits the other skills, rather than a second copy living in a Python string.

    Empty for a missing skill or an install with no readable skills directory --
    the same bargain :func:`plugin_dir` makes, degrading the prompt rather than
    failing the run.
    """
    root = _root()
    if root is None:
        return ""

    skill = root / slug / "SKILL.md"
    if not skill.is_file():
        return ""

    text = skill.read_text()

    # Frontmatter is the loader's business, not the model's: a `---` fence at the
    # very top runs to the next one, and everything after that fence is prose.
    if text.startswith("---"):
        _, _, rest = text.partition("---")
        _, fenced, after = rest.partition("---")
        if fenced:
            text = after

    return text.strip()


def plugin_dir(dest: str | Path | None = None) -> str | None:
    """Absolute path to a Claude plugin directory wrapping the bundled
    skills, or None if the skills cannot be located (e.g. a broken or
    zipimported install -- the caller then launches without them, degrading
    behaviour rather than failing the run).

    ``dest``, if given, is populated in place and returned; the caller owns
    its lifetime. The Claude harness passes its own per-launch temp dir here
    so the copy is cleaned up alongside `mcp.json` when that launch ends,
    rather than leaking a directory per launch (and per retry, for a
    long-lived `issuebot listen` process). With no ``dest``, a fresh temp dir
    is created and returned, for standalone callers that don't already have
    one to reuse (e.g. this module's own tests)."""
    skills = _root()
    if skills is None:
        return None

    root = Path(dest) if dest is not None else Path(tempfile.mkdtemp(prefix="issuebot-skills-"))
    # copytree creates `root` itself via makedirs, so it need not pre-exist.
    shutil.copytree(skills, root / "skills")
    manifest_dir = root / ".claude-plugin"
    manifest_dir.mkdir()
    (manifest_dir / "plugin.json").write_text(json.dumps(_MANIFEST))
    return str(root)
