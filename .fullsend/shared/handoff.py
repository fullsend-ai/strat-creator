#!/usr/bin/env python3
"""Copy a validated run's artifacts out of the downloaded repository.

The downloaded repository is agent-writable, so only what the validated
agent-result.json lists is copied: its `artifacts` entries (each already
checked by validate_result.py to be a regular file under artifacts/), plus the
skip and ticket tables. Anything else the agent left there stays behind, and
a symlink or a non-regular file is never copied. With a fourth argument,
tmp/strat-progress.json is copied there too, so a later strat-resume run can
read it (STRAT_PREVIOUS_PROGRESS).

Usage: handoff.py <downloaded-repo> <destination> <agent-result.json> [<progress-destination>]
"""
import json
import re
import shutil
import sys
from pathlib import Path

EXTRA = ("artifacts/strat-skipped.md", "artifacts/strat-tickets.md")
ARTIFACT_RE = re.compile(r"^artifacts/(strat-tasks|strat-reviews|strat-originals)/[A-Za-z0-9._-]+$")


def _copy(source, destination, root=None):
    """Copy a regular file; refuse symlinks on the path below root (the repository)."""
    if source.is_symlink() or not source.is_file():
        return 0
    if root is not None:
        rel = source.relative_to(root)
        if any((root / Path(*rel.parts[:i])).is_symlink() for i in range(1, len(rel.parts))):
            return 0
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, destination)
    return 1


def allowed(rel):
    """The artifact paths a run may hand off; validate_result.py applies the same rule."""
    return isinstance(rel, str) and (bool(ARTIFACT_RE.match(rel)) or rel in EXTRA)


def allowlist(result_path):
    listed = json.loads(Path(result_path).read_text()).get("artifacts", [])
    return sorted({rel for rel in listed if isinstance(rel, str) and ARTIFACT_RE.match(rel)} | set(EXTRA))


def handoff(repo, destination, result_path):
    repo, destination = Path(repo), Path(destination)
    if destination.exists():
        shutil.rmtree(destination)
    destination.mkdir(parents=True)
    copied = 0
    for rel in allowlist(result_path):
        done = _copy(repo / rel, destination / rel.removeprefix("artifacts/"), root=repo)
        if not done and rel not in EXTRA:
            raise SystemExit(f"handoff: ERROR: validated artifact {rel} could not be copied")
        copied += done
    return copied


if __name__ == "__main__":
    if len(sys.argv) not in (4, 5):
        sys.exit("usage: handoff.py <downloaded-repo> <destination> <agent-result.json> [<progress-destination>]")
    count = handoff(sys.argv[1], sys.argv[2], sys.argv[3])
    print(f"handoff: copied {count} artifact file(s) to {sys.argv[2]}")
    repo = Path(sys.argv[1])
    if len(sys.argv) == 5 and _copy(repo / "tmp" / "strat-progress.json", Path(sys.argv[4]), root=repo):
        print(f"handoff: progress record copied to {sys.argv[4]}")
