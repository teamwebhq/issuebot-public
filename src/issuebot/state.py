"""Everything issuebot persists, written the same way every time.

There were six state files, in four shapes, with the XDG path expression
copy-pasted eight times. They disagreed on atomicity, on permissions and on
whether a corrupt file raised or read as empty — and the disagreements ran the
wrong way round: the disposable status file wrote atomically while session
resume tokens truncated in place.

One implementation, one decision each:

* **Atomic.** Every write is temp-file-then-``os.replace``, so a crash mid-write
  leaves the previous contents rather than a truncated file.
* **Private.** Files are 0600 and directories 0700. Runner state holds resume
  tokens, identity and — via the config file — the PAT; the run logs hold the
  agent's full transcript. None of it is world-readable.
* **Never fatal.** A missing, unreadable or corrupt file reads as empty and a
  failed write is logged. Losing this state degrades a feature; it must not take
  the runner down.

Three ways in, by what the caller holds:

* :class:`StateFile` — one file, replaced whole (JSON or text).
* :class:`KeyedStore` — a flat ``key -> value`` map in one file.
* :func:`private_dir` / :func:`open_private` — for the one thing that cannot be
  written whole, the append-as-you-go run log.

A store on top of these is its domain logic and nothing else.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path
from typing import Any, TextIO

logger = logging.getLogger("issuebot")

# Serialises the read-modify-write in `KeyedStore`. Several runs are in flight
# at once (the listener's pool, and every listener sharing one session store),
# and each `set` reads the whole map, edits it and writes it back — without this
# the last writer wins and every concurrent update is lost.
#
# ponytail: one lock for every keyed store rather than one per path. A handful
# of writes per run means contention is not a thing that can be measured; move
# to a per-path lock if that ever stops being true. It does NOT serialise two
# issuebot processes sharing a state directory — that needs a file lock, and
# one runner per agent identity is the documented shape.
_KEYED_LOCK = threading.Lock()

# Runner state is private: it holds resume tokens, identity, the PAT and the
# agent's transcripts. Anything issuebot writes gets these permissions, not just
# the files somebody remembered to chmod.
_MODE = 0o600
_DIR_MODE = 0o700


def state_dir() -> Path:
    """``$XDG_STATE_HOME/issuebot``, falling back to ``~/.local/state/issuebot``.

    The one place this expression lives — every path issuebot writes under the
    state directory is built from here."""
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / "issuebot"


def config_dir() -> Path:
    """``$XDG_CONFIG_HOME/issuebot``, falling back to ``~/.config/issuebot``.

    Config is user-authored rather than runner state, so it lives on the config
    axis — but it holds the PAT, so it gets the same private, atomic treatment."""
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "issuebot"


def private_dir(path: Path) -> Path:
    """Create ``path`` (and parents) at 0700 and return it.

    For the directories issuebot owns outright — state, config, logs, clones.
    ``mkdir``'s mode is masked by the umask, so the mode is applied explicitly
    afterwards to a directory that may already have existed."""
    path.mkdir(parents=True, exist_ok=True)
    try:
        path.chmod(_DIR_MODE)
    except OSError:  # a directory we don't own (an existing clone root) is fine
        logger.debug("could not tighten permissions on %s", path, exc_info=True)
    return path


def open_private(path: Path) -> TextIO:
    """Open ``path`` for writing at 0600, creating its directory.

    The escape hatch from :class:`StateFile` for the one thing that genuinely
    streams: the per-run log, which is appended to for the life of a run and so
    cannot be written whole. Raises ``OSError`` — the run log's caller wants to
    know, because it degrades to console-only output."""
    private_dir(path.parent)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, _MODE)
    return os.fdopen(fd, "w", encoding="utf-8")


def state_path(name: str, *, env_override: str | None = None) -> Path:
    """The path for a named state file, honouring an optional env override.

    ``env_override`` names an environment variable holding a full path — an
    escape hatch available to every store alike, which tests lean on heavily.
    """
    if env_override:
        override = os.environ.get(env_override)
        if override:
            return Path(override)
    return state_dir() / name


class StateFile:
    """A single file of runner state.

    Writes are atomic (temp file, then ``os.replace``) so a crash mid-write
    leaves the previous contents rather than a truncated file. Reads and writes
    are best-effort: a missing, unreadable or corrupt file reads as empty and a
    failed write is logged, never raised. Losing this state degrades a feature —
    a session isn't resumed, a checkpoint isn't pruned — and must never take the
    runner down with it.
    """

    def __init__(self, path: Path) -> None:
        self._path = path

    @property
    def path(self) -> Path:
        return self._path

    # -- text ---------------------------------------------------------------

    def read_text(self) -> str | None:
        """The file's contents, or None if it is absent or unreadable."""
        try:
            return self._path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except OSError:
            logger.warning("state file at %s is unreadable; treating as absent", self._path)
            return None

    def write_text(self, text: str) -> None:
        """Replace the file's contents atomically, at 0600."""
        try:
            private_dir(self._path.parent)
            # A per-thread temp name: two writers of the same state file must not
            # race on the same intermediate path. Per-process was not enough —
            # concurrent runs live in threads of one process, and sharing the
            # name means one thread's O_TRUNC lands in the other's file and one
            # `os.replace` finds nothing there at all.
            unique = f"{os.getpid()}.{threading.get_ident()}"
            tmp = self._path.with_name(f"{self._path.name}.{unique}.tmp")
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, _MODE)
            try:
                os.write(fd, text.encode("utf-8"))
            finally:
                os.close(fd)
            # os.replace is atomic within a filesystem, so a reader sees either
            # the old contents or the new ones, never a half-written file.
            os.replace(tmp, self._path)
            os.chmod(self._path, _MODE)
        except OSError:
            logger.warning("could not write state file at %s", self._path, exc_info=True)

    # -- json ---------------------------------------------------------------

    def read_json(self) -> dict[str, Any]:
        """The file as a JSON object, or ``{}`` if absent, unreadable or corrupt."""
        text = self.read_text()
        if text is None:
            return {}
        try:
            data = json.loads(text)
        except ValueError:
            logger.warning("state file at %s is not valid JSON; treating as empty", self._path)
            return {}
        return data if isinstance(data, dict) else {}

    def write_json(self, data: dict[str, Any]) -> None:
        """Replace the file with ``data`` as JSON."""
        self.write_text(json.dumps(data))

    # -- lifecycle ----------------------------------------------------------

    def delete(self) -> None:
        """Remove the file, tolerating one that is already gone."""
        try:
            self._path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            logger.warning("could not remove state file at %s", self._path, exc_info=True)


class KeyedStore:
    """A state file holding a flat ``key -> value`` map.

    Load-modify-write on each mutation, under :data:`_KEYED_LOCK` — several runs
    are in flight at once and they share these files (one session store across
    every listener; one checkpoint file across every sandbox), so an unguarded
    read-edit-write drops whichever update lost the race. Both keyed stores in
    the runner — task sessions and task checkpoints — are this, plus a few lines
    of meaning.
    """

    def __init__(self, path: Path) -> None:
        self._file = StateFile(path)

    @property
    def path(self) -> Path:
        return self._file.path

    def all(self) -> dict[str, Any]:
        """The whole map (empty if the file is missing or corrupt)."""
        return self._file.read_json()

    def get(self, key: str) -> Any:
        return self.all().get(key)

    def set(self, key: str, value: Any) -> None:
        """Add or replace one entry, leaving every concurrent one intact."""
        with _KEYED_LOCK:
            data = self._file.read_json()
            data[key] = value
            self._file.write_json(data)

    def drop(self, key: str) -> None:
        """Remove one entry. A no-op when it isn't there."""
        with _KEYED_LOCK:
            data = self._file.read_json()
            if data.pop(key, None) is None:
                return
            self._file.write_json(data)

    def clear(self) -> None:
        """Drop every entry."""
        self._file.write_json({})
