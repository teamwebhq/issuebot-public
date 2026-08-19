#!/usr/bin/env bash
# Push a snapshot of HEAD to the public audit repo.
#
# The public repo gets one squashed commit per sync, so no internal commit
# messages or history are exposed. Paths marked `export-ignore` in
# .gitattributes (CLAUDE.md, CONTEXT.md, docs/adr, ...) are left out.
set -euo pipefail

PUBLIC_REPO="${PUBLIC_REPO:-git@github.com:teamwebhq/issuebot-public.git}"

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

git clone --depth 1 "$PUBLIC_REPO" "$WORK/public"

# Wipe the tree (but not .git) so deleted files propagate, then lay down the
# filtered snapshot of HEAD.
find "$WORK/public" -mindepth 1 -maxdepth 1 ! -name .git -exec rm -rf {} +
git archive HEAD | tar -x -C "$WORK/public"

git -C "$WORK/public" add -A

if git -C "$WORK/public" diff --cached --quiet; then
    echo "Public repo already matches $(git rev-parse --short HEAD). Nothing to do."
    exit 0
fi

git -C "$WORK/public" commit -m "Sync $(git rev-parse --short HEAD)"
git -C "$WORK/public" push
