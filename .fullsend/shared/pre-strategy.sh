#!/usr/bin/env bash
# Host pre-script for strat-single, strat-batch and strat-resume. fullsend runs
# it on the runner before the sandbox exists, with TARGET_REPO_DIR set to the
# trusted checkout of the repository the run happens in (strat-creator itself,
# or a consumer that installs these harnesses with base:).
#
# Self-contained on purpose: a consumer gets this file alone, fetched from the
# pinned harness URL, so every helper comes from a host clone of strat-creator
# at STRAT_CREATOR_REF, verified, in STRAT_STATE_DIR (never in the sandbox).
#   1. clone strat-creator at STRAT_CREATOR_REF (reused when already there);
#   2. vendor its skills, the strat-scorer agent, scripts/, config/ and
#      workflows/ into the target (vendor.py; skipped for strat-creator itself);
#   3. remove output left by earlier runs, so the agent and the validator only
#      see this run's artifacts and progress;
#   4. fetch the architecture context on the host, and fail before taking any
#      lock when it is missing or incomplete;
#   5. choose RFEs, compute their resume points and lock them once
#      (prepare_run.py); nothing to do -> exit 78, no sandbox, no model.
# If anything after the lock fails, the CI cleanup (release-locks.sh in the
# clone) still releases what the lock record lists.
set -euo pipefail

: "${TARGET_REPO_DIR:?TARGET_REPO_DIR must be set}"
: "${STRAT_STATE_DIR:?STRAT_STATE_DIR must name a host-only directory}"
MODE="${STRAT_MODE:?STRAT_MODE must be single, batch or resume}"
REF="${STRAT_CREATOR_REF:?STRAT_CREATOR_REF must be the strat-creator commit this harness is pinned to}"
REPO_URL="${STRAT_CREATOR_REPO:-https://github.com/opendatahub-io/strat-creator.git}"
CLONE="${STRAT_STATE_DIR}/strat-creator"
case "${MODE}" in single|batch|resume) ;; *) echo "STRAT_MODE must be single, batch or resume" >&2; exit 1 ;; esac
if [[ ! "${REF}" =~ ^[0-9a-f]{40}$ ]]; then
  echo "strat-${MODE} pre-script: STRAT_CREATOR_REF must be a full 40-character commit sha" >&2
  exit 1
fi
echo "strat-${MODE} pre-script: preparing ${TARGET_REPO_DIR} with strat-creator ${REF:0:12}"

mkdir -p -- "${STRAT_STATE_DIR}"
if [[ "$(git -C "${CLONE}" rev-parse HEAD 2>/dev/null || true)" != "${REF}" ]]; then
  rm -rf -- "${CLONE}"
  git init -q -- "${CLONE}"
  git -C "${CLONE}" fetch -q --depth 1 "${REPO_URL}" "${REF}"
  git -C "${CLONE}" -c advice.detachedHead=false checkout -q FETCH_HEAD
fi
if [[ "$(git -C "${CLONE}" rev-parse HEAD)" != "${REF}" ]]; then
  echo "strat-${MODE} pre-script: clone of ${REPO_URL} is not at ${REF}" >&2
  exit 1
fi

cd -- "${TARGET_REPO_DIR}"
# A repeated pre-script for the same prepared run must preserve recovery data.
# An absent host record, different run, or lost/foreign owner is not adopted.
if [[ -f tmp/strat-input.json && -f "${STRAT_STATE_DIR}/run.json" ]]; then
  if python3 - "${CLONE}" "${STRAT_STATE_DIR}/run.json" <<'PY'
import json, os, sys
from pathlib import Path
sys.path.insert(0, str(Path(sys.argv[1]) / 'scripts'))
from jira_utils import require_env
from strat_side_effects import check_owner
from strat_freshness import verify
saved = json.load(open(sys.argv[2]))
current = json.load(open('tmp/strat-input.json'))
if current['run_id'] != os.environ['STRAT_RUN_ID']:
    sys.exit(78)
if current != saved:
    sys.exit('pre-script: prepared input differs from host run record')
if 'freshness' not in current:
    sys.exit('pre-script: legacy prepared run lacks freshness; recovery data preserved')
if current.get('request') != {name: os.environ.get(name, '') for name in current.get('request', {})}:
    sys.exit('pre-script: invocation changed; recovery data preserved')
verify(Path.cwd(), current, trusted_source=Path(sys.argv[1]))
if not current['dry_run']:
    for key in current['acquired_keys']:
        check_owner(*require_env(), key, current)
sys.exit(0)
PY
  then
    echo "strat-${MODE} pre-script: reusing verified preparation; recovery data preserved"
    exit 0
  else
    rc=$?
    [[ "${rc}" == 78 ]] || exit "${rc}"
  fi
fi
# Do not erase uncertain work/ownership when preparation lost its final receipt.
if [[ -s "${STRAT_STATE_DIR}/owned-rfe-ids.txt" ]]; then
  echo "strat-${MODE} pre-script: outstanding lock intent; reconcile/release before new preparation" >&2
  exit 1
fi
if [[ -f tmp/strat-input.json && ! -f "${STRAT_STATE_DIR}/run.json" ]]; then
  python3 - "${CLONE}" <<'PY'
import json, os, sys
from pathlib import Path
sys.path.insert(0, str(Path(sys.argv[1]) / 'scripts'))
from jira_utils import get_issue, require_env
from strat_side_effects import OWNER_PREFIX, digest
previous = json.load(open('tmp/strat-input.json'))
if previous['run_id'] == os.environ['STRAT_RUN_ID']:
    sys.exit('pre-script: prepared input has no host record; recovery data preserved')
if not previous['dry_run']:
    credentials = require_env()
    expected_server = previous.get('freshness', {}).get('server')
    if not expected_server or expected_server != digest(credentials[0].rstrip('/')):
        sys.exit('pre-script: previous upstream/ownership cannot be verified; recovery data preserved')
    for key in previous['acquired_keys']:
        labels = get_issue(*credentials, key, fields=['labels'])['fields'].get('labels', [])
        if 'strat-creator-processing' in labels or any(x.startswith(OWNER_PREFIX) for x in labels):
            sys.exit('pre-script: previous owner has not released its claim; recovery data preserved')
PY
fi
python3 "${CLONE}/.fullsend/shared/vendor.py" "${CLONE}" "${TARGET_REPO_DIR}" "${REF}"
# The previous run's progress may live in this checkout's artifacts/, which the
# clean-up below removes: keep a host copy for prepare_run.py first.
if [[ -n "${STRAT_PREVIOUS_PROGRESS:-}" && -f "${STRAT_PREVIOUS_PROGRESS}" ]]; then
  cp -f -- "${STRAT_PREVIOUS_PROGRESS}" "${STRAT_STATE_DIR}/previous-progress.json"
  export STRAT_PREVIOUS_PROGRESS="${STRAT_STATE_DIR}/previous-progress.json"
fi
rm -rf -- artifacts/strat-tasks artifacts/strat-reviews artifacts/strat-originals \
  artifacts/reports tmp
rm -f -- artifacts/strat-skipped.md artifacts/strat-tickets.md artifacts/pipeline-data.json \
  artifacts/strat-progress.json

# STRAT_ARCHITECTURE_CONTEXT_PATH optionally names a local copy (the fetch
# script's local mode), for a CI mirror or a test fixture.
rm -rf -- .context/architecture-context
if ! env -u RFE_SKIP_BOOTSTRAP timeout 180 bash "${CLONE}/scripts/fetch-architecture-context.sh" \
    ${STRAT_ARCHITECTURE_CONTEXT_PATH:+"${STRAT_ARCHITECTURE_CONTEXT_PATH}"}; then
  echo "strat-${MODE} pre-script: architecture context fetch failed; no RFE was locked" >&2
  exit 1
fi
version="$(cat .context/architecture-context/LATEST_VERSION 2>/dev/null || true)"
if [[ -z "${version}" || ! -f ".context/architecture-context/architecture/${version}/PLATFORM.md" ]]; then
  echo "strat-${MODE} pre-script: architecture context incomplete; no RFE was locked" >&2
  exit 1
fi
echo "strat-${MODE} pre-script: architecture context ${version}"

set +e
python3 "${CLONE}/.fullsend/shared/prepare_run.py" "${MODE}"
rc=$?
set -e
case "${rc}" in
  0) echo "strat-${MODE} pre-script: complete" ;;
  78) exit 78 ;;
  *) echo "strat-${MODE} pre-script: prepare_run.py failed (${rc})" >&2; exit "${rc}" ;;
esac
