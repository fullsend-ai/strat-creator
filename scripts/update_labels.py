#!/usr/bin/env python3
"""Add and/or remove labels on a Jira issue.

Both --add and --remove are idempotent: adding a label that already exists
or removing one that is absent is a no-op, not an error.

Usage:
    python3 scripts/update_labels.py RHAIRFE-1234 --remove strat-creator-consumed --add strat-creator-rework-needed
    python3 scripts/update_labels.py RHAIRFE-1234 --add label1,label2
    python3 scripts/update_labels.py RHAIRFE-1234 --remove label1

Environment variables: JIRA_SERVER, JIRA_USER, JIRA_TOKEN
"""

import argparse
import sys

from jira_utils import add_labels, remove_labels, require_env


def _parse_labels(value):
    if not value:
        return []
    return [part.strip() for part in value.split(",") if part.strip()]


def main():
    parser = argparse.ArgumentParser(description="Add/remove Jira labels")
    parser.add_argument("key", help="Issue key (e.g. RHAIRFE-1234)")
    parser.add_argument("--add", default="",
                        help="Comma-separated labels to add")
    parser.add_argument("--remove", default="",
                        help="Comma-separated labels to remove")
    args = parser.parse_args()

    to_add = _parse_labels(args.add)
    to_remove = _parse_labels(args.remove)

    if not to_add and not to_remove:
        print("Error: specify --add and/or --remove", file=sys.stderr)
        sys.exit(1)

    server, user, token = require_env()

    if to_remove:
        remove_labels(server, user, token, args.key, to_remove)
        print(f"Removed labels from {args.key}: {to_remove}")

    if to_add:
        add_labels(server, user, token, args.key, to_add)
        print(f"Added labels to {args.key}: {to_add}")


if __name__ == "__main__":
    main()
