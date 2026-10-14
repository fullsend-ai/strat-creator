#!/usr/bin/env bash
# Host pre-script shared by strat-single and strat-batch. Runs before the
# sandbox exists, against the trusted checkout in TARGET_REPO_DIR:
#   1. remove working output left by earlier runs, so the agent only sees and
#      the validator only accepts this run's artifacts;
#   2. drop the unused local assessment directory from project settings;
#   3. fetch architecture context on the host, so the sandbox needs no
#      GitHub egress (RFE_SKIP_BOOTSTRAP=1 stops the skills fetching again);
#   4. choose and lock this run's RFEs (prepare_run.py), recording owned
#      keys in STRAT_STATE_DIR; nothing locked -> native skip (exit 78).
# Context is fetched before locking so a fetch failure takes no locks.
# Missing architecture context fails the run: reviews must not proceed
# without it (see README.md). If this script fails after locking, the outer
# CI still releases the recorded locks (release-locks.sh).
set -euo pipefail

MODE="${1:?usage: pre-strategy.sh single|batch}"
: "${TARGET_REPO_DIR:?TARGET_REPO_DIR must be set}"
SHARED="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd -- "${TARGET_REPO_DIR}"
echo "strat-${MODE} pre-script: preparing ${TARGET_REPO_DIR}"

rm -rf -- artifacts/strat-tasks artifacts/strat-reviews artifacts/strat-originals \
  artifacts/reports tmp
rm -f -- artifacts/strat-skipped.md artifacts/strat-tickets.md \
  artifacts/pipeline-data.json

python3 "${SHARED}/prepare-claude-settings.py" "${TARGET_REPO_DIR}"

# STRAT_ARCHITECTURE_CONTEXT_PATH optionally names a local copy (the fetch
# script's existing local mode), e.g. a CI mirror or a test fixture.
rm -rf -- .context/architecture-context
if ! env -u RFE_SKIP_BOOTSTRAP timeout 180 bash scripts/fetch-architecture-context.sh \
    ${STRAT_ARCHITECTURE_CONTEXT_PATH:+"${STRAT_ARCHITECTURE_CONTEXT_PATH}"}; then
  echo "strat-${MODE} pre-script: architecture context fetch failed" >&2
  exit 1
fi
version="$(cat .context/architecture-context/LATEST_VERSION 2>/dev/null || true)"
if [[ -z "${version}" || ! -f ".context/architecture-context/architecture/${version}/PLATFORM.md" ]]; then
  echo "strat-${MODE} pre-script: architecture context incomplete" >&2
  exit 1
fi
echo "strat-${MODE} pre-script: architecture context ${version}"

set +e
python3 "${SHARED}/prepare_run.py" "${MODE}"
rc=$?
set -e
case "${rc}" in
  0) ;;
  78) exit 78 ;;
  *) echo "strat-${MODE} pre-script: prepare_run failed (${rc})" >&2; exit "${rc}" ;;
esac
echo "strat-${MODE} pre-script: complete"
