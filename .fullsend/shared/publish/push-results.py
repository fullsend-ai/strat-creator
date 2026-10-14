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
import hashlib
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "scripts"))
from strat_side_effects import atomic, locked  # noqa: E402

ARTIFACT_DIRS = ("strat-tasks", "strat-reviews", "strat-originals", "reports")
LOOSE = ("strat-skipped.md", "strat-jira-guide.md", "strat-tickets.md", "pipeline-data.json", "claude-otel.jsonl")

# Carried from github.com/jctanner-opendatahub-io/strat-creator at 78ab3f1e,
# .fullsend/scripts/ci/push-results.py (Apache-2.0); see .fullsend/README.md.
# Adapted: accept an explicit local/remote repository URL and rely on the
# caller's process-scoped Git HTTP authorization header. Never persist tokens
# in the results repository's .git/config.


def git(args, cwd):
    return subprocess.run(args, cwd=cwd, capture_output=True, text=True)


def organize_run(results_dir, run_id=None):
    """Move strategy artifacts into a timestamped run directory."""
    if run_id is not None:
        return organize_identified_run(results_dir, run_id)
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


def file_hash(path):
    if path.is_symlink() or not path.is_file():
        raise ValueError("publication accepts regular files only")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def organize_identified_run(results_dir, run_id):
    """Immutable directory per run; recover partial copying and a lost push ack."""
    import re
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,100}", run_id):
        raise ValueError("invalid publication run ID")
    state = results_dir / ".git/strat-publication.json"
    run_dir = results_dir / "RHAISTRAT" / ("run-" + run_id)
    manifest_path = run_dir / ".publication.json"
    files = {}
    for directory in ARTIFACT_DIRS:
        for path in sorted((results_dir / directory).rglob("*")):
            if path.is_symlink():
                raise ValueError("symlink in publication inputs")
            if path.is_file():
                files[str(path.relative_to(results_dir))] = file_hash(path)
    for name in LOOSE:
        path = results_dir / name
        if path.exists():
            files[name] = file_hash(path)
    desired = {"contract": "strat-publication-v1", "run_id": run_id, "files": files}
    saved = json.loads(state.read_text()) if state.exists() else None
    existing = json.loads(manifest_path.read_text()) if manifest_path.exists() else None
    if saved and saved["run_id"] != run_id:
        raise ValueError("publication workspace belongs to another run")
    if saved and files and files != saved["files"]:
        raise ValueError("publication input changed during recovery")
    if not files:
        desired = saved or existing
        if not desired:
            return None
    if existing and desired != existing:
        raise ValueError("immutable run already published with different content")
    atomic(state, desired)  # Survives death before copies, commit or push.
    run_dir.mkdir(parents=True, exist_ok=True)
    for relative, expected in desired["files"].items():
        src, dest = results_dir / relative, run_dir / relative
        if src.exists():
            if file_hash(src) != expected:
                raise ValueError("publication source changed")
            if dest.exists() and file_hash(dest) != expected:
                raise ValueError("conflicting partial publication")
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, dest)
        if file_hash(dest) != expected:
            raise ValueError("publication recovery lacks required content")
    actual = {str(p.relative_to(run_dir)) for p in run_dir.rglob("*") if p.is_file()
              and p != manifest_path}
    if actual != set(desired["files"]):
        raise ValueError("publication directory contains unexpected content")
    atomic(manifest_path, desired)
    for directory in ARTIFACT_DIRS:
        src = results_dir / directory
        if src.exists():
            shutil.rmtree(src)
    for name in LOOSE:
        (results_dir / name).unlink(missing_ok=True)
    # Reconciliation of an already published run must not move current back
    # over a newer run. A first/partial publication can select this run.
    current = run_dir.parent / "current"
    in_head = git(["git", "cat-file", "-e", f"HEAD:RHAISTRAT/{run_dir.name}/.publication.json"],
                  cwd=results_dir).returncode == 0
    if not existing or (saved and not in_head) or not (current.exists() or current.is_symlink()):
        if current.is_symlink():
            current.unlink()
        current.symlink_to(run_dir.name)
    return run_dir


def main():
    parser = argparse.ArgumentParser(description="Push strategy results to data repo")
    parser.add_argument("--results-dir", default="artifacts",
                        help="Path to the cloned data repo (default: artifacts)")
    parser.add_argument("--results-repo", default=os.environ.get("RESULTS_REPO_URL", ""),
                        help="Results repository URL (default: $RESULTS_REPO_URL)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Organize and commit locally, but skip push")
    parser.add_argument("--run-id", default=os.environ.get("STRAT_RUN_ID"),
                        help="Stable run identity (required by the S1 post-script)")
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
    run_dir = organize_run(results_dir, args.run_id)
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
    else:
        message = (f"Strategy pipeline run {run_dir.name}\n\n"
                   f"{len(tasks)} strategies, {len(reviews)} reviews\n\nAssisted-by: Claude")
        result = git(["git", "commit", "-m", message], cwd=results_dir)
        if result.returncode != 0:
            print(f"ERROR: git commit failed: {result.stderr}")
            sys.exit(1)

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
        print("  Pulling and retrying...")
        rebase = git(["git", "pull", "--rebase", "-X", "theirs",
                      "origin", "HEAD"], cwd=results_dir)
        if rebase.returncode != 0:
            print(f"ERROR: pull --rebase failed: {rebase.stderr}")
            sys.exit(1)

    print("Pushed to remote.")


if __name__ == "__main__":
    # Hold through organization, commit and push for same-workspace restarts.
    parser_dir = argparse.ArgumentParser(add_help=False)
    parser_dir.add_argument("--results-dir", default="artifacts")
    preliminary, _ = parser_dir.parse_known_args()
    with locked(Path(preliminary.results_dir) / ".git/strat-publication.lock"):
        main()
