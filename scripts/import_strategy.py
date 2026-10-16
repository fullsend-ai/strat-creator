#!/usr/bin/env python3
"""Import the prepared strategy, including oversized attachment content."""
import argparse
import json
from pathlib import Path

from jira_utils import require_env
from pull_strategy import pull_strategy
from strat_pipeline_state import check_strat, load_input, load_progress


def import_strategy(key, rfe):
    check_strat(key)
    data = load_input()
    progress = load_progress(data["run_id"])
    if rfe not in data["acquired_keys"]:
        raise ValueError("RFE is not acquired by this prepared run")
    root = Path.cwd().resolve()
    reused = data.get("reused_push", {}).get(rfe)
    if reused and reused["key"] != key:
        raise ValueError("import key differs from the verified reusable strategy")
    # The pull helper writes several sibling artifacts. Refuse redirected paths
    # before any import writes, including originals, comments and prior reviews.
    for part in ("artifacts", "artifacts/strat-tasks", "artifacts/strat-originals",
                 "artifacts/strat-reviews", "artifacts/strat-comments"):
        directory = root / part
        if directory.is_symlink() or (directory.exists() and not directory.is_dir()):
            raise ValueError("import output directory is not a regular directory")
        if directory.exists() and any(path.is_symlink() for path in directory.iterdir()):
            raise ValueError("import output contains a symlink")
    existing = root / "artifacts/strat-tasks" / f"{key}.md"
    if existing.exists():
        from artifact_utils import read_frontmatter_validated
        fm, _ = read_frontmatter_validated(existing, "strat-task")
        if fm.get("strat_id") != key or fm.get("source_rfe") != rfe:
            raise ValueError("existing import identity disagrees")
        return {"strat_key": key, "already_imported": True, "files": [str(existing)]}
    if progress.get("review", {}).get(key) or (root / "tmp/review-composition" / key / "active.json").exists():
        raise ValueError("review already binds this task")
    return pull_strategy(*require_env(), key, "artifacts", pipeline_rfe=rfe)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("key")
    parser.add_argument("--rfe", required=True)
    args = parser.parse_args()
    print(json.dumps(import_strategy(args.key, args.rfe)))


if __name__ == "__main__":
    main()
