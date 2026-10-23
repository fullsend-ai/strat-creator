#!/usr/bin/env python3
"""Redact known CI secret values from live output and retained text artifacts."""

import argparse
import base64
import json
import os
import re
import sys
from pathlib import Path


def secrets(env):
    values = set()
    for name, value in env.items():
        if not value or not re.search(r"TOKEN|PASSWORD|SECRET|CREDENTIALS|API_KEY|SERVICE_ACCOUNT_KEY", name):
            continue
        if len(value) >= 8:
            values.add(value)
        if name in {"GOOGLE_APPLICATION_CREDENTIALS", "AIPCC_CICD_GCP_SERVICE_ACCOUNT_KEY"}:
            try:
                contents = Path(value).read_text()
                data = json.loads(contents)
            except (OSError, ValueError):
                continue
            values.add(contents)
            private_key = data.get("private_key", "")
            values.update(line for line in private_key.splitlines() if len(line) >= 8)
            if private_key:
                values.add(private_key)
                values.add(json.dumps(private_key)[1:-1])
    for user, token in (
        (env.get("JIRA_USER", ""), env.get("JIRA_TOKEN", "")),
        (env.get("RESULTS_GIT_USER", "oauth2"), env.get("RESULTS_PUSH_TOKEN", "")),
    ):
        if user and token:
            values.add(base64.b64encode(f"{user}:{token}".encode()).decode())
    return sorted(values, key=len, reverse=True)


def redact(text, values):
    for value in values:
        text = text.replace(value, "[REDACTED]")
    # ADC can mint an access token that was never an inherited CI variable.
    text = re.sub(r"(?i)\b(Bearer|Basic)\s+[A-Za-z0-9._~+/=-]{16,}", r"\1 [REDACTED]", text)
    text = re.sub(r'(?i)("(?:access_token|refresh_token)"\s*:\s*")[^"]+', r"\1[REDACTED]", text)
    return text


def scrub_tree(directory, values):
    paths = list(directory.rglob("*"))
    # Check names before retaining anything, including empty directories and
    # links. Redacting bodies alone leaves secrets in archive entry names.
    for path in paths:
        name = str(path.relative_to(directory))
        if redact(name, values) != name:
            raise ValueError("cannot safely scrub artifact path containing credentials")
    for path in paths:
        if path.is_symlink():
            # Never follow a generated link outside the artifact boundary.
            path.unlink()
        elif path.is_file():
            data = path.read_bytes()
            try:
                text = data.decode("utf-8")
            except UnicodeDecodeError as error:
                raise ValueError("cannot safely scrub binary artifact") from error
            updated = redact(text, values)
            if updated != text:
                path.write_text(updated)


def require_clean(directory, values):
    """Reject unsafe publication without changing validated or sealed bytes."""
    if directory.is_symlink() or not directory.exists():
        raise ValueError("publication input is missing or is a symlink")
    paths = directory.rglob("*") if directory.is_dir() else [directory]
    for path in paths:
        if path.is_symlink():
            raise ValueError("publication input contains a symlink")
        if path.is_dir():
            continue
        if not path.is_file():
            raise ValueError("publication input contains a non-regular file")
        try:
            text = path.read_bytes().decode("utf-8")
        except UnicodeDecodeError as error:
            raise ValueError("publication input contains a binary artifact") from error
        name = str(path.relative_to(directory)) if directory.is_dir() else path.name
        if redact(text, values) != text or redact(name, values) != name:
            raise ValueError("publication input contains credentials or authorization material")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=["stream", "tree", "check"])
    parser.add_argument("directory", nargs="?", type=Path)
    args = parser.parse_args()
    values = secrets(os.environ)
    if args.operation == "stream":
        for line in sys.stdin:
            print(redact(line, values), end="", flush=True)
    else:
        if args.directory is None:
            parser.error("tree/check requires a path")
        try:
            if args.operation == "check":
                require_clean(args.directory, values)
            else:
                scrub_tree(args.directory, values)
        except ValueError as error:
            parser.exit(1, f"ERROR: {error}\n")


if __name__ == "__main__":
    main()
