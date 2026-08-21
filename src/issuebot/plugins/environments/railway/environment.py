"""Railway as a sandbox provider — everything Railway-specific, and nothing else.

This module supplies *how Railway does the things a sandbox provider does*. The
controller that uses them — boot ladder, wire protocol, reporter lifecycle,
checkpoint policy, teardown guarantees — is :mod:`issuebot.sandbox`, and is
shared with every other provider.

Adding a second provider (an AWS sandbox, a container host) means writing a
folder this size and registering it. Nothing else in the runner changes. That
is only true because nothing Railway-shaped escapes this folder (ADR-0002):

* **Secret references.** ``${{shared.NAME}}`` is Railway template syntax,
  produced only by :meth:`RailwayProvider.secret_env`, which the protocol asks
  each provider for.
* **The tools template.** Its name and package list live here; this plugin's
  own ``cli.py`` reads the name from here.
* **The token variables.** ``RAILWAY_TOKEN``/``RAILWAY_API_TOKEN`` live in
  ``settings.py``, beside the model that chooses between them.

One class holds the credential and speaks the CLI.
"""

from __future__ import annotations

import json
import shlex
import threading
from collections.abc import Callable
from typing import TYPE_CHECKING, ClassVar

import issuebot
from issuebot import release
from issuebot.config import Connection
from issuebot.plugins.environments.railway import settings as railway_settings
from issuebot.process import REAL, Completed, Process
from issuebot.sandbox import SandboxEnvironment
from issuebot.sandbox_protocol import update_argv

if TYPE_CHECKING:
    from issuebot.runner import Wiring

# The shared sandbox template, built by `issuebot railway build-template`. A
# prebuilt template lets `create` start warm — git, gh and node already present —
# instead of installing them on every fresh sandbox.
TEMPLATE = "issuebot-tools"

# `curl` fetches issuebot's installer; `uv` is what that installer uses. The
# installer bootstraps uv for itself when an image lacks it, so this is a
# saved download per cold boot rather than a requirement.
TEMPLATE_PACKAGES = ["git", "gh", "curl", "nodejs", "npm", "uv"]

# How long a sandbox may sit idle before Railway reclaims it.
_IDLE_TIMEOUT_MINUTES = 120

# Infrastructure secrets the agent needs inside the sandbox, referenced as
# Railway shared variables so the values never pass through this process. The
# `${{shared.NAME}}` form is resolved by Railway at sandbox boot.
_SHARED_SECRETS = ("ANTHROPIC_API_KEY", "GH_TOKEN")


class RailwayError(RuntimeError):
    """A `railway` CLI invocation failed."""


class RailwayProvider:
    """Drives Railway Sandboxes through the `railway` CLI.

    Holds its own credential and environment, so the per-connection token that
    lets one runner drive several Railway projects never appears in the
    controller's call signatures.

    Railway ships no Python SDK and no documented GraphQL surface for sandboxes,
    so this shells out. Their docs are Priority Boarding, so argv forms are
    pinned here rather than assumed stable.
    """

    name = "railway"
    supports_checkpoints = True

    # What a user runs when the controller tells them their template is stale.
    # The controller quotes this; it never spells it.
    rebuild_command = "issuebot railway build-template"

    def __init__(
        self,
        *,
        auth: dict[str, str],
        environment_id: str | None = None,
        private_network: bool = False,
        command: str = railway_settings.DEFAULT_COMMAND,
        proc: Process = REAL,
    ) -> None:
        self._auth = auth
        self._environment_id = environment_id
        self._private_network = private_network
        self._command = command
        self._proc = proc

    @classmethod
    def for_connection(cls, connection: Connection, proc: Process = REAL) -> RailwayProvider:
        """The provider one railway connection runs its sandboxes through.

        A connection with no ``[railway]`` table at all still builds — falling
        back to the ambient credential and Railway's own default environment —
        rather than asserting: config validation is what guarantees a real table
        for any connection that actually selects this environment, and an
        environment that cannot be *constructed* has nowhere to report a failed
        run from (see the `ExecutionEnvironment` ABC's never-raise rule).
        """
        railway = railway_settings.for_connection(connection)
        if railway is None:
            return cls(auth={}, proc=proc)
        return cls(
            auth=railway_settings.token_env(railway.token, railway.token_kind),
            environment_id=railway.environment_id,
            private_network=railway.network == "private",
            command=railway.command,
            proc=proc,
        )

    # -- talking to the CLI -------------------------------------------------

    def _run(self, *argv: str) -> Completed:
        """Run a `railway` command under this provider's credential.

        The executable is the connection's own: a bare name is left to PATH, an
        absolute path is what a service-managed runner needs."""
        return self._proc.run([self._command, *argv], env=self._auth)

    def _check(self, *argv: str) -> str:
        """Run a `railway` command and return stdout, raising on failure."""
        result = self._run(*argv)
        if not result.ok:
            raise RailwayError(result.message or f"railway {' '.join(argv)} failed")
        return result.out

    def _targeted(self, argv: list[str]) -> list[str]:
        """Pin ``--environment`` on a sandbox invocation.

        The token itself is never a CLI flag: it reaches the child through its
        environment (see ``settings.token_env``)."""
        if self._environment_id is None:
            return argv
        return [*argv[:2], "--environment", self._environment_id, *argv[2:]]

    # -- SandboxProvider ----------------------------------------------------

    def secret_env(self) -> dict[str, str]:
        """Infrastructure secrets, as Railway shared-variable references.

        The values never pass through this process: Railway resolves each
        reference when it boots the sandbox. Another provider answers this
        question its own way — with real values, or with ``{}`` when its image
        already carries them."""
        return {name: f"${{{{shared.{name}}}}}" for name in _SHARED_SECRETS}

    def create(self, *, env: dict[str, str], checkpoint: str | None = None) -> str:
        """Create a sandbox and return its id.

        Boots from a checkpoint when one was chosen, else from the shared tools
        template. Variables are baked in at create time — a sandbox's environment
        cannot be changed once it is running."""
        argv = ["sandbox", "create", "--json", "--idle-timeout-minutes", str(_IDLE_TIMEOUT_MINUTES)]
        if self._private_network:
            argv.append("--private-network")
        if checkpoint:
            argv += ["--checkpoint", checkpoint]
        else:
            argv += ["--template", TEMPLATE]
        for key, value in env.items():
            argv += ["--variable", f"{key}={value}"]

        out = self._check(*self._targeted(argv))
        sandbox_id = json.loads(out or "{}").get("id")
        if not sandbox_id:
            raise RailwayError(f"no sandbox id in create output: {out!r}")
        return str(sandbox_id)

    def exec_stream(
        self,
        sandbox_id: str,
        argv: list[str],
        *,
        on_line: Callable[[str], None],
        cancel: threading.Event | None = None,
    ) -> int:
        """Run a command in the sandbox, streaming stdout line by line.

        On cancellation the CLI process is terminated and then killed if it
        lingers — the ladder belongs to :class:`~issuebot.process.RealProcess`,
        not here."""
        cmd = [self._command, "sandbox", "exec", "--id", sandbox_id, "--", *argv]
        return self._proc.spawn(cmd, on_line=on_line, env=self._auth, cancel=cancel)

    def read_file(self, sandbox_id: str, path: str) -> str:
        """Read a file out of the sandbox's filesystem."""
        return self._check("sandbox", "exec", "--id", sandbox_id, "--", "cat", path)

    def destroy(self, sandbox_id: str) -> None:
        """Destroy a sandbox."""
        self._check("sandbox", "destroy", sandbox_id)

    def list_checkpoints(self) -> list[str]:
        """Every checkpoint name that exists in this Railway project."""
        out = self._check("sandbox", "checkpoint", "list", "--json")
        return [c["name"] for c in json.loads(out or "[]")]

    def create_checkpoint(self, sandbox_id: str, name: str) -> None:
        """Snapshot a running sandbox's filesystem into a named checkpoint."""
        self._check("sandbox", "checkpoint", "create", sandbox_id, name)

    def delete_checkpoint(self, name: str) -> None:
        """Delete a named checkpoint."""
        self._check("sandbox", "checkpoint", "delete", name)

    # -- project-wide administration ----------------------------------------

    def build_template(self, name: str = TEMPLATE, packages: list[str] | None = None) -> None:
        """Build the named sandbox template with this released issuebot pinned.

        Verify the ``--package`` and ``--run`` flags against the installed CLI
        (``railway sandbox template --help``) — Railway's sandbox docs are
        Priority Boarding and these flag forms are not pinned upstream."""
        if not release.is_installed_wheel():
            raise RailwayError(
                "building a remote template requires a released issuebot wheel; "
                f"install it with: {release.INSTALL_COMMAND}"
            )

        argv = ["sandbox", "template", "build", name]
        for package in packages if packages is not None else TEMPLATE_PACKAGES:
            argv += ["--package", package]

        argv += ["--run", shlex.join(update_argv(issuebot.__version__))]

        self._check(*argv)


class RailwayEnvironment(SandboxEnvironment):
    """Railway's execution environment: the shared sandbox controller, driven
    by :class:`RailwayProvider`.

    Built over the connection's :class:`~issuebot.runner.Wiring` like every
    environment — which is what lets a new environment be a folder rather than
    an edit to the factory. What the sandbox controller reads of it is its own
    business (see :class:`~issuebot.sandbox.SandboxEnvironment`).
    """

    name: ClassVar[str] = "railway"

    def __init__(self, wiring: Wiring, proc: Process = REAL) -> None:
        """Wire the shared controller to a provider built from this
        connection's own ``[railway]`` table."""
        super().__init__(wiring, RailwayProvider.for_connection(wiring.connection, proc))
