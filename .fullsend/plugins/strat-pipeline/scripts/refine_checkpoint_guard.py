#!/usr/bin/env python3
"""Check refinement readiness at the structured-output boundary.

The model launches the helper and carries JSON. It cannot promote a strategy
whose current-run progress and artifact fail the deterministic check.
"""
import contextlib
import json
import sys
from pathlib import Path


def guard(event):
    if event.get("hook_event_name") != "PreToolUse" or event.get("tool_name") != "StructuredOutput":
        return None
    value = event.get("tool_input", {})
    snapshot = value.get("kind") == "strat-refine-readiness"
    initial = all(key in value for key in ("cwd", "run_id", "pending_create", "create_done", "refined"))
    if not snapshot and not initial:
        return None
    with contextlib.chdir(event["cwd"]):
        sys.path.insert(0, str(Path.cwd() / "scripts"))
        import strat_pipeline_state as state
        data = state.load_input(live=False)
        with state.progress_lock():
            expected = state.refinement_status(data, state.load_progress(data["run_id"], live=False))
    if snapshot and value != expected:
        raise ValueError("refinement checkpoint disagrees with current-run progress/artifacts; "
                         "return the helper JSON exactly")
    if initial and (value.get("run_id") != expected["run_id"] or value.get("refined") != expected["refined"]):
        raise ValueError("initial refinement readiness disagrees with current-run progress/artifacts")
    return {"kind": "checkpoint" if snapshot else "initial", "verified": expected}


def main():
    event = {}
    result, error = None, None
    try:
        event = json.load(sys.stdin)
        result = guard(event)
    except (Exception, SystemExit) as exception:
        error = str(exception)
    if result or error:
        directory = Path(event.get("cwd", ".")) / "tmp/refine-checkpoints"
        directory.mkdir(parents=True, exist_ok=True)
        # One append per hook process; retain both denials and verified outputs.
        with (directory / "events.jsonl").open("a") as stream:
            stream.write(json.dumps({"agent_id": event.get("agent_id"),
                "tool_use_id": event.get("tool_use_id"), "input": event.get("tool_input"),
                "allowed": error is None, "error": error, "result": result}) + "\n")
    if error:
        print("Refinement checkpoint: " + error, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
