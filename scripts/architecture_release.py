#!/usr/bin/env python3
"""Select a numbered RHOAI release using numeric directory-name ordering."""

import argparse
import json
import re
import sys
from pathlib import Path

RELEASE = re.compile(r"rhoai-[0-9]+(?:\.[0-9]+)*(?:-[A-Za-z0-9._-]+)?")


def latest_release(names):
    candidates = [name for name in names if isinstance(name, str) and RELEASE.fullmatch(name)]
    if not candidates:
        raise ValueError("no numbered RHOAI release directories found")

    def key(name):
        # Keep the existing suffix convention; compare every number as a number.
        return tuple((1, int(part)) if part.isdigit() else (0, part)
                     for part in re.split(r"([0-9]+)", name) if part)

    return max(candidates, key=key)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--directory", type=Path)
    source.add_argument("--github-json", action="store_true")
    args = parser.parse_args()
    if args.directory:
        names = [path.name for path in args.directory.iterdir() if path.is_dir() and not path.is_symlink()]
    else:
        entries = json.load(sys.stdin)
        if not isinstance(entries, list):
            raise ValueError("expected a GitHub directory listing")
        names = [entry.get("name") for entry in entries if isinstance(entry, dict) and entry.get("type") == "dir"]
    print(latest_release(names))


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError) as error:
        raise SystemExit(f"Architecture release selection failed: {error}")
