#!/usr/bin/env python3
"""Exact-match pipeline label gate for one RHAISTRAT issue.

The strategy skills stop when a STRAT already carries a pipeline verdict
label. This helper makes that check deterministic: labels are compared by
exact string equality against PIPELINE_GATE_LABELS, so lookalike labels
inherited from the source RFE at clone time (for example
rfe-creator-needs-attention or rfe-creator-autofix-rubric-pass) can never
satisfy the gate.

Usage:
    python3 scripts/strat_label_gate.py RHAISTRAT-NNNN
    python3 scripts/strat_label_gate.py RHAISTRAT-NNNN --labels a,b,c

--labels evaluates labels already read by other means (the Atlassian MCP
fallback) instead of fetching them from Jira.

Output (stdout, JSON):
    {"key": "RHAISTRAT-NNNN", "eligible": true, "gate_label": null,
     "gate_labels_present": []}

Exit codes:
    0  Gate evaluated; act on "eligible" in the JSON
    1  API/network/script error
    2  Missing Jira credentials (caller may read labels via MCP and pass --labels)
"""

import argparse
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from jira_utils import get_issue, require_env  # noqa: E402

# Order matters only for gate_label: it reports the first present label.
PIPELINE_GATE_LABELS = (
    "strat-creator-rubric-pass",
    "strat-creator-needs-attention",
)

STRAT_KEY_RE = re.compile(r"^RHAISTRAT-[1-9][0-9]*$")


def pipeline_label_gate(labels):
    """Return the exact gate-label state for a STRAT label list.

    A missing or malformed label list is an error, not an empty label set.
    """
    if not isinstance(labels, list) or any(not isinstance(label, str) for label in labels):
        raise ValueError("labels must be a list of strings")
    present = [gate for gate in PIPELINE_GATE_LABELS if gate in labels]
    return {
        "eligible": not present,
        "gate_label": present[0] if present else None,
        "gate_labels_present": present,
    }


def fetch_labels(server, user, token, key):
    data = get_issue(server, user, token, key, fields=["labels"])
    return data.get("fields", {}).get("labels")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("key", help="RHAISTRAT issue key")
    parser.add_argument("--labels",
                        help="comma-separated labels to evaluate instead of fetching from Jira")
    args = parser.parse_args(argv)

    if not STRAT_KEY_RE.match(args.key):
        print(f"Error: not a RHAISTRAT key: {args.key!r}", file=sys.stderr)
        return 1

    try:
        if args.labels is not None:
            labels = [label.strip() for label in args.labels.split(",") if label.strip()]
        else:
            credentials = require_env()
            if not all(credentials):
                print("Error: JIRA_SERVER, JIRA_USER, and JIRA_TOKEN required.",
                      file=sys.stderr)
                return 2
            labels = fetch_labels(*credentials, args.key)
        gate = pipeline_label_gate(labels)
    except Exception as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1

    print(json.dumps({"key": args.key, **gate}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
