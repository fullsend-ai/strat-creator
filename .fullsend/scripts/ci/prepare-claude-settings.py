#!/usr/bin/env python3
"""Prepare project settings and workspace trust for authorized strategy CI."""
import json
import os
import tempfile
import sys
from pathlib import Path


def prepare_trust(root, config_dir):
    directory = Path(config_dir)
    path = directory / ".claude.json"
    settings = json.loads(path.read_text()) if path.exists() else {}
    workspace = str(Path(root).resolve())
    project = settings.setdefault("projects", {}).setdefault(workspace, {})
    if project.get("hasTrustDialogAccepted") is True:
        return
    project["hasTrustDialogAccepted"] = True
    directory.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", dir=directory, delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(settings, stream, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def prepare(root):
    path = Path(root) / ".claude/settings.json"
    if not path.exists():
        return
    settings = json.loads(path.read_text())
    permissions = settings.get("permissions", {})
    directories = permissions.get("additionalDirectories", [])
    if "/tmp/strat-assess" not in directories:
        return
    remaining = [value for value in directories if value != "/tmp/strat-assess"]
    if remaining:
        permissions["additionalDirectories"] = remaining
    else:
        permissions.pop("additionalDirectories")
    path.write_text(json.dumps(settings, indent=2) + "\n")


if __name__ == "__main__":
    config_dir = os.environ.get("CLAUDE_CONFIG_DIR")
    if not config_dir:
        raise SystemExit("CLAUDE_CONFIG_DIR is required for strategy workspace trust")
    prepare(sys.argv[1])
    prepare_trust(sys.argv[1], config_dir)
