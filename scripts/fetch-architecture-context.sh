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

# Per-type source (as rfe-creator's script): a work-item type whose architecture context lives in its
# application's own repository names it in its descriptor's pipeline.context_sources args:
#   --repo <url or local clone> --path <dir in that repo> --dest <dir under .context/> [--ref <branch>]
# The docs land in <dest>/<path>. --ref picks a branch (e.g. an unmerged docs branch of a local clone).
# With no flags the RHOAI default below applies, unchanged.
if [ "${1:-}" != "${1#--}" ]; then
  REPO="" SUBPATH="" DEST="" REF=""
  while [ $# -gt 0 ]; do
    case "$1" in
      --repo) REPO="${2:-}"; shift 2 ;;
      --path) SUBPATH="${2:-}"; shift 2 ;;
      --dest) DEST="${2:-}"; shift 2 ;;
      --ref) REF="${2:-}"; shift 2 ;;
      *) echo "ERROR: unknown argument '$1' (expected --repo, --path, --dest, --ref)" >&2; exit 2 ;;
    esac
  done
  if [ -z "$REPO" ] || [ -z "$SUBPATH" ] || [ -z "$DEST" ]; then
    echo "ERROR: --repo, --path and --dest go together" >&2
    exit 2
  fi
  case "$DEST" in
    .context/*) ;;
    *) echo "ERROR: --dest must be under .context/ (got '$DEST')" >&2; exit 2 ;;
  esac
  case "$DEST$SUBPATH" in
    *..*) echo "ERROR: --dest and --path must not contain '..'" >&2; exit 2 ;;
  esac
  # A fresh clone every call: the copy is small, and an existing directory without .git
  # must never make git act on the enclosing checkout (see the RHOAI path below).
  rm -rf "$DEST"
  mkdir -p "$(dirname "$DEST")"
  git clone --quiet --depth 1 --filter=blob:none --sparse ${REF:+--branch "$REF"} "$REPO" "$DEST" || exit 1
  git -C "$DEST" sparse-checkout set "$SUBPATH" || exit 1
  if [ ! -d "$DEST/$SUBPATH" ]; then
    echo "Architecture context not found: $REPO${REF:+@$REF} has no $SUBPATH"
    exit 1
  fi
  echo "Architecture context ready: $DEST/$SUBPATH"
  exit 0
fi

CONTEXT_DIR=".context/architecture-context"
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

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
  if ! python3 "$SCRIPT_DIR/architecture_release.py" --directory "$CONTEXT_DIR/architecture" \
      > "$CONTEXT_DIR/LATEST_VERSION"; then
    exit 1
  fi
  echo "Architecture context copied from local path: $LOCAL_PATH"
  exit 0
fi

LATEST=$(curl -fsSL https://api.github.com/repos/opendatahub-io/architecture-context/contents/architecture \
  | python3 "$SCRIPT_DIR/architecture_release.py" --github-json)

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
