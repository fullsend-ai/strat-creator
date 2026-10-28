#!/usr/bin/env python3
"""Deterministic post-refine push: iterate strategy files and push to Jira.

Runs as a CI step after /strategy-refine to guarantee all refined strategies
reach Jira, regardless of whether the LLM skill called push_strategy.py.

Usage:
    python3 scripts/push_refined_strategies.py [--artifacts-dir DIR]

Default artifacts-dir: artifacts/strat-tasks
"""

import argparse
import glob
import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))

from jira_utils import add_labels, require_env

REFINED_LABEL = "strat-creator-auto-refined"


def excluded_strategies():
    """Do not repush review-only imports or tickets with verified input drift."""
    if not Path("tmp/strat-input.json").exists():
        return {}
    from strat_pipeline_state import load_state, progress_lock, review_only
    with progress_lock():
        data, progress = load_state(batch=True)
    reasons = dict.fromkeys(review_only(data), "verified review-only import; prior push reused")
    reasons.update({v["key"]: "deferred: " + v["reason"] for v in progress.get("deferred", [])})
    return {pair["strat"]: reasons[pair["rfe"]]
            for pair in (progress.get("create") or {}).get("strategies", []) if pair["rfe"] in reasons}


def read_frontmatter(filepath):
    """Read frontmatter from a strategy file via frontmatter.py."""
    script = os.path.join(os.path.dirname(__file__), "frontmatter.py")
    result = subprocess.run(
        [sys.executable, script, "read", filepath],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        print(f"  WARN: frontmatter read failed for {filepath}: "
              f"{result.stderr.strip()}", file=sys.stderr)
        return None
    return json.loads(result.stdout)


def push_strategy(jira_key, filepath):
    """Call push_strategy.py as a subprocess."""
    script = os.path.join(os.path.dirname(__file__), "push_strategy.py")
    result = subprocess.run(
        [sys.executable, script, jira_key, filepath],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        print(f"  ERROR: push failed for {jira_key}: "
              f"{result.stderr.strip()}", file=sys.stderr)
        return False
    if result.stdout.strip():
        print(f"  {result.stdout.strip()}", file=sys.stderr)
    return True


def main():
    parser = argparse.ArgumentParser(description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--artifacts-dir", default="artifacts/strat-tasks",
                        help="Directory containing strategy files")
    args = parser.parse_args()

    server, user, token = require_env()
    if not all([server, user, token]):
        print("Error: JIRA_SERVER, JIRA_USER, JIRA_TOKEN required.",
              file=sys.stderr)
        return 2

    files = sorted(glob.glob(os.path.join(args.artifacts_dir, "RHAISTRAT-*.md")))
    if not files:
        print("No RHAISTRAT files found, nothing to push.")
        return 0

    pushed = 0
    skipped = 0
    failed = 0
    excluded = excluded_strategies()

    for filepath in files:
        basename = os.path.basename(filepath)
        fm = read_frontmatter(filepath)
        if fm is None:
            print(f"[SKIP] {basename} — could not read frontmatter")
            skipped += 1
            continue

        status = fm.get("status")
        jira_key = fm.get("jira_key")

        if jira_key in excluded:
            print(f"[SKIP] {basename} — {excluded[jira_key]}")
            skipped += 1
            continue

        if status != "Refined" or not jira_key:
            print(f"[SKIP] {basename} — status={status}, jira_key={jira_key}")
            skipped += 1
            continue

        print(f"[PUSH] {jira_key} — pushing strategy to Jira...")
        if not push_strategy(jira_key, filepath):
            # An edit during this loop can defer one target without hiding an
            # unrelated push failure. Recompute from live inputs, not stderr.
            if jira_key in excluded_strategies():
                skipped += 1
                continue
            failed += 1
            continue

        from strat_freshness import StaleTicket
        try:
            add_labels(server, user, token, jira_key, [REFINED_LABEL])
        except StaleTicket:
            if jira_key not in excluded_strategies():
                raise
            skipped += 1
            continue
        print(f"[LABEL] {REFINED_LABEL} added to {jira_key}")
        pushed += 1

    total = pushed + skipped + failed
    print(f"\nPushed {pushed}/{total} strategies to Jira"
          f" ({skipped} skipped, {failed} failed)")

    return 1 if failed > 0 else 0


if __name__ == "__main__":
    sys.exit(main())
