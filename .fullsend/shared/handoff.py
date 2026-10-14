#!/usr/bin/env python3
"""Copy validated strategy artifacts out of the downloaded repository.

The source is agent-writable. Copy only regular files from the known
artifact locations; skip symlinks and anything else.

Usage: handoff.py <downloaded-repo> <destination>
"""
import shutil
import sys
from pathlib import Path

DIRECTORIES = ("strat-tasks", "strat-reviews", "strat-originals")
FILES = ("strat-skipped.md", "strat-tickets.md")


def copy_file(source, destination):
    if source.is_symlink() or not source.is_file():
        return False
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, destination)
    return True


def handoff(repo, destination):
    artifacts = Path(repo) / "artifacts"
    destination = Path(destination)
    if destination.exists():
        shutil.rmtree(destination)
    destination.mkdir(parents=True)
    copied = 0
    for name in DIRECTORIES:
        directory = artifacts / name
        if directory.is_symlink() or not directory.is_dir():
            continue
        for source in sorted(directory.iterdir()):
            copied += copy_file(source, destination / name / source.name)
    for name in FILES:
        copied += copy_file(artifacts / name, destination / name)
    return copied


if __name__ == "__main__":
    count = handoff(sys.argv[1], sys.argv[2])
    print(f"handoff: copied {count} artifact file(s) to {sys.argv[2]}")
