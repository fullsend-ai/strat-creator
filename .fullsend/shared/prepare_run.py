#!/usr/bin/env python3
"""Choose and lock this run's RFEs on the host, before any sandbox exists.

Called by pre-strategy.sh in the CI job container. Candidates come from
RFE_KEY (single) or the existing native JQL discovery (batch). The script
locks them once with lock_issues.py, recording owned keys in
$STRAT_STATE_DIR/owned-rfe-ids.txt, and writes:

  $STRAT_STATE_DIR/run.json        trusted run record (validator, cleanup)
  $TARGET_REPO_DIR/tmp/strat-input.json   the agent's input (same content)

The agent never locks, unlocks or receives JQL (ADR-0012 amendment). When
nothing can be locked it writes $STRAT_STATE_DIR/preflight-result.json, a
skipped agent result, and exits 78 so Fullsend skips the sandbox and model.

Usage: prepare_run.py single|batch
Environment: TARGET_REPO_DIR, STRAT_STATE_DIR, STRAT_RUN_ID, JIRA_*;
RFE_KEY (single); BATCH_SIZE, BATCH_OFFSET, BATCH_EXPECTED_KEYS (batch).
Exit codes: 0 work locked, 78 nothing to do, 1 error.
"""

import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(os.environ.get("TARGET_REPO_DIR") or Path.cwd())
sys.path.insert(0, str(ROOT / "scripts"))

from jira_utils import get_issue, require_env  # noqa: E402
from lock_issues import BLOCKING_LABELS  # noqa: E402

EXIT_SKIP = 78
RFE_RE = re.compile(r"^RHAIRFE-\d+$")
OWNED_FILE = "owned-rfe-ids.txt"
RUN_FILE = "run.json"
INPUT_FILE = Path("tmp") / "strat-input.json"


def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def fail(message):
    print(f"prepare-run: ERROR: {message}", file=sys.stderr)
    return 1


def discover():
    """Run the existing native JQL discovery with the configured bounds."""
    args = [sys.executable, str(ROOT / "scripts" / "list-rfe-ids.py"),
            "--jql-default"]
    for env, flag in (("BATCH_SIZE", "--batch-size"),
                      ("BATCH_OFFSET", "--batch-offset")):
        value = os.environ.get(env, "")
        if value:
            if not value.isdigit():
                raise ValueError(f"{env} must be a non-negative integer")
            args += [flag, value]
    out = subprocess.run(args, check=True, capture_output=True, text=True,
                         cwd=ROOT)
    sys.stderr.write(out.stderr)
    keys = out.stdout.split()
    bad = [k for k in keys if not RFE_RE.match(k)]
    if bad:
        raise ValueError(f"discovery returned invalid keys: {bad[:3]}")
    return keys


def lock(keys, owned_path):
    """Lock once; return the acquired keys as recorded by the helper."""
    proc = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "lock_issues.py"), "lock",
         "--locked-keys-file", str(owned_path), *keys],
        capture_output=True, text=True, cwd=ROOT)
    sys.stderr.write(proc.stderr)
    recorded = [k for k in owned_path.read_text().split()] \
        if owned_path.exists() else []
    if proc.returncode != 0:
        raise RuntimeError(f"lock_issues.py exited {proc.returncode}; "
                           f"recorded {recorded}")
    if proc.stdout.split() != recorded:
        raise RuntimeError(f"lock output {proc.stdout.split()} differs from "
                           f"record {recorded}")
    return recorded


def blocked_reasons(keys):
    server, user, token = require_env()
    reasons = {}
    for key in keys:
        data = get_issue(server, user, token, key, fields=["labels"])
        labels = set(data.get("fields", {}).get("labels", []))
        blocking = sorted(labels & BLOCKING_LABELS)
        reasons[key] = (f"blocked by label(s): {', '.join(blocking)}"
                        if blocking else "lock not acquired")
    return reasons


def skipped_result(record, summary):
    return {
        "action": "skipped",
        "mode": record["mode"],
        "run_id": record["run_id"],
        "summary": summary,
        "input_keys": record["input_keys"],
        "acquired_keys": [],
        "skipped": record["skipped"],
        "strategies": [],
        "completed_phases": [],
        "artifacts": [],
        "architecture_context": "missing",
        "publication_ready": False,
        "errors": [],
    }


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n")


def request_skip(state_dir, record, summary):
    print(f"prepare-run: no work: {summary}")
    write_json(state_dir / "preflight-result.json",
               skipped_result(record, summary))
    output = os.environ.get("FULLSEND_PRESCRIPT_OUTPUT")
    if output:
        with open(output, "a") as stream:
            stream.write(f"skipped=true\nreason={summary[:200]}\n")
    return EXIT_SKIP


def main(argv):
    if len(argv) != 2 or argv[1] not in ("single", "batch"):
        return fail("usage: prepare_run.py single|batch")
    mode = argv[1]
    run_id = os.environ.get("STRAT_RUN_ID", "")
    if not re.match(r"^[A-Za-z0-9._-]+$", run_id):
        return fail("STRAT_RUN_ID must be set to a simple identifier")
    state = os.environ.get("STRAT_STATE_DIR", "")
    if not state:
        return fail("STRAT_STATE_DIR must name the host run-state directory")
    state_dir = Path(state)
    state_dir.mkdir(parents=True, exist_ok=True)
    owned_path = state_dir / OWNED_FILE
    if owned_path.exists() and owned_path.read_text().strip():
        return fail(f"{owned_path} already records locks; release them "
                    "(release-locks.sh) before reusing this state directory")

    record = {"run_id": run_id, "mode": mode, "input_keys": [],
              "acquired_keys": [], "skipped": [], "prepared_at": now()}

    if mode == "single":
        key = os.environ.get("RFE_KEY", "")
        if not RFE_RE.match(key):
            return fail(f"RFE_KEY must look like RHAIRFE-NNNN, got {key!r}")
        candidates = [key]
    else:
        try:
            candidates = discover()
        except (ValueError, subprocess.CalledProcessError) as exc:
            return fail(f"discovery failed: {exc}")
        print(f"prepare-run: discovered {' '.join(candidates) or '(none)'}")
        expected = os.environ.get("BATCH_EXPECTED_KEYS", "").split()
        if expected and candidates != expected:
            return fail(f"discovered {candidates} differs from the expected "
                        f"bound {expected}")
    record["input_keys"] = candidates
    if not candidates:
        write_json(state_dir / RUN_FILE, record)
        return request_skip(state_dir, record,
                            "Discovery returned no eligible RFEs.")

    try:
        acquired = lock(candidates, owned_path)
    except RuntimeError as exc:
        return fail(str(exc))
    record["acquired_keys"] = acquired
    record["locked_at"] = now()
    unlocked = [k for k in candidates if k not in acquired]
    if unlocked:
        reasons = blocked_reasons(unlocked)
        record["skipped"] = [{"key": k, "reason": reasons[k]}
                             for k in unlocked]
    write_json(state_dir / RUN_FILE, record)
    if not acquired:
        return request_skip(state_dir, record,
                            "Every candidate is locked or held for human "
                            "attention.")

    write_json(ROOT / INPUT_FILE, record)
    print(f"prepare-run: locked {' '.join(acquired)}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
