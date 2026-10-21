#!/usr/bin/env python3
"""Execute a Jira workflow transition with optional resolution.

Usage:
    python3 scripts/do_transition.py RHAISTRAT-2283 31 --resolution "Won't Do"
    python3 scripts/do_transition.py RHAISTRAT-2283 31

Environment variables: JIRA_SERVER, JIRA_USER, JIRA_TOKEN
"""

import argparse

from jira_utils import do_transition, require_env


def main():
    parser = argparse.ArgumentParser(description="Execute Jira transition")
    parser.add_argument("key", help="Issue key (e.g. RHAISTRAT-2283)")
    parser.add_argument("transition_id", help="Transition ID")
    parser.add_argument("--resolution", help="Resolution name (e.g. \"Won't Do\")")
    args = parser.parse_args()

    server, user, token = require_env()
    fields = {}
    if args.resolution:
        fields["resolution"] = {"name": args.resolution}

    do_transition(server, user, token, args.key, args.transition_id,
                  fields=fields if fields else None)
    print(f"Transition {args.transition_id} applied to {args.key}")


if __name__ == "__main__":
    main()
