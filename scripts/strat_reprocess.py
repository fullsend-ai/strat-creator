#!/usr/bin/env python3
"""Verify the exact STRAT selected for a deliberate CI reprocessing request."""

import argparse
import json
import re

from find_strat_for_rfe import find_strat_clones
from jira_utils import get_issue, require_env

ACTIVE_STATUSES = {"Closed", "Resolved", "In Progress", "Review", "Release Pending"}
HUMAN_GATES = {"strat-creator-needs-attention", "strat-creator-human-sign-off"}


def inspect_strategy(key):
    """Read only. Reprocessing never adopts ambiguous or human-owned work."""
    if not re.fullmatch(r"RHAISTRAT-[1-9][0-9]*", key):
        raise ValueError("reprocessing requires a RHAISTRAT key")
    credentials = require_env()
    fields = get_issue(*credentials, key, fields=["labels", "status", "issuelinks"])["fields"]
    labels = set(fields.get("labels") or [])
    if "strat-creator-auto-created" not in labels:
        raise ValueError("reprocessing requires strat-creator-auto-created")
    if labels & HUMAN_GATES or (fields.get("status") or {}).get("name") in ACTIVE_STATUSES:
        raise ValueError("reprocessing cannot override human gates or active/completed STRAT states")
    rfes = {link.get(side, {}).get("key", "") for link in fields.get("issuelinks", [])
            if link.get("type", {}).get("name") == "Cloners" for side in ("inwardIssue", "outwardIssue")
            if re.fullmatch(r"RHAIRFE-[1-9][0-9]*", link.get(side, {}).get("key", ""))}
    if len(rfes) != 1:
        raise ValueError("reprocessing requires exactly one source RFE Cloners link")
    rfe = rfes.pop()
    open_clones = {clone["key"] for clone in find_strat_clones(*credentials, rfe)
                   if clone["status"] not in ACTIVE_STATUSES}
    if open_clones != {key}:
        raise ValueError("reprocessing requires the selected STRAT to be the unique open clone")
    return rfe


def selected_target(key, data):
    if data.get("mode") != "resume" or data.get("request", {}).get("STRAT_REPROCESS_KEY") != key:
        raise ValueError("no explicit prepared reprocess request for this STRAT")
    targets = data.get("reprocess", {})
    if not isinstance(targets, dict) or len(targets) != 1:
        raise ValueError("reprocessing requires one exact prepared target")
    rfe, target = next(iter(targets.items()))
    if target != key or data.get("acquired_keys") != [rfe]:
        raise ValueError("reprocess target is not acquired by this prepared run")
    return rfe


def authorized(key, data):
    rfe = selected_target(key, data)
    if inspect_strategy(key) != rfe:
        raise ValueError("reprocess source link changed")
    if not data.get("dry_run"):
        from strat_side_effects import check_owner
        check_owner(*require_env(), rfe, data)
    return rfe


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("key")
    parser.add_argument("--prepare", action="store_true", help="Import and record the selected existing STRAT")
    args = parser.parse_args()
    from strat_pipeline_state import cmd_record_create, load_input, load_progress
    data = load_input()
    rfe = selected_target(args.key, data)
    if args.prepare:
        progress = load_progress(data["run_id"])
        created = progress.get("create") or {}
        expected = [{"rfe": rfe, "strat": key} for rfe, key in data.get("reprocess", {}).items()]
        if expected and created.get("done_at") and created.get("strategies") == expected:
            if not data.get("dry_run"):
                from strat_side_effects import check_owner
                check_owner(*require_env(), rfe, data)
            print(json.dumps({"already_prepared": expected}))
            return
    rfe = authorized(args.key, data)
    if args.prepare:
        from import_strategy import import_strategy
        import_strategy(args.key, rfe)
        cmd_record_create(None)
    print(json.dumps({"reprocess": args.key, "source_rfe": rfe}))


if __name__ == "__main__":
    main()
