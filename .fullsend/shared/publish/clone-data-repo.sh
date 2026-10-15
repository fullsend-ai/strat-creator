#!/bin/bash
# Carried from github.com/jctanner-opendatahub-io/strat-creator at 78ab3f1e,
# .fullsend/scripts/ci/clone-data-repo.sh (Apache-2.0); see .fullsend/README.md.
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
