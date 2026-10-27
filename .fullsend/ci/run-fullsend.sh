#!/usr/bin/env bash
# One invocation of a versioned Workflow harness; no custom agent entrypoint.
set +x
set -Eeuo pipefail
umask 077
AGENT="${1:-}"
case "$AGENT" in strat-single|strat-batch|strat-resume) ;; *) echo 'Expected strat-single, strat-batch, or strat-resume' >&2; exit 2 ;; esac
: "${CI_PROJECT_DIR:?}" "${CI_JOB_ID:?}"
[[ "$CI_JOB_ID" =~ ^[0-9]+$ ]] || exit 2
CI_SCRIPTS="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
case "$AGENT" in
  strat-single) expected_mode=single ;;
  strat-batch) expected_mode="${CONFIG_FILE:+config}"; expected_mode="${expected_mode:-batch}" ;;
  strat-resume) expected_mode=reprocess ;;
esac
export FULLSEND_MODE="${FULLSEND_MODE:-$expected_mode}"
[[ "$FULLSEND_MODE" == "$expected_mode" ]] || { echo 'Agent and FULLSEND_MODE disagree' >&2; exit 2; }
export CONFIG_FILE="${CONFIG_FILE:-}" RFE_FILTER="${RFE_FILTER:-}"
export STRAT_REPROCESS_KEY= FULLSEND_WORK_ITEM_KEY=
if [[ "$FULLSEND_MODE" == reprocess ]]; then
  export STRAT_REPROCESS_KEY="${STRAT_KEY:-}" FULLSEND_WORK_ITEM_KEY="${STRAT_KEY:-}"
fi
export STRAT_DRY_RUN="${STRAT_DRY_RUN:-1}" FULLSEND_PUBLISH="${FULLSEND_PUBLISH:-0}"
export BATCH_SIZE="${BATCH_SIZE:-10}" BATCH_OFFSET="${BATCH_OFFSET:-0}"
python3 "$CI_SCRIPTS/fullsend-config.py" validate "$CI_PROJECT_DIR"
set -a
source "$CI_SCRIPTS/fullsend-versions.env"
set +a
export STRAT_PRIVATE_DIR="$CI_PROJECT_DIR/.fullsend-state/$CI_JOB_ID"
export FULLSEND_OUTPUT_DIR="$CI_PROJECT_DIR/fullsend-output"
[[ ! -e "$STRAT_PRIVATE_DIR" ]] || { echo 'Preserve the existing prepared run before retrying' >&2; exit 1; }
mkdir -p "$STRAT_PRIVATE_DIR" "$FULLSEND_OUTPUT_DIR"
export STRAT_STATE_DIR="$STRAT_PRIVATE_DIR/strat-state"
export STRAT_PUBLISH_DIR="$STRAT_PRIVATE_DIR/publish"
export TARGET_REPO_DIR="$STRAT_PRIVATE_DIR/target"
export STRAT_RUNTIME_DIR="$STRAT_PRIVATE_DIR/fullsend"
export STRAT_RUN_ID="gitlab-job-$CI_JOB_ID"
export BATCH_EXPECTED_KEYS= STRAT_PREVIOUS_PROGRESS=
export JIRA_TOKEN="${JIRA_TOKEN:-${JIRA_API_TOKEN:-}}"
export ANTHROPIC_VERTEX_PROJECT_ID="${ANTHROPIC_VERTEX_PROJECT_ID:-${AIPCC_CICD_GCP_PROJECT_ID:-${GCP_PROJECT_ID:-}}}"
export GOOGLE_CLOUD_PROJECT="$ANTHROPIC_VERTEX_PROJECT_ID" CLOUD_ML_REGION="${CLOUD_ML_REGION:-global}"
export GOOGLE_APPLICATION_CREDENTIALS="${AIPCC_CICD_GCP_SERVICE_ACCOUNT_KEY:-${GOOGLE_APPLICATION_CREDENTIALS:-}}"
export RESULTS_GIT_USER="${RESULTS_GIT_USER:-oauth2}"
export RESULTS_PUSH_TOKEN="${RESULTS_PUSH_TOKEN:-}"
PUBLISH_ORG_PULSE_URL="${ORG_PULSE_URL:-}"
PUBLISH_ORG_PULSE_TOKEN="${ORG_PULSE_API_TOKEN:-}"
export RESULTS_REPO_URL= ORG_PULSE_URL= ORG_PULSE_API_TOKEN=

cleanup() {
  local rc=$? release_rc=0 scrub_rc=0
  trap - EXIT HUP INT TERM
  set +e
  local release="$STRAT_STATE_DIR/strat-creator/.fullsend/shared/release-locks.sh"
  if [[ -f "$release" ]]; then
    if [[ "$(git -C "$STRAT_STATE_DIR/strat-creator" rev-parse HEAD 2>/dev/null)" == "$STRAT_CREATOR_REF" ]]; then
      bash "$release" "$STRAT_STATE_DIR" "$TARGET_REPO_DIR" 2>&1 \
        | python3 "$CI_SCRIPTS/fullsend-redact.py" stream
      release_rc=${PIPESTATUS[0]}
    else
      echo 'Unverified host clone: reconcile owned claims before manual cleanup' >&2
      release_rc=1
    fi
  fi
  mkdir -p "$FULLSEND_OUTPUT_DIR/host-state"
  for item in run.json owned-rfe-ids.txt owned-rfe-ids.txt.owner.json recovery; do
    [[ ! -e "$STRAT_STATE_DIR/$item" ]] || cp -R "$STRAT_STATE_DIR/$item" "$FULLSEND_OUTPUT_DIR/host-state/"
  done
  printf '{"run_exit":%s,"release_exit":%s}\n' "$rc" "$release_rc" >"$FULLSEND_OUTPUT_DIR/exit-status.json"
  python3 "$CI_SCRIPTS/fullsend-redact.py" tree "$FULLSEND_OUTPUT_DIR" || scrub_rc=$?
  if [[ "$scrub_rc" != 0 ]]; then
    # This is the fixed, generated artifact directory, never a user input.
    rm -rf -- "$CI_PROJECT_DIR/fullsend-output"
  fi
  [[ "$rc" != 0 ]] || rc=$release_rc
  [[ "$rc" != 0 ]] || rc=$scrub_rc
  exit "$rc"
}
trap cleanup EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM
: "${JIRA_SERVER:?}" "${JIRA_USER:?}" "${JIRA_TOKEN:?}" "${ANTHROPIC_VERTEX_PROJECT_ID:?}"
[[ -f "$GOOGLE_APPLICATION_CREDENTIALS" ]] || { echo 'Provide the Vertex service account as a GitLab File variable' >&2; exit 2; }
if [[ "$FULLSEND_PUBLISH" == 1 ]]; then
  : "${RESULTS_PUSH_TOKEN:?}" "${RESULTS_REPO:?}"
  [[ "$RESULTS_REPO" =~ ^[A-Za-z0-9_.-]+(/[A-Za-z0-9_.-]+)+$ ]] || exit 2
  export RESULTS_REPO_URL="https://gitlab.com/$RESULTS_REPO.git"
  export ORG_PULSE_URL="$PUBLISH_ORG_PULSE_URL" ORG_PULSE_API_TOKEN="$PUBLISH_ORG_PULSE_TOKEN"
fi
# The production path uses direct Vertex ADC, regardless of inherited gateway settings.
unset ANTHROPIC_API_KEY ANTHROPIC_AUTH_TOKEN ANTHROPIC_BASE_URL ANTHROPIC_VERTEX_BASE_URL
unset FULLSEND_MODEL FULLSEND_FALLBACK_MODELS FULLSEND_RUNTIME
export CLAUDE_CODE_USE_VERTEX=1 DISABLE_AUTOUPDATER=1
python3 "$CI_SCRIPTS/fullsend-config.py" source "$TARGET_REPO_DIR" "$FULLSEND_OUTPUT_DIR/source.json"
python3 "$CI_SCRIPTS/fullsend-config.py" runtime "$STRAT_RUNTIME_DIR"
export STRAT_ARCHITECTURE_CONTEXT_PATH="$STRAT_PRIVATE_DIR/architecture-context"
python3 "$CI_SCRIPTS/fullsend-config.py" context "$STRAT_ARCHITECTURE_CONTEXT_PATH" "$FULLSEND_OUTPUT_DIR/architecture-source.json"

mkdir -p "$STRAT_PRIVATE_DIR/tools"
archive="$STRAT_PRIVATE_DIR/tools/fullsend.tar.gz"
curl -fsSL --retry 3 --output "$archive" \
  "https://github.com/fullsend-ai/fullsend/releases/download/v$FULLSEND_VERSION/fullsend_${FULLSEND_VERSION}_linux_amd64.tar.gz"
printf '%s  %s\n' "$FULLSEND_TARBALL_SHA256" "$archive" | sha256sum -c -
tar -xzf "$archive" -C "$STRAT_PRIVATE_DIR/tools" fullsend
export PATH="$STRAT_PRIVATE_DIR/tools:$PATH"
fullsend --version >"$FULLSEND_OUTPUT_DIR/fullsend-version.txt"
python3 -c 'import jsonschema, yaml, certifi, diff_match_patch'

set +e
bash "$CI_SCRIPTS/with-fullsend-gateway.sh" -- fullsend run "$AGENT" \
  --fullsend-dir "$STRAT_RUNTIME_DIR" --target-repo "$TARGET_REPO_DIR" \
  --model claude-opus-4-6 --effort high --output-dir "$FULLSEND_OUTPUT_DIR/runs" --debug=api,hooks \
  2>&1 | python3 "$CI_SCRIPTS/fullsend-redact.py" stream
codes=("${PIPESTATUS[@]}")
set -e
[[ "${codes[0]}" == 0 ]] || exit "${codes[0]}"
exit "${codes[1]}"
