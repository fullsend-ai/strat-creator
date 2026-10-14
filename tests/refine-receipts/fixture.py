#!/usr/bin/env python3
"""Build a focused test from the candidate's actual refinement-stage code."""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
from artifact_utils import read_frontmatter, write_frontmatter  # noqa: E402

PROPOSALS = {
    "STRAT-9401": ("Serving request timeout", "Add a configured HTTP timeout at the routing component; "
                   "test delayed responses and preserve normal successful requests."),
    "STRAT-9402": ("Dataset retention and deletion", "Expire unheld dataset objects through the storage API; "
                   "preview deletion candidates and test legal holds and partial deletion failures."),
    "STRAT-9403": ("Pipeline credential rotation", "Use a current secret reference for new scheduled pipeline "
                   "jobs while active jobs retain their launch-time credential."),
    "STRAT-9404": ("Accessibility of notebook controls", "Add keyboard navigation and screen reader labels "
                   "to notebook creation controls and validate the complete creation flow."),
}


def setup(keys):
    pairs = [{"strat": key, "rfe": key.replace("STRAT", "RHAIRFE")} for key in keys]
    directory = ROOT / "tmp"
    directory.mkdir(exist_ok=True)
    data = {"run_id": "refine-receipts-fixture", "mode": "batch", "dry_run": True,
            "acquired_keys": [pair["rfe"] for pair in pairs], "input_keys": [pair["rfe"] for pair in pairs],
            "resume_points": {}, "skipped": [], "expected_skips": []}
    (directory / "strat-input.json").write_text(json.dumps(data) + "\n")
    (directory / "strat-progress.json").write_text(json.dumps({"run_id": data["run_id"],
        "create": {"done_at": "2026-01-01T00:00:00Z", "strategies": pairs},
        "refine": {}, "push": None, "review": {}}) + "\n")
    for pair in pairs:
        path = ROOT / "artifacts/strat-tasks" / f"{pair['strat']}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("## Strategy\n<!-- To be filled by /strategy-refine -->\n")
        write_frontmatter(path, {"strat_id": pair["strat"], "source_rfe": pair["rfe"],
            "title": PROPOSALS[pair["strat"]][0], "priority": "Major", "status": "Draft",
            "refine_count": 0}, "strat-task")


def apply(key):
    path = ROOT / "artifacts/strat-tasks" / f"{key}.md"
    metadata, _ = read_frontmatter(path)
    path.write_text("## Strategy\n" + PROPOSALS[key][1] + "\n")
    metadata["status"] = "Refined"
    metadata["refine_count"] = (metadata.get("refine_count") or 0) + 1
    write_frontmatter(path, metadata, "strat-task")
    print(json.dumps({"fixture_refined": key}))


def build():
    source = (ROOT / "workflows/strat-pipeline.js").read_text()
    # Keep production schemas, helpers, read/create and refine/readiness code.
    start = source.index("// The order lives here")
    end = source.index("// A refined strategy is reviewed only once")
    code = source[start:end].replace("effort: 'low'", "effort: 'medium'")
    before = code
    code = code.replace("      agent(\n", "      fixtureAgent(\n", 1)
    assert code != before
    wrapper = '''
const faults = args && args.receipt_faults !== undefined ? args.receipt_faults : {'STRAT-9402': 'nested'}
const injected = []
async function fixtureAgent(prompt, opts) {
  const result = await agent(prompt, {...opts, effort: 'medium'})
  const key = opts.label.slice('refine '.length)
  if (faults[key] === 'nested') {
    const fault = {recorded: JSON.stringify({recorded: key})}
    injected.push({key, receipt: fault})
    log(`TEST_REFINE_RECEIPT_FAULT ${key}: ${JSON.stringify(fault)}`)
    return fault
  }
  return result
}
'''
    meta = "export const meta={name:'refine-receipts',description:'Check refinement receipts against saved work'};\n"
    output = ROOT / "tests/fixtures/refine-receipts/plugin/workflows/refine-receipts.js"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(meta + wrapper + code + "\nreturn {ready: ready.map(p => p.strat), failed, faults, injected}\n")
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["setup", "apply"])
    parser.add_argument("keys", nargs="+")
    args = parser.parse_args()
    if any(key not in PROPOSALS for key in args.keys):
        parser.error("unknown authored fixture key")
    if args.mode == "setup":
        setup(args.keys)
        print(build())
    elif len(args.keys) == 1:
        apply(args.keys[0])
    else:
        parser.error("apply takes one key")


if __name__ == "__main__":
    main()
