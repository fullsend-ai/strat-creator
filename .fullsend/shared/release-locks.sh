#!/usr/bin/env bash
# CI cleanup: release every Jira lock this run recorded. Run it after
# `fullsend run` returns, whatever its exit status (an `after_script`, a
# `finally`, or `if: always()`). prepare_run.py writes the record on the host
# before any sandbox exists, so it survives agent, validation, post-script
# and sandbox failure. Only losing the CI job itself leaves locks behind; the
# README says how to find and release them.
#
# Usage: release-locks.sh <strat-state-dir>
set -euo pipefail

STATE_DIR="${1:?usage: release-locks.sh <strat-state-dir>}"
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
RECORD="${STATE_DIR}/owned-rfe-ids.txt"

if [[ ! -f "${RECORD}" ]]; then
  echo "release-locks: no lock record at ${RECORD}"
  exit 0
fi
keys=()
while IFS= read -r key || [[ -n "${key}" ]]; do
  key="${key//[[:space:]]/}"
  [[ -z "${key}" ]] && continue
  if [[ ! "${key}" =~ ^RHAIRFE-[0-9]+$ ]]; then
    echo "release-locks: ERROR: invalid key in ${RECORD}: ${key}" >&2
    exit 1
  fi
  [[ " ${keys[*]-} " == *" ${key} "* ]] || keys+=("${key}")
done < "${RECORD}"

if [[ ${#keys[@]} -eq 0 ]]; then
  echo "release-locks: lock record is empty"
  exit 0
fi
echo "release-locks: releasing ${keys[*]}"
OWNER_RECORD="${RECORD}.owner.json"
if [[ ! -f "${OWNER_RECORD}" ]]; then
  echo "release-locks: ERROR: no owner record; reconcile legacy locks manually" >&2
  exit 1
fi
owner=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["owner"])' "${OWNER_RECORD}")
# Release is owner-checked cleanup, independent of the failed work's freshness.
# An isolated directory prevents discovering the target's prepared input or
# inheriting its mutation journal. Keep the original host record on any error.
RECORD="$(cd -- "$(dirname -- "${RECORD}")" && pwd)/$(basename -- "${RECORD}")"
cleanup=$(mktemp -d)
trap 'rmdir -- "${cleanup}"' EXIT
(
  cd -- "${cleanup}"
  unset STRAT_EFFECTS_DIR STRAT_RUN_ID
  failed=false
  for key in "${keys[@]}"; do
    if ! python3 "${ROOT}/scripts/lock_issues.py" unlock --owner "${owner}" "${key}"; then
      failed=true
    fi
  done
  [[ "${failed}" == false ]]
)
# Keep the record as evidence, renamed so a rerun does not treat it as live.
mv -f -- "${RECORD}" "${RECORD}.released"
