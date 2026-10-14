"""Unit tests for scripts/strat_pipeline_state.py, the Workflow's run-state helper."""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "strat_pipeline_state.py"


def _run(repo, *args, check=True):
    env = {**os.environ, "FULLSEND_OUTPUT_DIR": str(repo / "output")}
    proc = subprocess.run([sys.executable, str(SCRIPT), *args], cwd=repo, env=env,
                          capture_output=True, text=True)
    if check:
        assert proc.returncode == 0, proc.stderr
    return proc


def _task(repo, strat, rfe):
    path = repo / "artifacts" / "strat-tasks" / f"{strat}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\nstrat_id: {strat}\nsource_rfe: {rfe}\njira_key: {strat}\ntitle: T\n"
                    "priority: Major\nstatus: Refined\n---\n## Strategy\nBody.\n")


def _review(repo, strat, recommendation):
    path = repo / "artifacts" / "strat-reviews" / f"{strat}-review.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    attention = "false" if recommendation == "approve" else "true"
    path.write_text(f"---\nstrat_id: {strat}\nrecommendation: {recommendation}\n"
                    f"needs_attention: {attention}\n---\nReview.\n")


@pytest.fixture
def repo(tmp_path):
    (tmp_path / "tmp").mkdir()
    (tmp_path / "tmp" / "strat-input.json").write_text(json.dumps({
        "run_id": "r1", "mode": "resume", "dry_run": False,
        "input_keys": ["RHAIRFE-1", "RHAIRFE-2"], "acquired_keys": ["RHAIRFE-1", "RHAIRFE-2"],
        "skipped": [], "resume_points": {"RHAIRFE-1": "review", "RHAIRFE-2": "create"},
        "architecture_context": "available"}))
    return tmp_path


def test_read_reports_review_only_rfes(repo):
    data = json.loads(_run(repo, "read").stdout)
    assert data["review_only"] == ["RHAIRFE-1"]
    assert data["create_done"] is False and data["strategies"] == []


def test_review_only_strategy_needs_no_refine_or_push(repo):
    _task(repo, "RHAISTRAT-1", "RHAIRFE-1")
    _task(repo, "RHAISTRAT-2", "RHAIRFE-2")
    created = json.loads(_run(repo, "record-create").stdout)
    assert [p["strat"] for p in created["strategies"]] == ["RHAISTRAT-1", "RHAISTRAT-2"]
    _run(repo, "mark", "refine", "RHAISTRAT-2")
    _run(repo, "mark", "push")
    _review(repo, "RHAISTRAT-1", "approve")
    _review(repo, "RHAISTRAT-2", "revise")
    _run(repo, "record-review", "RHAISTRAT-1")
    _run(repo, "record-review", "RHAISTRAT-2")
    _run(repo, "result")
    result = json.loads((repo / "output" / "agent-result.json").read_text())
    assert result["action"] == "completed", result["errors"]
    assert result["completed_phases"] == ["create", "refine", "push", "review"]


def test_missing_steps_become_errors(repo):
    _task(repo, "RHAISTRAT-2", "RHAIRFE-2")
    _run(repo, "record-create")
    _run(repo, "result", "--failed", "refine:RHAISTRAT-2")
    result = json.loads((repo / "output" / "agent-result.json").read_text())
    assert result["action"] == "failed"
    assert result["errors"] == [
        "refine: RHAISTRAT-2 returned no result",
        "create: no strategy recorded for RHAIRFE-1",
        "refine: RHAISTRAT-2 not recorded",
        "review: RHAISTRAT-2 not recorded",
    ]


def test_progress_from_another_run_is_ignored(repo):
    (repo / "tmp" / "strat-progress.json").write_text(json.dumps(
        {"run_id": "old", "create": {"strategies": [{"rfe": "RHAIRFE-2", "strat": "RHAISTRAT-9"}]},
         "refine": {}, "push": None, "review": {}}))
    assert json.loads(_run(repo, "read").stdout)["create_done"] is False


def test_rejects_bad_keys_and_failed_values(repo):
    assert _run(repo, "mark", "refine", "../etc", check=False).returncode == 1
    assert _run(repo, "result", "--failed", "rm -rf:x", check=False).returncode == 1


def test_parallel_marks_are_not_lost(repo):
    env = {**os.environ, "FULLSEND_OUTPUT_DIR": str(repo / "output")}
    keys = [f"RHAISTRAT-{n}" for n in range(1, 13)]
    data = json.loads((repo / "tmp/strat-input.json").read_text())
    data["acquired_keys"] = [f"RHAIRFE-{n}" for n in range(1, 13)]
    (repo / "tmp/strat-input.json").write_text(json.dumps(data))
    for n, key in enumerate(keys, 1):
        _task(repo, key, f"RHAIRFE-{n}")
    _run(repo, "record-create")
    procs = [subprocess.Popen([sys.executable, str(SCRIPT), "mark", "refine", k], cwd=repo, env=env,
                              stdout=subprocess.DEVNULL, stderr=subprocess.PIPE) for k in keys]
    assert all(p.wait() == 0 for p in procs)
    progress = json.loads((repo / "tmp" / "strat-progress.json").read_text())
    assert sorted(progress["refine"]) == sorted(keys)


def test_partial_create_stays_open_for_the_missing_rfes(repo):
    _task(repo, "RHAISTRAT-2", "RHAIRFE-2")
    created = json.loads(_run(repo, "record-create").stdout)
    assert created["pending"] == ["RHAIRFE-1"]
    state = json.loads(_run(repo, "read").stdout)
    assert state["create_done"] is False and state["pending_create"] == ["RHAIRFE-1"]
    _task(repo, "RHAISTRAT-1", "RHAIRFE-1")
    _run(repo, "record-create")
    state = json.loads(_run(repo, "read").stdout)
    assert state["create_done"] is True and state["pending_create"] == []
    assert [p["strat"] for p in state["strategies"]] == ["RHAISTRAT-1", "RHAISTRAT-2"]


def test_push_covers_only_what_the_push_script_pushes(repo):
    _task(repo, "RHAISTRAT-2", "RHAIRFE-2")
    path = repo / "artifacts" / "strat-tasks" / "RHAISTRAT-2.md"
    _run(repo, "record-create")
    _run(repo, "mark", "refine", "RHAISTRAT-2")
    path.write_text(path.read_text().replace("status: Refined", "status: Draft"))
    _run(repo, "mark", "push")
    state = json.loads(_run(repo, "read").stdout)
    assert state["pushed"] == []
    _run(repo, "result")
    errors = json.loads((repo / "output" / "agent-result.json").read_text())["errors"]
    assert "push: RHAISTRAT-2 not recorded" in errors


def test_an_unexpected_create_skip_stays_pending(repo):
    data = json.loads((repo / "tmp" / "strat-input.json").read_text())
    data["expected_skips"] = [{"key": "RHAIRFE-2", "reason": "quality: no quality label"}]
    (repo / "tmp" / "strat-input.json").write_text(json.dumps(data))
    skipped = repo / "artifacts" / "strat-skipped.md"
    skipped.parent.mkdir(parents=True, exist_ok=True)
    skipped.write_text("| RFE Key | Title | Reason | Run |\n|---|---|---|---|\n"
                       "| RHAIRFE-1 | T | made up | r |\n| RHAIRFE-2 | T | quality | r |\n")
    created = json.loads(_run(repo, "record-create").stdout)
    assert created["pending"] == ["RHAIRFE-1"]
    state = json.loads(_run(repo, "read").stdout)
    assert state["create_done"] is False and state["pending_create"] == ["RHAIRFE-1"]


def test_push_does_not_cover_a_task_the_push_script_rejects(repo):
    _task(repo, "RHAISTRAT-2", "RHAIRFE-2")
    path = repo / "artifacts" / "strat-tasks" / "RHAISTRAT-2.md"
    _run(repo, "record-create")
    _run(repo, "mark", "refine", "RHAISTRAT-2")
    path.write_text(path.read_text().replace("title: T\n", ""))  # fails the strat-task schema
    out = json.loads(_run(repo, "mark", "push").stdout)
    assert out == {"recorded": "push", "strategies": []}


@pytest.mark.parametrize("change", [
    ("status: Refined", "status: Draft"),
    ("source_rfe: RHAIRFE-2", "source_rfe: RHAIRFE-9"),
    ("strat_id: RHAISTRAT-2", "strat_id: RHAISTRAT-9"),
    ("jira_key: RHAISTRAT-2", "jira_key: RHAISTRAT-9"),
    ("## Strategy\nBody.", "## Strategy\n<!-- To be filled by /strategy-refine -->"),
    ("title: T\n", ""),
])
def test_refine_mark_rejects_unfinished_or_wrong_artifacts(repo, change):
    _task(repo, "RHAISTRAT-2", "RHAIRFE-2")
    _run(repo, "record-create")
    path = repo / "artifacts/strat-tasks/RHAISTRAT-2.md"
    path.write_text(path.read_text().replace(*change))
    assert _run(repo, "mark", "refine", "RHAISTRAT-2", check=False).returncode != 0
    assert json.loads(_run(repo, "refine-status").stdout)["refined"] == []


def test_only_valid_saved_completion_is_ready(repo):
    _task(repo, "RHAISTRAT-2", "RHAIRFE-2")
    _run(repo, "record-create")
    missing = json.loads(_run(repo, "refine-status").stdout)
    assert missing["refined"] == []
    assert missing["rejected"] == [{"key": "RHAISTRAT-2", "reason": "not_recorded"}]
    _run(repo, "mark", "refine", "RHAISTRAT-2")
    done = json.loads(_run(repo, "refine-status").stdout)
    assert done["refined"] == ["RHAISTRAT-2"] and done["rejected"] == []
    path = repo / "artifacts/strat-tasks/RHAISTRAT-2.md"
    path.write_text(path.read_text().replace("status: Refined", "status: Draft"))
    stale = json.loads(_run(repo, "refine-status").stdout)
    assert stale["refined"] == []
    assert stale["rejected"] == [{"key": "RHAISTRAT-2", "reason": "invalid_artifact"}]
    assert json.loads(_run(repo, "read").stdout)["refined"] == []


def test_unknown_target_and_arbitrary_record_are_not_completion(repo):
    _task(repo, "RHAISTRAT-2", "RHAIRFE-2")
    assert _run(repo, "mark", "refine", "RHAISTRAT-2", check=False).returncode != 0
    _run(repo, "record-create")
    path = repo / "tmp/strat-progress.json"
    progress = json.loads(path.read_text())
    progress["refine"]["RHAISTRAT-2"] = '{"recorded":"RHAISTRAT-2"}'
    path.write_text(json.dumps(progress))
    assert json.loads(_run(repo, "refine-status").stdout)["refined"] == []


def test_readiness_hook_rejects_model_invented_completion(repo, monkeypatch):
    import importlib.util
    spec = importlib.util.spec_from_file_location("guard", SCRIPT.with_name("refine_checkpoint_guard.py"))
    guard = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(guard)
    monkeypatch.syspath_prepend(str(SCRIPT.parent))
    _task(repo, "RHAISTRAT-2", "RHAIRFE-2")
    _run(repo, "record-create")
    snapshot = json.loads(_run(repo, "refine-status").stdout)
    event = {"cwd": str(repo), "hook_event_name": "PreToolUse", "tool_name": "StructuredOutput", "tool_input": snapshot}
    assert guard.guard(event)["verified"] == snapshot
    event["tool_input"] = {**snapshot, "refined": ["RHAISTRAT-2"], "rejected": []}
    with pytest.raises(ValueError):
        guard.guard(event)


def test_refuses_a_symlinked_score_dir(repo, tmp_path, monkeypatch):
    import importlib.util
    spec = importlib.util.spec_from_file_location("sps", SCRIPT)
    sps = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(sps)
    root = tmp_path / "assess"
    root.mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (root / "RHAISTRAT-1").symlink_to(elsewhere)
    monkeypatch.setattr(sps, "SCORE_ROOT", root)
    with pytest.raises(SystemExit):
        sps.prepare_score_dir("RHAISTRAT-1", clean=True)
    assert elsewhere.is_dir()
