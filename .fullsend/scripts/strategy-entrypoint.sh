#!/usr/bin/env bash
# Script-led equivalent of strat-pipeline's single-rfe job.
set -Eeuo pipefail

printf '%s\n' \
  '###################################################################' \
  '# STRAT CREATOR ENTRYPOINT SCRIPT' \
  '###################################################################'

MODE=single-rfe
if [[ ${1:-} == batch-discover && $# -eq 1 ]]; then
  MODE=batch-discover
elif [[ $# -ne 1 || ! $1 =~ ^RHAIRFE-[0-9]+$ ]]; then
  echo "usage: strategy-entrypoint.sh RHAIRFE-NNNN | batch-discover" >&2
  exit 2
fi
for value in "${BATCH_SIZE:-}" "${BATCH_OFFSET:-}"; do
  [[ -z "$value" || "$value" =~ ^[0-9]+$ ]] || { echo "Invalid batch limit" >&2; exit 2; }
done

ROOT="${STRAT_CREATOR_ROOT:-$PWD}"
CI_SCRIPTS="$ROOT/.fullsend/scripts/ci"
# The local assessment directory is unused by strategy CI. Preserve all other
# settings and Fullsend hooks; configure trust for this authorized CI project.
python3 "$CI_SCRIPTS/prepare-claude-settings.py" "$ROOT"
source "$CI_SCRIPTS/ca-bundle.sh"
CA_BUNDLE="${TMPDIR:-/tmp}/strat-ca-${CI_JOB_ID:-$$}.pem"
fullsend_prepare_ca_bundle "$CA_BUNDLE"
ARTIFACTS="$ROOT/artifacts"
LOCKED_FILE="$ARTIFACTS/locked-rfe-ids.txt"
OUTPUT_DIR="${FULLSEND_OUTPUT_DIR:-/sandbox/workspace/output}"
RFE_KEY="${1}"
CANDIDATES=("$RFE_KEY")
COLLECTOR_PID=""
LOCKED_KEYS=""
PREVIOUS_RUN=""

mkdir -p "$OUTPUT_DIR"
if [[ -z "${RESULTS_PUSH_TOKEN:-}" ]]; then
  echo "ERROR: RESULTS_PUSH_TOKEN is required for result publication" >&2
  exit 1
fi
auth="$(printf '%s:%s' "${RESULTS_GIT_USER:-oauth2}" "$RESULTS_PUSH_TOKEN" | base64 | tr -d '\n')"
export GIT_CONFIG_COUNT=1
export GIT_CONFIG_KEY_0="http.${RESULTS_REPO_URL:?RESULTS_REPO_URL is required}.extraHeader"
export GIT_CONFIG_VALUE_0="Authorization: Basic $auth"
unset auth

if [[ -d "$ARTIFACTS/.git" ]]; then
  echo "Using existing result repository at $ARTIFACTS"
elif [[ -e "$ARTIFACTS" ]]; then
  echo "ERROR: $ARTIFACTS exists but is not the results repository" >&2
  exit 1
else
  "$CI_SCRIPTS/clone-data-repo.sh" "${RESULTS_REPO_URL:?RESULTS_REPO_URL is required}" "$ARTIFACTS"
fi
# Historical runs remain under RHAISTRAT/. Remove only working output from
# earlier jobs so this invocation never refines, reviews or republishes it.
for dir in strat-tasks strat-reviews strat-originals reports; do
  rm -rf -- "$ARTIFACTS/$dir"
done
rm -f "$ARTIFACTS/claude-stderr.log"
rm -rf -- "$ARTIFACTS/claude-stderr"
rm -f "$ARTIFACTS/pipeline-data.json" "$ARTIFACTS/strat-tickets.md" "$ARTIFACTS/strat-skipped.md" "$ARTIFACTS/claude-otel.jsonl" "$ARTIFACTS/claude-otel-rate.json"
mkdir -p "$ARTIFACTS/.git/info"
printf '%s\n' 'locked-rfe-ids.txt' >> "$ARTIFACTS/.git/info/exclude"
git -C "$ARTIFACTS" rm --cached --ignore-unmatch -q locked-rfe-ids.txt 2>/dev/null || true

export OTEL_LOG_FILE="$ARTIFACTS/claude-otel.jsonl"
export OTEL_RATE_FILE="$ARTIFACTS/claude-otel-rate.json"
if [[ -L "$ARTIFACTS/RHAISTRAT/current" ]]; then
  PREVIOUS_RUN="$(readlink "$ARTIFACTS/RHAISTRAT/current")"
fi

cleanup() {
  local rc=$?
  trap - EXIT HUP INT TERM
  set +e

  if [[ -n "$COLLECTOR_PID" ]]; then
    kill -TERM "$COLLECTOR_PID" 2>/dev/null
    wait "$COLLECTOR_PID" 2>/dev/null
  fi

  if [[ -s "$LOCKED_FILE" ]]; then
    cp "$LOCKED_FILE" "$OUTPUT_DIR/owned-rfe-ids.txt" || rc=1
    LOCKED_KEYS="$(tr '\n' ' ' < "$LOCKED_FILE" | xargs)"
    if [[ -n "$LOCKED_KEYS" ]]; then
      python3 "$ROOT/scripts/lock_issues.py" unlock $LOCKED_KEYS || {
        echo "ERROR: failed to release Jira lock(s): $LOCKED_KEYS" >&2
        rc=1
      }
    fi
  fi

  CURRENT_RUN=""
  if [[ -L "$ARTIFACTS/RHAISTRAT/current" ]]; then
    CURRENT_RUN="$(readlink "$ARTIFACTS/RHAISTRAT/current")"
  fi
  if [[ -n "$CURRENT_RUN" && "$CURRENT_RUN" != "$PREVIOUS_RUN" ]]; then
    mkdir -p "$OUTPUT_DIR/strategy-run"
    cp -aL "$ARTIFACTS/RHAISTRAT/current/." "$OUTPUT_DIR/strategy-run/" 2>/dev/null || rc=1
  fi
  for file in "$ARTIFACTS/pipeline-data.json" "$ARTIFACTS/strat-tickets.md" "$ARTIFACTS/strat-skipped.md"; do
    [[ -f "$file" ]] && cp -f "$file" "$OUTPUT_DIR/" || true
  done
  if [[ -d "$ARTIFACTS/claude-stderr" ]]; then
    cp -a "$ARTIFACTS/claude-stderr" "$OUTPUT_DIR/" || rc=1
  fi
  if [[ -d "$ARTIFACTS/reports" ]]; then
    mkdir -p "$OUTPUT_DIR/reports"
    cp -a "$ARTIFACTS/reports/." "$OUTPUT_DIR/reports/"
  fi

  if [[ "$rc" -ne 0 ]]; then
    mkdir -p "$OUTPUT_DIR/partial-work"
    for dir in strat-tasks strat-reviews strat-originals; do
      [[ ! -d "$ARTIFACTS/$dir" ]] || cp -a "$ARTIFACTS/$dir" "$OUTPUT_DIR/partial-work/" || rc=1
    done
  fi

  rm -f "$LOCKED_FILE"
  rm -f "$CA_BUNDLE"
  exit "$rc"
}
trap cleanup EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

# Fetch outside the agents so setup errors are visible in the runner trace.
# Context is optional under the existing skills; keep that behavior explicit.
echo "--- Fetching architecture context before starting Claude ---"
CONTEXT_LOG="$OUTPUT_DIR/architecture-context-fetch.log"
set +e
(cd "$ROOT" && timeout 180 bash "$ROOT/scripts/fetch-architecture-context.sh") 2>&1 | tee "$CONTEXT_LOG"
context_status=("${PIPESTATUS[@]}")
set -e
[[ "${context_status[1]}" -eq 0 ]] || { echo "ERROR: cannot retain architecture fetch log" >&2; exit 1; }
CONTEXT_RC="${context_status[0]}"
printf 'Architecture context fetch exit status: %s\n' "$CONTEXT_RC" | tee -a "$CONTEXT_LOG"
printf '%s\n' "$CONTEXT_RC" > "$OUTPUT_DIR/architecture-context-fetch.exit-code"
if [[ "$CONTEXT_RC" -ne 0 ]]; then
  echo "WARNING: architecture context setup failed; proceeding under existing optional-context behavior. See architecture-context-fetch.log."
fi

export CLAUDE_CODE_ENABLE_TELEMETRY=1
export OTEL_METRICS_EXPORTER=otlp
export OTEL_LOGS_EXPORTER=otlp
export OTEL_EXPORTER_OTLP_PROTOCOL=http/json
export OTEL_EXPORTER_OTLP_ENDPOINT=http://127.0.0.1:4318
export OTEL_METRIC_EXPORT_INTERVAL=10000

python3 "$CI_SCRIPTS/otel-collector.py" &
COLLECTOR_PID=$!
for ((attempt = 0; attempt < 30; attempt++)); do
  if python3 -c 'import socket; s=socket.create_connection(("127.0.0.1", 4318), 1); s.close()' >/dev/null 2>&1; then
    break
  fi
  kill -0 "$COLLECTOR_PID" 2>/dev/null || { echo "OTEL collector exited during startup" >&2; exit 1; }
  sleep 1
done
kill -0 "$COLLECTOR_PID" 2>/dev/null || { echo "OTEL collector did not start" >&2; exit 1; }

rm -f "$LOCKED_FILE"
if [[ "$MODE" == batch-discover ]]; then
  args=(--jql-default)
  [[ -z "${BATCH_SIZE:-}" ]] || args+=(--batch-size "$BATCH_SIZE")
  [[ -z "${BATCH_OFFSET:-}" ]] || args+=(--batch-offset "$BATCH_OFFSET")
  # Avoid process substitution here: discovery failures must propagate.
  discovered="$(python3 "$ROOT/scripts/list-rfe-ids.py" "${args[@]}")"
  CANDIDATES=()
  while IFS= read -r key; do
    [[ -z "$key" ]] && continue
    [[ "$key" =~ ^RHAIRFE-[0-9]+$ ]] || { echo "Invalid discovered key: $key" >&2; exit 1; }
    CANDIDATES+=("$key")
  done <<<"$discovered"
  echo "Discovered RFE_IDS: ${CANDIDATES[*]}"
  # Optional acceptance safety bound; discovery/filtering still uses native JQL.
  if [[ -n "${BATCH_EXPECTED_KEYS:-}" && "${CANDIDATES[*]}" != "$BATCH_EXPECTED_KEYS" ]]; then
    echo "ERROR: discovered candidates differ from expected bound" >&2; exit 1
  fi
fi
if [[ ${#CANDIDATES[@]} -eq 0 ]]; then
  echo "No work: discovery returned no eligible RFEs."
  exit 0
fi
LOCKED_KEYS="$(python3 "$ROOT/scripts/lock_issues.py" lock --locked-keys-file "$LOCKED_FILE" "${CANDIDATES[@]}")"
if [[ -z "$LOCKED_KEYS" ]]; then
  echo "No work: candidates are already locked or blocked by Jira labels."
  exit 0
fi
# Check the recorded acquired subset, not all discovery candidates.
read -r -a owned <<<"$LOCKED_KEYS"
recorded="$(tr '\n' ' ' < "$LOCKED_FILE" | xargs)"
[[ "$recorded" == "${owned[*]}" ]] || { echo "ERROR: lock record mismatch" >&2; exit 1; }
for key in "${owned[@]}"; do
  [[ " ${CANDIDATES[*]} " == *" $key "* ]] || { echo "ERROR: unexpected acquired key" >&2; exit 1; }
done
export FULLSEND_OWNED_RFE_KEYS="${owned[*]}"
"$CI_SCRIPTS/run-claude.sh" "/strategy-create ${owned[*]}"

mapfile -t strategy_files < <(find "$ARTIFACTS/strat-tasks" -maxdepth 1 -type f -name 'RHAISTRAT-*.md' -print 2>/dev/null | sort)
if [[ ${#strategy_files[@]} -ne ${#owned[@]} ]]; then
  echo "ERROR: expected ${#owned[@]} created strategies; found ${#strategy_files[@]}" >&2
  exit 1
fi
for file in "${strategy_files[@]}"; do
  "$CI_SCRIPTS/run-claude.sh" "/strategy-refine $(basename "$file" .md)"
done
python3 "$ROOT/scripts/push_refined_strategies.py" --artifacts-dir "$ARTIFACTS/strat-tasks"
for file in "${strategy_files[@]}"; do
  "$CI_SCRIPTS/run-claude.sh" "/strategy-review $(basename "$file" .md)"
done
label=single-rfe
[[ "$MODE" != batch-discover ]] || label=pipeline-settings
"$CI_SCRIPTS/pipeline-post.sh" "$label"
python3 "$CI_SCRIPTS/otel-summary.py" "$OTEL_LOG_FILE"
