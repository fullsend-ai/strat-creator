#!/usr/bin/env bash
# Host post-script for strat-single and strat-batch. Fullsend runs it only
# after validate-output.sh passed. It hands the validated artifacts to a
# publication workspace and publishes them with trusted helpers from
# TARGET_REPO_DIR (the trusted checkout). The downloaded repository (REPO_DIR)
# is agent-writable: only regular files under artifacts/ are copied from it,
# as data. Lock release is not done here; the outer CI runs release-locks.sh
# whether or not this script runs.
set -euo pipefail

SHARED="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
: "${TARGET_REPO_DIR:?TARGET_REPO_DIR must be set}"
: "${FULLSEND_VALIDATED_ITERATION_DIR:?no validated iteration}"
RESULT_FILE="${FULLSEND_VALIDATED_ITERATION_DIR}/agent-result.json"

field() {
  python3 -c 'import json, sys; print(json.load(open(sys.argv[1]))[sys.argv[2]])' \
    "${RESULT_FILE}" "$1"
}
ACTION="$(field action)"
MODE="$(field mode)"
echo "Result: action=${ACTION} mode=${MODE} run=$(field run_id)"
echo "Summary: $(field summary | tr -s '[:space:]' ' ' | cut -c1-400)"

case "${ACTION}" in
  skipped) echo "Nothing to publish."; exit 0 ;;
  completed) ;;
  *) echo "unexpected validated action: ${ACTION}" >&2; exit 1 ;;
esac

if [[ -z "${REPO_DIR:-}" || ! -d "${REPO_DIR}/artifacts" ]]; then
  echo "ERROR: downloaded repository artifacts are unavailable" >&2
  exit 1
fi

PUBLISH_DIR="${STRAT_PUBLISH_DIR:-${FULLSEND_RUN_DIR:-${PWD}}/publish}"
python3 "${SHARED}/handoff.py" "${REPO_DIR}" "${PUBLISH_DIR}/handoff"
cp -f "${RESULT_FILE}" "${PUBLISH_DIR}/handoff/agent-result.json"

label=single-rfe
[[ "${MODE}" != batch ]] || label=pipeline-settings
bash "${SHARED}/publish-results.sh" "${PUBLISH_DIR}" "${label}"
