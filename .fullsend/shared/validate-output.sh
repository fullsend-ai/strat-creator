#!/usr/bin/env bash
# Fullsend validation_loop script for strat-single and strat-batch.
# Runs on the host in the iteration directory, with TARGET_REPO_DIR set to the
# downloaded (agent-writable) repository and the runner environment,
# including STRAT_STATE_DIR (the host run and lock record). The repository and
# output are read as data by trusted validate_result.py; nothing from them is
# executed.
set -euo pipefail

: "${FULLSEND_OUTPUT_SCHEMA:?FULLSEND_OUTPUT_SCHEMA must be set}"
SHARED="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
RESULT_FILE="output/agent-result.json"

if [[ ! -f "${RESULT_FILE}" ]]; then
  # This text is appended to the retry prompt (feedback_mode: append).
  echo "FAIL: ${RESULT_FILE} not found: the previous attempt stopped before finishing."
  echo "FAIL: Resume this run: run 'bash .fullsend/shared/progress.sh read', complete only the phases and strategies without entries, then write the result."
  exit 1
fi
if [[ -z "${TARGET_REPO_DIR:-}" || ! -d "${TARGET_REPO_DIR}" ]]; then
  echo "FAIL: downloaded repository unavailable; cannot check artifacts"
  exit 1
fi

exec python3 "${SHARED}/validate_result.py" \
  --result "${RESULT_FILE}" \
  --output-dir output \
  --repo "${TARGET_REPO_DIR}" \
  --schema "${FULLSEND_OUTPUT_SCHEMA}"
