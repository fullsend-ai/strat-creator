#!/usr/bin/env python3
"""Organize, commit, and push strategy pipeline results to the data repo.

The data repo is already cloned at --results-dir (default: artifacts/).
This script:
1. Moves strategy artifacts into a timestamped run directory
2. Updates the 'current' symlink to point to the latest run
3. Commits and pushes with retry+rebase (no force push)

Data repo structure:
    RHAISTRAT/
        20260411-143000/        # timestamped run
            strat-tasks/
            strat-reviews/
            strat-originals/
            reports/
        20260412-100000/
        current -> 20260412-100000
"""

import argparse
import os
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

# Source: strat-pipeline ci-scripts/push-results.py at fd36b15c5095c9f20a270b1d69933c578c04d9da.
# Adapted: accept an explicit local/remote repository URL and rely on the
# caller's process-scoped Git HTTP authorization header. Never persist tokens
# in the results repository's .git/config.


def git(args, cwd):
    return subprocess.run(args, cwd=cwd, capture_output=True, text=True)


def organize_run(results_dir):
    """Move strategy artifacts into a timestamped run directory."""
    rhaistrat_dir = results_dir / "RHAISTRAT"
    rhaistrat_dir.mkdir(exist_ok=True)

    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    run_dir = rhaistrat_dir / ts

    # Collect artifacts written by Claude
    artifact_dirs = ["strat-tasks", "strat-reviews", "strat-originals", "reports"]
    has_artifacts = False

    for d in artifact_dirs:
        src = results_dir / d
        if src.is_dir() and any(src.iterdir()):
            has_artifacts = True
            break

    if not has_artifacts:
        print("WARNING: No strategy artifacts found to organize.")
        return None

    run_dir.mkdir(exist_ok=True)
    for d in artifact_dirs:
        src = results_dir / d
        if src.is_dir():
            dest = run_dir / d
            if any(src.iterdir()):
                shutil.copytree(src, dest, dirs_exist_ok=True)

    # Clean up flat directories now that they're organized
    for d in artifact_dirs:
        src = results_dir / d
        if src.is_dir():
            shutil.rmtree(src)
    # Copy loose artifact files into the run directory
    for name in ["strat-skipped.md", "strat-jira-guide.md", "strat-tickets.md",
                  "pipeline-data.json", "claude-otel.jsonl"]:
        src = results_dir / name
        if src.exists():
            shutil.copy2(src, run_dir / name)
            src.unlink()

    # Update current symlink
    current = rhaistrat_dir / "current"
    if current.is_symlink():
        current.unlink()
    current.symlink_to(ts)

    print(f"Organized artifacts into RHAISTRAT/{ts}/")
    return run_dir


def main():
    parser = argparse.ArgumentParser(description="Push strategy results to data repo")
    parser.add_argument("--results-dir", default="artifacts",
                        help="Path to the cloned data repo (default: artifacts)")
    parser.add_argument("--results-repo", default=os.environ.get("RESULTS_REPO_URL", ""),
                        help="Results repository URL (default: $RESULTS_REPO_URL)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Organize and commit locally, but skip push")
    args = parser.parse_args()

    results_dir = Path(args.results_dir)

    if not results_dir.exists():
        print("ERROR: results directory does not exist.")
        sys.exit(1)
    if not (results_dir / ".git").exists():
        print("ERROR: results directory is not a git repository.")
        sys.exit(1)

    if not args.results_repo:
        print("ERROR: RESULTS_REPO_URL or --results-repo is required.")
        sys.exit(1)

    # Credentials are supplied through GIT_CONFIG_* by the entrypoint, not
    # embedded in the URL persisted by this command.
    git(["git", "remote", "set-url", "origin", args.results_repo], cwd=results_dir)

    # Configure git user
    git(["git", "config", "user.email", "strat-pipeline@ci.noreply"], cwd=results_dir)
    git(["git", "config", "user.name", "strat-pipeline"], cwd=results_dir)

    # Pull BEFORE organize_run — the working tree is still clean here.
    # organize_run updates the tracked 'current' symlink, which would
    # cause rebase to fail with "You have unstaged changes."
    pull = git(["git", "pull", "--rebase", "-X", "theirs",
                "origin", "main"], cwd=results_dir)
    if pull.returncode != 0:
        print(f"WARNING: pre-commit pull failed (may be initial push): "
              f"{pull.stderr.strip()}")

    # Organize artifacts into timestamped run directory
    run_dir = organize_run(results_dir)
    if run_dir is None:
        print("No artifacts to push.")
        sys.exit(0)

    # Spot check: verify the run has content
    tasks = list(run_dir.glob("strat-tasks/*.md"))
    reviews = list(run_dir.glob("strat-reviews/*.md"))
    print(f"Spot check: {run_dir.name}/ has {len(tasks)} task(s), {len(reviews)} review(s)")

    # Stage all changes (including deletions from pruning)
    result = git(["git", "add", "-A"], cwd=results_dir)
    if result.returncode != 0:
        print(f"ERROR: git add failed: {result.stderr}")
        sys.exit(1)

    # Check if there's anything to commit
    status = git(["git", "status", "--porcelain"], cwd=results_dir)
    if status.returncode != 0:
        print(f"ERROR: git status failed: {status.stderr}")
        sys.exit(1)

    if not status.stdout.strip():
        print("Nothing to commit — results are clean.")
        sys.exit(0)

    # Commit
    message = (
        f"Strategy pipeline run {run_dir.name}\n\n"
        f"{len(tasks)} strategies, {len(reviews)} reviews\n\n"
        "Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>"
    )
    result = git(["git", "commit", "-m", message], cwd=results_dir)
    if result.returncode != 0:
        print(f"ERROR: git commit failed: {result.stderr}")
        sys.exit(1)
    for line in result.stdout.splitlines():
        if line.strip():
            print(line)
            break

    if args.dry_run:
        print("Dry run: committed locally, skipping push.")
        sys.exit(0)

    if not args.dry_run and not os.environ.get("RESULTS_PUSH_TOKEN"):
        print("ERROR: RESULTS_PUSH_TOKEN is required to publish results.")
        sys.exit(1)

    # Push with retry + rebase (no force push)
    for attempt in range(1, 4):
        result = git(["git", "push", "-u", "origin", "HEAD"], cwd=results_dir)
        if result.returncode == 0:
            break
        print(f"  Push failed (attempt {attempt}/3): {result.stderr.strip()}")
        if attempt == 3:
            print("ERROR: push failed after 3 attempts")
            sys.exit(1)
        # Check if remote has any branches before attempting pull --rebase
        ls_remote = git(["git", "ls-remote", "--heads", "origin"], cwd=results_dir)
        if not ls_remote.stdout.strip():
            print("ERROR: remote is empty and initial push failed (check token permissions)")
            sys.exit(1)
        print(f"  Pulling and retrying...")
        rebase = git(["git", "pull", "--rebase", "-X", "theirs",
                      "origin", "HEAD"], cwd=results_dir)
        if rebase.returncode != 0:
            print(f"ERROR: pull --rebase failed: {rebase.stderr}")
            sys.exit(1)

    print("Pushed to remote.")


if __name__ == "__main__":
    main()
