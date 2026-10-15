#!/usr/bin/env python3
"""Check a strat-single/strat-batch result against what actually happened.

fullsend runs this on the host after each agent iteration (validation_loop),
from the trusted checkout, with TARGET_REPO_DIR pointing at the downloaded,
agent-writable repository. Everything under --repo is read as data; nothing
from it is imported or executed. Helpers come from this file's own checkout.

Checks, in order:
  1. the result matches the JSON schema;
  2. run_id, mode, input and acquired keys match the host run record
     ($STRAT_STATE_DIR/run.json, written by prepare_run.py), and every host
     skip is reported;
  3. every acquired RFE has a strategy or a skip reason, and every skip the
     agent claims for strategy-create matches the gate outcome the host
     computed before the run (run.json expected_skips);
  4. tmp/strat-progress.json records review for every strategy, and refine
     and the push for every strategy whose resume point is before review
     (the push is skipped only in a dry run);
  5. every listed artifact is a regular file under artifacts/, and each
     strategy's task and review files agree with the claim;
  6. the verdict follows from the review's scores (parse_results.py rules);
  7. with Jira credentials and not a dry run, each STRAT exists, is cloned
     from its RFE and carries the verdict label.

Each failure prints one FAIL line; fullsend appends them to the next attempt's
prompt (feedback_mode: append). Exit 0 only when every check passes.
"""

import argparse
import json
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "scripts" / "assess-strat"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from artifact_utils import compute_strat_labels, read_frontmatter  # noqa: E402
from handoff import allowed as handoff_allows  # noqa: E402
from parse_results import CRITERIA, compute_verdict  # noqa: E402

RESUME = ("Resume: call the Workflow tool with name \"strat-pipeline:strat-pipeline\" again; it reads "
          "tmp/strat-progress.json and runs only the steps that are not recorded.")


class Checker:
    def __init__(self):
        self.failures = []

    def fail(self, message):
        self.failures.append(message)

    def check(self, condition, message):
        if not condition:
            self.fail(message)
        return condition


def regular_file(repo, rel):
    """repo/rel if it is a regular file reached without any symlink below repo.

    The same rule as handoff.py: a symlinked ancestor of the repository itself
    is fine, a symlink inside it is not, so validation never accepts a file
    the handoff would refuse to copy.
    """
    path = repo / rel
    try:
        resolved = path.resolve()
    except OSError:
        return None
    parts = Path(rel).parts
    if any((repo / Path(*parts[:i])).is_symlink() for i in range(1, len(parts) + 1)):
        return None
    if not path.is_file() or repo.resolve() not in resolved.parents:
        return None
    return path


def read_json(path):
    with open(path) as f:
        return json.load(f)


def check_schema(c, result, schema_path):
    try:
        from jsonschema import Draft202012Validator
    except ImportError:
        c.fail("jsonschema is not installed on the runner")
        return False
    validator = Draft202012Validator(read_json(schema_path))
    for error in sorted(validator.iter_errors(result), key=lambda e: list(e.path)):
        where = ".".join(str(p) for p in error.path) or "(root)"
        c.fail(f"schema: {where}: {error.message}"[:300])
    return not c.failures


def check_run_record(c, result, run):
    for field in ("run_id", "mode", "input_keys", "acquired_keys", "dry_run"):
        c.check(result.get(field) == run.get(field),
                f"{field} is {result.get(field)!r}, the host run record says {run.get(field)!r}")
    reported = {s["key"] for s in result["skipped"]}
    for skip in run.get("skipped", []):
        c.check(skip["key"] in reported, f"host skip of {skip['key']} is not reported")
    c.check(result["architecture_context"] == run.get("architecture_context") == "available",
            "architecture context was not available for this run")


def check_coverage(c, result):
    with_strategy = {s["rfe"] for s in result["strategies"]}
    skipped = {s["key"] for s in result["skipped"]}
    for key in result["acquired_keys"]:
        c.check(key in with_strategy or key in skipped,
                f"{key} has neither a reviewed strategy nor a skip reason")
    for s in result["strategies"]:
        c.check(s["rfe"] in result["acquired_keys"], f"{s['strat']} is for {s['rfe']}, which this run did not lock")


def check_create_skips(c, result, run):
    """Agent-claimed create skips must match the host's gate check (prepare_run.py)."""
    host = {s["key"] for s in run.get("skipped", [])}
    acquired = set(result["acquired_keys"])
    claimed = {s["key"]: s["reason"] for s in result["skipped"] if s["key"] in acquired and s["key"] not in host}
    expected = run.get("expected_skips")
    created = {s["rfe"] for s in result["strategies"]}
    if expected is None:
        if claimed or created:
            c.fail("the host could not compute strategy-create's expected skips for this run, so neither a "
                   "skip nor a created strategy can be checked against the gates; rerun once Jira is reachable")
        return
    expected = {s["key"]: s["reason"] for s in expected}
    for key, reason in claimed.items():
        if "reconstruction failed" in reason.lower():
            strat = re.search(r"RHAISTRAT-\d+", reason)
            c.fail(f"STRAT {strat.group(0) if strat else 'for ' + key} references a business need the run "
                   "could not restore; restore the RFE original or fix the STRAT description, then rerun")
        elif key not in expected:
            c.fail(f"{key} is reported skipped by strategy-create ({reason[:120]}), but the host gate check "
                   "expected a strategy")
    for key, reason in expected.items():
        if key in created:
            c.fail(f"{key} fails strategy-create's gate ({reason}) but a strategy was created")


def check_progress(c, result, repo, run):
    path = regular_file(repo, "tmp/strat-progress.json")
    if not c.check(path is not None, "tmp/strat-progress.json is missing"):
        return
    try:
        progress = read_json(path)
    except (OSError, ValueError) as exc:
        c.fail(f"tmp/strat-progress.json is unreadable: {str(exc)[:200]}")
        return
    if not c.check(isinstance(progress, dict), "tmp/strat-progress.json is not a JSON object"):
        return
    for field, kinds in (("refine", (dict,)), ("review", (dict,)), ("push", (dict, type(None)))):
        if not c.check(isinstance(progress.get(field), kinds),
                       f"tmp/strat-progress.json field {field!r} has the wrong type"):
            return
    if not c.check(progress.get("run_id") == result["run_id"], "progress file belongs to another run"):
        return
    points = run.get("resume_points", {})
    refined = [s for s in result["strategies"] if points.get(s["rfe"]) != "review"]
    for s in refined:
        c.check(s["strat"] in progress.get("refine", {}), f"refine of {s['strat']} is not recorded")
    for s in result["strategies"]:
        review = progress.get("review", {}).get(s["strat"])
        c.check(review is not None, f"review of {s['strat']} is not recorded")
    push = progress.get("push") or {}
    covered = push.get("strategies", [])
    if not c.check(isinstance(covered, list), "tmp/strat-progress.json push.strategies is not a list"):
        return
    for s in refined:
        c.check(s["strat"] in covered, f"push of {s['strat']} is not recorded")
    if refined and push:
        c.check(result["dry_run"] or not push.get("skipped"), "push was skipped outside a dry run")


def review_verdict(c, strat, fm):
    scores = fm.get("scores") or {}
    try:
        values = {crit: int(scores[crit.lower()]) for crit in CRITERIA}
    except (KeyError, TypeError, ValueError):
        c.fail(f"{strat} review has no complete scores")
        return None
    values["Total"] = sum(values.values())
    if scores.get("total") is not None:
        c.check(int(scores["total"]) == values["Total"], f"{strat} review total does not add up")
    verdict, needs_attention = compute_verdict(values)
    return verdict.lower(), needs_attention


def check_artifacts(c, result, repo):
    for rel in result["artifacts"]:
        c.check(".." not in rel.split("/") and regular_file(repo, rel) is not None,
                f"listed artifact {rel} is not a regular file in the repository")
        c.check(handoff_allows(rel), f"listed artifact {rel} is not a path the handoff publishes")
    listed = set(result["artifacts"])
    for s in result["strategies"]:
        for rel in (f"artifacts/strat-tasks/{s['strat']}.md", f"artifacts/strat-reviews/{s['strat']}-review.md"):
            c.check(rel in listed, f"{rel} is not in the result's artifacts list")
    for s in result["strategies"]:
        task = regular_file(repo, f"artifacts/strat-tasks/{s['strat']}.md")
        review = regular_file(repo, f"artifacts/strat-reviews/{s['strat']}-review.md")
        if not c.check(task is not None, f"artifacts/strat-tasks/{s['strat']}.md is missing"):
            continue
        if not c.check(review is not None, f"artifacts/strat-reviews/{s['strat']}-review.md is missing"):
            continue
        task_fm, _ = read_frontmatter(task)
        c.check(task_fm.get("strat_id") == s["strat"] and task_fm.get("source_rfe") == s["rfe"],
                f"{s['strat']} task frontmatter does not match {s['rfe']}")
        review_fm, _ = read_frontmatter(review)
        c.check(review_fm.get("recommendation") == s["recommendation"],
                f"{s['strat']} is reported {s['recommendation']}, its review says "
                f"{review_fm.get('recommendation')}")
        c.check(bool(review_fm.get("needs_attention")) == s["needs_attention"],
                f"{s['strat']} needs_attention does not match its review")
        derived = review_verdict(c, s["strat"], review_fm)
        if derived:
            c.check(derived == (s["recommendation"], s["needs_attention"]),
                    f"{s['strat']} scores give {derived[0]}, not {s['recommendation']}")


def check_jira(c, result):
    from find_strat_for_rfe import find_strat_clones
    from jira_utils import get_issue, require_env

    server, user, token = require_env()
    for s in result["strategies"]:
        try:
            clones = {clone["key"] for clone in find_strat_clones(server, user, token, s["rfe"])}
            labels = set(get_issue(server, user, token, s["strat"], fields=["labels"])
                         .get("fields", {}).get("labels", []))
        except Exception as exc:  # report and continue: one unreachable issue fails the run
            c.fail(f"Jira readback of {s['strat']} failed: {str(exc)[:200]}")
            continue
        c.check(s["strat"] in clones, f"{s['strat']} is not cloned from {s['rfe']} in Jira")
        expected = set(compute_strat_labels("Reviewed", s["recommendation"]))
        missing = sorted(expected - labels)
        c.check(not missing, f"{s['strat']} is missing Jira label(s) {', '.join(missing)}")


def validate(result_path, repo, schema_path, run_path, env):
    c = Checker()
    try:
        result = read_json(result_path)
    except (OSError, ValueError) as exc:
        c.fail(f"agent-result.json is unreadable: {exc}")
        return c.failures
    if not check_schema(c, result, schema_path):
        return c.failures
    if result["action"] == "failed":
        c.fail("the run reports failure: " + "; ".join(result["errors"])[:600])
    elif result["action"] == "skipped":
        c.fail("the agent may not skip: the host pre-script already chose and locked work")
    run = read_json(run_path)
    check_run_record(c, result, run)
    check_coverage(c, result)
    check_create_skips(c, result, run)
    check_progress(c, result, repo, run)
    check_artifacts(c, result, repo)
    if result["dry_run"]:
        print("INFO: dry run, Jira readback skipped")
    elif all(env.get(k) for k in ("JIRA_SERVER", "JIRA_USER", "JIRA_TOKEN")):
        check_jira(c, result)
    else:
        print("INFO: no Jira credentials on the runner, Jira readback skipped")
    return c.failures


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--result", required=True)
    parser.add_argument("--repo", required=True, help="downloaded repository (data only)")
    parser.add_argument("--schema", required=True)
    parser.add_argument("--run", required=True, help="host run record (run.json)")
    args = parser.parse_args(argv)
    failures = validate(Path(args.result), Path(args.repo), Path(args.schema), Path(args.run),
                        dict(os.environ))
    if failures:
        for message in failures:
            print(f"FAIL: {message}")
        print(f"FAIL: {RESUME}")
        return 1
    print("PASS: result matches the run record, progress, artifacts and scores")
    return 0


if __name__ == "__main__":
    sys.exit(main())
