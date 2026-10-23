#!/usr/bin/env python3
"""Require the Claude runtime qualified for the S1 hook/transcript adapter."""
import json
import os
import re
import subprocess
import sys

QUALIFIED_VERSION = "2.1.296"


def check(version):
    match = re.match(r"^(\d+\.\d+\.\d+)\b", version.strip())
    if not match or match[1] != QUALIFIED_VERSION:
        raise ValueError(f"S1 requires qualified Claude Code {QUALIFIED_VERSION}; got {version.strip()!r}")
    return match[1]


def runtime_version():
    version = subprocess.run([os.environ.get("CLAUDE_BIN", "claude"), "--version"],
                             capture_output=True, text=True, check=True, timeout=5)
    return check(version.stdout)


def main():
    try:
        print(json.dumps({"qualified_claude_version": runtime_version()}))
    except (ValueError, OSError, subprocess.SubprocessError) as error:
        print(str(error), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
