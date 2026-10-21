#!/usr/bin/env python3
"""Find child epics of a parent issue in RHAISTRAT.

Searches for issues whose parent is the given key and whose issue type
is Epic. Outputs a JSON array of matching keys.

Usage:
    python3 scripts/find_child_epics.py RHAISTRAT-2283

Environment variables: JIRA_SERVER, JIRA_USER, JIRA_TOKEN
"""

import json
import sys

from jira_utils import require_env, search_issues


def main():
    if len(sys.argv) != 2:
        print("Usage: find_child_epics.py PARENT_KEY", file=sys.stderr)
        sys.exit(1)

    parent_key = sys.argv[1]
    server, user, token = require_env()

    jql = f"parent = {parent_key} AND project = RHAISTRAT"
    issues = search_issues(server, user, token, jql,
                           fields=["key", "issuetype"])
    epics = [
        issue["key"] for issue in issues
        if issue.get("fields", {}).get("issuetype", {}).get("name") == "Epic"
    ]
    print(json.dumps(epics))


if __name__ == "__main__":
    main()
