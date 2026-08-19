# Architecture

This document gives the issuebot design. Read [`README.md`](../README.md)
for setup and operation.

## The plugin architecture

Issuebot has five plugin axes: source, workspace, environment, harness, and
sink. Each axis has a package in `src/issuebot/plugins/`.

A plugin package can declare a module-level `PLUGIN`. Plugin discovery examines
the axis packages when the registry loads. A package without `PLUGIN` is not a
plugin.

The declaration gives the plugin name. An axis-specific declaration gives its
implementation. A declaration can also give settings models, a `validate`
function, a CLI application, doctor checks, and wizard questions. A plugin can
keep settings in its own table or use flat connection settings.

Plugins with a CLI application add a command group below their name. The git
plugin owns the worktree command group.

The registry rejects two plugins that claim the same flat settings key. A hidden
plugin stays available in configuration, but the CLI and wizard do not offer
it.

## The source axis

The source axis has `SourceClient` and `Source`. A `SourceClient` does
install-wide work. For example, it does registration, telemetry, and command
polling.

A `Source` does work for one connection. It polls for work, claims work, releases a
claim, and reports the result. `Supervisor` receives a `SourceClient` and builds
a `Source` for each connection with `source_for`.

## Development

Use these commands from a checkout:

```sh
git clone https://github.com/teamwebhq/issuebot && cd issuebot
uv sync
uv run issuebot --help
tools/check.sh
uv run pytest
```

`tools/check.sh` runs Ruff checks, Ruff format checks, and Ty checks. The plugin
registry and CLI mounting tests are in `tests/plugins/`.

## Release identity and remote execution

Issuebot releases use stable `X.Y.Z` versions. The project version is in
`pyproject.toml`. The release module makes URLs for the latest installer and an
installer for one stable version.

`install.sh` uses a version argument or `ISSUEBOT_VERSION`. If the script gets
no version from an argument or `ISSUEBOT_VERSION`, it resolves the latest
release. It installs the release wheel with
`uv tool install` and makes sure that the installed version is the selected
version.

Remote execution uses a sandbox. The controller examines the sandbox version.
It installs the controller release when the versions are different. The worker
makes sure that its version is the requested version before it starts work.

### GitHub workflows

The pull-request workflow starts for each pull-request event. It rejects a
hexadecimal head-branch name that has 7 through 64 characters. Then, it checks
out the full repository history and installs the locked dependencies. It
compares the release version with the target branch. Finally, it runs the
test suite and the static checks.

The release workflow starts when GitHub closes a pull request for `main`. The
release job runs only when GitHub merged the pull request. The concurrency group
runs one release at a time. It does not cancel an active release.

The release job checks out the merge commit. It makes sure that the checked out
commit is the merge commit and has two parents. Then, it compares the version
with the first parent. It runs the test suite and the static checks. It builds
one wheel and makes sure that the wheel metadata version is the release version.

The job uses `${{ github.token }}` as `GH_TOKEN` for its GitHub CLI commands.
It makes sure that there is no tag or release. Then, it creates the tag for the
merge commit and makes sure that the tag target is the merge commit. It creates
a draft release with the wheel and `install.sh`. Finally, it publishes the
release.

### Documentation checks

`tests/test_readme.py` examines `README.md` and each Markdown file in `docs/`.
It does not examine `docs/` subdirectories. It examines complete TOML examples,
commands, flags, and internal links.

Complete TOML examples and the command and flag checks use the installed plugin
registry. Complete TOML examples must load with the installed plugins. Command
and flag checks skip an invocation when its first command name is not available.
