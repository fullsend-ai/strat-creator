#!/usr/bin/env python3
"""Vendor strat-creator from a pinned host clone into the target repository.

fullsend cannot pin a whole definition repository yet (see .fullsend/README.md,
"Platform dependency"), so the host pre-script clones strat-creator at
STRAT_CREATOR_REF and this script copies what the sandbox needs into the
target repository before fullsend uploads it:

  .claude/skills/   every skill; the skills' scripts symlinks are dereferenced
  .claude/agents/strat-scorer.md
  scripts/  config/  workflows/

Only regular files are copied; a symlink is followed only when it resolves
inside the clone. A marker file, .strat-creator-vendor.json, lists what was
written, so a rerun replaces exactly those files and nothing the consumer owns
is overwritten: a path that exists and is not in the marker stops the run.
When the target is a strat-creator checkout itself, nothing is copied.

Usage: vendor.py <clone> <target>
"""

import json
import os
import shutil
import sys
from pathlib import Path

TREES = (".claude/skills", "scripts", "config", "workflows")
FILES = (".claude/agents/strat-scorer.md",)
MARKER = ".strat-creator-vendor.json"
SKIP_PARTS = {"__pycache__", ".pytest_cache"}


def is_strat_creator(root):
    manifest = root / ".claude-plugin" / "plugin.json"
    try:
        return json.loads(manifest.read_text()).get("name") == "strat-creator"
    except (OSError, ValueError):
        return False


def inside(path, root):
    resolved = path.resolve()
    return resolved == root or root in resolved.parents


def walk(clone, rel):
    """Regular files under clone/rel, as logical paths relative to clone.

    Directory symlinks are followed (a skill's scripts -> ../../../scripts),
    but only while they resolve inside the clone.
    """
    top = clone / rel
    if top.is_file():
        if inside(top, clone):
            yield Path(rel)
        return
    for dirpath, dirnames, filenames in os.walk(top, followlinks=True):
        here = Path(dirpath)
        if not inside(here, clone):
            dirnames[:] = []
            continue
        dirnames[:] = sorted(n for n in dirnames if n not in SKIP_PARTS)
        for name in sorted(filenames):
            path = here / name
            if path.is_file() and inside(path, clone):
                yield path.relative_to(clone)


def contained(target, rel):
    """target/rel if rel is a plain relative path that stays inside target, else None.

    The marker comes from the target repository, which a pull request can
    change, so its entries are untrusted: an absolute path, a .. segment, a
    symlinked ancestor or a resolved path outside the target is refused.
    """
    if not isinstance(rel, str) or not rel or rel.startswith("/") or "\\" in rel:
        return None
    parts = Path(rel).parts
    if ".." in parts or "." in parts:
        return None
    path = target / rel
    for ancestor in list(path.parents)[:len(parts) - 1]:
        if ancestor.is_symlink():
            return None
    if not inside(path.parent, target):
        return None
    return path


def vendor(clone, target, ref):
    clone, target = clone.resolve(), target.resolve()
    if is_strat_creator(target):
        print(f"vendor: {target} is a strat-creator checkout; nothing to copy")
        return 0
    marker = target / MARKER
    previous = set()
    if marker.is_symlink():
        print(f"vendor: ERROR: {marker} is a symlink", file=sys.stderr)
        return 1
    if marker.exists():
        listed = json.loads(marker.read_text()).get("files", [])
        bad = [rel for rel in listed if contained(target, rel) is None]
        if bad:
            print(f"vendor: ERROR: {marker} lists paths outside the target, e.g. {bad[:3]}", file=sys.stderr)
            return 1
        previous = set(listed)
    files = sorted({str(p) for rel in TREES + FILES for p in walk(clone, rel)})
    clashes = [f for f in files if (target / f).exists() and f not in previous]
    if clashes:
        print(f"vendor: ERROR: {len(clashes)} path(s) already exist in the target and were not "
              f"vendored by strat-creator, e.g. {clashes[:3]}; refusing to overwrite them",
              file=sys.stderr)
        return 1
    unsafe = [rel for rel in files if contained(target, rel) is None]
    if unsafe:
        print(f"vendor: ERROR: {len(unsafe)} destination(s) go through a symlink, e.g. {unsafe[:3]}",
              file=sys.stderr)
        return 1
    for stale in sorted(previous - set(files)):
        path = contained(target, stale)
        if path is not None and path.is_file() and not path.is_symlink():
            path.unlink()
    for rel in files:
        destination = contained(target, rel)
        if destination.is_symlink():
            destination.unlink()
        destination.parent.mkdir(parents=True, exist_ok=True)
        source = (clone / rel).resolve()
        shutil.copyfile(source, destination)
        shutil.copymode(source, destination)
    marker.write_text(json.dumps({"ref": ref, "files": files}, indent=2) + "\n")
    print(f"vendor: copied {len(files)} file(s) from strat-creator {ref[:12]} into {target}")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 4:
        sys.exit("usage: vendor.py <clone> <target> <ref>")
    sys.exit(vendor(Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]))
