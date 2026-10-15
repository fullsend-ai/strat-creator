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
python3 "${ROOT}/scripts/lock_issues.py" unlock "${keys[@]}"
# Keep the record as evidence, renamed so a rerun does not treat it as live.
mv -f -- "${RECORD}" "${RECORD}.released"
