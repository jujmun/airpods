#!/bin/sh
# One-line install:
#
#   curl -fsSL https://raw.githubusercontent.com/jujmun/airpods/main/get.sh | sh
#
# Clones the repo to ~/.airpods (or $AIRPODS_DIR) and runs its installer. Re-running updates the
# clone instead of fetching it twice. Options are passed through to install.sh, so
#
#   curl -fsSL ... | sh -s -- --uninstall
#
# removes what the installer created.
set -eu

REPO_URL="${AIRPODS_REPO:-https://github.com/jujmun/airpods.git}"
DEST="${AIRPODS_DIR:-$HOME/.airpods}"

die() { printf 'error: %s\n' "$*" >&2; exit 1; }

command -v git >/dev/null 2>&1 || die "git is required: install Xcode Command Line Tools (xcode-select --install)"
[ "$(uname -s)" = "Darwin" ] || die "macOS only: this app uses CoreMotion, Vision and Quartz"

if [ -d "$DEST/.git" ]; then
  printf 'updating %s\n' "$DEST"
  git -C "$DEST" pull --quiet --ff-only || die "could not update $DEST; remove it and run again"
else
  printf 'cloning into %s\n' "$DEST"
  git clone --quiet --depth 1 "$REPO_URL" "$DEST" || die "could not clone $REPO_URL"
fi

exec sh "$DEST/install.sh" "$@"
