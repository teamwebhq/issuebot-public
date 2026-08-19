"""The docs are checked against the code, not reviewed against memory.

Nothing else in the suite reads them, so every claim in them was only ever as
true as the last person to look. That is how the README came to document a
`issuebot worktree` that had become `issuebot git worktree`, an install command
for a package registry the project will never be on, and config keys three
plugins ago. A doc example that `load_config` rejects, or a flag that no longer
exists, teaches the wrong thing more confidently than no example at all.

Two files are checked today, and `DOCS` finds them rather than listing them:
`README.md`, which is how to set issuebot up and use it, and
`docs/ARCHITECTURE.md`, which is how it is built. The split is by audience, not
by trustworthiness — architecture prose names commands and flags exactly as
confidently as setup prose does, and rots exactly as quietly, so it is held to
the same four checks.

Four checks, each aimed at a failure that actually happened:

* every complete config example loads and validates for real;
* every command and flag the docs show resolves against the live CLI;
* every flag they name in prose belongs to *some* command;
* every internal link lands on a heading that exists **in its own file**.

The first three read the two files as one corpus: a command is documented if it
is written down *somewhere*, and it does not matter which file an example sits
in. The fourth cannot — an anchor is relative to the file that contains it, and
resolving `README.md`'s links against `ARCHITECTURE.md`'s headings would forgive
exactly the broken link the check exists to catch — so it runs per file.

Each check asserts a floor on how much it found, because a check that passes on
an empty document is not a check. The floors are over the corpus for the same
reason the checks are: moving a section between the two files is not supposed to
change what is verified.

**The docs name plugins, and naming one must not make it load-bearing.** An
earlier version of this file turned that prose into an executable dependency on
three plugin directories: deleting one sink made a config example fail to
validate, and deleting an environment broke a command, a flag and a config at
once. So the examples below use placeholders rather than real plugin names, and
every check resolves against the installed registry rather than a fixed list.

So every check here resolves against the *installed* registry and skips only
what this build genuinely no longer has: a config naming an uninstalled plugin,
an invocation whose first word exists nowhere in the command tree. Skipping is
deliberately narrow — see `_live` for why "not a live command" would have been
too wide, and forgiven the one failure this file was written for. What is
installed is checked exactly as strictly as before. The one thing prose must not
do is name a plugin command's flag *bare* — see `NOT_OURS` — so those are
written attached to their command instead.
"""

from __future__ import annotations

import re
import shlex
from pathlib import Path

import pytest
import typer.main

from issuebot.cli import app
from issuebot.config import load_config

ROOT = Path(__file__).resolve().parents[1]

# Every document held to the checks below: the README, plus every markdown file
# directly under `docs/`.
#
# Discovered rather than listed, because a list is silently droppable. The
# floors below are over the corpus and the README alone clears every one of
# them, so removing `docs/ARCHITECTURE.md` from a hand-written list would leave
# 6/6 passing while that file quietly stopped being checked — the same shape as
# a suite that keeps passing after it stops covering anything. Nothing can
# assert a floor against that; only not having a list can.
#
# Non-recursive on purpose: `docs/adr/` and `docs/superpowers/` are decision
# records and planning notes, not documentation of the shipped CLI.
DOCS = [ROOT / "README.md", *sorted((ROOT / "docs").glob("*.md"))]

# A fenced block that is deliberately not a whole file says so on its first
# line. Marking the partial ones (rather than trying to guess which blocks are
# complete) keeps the default strict: a new example is validated unless its
# author says why it cannot be.
FRAGMENT = re.compile(r"^#\s*fragment\b", re.MULTILINE)

# ```<info> \n <body> ``` — the info string picks which checker gets the body.
FENCE = re.compile(r"^```([\w-]*)\n(.*?)^```", re.MULTILINE | re.DOTALL)

# `like this`, including a span wrapped across a line break in the source.
INLINE_CODE = re.compile(r"`([^`]+)`")

# A markdown link into this same document.
ANCHOR = re.compile(r"\]\(#([^)]+)\)")

# An ATX heading, with its level.
HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*$", re.MULTILINE)

# A stand-in for something the reader supplies (`<board-id>`) or for the rest of
# a command line (`issuebot <name> …`). Not a subcommand, and not checkable.
PLACEHOLDER = re.compile(r"^<.+>$|…")


def _text() -> str:
    """Every checked document, as one string.

    Joined with a newline so a file that does not end in one cannot glue its
    last line onto the next file's first — which would hide a heading and
    invent a command line at the seam.
    """
    return "\n".join(path.read_text() for path in DOCS)


def _shipped_source_files() -> list[Path]:
    """Readable source assets that ship inside the issuebot package."""
    source = ROOT / "src" / "issuebot"
    return sorted(
        path for path in source.rglob("*") if path.is_file() and path.suffix in {".md", ".py"}
    )


def test_readme_installs_the_latest_immutable_release() -> None:
    """The public installer must follow the latest complete GitHub Release."""
    readme = (ROOT / "README.md").read_text()

    assert "/releases/latest/download/install.sh" in readme
    assert "raw.githubusercontent.com/teamwebhq/issuebot-public/main/install.sh" not in readme
    assert "version --commit" not in readme


def test_release_vocabulary_scan_includes_shipped_markdown_assets() -> None:
    """A shipped prompt must remain inside the release-vocabulary boundary."""
    prompt = ROOT / "src" / "issuebot" / "plugins" / "sources" / "issuebear" / "templates"

    assert prompt / "work_a_task.md" in _shipped_source_files()


def test_released_code_uses_version_identity_only() -> None:
    """Shipped surfaces must retain release-version identity exclusively."""
    paths = [
        *_shipped_source_files(),
        ROOT / "README.md",
        ROOT / "docs" / "ARCHITECTURE.md",
        ROOT / "install.sh",
        ROOT / "pyproject.toml",
    ]
    forbidden = (
        "ISSUEBOT_COMMIT",
        "commit_argv",
        "parse_commit",
        "head_commit",
        "version --commit",
        "hatch_build.py",
    )

    found = {
        str(path.relative_to(ROOT)): [term for term in forbidden if term in path.read_text()]
        for path in paths
    }
    assert not (matches := {path: terms for path, terms in found.items() if terms}), matches


def _blocks(language: str) -> list[str]:
    """Every fenced block tagged with `language`."""
    return [body for info, body in FENCE.findall(_text()) if info == language]


# ---------------------------------------------------------------------------
# Config examples
# ---------------------------------------------------------------------------


def test_every_config_example_in_the_readme_is_valid(tmp_path: pytest.TempPathFactory) -> None:
    """An example that fails validation teaches the wrong thing, and nothing
    else in the suite reads the docs.

    The floor counts examples *found*, not examples checked, so a build with a
    plugin deleted cannot satisfy it by having nothing left to validate. An
    example naming a plugin this build does not have is skipped, not failed:
    documenting a plugin must not make it undeletable (see the module docstring).
    """
    complete = [body for body in _blocks("toml") if not FRAGMENT.search(body)]

    assert len(complete) >= 3, "the docs should show several complete configs"

    for index, body in enumerate(complete):
        path = Path(str(tmp_path)) / f"example-{index}.toml"
        path.write_text(body)
        try:
            loaded = load_config(path)
        except Exception as exc:  # noqa: BLE001 - report which example, and why
            pytest.fail(f"config example {index + 1} does not load:\n{body}\n\n{exc}")
        assert loaded is not None


# ---------------------------------------------------------------------------
# Commands and flags
# ---------------------------------------------------------------------------


def _invocations() -> list[list[str]]:
    """Every `issuebot …` command line the docs show, tokenised.

    Both places one can appear: a shell fence, and an inline code span in prose
    (which is where `issuebot worktree prune` outlived the rename). A leading
    `uv run` is stripped so a checkout-relative example is checked as the same
    command.
    """
    lines = [line for body in _blocks("sh") for line in body.replace("\\\n", " ").splitlines()]

    # Fences are removed before looking for inline spans: a fence delimiter is
    # three backticks, so an untagged block of sample *output* otherwise reads
    # to this regex as one enormous inline span.
    prose = FENCE.sub("", _text())
    lines += [" ".join(span.split()) for span in INLINE_CODE.findall(prose)]

    found: list[list[str]] = []
    for line in lines:
        stripped = line.strip().removeprefix("$ ").strip()
        stripped = stripped.removeprefix("uv run ").strip()
        if not stripped.startswith("issuebot") or "|" in stripped.split("issuebot", 1)[0]:
            continue
        try:
            tokens = shlex.split(stripped, comments=True)
        except ValueError:
            continue  # unbalanced quotes: prose, not a command line
        if tokens and tokens[0] == "issuebot":
            found.append(tokens[1:])
    return found


def _root() -> object:
    """The live CLI's root group."""
    return typer.main.get_command(app)


def _every_command_name() -> set[str]:
    """Every command name anywhere in the live tree, at any depth."""
    names: set[str] = set()
    pending = [_root()]
    while pending:
        children = _subcommands(pending.pop())
        names |= set(children)
        pending += list(children.values())
    return names


def _live(tokens: list[str]) -> bool:
    """Whether this invocation is one this build can be held to.

    An `issuebot <plugin> …` line documents commands a build may not have, and a
    README that documents a plugin must not be the thing that stops it being
    deleted. But "first word is not a live command" is too blunt a test for
    that: it is equally true of `issuebot worktree list` — the *wrong path* for a
    command that does exist, and the single failure this file was written to
    catch — which would then be forgiven rather than reported.

    The discriminator is whether the word survives anywhere in the tree. A
    mis-pathed command names a group that still exists, just not there
    (`worktree` under `git`, `session` under `claude`), so it is checked and
    fails. A deleted plugin's command names something that exists nowhere at
    all, so it is skipped. A line that is only flags is always checked.
    """
    if not tokens or tokens[0].startswith("-"):
        return True
    return tokens[0] in _subcommands(_root()) or tokens[0] in _every_command_name()


def _subcommands(command: object) -> dict[str, object]:
    """The child commands of a group, or {} for a leaf.

    Duck-typed rather than `isinstance(command, click.Group)`: typer vendors its
    own click, so the class this walk would have to name is a private module of
    a dependency. What both shapes agree on is `.commands`.
    """
    return getattr(command, "commands", None) or {}


def _resolve(tokens: list[str]) -> tuple[object, list[str]]:
    """Walk the live CLI to the command these tokens name, and its remaining tokens.

    Descends only while a token is a real subcommand of a real group, so a
    positional argument (`issuebot logs ISS-42`) stops the walk rather than
    being mistaken for a command that does not exist.
    """
    command: object = _root()
    index = 0
    while index < len(tokens) and tokens[index] in _subcommands(command):
        command = _subcommands(command)[tokens[index]]
        index += 1
    return command, tokens[index:]


def _documented_options(tokens: list[str]) -> list[str]:
    """The option names in one command line, as the CLI would see them.

    Pseudo-syntax the README uses to show alternatives is dropped: `|` separates
    forms, and `[--force]` marks one optional. Both are prose about the command
    rather than part of it, and neither changes which flag is being claimed to
    exist.
    """
    options = []
    for token in tokens:
        cleaned = token.strip("[]")
        if cleaned == "|" or not cleaned.startswith("-"):
            continue
        options.append(cleaned.partition("=")[0])
    return options


def test_every_command_the_cli_offers_is_documented() -> None:
    """The other half of the check below, and the half that makes skipping safe.

    Skipping an invocation whose first word is not a live command is what keeps
    a documented plugin deletable — but on its own it would also swallow the
    exact failure this file was written for: rename `issuebot git worktree` to
    `issuebot worktree` in the README and the line becomes unresolvable, which
    the skip would quietly forgive.

    Coming at it from the CLI's side closes that. A renamed command leaves its
    real name documented nowhere, and this fails naming it. A *deleted* plugin's
    command is not in this list to begin with, so the property survives. It also
    catches the plainer thing: a command nobody wrote down.
    """
    documented = _text()
    missing = [
        name for name in sorted(_subcommands(_root())) if f"issuebot {name}" not in documented
    ]

    assert not missing, f"commands this build offers that the docs never mention: {missing}"


def test_every_command_the_readme_shows_exists() -> None:
    """A README naming a command that was renamed is worse than one that omits
    it — `issuebot worktree` outlived its own rename by an entire refactor."""
    invocations = _invocations()

    assert len(invocations) >= 10, "these are a CLI's docs; they should show commands"

    for tokens in filter(_live, invocations):
        command, rest = _resolve(tokens)
        children = _subcommands(command)
        leftover = [
            token
            for token in rest
            if not token.startswith("-") and token != "|" and not PLACEHOLDER.search(token)
        ]
        if children and leftover:
            pytest.fail(
                f"'issuebot {' '.join(tokens)}' names no such command: "
                f"'{leftover[0]}' is not one of {sorted(children)}"
            )


def test_every_flag_the_readme_shows_exists() -> None:
    """Same failure one level down: a flag that moved to `--set`, or went with
    the setting it configured, reads exactly like one that still works."""
    checked = 0

    for tokens in filter(_live, _invocations()):
        command, rest = _resolve(tokens)
        params = getattr(command, "params", [])
        # `--help` is added by click at parse time rather than declared as a
        # param, so it is not in `params` on any command — and it is valid on
        # every one of them.
        known = {opt for param in params for opt in param.opts + param.secondary_opts} | {"--help"}
        for option in _documented_options(rest):
            assert option in known, (
                f"'issuebot {' '.join(tokens)}' documents {option}, which "
                f"'{getattr(command, 'name', '?')}' does not take "
                f"(it takes: {', '.join(sorted(known))})"
            )
            checked += 1

    assert checked >= 10, "the docs should show flags, not just bare commands"


# A flag written on its own, with no command beside it: a table row, or prose
# ("pass `--force` to override").
BARE_FLAG = re.compile(r"^-{1,2}[a-z][a-z-]*$")

# Flags of the agent CLIs issuebot drives, which appear in prose because the
# README explains what issuebot passes to them. They are Claude Code's, not
# ours, and no amount of looking in our own registry will find them.
#
# This set is only for *another tool's* flags. A flag of one of issuebot's own
# plugin-mounted commands must not be written bare — it would be unfindable the
# moment that plugin were deleted, which is the coupling this file exists not to
# have — so those are written attached to their command
# (`issuebot git worktree prune --force`) and checked by the test above.
#
# Exactly the ones the corpus names, no more: five of these were carried for a
# README that no longer mentions them, and a whitelist entry nothing needs is
# a forgiveness waiting for the wrong flag to land on it.
NOT_OURS = frozenset(
    {
        "--dangerously-skip-permissions",
        "--plugin-dir",
        "--strict-mcp-config",
    }
)


def test_every_flag_the_readme_names_in_prose_belongs_to_some_command() -> None:
    """The largest concentration of flags in the README is a table, and a table
    cell has no command beside it for the check above to resolve against.

    That is exactly where a stale flag hides: the old README documented
    `--integrate` in a fenced example *and* in prose, and removing it from one
    would have left the other. So every bare flag is resolved against the union
    of every param on every live command — weaker than the per-command check,
    but it catches the whole class of "this flag no longer exists anywhere".
    """
    prose = FENCE.sub("", _text())
    named = {span for span in INLINE_CODE.findall(prose) if BARE_FLAG.fullmatch(span)}

    assert len(named) >= 10, "the docs document a CLI's flags; they should name them"

    known = {"--help"}
    pending: list[object] = [_root()]
    while pending:
        command = pending.pop()
        for param in getattr(command, "params", []):
            known |= set(param.opts) | set(param.secondary_opts)
        pending += list(_subcommands(command).values())

    for flag in sorted(named - NOT_OURS):
        assert flag in known, f"the docs name {flag}, which no installed command takes"


# ---------------------------------------------------------------------------
# Internal links
# ---------------------------------------------------------------------------


def _slug(heading: str) -> str:
    """GitHub's heading slug: lowercased, punctuation dropped, spaces hyphenated."""
    text = heading.strip().lower()
    text = re.sub(r"[^\w\s-]", "", text.replace("`", ""))
    return re.sub(r"\s+", "-", text).strip("-")


def test_every_internal_link_in_the_readme_resolves() -> None:
    """One of these broke the moment a heading was renamed without its link,
    and nothing noticed until someone clicked it.

    Per file, not over the corpus: `](#foo)` is relative to the document it sits
    in, so a README link to a heading that now lives in `ARCHITECTURE.md` is
    broken and must be reported as broken. Resolving both files' anchors against
    both files' headings would call it fine — which is precisely the move that
    splitting the docs made possible.
    """
    found = 0

    for path in DOCS:
        text = path.read_text()
        slugs = {_slug(title) for _, title in HEADING.findall(text)}
        anchors = ANCHOR.findall(text)
        found += len(anchors)

        for anchor in anchors:
            assert anchor in slugs, (
                f"{path.name}: '#{anchor}' matches no heading in that file "
                f"(have: {sorted(slugs)}) — a cross-file link needs the filename too"
            )

    assert found >= 5, "the docs have a contents section; they should link into themselves"
