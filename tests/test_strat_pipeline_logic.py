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
    assert out["error"] is None, out
    return out


def _full(responses=None):
    base = {
        "read input": _read(),
        "strategy-create": {"strategies": PAIRS},
        "refine RHAISTRAT-1": {"recorded": "RHAISTRAT-1"},
        "refine RHAISTRAT-2": {"recorded": "RHAISTRAT-2"},
        "push": {"recorded": "push", "strategies": ["RHAISTRAT-1", "RHAISTRAT-2"]},
        "score RHAISTRAT-1": "scored", "score RHAISTRAT-2": "scored",
        "review RHAISTRAT-1": _verdict("RHAISTRAT-1"), "review RHAISTRAT-2": _verdict("RHAISTRAT-2"),
        "write result": {"action": "completed", "check_exit": 0},
    }
    base.update(responses or {})
    return base


def test_fresh_run_runs_every_step_in_order(tmp_path):
    out = _run(tmp_path, _full())
    calls = out["calls"]
    assert calls[:2] == ["read input", "strategy-create"]
    assert set(calls[2:4]) == {"refine RHAISTRAT-1", "refine RHAISTRAT-2"}
    assert calls[4] == "push"
    assert calls.index("score RHAISTRAT-1") < calls.index("review RHAISTRAT-1")
    assert calls[-1] == "write result"
    assert out["result"] == {"action": "completed", "check_exit": 0, "failed": []}


def test_a_failed_push_holds_back_every_refined_review(tmp_path):
    out = _run(tmp_path, _full({"push": None, "write result": {"action": "failed", "check_exit": 0}}))
    assert not [c for c in out["calls"] if c.startswith(("score", "review"))]
    assert out["result"]["failed"] == ["push"]


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
    assert out["result"]["failed"] == ["score:RHAISTRAT-2"]


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
