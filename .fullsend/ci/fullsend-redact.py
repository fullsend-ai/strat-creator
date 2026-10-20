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
    for path in directory.rglob("*"):
        if path.is_symlink():
            # Never follow a generated link outside the artifact boundary.
            path.unlink()
        elif path.is_file():
            data = path.read_bytes()
            try:
                text = data.decode("utf-8")
            except UnicodeDecodeError as error:
                raise ValueError(f"cannot safely scrub binary artifact {path.name}") from error
            updated = redact(text, values)
            if updated != text:
                path.write_text(updated)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=["stream", "tree"])
    parser.add_argument("directory", nargs="?", type=Path)
    args = parser.parse_args()
    values = secrets(os.environ)
    if args.operation == "stream":
        for line in sys.stdin:
            print(redact(line, values), end="", flush=True)
    else:
        if args.directory is None:
            parser.error("tree requires a directory")
        scrub_tree(args.directory, values)


if __name__ == "__main__":
    main()
