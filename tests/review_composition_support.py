"""Authored composition inputs shared by state and trusted-host tests."""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "scripts/assess-strat"))
import review_composition as composition  # noqa: E402
from artifact_utils import write_frontmatter  # noqa: E402


def prepare(root, key, rfe, scores, verdicts=None, finish=True):
    root = root.resolve()
    source = root / "artifacts/strat-originals" / f"{rfe}.md"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text("Authored source RFE for composition tests.\n")
    review = root / "artifacts/strat-reviews" / f"{key}-review.md"
    review.parent.mkdir(parents=True, exist_ok=True)
    review.write_text("## Scores\nPrepared numeric assessment.\n")
    values = dict(zip(composition.ROLES, scores))
    values["total"] = sum(scores)
    decision = composition.numeric_decision(values)
    write_frontmatter(review, {"strat_id": key, **decision, "scores": values,
                              "reviewers": dict.fromkeys(composition.ROLES, "pending")}, "strat-review")
    scorer = root / "tmp/composition-scorer" / f"{key}.result.md"
    scorer.parent.mkdir(parents=True, exist_ok=True)
    scorer.write_text("## Scores\n| Criterion | Score |\n| --- | --- |\n" +
                      "".join(f"| {role.title()} | {value}/2 |\n" for role, value in zip(composition.ROLES, scores)) +
                      "\n## Feedback\nSCORER_ONLY: numeric assessment, not a specialist report.\n")
    started = composition.begin(root, key, scorer)
    event = {"cwd": str(root), "hook_event_name": "PostToolUse", "session_id": "fixture-session",
             "agent_id": "fixture-caller-" + key, "tool_name": "Bash",
             "tool_response": {"stdout": json.dumps(started)}}
    composition.capture(event)
    verdicts = verdicts or dict.fromkeys(composition.ROLES, decision["recommendation"])
    reports = {}
    for role in composition.ROLES:
        report = (f"### {key}: Authored strategy\n**{role.title()} assessment**: authored finding\n"
                  f"{role.upper()}_CONCERN: retain this exact finding and its opposing view.\n"
                  f"**Recommendation**: {verdicts[role]}")
        reports[role] = {**event, "tool_name": "Skill", "tool_use_id": key + "_" + role,
                         "tool_input": {"skill": "strategy-" + role + "-review", "args": key},
                         "tool_response": {"success": True, "status": "forked", "agentId": key + "-" + role,
                                           "commandName": "strategy-" + role + "-review", "result": report}}
        if finish:
            composition.capture(reports[role])
    if finish:
        composition.compose(root, key)
    return started, reports
