#!/usr/bin/env python3
"""List RFE IDs from a config file or Jira JQL query.

Usage:
    # From config file (existing behavior)
    python3 scripts/list-rfe-ids.py --config config/road-to-production/batch-03.yaml

    # From Jira using the work type's settings file (discovery.settings; config/pipeline-settings.yaml
    # for rfe-strategy, the default; --type picks another type)
    python3 scripts/list-rfe-ids.py --jql-default

    # From Jira with raw JQL
    python3 scripts/list-rfe-ids.py --jql 'project = RHAIRFE AND labels = "strat-creator-3.5"'

    # Batching
    python3 scripts/list-rfe-ids.py --jql-default --batch-size 10
    python3 scripts/list-rfe-ids.py --jql-default --batch-size 10 --batch-offset 10
"""

import argparse
import os
import sys
from pathlib import Path

import yaml

sys.path.insert(0, os.path.dirname(__file__))

import type_registry

REPO = Path(__file__).resolve().parent.parent
# The work type (--type, default rfe-strategy) names its settings file: the intake gate, the skip
# rules and the batch size (discovery.settings). --jql-default SETTINGS passes another file.
_TYPES = type_registry.load()


def ids_from_config(config_path, baseline=None):
    """Read RFE IDs from a YAML config file."""
    with open(config_path) as f:
        data = yaml.safe_load(f)
    ids = []
    for rfe in data.get("test_rfes", []):
        is_baseline = rfe.get("baseline", False)
        if baseline is True and not is_baseline:
            continue
        if baseline is False and is_baseline:
            continue
        ids.append(rfe["id"])
    return ids


def ids_from_jql(jql):
    """Search Jira with a JQL query and return issue keys."""
    from jira_utils import require_env, search_issues

    server, user, token = require_env()
    if not all([server, user, token]):
        print("Error: JIRA_SERVER, JIRA_USER, JIRA_TOKEN required for "
              "--jql mode.", file=sys.stderr)
        sys.exit(2)
    issues = search_issues(server, user, token, jql, fields=["key"])
    return [issue["key"] for issue in issues]


def main():
    parser = argparse.ArgumentParser(description="List RFE IDs")

    source = parser.add_mutually_exclusive_group()
    source.add_argument("--config", default=None,
                        help="Path to batch config YAML")
    source.add_argument("--jql", default=None,
                        help="Raw JQL query string")
    source.add_argument("--jql-default", nargs="?", const="",
                        default=None, metavar="SETTINGS",
                        help="Build JQL from the work type's settings file "
                             "(optionally pass a custom settings path)")

    baseline_group = parser.add_mutually_exclusive_group()
    baseline_group.add_argument("--baseline", action="store_true",
                                help="Only baseline RFEs (config mode)")
    baseline_group.add_argument("--no-baseline", action="store_true",
                                help="Exclude baseline RFEs (config mode)")

    parser.add_argument("--include-processed", action="store_true",
                        help="Include RFEs whose STRATs already have "
                             "skip labels (default: exclude them)")
    parser.add_argument("--verbose", action="store_true",
                        help="Log each excluded RFE individually")
    parser.add_argument("--batch-size", type=int, default=None,
                        help="Limit output to N RFE IDs")
    parser.add_argument("--batch-offset", type=int, default=0,
                        help="Skip first N RFE IDs")
    parser.add_argument("--type", choices=_TYPES.choices(), default=None,
                        help="Work type whose settings file --jql-default and the already-processed "
                             "check read (default: rfe-strategy)")

    args = parser.parse_args()
    if args.type and args.jql is None and args.jql_default != "":
        parser.error("--type works with --jql-default (no settings path) or --jql")

    # rfe-creator's ladder: --type, else the default. The ids are this script's output, not a signal.
    # The TYPE RESOLVED line only for an explicit --type, so a run without it prints what it did before.
    resolution = type_registry.resolve(_TYPES, explicit_type=args.type)
    if resolution.rung != type_registry.LEGACY_DEFAULT_RUNG:
        print(resolution.line(), file=sys.stderr)
    work_type = resolution.desc
    settings_path = args.jql_default or str(REPO / work_type.get("discovery.settings"))
    settings = {}
    if args.jql is not None or args.jql_default is not None:
        with open(settings_path) as f:
            settings = yaml.safe_load(f) or {}

    # Determine source mode
    if args.jql is not None:
        ids = ids_from_jql(args.jql)
        print(f"JQL returned {len(ids)} RFE(s)", file=sys.stderr)

    elif args.jql_default is not None:
        from jira_utils import build_jql_from_config
        jql = build_jql_from_config(settings_path)
        print(f"JQL: {jql}", file=sys.stderr)
        ids = ids_from_jql(jql)
        print(f"JQL returned {len(ids)} RFE(s)", file=sys.stderr)

        # Read batch_size from settings if not overridden on CLI
        if args.batch_size is None:
            args.batch_size = settings.get("batch_size")

    else:
        config_path = args.config or str(
            Path(__file__).resolve().parent.parent / "config" / "test-rfes.yaml"
        )
        if not Path(config_path).exists():
            print(f"Error: {config_path} not found", file=sys.stderr)
            sys.exit(1)
        baseline = None
        if args.baseline:
            baseline = True
        elif args.no_baseline:
            baseline = False
        ids = ids_from_config(config_path, baseline)

    # Exclude already-processed RFEs (JQL modes only, unless --include-processed)
    in_jql_mode = args.jql is not None or args.jql_default is not None
    if not args.include_processed and in_jql_mode:
        from jira_utils import find_processed_rfe_ids, require_env
        skip_labels = settings.get("skip_labels", [])
        excluded_strat_statuses = settings.get("excluded_strat_statuses", [])
        if skip_labels or excluded_strat_statuses:
            server, user, token = require_env()
            processed = find_processed_rfe_ids(
                server, user, token, skip_labels,
                excluded_strat_statuses=excluded_strat_statuses, desc=work_type)
            excluded_ids = [i for i in ids if i in processed]
            ids = [i for i in ids if i not in processed]
            if excluded_ids:
                if args.verbose:
                    for eid in excluded_ids:
                        print(f"  excluding {eid}", file=sys.stderr)
                print(f"Excluded {len(excluded_ids)} already-processed "
                      f"RFE(s), {len(ids)} remaining",
                      file=sys.stderr)

    # Apply batching
    ids = ids[args.batch_offset:]
    if args.batch_size is not None:
        ids = ids[:args.batch_size]

    for rfe_id in ids:
        print(rfe_id)


if __name__ == "__main__":
    main()
