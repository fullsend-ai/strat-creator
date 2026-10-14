"""Ticket scope controls, including parallel identity and escaped input paths."""
import importlib.util
import json
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("review_scope", ROOT / "scripts/review_scope.py")
scope = importlib.util.module_from_spec(spec)
spec.loader.exec_module(scope)


def strategy(root, key, rfe, mode="artifacts"):
    path = root / mode / "strat-tasks" / f"{key}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\nstrat_id: {key}\nsource_rfe: {rfe}\n---\nStrategy.\n")
    rfe_dir = "strat-originals" if mode == "local" else "rfe-tasks"
    original = root / mode / rfe_dir / f"{rfe}.md"
    original.parent.mkdir(parents=True, exist_ok=True)
    original.write_text("Source RFE.\n")
    return path


@pytest.fixture
def batch(tmp_path):
    for number in (1, 2, 3):
        strategy(tmp_path, f"RHAISTRAT-{number}", f"RHAIRFE-{number}")
    directory = tmp_path / ".context/architecture-context"
    directory.mkdir(parents=True)
    (directory / "PLATFORM.md").write_text("Architecture.\n")
    return tmp_path, scope.inputs(tmp_path, "RHAISTRAT-1")


@pytest.mark.parametrize("arguments", ["", "RHAIRFE-1", "RHAISTRAT-1 RHAISTRAT-2", "RHAISTRAT-1 --all"])
def test_invalid_assignment_has_no_batch_fallback(arguments):
    with pytest.raises(scope.ScopeError):
        scope.assignment(f"{scope.MARKER}{arguments}\n")


def test_local_mode_depends_on_the_assigned_ticket(batch):
    root, _ = batch
    strategy(root, "RHAISTRAT-2", "RHAIRFE-2", mode="local")
    assert scope.inputs(root, "RHAISTRAT-1")["strategy"].parent == root / "artifacts/strat-tasks"
    assert scope.inputs(root, "RHAISTRAT-2")["rfe"].parent == root / "local/strat-originals"


@pytest.mark.parametrize("name,value", [
    ("Read", {"file_path": "artifacts/strat-tasks/RHAISTRAT-2.md"}),
    ("Read", {"file_path": "artifacts/rfe-tasks/RHAIRFE-2.md"}),
    ("Grep", {"path": "artifacts/strat-tasks", "pattern": "Strategy"}),
    ("Grep", {"pattern": "Strategy"}),
    ("Glob", {"path": "artifacts/strat-tasks", "pattern": "*.md"}),
    ("Glob", {"path": ".context/architecture-context", "pattern": "../../artifacts/**/*.md"}),
    ("Read", {"file_path": ".context/architecture-context/../../artifacts/strat-tasks/RHAISTRAT-2.md"}),
    ("Read", {"file_path": "/etc/passwd"}),
    ("StructuredOutput", {}),
])
def test_wrong_ticket_or_broad_search_is_blocked(batch, name, value):
    root, inputs = batch
    with pytest.raises(scope.ScopeError):
        scope.check_tool(root, inputs, name, value)


@pytest.mark.parametrize("name,value", [
    ("Read", {"file_path": "artifacts/strat-tasks/RHAISTRAT-1.md"}),
    ("Read", {"file_path": "artifacts/rfe-tasks/RHAIRFE-1.md"}),
    ("Read", {"file_path": "artifacts/strat-reviews/RHAISTRAT-1-review.md"}),
    ("Grep", {"path": "artifacts/strat-tasks/RHAISTRAT-1.md", "pattern": "Strategy"}),
    ("Grep", {"path": ".context/architecture-context", "pattern": "Architecture"}),
    ("Glob", {"path": ".context/architecture-context", "pattern": "**/*.md"}),
])
def test_assigned_inputs_and_shared_architecture_are_allowed(batch, name, value):
    root, inputs = batch
    scope.check_tool(root, inputs, name, value)


def test_symlink_cannot_expose_another_ticket_as_architecture(batch):
    root, inputs = batch
    path = root / ".context/architecture-context/peer.md"
    path.symlink_to(root / "artifacts/strat-tasks/RHAISTRAT-2.md")
    for name, value in [("Read", {"file_path": str(path)}),
                        ("Grep", {"path": str(path.parent), "pattern": "Strategy"})]:
        with pytest.raises(scope.ScopeError):
            scope.check_tool(root, inputs, name, value)


def test_mismatched_metadata_and_missing_source_fail_closed(batch):
    root, inputs = batch
    inputs["strategy"].write_text("---\nstrat_id: RHAISTRAT-2\nsource_rfe: RHAIRFE-2\n---\n")
    with pytest.raises(scope.ScopeError):
        scope.inputs(root, "RHAISTRAT-1")
    strategy(root, "RHAISTRAT-1", "RHAIRFE-1")
    inputs["rfe"].unlink()
    with pytest.raises(scope.ScopeError):
        scope.inputs(root, "RHAISTRAT-1")


def test_parallel_assignments_use_their_own_transcripts(batch):
    root, _ = batch
    main = root / "sessions/session.jsonl"
    directory = main.parent / main.stem / "subagents"
    directory.mkdir(parents=True)
    for key, agent in [("RHAISTRAT-1", "a111"), ("RHAISTRAT-2", "a222")]:
        (directory / f"agent-{agent}.jsonl").write_text(json.dumps({"type": "user", "message": {
            "content": f"{scope.MARKER}{key}\n"}}) + "\n")
        event = {"cwd": str(root), "transcript_path": str(main), "agent_id": agent,
                 "agent_type": "strat-creator:strat-reviewer", "hook_event_name": "PreToolUse",
                 "tool_name": "Read", "tool_input": {"file_path": f"artifacts/strat-tasks/{key}.md"}}
        assert scope.guard(event)["key"] == key
        event["tool_input"]["file_path"] = "artifacts/strat-tasks/RHAISTRAT-3.md"
        with pytest.raises(scope.ScopeError):
            scope.guard(event)


def test_first_tool_waits_for_the_asynchronous_assignment_flush(tmp_path):
    path = tmp_path / "agent-late.jsonl"
    def flush():
        time.sleep(0.05)
        path.write_text(json.dumps({"type": "user", "message": {
            "content": "REVIEW_SCOPE_TARGET: RHAISTRAT-1\n"}}) + "\n")
    writer = threading.Thread(target=flush)
    writer.start()
    try:
        assert scope.assigned_key({"agent_transcript_path": str(path)}) == "RHAISTRAT-1"
    finally:
        writer.join()


@pytest.mark.parametrize("report", ["Skill execution completed", "### RHAISTRAT-2: Wrong ticket",
    "### RHAISTRAT-1: First\n### RHAISTRAT-2: Second",
    "### RHAISTRAT-1: First\nAlso assesses RHAISTRAT-2.",
    "### RHAISTRAT-1: First\nBased on RHAIRFE-2."])
def test_foreign_or_missing_report_target_is_blocked(batch, report):
    with pytest.raises(scope.ScopeError):
        scope.check_report(batch[1], report)


def test_assigned_assessment_or_explicit_error_can_return(batch):
    scope.check_report(batch[1], "### RHAISTRAT-1: Feature\nRHAIRFE-1. Recommendation: revise")
    scope.check_report(batch[1], "REVIEW_SCOPE_ERROR: source unavailable")


def test_fullsend_bundles_the_same_scope_implementation_and_hooks():
    for relative in ("scripts/review_scope.py", "hooks/hooks.json"):
        assert (ROOT / relative).read_bytes() == (ROOT / ".fullsend/plugins/strat-pipeline" / relative).read_bytes()


def test_parent_is_not_restricted():
    assert scope.guard({"agent_type": "strat-scorer"}) is None
