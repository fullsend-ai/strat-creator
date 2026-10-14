#!/usr/bin/env python3
"""Authored scorer/report fixtures for testing preservation, not strategy quality."""

import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
from artifact_utils import write_frontmatter  # noqa: E402

CASES = {
    "STRAT-9501": {
        "title": "Serving timeout ownership",
        "scores": [2, 2, 2, 2],
        "strategy": "Configure a timeout at the HTTP routing component for slow serving requests.",
        "reports": {
            "feasibility": (
                "approve",
                "FEASIBILITY_9501: The router can enforce the timeout without a storage change.",
            ),
            "testability": (
                "revise",
                "TESTABILITY_9501: No test covers timeout after response headers; add a delayed-stream failure test.",
            ),
            "scope": (
                "approve",
                "SCOPE_9501: Keep this work limited to routing timeouts; connection pooling is out of scope.",
            ),
            "architecture": (
                "revise",
                "ARCHITECTURE_9501: Unlike the feasibility view, router ownership alone is insufficient; "
                "document downstream cancellation ownership.",
            ),
        },
    },
    "STRAT-9502": {
        "title": "Dataset retention holds",
        "scores": [1, 1, 1, 1],
        "strategy": "Expire stored dataset objects while retaining objects with active legal holds.",
        "reports": {
            "feasibility": (
                "approve",
                "FEASIBILITY_9502: Expiry can use the existing storage listing and deletion APIs.",
            ),
            "testability": (
                "revise",
                "TESTABILITY_9502: A hold can change during deletion; "
                "add a race test and partial-deletion failure test.",
            ),
            "scope": (
                "approve",
                "SCOPE_9502: Limit retention work to dataset objects; notebook cleanup is a separate capability.",
            ),
            "architecture": (
                "approve",
                "ARCHITECTURE_9502: The storage service must own the hold check at deletion time.",
            ),
        },
    },
}


def report(key, role):
    verdict, finding = CASES[key]["reports"][role]
    return (
        f"### {key}: {CASES[key]['title']}\n**{role.title()} assessment**: authored fixture\n"
        f"{finding}\n**Recommendation**: {verdict}"
    )


def setup():
    for key, case in CASES.items():
        rfe = key.replace("STRAT", "RHAIRFE")
        strategy = ROOT / "artifacts/strat-tasks" / f"{key}.md"
        strategy.parent.mkdir(parents=True, exist_ok=True)
        strategy.write_text("## Strategy\n" + case["strategy"] + "\n")
        write_frontmatter(
            strategy,
            {
                "strat_id": key,
                "source_rfe": rfe,
                "title": case["title"],
                "priority": "Major",
                "status": "Refined",
                "refine_count": 1,
            },
            "strat-task",
        )
        source = ROOT / "artifacts/strat-originals" / f"{rfe}.md"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text(
            f"# {rfe}: {case['title']}\n\n{case['strategy']}\n\n"
            "Authored report inputs for composition testing, not independent content grading.\n\n"
            + "\n\n".join(f"## Authored {role} report\n\n{report(key, role)}" for role in case["reports"])
            + "\n"
        )
        scorer = ROOT / "tmp/composition-scores" / key / f"{key}.result.md"
        scorer.parent.mkdir(parents=True, exist_ok=True)
        scorer.write_text(
            "## Scores\n| Criterion | Score |\n| --- | --- |\n"
            + "".join(f"| {role.title()} | {score}/2 |\n" for role, score in zip(case["reports"], case["scores"]))
            + f"\n## Feedback\nSCORER_ONLY_{key}: Numeric fixture, not a specialist finding.\n"
        )
    directory = ROOT / "tmp"
    data = {
        "run_id": "review-composition-fixture",
        "mode": "batch",
        "dry_run": True,
        "acquired_keys": [key.replace("STRAT", "RHAIRFE") for key in CASES],
        "input_keys": [],
        "resume_points": {},
        "expected_skips": [],
        "skipped": [],
    }
    (directory / "strat-input.json").write_text(json.dumps(data) + "\n")
    pairs = [{"strat": key, "rfe": key.replace("STRAT", "RHAIRFE")} for key in CASES]
    (directory / "strat-progress.json").write_text(
        json.dumps(
            {
                "run_id": data["run_id"],
                "create": {"done_at": "2026-01-01T00:00:00Z", "strategies": pairs},
                "refine": dict.fromkeys(CASES, "2026-01-01T00:00:00Z"),
                "push": {"done_at": "2026-01-01T00:00:00Z", "strategies": list(CASES), "skipped": True},
                "review": {},
            }
        )
        + "\n"
    )
    fixture = ROOT / "tests/fixtures/review-composition"
    architecture = fixture / "context/architecture/rhoai-fixture"
    architecture.mkdir(parents=True, exist_ok=True)
    (architecture / "PLATFORM.md").write_text(
        "# Authored platform fixture\nRouter handles HTTP serving; storage handles dataset deletion.\n"
    )
    for role in next(iter(CASES.values()))["reports"]:
        skill = ROOT / ".claude/skills" / f"strategy-{role}-review/SKILL.md"
        skill.parent.mkdir(parents=True, exist_ok=True)
        skill.write_text(f"""---
name: strategy-{role}-review
description: Return one authored {role} report to test composition, not content accuracy.
context: fork
agent: strat-reviewer
allowed-tools: Read, Grep, Glob
model: inherit
user-invocable: false
---

REVIEW_SCOPE_TARGET: $ARGUMENTS

Read only `artifacts/strat-originals/<SOURCE_RFE>.md`, where SOURCE_RFE is the
assigned key with STRAT replaced by RHAIRFE. Return the complete authored
{role} report under `## Authored {role} report` verbatim. Start with its
`### <KEY>:` heading and end with its Recommendation line. Do not include
another role, scorer text, a summary, code fences or another ticket. No writes.
""")
    source = (ROOT / "workflows/strat-pipeline.js").read_text()
    pattern = next(line for line in source.splitlines() if line.startswith("const STRAT = "))
    schema = pattern + "\n" + source[source.index("const VERDICT = {") : source.index("const RESULT = {")]
    code = (
        """export const meta={name:'review-composition',
description:'Preserve authored reports through the actual review Skill'};
"""
        + schema
        + """
const targets = ['STRAT-9501', 'STRAT-9502']
const results = await parallel(targets.map(key => () => agent(
  `Run the strategy-review Skill once with args "${key} --scores-from tmp/composition-scores/${key} ` +
  '--architecture-context tests/fixtures/review-composition/context --dry-run". ' +
  'This tests preservation of authored findings, not strategy content generation. Follow the actual review Skill, ' +
  'including numeric parsing, begin, four forked reviewers, compose and verify. Do not regenerate scores, ' +
  'rerun reviewers, rewrite the composed file, call Jira or read other ticket files. ' +
  `After successful composition, run python3 scripts/strat_pipeline_state.py record-review ${key} ` +
  'and return its JSON exactly.',
  {label:`compose review ${key}`, effort:'medium',
   schema:{...VERDICT,properties:{...VERDICT.properties,key:{type:'string',const:key}}}},
)))
if (results.some((result,i) => !result || result.key !== targets[i])) throw new Error('Missing composition result')
return results
"""
    )
    workflow = fixture / "plugin/workflows/review-composition.js"
    workflow.parent.mkdir(parents=True, exist_ok=True)
    workflow.write_text(code)


def prepare_score(key):
    directory = Path("tmp/composition-scores") / key
    subprocess.run([sys.executable, "scripts/assess-strat/parse_results.py", str(directory)], cwd=ROOT, check=True)
    subprocess.run(
        [
            sys.executable,
            "scripts/apply_scores.py",
            str(directory / "scores.csv"),
            "--review-dir",
            "artifacts/strat-reviews",
            "--result-dir",
            str(directory),
        ],
        cwd=ROOT,
        check=True,
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["setup", "prepare-score"])
    parser.add_argument("key", nargs="?", choices=list(CASES))
    args = parser.parse_args()
    if args.command == "setup":
        setup()
    elif args.key:
        prepare_score(args.key)
    else:
        parser.error("prepare-score needs one key")


if __name__ == "__main__":
    main()
