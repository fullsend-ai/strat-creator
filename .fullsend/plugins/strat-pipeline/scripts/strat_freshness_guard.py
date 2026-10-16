#!/usr/bin/env python3
"""Check S1 freshness before Skill/Workflow launch; refuse native journal replay."""

import json
import sys
from pathlib import Path


def guard(event):
    root = Path(event.get("cwd") or Path.cwd()).resolve()
    path = root / "tmp/strat-input.json"
    if not path.exists():
        return
    sys.path.insert(0, str(root / "scripts"))
    # Unit fixtures can invoke the root copy; the plugin resolves vendored code.
    from strat_freshness import pipeline, verify_progress
    data = json.loads(path.read_text())
    if not pipeline(root, data):
        return
    values = event.get("tool_input") or {}
    if event.get("tool_name") == "Workflow" and values.get("resumeFromRunId") is not None:
        raise ValueError("S1 requires a fresh Workflow launch; native journal replay is unsupported")
    completion = root / "tmp/strat-completion.json"
    if completion.exists():
        receipt = json.loads(completion.read_text())
        if receipt.get("run_id") == data["run_id"] and receipt.get("check_exit") == 1:
            raise ValueError("S1 business validation failed; preserve the result and end this invocation. "
                             "Do not repair artifacts or relaunch work inside this prepared run")
    progress = root / "tmp/strat-progress.json"
    verify_progress(root, data, json.loads(progress.read_text()) if progress.exists() else {})


if __name__ == "__main__":
    try:
        guard(json.load(sys.stdin))
    except Exception as error:
        print("S1 freshness check refused launch: " + str(error), file=sys.stderr)
        raise SystemExit(2)
