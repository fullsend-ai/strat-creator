#!/usr/bin/env bash
# Publish a validated strategy run: report, per-run JSON, results repository,
# summaries and Org Pulse. Adapted from the script-led POC's pipeline-post.sh
# (strat-pipeline ci-scripts/pipeline-post.sh; see ../SOURCE-PROVENANCE.md).
# Runs on the host from the trusted checkout. Without RESULTS_REPO_URL the
# run is assembled locally under <publish-dir>/results and not pushed.
#
# Usage: publish-results.sh <publish-dir> <config-label>
set -euo pipefail

PUBLISH_DIR="${1:?usage: publish-results.sh <publish-dir> <config-label>}"
CONFIG_LABEL="${2:?usage: publish-results.sh <publish-dir> <config-label>}"
SHARED="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
WORKDIR="${TARGET_REPO_DIR:?TARGET_REPO_DIR must be set}"
HANDOFF="${PUBLISH_DIR}/handoff"
RESULTS="${PUBLISH_DIR}/results"

if [[ -n "${RESULTS_REPO_URL:-}" ]]; then
  : "${RESULTS_PUSH_TOKEN:?RESULTS_PUSH_TOKEN is required to publish results}"
  auth="$(printf '%s:%s' "${RESULTS_GIT_USER:-oauth2}" "${RESULTS_PUSH_TOKEN}" | base64 | tr -d '\n')"
  export GIT_CONFIG_COUNT=1
  export GIT_CONFIG_KEY_0="http.${RESULTS_REPO_URL}.extraHeader"
  export GIT_CONFIG_VALUE_0="Authorization: Basic ${auth}"
  unset auth
  rm -rf -- "${RESULTS}"
  bash "${SHARED}/publish/clone-data-repo.sh" "${RESULTS_REPO_URL}" "${RESULTS}"
else
  echo "INFO: RESULTS_REPO_URL not set; assembling results locally only"
  rm -rf -- "${RESULTS}"
  mkdir -p "${RESULTS}"
fi

# Historical runs stay under RHAISTRAT/; working output comes only from the
# validated handoff of this run.
for dir in strat-tasks strat-reviews strat-originals reports; do
  rm -rf -- "${RESULTS:?}/${dir}"
done
rm -f -- "${RESULTS}/strat-skipped.md" "${RESULTS}/strat-tickets.md" "${RESULTS}/pipeline-data.json"
cp -R "${HANDOFF}/." "${RESULTS}/"
rm -f -- "${RESULTS}/agent-result.json"

python3 "${WORKDIR}/scripts/generate-report.py" \
  --artifacts "${RESULTS}" \
  --output "${RESULTS}/reports/report.html"
python3 "${WORKDIR}/scripts/extract-pipeline-data.py" \
  --run-dir "${RESULTS}" \
  --output "${RESULTS}/pipeline-data.json" \
  --config "${CONFIG_LABEL}"

if [[ -z "${RESULTS_REPO_URL:-}" ]]; then
  echo "Results assembled at ${RESULTS}"
  exit 0
fi

python3 "${SHARED}/publish/push-results.py" \
  --results-dir "${RESULTS}" \
  --results-repo "${RESULTS_REPO_URL}"

cd "${RESULTS}"
git pull --rebase -X theirs origin main || true
python3 "${WORKDIR}/scripts/extract-pipeline-data.py" \
  --data-dir "${RESULTS}/RHAISTRAT" \
  --output "${RESULTS}/RHAISTRAT/summary.json" \
  --no-body
git add RHAISTRAT/summary.json RHAISTRAT/summary-production.json
if git diff --cached --quiet; then
  echo "Summaries unchanged, nothing to push."
else
  git commit -m "Update summaries"
  push_ok=false
  for attempt in 1 2 3; do
    if git push origin HEAD; then push_ok=true; break; fi
    echo "Summary push failed (attempt ${attempt}/3), pulling and retrying..."
    git pull --rebase -X theirs origin main || true
  done
  if [[ "${push_ok}" == false ]]; then
    echo "ERROR: summary push failed after 3 attempts" >&2
    exit 1
  fi
fi

if [[ -n "${ORG_PULSE_URL:-}" && -n "${ORG_PULSE_API_TOKEN:-}" ]]; then
  python3 "${SHARED}/publish/push-to-org-pulse.py" \
    --results-dir "${RESULTS}" || echo "WARNING: Org Pulse push failed (non-blocking)"
else
  echo "INFO: ORG_PULSE_URL or ORG_PULSE_API_TOKEN not set, skipping Org Pulse push"
fi
unset RESULTS_PUSH_TOKEN GIT_CONFIG_VALUE_0
