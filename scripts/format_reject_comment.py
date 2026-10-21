#!/usr/bin/env python3
"""Format a strategy-rejection comment for a STRAT or RFE issue.

Reads the rejection reason from a file and outputs the formatted markdown
comment to stdout. Pipe the output to a file for use with post_comment.py.

Usage:
    python3 scripts/format_reject_comment.py strat \\
        --reason-file tmp/reason.md --strat-key RHAISTRAT-2283 \\
        --rfe-key RHAIRFE-1234 > tmp/strat-comment.md
    python3 scripts/format_reject_comment.py rfe \\
        --reason-file tmp/reason.md --strat-key RHAISTRAT-2283 \\
        --rfe-key RHAIRFE-1234 > tmp/rfe-comment.md
"""

import argparse
import sys
from pathlib import Path

STRAT_TEMPLATE = """\
### Strategy rejected

{reason}

This strategy has been closed. The source RFE ({rfe_key}) has been returned for rework.
"""

RFE_TEMPLATE = """\
### Strategy rejected -- rework needed

The strategy {strat_key} has been rejected for the following reason:

{reason}

Please review and update this RFE. When ready, remove the
strat-creator-rework-needed label to allow a new strategy to be created.
"""


def main():
    parser = argparse.ArgumentParser(
        description="Format rejection comment for STRAT or RFE")
    parser.add_argument("type", choices=["strat", "rfe"],
                        help="Comment type: strat or rfe")
    parser.add_argument("--reason-file", required=True,
                        help="Path to file containing rejection reason")
    parser.add_argument("--strat-key", required=True,
                        help="RHAISTRAT issue key")
    parser.add_argument("--rfe-key", required=True,
                        help="RHAIRFE issue key")
    args = parser.parse_args()

    reason = Path(args.reason_file).read_text().strip()
    if not reason:
        print("Error: reason file is empty", file=sys.stderr)
        sys.exit(1)

    if args.type == "strat":
        template = STRAT_TEMPLATE
    else:
        template = RFE_TEMPLATE

    print(template.format(
        reason=reason,
        strat_key=args.strat_key,
        rfe_key=args.rfe_key,
    ))


if __name__ == "__main__":
    main()
