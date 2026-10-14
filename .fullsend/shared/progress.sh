#!/usr/bin/env bash
# Record strategy-agent progress for the current run.
#
# The canonical file is tmp/strat-progress.yaml in the repository. A
# validation retry runs in the same sandbox but Fullsend clears
# $FULLSEND_OUTPUT_DIR first, so only the repository copy survives to drive
# resume. Every update is mirrored to $FULLSEND_OUTPUT_DIR, which the host
# collects after each iteration (pass or fail) and the validator reads.
#
# Usage (from the repository root):
#   bash .fullsend/shared/progress.sh read
#   bash .fullsend/shared/progress.sh init run_id=<id> mode=<single|batch> [...]
#   bash .fullsend/shared/progress.sh set key=value [...]
#   bash .fullsend/shared/progress.sh mark key [...]     # key=<UTC timestamp>
set -euo pipefail

P=tmp/strat-progress.yaml
cmd="${1:?usage: progress.sh read|init|set|mark ...}"
shift
case "$cmd" in
  read)
    # Also re-mirror: a retry starts with an empty $FULLSEND_OUTPUT_DIR.
    if [[ -f "$P" ]]; then
      cat "$P"
      cp "$P" "${FULLSEND_OUTPUT_DIR:?FULLSEND_OUTPUT_DIR must be set}/strat-progress.yaml"
    else
      echo "no progress recorded"
    fi
    exit 0 ;;
  init)
    if [[ -f "$P" ]]; then
      echo "ERROR: $P exists; this is a resume, use set/mark" >&2
      exit 1
    fi
    python3 scripts/state.py init "$P" "$@" ;;
  set)
    python3 scripts/state.py set "$P" "$@" ;;
  mark)
    stamp="$(python3 scripts/state.py timestamp)"
    for key in "$@"; do
      python3 scripts/state.py set "$P" "$key=$stamp"
    done ;;
  *)
    echo "unknown command: $cmd" >&2
    exit 2 ;;
esac
cp "$P" "${FULLSEND_OUTPUT_DIR:?FULLSEND_OUTPUT_DIR must be set}/strat-progress.yaml"
