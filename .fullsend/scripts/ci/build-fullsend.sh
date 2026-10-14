#!/usr/bin/env bash
# Build the Fullsend entrypoint feature as a separate CI artifact.
set -Eeuo pipefail

FEATURE_SHA=bdedebeaf96cbfabf298472397321a946af621c9
FEATURE_BRANCH=feat/entrypoint-harness
REPO_URL="${FULLSEND_REPO_URL:-https://github.com/jctanner/fullsend.git}"
ROOT="${CI_PROJECT_DIR:-$PWD}"
OUTPUT_DIR="${FULLSEND_BUILD_DIR:-$ROOT/fullsend-build}"
SOURCE_DIR="${FULLSEND_SOURCE_DIR:-}"
TEMP_DIR=""

cleanup() {
  if [[ -n "$TEMP_DIR" ]]; then
    rm -rf -- "$TEMP_DIR"
  fi
}
trap cleanup EXIT

if ! command -v go >/dev/null 2>&1; then
  echo "ERROR: Go is required; run this in the separate Go build job" >&2
  exit 127
fi
if [[ -z "$SOURCE_DIR" ]]; then
  command -v git >/dev/null || { echo "ERROR: git is required to fetch Fullsend" >&2; exit 127; }
  TEMP_DIR="$(mktemp -d "${TMPDIR:-/tmp}/fullsend-build.XXXXXX")"
  SOURCE_DIR="$TEMP_DIR/source"
  git clone --filter=blob:none --branch "$FEATURE_BRANCH" --single-branch "$REPO_URL" "$SOURCE_DIR"
fi

actual_sha="$(git -C "$SOURCE_DIR" rev-parse HEAD)"
if [[ "$actual_sha" != "$FEATURE_SHA" ]]; then
  echo "ERROR: Fullsend source revision $actual_sha does not match required $FEATURE_SHA" >&2
  exit 1
fi

mkdir -p "$OUTPUT_DIR"
version="feature-${FEATURE_SHA:0:12}"
for attempt in 1 2 3; do
  if (cd "$SOURCE_DIR" && GOTOOLCHAIN=auto go build -trimpath \
    -ldflags "-X github.com/fullsend-ai/fullsend/internal/cli.version=$version" \
    -o "$OUTPUT_DIR/fullsend" ./cmd/fullsend); then
    break
  fi
  if [[ "$attempt" -eq 3 ]]; then
    echo "ERROR: Fullsend feature build failed after $attempt attempts" >&2
    exit 1
  fi
  echo "Fullsend build attempt $attempt failed; retrying module downloads" >&2
  sleep 2
done
chmod 0755 "$OUTPUT_DIR/fullsend"
printf '%s\n' "$FEATURE_SHA" >"$OUTPUT_DIR/fullsend-source-sha"
(cd "$OUTPUT_DIR" && sha256sum fullsend >fullsend.sha256)
"$OUTPUT_DIR/fullsend" -v | tee "$OUTPUT_DIR/fullsend-version.txt"
printf 'Built Fullsend %s from %s\n' "$version" "$actual_sha"
