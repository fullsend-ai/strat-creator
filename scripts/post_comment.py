#!/usr/bin/env python3
"""Post a markdown comment to a Jira issue.

Reads markdown from a file, converts it to ADF, and posts it as a comment.

Usage:
    python3 scripts/post_comment.py RHAISTRAT-2283 --body-file tmp/comment.md

Environment variables: JIRA_SERVER, JIRA_USER, JIRA_TOKEN
"""

import argparse
import sys
from pathlib import Path

from jira_utils import add_comment, markdown_to_adf, require_env


def main():
    parser = argparse.ArgumentParser(description="Post markdown comment to Jira")
    parser.add_argument("key", help="Issue key (e.g. RHAISTRAT-2283)")
    parser.add_argument("--body-file", required=True,
                        help="Path to markdown file with comment body")
    args = parser.parse_args()

    body = Path(args.body_file).read_text().strip()
    if not body:
        print("Error: comment body is empty", file=sys.stderr)
        sys.exit(1)

    server, user, token = require_env()
    adf = markdown_to_adf(body)
    add_comment(server, user, token, args.key, adf)
    print(f"Comment posted to {args.key}")


if __name__ == "__main__":
    main()
