"""Completion is a checked saved result, independent of CLI/reporting success."""

import copy
import json
import subprocess
import sys
from pathlib import Path

import pytest
from review_composition_support import prepare

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import strat_pipeline_outcome as outcome  # noqa: E402
import strat_pipeline_state as state  # noqa: E402


@pytest.fixture
def finished(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    data = {
        "run_id": "outcome-unit",
        "mode": "batch",
        "dry_run": True,
        "input_keys": ["RHAIRFE-9601"],
        "acquired_keys": ["RHAIRFE-9601"],
        "expected_skips": [],
        "skipped": [],
        "resume_points": {},
        "architecture_context": "available",
    }
    (tmp_path / "tmp").mkdir()
    (tmp_path / state.INPUT).write_text(json.dumps(data))
    task = tmp_path / "artifacts/strat-tasks/STRAT-9601.md"
    task.parent.mkdir(parents=True)
    task.write_text(
        "---\nstrat_id: STRAT-9601\nsource_rfe: RHAIRFE-9601\ntitle: Timeouts\n"
        "priority: Major\nstatus: Refined\n---\n## Strategy\nThe gateway enforces request timeouts.\n"
    )
    started, _ = prepare(tmp_path, "STRAT-9601", "RHAIRFE-9601", (1, 1, 1, 1))
    when = "2026-10-14T00:00:00Z"
    progress = {
        "run_id": data["run_id"],
        "create": {
            "done_at": when,
            "pending": [],
            "skipped": [],
            "strategies": [{"rfe": "RHAIRFE-9601", "strat": "STRAT-9601"}],
        },
        "refine": {"STRAT-9601": when},
        "push": {"done_at": when, "skipped": True, "strategies": ["STRAT-9601"]},
        "review": {
            "STRAT-9601": {
                "recommendation": "revise",
                "needs_attention": True,
                "done_at": when,
                "composition_session": started["session_id"],
            }
        },
    }
    state.save_progress(progress)
    result = state.assemble(data, progress, [])
    output = tmp_path / "output/agent-result.json"
    return tmp_path, data, progress, result, output


def finish(fixture, failed=()):
    root, data, progress, result, output = fixture
    return outcome.finalize(root, data, progress, copy.deepcopy(result), failed, output)


def test_completed_revision_is_successful_work_under_numeric_policy(finished):
    root, _, _, _, output = finished
    receipt = finish(finished)
    assert receipt["action"] == "completed" and receipt["check_exit"] == 0
    actual = outcome.verify_completion(root, output)["result"]
    assert actual["strategies"][0]["recommendation"] == "revise"
    assert actual["strategies"][0]["needs_attention"] is True


@pytest.mark.parametrize(
    "fault",
    [
        "refine",
        "push",
        "review",
        "missing_task",
        "changed_role",
        "verdict",
        "wrong_run",
        "unfinished_create",
        "unexpected_skip",
        "duplicate_mapping",
        "malformed_progress",
        "input_inventory",
    ],
)
def test_saved_success_marks_cannot_cover_invalid_or_missing_work(finished, fault):
    root, data, progress, result, output = finished
    if fault in ("refine", "review"):
        progress[fault] = {}
    elif fault == "push":
        progress["push"] = None
    elif fault == "missing_task":
        (root / "artifacts/strat-tasks/STRAT-9601.md").unlink()
    elif fault == "changed_role":
        p = root / "artifacts/strat-reviews/STRAT-9601-review-scope.md"
        p.write_text(p.read_text() + "\nAn omitted finding.\n")
    elif fault == "verdict":
        progress["review"]["STRAT-9601"]["needs_attention"] = False
    elif fault == "wrong_run":
        progress["run_id"] = "other"
    elif fault == "unfinished_create":
        progress["create"]["done_at"] = None
    elif fault == "unexpected_skip":
        progress["create"]["skipped"] = [{"key": "RHAIRFE-9601", "reason": "made up"}]
    elif fault == "duplicate_mapping":
        progress["create"]["strategies"] *= 2
    elif fault == "malformed_progress":
        progress["review"] = []
    elif fault == "input_inventory":
        data["input_keys"] = []
    (root / state.INPUT).write_text(json.dumps(data))
    state.save_progress(progress)
    receipt = finish(finished, [("review", "STRAT-9601")])
    assert receipt["action"] == "failed" and receipt["check_exit"] == 1
    assert json.loads(output.read_text())["errors"]
    with pytest.raises(ValueError):
        outcome.verify_completion(root, output)


def test_malformed_final_schema_cannot_override_saved_validation(finished):
    root = finished[0]
    value = finish(finished)
    event = {"cwd": str(root), "hook_event_name": "PreToolUse", "tool_name": "StructuredOutput", "tool_input": value}
    assert outcome.guard(event) == value
    for changes in [{"receipt_sha256": "f" * 64}, {"run_id": "other"}, {"check_exit": 1}, {"action": "failed"}]:
        event["tool_input"] = value | changes
        with pytest.raises(ValueError):
            outcome.guard(event)


def test_receipt_cannot_change_saved_result_while_retaining_its_hash(finished):
    root = finished[0]
    finish(finished)
    path = root / outcome.RECEIPT
    receipt = json.loads(path.read_text())
    receipt["result"]["summary"] = "A changed success claim."
    path.write_text(json.dumps(receipt))
    with pytest.raises(ValueError, match="completion hash"):
        outcome.verify_completion(root, finished[-1])


@pytest.mark.parametrize("fault", ["result", "receipt", "progress", "input", "deleted_receipt"])
def test_trusted_receipt_check_rejects_changed_outputs_and_inputs(finished, fault):
    root, _, _, _, output = finished
    finish(finished)
    p = {
        "result": output,
        "receipt": root / outcome.RECEIPT,
        "progress": root / state.PROGRESS,
        "input": root / state.INPUT,
        "deleted_receipt": root / outcome.RECEIPT,
    }[fault]
    if fault == "deleted_receipt":
        p.unlink()
    else:
        p.write_text(p.read_text() + "\n ")
    if fault == "receipt":
        # Whitespace is immaterial to the receipt's data, but not its outer hash.
        value = json.loads(p.read_text())
        value["result"]["completion"]["receipt_id"] = "f" * 32
        p.write_text(json.dumps(value))
    with pytest.raises(ValueError):
        outcome.verify_completion(root, output)


def native(root, value, status="completed", returned=True):
    directory = root / "native/subagents/workflows/wf_unit"
    directory.mkdir(parents=True, exist_ok=True)
    journal = directory / "journal.jsonl"
    rows = [{"type": "launched"}, {"type": "started", "key": "v2:final", "agentId": "a1", "label": "write result"}]
    if returned:
        rows.append({"type": "result", "key": "v2:final", "agentId": "a1", "result": value})
    journal.write_text("\n".join(map(json.dumps, rows)) + "\n")
    state_path = root / "native/workflows/wf_unit.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(
        json.dumps(
            {
                "runId": "wf_unit",
                "status": status,
                "result": value,
                "workflowProgress": [{"agentId": "a1", "state": "done"}],
            }
        )
    )
    return state_path, journal


@pytest.mark.parametrize("status", ["failed", "killed", "running"])
def test_cli_zero_never_overrules_failed_cancelled_or_stalled_runtime(finished, status):
    root = finished[0]
    value = finish(finished)
    state_path, journal = native(root, value, status)
    observed = outcome.observe(root, state_path, journal, 0, finished[-1])
    assert not observed["runtime_verified"] and observed["business_completed"]
    assert observed["retry_work"] is False


def test_completed_runtime_with_null_or_unjournaled_result_is_unverified(finished):
    root = finished[0]
    finish(finished)
    state_path, journal = native(root, None)
    assert not outcome.observe(root, state_path, journal, 0)["runtime_verified"]
    state_path, journal = native(root, finish(finished), returned=False)
    assert not outcome.observe(root, state_path, journal, 0)["runtime_verified"]
    journal.write_text(journal.read_text() + '{"type":')
    assert not outcome.observe(root, state_path, journal, 0)["runtime_verified"]


def test_reporting_failure_preserves_completion_and_restores_same_receipt(finished):
    root = finished[0]
    value = finish(finished)
    state_path, journal = native(root, value)
    observed = outcome.observe(root, state_path, journal, 1, finished[-1])
    assert observed["runtime_verified"] and observed["outcome"] == "completed_with_reporting_error"
    assert observed["retry_work"] is False
    before = (root / state.PROGRESS).read_bytes()
    finished[-1].unlink()  # fresh Fullsend iteration empties output; progress persists
    restored = finish(finished, [("workflow", "")])
    assert restored == value
    assert (root / state.PROGRESS).read_bytes() == before
    assert outcome.observe(root, state_path, journal, 1, finished[-1])["runtime_verified"]


def test_final_receipt_and_status_cli_are_read_only_for_completed_steps(finished):
    root = finished[0]
    finish(finished)
    before = (root / state.PROGRESS).read_bytes()
    process = subprocess.run(
        [sys.executable, str(ROOT / "scripts/strat_pipeline_state.py"), "completion-status"],
        cwd=root,
        capture_output=True,
        text=True,
    )
    assert process.returncode == 0 and json.loads(process.stdout)["work_complete"]
    assert (root / state.PROGRESS).read_bytes() == before
