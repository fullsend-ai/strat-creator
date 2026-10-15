#!/usr/bin/env python3
"""Run state for the strat-pipeline Workflow, inside the sandbox.

The Workflow script (workflows/strat-pipeline.js) has no shell of its
own: each step agent calls this helper to read the run's input, record what
finished, and assemble the agent result. Progress lives in
tmp/strat-progress.json in the repository, because a fullsend validation retry
runs in the same sandbox but starts with an empty $FULLSEND_OUTPUT_DIR. The
host validator reads the same file from the downloaded repository.

Commands (run from the repository root):
  read                      input and progress as one JSON object
  record-create             strategies created for this run's RFEs
  mark refine KEY | mark push [--skipped]   (push covers every refined strategy)
  record-review KEY         verdict from the review file
  result [--failed STEP[:KEY]]...
                            write $FULLSEND_OUTPUT_DIR/agent-result.json

The host pre-script writes tmp/strat-input.json; this helper never locks,
unlocks or calls Jira.
"""

import argparse
import contextlib
import fcntl
import json
import os
import re
import shutil
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from artifact_utils import read_frontmatter, read_frontmatter_validated  # noqa: E402

INPUT = Path("tmp/strat-input.json")
PROGRESS = Path("tmp/strat-progress.json")
PROGRESS_LOCK = Path("tmp/strat-progress.lock")
TASKS = Path("artifacts/strat-tasks")
REVIEWS = Path("artifacts/strat-reviews")
ORIGINALS = Path("artifacts/strat-originals")
SKIPPED_MD = Path("artifacts/strat-skipped.md")
TICKETS_MD = Path("artifacts/strat-tickets.md")
SCORE_ROOT = Path("/tmp/strat-assess")

RFE_RE = re.compile(r"^RHAIRFE-\d+$")
STRAT_RE = re.compile(r"^(RHAISTRAT|STRAT)-\d+$")
STEPS = ("create", "refine", "push", "score", "review", "workflow")
RESUME_STEPS = ("create", "refine", "push", "review")


def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def die(message):
    print(f"strat_pipeline_state: ERROR: {message}", file=sys.stderr)
    sys.exit(1)


def load_input():
    if not INPUT.is_file():
        die(f"{INPUT} not found; the host pre-script writes it")
    data = json.loads(INPUT.read_text())
    for key in data.get("acquired_keys", []):
        if not RFE_RE.match(key):
            die(f"invalid key in {INPUT}: {key!r}")
    return data


def load_progress(run_id):
    if PROGRESS.is_file():
        progress = json.loads(PROGRESS.read_text())
        if progress.get("run_id") == run_id:
            return progress
    return {"run_id": run_id, "create": None, "refine": {}, "push": None,
            "review": {}}


def save_progress(progress):
    PROGRESS.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=PROGRESS.parent, prefix=".strat-progress.", suffix=".tmp")
    with os.fdopen(fd, "w") as stream:
        stream.write(json.dumps(progress, indent=2, sort_keys=True) + "\n")
    os.replace(tmp, PROGRESS)


@contextlib.contextmanager
def progress_lock():
    """Serialise read-modify-write of the progress file.

    The Workflow runs refine and review agents in parallel, and each records
    its own step; without the lock two of them can read the same snapshot and
    one entry is lost.
    """
    PROGRESS_LOCK.parent.mkdir(parents=True, exist_ok=True)
    with open(PROGRESS_LOCK, "a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def pushed(progress):
    """Strategies a recorded push covered (dry-run pushes are recorded as skipped)."""
    return sorted((progress.get("push") or {}).get("strategies", []))


def pushable(key):
    path = TASKS / f"{key}.md"
    if path.is_symlink() or not path.is_file():
        return False
    try:  # push_refined_strategies.py skips a file whose frontmatter fails the schema
        fm, _ = read_frontmatter_validated(path, "strat-task")
    except Exception:
        return False
    return key.startswith("RHAISTRAT-") and fm.get("status") == "Refined" and fm.get("jira_key") == key


def review_only(data):
    """RFEs whose host-computed resume point is review: refine and push are skipped."""
    points = data.get("resume_points", {})
    return [k for k in data.get("acquired_keys", []) if points.get(k) == "review"]


def check_strat(key):
    if not STRAT_RE.match(key):
        die(f"not a strategy key: {key!r}")
    return key


def score_dir(key):
    return SCORE_ROOT / check_strat(key)


def prepare_score_dir(key, clean):
    """Create /tmp/strat-assess/<KEY> for the scorer, refusing symlinks.

    /tmp can be shared outside the sandbox, so a planted link in place of the
    run directory (or its parent) must not redirect the scorer's writes or the
    clean-up below.
    """
    path = score_dir(key)
    for part in (SCORE_ROOT, path):
        if part.is_symlink():
            die(f"{part} is a symlink; refusing to use it")
    SCORE_ROOT.mkdir(mode=0o700, exist_ok=True)
    if clean and path.exists():
        shutil.rmtree(path)
    path.mkdir(mode=0o700, exist_ok=True)


def skipped_reasons():
    """RFE key -> reason, from the skip table strategy-create appends to."""
    reasons = {}
    if not SKIPPED_MD.is_file():
        return reasons
    for line in SKIPPED_MD.read_text().splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) >= 3 and RFE_RE.match(cells[0]):
            reasons[cells[0]] = cells[2][:500] or "skipped by strategy-create"
    return reasons


def created_strategies(acquired):
    """(rfe, strat) pairs for this run's RFEs, from the task frontmatter."""
    pairs = []
    if not TASKS.is_dir():
        return pairs
    for path in sorted(TASKS.glob("*.md")):
        if path.is_symlink() or not path.is_file():
            continue
        data, _ = read_frontmatter(path)
        rfe, strat = data.get("source_rfe"), data.get("strat_id")
        if rfe in acquired and isinstance(strat, str) and STRAT_RE.match(strat) \
                and path.name == f"{strat}.md":
            pairs.append({"rfe": rfe, "strat": strat})
    return sorted(pairs, key=lambda p: (acquired.index(p["rfe"]), p["strat"]))


def cmd_read(_args):
    data = load_input()
    progress = load_progress(data["run_id"])
    create = progress.get("create") or {}
    strategies = create.get("strategies", [])
    reviewed = sorted(progress.get("review", {}))
    # A create skip the host did not expect (expected_skips) stays pending, so a
    # retry runs strategy-create for it again instead of carrying the claim.
    expected = {s["key"] for s in data.get("expected_skips") or []}
    resolved = {p["rfe"] for p in strategies} | {s["key"] for s in create.get("skipped", []) if s["key"] in expected}
    pending = [k for k in data.get("acquired_keys", []) if k not in resolved]
    for pair in strategies:
        if pair["strat"] not in reviewed:
            prepare_score_dir(pair["strat"], clean=False)
    print(json.dumps({
        "cwd": os.getcwd(),
        "run_id": data["run_id"],
        "mode": data["mode"],
        "dry_run": bool(data.get("dry_run")),
        "acquired_keys": data.get("acquired_keys", []),
        "review_only": review_only(data),
        "create_done": bool(create.get("done_at")),
        "pending_create": pending,
        "strategies": strategies,
        "refined": sorted(progress.get("refine", {})),
        "pushed": pushed(progress),
        "reviewed": reviewed,
    }, indent=2))


def cmd_record_create(_args):
    """Record what strategy-create produced; done only when every RFE is resolved.

    An RFE with neither a strategy nor a skip row stays pending, so the next
    attempt runs strategy-create again for the pending keys only.
    """
    data = load_input()
    acquired = data.get("acquired_keys", [])
    with progress_lock():
        progress = load_progress(data["run_id"])
        known = {p["strat"] for p in (progress.get("create") or {}).get("strategies", [])}
        strategies = created_strategies(acquired)
        with_strategy = {p["rfe"] for p in strategies}
        reasons = skipped_reasons()
        skipped = [{"key": k, "reason": reasons[k]} for k in acquired
                   if k not in with_strategy and k in reasons]
        expected = {s["key"] for s in data.get("expected_skips") or []}
        pending = [k for k in acquired if k not in with_strategy and not (k in reasons and k in expected)]
        for pair in strategies:
            if pair["strat"] not in known:
                prepare_score_dir(pair["strat"], clean=True)
        progress["create"] = {"done_at": None if pending else now(), "strategies": strategies,
                              "skipped": skipped, "pending": pending}
        save_progress(progress)
    print(json.dumps({"strategies": strategies, "skipped": skipped, "pending": pending}, indent=2))


def cmd_mark(args):
    data = load_input()
    with progress_lock():
        progress = load_progress(data["run_id"])
        recorded = _mark(args, progress)
    out = {"recorded": recorded}
    if recorded == "push":
        out["strategies"] = pushed(progress)
    print(json.dumps(out))


def _mark(args, progress):
    if args.step == "refine":
        if not args.key:
            die("mark refine needs a strategy key")
        strategy = check_strat(args.key)
        progress["refine"][strategy] = now()
        recorded = strategy
    elif args.step == "push":
        # push_refined_strategies.py pushes every artifacts/strat-tasks/RHAISTRAT-*.md
        # whose frontmatter says status Refined with that jira_key, skips the rest,
        # and exits nonzero if any push fails; so after a successful run those
        # are exactly the strategies it pushed. A strategy refined later (a
        # retried create) needs a push of its own. A dry run pushes nothing.
        refined_now = set(progress.get("refine", {}))
        if not args.skipped:
            refined_now = {k for k in refined_now if pushable(k)}
        covered = set(pushed(progress)) | refined_now
        progress["push"] = {"done_at": now(), "skipped": bool(args.skipped), "strategies": sorted(covered)}
        recorded = "push"
    else:
        die(f"cannot mark {args.step!r}")
    save_progress(progress)
    return recorded


def cmd_record_review(args):
    data = load_input()
    key = check_strat(args.key)
    path = REVIEWS / f"{key}-review.md"
    if path.is_symlink() or not path.is_file():
        die(f"{path} not found")
    fm, _ = read_frontmatter(path)
    recommendation = fm.get("recommendation")
    if recommendation not in ("approve", "revise", "reject"):
        die(f"{path} has no recommendation")
    verdict = {"recommendation": recommendation,
               "needs_attention": bool(fm.get("needs_attention")),
               "done_at": now()}
    with progress_lock():
        progress = load_progress(data["run_id"])
        progress["review"][key] = verdict
        save_progress(progress)
    print(json.dumps({"key": key, "recommendation": recommendation,
                      "needs_attention": verdict["needs_attention"]}))


def parse_failed(values):
    failed = []
    for value in values or []:
        step, _, key = value.partition(":")
        if step not in STEPS or (key and not (STRAT_RE.match(key) or RFE_RE.match(key))):
            die(f"invalid --failed value: {value!r}")
        failed.append((step, key))
    return failed


def existing(paths):
    return [str(p) for p in paths if p.is_file() and not p.is_symlink()]


def assemble(data, progress, failed):
    acquired = data.get("acquired_keys", [])
    create = progress.get("create") or {}
    strategies = create.get("strategies", [])
    refined, reviews = progress.get("refine", {}), progress.get("review", {})
    skip_refine = set(review_only(data))
    needs_refine = [p for p in strategies if p["rfe"] not in skip_refine]
    errors = [f"{step}: {key or 'step'} returned no result" for step, key in failed]

    if not create:
        errors.append("create: not recorded")
    created = {p["rfe"] for p in strategies}
    skipped_create = {s["key"] for s in create.get("skipped", [])}
    for key in acquired:
        if create and key not in created and key not in skipped_create:
            errors.append(f"create: no strategy recorded for {key}")
    for pair in strategies:
        if pair in needs_refine and pair["strat"] not in refined:
            errors.append(f"refine: {pair['strat']} not recorded")
        if pair["strat"] not in reviews:
            errors.append(f"review: {pair['strat']} not recorded")
    covered = set(pushed(progress))
    for pair in needs_refine:
        if pair["strat"] in refined and pair["strat"] not in covered:
            errors.append(f"push: {pair['strat']} not recorded")
    errors = list(dict.fromkeys(errors))

    phases = []
    if create.get("done_at"):
        phases.append("create")
    if create and all(p["strat"] in refined for p in needs_refine):
        phases.append("refine")
    if create and all(p["strat"] in covered for p in needs_refine):
        phases.append("push")
    if create and all(p["strat"] in reviews for p in strategies):
        phases.append("review")

    results = [{"rfe": p["rfe"], "strat": p["strat"],
                "recommendation": reviews[p["strat"]]["recommendation"],
                "needs_attention": reviews[p["strat"]]["needs_attention"]}
               for p in strategies if p["strat"] in reviews]
    artifacts = []
    for pair in strategies:
        artifacts += existing([TASKS / f"{pair['strat']}.md",
                               REVIEWS / f"{pair['strat']}-review.md",
                               REVIEWS / f"{pair['strat']}-review-comment.md",
                               ORIGINALS / f"{pair['rfe']}.md",
                               ORIGINALS / f"{pair['rfe']}-comments.md",
                               ORIGINALS / f"{pair['strat']}.md"])
    artifacts += existing([SKIPPED_MD, TICKETS_MD])

    action = "failed" if errors else "completed"
    summary = (f"{len(results)} of {len(strategies)} strategies reviewed for "
               f"{len(acquired)} locked RFE(s); "
               f"{len(create.get('skipped', []))} skipped by strategy-create")
    if errors:
        summary += f"; {len(errors)} error(s)"
    return {
        "action": action,
        "mode": data["mode"],
        "run_id": data["run_id"],
        "summary": summary + ".",
        "input_keys": data.get("input_keys", []),
        "acquired_keys": acquired,
        "skipped": data.get("skipped", []) + create.get("skipped", []),
        "strategies": results,
        "completed_phases": phases,
        "artifacts": artifacts,
        "architecture_context": data.get("architecture_context", "missing"),
        "dry_run": bool(data.get("dry_run")),
        "errors": errors,
    }


def cmd_result(args):
    data = load_input()
    progress = load_progress(data["run_id"])
    result = assemble(data, progress, parse_failed(args.failed))
    out_dir = os.environ.get("FULLSEND_OUTPUT_DIR")
    if not out_dir:
        die("FULLSEND_OUTPUT_DIR is not set")
    path = Path(out_dir) / "agent-result.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps({"path": str(path), "action": result["action"],
                      "errors": result["errors"]}, indent=2))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("read").set_defaults(func=cmd_read)
    sub.add_parser("record-create").set_defaults(func=cmd_record_create)
    mark = sub.add_parser("mark")
    mark.add_argument("step", choices=["refine", "push"])
    mark.add_argument("key", nargs="?")
    mark.add_argument("--skipped", action="store_true",
                      help="push: recorded as skipped (dry run)")
    mark.set_defaults(func=cmd_mark)
    review = sub.add_parser("record-review")
    review.add_argument("key")
    review.set_defaults(func=cmd_record_review)
    result = sub.add_parser("result")
    result.add_argument("--failed", action="append",
                        help="STEP[:KEY] the Workflow saw return no result")
    result.set_defaults(func=cmd_result)
    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
