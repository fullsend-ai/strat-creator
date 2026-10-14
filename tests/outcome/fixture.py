#!/usr/bin/env python3
"""Authored completed work for testing the real final checkpoint, not content quality."""

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "scripts"))
from review_composition_support import prepare  # noqa: E402
from strat_pipeline_outcome import atomic_json, observe, verify_completion  # noqa: E402


def setup():
    root = ROOT
    data = {
        "run_id": "outcome-fixture",
        "mode": "batch",
        "dry_run": True,
        "input_keys": ["RHAIRFE-9601", "RHAIRFE-9602"],
        "acquired_keys": ["RHAIRFE-9601", "RHAIRFE-9602"],
        "expected_skips": [],
        "skipped": [],
        "resume_points": {},
        "architecture_context": "available",
    }
    atomic_json(root / "tmp/strat-input.json", data)
    when = "2026-10-14T00:00:00Z"
    progress = {
        "run_id": data["run_id"],
        "create": {"done_at": when, "pending": [], "skipped": [], "strategies": []},
        "refine": {},
        "push": {"done_at": when, "skipped": True, "strategies": []},
        "review": {},
    }
    for number, scores in [(9601, (2, 2, 2, 2)), (9602, (1, 1, 1, 1))]:
        key, rfe = f"STRAT-{number}", f"RHAIRFE-{number}"
        path = root / "artifacts/strat-tasks" / f"{key}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        body = (
            "The gateway owns timeout cancellation." if number == 9601 else "The storage service owns retention holds."
        )
        path.write_text(
            f"---\nstrat_id: {key}\nsource_rfe: {rfe}\ntitle: Authored completed fixture\n"
            "priority: Major\nstatus: Refined\n---\n## Strategy\n" + body + "\n"
        )
        started, _ = prepare(root, key, rfe, scores)
        decision = {"recommendation": "approve" if number == 9601 else "revise", "needs_attention": number == 9602}
        progress["create"]["strategies"].append({"rfe": rfe, "strat": key})
        progress["refine"][key] = when
        progress["push"]["strategies"].append(key)
        progress["review"][key] = {**decision, "done_at": when, "composition_session": started["session_id"]}
    atomic_json(root / "tmp/strat-progress.json", progress)
    source = (root / "workflows/strat-pipeline.js").read_text()
    schema = source[source.index("const RESULT = {") : source.index("const failed = []")]
    # The hardening plan fixes every live GLM test agent at medium effort.
    terminal = source[source.index("phase('Result')") :].replace("effort: 'low'", "effort: 'medium'")
    workflow = root / "tests/fixtures/outcome/plugin/workflows/outcome.js"
    workflow.parent.mkdir(parents=True, exist_ok=True)
    workflow.write_text(
        "export const meta={name:'outcome',description:'Verify saved business completion'};\n"
        "const HEADLESS='OUTCOME_CONTROL: use only the exact final helper command. ';\n"
        "const run={run_id:'outcome-fixture'};\nconst failed=[];\n" + schema + terminal
    )


def inspect(artifacts, cli_exit):
    receipt = verify_completion(ROOT, ROOT / "output/agent-result.json")
    states = list((artifacts / "native-projects").rglob("workflows/wf_*.json"))
    matches = []
    for path in states:
        value = json.loads(path.read_text()).get("result")
        if isinstance(value, dict) and value.get("receipt_id") == receipt["receipt_id"]:
            matches.append(path)
    if len(matches) != 1:
        raise ValueError("need one native Workflow with this completion identity")
    state = matches[0]
    identity = json.loads(state.read_text())["runId"]
    journals = list((artifacts / "native-projects").rglob(f"subagents/workflows/{identity}/journal.jsonl"))
    if len(journals) != 1:
        raise ValueError("need the matched native journal")
    observed = observe(ROOT, state, journals[0], cli_exit, ROOT / "output/agent-result.json")
    atomic_json(artifacts / "runtime-outcome.json", observed)
    if not observed["runtime_verified"]:
        raise ValueError("runtime completion failed: " + "; ".join(observed["errors"]))
    return observed


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["setup", "inspect"])
    parser.add_argument("--artifacts", type=Path)
    parser.add_argument("--cli-exit", type=int, default=0)
    args = parser.parse_args()
    if args.command == "setup":
        setup()
    else:
        print(json.dumps(inspect(args.artifacts, args.cli_exit)))


if __name__ == "__main__":
    main()
