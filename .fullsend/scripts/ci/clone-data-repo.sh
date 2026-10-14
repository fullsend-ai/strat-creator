#!/bin/bash
# Source: strat-pipeline ci-scripts/clone-data-repo.sh at fd36b15c5095c9f20a270b1d69933c578c04d9da.
# Adapted: the caller supplies a complete repo URL and Git HTTP auth through
# process-scoped configuration; credentials are never written into the remote URL.
set -euo pipefail

repo="$1"
dest="$2"

if [ -d "$dest" ]; then
  echo "ERROR: results repository already exists at $dest" >&2
  exit 1
fi
mkdir -p "$(dirname "$dest")"

git clone "$repo" "$dest"
git -C "$dest" config user.email "strat-pipeline@ci.noreply"
git -C "$dest" config user.name "strat-pipeline"
echo "Results repository cloned to $dest"
