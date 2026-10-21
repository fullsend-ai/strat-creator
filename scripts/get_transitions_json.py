#!/usr/bin/env python3
"""List available transitions for a Jira issue as JSON.

Usage:
    python3 scripts/get_transitions_json.py RHAISTRAT-2283

Environment variables: JIRA_SERVER, JIRA_USER, JIRA_TOKEN
"""

import json
import sys

from jira_utils import get_transitions, require_env


def main():
    if len(sys.argv) != 2:
        print("Usage: get_transitions_json.py ISSUE_KEY", file=sys.stderr)
        sys.exit(1)

    server, user, token = require_env()
    transitions = get_transitions(server, user, token, sys.argv[1])
    print(json.dumps(transitions, indent=2))


if __name__ == "__main__":
    main()
