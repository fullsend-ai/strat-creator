#!/bin/bash
# Fetches the latest RHOAI architecture context via sparse checkout.
# Safe to run multiple times — pulls updates if already cloned.
#
# Usage:
#   bash scripts/fetch-architecture-context.sh                  # fetch from remote (default)
#   bash scripts/fetch-architecture-context.sh /path/to/local   # use local checkout (copies into .context/)

set -eo pipefail

if [ -n "${RFE_SKIP_BOOTSTRAP:-}" ]; then
  echo "RFE_SKIP_BOOTSTRAP set - skipping dependency bootstrapping step"
  exit 0
fi

CONTEXT_DIR=".context/architecture-context"

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

ARCHITECTURE_LISTING=$(curl --fail --silent --show-error --location --connect-timeout 10 --max-time 60 https://api.github.com/repos/opendatahub-io/architecture-context/contents/architecture) || exit $?
LATEST=$(printf '%s' "$ARCHITECTURE_LISTING" | python3 -c "import sys,json; entries=json.load(sys.stdin); names=sorted(e['name'] for e in entries if e['name'].startswith('rhoai-')); print(names[-1] if names else '')")

if [ -z "$LATEST" ] || [ "$LATEST" = "null" ]; then
  echo "Could not detect latest architecture version"
  exit 1
fi

if [ -L "$CONTEXT_DIR" ]; then
  rm "$CONTEXT_DIR"
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
