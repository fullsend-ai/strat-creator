#!/usr/bin/env bash
# validation_loop script for strat-single, strat-batch and strat-resume.
# fullsend runs it on the host in the iteration directory, with TARGET_REPO_DIR
# set to the downloaded (agent-writable) repository and the runner
# environment, which includes STRAT_STATE_DIR. Self-contained like the
# pre-script: the checker is the pinned host clone's validate_result.py, which
# reads the repository and the output as data; nothing from them is executed.
set -euo pipefail

: "${FULLSEND_OUTPUT_SCHEMA:?FULLSEND_OUTPUT_SCHEMA must be set}"
: "${STRAT_STATE_DIR:?STRAT_STATE_DIR must be set}"
CLONE="${STRAT_STATE_DIR}/strat-creator"
# Validation failures do not invoke post_script. Preserve data for an explicit
# later host-prepared resume without running anything from the downloaded repo.
retain_failed_run() {
  code=$?
  trap - EXIT
  if [[ "$code" != 0 && -n "${TARGET_REPO_DIR:-}" && -d "$TARGET_REPO_DIR" &&
        -n "${STRAT_CREATOR_REF:-}" &&
        "$(git -C "$CLONE" rev-parse HEAD 2>/dev/null || true)" == "$STRAT_CREATOR_REF" ]]; then
    python3 "$CLONE/.fullsend/shared/capture_failure.py" "$TARGET_REPO_DIR" \
      "$STRAT_STATE_DIR" "output/agent-result.json" || echo "FAIL: recovery capture failed" >&2
  fi
  exit "$code"
}
trap retain_failed_run EXIT
RESULT_FILE="output/agent-result.json"

if [[ ! -f "${RESULT_FILE}" ]]; then
  # fullsend appends this text to the next attempt's prompt.
  echo "FAIL: ${RESULT_FILE} not found: the previous attempt stopped before its last step."
  echo "FAIL: Check python3 scripts/strat_pipeline_state.py completion-status first. If work_complete is true, restore output with the result command and retry reporting without repeating the Workflow."
  echo "FAIL: Preserve this failed run. Start a new host-prepared strat-resume run with verified prior progress; do not relaunch the Workflow in this prepared run."
  exit 1
fi
if [[ -z "${TARGET_REPO_DIR:-}" || ! -d "${TARGET_REPO_DIR}" ]]; then
  echo "FAIL: downloaded repository unavailable; cannot check artifacts"
  exit 1
fi

if [[ -z "${STRAT_CREATOR_REF:-}" || "$(git -C "${CLONE}" rev-parse HEAD 2>/dev/null || true)" != "${STRAT_CREATOR_REF}" ]]; then
  echo "FAIL: the host clone of strat-creator at ${CLONE} is missing or not at STRAT_CREATOR_REF"
  exit 1
fi

python3 "${CLONE}/.fullsend/shared/validate_result.py" \
  --result "${RESULT_FILE}" \
  --repo "${TARGET_REPO_DIR}" \
  --schema "${FULLSEND_OUTPUT_SCHEMA}" \
  --run "${STRAT_STATE_DIR}/run.json"
