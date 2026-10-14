#!/usr/bin/env python3
"""Check a strat-single/strat-batch agent result against what actually exists.

Fullsend runs this on the host after the agent exits (through
validate-output.sh). Valid JSON is not enough: every claim in
agent-result.json is compared with the host run record and lock record
written by prepare_run.py, the agent's progress state, the downloaded
repository's artifacts and, when credentials are present, Jira.

Trust boundary: this file and the helpers it imports come from the trusted
checkout that holds .fullsend/, and STRAT_STATE_DIR is written only on the
host. The downloaded repository (--repo) and the iteration output
(--output-dir) are agent-writable, so they are only read as data. Nothing in
them is executed, imported or sourced.

Usage:
    validate_result.py --result output/agent-result.json --output-dir output \
        --repo "$TARGET_REPO_DIR" --state-dir "$STRAT_STATE_DIR" \
        [--schema result.schema.json] [--no-jira]

Environment:
    STRAT_MODE                           expected mode (checked when set)
    JIRA_SERVER, JIRA_USER, JIRA_TOKEN   enable Jira readback
    STRAT_VALIDATE_JIRA=0                disable Jira readback
    STRAT_TRUSTED_ROOT                   trusted checkout (default: ../..)

Exit codes: 0 valid, 1 invalid (reasons on stdout), 2 usage/setup error.
"""

import argparse
import json
import os
import re
import sys
from pathlib import Path

TRUSTED_ROOT = Path(os.environ.get("STRAT_TRUSTED_ROOT")
                    or Path(__file__).resolve().parents[2])
sys.path.insert(0, str(TRUSTED_ROOT / "scripts"))

from artifact_utils import read_frontmatter  # noqa: E402


def _load_compute_verdict():
    """The skill's deterministic verdict rule (scripts/assess-strat)."""
    import importlib.util
    path = TRUSTED_ROOT / "scripts" / "assess-strat" / "parse_results.py"
    spec = importlib.util.spec_from_file_location("strat_parse_results", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.compute_verdict, module.CRITERIA


compute_verdict, CRITERIA = _load_compute_verdict()

PHASE_ORDER = ["create", "refine", "push", "review"]
STRATEGY_PHASES = PHASE_ORDER
RFE_RE = re.compile(r"^RHAIRFE-\d+$")
STRAT_RE = re.compile(r"^RHAISTRAT-\d+$")

OWNED_FILE = "owned-rfe-ids.txt"
RUN_FILE = "run.json"
PROGRESS_FILE = "strat-progress.yaml"

PROCESSING_LABEL = "strat-creator-processing"
CREATED_LABEL = "strat-creator-auto-created"
REFINED_LABEL = "strat-creator-auto-refined"
VERDICT_LABELS = {
    "approve": "strat-creator-rubric-pass",
    "revise": "strat-creator-needs-attention",
    "reject": "strat-creator-needs-attention",
}
REVIEWERS = ("feasibility", "testability", "scope", "architecture")


def read_keys(path):
    """Read an RFE key list written by lock_issues.py; reject anything else."""
    if not path.is_file():
        return None
    keys = [line.strip() for line in path.read_text().splitlines()
            if line.strip()]
    bad = [k for k in keys if not RFE_RE.match(k)]
    if bad:
        raise ValueError(f"{path.name} has invalid entries: {bad[:3]}")
    return keys


def read_progress(path):
    """Parse a state.py file (`key: value` lines) without executing it."""
    if not path.is_file():
        return None
    data = {}
    for line in path.read_text().splitlines():
        if ":" in line:
            key, value = line.split(":", 1)
            data[key.strip()] = value.strip()
    return data


def check_schema(result, schema_path):
    if not schema_path:
        return []
    import jsonschema
    schema = json.loads(Path(schema_path).read_text())
    validator = jsonschema.Draft202012Validator(schema)
    return [f"schema: {'.'.join(map(str, e.path)) or '(root)'}: {e.message}"
            for e in sorted(validator.iter_errors(result), key=str)]


def context_status(repo):
    """Recompute whether the reviews had architecture context to read."""
    base = repo / ".context" / "architecture-context"
    latest = base / "LATEST_VERSION"
    if not latest.is_file():
        return "missing"
    version = latest.read_text().strip()
    if not re.match(r"^[A-Za-z0-9._-]+$", version):
        return "missing"
    platform = base / "architecture" / version / "PLATFORM.md"
    return "available" if platform.is_file() else "missing"


def safe_artifact(repo, rel):
    """Resolve an artifacts/ path inside the downloaded repo, or None."""
    target = (repo / rel).resolve()
    root = (repo / "artifacts").resolve()
    if target != root and root not in target.parents:
        return None
    return target


def check_phases(result, progress, errors):
    phases = result["completed_phases"]
    positions = [PHASE_ORDER.index(p) for p in phases]
    if positions != sorted(positions):
        errors.append(f"completed_phases out of order: {phases}")
    if progress is None:
        if phases:
            errors.append(f"{PROGRESS_FILE} missing but phases are claimed")
        return
    stamps = []
    for phase in phases:
        stamp = progress.get(f"phase_{phase}")
        if not stamp:
            errors.append(f"phase {phase} claimed but not recorded in "
                          f"{PROGRESS_FILE}")
        else:
            stamps.append((phase, stamp))
    # state.py timestamps are ISO 8601 UTC, so string order is time order.
    for (p1, t1), (p2, t2) in zip(stamps, stamps[1:]):
        if t2 < t1:
            errors.append(f"phase {p2} recorded before {p1}")
    for phase in PHASE_ORDER:
        if progress.get(f"phase_{phase}") and phase not in phases:
            errors.append(f"phase {phase} recorded in {PROGRESS_FILE} but "
                          "not reported in completed_phases")
    # Per-strategy markers let a validation retry resume a partly finished
    # phase; every claimed strategy must have one for each per-strategy phase.
    strats = {s["strat"] for s in result["strategies"]}
    for phase in ("refine", "review"):
        if phase in phases:
            for strat in sorted(strats):
                if not progress.get(f"{phase}_{strat}"):
                    errors.append(f"{phase} of {strat} not recorded in "
                                  f"{PROGRESS_FILE}")
        for key in progress:
            if key.startswith(f"{phase}_RHAISTRAT-") and \
                    key.split("_", 1)[1] not in strats:
                errors.append(f"{PROGRESS_FILE} records {key} for a STRAT "
                              "not in the result")


def frontmatter(path, errors):
    try:
        data, _ = read_frontmatter(str(path))
        return data
    except Exception as exc:  # malformed agent output is a finding, not a crash
        errors.append(f"{path.name}: unreadable frontmatter ({exc})")
        return {}


def check_strategy_files(repo, strategy, errors):
    rfe, strat = strategy["rfe"], strategy["strat"]
    task = repo / "artifacts" / "strat-tasks" / f"{strat}.md"
    review = repo / "artifacts" / "strat-reviews" / f"{strat}-review.md"
    original = repo / "artifacts" / "strat-originals" / f"{rfe}.md"
    if not task.is_file():
        errors.append(f"{strat}: strategy file missing")
    else:
        fm = frontmatter(task, errors)
        if fm.get("source_rfe") != rfe:
            errors.append(f"{strat}: source_rfe is {fm.get('source_rfe')}, "
                          f"expected {rfe}")
        if fm.get("jira_key") != strat or fm.get("strat_id") != strat:
            errors.append(f"{strat}: strat_id/jira_key do not match")
        if fm.get("status") not in ("Refined", "Reviewed"):
            errors.append(f"{strat}: status is {fm.get('status')}, expected "
                          "Refined or Reviewed")
    if not original.is_file():
        errors.append(f"{strat}: RFE snapshot {rfe}.md missing")
    if not review.is_file():
        errors.append(f"{strat}: review file missing")
        return
    fm = frontmatter(review, errors)
    if fm.get("recommendation") != strategy["recommendation"]:
        errors.append(f"{strat}: result claims {strategy['recommendation']}, "
                      f"review file says {fm.get('recommendation')}")
    if bool(fm.get("needs_attention")) != strategy["needs_attention"]:
        errors.append(f"{strat}: needs_attention does not match review file")
    # Prose reviewers' verdicts are informational (strategy-review SKILL.md,
    # Step 6); they must be present but never decide the recommendation.
    reviewers = fm.get("reviewers") or {}
    missing = [r for r in REVIEWERS if reviewers.get(r) not in
               ("approve", "revise", "reject")]
    if missing:
        errors.append(f"{strat}: review incomplete, no verdict from "
                      f"{', '.join(missing)}")
    # The recommendation comes from the numeric scores only, by the skill's
    # deterministic rule; check the file is consistent with its own scores.
    scores = fm.get("scores") or {}
    try:
        values = {c: int(scores[c.lower()]) for c in CRITERIA}
        total = int(scores["total"])
    except (KeyError, TypeError, ValueError):
        errors.append(f"{strat}: review scores incomplete")
        return
    if total != sum(values.values()):
        errors.append(f"{strat}: score total {total} is not the sum of "
                      f"{values}")
        return
    verdict, attention = compute_verdict({"Total": total, **values})
    if (fm.get("recommendation") != verdict.lower()
            or bool(fm.get("needs_attention")) != attention):
        errors.append(f"{strat}: recommendation {fm.get('recommendation')} "
                      f"does not follow from scores {values} (total "
                      f"{total} -> {verdict.lower()})")


def unclaimed_files(repo, strategies):
    claimed_tasks = {f"{s['strat']}.md" for s in strategies}
    claimed_reviews = {f"{s['strat']}-review.md" for s in strategies}
    extra = []
    for sub, claimed in (("strat-tasks", claimed_tasks),
                         ("strat-reviews", claimed_reviews)):
        directory = repo / "artifacts" / sub
        if directory.is_dir():
            extra += [f"artifacts/{sub}/{p.name}"
                      for p in sorted(directory.glob("*.md"))
                      if p.name not in claimed]
    return extra


def check_jira(result, errors):
    from find_strat_for_rfe import find_strat_clones
    from jira_utils import get_issue

    server = os.environ["JIRA_SERVER"]
    user = os.environ["JIRA_USER"]
    token = os.environ["JIRA_TOKEN"]

    def labels(key):
        data = get_issue(server, user, token, key, fields=["labels"])
        return set(data.get("fields", {}).get("labels", []))

    # Release is the outer CI's job, so every owned lock must still be held.
    for key in result["acquired_keys"]:
        if PROCESSING_LABEL not in labels(key):
            errors.append(f"jira: {key} is not locked although recorded as "
                          "owned")
    for strategy in result["strategies"]:
        rfe, strat = strategy["rfe"], strategy["strat"]
        clones = find_strat_clones(server, user, token, rfe)
        if strat not in {c["key"] for c in clones}:
            errors.append(f"jira: {strat} is not a Cloners clone of {rfe}")
        # A resumed run must import the existing clone, never clone again.
        open_clones = sorted(c["key"] for c in clones
                             if c["status"] not in ("Closed", "Resolved"))
        if len(open_clones) > 1:
            errors.append(f"jira: {rfe} has more than one open STRAT clone "
                          f"({', '.join(open_clones)})")
        have = labels(strat)
        expected = {CREATED_LABEL, REFINED_LABEL,
                    VERDICT_LABELS[strategy["recommendation"]]}
        for label in sorted(expected - have):
            errors.append(f"jira: {strat} lacks label {label}")


def load_run(state_dir):
    """Read the trusted run record written by prepare_run.py on the host."""
    if state_dir is None:
        raise ValueError("STRAT_STATE_DIR is not set; the run record is "
                         "needed to check the result")
    run_path = state_dir / RUN_FILE
    if not run_path.is_file():
        raise ValueError(f"run record {run_path} is missing")
    record = json.loads(run_path.read_text())
    owned = read_keys(state_dir / OWNED_FILE) or []
    if owned != record.get("acquired_keys"):
        raise ValueError(f"host lock record {owned} differs from run record "
                         f"{record.get('acquired_keys')}")
    return record


def validate(result, output_dir, repo, state_dir, schema=None,
             use_jira=False, env=os.environ):
    errors = check_schema(result, schema)
    if errors:
        return errors
    try:
        record = load_run(state_dir)
    except (OSError, ValueError) as exc:
        return [str(exc)]

    mode = result["mode"]
    action = result["action"]
    acquired = result["acquired_keys"]
    for field in ("run_id", "mode", "input_keys", "acquired_keys"):
        if result[field] != record[field]:
            errors.append(f"{field} {result[field]} differs from the host run "
                          f"record {record[field]}")
    if env.get("STRAT_MODE") and env["STRAT_MODE"] != mode:
        errors.append(f"mode is {mode}, harness runs {env['STRAT_MODE']}")
    if mode == "single" and len(record["input_keys"]) != 1:
        errors.append("single mode takes exactly one RFE")

    progress = read_progress(output_dir / PROGRESS_FILE)
    if progress is not None:
        if progress.get("run_id") != result["run_id"]:
            errors.append(f"{PROGRESS_FILE} belongs to run "
                          f"{progress.get('run_id')}")
        for strategy in result["strategies"]:
            if progress.get(f"map_{strategy['rfe']}") != strategy["strat"]:
                errors.append(f"{PROGRESS_FILE} does not map "
                              f"{strategy['rfe']} to {strategy['strat']}")
    check_phases(result, progress, errors)

    for rel in result["artifacts"]:
        target = safe_artifact(repo, rel)
        if target is None or not target.exists():
            errors.append(f"artifact {rel} does not exist")

    # Every input key is accounted for exactly once, and the host's lock
    # skips are carried through with the agent's own create-gate skips.
    counts = {}
    for key in ([s["rfe"] for s in result["strategies"]]
                + [s["key"] for s in result["skipped"]]):
        counts[key] = counts.get(key, 0) + 1
    if action != "failed":
        for key in record["input_keys"]:
            if counts.get(key, 0) != 1:
                errors.append(f"{key} must appear exactly once in strategies "
                              "or skipped")
    for key in counts:
        if key not in record["input_keys"]:
            errors.append(f"{key} was not an input of this run")
    host_skipped = {s["key"] for s in record["skipped"]}
    for s in result["strategies"]:
        if s["rfe"] not in acquired:
            errors.append(f"{s['strat']} comes from {s['rfe']}, which this "
                          "run did not lock")
        if s["rfe"] in host_skipped:
            errors.append(f"{s['rfe']} was not locked but has a strategy")

    if action == "failed":
        detail = "; ".join(result["errors"]) or result["summary"]
        errors.append(f"agent reported failure: {detail[:300]}")
        return errors

    if action == "skipped":
        # Only prepare_run.py skips: once keys are locked the agent runs and
        # must report completed or failed.
        if acquired or result["strategies"]:
            errors.append("skipped result must not hold locks or claim "
                          "strategies")
        if result["publication_ready"]:
            errors.append("skipped result cannot be publication_ready")
        extra = unclaimed_files(repo, [])
        if extra:
            errors.append(f"skipped result left strategy files: {extra[:5]}")
        return errors

    # completed
    if not result["publication_ready"]:
        errors.append("completed result must be publication_ready")
    strategies = result["strategies"]
    if len({s["strat"] for s in strategies}) != len(strategies):
        errors.append("a STRAT is claimed more than once")
    required = STRATEGY_PHASES if strategies else ["create"]
    absent = [p for p in required if p not in result["completed_phases"]]
    if absent:
        errors.append(f"completed result is missing phases {absent}")

    actual_context = context_status(repo)
    if actual_context != result["architecture_context"]:
        errors.append(f"architecture_context claimed "
                      f"{result['architecture_context']}, repository has "
                      f"{actual_context}")
    if strategies and actual_context != "available":
        errors.append("reviews ran without architecture context; the review "
                      "is incomplete and cannot be published")

    for strategy in strategies:
        check_strategy_files(repo, strategy, errors)
    extra = unclaimed_files(repo, strategies)
    if extra:
        errors.append(f"strategy files not produced by this run: {extra[:5]}")

    if use_jira and not errors:
        check_jira(result, errors)
    return errors


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--result", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--repo", required=True, type=Path)
    parser.add_argument("--state-dir", type=Path,
                        default=os.environ.get("STRAT_STATE_DIR") or None)
    parser.add_argument("--schema")
    parser.add_argument("--no-jira", action="store_true")
    args = parser.parse_args(argv)

    try:
        result = json.loads(args.result.read_text())
    except (OSError, ValueError) as exc:
        print(f"FAIL: cannot read {args.result}: {exc}")
        return 1
    if not isinstance(result, dict):
        print("FAIL: result is not a JSON object")
        return 1

    use_jira = (not args.no_jira
                and os.environ.get("STRAT_VALIDATE_JIRA", "1") != "0"
                and all(os.environ.get(v) for v in
                        ("JIRA_SERVER", "JIRA_USER", "JIRA_TOKEN")))
    errors = validate(result, args.output_dir, args.repo, args.state_dir,
                      args.schema, use_jira)
    if errors:
        for error in errors:
            print(f"FAIL: {' '.join(error.split())[:400]}")
        return 1
    checked = "run record, artifacts, progress" + (" and Jira" if use_jira
                                                    else "")
    print(f"PASS: {result['action']} {result['mode']} result matches "
          f"{checked}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
