"""The strat-pipeline Workflow's control flow, executed under Node with mocked agents.

tests/fixtures/run-workflow.mjs evaluates workflows/strat-pipeline.js with a
mocked agent()/parallel()/pipeline(), so these tests check which steps run in
which order for fresh runs, retries and failures, without a model.
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "workflows" / "strat-pipeline.js"
RUNNER = ROOT / "tests" / "fixtures" / "run-workflow.mjs"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")


def _read(**overrides):
    state = {"cwd": "/repo", "run_id": "r1", "mode": "batch", "dry_run": False,
             "acquired_keys": ["RHAIRFE-1", "RHAIRFE-2"], "review_only": [], "create_done": False,
             "pending_create": ["RHAIRFE-1", "RHAIRFE-2"], "strategies": [], "refined": [], "pushed": [],
             "reviewed": []}
    state.update(overrides)
    return state


PAIRS = [{"rfe": "RHAIRFE-1", "strat": "RHAISTRAT-1"}, {"rfe": "RHAIRFE-2", "strat": "RHAISTRAT-2"}]


def _verdict(key):
    return {"key": key, "recommendation": "approve", "needs_attention": False}


def _run(tmp_path, responses, args=None):
    scenario = tmp_path / "scenario.json"
    scenario.write_text(json.dumps({"args": args, "responses": responses}))
    proc = subprocess.run(["node", str(RUNNER), str(SCRIPT), str(scenario)], capture_output=True, text=True,
                          timeout=60)
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    if (responses.get("write result") or {}).get("action") == "failed":
        assert out["error"] and "business completion not verified" in out["error"], out
    else:
        assert out["error"] is None, out
    return out


def _full(responses=None):
    base = {
        "read input": _read(),
        "prepare reviews": {"reviewed": []},
        "strategy-create": {"strategies": PAIRS},
        "refine RHAISTRAT-1": {"recorded": "RHAISTRAT-1"},
        "refine RHAISTRAT-2": {"recorded": "RHAISTRAT-2"},
        "verify refinement progress": {"kind": "strat-refine-readiness", "run_id": "r1",
                                       "refined": [p["strat"] for p in PAIRS], "rejected": []},
        "push": {"recorded": "push", "strategies": ["RHAISTRAT-1", "RHAISTRAT-2"]},
        "score RHAISTRAT-1": "scored", "score RHAISTRAT-2": "scored",
        "review RHAISTRAT-1": _verdict("RHAISTRAT-1"), "review RHAISTRAT-2": _verdict("RHAISTRAT-2"),
        "write result": _completion(),
    }
    base.update(responses or {})
    return base


def _completion(**changes):
    return {"kind": "strat-completion", "run_id": "r1", "action": "completed", "check_exit": 0,
            "receipt_id": "a" * 32, "receipt_sha256": "b" * 64, **changes}


def test_fresh_run_runs_every_step_in_order(tmp_path):
    out = _run(tmp_path, _full())
    calls = out["calls"]
    assert calls[:2] == ["read input", "strategy-create"]
    assert set(calls[2:4]) == {"refine RHAISTRAT-1", "refine RHAISTRAT-2"}
    assert calls[4:6] == ["verify refinement progress", "push"]
    assert calls.index("score RHAISTRAT-1") < calls.index("review RHAISTRAT-1")
    assert calls[-1] == "write result"
    assert out["result"] == _completion()


def test_every_emitted_tool_schema_avoids_anthropic_top_level_unions(tmp_path):
    out = _run(tmp_path, _full())
    assert {"review RHAISTRAT-1", "review RHAISTRAT-2", "write result"} <= out["schemas"].keys()
    for label, schema in out["schemas"].items():
        assert schema["type"] == "object", label
        assert not {"oneOf", "allOf", "anyOf"} & schema.keys(), label
    assert out["schemas"]["write result"]["properties"]["run_id"] == {"type": "string", "const": "r1"}


@pytest.mark.parametrize(("recommendation", "needs_attention"), [
    ("approve", False), ("revise", True), ("reject", True),
])
def test_consistent_review_verdicts_can_complete(tmp_path, recommendation, needs_attention):
    out = _run(tmp_path, _full({"review RHAISTRAT-2": {
        "key": "RHAISTRAT-2", "recommendation": recommendation, "needs_attention": needs_attention,
    }}))
    assert out["result"] == _completion()
    assert not any("review RHAISTRAT-2: no result" in line for line in out["logs"])


@pytest.mark.parametrize(("recommendation", "needs_attention"), [
    ("approve", True), ("revise", False), ("reject", False), ("unknown", True),
    ("approve", None), ("approve", 0), ("revise", 1),
])
def test_invalid_review_verdict_blocks_even_a_claimed_success(tmp_path, recommendation, needs_attention):
    scenario = tmp_path / "bad-verdict.json"
    scenario.write_text(json.dumps({"responses": _full({"review RHAISTRAT-2": {
        "key": "RHAISTRAT-2", "recommendation": recommendation, "needs_attention": needs_attention,
    }})}))
    proc = subprocess.run(["node", str(RUNNER), str(SCRIPT), str(scenario)],
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    assert "review RHAISTRAT-1" in out["calls"]
    assert "review RHAISTRAT-2: no result" in out["logs"]
    assert out["error"] and "business completion not verified" in out["error"]
    assert out["result"] is None


def test_a_failed_push_holds_back_every_refined_review(tmp_path):
    out = _run(tmp_path, _full({"push": None, "write result": {"action": "failed", "check_exit": 0}}))
    assert not [c for c in out["calls"] if c.startswith(("score", "review"))]
    assert out["result"] is None  # failed business action must fail the Workflow


def test_retry_after_a_partial_create_pushes_the_new_strategy_before_its_review(tmp_path):
    # Attempt 1 created, refined, pushed and reviewed RHAISTRAT-1; RHAIRFE-2 stayed pending.
    out = _run(tmp_path, _full({"read input": _read(
        pending_create=["RHAIRFE-2"], strategies=PAIRS[:1], refined=["RHAISTRAT-1"],
        pushed=["RHAISTRAT-1"], reviewed=["RHAISTRAT-1"])}))
    calls = out["calls"]
    assert "refine RHAISTRAT-1" not in calls and "review RHAISTRAT-1" not in calls
    assert calls.index("refine RHAISTRAT-2") < calls.index("push") < calls.index("review RHAISTRAT-2")


def test_review_only_rfes_skip_refine_and_push(tmp_path):
    out = _run(tmp_path, _full({"read input": _read(review_only=["RHAIRFE-1", "RHAIRFE-2"])}))
    calls = out["calls"]
    assert not [c for c in calls if c.startswith("refine") or c == "push"]
    assert {"review RHAISTRAT-1", "review RHAISTRAT-2"} <= set(calls)


def test_a_dead_scorer_skips_only_its_own_review(tmp_path):
    out = _run(tmp_path, _full({"score RHAISTRAT-2": None, "write result": {"action": "failed", "check_exit": 0}}))
    assert "review RHAISTRAT-1" in out["calls"] and "review RHAISTRAT-2" not in out["calls"]
    assert out["result"] is None


def test_a_malformed_namespace_is_refused(tmp_path):
    scenario = tmp_path / "scenario.json"
    scenario.write_text(json.dumps({"args": {"namespace": "Bad Name"}, "responses": {}}))
    proc = subprocess.run(["node", str(RUNNER), str(SCRIPT), str(scenario)], capture_output=True, text=True)
    assert "namespace must look like" in json.loads(proc.stdout)["error"]


def test_only_strategies_the_push_covered_are_reviewed(tmp_path):
    # RHAISTRAT-2 was refined but the push script skipped it (for example, status still Draft).
    out = _run(tmp_path, _full({"push": {"recorded": "push", "strategies": ["RHAISTRAT-1"]},
                                "write result": {"action": "failed", "check_exit": 0}}))
    assert "review RHAISTRAT-1" in out["calls"]
    assert "score RHAISTRAT-2" not in out["calls"] and "review RHAISTRAT-2" not in out["calls"]


@pytest.mark.parametrize("receipt", [None, {"recorded": '{"recorded":"RHAISTRAT-2"}'},
                                     {"recorded": "RHAISTRAT-1"}, {"recorded": "none"}])
def test_bad_handoff_recovers_verified_completion_without_repeating_refine(tmp_path, receipt):
    out = _run(tmp_path, _full({"refine RHAISTRAT-2": receipt}))
    assert out["calls"].count("refine RHAISTRAT-2") == 1
    assert "review RHAISTRAT-2" in out["calls"]
    assert out["result"]["action"] == "completed"
    assert any("completion verified from saved progress" in line for line in out["logs"])


def test_a_valid_receipt_cannot_promote_unrecorded_work(tmp_path):
    out = _run(tmp_path, _full({"verify refinement progress": {
        "kind": "strat-refine-readiness", "run_id": "r1", "refined": ["RHAISTRAT-1"],
        "rejected": [{"key": "RHAISTRAT-2", "reason": "not_recorded"}]},
        "push": {"recorded": "push", "strategies": ["RHAISTRAT-1"]},
        "write result": {"action": "failed", "check_exit": 0}}))
    assert "review RHAISTRAT-1" in out["calls"]
    assert "score RHAISTRAT-2" not in out["calls"]
    assert out["result"] is None


@pytest.mark.parametrize("snapshot", [None,
    {"kind": "strat-refine-readiness", "run_id": "old", "refined": ["RHAISTRAT-1", "RHAISTRAT-2"], "rejected": []},
    {"kind": "strat-refine-readiness", "run_id": "r1", "refined": ["RHAISTRAT-1", "RHAISTRAT-9"], "rejected": []},
    {"kind": "strat-refine-readiness", "run_id": "r1", "refined": ["RHAISTRAT-1"], "rejected": []}])
def test_unverified_or_wrong_run_checkpoint_holds_back_push_and_review(tmp_path, snapshot):
    out = _run(tmp_path, _full({"verify refinement progress": snapshot,
                              "write result": {"action": "failed", "check_exit": 0}}))
    assert not [c for c in out["calls"] if c.startswith(("score", "review")) or c == "push"]
    assert out["result"] is None


@pytest.mark.parametrize("result", [None, {"action": "completed", "check_exit": 0},
    _completion(run_id="another-run"), _completion(check_exit=1),
    _completion(action="failed", check_exit=0), _completion(action="failed", check_exit=1),
    _completion(check_exit=False), _completion(check_exit="0"),
    _completion(action="skipped"), _completion(receipt_id=""), _completion(receipt_sha256="")])
def test_invalid_final_receipt_cannot_complete_workflow(tmp_path, result):
    scenario = tmp_path / "final-fault.json"
    scenario.write_text(json.dumps({"responses": _full({"write result": result})}))
    proc = subprocess.run(["node", str(RUNNER), str(SCRIPT), str(scenario)], capture_output=True, text=True)
    value = json.loads(proc.stdout)
    assert value["error"] and value["result"] is None


def test_prepared_completed_reviews_skip_scorers_and_reviewers(tmp_path):
    out = _run(tmp_path, _full({"prepare reviews": {"reviewed": [p["strat"] for p in PAIRS]}}))
    assert not any(call.startswith(("score ", "review ")) for call in out["calls"])
    assert out["result"] == _completion()


def test_failed_preparation_cannot_advance_to_scorers_or_claim_success(tmp_path):
    scenario = tmp_path / "failed-prepare.json"
    scenario.write_text(json.dumps({"responses": _full({"prepare reviews": None})}))
    process = subprocess.run(["node", str(RUNNER), str(SCRIPT), str(scenario)], capture_output=True,
                             text=True, check=True, timeout=60)
    out = json.loads(process.stdout)
    assert out["error"] == "review preparation failed"
    assert not any(call.startswith(("score ", "review ")) for call in out["calls"])
    assert "write result" not in out["calls"]
