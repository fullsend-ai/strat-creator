#!/usr/bin/env python3
"""Find RHAISTRAT clones for an RHAIRFE issue.

Deterministic lookup — fetches the RFE's issuelinks from Jira and
returns only Cloners links to RHAISTRAT issues, with their status
and labels.

Usage:
    python3 scripts/find_strat_for_rfe.py RHAIRFE-442
    python3 scripts/find_strat_for_rfe.py RHAIRFE-442 --json

Output (text mode):
    RHAISTRAT-1284 status=New labels=strat-creator-rubric-pass,...
    RHAISTRAT-1578 status=New labels=strat-creator-rubric-pass,...

Output (json mode):
    [{"key": "RHAISTRAT-1284", "status": "New", "labels": [...],
      "gate_label": "strat-creator-rubric-pass",
      "gate_labels_present": ["strat-creator-rubric-pass"]}]

    gate_label/gate_labels_present come from strat_label_gate.py's exact
    match; gate_label is null when no pipeline gate label is present.

Exit codes:
    0 — found one or more STRAT clones
    1 — no STRAT clones found
    2 — error (missing env vars, API failure)
"""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

from jira_utils import get_issue, require_env
from strat_label_gate import pipeline_label_gate


def find_strat_clones(server, user, token, rfe_key):
    """Find all RHAISTRAT issues linked via Cloners to an RHAIRFE."""
    data = get_issue(server, user, token, rfe_key, fields=["issuelinks"])
    links = data.get("fields", {}).get("issuelinks", [])

    strat_keys = []
    for link in links:
        if link.get("type", {}).get("name") != "Cloners":
            continue
        for direction in ("outwardIssue", "inwardIssue"):
            issue = link.get(direction, {})
            key = issue.get("key", "")
            if key.startswith("RHAISTRAT-"):
                strat_keys.append(key)

    if not strat_keys:
        return []

    results = []
    for key in strat_keys:
        strat_data = get_issue(server, user, token, key,
                               fields=["status", "labels"])
        fields = strat_data.get("fields", {})
        status = fields.get("status", {}).get("name", "Unknown")
        labels = fields.get("labels", [])
        gate = pipeline_label_gate(labels)
        results.append({"key": key, "status": status, "labels": labels,
                        "gate_label": gate["gate_label"],
                        "gate_labels_present": gate["gate_labels_present"]})

    return results


def main():
    parser = argparse.ArgumentParser(
        description="Find RHAISTRAT clones for an RHAIRFE")
    parser.add_argument("rfe_key", help="RHAIRFE issue key")
    parser.add_argument("--json", action="store_true",
                        help="Output as JSON")
    args = parser.parse_args()

    server, user, token = require_env()
    if not all([server, user, token]):
        print("Error: JIRA_SERVER, JIRA_USER, JIRA_TOKEN required.",
              file=sys.stderr)
        return 2

    results = find_strat_clones(server, user, token, args.rfe_key)

    if not results:
        if args.json:
            print("[]")
        else:
            print("none")
        return 1

    if args.json:
        print(json.dumps(results, indent=2))
    else:
        for r in results:
            labels_str = ",".join(r["labels"]) if r["labels"] else "(none)"
            print(f'{r["key"]} status={r["status"]} labels={labels_str}')

    return 0


if __name__ == "__main__":
    sys.exit(main())
