#!/usr/bin/env bash
# Outer CI cleanup: release every Jira lock this run recorded as owned.
# Run after `fullsend run` returns, whatever its exit status. The host
# pre-script locks once with lock_issues.py --locked-keys-file, so the record
# is written in the CI job container before any sandbox exists and survives
# agent, validation, post-script and sandbox failure. Only loss of the job
# pod itself needs reconciliation in Jira (README.md).
#
# Usage: release-locks.sh <strat-state-dir> [<trusted-checkout>]
set -euo pipefail

STATE_DIR="${1:?usage: release-locks.sh <strat-state-dir> [<trusted-checkout>]}"
ROOT="${2:-$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)}"
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
    echo "ERROR: invalid key in ${RECORD}: ${key}" >&2
    exit 1
  fi
  [[ " ${keys[*]} " == *" ${key} "* ]] || keys+=("${key}")
done < "${RECORD}"

if [[ ${#keys[@]} -eq 0 ]]; then
  echo "release-locks: lock record is empty"
  exit 0
fi
echo "release-locks: releasing ${keys[*]}"
python3 "${ROOT}/scripts/lock_issues.py" unlock "${keys[@]}"
# Keep the record for evidence, but mark it released so a rerun of this
# state directory does not mistake it for live locks.
mv -f -- "${RECORD}" "${RECORD}.released"
