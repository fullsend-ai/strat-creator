#!/usr/bin/env python3
"""Plugin adapter for final structured completion; helpers are vendored in scripts/."""
import json
import sys
from pathlib import Path


def main():
    event = {}
    verified, error = None, None
    try:
        event = json.load(sys.stdin)
        if event.get("tool_input", {}).get("kind") != "strat-completion":
            return 0
        sys.path.insert(0, str(Path(event["cwd"]) / "scripts"))
        from strat_pipeline_outcome import guard
        verified = guard(event)
    except (Exception, SystemExit) as exception:
        error = str(exception)
    path = Path(event.get("cwd", ".")) / "tmp/completion-checkpoints/events.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as stream:
        stream.write(json.dumps({"agent_id": event.get("agent_id"), "tool_use_id": event.get("tool_use_id"),
                                 "input": event.get("tool_input"), "allowed": error is None,
                                 "error": error, "verified": verified}) + "\n")
    if error:
        print("Completion checkpoint: " + error, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
