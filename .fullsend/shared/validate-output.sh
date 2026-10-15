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
RESULT_FILE="output/agent-result.json"

if [[ ! -f "${RESULT_FILE}" ]]; then
  # fullsend appends this text to the next attempt's prompt.
  echo "FAIL: ${RESULT_FILE} not found: the previous attempt stopped before its last step."
  echo "FAIL: Resume: call the Workflow tool with name \"strat-pipeline:strat-pipeline\" again; it reads tmp/strat-progress.json and runs only the steps that are not recorded."
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

exec python3 "${CLONE}/.fullsend/shared/validate_result.py" \
  --result "${RESULT_FILE}" \
  --repo "${TARGET_REPO_DIR}" \
  --schema "${FULLSEND_OUTPUT_SCHEMA}" \
  --run "${STRAT_STATE_DIR}/run.json"
