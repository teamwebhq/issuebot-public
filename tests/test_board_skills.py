"""Tests for materialising board-provided skills into a plugin directory.

Covers the happy path (fetch, unpack, plugin.json), the cache (a second build
with the same set never refetches), the no-op case (no refs, no plugin dir),
and the archive-safety guards a client that unpacks a zip owes itself
regardless of what the server promises."""

from __future__ import annotations

import io
import struct
import zipfile
from pathlib import Path

import pytest

from issuebot import board_skills
from issuebot.contracts import SkillRef


def _zip(entries):
    """Build an in-memory zip from {name: text}."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, text in entries.items():
            z.writestr(name, text)
    return buf.getvalue()


def test_materialises_a_plugin_dir_with_the_skill_folder(tmp_path):
    refs = (SkillRef(id="s1", slug="board-planning", updated_at="v1"),)
    fetch = {"s1": _zip({"board-planning/SKILL.md": "---\nname: p\n---\nPlan well."})}.get

    bundle = board_skills.build(refs, fetch, root=tmp_path)

    root = Path(bundle.plugin_dir)
    assert (root / ".claude-plugin" / "plugin.json").is_file()
    assert (root / "skills" / "board-planning" / "SKILL.md").read_text().endswith("Plan well.")


def test_body_returns_the_prose_without_frontmatter(tmp_path):
    refs = (SkillRef(id="s1", slug="writing-pull-requests", updated_at="v1"),)
    fetch = {"s1": _zip({"writing-pull-requests/SKILL.md": "---\nname: w\n---\nTitle first."})}.get

    assert board_skills.build(refs, fetch, root=tmp_path).body("writing-pull-requests") == (
        "Title first."
    )


def test_body_returns_empty_string_for_an_unknown_slug(tmp_path):
    refs = (SkillRef(id="s1", slug="board-planning", updated_at="v1"),)
    fetch = {"s1": _zip({"board-planning/SKILL.md": "x"})}.get

    assert board_skills.build(refs, fetch, root=tmp_path).body("does-not-exist") == ""


def test_body_returns_empty_string_when_there_is_no_plugin_dir(tmp_path):
    assert board_skills.build((), lambda _: b"", root=tmp_path).body("anything") == ""


def test_a_second_build_does_not_refetch(tmp_path):
    calls = []

    def fetch(skill_id):
        calls.append(skill_id)
        return _zip({"board-planning/SKILL.md": "x"})

    refs = (SkillRef(id="s1", slug="board-planning", updated_at="v1"),)
    board_skills.build(refs, fetch, root=tmp_path)
    board_skills.build(refs, fetch, root=tmp_path)

    assert calls == ["s1"]


def test_a_changed_updated_at_refetches_into_a_different_directory(tmp_path):
    refs_v1 = (SkillRef(id="s1", slug="board-planning", updated_at="v1"),)
    refs_v2 = (SkillRef(id="s1", slug="board-planning", updated_at="v2"),)
    fetch = lambda _id: _zip({"board-planning/SKILL.md": "x"})  # noqa: E731

    bundle1 = board_skills.build(refs_v1, fetch, root=tmp_path)
    bundle2 = board_skills.build(refs_v2, fetch, root=tmp_path)

    assert bundle1.plugin_dir != bundle2.plugin_dir


def test_no_refs_means_no_plugin_dir(tmp_path):
    assert board_skills.build((), lambda _: b"", root=tmp_path).plugin_dir is None


def test_a_traversing_entry_is_refused(tmp_path):
    refs = (SkillRef(id="s1", slug="evil", updated_at="v1"),)
    fetch = {"s1": _zip({"../escaped.md": "no"})}.get

    with pytest.raises(board_skills.SkillBundleError):
        board_skills.build(refs, fetch, root=tmp_path)


def test_an_absolute_entry_is_refused(tmp_path):
    refs = (SkillRef(id="s1", slug="evil", updated_at="v1"),)
    fetch = {"s1": _zip({"/etc/passwd": "no"})}.get

    with pytest.raises(board_skills.SkillBundleError):
        board_skills.build(refs, fetch, root=tmp_path)


def test_a_windows_drive_absolute_entry_is_refused(tmp_path):
    refs = (SkillRef(id="s1", slug="evil", updated_at="v1"),)
    fetch = {"s1": _zip({"C:/evil/escaped.md": "no"})}.get

    with pytest.raises(board_skills.SkillBundleError):
        board_skills.build(refs, fetch, root=tmp_path)


def test_a_dotdot_hidden_inside_a_later_segment_is_refused(tmp_path):
    refs = (SkillRef(id="s1", slug="evil", updated_at="v1"),)
    fetch = {"s1": _zip({"evil/subdir/../../escaped.md": "no"})}.get

    with pytest.raises(board_skills.SkillBundleError):
        board_skills.build(refs, fetch, root=tmp_path)


def test_a_symlink_entry_is_refused(tmp_path):
    refs = (SkillRef(id="s1", slug="evil", updated_at="v1"),)

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        info = zipfile.ZipInfo("evil/link")
        # Unix mode for a symlink (S_IFLNK | 0o777) shifted into the external
        # attr's high 16 bits, exactly how zip tools mark symlink entries.
        info.external_attr = 0o120777 << 16
        z.writestr(info, "/etc/passwd")

    fetch = {"s1": buf.getvalue()}.get

    with pytest.raises(board_skills.SkillBundleError):
        board_skills.build(refs, fetch, root=tmp_path)


def test_a_zip_with_too_many_entries_is_refused(tmp_path):
    refs = (SkillRef(id="s1", slug="evil", updated_at="v1"),)
    entries = {f"evil/f{i}.md": "x" for i in range(board_skills.MAX_ENTRIES + 1)}
    fetch = {"s1": _zip(entries)}.get

    with pytest.raises(board_skills.SkillBundleError):
        board_skills.build(refs, fetch, root=tmp_path)


def test_an_oversized_entry_is_refused(tmp_path):
    """The cheap, honest case: the header truthfully declares an entry bigger
    than the cap, and the early declared-size check in `_safe_members` rejects
    it before any bytes are decompressed. This does NOT exercise the bounded
    read in `_unpack` — see `test_a_declared_size_lie_is_refused` below for
    the case where the header itself cannot be trusted."""
    refs = (SkillRef(id="s1", slug="evil", updated_at="v1"),)
    big = "x" * (board_skills.MAX_ENTRY_BYTES + 1)
    fetch = {"s1": _zip({"evil/big.md": big})}.get

    with pytest.raises(board_skills.SkillBundleError):
        board_skills.build(refs, fetch, root=tmp_path)


def _zip_with_lying_declared_size(name: str, real_content: bytes, declared_size: int) -> bytes:
    """Build a real, decompressible zip entry and then hand-patch its
    `uncompressed size` field — in both the local file header and the central
    directory record, which is what `ZipFile.infolist()` reads — down to
    `declared_size`.

    This is deliberate forgery: `zipfile.ZipFile.writestr` always computes an
    honest `file_size` from what you actually gave it, so there is no way to
    produce a lying header through the public write API. The only way to
    prove the guard against a lying header is to write the lie into the
    archive's own bytes after the fact, the same way an attacker would.
    """
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as z:
        z.writestr(name, real_content)
    data = bytearray(buf.getvalue())

    # Local file header: signature(4) ... crc32(4) compressed(4) uncompressed(4) ...
    # -- uncompressed size sits 22 bytes past the 'PK\x03\x04' signature.
    local = data.find(b"PK\x03\x04")
    struct.pack_into("<I", data, local + 22, declared_size)

    # Central directory header: uncompressed size sits 24 bytes past 'PK\x01\x02'
    # -- this is the value `ZipInfo.file_size` (and our cheap early check)
    # actually reads, since `infolist()` is built from the central directory.
    central = data.find(b"PK\x01\x02")
    struct.pack_into("<I", data, central + 24, declared_size)

    return bytes(data)


def test_a_declared_size_lie_is_refused(tmp_path):
    """The critical case: a header that LIES about `file_size`, declaring a
    small entry while the real (correctly-CRC'd) decompressed content is
    several times the cap.

    A declared size at or under the cap sails past the cheap early check in
    `_safe_members` — that check only ever sees the (forged) small number.
    Reaching `_unpack`, the archive's real CRC no longer matches what a
    bounded read produces once it hits the (small) declared length, which
    surfaces from zipfile as `BadZipFile`; `_unpack` folds that into
    `SkillBundleError` rather than leaking it. The point of this test is not
    that particular exception, though -- it is that this construction, run
    against the *unbounded* `archive.read(info)` this module used before the
    fix, decompresses the real (multi-megabyte) content into memory in one
    call before any check ever runs. The bounded `fh.read(MAX_ENTRY_BYTES + 1)`
    caps what any single decompression call can produce, regardless of what
    the header declares or how the entry actually compresses.
    """
    refs = (SkillRef(id="s1", slug="evil", updated_at="v1"),)
    real_content = b"A" * (5 * board_skills.MAX_ENTRY_BYTES)  # highly compressible
    lying_zip = _zip_with_lying_declared_size("evil/big.md", real_content, declared_size=10)
    fetch = {"s1": lying_zip}.get

    with pytest.raises(board_skills.SkillBundleError):
        board_skills.build(refs, fetch, root=tmp_path)
