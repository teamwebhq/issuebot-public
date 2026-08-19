"""The Workspace ABC: where a task's working copy is prepared, and how its
result is derived.

An ABC rather than a Protocol, matching :class:`~issuebot.plugins.harnesses.base.
Harness`: every workspace must actually subclass this (checked by the
conformance suite), not merely happen to match its shape.

Implementations live in sibling directories, one per workspace plugin — this
module names none of them, so any one can be deleted. Workspace strategy is
not a permission: a workspace declares which output kinds a run in it could
possibly produce (``produces``), and a workspace with no version control to
diff against cannot derive `Changes`, so it can never produce `changes`.

``permits = source.permits(work) ∩ workspace.produces`` is computed once, by
``runner.job_for``, when it builds the job — so a folder connection's run is
never *told* it may report ``changes``, and is held to the same narrowed set
when its response comes back. ``runner.workspace_for`` is what resolves a
connection onto a real ``Workspace`` instance for that.

ponytail: the intersection could also run at *config* load, so an impossible
combination is rejected before a run rather than silently narrowed during one.
Per-run narrowing is the half that makes the agent's instructions honest; the
config-load rejection needs a `validate` hook that can see both plugins at
once, which no axis has yet (ADR-0011).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar

from pydantic import BaseModel

from issuebot.process import REAL, Process

if TYPE_CHECKING:
    from issuebot.config import Connection
    from issuebot.contracts import Changes, OutputKind
    from issuebot.reporter import Reporter


@dataclass(frozen=True)
class WorkspaceProblem:
    """A condition :meth:`Workspace.prepare` found that the run can proceed
    through only if the agent is told about it first.

    Data, not an exception: raising would make the runner read it as an
    ordinary prep failure, when the workspace is in fact launchable and the
    problem is the agent's to resolve in it. The runner routes it back through
    ``Source.prompt``, since only a source knows how to phrase instructions.

    ``kind`` is neutral vocabulary (nothing plugin-specific): today the git
    workspace reports ``"diverged-branch"`` (the task branch's remote gained
    commits the local copy lacks) and ``"diverged-base"`` (updating from the
    base branch conflicted); other kinds have room here. ``detail`` is the
    human sentence of what happened; ``branch``/``base`` name the refs
    involved, blank when not applicable.
    """

    kind: str
    detail: str = ""
    branch: str = ""
    base: str = ""


@dataclass(frozen=True)
class Prepared:
    """Where a task's working copy ended up, and what :meth:`Workspace.
    commit_and_push` needs afterwards to derive :class:`~issuebot.contracts.
    Changes` from it.

    ``branch``/``base_sha`` are blank for a workspace that can never produce
    ``changes`` (folder) — its ``commit_and_push`` is unreachable, since
    ``produces`` excludes ``changes`` and the config check enforces it.

    ``problem`` is the channel for a condition the run proceeds through by
    telling the agent (:class:`WorkspaceProblem`) — None for the common case
    of a prepare with nothing to report.
    """

    folder: str
    branch: str = ""
    base_sha: str = ""
    problem: WorkspaceProblem | None = None


class Workspace(ABC):
    """Somewhere a task's working copy is prepared, and how its result is derived.

    Deriving `Changes` is the workspace's job because it is the only thing
    that knows how to ask its own strategy (git, or nothing) what moved — an
    agent's self-report cannot be trusted to move `head_sha`.
    """

    # Set by each subclass; also the name it is registered under in the plugin
    # registry (`plugins.get("workspaces", workspace.name)`).
    name: ClassVar[str]

    # Which output kinds a run in this *kind* of workspace could ever produce —
    # the most any connection using it could do. What one particular connection
    # can do is `produces_for`, which may be narrower.
    produces: ClassVar[frozenset[OutputKind]]

    def produces_for(self, settings: BaseModel) -> frozenset[OutputKind]:
        """What a run under these settings could produce — :attr:`produces`
        unless the connection's own settings narrow it.

        Two questions wear the same word and only one of them is answerable by
        the class. "Could a workspace of this kind ever derive `Changes`" is a
        property of the implementation, asked before any connection exists
        (:func:`~issuebot.config.unconfigured_workspace` picks the keyless
        default with it). "Can *this* connection" can depend on how it is
        configured: git derives changes from a task branch, and a connection
        that asks for no branch has none to derive them from, whether its
        working copy is a folder or a clone of its own.

        The default answers the class, which is right for any workspace whose
        settings cannot change what it produces.
        """
        return self.produces

    @abstractmethod
    def prepare(
        self, connection: Connection, ref: str, *, settings: BaseModel, proc: Process = REAL
    ) -> Prepared:
        """Ensure the task's working copy exists and return where to launch it."""

    @abstractmethod
    def commit_and_push(
        self, prepared: Prepared, message: str, *, settings: BaseModel, proc: Process = REAL
    ) -> Changes:
        """Commit whatever the agent left and report what actually changed.

        Derived from git, never from the agent. Pushes unless the settings say
        not to, or there is no remote — `Changes.pushed` records which."""

    # ------------------------------------------------------------------
    # Optional hooks, both with a default that means "nothing to say"
    # ------------------------------------------------------------------

    @classmethod
    def folder_problem(cls, folder: str) -> str | None:
        """Why this workspace cannot be prepared in `folder`, or None when it can.

        What a workspace *requires of a folder*, asked before any working copy
        exists — `issuebot connect` calls it on the folder a draft names, and the
        wizard calls it again on every folder the user types. So it must be
        synchronous and cheap; a run's own prerequisites belong in
        :meth:`prepare` or in the plugin's `doctor`, which run once and may talk
        to the network.

        Read off the class, exactly like `produces` and a sink's
        `needs_pushed_branch`: there is no connection to build an instance for
        yet. Core asks whichever workspace the draft's keys select
        (:func:`~issuebot.config.workspaces_claiming`), so a strategy nobody
        picked never gets to reject a folder.

        The default says nothing, which is the honest answer for a workspace
        with no requirement of its own — a plain folder is a plain folder.
        """
        return None

    def refresh(
        self, connection: Connection, ref: str, *, reporter: Reporter, proc: Process = REAL
    ) -> None:
        """Top up a working copy inherited from a project checkpoint.

        Only the warm-boot path calls this: the sandbox booted from a checkpoint
        some *earlier* task populated, so a working copy is already on disk but
        belongs to that task's ref. Bringing it to this one is the workspace's
        own business — where it put the copy, and what "up to date" means for it
        — and the ordinary :meth:`prepare` that follows then finds an
        already-current checkout and does cheap no-op work instead of building
        one from scratch.

        No settings argument: what a top-up needs (where the copy lives, what
        the task's ref is) the workspace either holds already or is handed here.

        The default does nothing, which is right for any workspace whose
        `prepare` is idempotent and cheap enough not to need warming — hence
        the explicit `return`: this is a real default, not an unimplemented
        method waiting for a subclass.
        """
        return
