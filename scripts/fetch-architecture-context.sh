#!/bin/bash
# Fetches the latest RHOAI architecture context via sparse checkout.
# Safe to run multiple times — pulls updates if already cloned.
#
# Usage:
#   bash scripts/fetch-architecture-context.sh                  # fetch from remote (default)
#   bash scripts/fetch-architecture-context.sh /path/to/local   # use local checkout (copies into .context/)

if [ -n "${RFE_SKIP_BOOTSTRAP:-}" ]; then
  echo "RFE_SKIP_BOOTSTRAP set - skipping dependency bootstrapping step"
  exit 0
fi

CONTEXT_DIR=".context/architecture-context"

# A pinned snapshot wins on every call. The entrypoint and pre-strategy.sh pass
# it as $1, but skills re-run this script with no argument; without this, that
# call would fetch the newest version from GitHub (unpinning the run) and update
# the copy with git (see below).
set -- "${1:-${STRAT_ARCHITECTURE_CONTEXT_PATH:-}}"

if [ -n "$1" ]; then
  LOCAL_PATH="$1"
  if [ ! -d "$LOCAL_PATH" ]; then
    echo "Local architecture context path does not exist: $LOCAL_PATH"
    exit 1
  fi
  rm -rf "$CONTEXT_DIR"
  mkdir -p "$CONTEXT_DIR"
  cp -R "$LOCAL_PATH"/* "$CONTEXT_DIR"/
  # Marker the review skills Read instead of probing directories (mirrors the remote path below).
  ls "$CONTEXT_DIR/architecture" 2>/dev/null | grep '^rhoai-' | sort | tail -1 > "$CONTEXT_DIR/LATEST_VERSION"
  echo "Architecture context copied from local path: $LOCAL_PATH"
  exit 0
fi

LATEST=$(curl -sL https://api.github.com/repos/opendatahub-io/architecture-context/contents/architecture | python3 -c "import sys,json; entries=json.load(sys.stdin); names=sorted(e['name'] for e in entries if e['name'].startswith('rhoai-')); print(names[-1] if names else '')")

if [ -z "$LATEST" ] || [ "$LATEST" = "null" ]; then
  echo "Could not detect latest architecture version"
  exit 1
fi

if [ -L "$CONTEXT_DIR" ]; then
  rm "$CONTEXT_DIR"
fi

# Only a clone of its own is updated in place. A copy made by the local mode has
# no .git, so `git -C` would act on the enclosing repository (this checkout) and
# sparse-check it out down to two directories, deleting scripts/, config/,
# .fullsend/ and .claude/skills/. Replace such a copy with a fresh clone.
if [ -d "$CONTEXT_DIR" ] && [ ! -e "$CONTEXT_DIR/.git" ]; then
  rm -rf "$CONTEXT_DIR"
fi

if [ -d "$CONTEXT_DIR" ]; then
  git -C "$CONTEXT_DIR" sparse-checkout set "architecture/$LATEST" "overlays"
  git -C "$CONTEXT_DIR" pull --quiet
else
  git clone --depth 1 --filter=blob:none --sparse https://github.com/opendatahub-io/architecture-context "$CONTEXT_DIR"
  git -C "$CONTEXT_DIR" sparse-checkout set "architecture/$LATEST" "overlays"
fi

# Marker the review skills Read instead of probing directories with Glob or Bash.
echo "$LATEST" > "$CONTEXT_DIR/LATEST_VERSION"
echo "Architecture context ready: $CONTEXT_DIR/architecture/$LATEST"
