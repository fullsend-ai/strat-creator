#!/usr/bin/env python3
"""Issue-level locking via Jira labels for concurrent pipeline jobs.

Applies/removes `strat-creator-processing` to prevent two pipeline jobs
from processing the same RFE simultaneously.

Commands:
    lock RHAIRFE-1234 [...]
        Fetch labels, validate guards, apply strat-creator-processing.
        Skips blocked keys, prints locked subset to stdout.

    unlock RHAIRFE-1234 [...]
        Remove strat-creator-processing.

    lock-strat RHAISTRAT-1500
        Resolve STRAT→RFE via Cloners link, validate STRAT guards
        (must have strat-creator-auto-created, must NOT have
        strat-creator-needs-attention or strat-creator-human-sign-off),
        then lock the RFE. Fails if no Cloners link to RHAIRFE exists.

    unlock-strat RHAISTRAT-1500
        Resolve STRAT→RFE via Cloners link, remove lock from RFE.

Exit codes:
    0 — success (including when all keys are blocked)
    1 — blocked (lock-strat only: STRAT-level or linked-RFE contention)
    2 — error (missing env vars, API failure, missing
        strat-creator-auto-created label, or no linked RFE)
"""

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))

from jira_utils import (
    add_labels,
    get_issue,
    remove_labels,
    require_env,
)
from strat_side_effects import OWNER_PREFIX, atomic, locked, owner_label

PROCESSING_LABEL = "strat-creator-processing"

BLOCKING_LABELS = frozenset({
    "strat-creator-processing",
    "strat-creator-needs-attention",
    "strat-creator-human-sign-off",
})

STRAT_REQUIRED_LABEL = "strat-creator-auto-created"

STRAT_BLOCKING_LABELS = frozenset({
    "strat-creator-needs-attention",
    "strat-creator-human-sign-off",
})


def _init_locked_keys_file(path):
    """Create parent directories and truncate the locked-keys file."""
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, 'w'):
        pass


def _get_labels(server, user, token, key):
    """Fetch the labels for a Jira issue."""
    data = get_issue(server, user, token, key, fields=["labels"])
    return set(data.get("fields", {}).get("labels", []))


def _resolve_strat_to_rfe(server, user, token, strat_key):
    """Resolve a RHAISTRAT key to its linked RHAIRFE via Cloners links.

    Returns the first RHAIRFE key found, or None.
    """
    data = get_issue(server, user, token, strat_key,
                     fields=["issuelinks"])
    links = data.get("fields", {}).get("issuelinks", [])
    for link in links:
        if link.get("type", {}).get("name") != "Cloners":
            continue
        for direction in ("outwardIssue", "inwardIssue"):
            issue = link.get(direction, {})
            key = issue.get("key", "")
            if key.startswith("RHAIRFE-"):
                return key
    return None


def lock(server, user, token, keys, locked_keys_file=None):
    """Lock RFE(s) by applying strat-creator-processing label.

    Skips blocked keys, prints locked subset to stdout.
    Returns (exit_code, locked_keys).
    """
    locked = []
    exit_code = 0

    for key in keys:
        labels = _get_labels(server, user, token, key)
        blocked_by = (labels & BLOCKING_LABELS) | {x for x in labels if x.startswith(OWNER_PREFIX)}
        if blocked_by:
            print(f"BLOCKED {key} — has label(s): "
                  f"{', '.join(sorted(blocked_by))}", file=sys.stderr)
            continue

        add_labels(server, user, token, key, [PROCESSING_LABEL])

        if locked_keys_file is not None:
            try:
                with open(locked_keys_file, 'a') as f:
                    f.write(key + '\n')
                    f.flush()
            except OSError as exc:
                print(f"ERROR recording lock for {key}: {exc}",
                      file=sys.stderr)
                try:
                    remove_labels(server, user, token, key,
                                  [PROCESSING_LABEL])
                    print(f"ROLLED BACK {key}", file=sys.stderr)
                except Exception:
                    print(f"WARNING: rollback failed for {key}",
                          file=sys.stderr)
                exit_code = 2
                break

        locked.append(key)
        print(f"LOCKED {key}", file=sys.stderr)

    # Print locked keys to stdout for capture by CI script
    print(" ".join(locked))

    if not locked:
        print("No keys locked", file=sys.stderr)
        return exit_code, []

    return exit_code, locked


def unlock(server, user, token, keys):
    """Unlock RFE(s) by removing strat-creator-processing label."""
    for key in keys:
        if any(x.startswith(OWNER_PREFIX) for x in _get_labels(server, user, token, key)):
            raise RuntimeError("owned lock requires its matching --owner; refusing unowned cleanup")
        remove_labels(server, user, token, key, [PROCESSING_LABEL])
        print(f"UNLOCKED {key}", file=sys.stderr)
    return 0


def lock_owned(server, user, token, keys, record, owner):
    """Persist candidate intent first; reconcile this owner's existing claim.

    Label claims are not an atomic Jira lease. Detect foreign/multiple owners
    and stop; no automatic stealing or clearing of an orphaned lock.
    """
    record = Path(record)
    label = owner_label(owner)
    with locked(Path(str(record) + ".lock")):
        owner_path = Path(str(record) + ".owner.json")
        if owner_path.exists() and json.loads(owner_path.read_text()) != {"owner": owner}:
            raise RuntimeError("lock record belongs to another run")
        atomic(owner_path, {"owner": owner})
        recorded = record.read_text().split() if record.exists() else []
        acquired = []
        for key in keys:
            labels = _get_labels(server, user, token, key)
            owners = {x for x in labels if x.startswith(OWNER_PREFIX)}
            if owners == {label} and PROCESSING_LABEL in labels:
                if labels & (BLOCKING_LABELS - {PROCESSING_LABEL}):
                    raise RuntimeError("owned RFE moved to a human gate")
            elif owners or labels & BLOCKING_LABELS:
                print(f"BLOCKED {key} — existing lock or human gate", file=sys.stderr)
                continue
            else:
                # The record is intent, not proof of acquisition. Cleanup reads
                # the matching remote owner, including death before response.
                if key not in recorded:
                    with record.open("a") as stream:
                        stream.write(key + "\n")
                        stream.flush()
                        os.fsync(stream.fileno())
                    recorded.append(key)
                add_labels(server, user, token, key, [PROCESSING_LABEL, label])
            current = _get_labels(server, user, token, key)
            if {x for x in current if x.startswith(OWNER_PREFIX)} != {label} or PROCESSING_LABEL not in current:
                raise RuntimeError("conflicting ownership after lock request; no work is authorized")
            if key not in recorded:
                with record.open("a") as stream:
                    stream.write(key + "\n")
                    stream.flush()
                    os.fsync(stream.fileno())
                recorded.append(key)
            acquired.append(key)
            print(f"LOCKED {key} owner={owner}", file=sys.stderr)
        print(" ".join(acquired))
        return 0, acquired


def unlock_owned(server, user, token, keys, owner):
    label = owner_label(owner)
    for key in keys:
        labels = _get_labels(server, user, token, key)
        owners = {x for x in labels if x.startswith(OWNER_PREFIX)}
        if owners and owners != {label}:
            raise RuntimeError("foreign or conflicting lock owner; cleanup refused")
        if not owners:
            if PROCESSING_LABEL in labels:
                raise RuntimeError("unowned processing label; cleanup refused")
            continue  # Already released (or died before acquiring).
        remove_labels(server, user, token, key, [PROCESSING_LABEL, label])
        print(f"UNLOCKED {key} owner={owner}", file=sys.stderr)
    return 0


def lock_strat(server, user, token, strat_key, locked_keys_file=None):
    """Lock via STRAT key: resolve to RFE, validate STRAT guards, lock RFE."""
    # Validate STRAT labels
    strat_labels = _get_labels(server, user, token, strat_key)
    if STRAT_REQUIRED_LABEL not in strat_labels:
        print(f"ERROR {strat_key} — missing required label: "
              f"{STRAT_REQUIRED_LABEL}. Not created by our pipeline.",
              file=sys.stderr)
        return 2

    strat_blocked = strat_labels & STRAT_BLOCKING_LABELS
    if strat_blocked:
        print(f"BLOCKED {strat_key} — has label(s): "
              f"{', '.join(sorted(strat_blocked))}",
              file=sys.stderr)
        return 1

    # Resolve STRAT → RFE
    rfe_key = _resolve_strat_to_rfe(server, user, token, strat_key)
    if not rfe_key:
        print(f"ERROR {strat_key} — no Cloners link to RHAIRFE found. "
              f"Not created by our pipeline.", file=sys.stderr)
        return 2

    # Lock the RFE. A blocked linked RFE is contention for this specific
    # STRAT request, so lock-strat must still fail.
    exit_code, locked_keys = lock(server, user, token, [rfe_key],
                                  locked_keys_file=locked_keys_file)
    if exit_code != 0:
        return exit_code
    return 0 if locked_keys else 1


def unlock_strat(server, user, token, strat_key):
    """Unlock via STRAT key: resolve to RFE, remove lock from RFE."""
    rfe_key = _resolve_strat_to_rfe(server, user, token, strat_key)
    if not rfe_key:
        print(f"ERROR {strat_key} — no Cloners link to RHAIRFE found.",
              file=sys.stderr)
        return 2
    return unlock(server, user, token, [rfe_key])


def main():
    if len(sys.argv) < 3:
        print("Usage: lock_issues.py <command> <key> [key ...]",
              file=sys.stderr)
        print("Commands: lock, unlock, lock-strat, unlock-strat",
              file=sys.stderr)
        return 2

    command = sys.argv[1]
    args = sys.argv[2:]

    owner = None
    if "--owner" in args:
        idx = args.index("--owner")
        if idx + 1 >= len(args):
            return 2
        owner = args[idx + 1]
        args = args[:idx] + args[idx + 2:]

    locked_keys_file = None
    if command in ("lock", "lock-strat") and "--locked-keys-file" in args:
        idx = args.index("--locked-keys-file")
        if idx + 1 >= len(args):
            print("--locked-keys-file requires a path argument",
                  file=sys.stderr)
            return 2
        locked_keys_file = args[idx + 1]
        args = args[:idx] + args[idx + 2:]

    keys = args

    if locked_keys_file is not None and not owner:
        try:
            _init_locked_keys_file(locked_keys_file)
        except OSError as exc:
            print(f"ERROR initializing {locked_keys_file}: {exc}",
                  file=sys.stderr)
            return 2

    server, user, token = require_env()
    if not all([server, user, token]):
        print("Error: JIRA_SERVER, JIRA_USER, JIRA_TOKEN required.",
              file=sys.stderr)
        return 2

    if command == "lock":
        if owner:
            if not locked_keys_file:
                raise ValueError("owned locking requires --locked-keys-file")
            code, _ = lock_owned(server, user, token, keys, locked_keys_file, owner)
            return code
        exit_code, _ = lock(server, user, token, keys,
                            locked_keys_file=locked_keys_file)
        return exit_code
    elif command == "unlock":
        if owner:
            return unlock_owned(server, user, token, keys, owner)
        return unlock(server, user, token, keys)
    elif command == "lock-strat":
        if len(keys) != 1:
            print("lock-strat takes exactly one STRAT key", file=sys.stderr)
            return 2
        return lock_strat(server, user, token, keys[0],
                          locked_keys_file=locked_keys_file)
    elif command == "unlock-strat":
        if len(keys) != 1:
            print("unlock-strat takes exactly one STRAT key", file=sys.stderr)
            return 2
        return unlock_strat(server, user, token, keys[0])
    else:
        print(f"Unknown command: {command}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
