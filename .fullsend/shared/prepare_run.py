#!/usr/bin/env python3
"""Choose and lock this run's RFEs on the host, before any sandbox exists.

Run by pre-strategy.sh in the trusted checkout. Candidates come from RFE_KEY
(strat-single), the repository's default JQL discovery (strat-batch), or
STRAT_RESUME_KEY (strat-resume: a RHAISTRAT key, resolved to its RFE through
the Cloners link). Each candidate gets a resume point from durable state
(resume_point below); a STRAT at a human gate is skipped, not locked. The rest
are locked once with scripts/lock_issues.py --locked-keys-file, so the lock
record is on the host before the agent starts and the CI cleanup
(release-locks.sh) can release it whatever happens next.

Resume points, per RFE (first match wins):
  no open STRAT                                  -> create
  STRAT has rubric-pass, needs-attention or
    human-sign-off                               -> skipped (done, or waiting for a human)
  STRAT lacks strat-creator-auto-refined         -> refine
  previous run pushed it but recorded no review  -> review  (STRAT_PREVIOUS_PROGRESS)
  otherwise (a human cleared needs-attention)    -> refine
create always runs in the sandbox, because it is also how an existing STRAT is
imported; review skips refine and push.

expected_skips in run.json lists the acquired RFEs strategy-create will skip,
from the same gates and pipeline-settings.yaml values the skill uses; the
validator accepts an agent-claimed skip only when it is listed there.

Writes:
  $STRAT_STATE_DIR/owned-rfe-ids.txt   keys this run locked (lock_issues.py)
  $STRAT_STATE_DIR/run.json            the trusted run record
  $TARGET_REPO_DIR/tmp/strat-input.json   the agent's copy of the run record

With STRAT_DRY_RUN=1 nothing is written to Jira: candidates are checked for
the blocking labels instead of locked, and the skills run with --dry-run.

Exit codes: 0 work to do, 78 nothing to do (fullsend skips the sandbox), 1 error.
"""

import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

from find_strat_for_rfe import find_strat_clones  # noqa: E402
from jira_utils import get_issue, require_env  # noqa: E402
from lock_issues import BLOCKING_LABELS  # noqa: E402

EXIT_SKIP = 78
RFE_RE = re.compile(r"^RHAIRFE-\d+$")
STRAT_RE = re.compile(r"^RHAISTRAT-\d+$")
STEPS = ("create", "refine", "push", "review")
# strategy-create Path A leaves STRATs in these states alone.
ACTIVE_STATUSES = {"Closed", "Resolved", "In Progress", "Review", "Release Pending"}
GATE_LABELS = {
    "strat-creator-human-sign-off": "signed off by a human",
    "strat-creator-needs-attention": "waiting for a human (needs-attention)",
    "strat-creator-rubric-pass": "already approved (rubric-pass)",
}
REFINED_LABEL = "strat-creator-auto-refined"
SETTINGS = ROOT / "config" / "pipeline-settings.yaml"
TARGET_VERSION_FIELD = "customfield_10855"
RUN_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,100}$")


def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n")


def discover(env):
    args = [sys.executable, str(ROOT / "scripts" / "list-rfe-ids.py"), "--jql-default"]
    for name, flag in (("BATCH_SIZE", "--batch-size"), ("BATCH_OFFSET", "--batch-offset")):
        value = env.get(name, "")
        if value:
            if not value.isdigit():
                raise ValueError(f"{name} must be a non-negative integer, got {value!r}")
            args += [flag, value]
    proc = subprocess.run(args, capture_output=True, text=True, cwd=ROOT, env=env)
    sys.stderr.write(proc.stderr)
    if proc.returncode != 0:
        raise ValueError(f"list-rfe-ids.py exited {proc.returncode}")
    keys = proc.stdout.split()
    bad = [k for k in keys if not RFE_RE.match(k)]
    if bad:
        raise ValueError(f"discovery printed keys that are not RHAIRFE keys: {bad[:3]}")
    return keys


def labels_of(keys):
    server, user, token = require_env()
    return {k: set(get_issue(server, user, token, k, fields=["labels"])
                   .get("fields", {}).get("labels", [])) for k in keys}


def resolve_strat(strat):
    """RHAISTRAT key -> the RHAIRFE it was cloned from (Cloners link)."""
    server, user, token = require_env()
    links = get_issue(server, user, token, strat, fields=["issuelinks"]).get("fields", {}).get("issuelinks", [])
    rfes = sorted({link.get(side, {}).get("key", "") for link in links
                   if link.get("type", {}).get("name") == "Cloners"
                   for side in ("outwardIssue", "inwardIssue")
                   if RFE_RE.match(link.get(side, {}).get("key", ""))})
    if len(rfes) != 1:
        raise ValueError(f"{strat} is cloned from {rfes or 'no RHAIRFE'}; expected exactly one")
    return rfes[0]


def load_previous(path):
    if not path:
        return {}
    try:
        previous = json.loads(Path(path).read_text())
    except (OSError, ValueError) as exc:
        print(f"prepare-run: ignoring previous progress {path}: {exc}", file=sys.stderr)
        return {}
    return previous if isinstance(previous, dict) else {}


def resume_point(rfe, previous):
    """(step, None) or (None, skip reason) for one RFE; see the module docstring."""
    server, user, token = require_env()
    clones = [c for c in find_strat_clones(server, user, token, rfe) if c["status"] not in ACTIVE_STATUSES]
    if len(clones) != 1:
        return "create", None  # none: Path B clone; several: strategy-create skips and records why
    strat, labels = clones[0]["key"], set(clones[0]["labels"])
    for label, reason in GATE_LABELS.items():
        if label in labels:
            return None, f"{strat} {reason}"
    if REFINED_LABEL not in labels:
        return "refine", None
    strategies = {p["rfe"]: p["strat"] for p in (previous.get("create") or {}).get("strategies", [])}
    push = previous.get("push") or {}
    # A dry run's push is recorded as skipped: nothing reached Jira, so refine again.
    if strategies.get(rfe) == strat and strat in push.get("strategies", []) and not push.get("skipped") \
            and strat not in previous.get("review", {}):
        return "review", None
    return "refine", None


def expected_create_skip(rfe, settings):
    """The reason strategy-create will skip this RFE, or None.

    Reproduces the skill's gates from Jira fields (its Step 2a status, release
    scope and quality gates, and the Path A STRAT-state gates), with the same
    pipeline-settings.yaml values the skill reads. The validator accepts a
    create skip only when it is listed here. strategy-create's reconstruction
    failure cannot be predicted from fields, so it is never expected.
    """
    server, user, token = require_env()
    fields = get_issue(server, user, token, rfe,
                       fields=["status", "labels", TARGET_VERSION_FIELD]).get("fields", {})
    jql = settings.get("jql", {})
    status = (fields.get("status") or {}).get("name", "")
    if status in set(jql.get("excluded_statuses", [])):
        return f"RFE status: {status}"
    labels = set(fields.get("labels") or [])
    versions = {v.get("name", "") for v in fields.get(TARGET_VERSION_FIELD) or [] if isinstance(v, dict)}
    if not (labels & set(jql.get("required_labels", [])) or versions & set(jql.get("target_versions", []))):
        return "release scope: no required label or target version"
    quality = set(jql.get("quality_labels", []))
    if quality and not labels & quality:
        return "quality: no quality label"
    clones = find_strat_clones(server, user, token, rfe)
    open_clones = [c for c in clones if c["status"] not in ACTIVE_STATUSES]
    if clones and not open_clones:
        return "existing STRAT(s) in active/completed state"
    if len(open_clones) > 1:
        return "multiple open STRATs"
    for clone in open_clones:  # Path A pipeline label gate
        for label in ("strat-creator-rubric-pass", "strat-creator-needs-attention"):
            if label in clone["labels"]:
                return f"{clone['key']} already processed (label: {label})"
    return None


def lock(keys, record, env):
    proc = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "lock_issues.py"), "lock",
         "--locked-keys-file", str(record), *keys],
        capture_output=True, text=True, cwd=ROOT, env=env)
    sys.stderr.write(proc.stderr)
    recorded = record.read_text().split() if record.exists() else []
    if proc.returncode != 0:
        raise RuntimeError(f"lock_issues.py exited {proc.returncode}; it recorded {recorded}")
    if proc.stdout.split() != recorded:
        raise RuntimeError(f"lock_issues.py printed {proc.stdout.split()} but recorded {recorded}")
    return recorded


def skip_reason(labels):
    blocking = sorted(labels & BLOCKING_LABELS)
    return f"blocked by label(s): {', '.join(blocking)}" if blocking else "lock not acquired"


def request_skip(env, summary):
    print(f"prepare-run: nothing to do: {summary}")
    output = env.get("FULLSEND_PRESCRIPT_OUTPUT")
    if output:
        with open(output, "a") as stream:
            stream.write(f"skipped=true\nreason={summary[:200]}\n")
    return EXIT_SKIP


def main(argv, env):
    if len(argv) != 2 or argv[1] not in ("single", "batch", "resume"):
        print("usage: prepare_run.py single|batch|resume", file=sys.stderr)
        return 1
    mode = argv[1]
    run_id, state = env.get("STRAT_RUN_ID", ""), env.get("STRAT_STATE_DIR", "")
    if not RUN_ID_RE.match(run_id):
        print("prepare-run: STRAT_RUN_ID must be a simple identifier", file=sys.stderr)
        return 1
    if not state:
        print("prepare-run: STRAT_STATE_DIR must name a host-only directory", file=sys.stderr)
        return 1
    state_dir = Path(state)
    state_dir.mkdir(parents=True, exist_ok=True)
    record = state_dir / "owned-rfe-ids.txt"
    if record.exists() and record.read_text().strip():
        print(f"prepare-run: {record} still records locks; run release-locks.sh first",
              file=sys.stderr)
        return 1
    target = Path(env.get("TARGET_REPO_DIR") or ROOT)
    version = (target / ".context/architecture-context/LATEST_VERSION").read_text().strip()
    dry_run = env.get("STRAT_DRY_RUN", "") in ("1", "true")

    if mode == "single":
        key = env.get("RFE_KEY", "")
        if not RFE_RE.match(key):
            print(f"prepare-run: RFE_KEY must look like RHAIRFE-NNNN, got {key!r}", file=sys.stderr)
            return 1
        candidates = [key]
    elif mode == "resume":
        key = env.get("STRAT_RESUME_KEY", "").strip()
        try:
            if STRAT_RE.match(key):
                key = resolve_strat(key)
            elif not RFE_RE.match(key):
                raise ValueError(f"STRAT_RESUME_KEY must be a RHAISTRAT or RHAIRFE key, got {key!r}")
        except ValueError as exc:
            print(f"prepare-run: {exc}", file=sys.stderr)
            return 1
        candidates = [key]
    else:
        try:
            candidates = discover(env)
        except ValueError as exc:
            print(f"prepare-run: discovery failed: {exc}", file=sys.stderr)
            return 1
        print(f"prepare-run: discovered {' '.join(candidates) or '(none)'}")
        expected = env.get("BATCH_EXPECTED_KEYS", "").split()
        if expected and candidates != expected:
            print(f"prepare-run: discovered {candidates}, expected {expected}", file=sys.stderr)
            return 1

    run = {"run_id": run_id, "mode": mode, "dry_run": dry_run, "input_keys": candidates,
           "acquired_keys": [], "skipped": [], "resume_points": {}, "resume_from": None,
           "expected_skips": None,
           "architecture_context": "available", "architecture_version": version,
           "prepared_at": now()}
    if not candidates:
        write_json(state_dir / "run.json", run)
        return request_skip(env, "discovery returned no eligible RFEs")

    previous = load_previous(env.get("STRAT_PREVIOUS_PROGRESS", ""))
    gated, points = {}, {}
    try:
        for key in candidates:
            step, reason = resume_point(key, previous)
            if step:
                points[key] = step
            else:
                gated[key] = reason
    except Exception as exc:  # Jira unreachable or the key unknown: nothing is locked yet
        print(f"prepare-run: reading Jira state failed, no RFE was locked: {exc}", file=sys.stderr)
        return 1
    lockable = [k for k in candidates if k in points]
    # strategy-create's gate outcome for each lockable RFE, before any lock: if
    # it cannot be computed, nothing is locked and the run fails.
    try:
        settings = yaml.safe_load(SETTINGS.read_text()) or {}
        gate_skips = {k: r for k in lockable if (r := expected_create_skip(k, settings))}
    except Exception as exc:
        print(f"prepare-run: could not compute expected create skips, no RFE was locked: {exc}",
              file=sys.stderr)
        return 1
    try:
        if not lockable:
            acquired, labels = [], {}
        elif dry_run:
            labels = labels_of(lockable)
            acquired = [k for k in lockable if not labels[k] & BLOCKING_LABELS]
        else:
            acquired = lock(lockable, record, env)
            run["locked_at"] = now()
            labels = labels_of([k for k in lockable if k not in acquired])
    except Exception as exc:  # the lock record, if any, is already on disk for release-locks.sh
        print(f"prepare-run: {exc}", file=sys.stderr)
        return 1
    run["acquired_keys"] = acquired
    run["resume_points"] = {k: points[k] for k in acquired}
    run["expected_skips"] = [{"key": k, "reason": gate_skips[k]} for k in acquired if k in gate_skips]
    run["resume_from"] = min(run["resume_points"].values(), key=STEPS.index, default=None)
    run["skipped"] = [{"key": k, "reason": gated[k] if k in gated else skip_reason(labels[k])}
                      for k in candidates if k not in acquired]
    write_json(state_dir / "run.json", run)
    if not acquired:
        return request_skip(env, "every candidate is locked, done, or waiting for a human")
    write_json(target / "tmp" / "strat-input.json", run)
    points_text = " ".join(f"{k}:{v}" for k, v in run["resume_points"].items())
    print(f"prepare-run: {'selected (dry run)' if dry_run else 'locked'} {points_text}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv, dict(os.environ)))
