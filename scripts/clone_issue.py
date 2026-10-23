#!/usr/bin/env python3
"""Clone a Jira issue into a target project via REST API.

Replicates Jira's UI clone: creates a new issue with the same summary,
description, and priority, then links the two with a Cloners link.

Usage:
    python3 scripts/clone_issue.py RHAIRFE-1397 --target-project RHAISTRAT --issue-type Feature

Output (stdout):
    The new issue key (e.g. RHAISTRAT-1500)

Environment variables:
    JIRA_SERVER  Jira server URL
    JIRA_USER    Jira username/email
    JIRA_TOKEN   Jira API token
"""

import argparse
import sys

from jira_utils import (
    create_clone,
    create_issue_link,
    get_issue,
    get_project_versions,
    require_env,
)
from strat_side_effects import OWNER_PREFIX


def _resolve_parent_outcome(server, user, token, source_fields):
    """Return the source RFE's parent key if it's a valid, non-Closed Outcome."""
    parent = source_fields.get("parent")
    if not parent or not isinstance(parent, dict):
        print("No parent Outcome found on source RFE.", file=sys.stderr)
        return None

    parent_key = parent.get("key")
    if not isinstance(parent_key, str):
        print("Source parent key is missing or invalid, skipping.",
              file=sys.stderr)
        return None
    if not parent_key.startswith("RHAISTRAT-"):
        print(f"Source parent {parent_key} is not a RHAISTRAT Outcome, skipping.",
              file=sys.stderr)
        return None

    try:
        parent_issue = get_issue(server, user, token, parent_key,
                                 fields=["status", "issuetype"])
    except Exception as exc:
        print(f"Could not fetch parent Outcome {parent_key}: {exc}. Skipping.",
              file=sys.stderr)
        return None

    parent_fields = parent_issue.get("fields", {})
    issue_type = parent_fields.get("issuetype", {}).get("name", "")
    if issue_type != "Outcome":
        print(f"Source parent {parent_key} is type '{issue_type}', not Outcome. Skipping.",
              file=sys.stderr)
        return None

    status = parent_fields.get("status", {}).get("name", "")
    if status == "Closed":
        print(f"Source parent {parent_key} is Closed, skipping.",
              file=sys.stderr)
        return None

    print(f"Setting parent Outcome: {parent_key}", file=sys.stderr)
    return parent_key


def main():
    parser = argparse.ArgumentParser(description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("source_key", help="Source issue key (e.g. RHAIRFE-1397)")
    parser.add_argument("--target-project", required=True,
                        help="Target project key (e.g. RHAISTRAT)")
    parser.add_argument("--issue-type", default="Feature",
                        help="Issue type in target project (default: Feature)")
    args = parser.parse_args()

    server, user, token = require_env()
    if not all([server, user, token]):
        print("Error: JIRA_SERVER, JIRA_USER, and JIRA_TOKEN required.",
              file=sys.stderr)
        sys.exit(2)

    source = get_issue(server, user, token, args.source_key,
                       fields=["summary", "description", "priority", "labels",
                               "components", "versions",
                               "customfield_10855", "parent"])
    fields = source.get("fields", {})

    summary = fields.get("summary", "")
    if not summary.startswith("[DRAFT] "):
        summary = f"[DRAFT] {summary}"
    description_adf = fields.get("description")
    priority_obj = fields.get("priority")
    priority = priority_obj.get("name", "Major") if isinstance(
        priority_obj, dict) else "Major"
    labels = [label for label in fields.get("labels", [])
              if label != "strat-creator-processing" and not label.startswith(OWNER_PREFIX)]
    components = [c["name"] for c in fields.get("components", [])
                  if isinstance(c, dict) and "name" in c]
    affects_versions = [v["name"] for v in fields.get("versions", [])
                        if isinstance(v, dict) and "name" in v]
    source_tv = [v for v in (fields.get("customfield_10855") or [])
                 if isinstance(v, dict) and "name" in v]
    target_versions = []
    if source_tv:
        tp_versions = get_project_versions(server, user, token,
                                           args.target_project)
        tv_by_name = {v["name"]: v["id"] for v in tp_versions}
        for v in source_tv:
            tid = tv_by_name.get(v["name"])
            if tid:
                target_versions.append({"id": tid})
            else:
                print(f"Target Version '{v['name']}' not found in "
                      f"{args.target_project}, omitting.", file=sys.stderr)

    parent_key = _resolve_parent_outcome(server, user, token, fields)

    new_key = create_clone(
        server, user, token,
        source_key=args.source_key,
        project=args.target_project,
        issue_type=args.issue_type,
        summary=summary,
        description_adf=description_adf,
        priority=priority,
        labels=labels,
        components=components,
        affects_versions=affects_versions,
        target_versions=target_versions,
        parent_key=parent_key,
    )

    # Cloners link: new issue "is cloned by" source (STRAT is the clone of the RFE)
    create_issue_link(server, user, token,
                      type_name="Cloners",
                      inward_key=new_key,
                      outward_key=args.source_key)

    print(new_key)


if __name__ == "__main__":
    main()
