#!/usr/bin/env python3
"""Print the Target Version names an intake gate matches, one per line.

The names come from the settings file's ``jql`` block, resolved exactly as
``list-rfe-ids.py --jql-default`` resolves them (jira_utils.resolve_target_versions):
``target_versions`` verbatim when set, else the live unreleased versions of each
``target_version_trains`` entry plus ``extra_target_versions``. Release trains need the
JIRA_SERVER, JIRA_USER and JIRA_TOKEN environment variables.

Usage:
    python3 scripts/target_versions.py                  # the default type's settings file
    python3 scripts/target_versions.py --type <type>    # another type's discovery.settings
    python3 scripts/target_versions.py SETTINGS         # an explicit settings file
"""

import argparse
import os
import sys

import yaml

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import type_registry  # noqa: E402
from jira_utils import TargetVersionError, resolve_target_versions  # noqa: E402


def main(argv=None):
    types = type_registry.load()
    parser = argparse.ArgumentParser(description="Print an intake gate's resolved Target Versions")
    parser.add_argument("settings", nargs="?", default=None,
                        help="Settings file (default: the work type's discovery.settings)")
    parser.add_argument("--type", choices=types.choices(), default=None,
                        help="Work type whose settings file to read (default: rfe-strategy)")
    args = parser.parse_args(argv)
    if args.settings and args.type:
        parser.error("pass a settings file or --type, not both")
    desc = types.get(args.type or type_registry.LEGACY_DEFAULT_TYPE)
    path = args.settings or str(type_registry.PLUGIN_ROOT / desc.get("discovery.settings"))
    with open(path) as f:
        settings = yaml.safe_load(f) or {}
    try:
        names = resolve_target_versions(settings.get("jql") or {})
    except TargetVersionError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    for name in names:
        print(name)
    return 0


if __name__ == "__main__":
    sys.exit(main())
