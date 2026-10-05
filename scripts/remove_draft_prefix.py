#!/usr/bin/env python3
"""Remove the [DRAFT] prefix from a Jira issue's summary.

Idempotent: exits cleanly if the summary has no [DRAFT] prefix.

Usage:
    python3 scripts/remove_draft_prefix.py RHAISTRAT-1500

Environment variables:
    JIRA_SERVER  Jira server URL
    JIRA_USER    Jira username/email
    JIRA_TOKEN   Jira API token
"""

import argparse
import sys

import type_registry
from jira_utils import get_issue, require_env, update_summary

# conventions.summary_prefix of the default work type: added at create, removed here at signoff.
# For now: one type's prefix; resolve it from the issue key once a type with its own prefix lands.
# A key whose type has no prefix (initiative-strategy) is skipped, so its summary is never touched.
_TYPES = type_registry.load()
DRAFT_PREFIX = _TYPES.get(type_registry.LEGACY_DEFAULT_TYPE).get("conventions.summary_prefix.value")


def main():
    parser = argparse.ArgumentParser(description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("issue_key", help="Jira issue key (e.g. RHAISTRAT-1500)")
    args = parser.parse_args()

    owner = _TYPES.detect(args.issue_key)
    if owner and owner.get("conventions.summary_prefix", None) is None:
        print(f"[SKIP] {owner.name} has no summary prefix -- {args.issue_key} summary unchanged")
        return

    server, user, token = require_env()
    if not all([server, user, token]):
        print("Error: JIRA_SERVER, JIRA_USER, and JIRA_TOKEN required.",
              file=sys.stderr)
        sys.exit(2)

    issue = get_issue(server, user, token, args.issue_key, fields=["summary"])
    summary = issue["fields"]["summary"]

    if not summary.startswith(DRAFT_PREFIX):
        print(f"[SKIP] No {DRAFT_PREFIX.strip()} prefix on {args.issue_key} -- summary unchanged")
        return

    update_summary(server, user, token, args.issue_key,
                   summary[len(DRAFT_PREFIX):])
    print(f"[SUMMARY] Removed {DRAFT_PREFIX.strip()} prefix from {args.issue_key}")


if __name__ == "__main__":
    main()
