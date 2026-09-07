"""How issuebot authenticates git to a forge over HTTPS.

One definition, imported by both plugins that need it: the git workspace, which
configures every clone it cuts, and a source that lends a short-lived token and
has to make the agent's *own* git calls use it. Neither imports the other, so
either stays deletable.

It lives in core rather than in one of them because it is not either plugin's
private business — the workspace decides how a clone authenticates, the source
decides what credential a run holds, and this is the one sentence they have to
agree on. Two copies of it is not a hypothetical cost: it is why a sandbox that
authenticated its clone correctly still failed on the agent's first push.

Nothing here holds a credential. The helper reads the token from the
environment at the moment git asks, so the value never appears in argv or in a
config file.
"""

from __future__ import annotations

# The git config key: a credential helper scoped to github.com, so a remote on
# another host keeps whatever it already uses.
GH_CREDENTIAL_KEY = "credential.https://github.com.helper"

# The environment variable a run's forge token arrives in — the short-lived one
# a source lends for this item, else whatever the environment was given.
TOKEN_ENV = "GH_TOKEN"

# First choice: that token, read by the helper itself so it never appears in
# argv, where any process listing would show it.
#
# Answers only git's ``get`` and stays silent when the variable is unset, which
# is what lets the next helper in the list have its turn. No single quotes: this
# string is packed into ``GIT_CONFIG_PARAMETERS``, whose format quotes each
# entry.
TOKEN_CREDENTIAL_HELPER = (
    f'!f() {{ test "$1" = get && test -n "${{{TOKEN_ENV}:-}}" && '
    f'printf "username=x-access-token\\npassword=%s\\n" "${TOKEN_ENV}"; }}; f'
)

# Second choice: ``gh``'s own credential store, for a machine where a person ran
# `gh auth login` and set no token.
#
# Not first, and never the only one. A sandbox image may ship a wrapper around
# `gh` that refuses `gh auth git-credential` — Railway's `safe-gh` does — and a
# clone with only this helper then fails with git asking for a username that
# nobody is there to type.
GH_CREDENTIAL_HELPER = "!gh auth git-credential"

# The whole list, in the order git reads it.
#
# Adding a helper only *appends* to the list git already has, and git asks every
# helper in turn and takes the first answer — so a helper configured earlier on
# the machine (osxkeychain in Xcode's system gitconfig, store on a Linux runner)
# would answer first with whatever stale personal credential it holds, and the
# push is rejected. An empty value is git's documented reset of the list, so the
# first entry clears it and the two after it are the only helpers there are.
GH_CREDENTIAL_CONFIG = (
    f"{GH_CREDENTIAL_KEY}=",
    f"{GH_CREDENTIAL_KEY}={TOKEN_CREDENTIAL_HELPER}",
    f"{GH_CREDENTIAL_KEY}={GH_CREDENTIAL_HELPER}",
)

# The same list as ``git -c`` arguments, for a clone that has no config yet.
GH_CREDENTIAL_ARGS = tuple(arg for entry in GH_CREDENTIAL_CONFIG for arg in ("-c", entry))


# One single quote, and git's documented way of writing one *inside* a quoted
# entry: end the quote, an escaped quote, reopen. Spelled as constants because
# the alternative is a literal that is four backslashes and quotes long.
_QUOTE = "'"
_ESCAPED_QUOTE = "'\\''"


def _quoted(entry: str) -> str:
    """One ``GIT_CONFIG_PARAMETERS`` entry, always quoted."""
    return _QUOTE + entry.replace(_QUOTE, _ESCAPED_QUOTE) + _QUOTE


def git_config_parameters() -> str:
    """The list as ``GIT_CONFIG_PARAMETERS``, for git commands nothing configured.

    The channel for the agent's own git calls: it applies to every git process
    that inherits the environment, including ones issuebot never runs and so
    cannot pass ``-c`` to.

    ``GIT_CONFIG_PARAMETERS`` outranks a repository's own config, and this list
    opens by *resetting* git's helper list — so a value here replaces whatever a
    clone was configured with rather than adding to it. That is why it must be
    this same list and not a shorter one: a subset here silently disables the
    helpers the clone was given.

    The format is space-separated entries, each one single-quoted — *always*,
    not only when it contains something that needs it. ``shlex.quote`` is the
    wrong tool here for exactly that reason: it leaves an entry with no special
    characters bare, and git answers a bare entry with "bogus format in
    GIT_CONFIG_PARAMETERS" and parses none of them. A literal quote inside an
    entry is escaped git's own documented way.
    """
    return " ".join(_quoted(entry) for entry in GH_CREDENTIAL_CONFIG)
