"""The skills a board gave this run, on disk and ready to load.

The board owns which skills a clanker gets; this module is the only thing that
turns that list into something a harness can point at. Content is fetched once
per skill version and cached under the user's state directory, and the plugin
directory itself is keyed by the whole resolved set — so the second task on a
board costs no downloads and no copying, and two boards with different
selections never share a folder.

Nothing here is cleaned up on exit, deliberately: the cache is the point, and
every path in it is content-addressed, so a stale entry is unreachable rather
than wrong.
"""

from __future__ import annotations

import hashlib
import json
import zipfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path, PureWindowsPath

from issuebot.contracts import SkillRef

Fetch = Callable[[str], bytes]

_MANIFEST = {
    "name": "issuebear-board",
    "version": "1.0.0",
    "description": "The skills this board gives its clankers.",
}

# One skill is instructions, not a payload: a board that sends more than this
# is misconfigured, and unpacking it would fill a runner's disk.
MAX_ENTRY_BYTES = 1_048_576
MAX_ENTRIES = 200

# The Unix file-type bits a zip's external_attr encodes in its high 16 bits
# when the entry was written on a POSIX system (the low 16 bits are the DOS
# attributes every zip carries regardless of origin). S_IFLNK == 0o120000.
_ZIP_SYMLINK_TYPE = 0o120000
_ZIP_TYPE_MASK = 0o170000


class SkillBundleError(RuntimeError):
    """A bundle that cannot be trusted or written. Fails the run: the agent
    would otherwise work without the instructions the board requires."""


@dataclass(frozen=True)
class Bundle:
    """What one run's skills came to: a plugin directory, and their prose."""

    plugin_dir: str | None
    _root: Path | None = None

    def body(self, slug: str) -> str:
        """One skill's prose, frontmatter stripped, or "" if it is not here.

        For the callers that cannot load a plugin at all — the tools-free
        `claude -p` that writes PR descriptions has no session to load one
        into, so the guidance is inlined into its prompt instead.
        """
        if self._root is None:
            return ""

        path = self._root / "skills" / slug / "SKILL.md"
        if not path.is_file():
            return ""

        text = path.read_text()
        if text.startswith("---"):
            _, _, rest = text.partition("---")
            _, fenced, after = rest.partition("---")
            if fenced:
                text = after
        return text.strip()


def _key(refs: Sequence[SkillRef]) -> str:
    """A stable name for exactly this set at exactly these versions."""
    material = "\n".join(f"{r.id}@{r.updated_at}" for r in refs)
    return hashlib.sha256(material.encode()).hexdigest()[:16]


def _is_symlink(info: zipfile.ZipInfo) -> bool:
    """Whether a zip entry is a symlink, per the Unix mode stashed in the
    upper 16 bits of ``external_attr`` by every tool that writes one.

    A symlink entry's "content" is just the link target text — unpacking it
    as a regular file would write that text harmlessly, but writing it out as
    an actual symlink (which some archivers/extractors do) would plant a link
    that later reads or writes escape the skill folder through. Refused
    outright rather than trusted to be inert.
    """
    unix_mode = info.external_attr >> 16
    return (unix_mode & _ZIP_TYPE_MASK) == _ZIP_SYMLINK_TYPE


def _escapes(name: str) -> bool:
    """Whether a zip entry's name would land outside the folder it is
    unpacked into.

    Checked three ways because a zip filename is just a string with no
    filesystem semantics attached to it until something interprets it:
    a leading ``/`` (POSIX absolute), a drive letter (``C:\\...`` — the archive
    may have been built on Windows even though we run on POSIX, and Path()
    here would treat the whole thing as one opaque relative segment and miss
    it), and a ``..`` segment anywhere, not just at the front — normalising
    away a ``foo/../..`` still leaves an escape one level up.
    """
    if name.startswith("/") or name.startswith("\\"):
        return True
    if PureWindowsPath(name).drive:
        return True
    return ".." in Path(name).parts


def _safe_members(archive: zipfile.ZipFile) -> list[zipfile.ZipInfo]:
    """The archive's real files, refusing anything that would write outside it.

    The board validates paths on the way in, but this is the side that writes
    to a filesystem: an unpacker that trusts an archive is an unpacker that
    can be handed a different archive.
    """
    members = [i for i in archive.infolist() if not i.is_dir()]
    if len(members) > MAX_ENTRIES:
        raise SkillBundleError(f"skill archive has {len(members)} entries (limit {MAX_ENTRIES})")

    for info in members:
        name = info.filename
        if _escapes(name):
            raise SkillBundleError(f"refusing skill entry outside the folder: {name!r}")
        if _is_symlink(info):
            raise SkillBundleError(f"refusing symlink skill entry: {name!r}")
        # Cheap early reject for an archive that honestly declares itself too
        # big — costs nothing and skips decompressing it at all. It is NOT
        # the guard: `file_size` is read out of the zip's own central
        # directory, which a crafted archive can simply lie about (declare
        # 100 bytes for a deflate stream that expands to gigabytes). The
        # authoritative check is the bounded read in `_unpack` below.
        if info.file_size > MAX_ENTRY_BYTES:
            raise SkillBundleError(f"skill entry {name!r} is too large")

    return members


def _unpack(data: bytes, into: Path) -> None:
    """Write one skill's zip into ``into``, dropping the folder the board
    wrapped it in — the folder here is named for the slug we were given, not
    for whatever the archive happens to call it."""
    with zipfile.ZipFile(BytesIO(data)) as archive:
        for info in _safe_members(archive):
            relative = Path(*Path(info.filename).parts[1:])
            if not relative.parts:
                continue
            target = into / relative
            target.parent.mkdir(parents=True, exist_ok=True)

            # Read bounded by one byte past the cap and measure what actually
            # came out, rather than trusting `info.file_size`: `ZipFile.read`
            # decompresses the whole entry into memory before it ever checks
            # size/CRC, so a declared-small entry with a deflate bomb behind
            # it would otherwise exhaust memory/disk before any guard fired.
            # Bounding the read caps each decompression call's own output to
            # our limit no matter what the header claims or how the entry
            # actually compresses.
            #
            # A header that lies about `file_size` also fails zipfile's own
            # CRC check once the truncated read reaches what it declared —
            # that surfaces as `zipfile.BadZipFile`, not a length mismatch,
            # so it is caught here and folded into the same domain error
            # rather than leaking a stdlib exception out of this module.
            try:
                with archive.open(info) as fh:
                    content = fh.read(MAX_ENTRY_BYTES + 1)
            except zipfile.BadZipFile as exc:
                raise SkillBundleError(f"skill entry {info.filename!r} is corrupt: {exc}") from exc
            if len(content) > MAX_ENTRY_BYTES:
                raise SkillBundleError(f"skill entry {info.filename!r} is too large")

            target.write_bytes(content)


def _cache_root(root: Path | None) -> Path:
    return (root or Path.home() / ".issuebot" / "skills").expanduser()


def build(refs: Sequence[SkillRef], fetch: Fetch, root: Path | None = None) -> Bundle:
    """Materialise these skills and return the plugin directory holding them.

    Idempotent and content-addressed: an existing directory for this exact set
    is reused untouched, which is what makes the second task on a board free.
    """
    if not refs:
        return Bundle(plugin_dir=None)

    base = _cache_root(root) / _key(refs)
    marker = base / ".complete"
    if marker.is_file():
        return Bundle(plugin_dir=str(base), _root=base)

    skills_dir = base / "skills"
    for ref in refs:
        _unpack(fetch(ref.id), skills_dir / ref.slug)

    manifest_dir = base / ".claude-plugin"
    manifest_dir.mkdir(parents=True, exist_ok=True)
    (manifest_dir / "plugin.json").write_text(json.dumps(_MANIFEST))

    # Written last: a directory without it is a half-finished unpack (a crash,
    # a full disk) and is rebuilt rather than loaded.
    marker.write_text("")
    return Bundle(plugin_dir=str(base), _root=base)
