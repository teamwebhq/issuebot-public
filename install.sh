#!/bin/sh
#
# Install issuebot from an immutable GitHub Release wheel.
#
#   curl -fsSL https://github.com/teamwebhq/issuebot-public/releases/latest/download/install.sh | sh
#
# Pass a stable X.Y.Z release to install that exact artifact. With no argument,
# the installer resolves GitHub's latest-release redirect before constructing
# the wheel URL.

set -eu

VERSION="${1:-${ISSUEBOT_VERSION:-}}"

if [ -z "$VERSION" ]; then
    LATEST_URL="$(curl -fsSL -o /dev/null -w '%{url_effective}' \
        https://github.com/teamwebhq/issuebot-public/releases/latest)"
    VERSION="${LATEST_URL##*/}"
    VERSION="${VERSION#v}"
fi

printf '%s' "$VERSION" | grep -qE \
    '^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$' || {
    echo "issuebot: '$VERSION' is not a stable X.Y.Z release" >&2
    exit 1
}

WHEEL_URL="https://github.com/teamwebhq/issuebot-public/releases/download/v$VERSION/issuebot-$VERSION-py3-none-any.whl"

# The uv this bootstraps when an image has none. Pinned, because it runs beside
# a sandbox's GH_TOKEN and ANTHROPIC_API_KEY and "latest, whatever that is
# today" is not a thing to hand that. Bump deliberately.
UV_INSTALL_VERSION="${UV_INSTALL_VERSION:-0.9.18}"

# PATH as we were called with it. Checks below use this rather than $PATH: this
# script prepends to its own PATH, and a binary that is only findable because of
# that mutation is not installed as far as the next process is concerned. That
# is the exact bug the final check exists to catch, so it must not be able to
# pass because of us.
AMBIENT_PATH="$PATH"

# uv does the actual install. It is a project decision, not a wire one. Images
# that ship uv (issuebot's own sandbox template does) skip this; the rest
# bootstrap it, since uv fetches its own Python and so needs nothing from the
# image but curl.
if ! command -v uv >/dev/null 2>&1; then
    echo "issuebot: installing uv $UV_INSTALL_VERSION" >&2
    curl -fsSL "https://astral.sh/uv/$UV_INSTALL_VERSION/install.sh" | sh
    PATH="$HOME/.local/bin:$PATH"
    export PATH
    # `set -e` is blind to this: a curl that 404s feeds `sh` an empty script,
    # which exits 0, and the next thing anyone sees is a confusing "uv: not
    # found" from a line that had nothing to do with it.
    command -v uv >/dev/null 2>&1 || {
        echo "issuebot: could not install uv $UV_INSTALL_VERSION" >&2
        exit 1
    }
fi

# Where the binary goes. uv's own default is ~/.local/bin, which a
# non-interactive `exec` into a sandbox — no login shell, no profile — routinely
# does not have on PATH; /usr/local/bin is on the default PATH everywhere this
# runs. Fall back to uv's default when it isn't ours to write to.
BIN_DIR="${ISSUEBOT_BIN_DIR:-}"
if [ -z "$BIN_DIR" ]; then
    if [ -w /usr/local/bin ]; then
        BIN_DIR="/usr/local/bin"
    else
        BIN_DIR="$HOME/.local/bin"
    fi
fi

# Checked before the install rather than after it: an unreachable install is a
# waste of everyone's time either way, and this way the message arrives before
# the minutes do.
case ":$AMBIENT_PATH:" in
    *":$BIN_DIR:"*) ;;
    *)
        echo "issuebot: $BIN_DIR is not on PATH — add it, or set ISSUEBOT_BIN_DIR" >&2
        exit 1
        ;;
esac

echo "issuebot: installing $VERSION into $BIN_DIR" >&2
UV_TOOL_BIN_DIR="$BIN_DIR" uv tool install --force "$WHEEL_URL"

# Named directly rather than through `command -v`, which would search this
# script's own mutated PATH.
[ -x "$BIN_DIR/issuebot" ] || {
    echo "issuebot: nothing landed in $BIN_DIR" >&2
    exit 1
}
if ! INSTALLED_VERSION="$("$BIN_DIR/issuebot" version)"; then
    echo "issuebot: the installed binary does not run" >&2
    exit 1
fi
[ "$INSTALLED_VERSION" = "$VERSION" ] || {
    echo "issuebot: installed binary reports $INSTALLED_VERSION; expected $VERSION" >&2
    exit 1
}

echo "issuebot: $VERSION ready in $BIN_DIR" >&2
