#!/usr/bin/env bash
# Host post-script for strat-single, strat-batch and strat-resume. fullsend
# runs it only after validate-output.sh passed, with TARGET_REPO_DIR set to
# the trusted checkout the run happened in and REPO_DIR to the downloaded,
# agent-writable repository. Self-contained like the pre-script: helpers come
# from the pinned host clone in STRAT_STATE_DIR.
#   1. copy the artifacts the validated result lists out of REPO_DIR as data
#      (handoff.py) into the target's artifacts/, with the progress record a
#      later strat-resume run reads (artifacts/strat-progress.json);
#   2. build the HTML report and pipeline data with the trusted scripts;
#   3. when RESULTS_REPO_URL is set, push the run to the results repository,
#      and to Org Pulse when ORG_PULSE_URL and ORG_PULSE_API_TOKEN are set.
# Lock release is not done here: the CI cleanup (release-locks.sh) runs
# whether or not this script runs.
set -euo pipefail

: "${TARGET_REPO_DIR:?TARGET_REPO_DIR must be set}"
: "${STRAT_STATE_DIR:?STRAT_STATE_DIR must be set}"
CLONE="${STRAT_STATE_DIR}/strat-creator"
if [[ -z "${STRAT_CREATOR_REF:-}" || "$(git -C "${CLONE}" rev-parse HEAD 2>/dev/null || true)" != "${STRAT_CREATOR_REF}" ]]; then
  echo "ERROR: the host clone of strat-creator at ${CLONE} is missing or not at STRAT_CREATOR_REF" >&2
  exit 1
fi
SHARED="${CLONE}/.fullsend/shared"
: "${FULLSEND_VALIDATED_ITERATION_DIR:?no validated iteration}"
RESULT_FILE="${FULLSEND_VALIDATED_ITERATION_DIR}/agent-result.json"

field() {
  python3 -c 'import json, sys; print(json.load(open(sys.argv[1]))[sys.argv[2]])' "${RESULT_FILE}" "$1"
}
ACTION="$(field action)"
MODE="$(field mode)"
echo "Result: action=${ACTION} mode=${MODE} run=$(field run_id) dry_run=$(field dry_run)"
echo "Summary: $(field summary | tr -s '[:space:]' ' ' | cut -c1-400)"
if [[ "${ACTION}" != completed ]]; then
  echo "unexpected validated action: ${ACTION}" >&2
  exit 1
fi
if [[ -z "${REPO_DIR:-}" || ! -d "${REPO_DIR}" ]]; then
  echo "ERROR: downloaded repository is unavailable" >&2
  exit 1
fi

PUBLISH_DIR="${STRAT_PUBLISH_DIR:-${FULLSEND_RUN_DIR:-${PWD}}/publish}"
HANDOFF="${PUBLISH_DIR}/handoff"
RESULTS="${PUBLISH_DIR}/results"
mkdir -p -- "${PUBLISH_DIR}"
python3 "${SHARED}/handoff.py" "${REPO_DIR}" "${HANDOFF}" "${RESULT_FILE}" "${HANDOFF}/strat-progress.json"
cp -f "${RESULT_FILE}" "${HANDOFF}/agent-result.json"
# The run's record lives in the target repository: artifacts/ as CI uploads it.
mkdir -p -- "${TARGET_REPO_DIR}/artifacts"
for dir in strat-tasks strat-reviews strat-originals; do rm -rf -- "${TARGET_REPO_DIR}/artifacts/${dir}"; done
cp -R "${HANDOFF}/." "${TARGET_REPO_DIR}/artifacts/"

rm -rf -- "${RESULTS}"
if [[ -n "${RESULTS_REPO_URL:-}" ]]; then
  : "${RESULTS_PUSH_TOKEN:?RESULTS_PUSH_TOKEN is required with RESULTS_REPO_URL}"
  # Process-scoped Git auth: the token never lands in a remote URL or .git/config.
  auth="$(printf '%s:%s' "${RESULTS_GIT_USER:-oauth2}" "${RESULTS_PUSH_TOKEN}" | base64 | tr -d '\n')"
  export GIT_CONFIG_COUNT=1
  export GIT_CONFIG_KEY_0="http.${RESULTS_REPO_URL}.extraHeader"
  export GIT_CONFIG_VALUE_0="Authorization: Basic ${auth}"
  unset auth
  bash "${SHARED}/publish/clone-data-repo.sh" "${RESULTS_REPO_URL}" "${RESULTS}"
  for dir in strat-tasks strat-reviews strat-originals reports; do rm -rf -- "${RESULTS:?}/${dir}"; done
  rm -f -- "${RESULTS}/strat-skipped.md" "${RESULTS}/strat-tickets.md" "${RESULTS}/pipeline-data.json"
else
  mkdir -p "${RESULTS}"
fi
cp -R "${HANDOFF}/." "${RESULTS}/"
rm -f -- "${RESULTS}/agent-result.json" "${RESULTS}/strat-progress.json"

label=single-rfe
[[ "${MODE}" != batch ]] || label=pipeline-settings
python3 "${CLONE}/scripts/generate-report.py" \
  --artifacts "${RESULTS}" --output "${RESULTS}/reports/report.html"
python3 "${CLONE}/scripts/extract-pipeline-data.py" \
  --run-dir "${RESULTS}" --output "${RESULTS}/pipeline-data.json" --config "${label}"

if [[ -z "${RESULTS_REPO_URL:-}" ]]; then
  echo "INFO: RESULTS_REPO_URL not set; results assembled at ${RESULTS}, nothing pushed"
  exit 0
fi

python3 "${SHARED}/publish/push-results.py" --results-dir "${RESULTS}" --results-repo "${RESULTS_REPO_URL}"
cd -- "${RESULTS}"
git pull --rebase -X theirs origin main || true
python3 "${CLONE}/scripts/extract-pipeline-data.py" \
  --data-dir "${RESULTS}/RHAISTRAT" --output "${RESULTS}/RHAISTRAT/summary.json" --no-body
git add RHAISTRAT/summary.json RHAISTRAT/summary-production.json
if git diff --cached --quiet; then
  echo "Summaries unchanged, nothing to push."
else
  git commit -q -m "Update summaries"
  pushed=false
  for attempt in 1 2 3; do
    if git push origin HEAD; then pushed=true; break; fi
    echo "Summary push failed (attempt ${attempt}/3), pulling and retrying..."
    git pull --rebase -X theirs origin main || true
  done
  [[ "${pushed}" == true ]] || { echo "ERROR: summary push failed after 3 attempts" >&2; exit 1; }
fi

if [[ -n "${ORG_PULSE_URL:-}" && -n "${ORG_PULSE_API_TOKEN:-}" ]]; then
  python3 "${SHARED}/publish/push-to-org-pulse.py" --results-dir "${RESULTS}" \
    || echo "WARNING: Org Pulse push failed (non-blocking)"
else
  echo "INFO: ORG_PULSE_URL or ORG_PULSE_API_TOKEN not set, skipping Org Pulse"
fi
