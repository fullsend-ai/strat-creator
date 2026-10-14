#!/bin/bash
# Source: strat-pipeline ci-scripts/pipeline-post.sh at fd36b15c5095c9f20a270b1d69933c578c04d9da.
# Adapted: run inside the checked-out strat-creator tree and use explicit
# environment variables rather than CI_PROJECT_DIR and /root/.tokens.
# Post-pipeline steps: report, extract JSON, push data, summaries, org pulse.
# Called by all 4 CI jobs after strategy processing completes.
#
# Usage: bash ci-scripts/pipeline-post.sh <config-label>
#   config-label: "pipeline-settings", "${CONFIG_FILE}", "single-rfe", "reprocess"
set -euo pipefail

CONFIG_LABEL="${1:?Usage: pipeline-post.sh <config-label>}"
WORKDIR="${STRAT_CREATOR_ROOT:-$PWD}"
ARTIFACTS="$WORKDIR/artifacts"
CI_SCRIPTS="$WORKDIR/.fullsend/scripts/ci"

# Generate HTML report
REPORT_CONFIG=""
if [ "${CONFIG_LABEL}" != "pipeline-settings" ] && \
   [ "${CONFIG_LABEL}" != "single-rfe" ] && \
   [ "${CONFIG_LABEL}" != "reprocess" ]; then
  REPORT_CONFIG="--config ${WORKDIR}/${CONFIG_LABEL}"
fi
python3 "${WORKDIR}/scripts/generate-report.py" \
  --artifacts "$ARTIFACTS" \
  ${REPORT_CONFIG} \
  --output "$ARTIFACTS/reports/report.html"

# Extract per-run JSON
python3 "${WORKDIR}/scripts/extract-pipeline-data.py" \
  --run-dir "$ARTIFACTS" \
  --output "$ARTIFACTS/pipeline-data.json" \
  --config "${CONFIG_LABEL}"

# Copy OTEL data into artifacts so push-results.py can persist it
if [ -n "${OTEL_LOG_FILE:-}" ] && [ -f "$OTEL_LOG_FILE" ] && [ "$OTEL_LOG_FILE" != "$ARTIFACTS/claude-otel.jsonl" ]; then
  cp -f "$OTEL_LOG_FILE" "$ARTIFACTS/claude-otel.jsonl"
fi

# Push to data repo
if [ -n "${RESULTS_REPO_URL:-}" ]; then
  python3 "$CI_SCRIPTS/push-results.py" \
    --results-dir "$ARTIFACTS" \
    --results-repo "$RESULTS_REPO_URL"

  # Pull latest before regenerating summary (avoids "unstaged changes" error)
  cd "$ARTIFACTS"
  git pull --rebase -X theirs origin main || true

  # Regenerate aggregated summary across all runs
  python3 "${WORKDIR}/scripts/extract-pipeline-data.py" \
    --data-dir "$ARTIFACTS/RHAISTRAT" \
    --output "$ARTIFACTS/RHAISTRAT/summary.json" \
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
    if [ "$push_ok" = false ]; then
      echo "ERROR: summary push failed after 3 attempts"
      exit 1
    fi
  fi
fi

# Push feature data to org pulse (non-blocking)
if [ -n "${ORG_PULSE_URL:-}" ] && [ -n "${ORG_PULSE_API_TOKEN:-}" ]; then
  python3 "$CI_SCRIPTS/push-to-org-pulse.py" \
    --results-dir "$ARTIFACTS" || echo "WARNING: Org pulse push failed (non-blocking)"
else
  echo "INFO: ORG_PULSE_URL or ORG_PULSE_API_TOKEN not set, skipping org pulse push"
fi

unset RESULTS_PUSH_TOKEN GIT_CONFIG_VALUE_0
