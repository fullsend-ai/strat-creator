"""Regression cases from PR 85: real helper boundaries, no model requests."""
import copy
import importlib.util
import json
from pathlib import Path

import pytest
from review_composition_support import composition, prepare

ROOT = Path(__file__).resolve().parents[1]


def module(relative, name):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


def target(root, key="STRAT-9501"):
    rfe = key.replace("STRAT", "RHAIRFE")
    path = root / "artifacts/strat-tasks" / f"{key}.md"
    path.parent.mkdir(parents=True)
    path.write_text(f"---\nstrat_id: {key}\nsource_rfe: {rfe}\n---\n## Strategy\nAuthored strategy.\n")
    return key, rfe


def test_crlf_reports_survive_capture_compose_and_trusted_verification(tmp_path):
    key, rfe = target(tmp_path)
    _, events = prepare(tmp_path, key, rfe, (2, 2, 2, 2), finish=False)
    original = events["feasibility"]["tool_response"]["result"].replace("\n", "\r\n")
    events["feasibility"]["tool_response"]["result"] = original
    for event in events.values():
        composition.capture(event)
    composition.compose(tmp_path, key)
    manifest = composition.verify_review(tmp_path, key)
    report = manifest["reports"]["feasibility"]
    assert (tmp_path / report["path"]).read_bytes() == original.encode()
    assert original.encode() in (tmp_path / manifest["review"]["path"]).read_bytes()


def test_provisional_review_is_not_mistaken_for_completed_review(tmp_path):
    from publish_review import publish
    key, rfe = target(tmp_path)
    _, events = prepare(tmp_path, key, rfe, (2, 2, 2, 2), finish=False)
    assert publish(tmp_path, key, check_only=True) is None
    # A death after all captures can recover composition without new role calls.
    for event in events.values():
        composition.capture(event)
    assert composition.recover(tmp_path, key)["recommendation"] == "approve"
    assert publish(tmp_path, key, check_only=True)["recommendation"] == "approve"
    composition.recover(tmp_path, key)
    assert len(list((tmp_path / "tmp/review-composition" / key).glob("*/session.json"))) == 1


def test_completed_review_missing_provenance_is_not_regenerated(tmp_path):
    from publish_review import publish
    key, rfe = target(tmp_path)
    prepare(tmp_path, key, rfe, (2, 2, 2, 2))
    (tmp_path / "artifacts/strat-reviews" / f"{key}-review-provenance.json").unlink()
    with pytest.raises(ValueError, match="completed review has no provenance"):
        publish(tmp_path, key, check_only=True)


def test_adf_readback_ignores_only_server_ids_and_default_presentation():
    from jira_utils import canonical_adf, markdown_to_adf, same_jira_value
    expected = markdown_to_adf("| Parameter | Value |\n| --- | --- |\n| limit | 10 |")
    normalized = copy.deepcopy(expected)
    table = normalized["content"][0]
    table["attrs"] = {"localId": "server-generated", "layout": "default", "isNumberColumnEnabled": False}
    table["content"][0]["content"][0]["attrs"] = {"colspan": 1, "rowspan": 1, "colwidth": None}
    assert same_jira_value(normalized, expected)
    table["content"][0]["content"][0]["attrs"]["colspan"] = 2
    assert not same_jira_value(normalized, expected)
    linked = markdown_to_adf("[Reference](https://example.test/original)")
    other = copy.deepcopy(linked)
    other["content"][0]["content"][0]["marks"][0]["attrs"]["href"] = "https://example.test/different"
    assert canonical_adf(linked) != canonical_adf(other)


def test_partial_source_cleanup_recovers_only_matching_sealed_content(tmp_path, monkeypatch):
    import subprocess
    publisher = module(".fullsend/shared/publish/push-results.py", "pr85_publisher")
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    tasks = tmp_path / "strat-tasks"
    tasks.mkdir()
    (tasks / "one.md").write_text("First immutable strategy.")
    (tasks / "two.md").write_text("Second immutable strategy.")
    original = publisher.shutil.rmtree

    def interrupted(path):
        (path / "one.md").unlink()
        raise OSError("authored interruption during cleanup")

    monkeypatch.setattr(publisher.shutil, "rmtree", interrupted)
    with pytest.raises(OSError, match="authored interruption"):
        publisher.organize_identified_run(tmp_path, "r1")
    monkeypatch.setattr(publisher.shutil, "rmtree", original)
    run = publisher.organize_identified_run(tmp_path, "r1")
    assert (run / "strat-tasks/one.md").read_text() == "First immutable strategy."
    assert (run / "strat-tasks/two.md").read_text() == "Second immutable strategy."
    assert not tasks.exists()


@pytest.mark.parametrize("dry_run", [True, False])
def test_skip_only_bundle_is_explicit_and_retains_dry_run(tmp_path, dry_run):
    publication = module(".fullsend/shared/prepare_publication.py", "pr85_bundle")
    handoff = tmp_path / "handoff"
    handoff.mkdir()
    (handoff / "strat-skipped.md").write_text(
        "| RFE | Summary | Reason |\n| --- | --- | --- |\n| RHAIRFE-1 | T | Gate |\n")
    result = tmp_path / "agent-result.json"
    result.write_text(json.dumps({"dry_run": dry_run}))
    bundle = tmp_path / "bundle"
    publication.prepare(handoff, bundle, result, "batch")
    data = json.loads((bundle / "pipeline-data.json").read_text())
    assert data["dry_run"] is dry_run
    assert data["strategies"] == [] and data["stats"]["total"] == 0
    before = (bundle / "pipeline-data.json").read_bytes()
    publication.prepare(handoff, bundle, result, "batch")
    assert (bundle / "pipeline-data.json").read_bytes() == before


def test_failed_handoff_retains_progress_without_following_symlinks(tmp_path):
    recovery = module(".fullsend/shared/capture_failure.py", "pr85_failure")
    repo, state = tmp_path / "repo", tmp_path / "host"
    (repo / "tmp").mkdir(parents=True)
    (repo / "tmp/strat-progress.json").write_text('{"run_id":"failed-run"}')
    outside = tmp_path / "outside.txt"
    outside.write_text("Unrelated file.")
    (repo / "tmp/foreign").symlink_to(outside)
    snapshot = recovery.capture(repo, state, tmp_path / "missing-result.json")
    assert json.loads((snapshot / "tmp/strat-progress.json").read_text())["run_id"] == "failed-run"
    assert not (snapshot / "tmp/foreign").exists()
    assert set(json.loads((snapshot / "manifest.json").read_text())["files"]) == {"tmp/strat-progress.json"}


def test_runtime_qualification_rejects_untested_versions():
    from check_claude_runtime import check
    assert check("2.1.296 (Claude Code)") == "2.1.296"
    for version in ["2.1.286", "2.1.295", "2.1.300", "unknown"]:
        with pytest.raises(ValueError):
            check(version)


def test_short_refinement_hook_never_requests_live_jira(tmp_path, monkeypatch):
    import refine_checkpoint_guard
    import strat_freshness
    from strat_side_effects import digest
    data = {"run_id": "r1", "mode": "batch", "acquired_keys": []}
    strat_freshness.initialize(tmp_path, data)
    data["freshness"].update(live=True, server=digest("https://jira.example.test"), rfes={})
    (tmp_path / "tmp").mkdir()
    (tmp_path / "tmp/strat-input.json").write_text(json.dumps(data))
    monkeypatch.setenv("JIRA_SERVER", "https://jira.example.test")
    monkeypatch.setenv("JIRA_USER", "fixture")
    monkeypatch.setenv("JIRA_TOKEN", "fixture")
    monkeypatch.chdir(tmp_path)

    def forbidden(*args, **kwargs):
        raise AssertionError("Hook attempted a live Jira call")

    monkeypatch.setattr(strat_freshness, "jira_rfe", forbidden)
    monkeypatch.setattr(strat_freshness, "jira_human", forbidden)
    monkeypatch.setattr(strat_freshness, "jira_published", forbidden)
    result = refine_checkpoint_guard.guard({"hook_event_name": "PreToolUse", "tool_name": "StructuredOutput",
        "cwd": str(tmp_path), "tool_input": {"kind": "strat-refine-readiness", "run_id": "r1",
                                            "refined": [], "rejected": []}})
    assert result["verified"]["run_id"] == "r1"


@pytest.mark.parametrize("active", [False, True])
@pytest.mark.parametrize("error_type", ["parser", "scope"])
def test_stop_hook_errors_allow_at_most_one_correction(monkeypatch, capsys, active, error_type):
    import io
    import sys

    import review_scope
    error = ValueError("broken parser") if error_type == "parser" else review_scope.ScopeError("bad report")
    event = {"hook_event_name": "SubagentStop", "stop_hook_active": active}
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(event)))
    def failed(*_):
        raise error
    monkeypatch.setattr(review_scope, "guard", failed)
    monkeypatch.setattr(review_scope, "audit", failed)
    code = review_scope.main()
    output = capsys.readouterr()
    if active:
        assert code == 0
        assert "decision" not in json.loads(output.out)
    else:
        assert code == 2 or json.loads(output.out)["decision"] == "block"


def test_normalized_jira_write_readbacks_do_not_duplicate_comments_or_fields(tmp_path, monkeypatch):
    import jira_utils
    from strat_side_effects import UncertainWrite
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("STRAT_EFFECTS_DIR", str(tmp_path / "effects"))
    monkeypatch.setenv("STRAT_RUN_ID", "normalizing-response-fixture")
    expected = jira_utils.markdown_to_adf("[Policy](https://example.test/policy)")
    current = {"fields": None, "comments": []}
    writes = []
    def write(_server, path, _user, _token, *, body, method=None):
        writes.append((path, method))
        value = copy.deepcopy(body.get("body", body.get("fields", {}).get("description")))
        value["attrs"] = {"localId": "server-id"}
        if method == "PUT":
            current["fields"] = value
        else:
            current["comments"].append({"id": "10", "body": value})
    monkeypatch.setattr(jira_utils, "api_call", write)
    monkeypatch.setattr(jira_utils, "check_owner", lambda *_: None)
    monkeypatch.setattr(jira_utils, "get_comments", lambda *_: current["comments"])
    monkeypatch.setattr(jira_utils, "get_issue", lambda *_, **kw: {"fields": {"description": current["fields"]}})
    for _ in range(2):
        jira_utils.add_comment("https://jira.test", "fixture", "fixture", "RHAISTRAT-1", expected)
        jira_utils.set_fields_verified("https://jira.test", "fixture", "fixture", "RHAISTRAT-1",
                                      {"description": expected})
    assert len(writes) == 2 and len(current["comments"]) == 1
    current["fields"]["content"][0]["content"][0]["marks"][0]["attrs"]["href"] = "https://example.test/changed"
    assert not jira_utils.same_jira_value(current["fields"], expected)
    with pytest.raises(UncertainWrite):
        jira_utils.set_fields_verified("https://jira.test", "fixture", "fixture", "RHAISTRAT-1",
                                      {"description": expected})
    assert len(writes) == 2  # A meaningful edit is preserved, never overwritten.


def test_smoke_review_stubs_match_the_enforced_target_and_receipt_contract():
    import re

    from review_scope import check_report
    scope = {"key": "STRAT-9001", "source_rfe": "RHAIRFE-9001"}
    for role in composition.ROLES:
        source = ROOT / "tests/fixtures/workflow-smoke/skills" / f"strategy-{role}-review/SKILL.md"
        text = source.read_text()
        assert "agent: strat-reviewer" in text and "REVIEW_SCOPE_TARGET: $0" in text
        report = text[text.index("### KEY:"):].replace("KEY", scope["key"])
        check_report(scope, report)
        assert re.findall(r"^\*\*Recommendation\*\*:\s*(\w+)", report, re.M) == ["approve"]


def test_observed_native_fork_payload_is_consumed_without_transcript_recovery(tmp_path):
    fixture = ROOT / "tests/fixtures/native-hooks"
    event = json.loads((fixture / "post-forked-skill.json").read_text())
    key, rfe = target(tmp_path, event["tool_input"]["args"])
    _, expected = prepare(tmp_path, key, rfe, (2, 2, 2, 2), finish=False)
    assert event["tool_response"]["status"] == "forked"
    assert event["tool_input"]["args"] == key
    event.update({name: expected["feasibility"][name] for name in ("cwd", "session_id", "agent_id")})
    composition.capture(event)
    for role in composition.ROLES[1:]:
        composition.capture(expected[role])
    composition.compose(tmp_path, key)
    manifest = composition.verify_review(tmp_path, key)
    exported = tmp_path / manifest["reports"]["feasibility"]["path"]
    assert exported.read_bytes().decode() == event["tool_response"]["result"]


def test_native_payload_fixture_version_and_short_hook_timeout_policy():
    from check_claude_runtime import QUALIFIED_VERSION
    fixture = ROOT / "tests/fixtures/native-hooks"
    assert json.loads((fixture / "provenance.json").read_text())["claude_version"] == QUALIFIED_VERSION
    for name in ("pre-workflow", "refinement-structured-output", "completion-structured-output"):
        event = json.loads((fixture / (name + ".json")).read_text())
        assert event["hook_event_name"] == "PreToolUse"
        assert event["tool_name"] in {"Workflow", "StructuredOutput"}
        assert event["cwd"] and event["session_id"] and isinstance(event["tool_input"], dict)
    hooks = json.loads((ROOT / "hooks/hooks.json").read_text())["hooks"]
    for name in ("PreToolUse", "PostToolUse"):
        assert all(hook.get("onFailure") == "block" for entry in hooks[name] for hook in entry["hooks"])
    stop = json.loads((fixture / "subagent-stop.json").read_text())
    assert "stop_hook_active" in stop and "last_assistant_message" in stop
