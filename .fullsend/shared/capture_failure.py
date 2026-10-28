#!/usr/bin/env python3
"""Retain failed-run artifacts/progress as data in the trusted host state."""
import argparse
import hashlib
import json
import shutil
import tempfile
from pathlib import Path


def capture(repo, state, result, *, deferred=False):
    repo, state, result = Path(repo).resolve(), Path(state).resolve(), Path(result)
    destination = state / "recovery"
    destination.mkdir(parents=True, exist_ok=True)
    snapshot = Path(tempfile.mkdtemp(prefix="deferred-" if deferred else "failed-", dir=destination))
    files = {}
    for directory in ("artifacts", "tmp"):
        for source in (repo / directory).rglob("*"):
            if source.is_symlink() or not source.is_file():
                continue
            relative = source.relative_to(repo)
            if any((repo / Path(*relative.parts[:i])).is_symlink() for i in range(1, len(relative.parts))):
                continue
            target = snapshot / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
            files[str(relative)] = hashlib.sha256(target.read_bytes()).hexdigest()
    if result.is_file() and not result.is_symlink():
        shutil.copyfile(result, snapshot / "agent-result.json")
        files["agent-result.json"] = hashlib.sha256((snapshot / "agent-result.json").read_bytes()).hexdigest()
    (snapshot / "manifest.json").write_text(json.dumps({"files": files}, indent=2) + "\n")
    return snapshot


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("repo")
    parser.add_argument("state")
    parser.add_argument("result")
    parser.add_argument("--deferred", action="store_true")
    args = parser.parse_args()
    label = "Deferred-work" if args.deferred else "Failed-run"
    print(label + " recovery retained at " + str(capture(args.repo, args.state, args.result, deferred=args.deferred)))
