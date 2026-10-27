"""Real receipt provenance and preservation checks, including failure cases."""
import itertools
import json

import pytest
from review_composition_support import composition, prepare


@pytest.fixture
def target(tmp_path):
    key, rfe = "STRAT-9501", "RHAIRFE-9501"
    path = tmp_path / "artifacts/strat-tasks" / f"{key}.md"
    path.parent.mkdir(parents=True)
    path.write_text(f"---\nstrat_id: {key}\nsource_rfe: {rfe}\n---\n## Strategy\nAuthored strategy.\n")
    return tmp_path, key, rfe


def test_numeric_policy_matches_existing_rubric_for_all_score_vectors():
    from parse_results import compute_verdict
    for values in itertools.product(range(3), repeat=4):
        scores = dict(zip(composition.ROLES, values), total=sum(values))
        old = compute_verdict({role.title(): scores[role] for role in composition.ROLES} | {"Total": sum(values)})
        assert composition.numeric_decision(scores) == {"recommendation": old[0].lower(), "needs_attention": old[1]}


def test_numeric_approval_preserves_opposing_specialist_recommendations(target):
    root, key, rfe = target
    _, reports = prepare(root, key, rfe, (2, 2, 2, 2), {"feasibility": "approve", "testability": "revise",
                                                      "scope": "reject", "architecture": "approve"})
    manifest = composition.verify_review(root, key)
    assert manifest["decision"] == {"recommendation": "approve", "needs_attention": False}
    review = (root / manifest["review"]["path"]).read_text()
    for role, event in reports.items():
        assert event["tool_response"]["result"] in review
        assert manifest["reports"][role]["agent_id"] == event["tool_response"]["agentId"]
        assert manifest["reports"][role]["tool_use_id"] == event["tool_use_id"]
    assert "testability: revise (numeric: approve)" in review
    assert "scope: reject (numeric: approve)" in review
    assert "separate numeric assessor" in review


def test_architecture_skip_is_explicit_and_not_invented_approval(target):
    root, key, rfe = target
    prepare(root, key, rfe, (2, 2, 2, 2), {**dict.fromkeys(composition.ROLES, "approve"), "architecture": "skipped"})
    manifest = composition.verify_review(root, key)
    assert manifest["reports"]["architecture"]["recommendation"] == "skipped"
    assert manifest["decision"]["needs_attention"] is False


@pytest.mark.parametrize("role,verdict", [
    *itertools.product(composition.ROLES, ("approve", "revise", "reject")),
    ("architecture", "skipped"),
])
def test_agreeing_recommendations_survive_capture_composition_and_host_verification(target, role, verdict):
    root, key, rfe = target
    _, reports = prepare(root, key, rfe, (2, 2, 2, 2), finish=False)
    report = reports[role]["tool_response"]["result"].replace(
        "**Recommendation**: approve", f"**Recommendation**: {verdict}")
    report += f"\n\nRequested changes and supporting findings remain verbatim.\n\n**Recommendation**: {verdict}\n"
    reports[role]["tool_response"]["result"] = report
    for event in reports.values():
        composition.capture(event)
    composition.compose(root, key)
    manifest = composition.verify_review(root, key)
    source = manifest["reports"][role]
    assert source["recommendation"] == verdict
    assert source["sha256"] == composition.digest(report)
    assert (root / source["path"]).read_bytes() == report.encode()
    assert report in (root / manifest["review"]["path"]).read_text()
    assert manifest["decision"] == {"recommendation": "approve", "needs_attention": False}


@pytest.mark.parametrize("verdicts", [
    *itertools.permutations(("approve", "revise", "reject"), 2),
    ("approve", "approve", "reject"),
    (), ("pending",), ("<approve / revise / reject>",),
    ("revise", ""), ("revise", "pending"), ("revise", "REVISE"),
    ("skipped", "skipped"),
])
def test_missing_invalid_or_conflicting_recommendations_are_not_captured(target, verdicts):
    root, key, rfe = target
    started, reports = prepare(root, key, rfe, (2, 2, 2, 2), finish=False)
    reports["feasibility"]["tool_response"]["result"] = (
        f"### {key}: Authored strategy\nFeasibility findings for {rfe}.\n" +
        "\n".join(f"**Recommendation**: {value}" for value in verdicts))
    for role in composition.ROLES[1:]:
        composition.capture(reports[role])
    with pytest.raises(ValueError, match="consistent Recommendation"):
        composition.capture(reports["feasibility"])
    session = root / "tmp/review-composition" / key / started["session_id"]
    assert not (session / "feasibility.json").exists()
    with pytest.raises(ValueError, match="Missing regular input"):
        composition.compose(root, key)


def test_agreeing_recommendation_values_allow_crlf_and_trailing_whitespace(target):
    _, key, rfe = target
    report = (f"### {key}: Authored strategy\r\nFindings for {rfe}.\r\n"
              "**Recommendation**:\trevise  \r\nMore findings.\r\n**Recommendation**: revise\r\n")
    assert composition.report_verdict({"key": key, "source_rfe": rfe}, "feasibility", report) == "revise"


def test_missing_report_cannot_be_replaced_by_scorer_feedback(target):
    root, key, rfe = target
    _, reports = prepare(root, key, rfe, (2, 2, 2, 2), finish=False)
    for role in composition.ROLES[:-1]:
        composition.capture(reports[role])
    with pytest.raises(ValueError, match="Missing regular input"):
        composition.compose(root, key)


@pytest.mark.parametrize("fault", ["acknowledgement", "wrong_target", "wrong_caller", "unforked", "wrong_command"])
def test_bad_receipts_do_not_become_source_reports(target, fault):
    root, key, rfe = target
    _, reports = prepare(root, key, rfe, (1, 1, 1, 1), finish=False)
    event = reports["feasibility"]
    if fault == "acknowledgement":
        event["tool_response"]["result"] = "Skill execution completed"
    elif fault == "wrong_target":
        event["tool_response"]["result"] = event["tool_response"]["result"].replace(key, "STRAT-9502")
    elif fault == "wrong_caller":
        event["agent_id"] = "another-caller"
    elif fault == "unforked":
        event["tool_response"]["status"] = "loaded"
    else:
        event["tool_response"]["commandName"] = "strategy-testability-review"
    with pytest.raises(ValueError):
        composition.capture(event)


def test_old_caller_cannot_fill_a_new_session(target):
    root, key, rfe = target
    _, old = prepare(root, key, rfe, (1, 1, 1, 1), finish=False)
    prepare(root, key, rfe, (1, 1, 1, 1), finish=False)
    event = old["feasibility"]
    event["agent_id"] = "previous-workflow-agent"
    with pytest.raises(ValueError, match="another caller"):
        composition.capture(event)


@pytest.mark.parametrize("fault", ["final_body", "role_body", "role_swap", "flag", "source", "scorer"])
def test_host_verification_rejects_changed_composition(target, fault):
    root, key, rfe = target
    prepare(root, key, rfe, (1, 1, 1, 1))
    directory = root / "artifacts/strat-reviews"
    if fault == "source":
        path = root / "artifacts/strat-originals" / f"{rfe}.md"
    else:
        suffix = {"final_body": "", "role_body": "-testability", "scorer": "-scorer",
                  "flag": "", "role_swap": "-provenance"}[fault]
        path = directory / f"{key}-review{suffix}.{'json' if fault == 'role_swap' else 'md'}"
    if fault == "role_swap":
        manifest = json.loads(path.read_text())
        manifest["reports"]["scope"]["path"] = manifest["reports"]["feasibility"]["path"]
        path.write_text(json.dumps(manifest))
    elif fault == "flag":
        path.write_text(path.read_text().replace("needs_attention: true", "needs_attention: false"))
    else:
        path.write_text(path.read_text() + "\nChanged content.\n")
    with pytest.raises(ValueError):
        composition.verify_review(root, key)


def test_different_targets_have_independent_sessions(target):
    root, key, rfe = target
    other = root / "artifacts/strat-tasks/STRAT-9502.md"
    other.write_text("---\nstrat_id: STRAT-9502\nsource_rfe: RHAIRFE-9502\n---\n## Strategy\nDifferent proposal.\n")
    first, reports1 = prepare(root, key, rfe, (2, 2, 2, 2), finish=False)
    second, reports2 = prepare(root, "STRAT-9502", "RHAIRFE-9502", (1, 1, 1, 1), finish=False)
    assert first["session_id"] != second["session_id"]
    for role in composition.ROLES:
        composition.capture(reports2[role])
        composition.capture(reports1[role])
    assert composition.compose(root, key)["needs_attention"] is False
    assert composition.compose(root, "STRAT-9502")["needs_attention"] is True


def test_begin_receipt_is_bound_even_when_shell_includes_other_log_lines(target):
    root, key, rfe = target
    started, _ = prepare(root, key, rfe, (1, 1, 1, 1), finish=False)
    directory = root / "tmp/review-composition" / key
    path = directory / started["session_id"] / "session.json"
    session = json.loads(path.read_text())
    session["owner"] = None
    path.write_text(json.dumps(session))
    event = {"cwd": str(root), "hook_event_name": "PostToolUse", "tool_name": "Bash",
             "session_id": "new-session", "agent_id": "new-caller",
             "tool_response": {"stdout": "Scores parsed.\n" + json.dumps(started) + "\nDone.\n"}}
    composition.capture(event)
    assert json.loads(path.read_text())["owner"]["agent_id"] == "new-caller"


def test_multiple_begin_receipts_cannot_choose_an_arbitrary_owner(target):
    root, key, rfe = target
    started, _ = prepare(root, key, rfe, (1, 1, 1, 1), finish=False)
    event = {"cwd": str(root), "hook_event_name": "PostToolUse", "tool_name": "Bash",
             "session_id": "new-session", "agent_id": "new-caller",
             "tool_response": {"stdout": json.dumps(started) + "\n" + json.dumps(started)}}
    with pytest.raises(ValueError, match="One begin"):
        composition.capture(event)
