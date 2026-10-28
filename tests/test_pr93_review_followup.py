"""Local reproductions of PR93 review findings, with no model calls."""

import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from jira_utils import markdown_to_adf  # noqa: E402
from test_batch_freshness_isolation import ROOT, command, edit, review  # noqa: E402
from test_batch_freshness_isolation import batch as batch_fixture  # noqa: E402

batch = batch_fixture


def push(root):
    response = subprocess.run(
        [sys.executable, str(ROOT / "scripts/push_refined_strategies.py")],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert response.returncode == 0, response.stderr
    command(root, "mark", "push")


def result(root):
    command(root, "result")
    value = json.loads((root / "output/agent-result.json").read_text())
    assert value["action"] == "completed", value["errors"]
    return value


@pytest.mark.parametrize("batch", ["before-push"], indirect=True)
def test_edit_between_push_and_label_continues_other_ticket(batch, monkeypatch):
    import push_refined_strategies as publisher

    root, _, jira = batch
    original = publisher.add_labels

    def edit_before_label(server, user, token, key, labels):
        if key == "RHAISTRAT-8901":
            jira.request(
                "PUT",
                "/rest/api/3/issue/RHAIRFE-8901",
                {"fields": {"description": markdown_to_adf("Changed after push, before label.")}},
            )
        return original(server, user, token, key, labels)

    monkeypatch.setattr(publisher, "add_labels", edit_before_label)
    monkeypatch.setattr(sys, "argv", ["push_refined_strategies.py"])
    assert publisher.main() == 0
    assert "strat-creator-auto-refined" in jira.get("RHAISTRAT-8902")["fields"]["labels"]
    command(root, "mark", "push")
    review(root, "RHAISTRAT-8902")
    value = result(root)
    assert [v["key"] for v in value["deferred"]] == ["RHAIRFE-8901"]


@pytest.mark.parametrize("batch", ["skipped-create"], indirect=True)
def test_recorded_skip_then_edit_is_only_deferred(batch):
    root, data, _ = batch
    (root / "artifacts/strat-tasks/RHAISTRAT-8902.md").unlink()
    (root / "artifacts/strat-skipped.md").write_text(
        "| RHAIRFE-8902 | release | " + data["expected_skips"][0]["reason"] + " |\n"
    )
    command(root, "record-create")
    command(root, "mark", "refine", "RHAISTRAT-8901")
    push(root)
    review(root, "RHAISTRAT-8901")
    edit(batch)
    value = result(root)
    assert not any(v["key"] == "RHAIRFE-8902" for v in value["skipped"])
    assert [v["key"] for v in value["deferred"]] == ["RHAIRFE-8902"]
    saved = json.loads((root / "tmp/strat-progress.json").read_text())
    assert saved["create"]["skipped"] == data["expected_skips"]  # Keep original evidence.
    handoff = root / "handoff"
    copied = subprocess.run(
        [
            sys.executable,
            str(ROOT / ".fullsend/shared/handoff.py"),
            str(root),
            str(handoff),
            str(root / "output/agent-result.json"),
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert copied.returncode == 0, copied.stderr
    prepared = subprocess.run(
        [
            sys.executable,
            str(ROOT / ".fullsend/shared/prepare_publication.py"),
            str(handoff),
            str(root / "bundle"),
            str(root / "output/agent-result.json"),
            "pipeline-settings",
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert prepared.returncode == 0, prepared.stderr
    assert "RHAIRFE-8902" not in (root / "bundle/strat-skipped.md").read_text()


@pytest.mark.parametrize("batch", ["before-create"], indirect=True)
def test_partial_create_pending_ticket_becomes_deferred(batch):
    root = batch[0]
    (root / "artifacts/strat-tasks/RHAISTRAT-8902.md").unlink()
    command(root, "record-create")
    recorded = json.loads((root / "tmp/strat-progress.json").read_text())["create"]
    assert recorded["pending"] == ["RHAIRFE-8902"] and recorded["done_at"] is None
    command(root, "mark", "refine", "RHAISTRAT-8901")
    push(root)
    review(root, "RHAISTRAT-8901")
    edit(batch)
    value = result(root)
    assert value["completed_phases"] == ["create", "refine", "push", "review"]


def test_second_change_preserves_deferral_and_receipt(batch):
    from strat_pipeline_outcome import verify_completion

    root, _, jira = batch
    jira.request(
        "PUT",
        "/rest/api/3/issue/RHAISTRAT-8902",
        {"fields": {"description": markdown_to_adf("## Staff Engineer / SME Input\nUse a shared queue.")}},
    )
    command(root, "ticket-status", "RHAISTRAT-8902")
    review(root, "RHAISTRAT-8901")
    value = result(root)
    first_reason = value["deferred"][0]["reason"]
    edit(batch)  # RFE now differs as well, changing the first observed error category.
    checked = verify_completion(root)["result"]
    assert checked["deferred"][0]["reason"] == first_reason


def test_reverted_rfe_stays_deferred_with_independent_history_proof(batch):
    from strat_pipeline_outcome import verify_completion

    root, _, jira = batch
    original = jira.get("RHAIRFE-8902")["fields"]["description"]
    edit(batch)
    command(root, "ticket-status", "RHAISTRAT-8902")
    jira.request("PUT", "/rest/api/3/issue/RHAIRFE-8902", {"fields": {"description": original}})
    status = json.loads(command(root, "ticket-status", "RHAISTRAT-8902").stdout)
    assert status["eligible"] is False
    review(root, "RHAISTRAT-8901")
    result(root)
    assert len(verify_completion(root)["result"]["strategies"]) == 1


def test_fully_deferred_report_lists_required_work(batch):
    root, data, jira = batch
    for key in data["acquired_keys"]:
        jira.request(
            "PUT",
            "/rest/api/3/issue/" + key,
            {"fields": {"description": markdown_to_adf("Changed requirements for " + key)}},
        )
    result(root)
    handoff = root / "empty-handoff"
    handoff.mkdir()
    spec = importlib.util.spec_from_file_location("publication", ROOT / ".fullsend/shared/prepare_publication.py")
    publication = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(publication)
    bundle = root / "bundle"
    publication.prepare(handoff, bundle, root / "output/agent-result.json", "pipeline-settings")
    html = (bundle / "reports/report.html").read_text()
    assert "No completed strategies" in html and "2 ticket" in html
    assert all(key in html for key in data["acquired_keys"])


def test_missing_strategy_and_refine_argument_report_clear_errors(batch):
    from strat_freshness import source_rfe

    root = batch[0]
    (root / "artifacts/strat-tasks/RHAISTRAT-8902.md").unlink()
    with pytest.raises(ValueError, match="freshness: .*missing or symlinked"):
        source_rfe(root, "RHAISTRAT-8902")
    response = command(root, "mark", "refine", check=False)
    assert "mark refine needs a strategy key" in response.stderr


@pytest.mark.skipif(shutil.which("node") is None, reason="Node required for offline Workflow execution")
@pytest.mark.parametrize("pending", [False, True])
def test_missing_create_handoff_reconciles_verified_eligible_inventory(tmp_path, pending):
    from test_strat_pipeline_logic import PAIRS, _completion, _full, _read, _run

    deferred = [] if pending else [{"key": "RHAIRFE-2", "reason": "RFE changed"}]
    snapshot = _read(
        create_done=not pending,
        pending_create=["RHAIRFE-2"] if pending else [],
        strategies=PAIRS[:1],
        deferred=deferred,
    )
    responses = _full(
        {
            "strategy-create": None,
            "reconcile creation": snapshot,
            "verify refinement progress": {
                "kind": "strat-refine-readiness",
                "run_id": "r1",
                "refined": ["RHAISTRAT-1"],
                "rejected": [],
            },
            "push": {"recorded": "push", "strategies": ["RHAISTRAT-1"]},
            "prepare reviews": {"reviewed": [], "deferred": deferred},
            "write result": _completion(
                action="failed" if pending else "completed",
                check_exit=1 if pending else 0,
                deferred_rfes=[] if pending else ["RHAIRFE-2"],
            ),
        }
    )
    value = _run(tmp_path, responses)
    assert "reconcile creation" in value["calls"]
    assert "review RHAISTRAT-1" in value["calls"]
    assert "review RHAISTRAT-2" not in value["calls"]


@pytest.mark.parametrize("field", ["sme", "signoff"])
def test_reverted_sme_or_signoff_has_external_history_proof(batch, field):
    root, _, jira = batch
    issue = jira.get("RHAISTRAT-8902")["fields"]
    changed = (
        {"description": markdown_to_adf("## Staff Engineer / SME Input\nUse a shared queue.")}
        if field == "sme"
        else {"labels": issue["labels"] + ["strat-creator-human-sign-off"]}
    )
    original = {key: issue[key] for key in changed}
    jira.request("PUT", "/rest/api/3/issue/RHAISTRAT-8902", {"fields": changed})
    assert not json.loads(command(root, "ticket-status", "RHAISTRAT-8902").stdout)["eligible"]
    jira.request("PUT", "/rest/api/3/issue/RHAISTRAT-8902", {"fields": original})
    assert not json.loads(command(root, "ticket-status", "RHAISTRAT-8902").stdout)["eligible"]
    review(root, "RHAISTRAT-8901")
    assert len(result(root)["strategies"]) == 1


def test_unavailable_history_cannot_authorize_a_reverted_deferral(batch, monkeypatch):
    import strat_history
    from strat_pipeline_outcome import verify_completion

    root, _, jira = batch
    original = jira.get("RHAIRFE-8902")["fields"]["description"]
    edit(batch)
    command(root, "ticket-status", "RHAISTRAT-8902")
    review(root, "RHAISTRAT-8901")
    result(root)
    jira.request("PUT", "/rest/api/3/issue/RHAIRFE-8902", {"fields": {"description": original}})

    def unavailable(*args, **kwargs):
        raise OSError("History unavailable")

    monkeypatch.setattr(strat_history, "histories", unavailable)
    with pytest.raises(ValueError, match="independent change evidence"):
        verify_completion(root)


def test_bookkeeping_history_cannot_authorize_a_forged_deferral(batch):
    root, _, jira = batch
    original = jira.get("RHAIRFE-8902")["fields"]["labels"]
    jira.request("PUT", "/rest/api/3/issue/RHAIRFE-8902", {"fields": {"labels": ["strat-creator-processing"]}})
    jira.request("PUT", "/rest/api/3/issue/RHAIRFE-8902", {"fields": {"labels": original}})
    path = root / "tmp/strat-progress.json"
    progress = json.loads(path.read_text())
    progress["deferred"] = [{"key": "RHAIRFE-8902", "reason": "Agent-claimed change"}]
    path.write_text(json.dumps(progress))
    command(root, "result")
    value = json.loads((root / "output/agent-result.json").read_text())
    assert value["action"] == "failed"
    assert any("independent change evidence" in error for error in value["errors"])


@pytest.mark.parametrize("batch", ["before-push"], indirect=True)
@pytest.mark.parametrize("fault", ["shared", "unreadable", "ownership", "unproven-ticket"])
def test_label_failures_without_independent_deferral_remain_fatal(batch, monkeypatch, fault):
    import push_refined_strategies as publisher
    from strat_freshness import StaleTicket
    from strat_side_effects import UncertainWrite

    errors = {
        "shared": ValueError("Shared source changed"),
        "unreadable": OSError("Jira unavailable"),
        "ownership": UncertainWrite("Foreign owner"),
        "unproven-ticket": StaleTicket("RHAIRFE-8901", "Unproven input-change claim"),
    }

    def fail(*args, **kwargs):
        raise errors[fault]

    monkeypatch.setattr(publisher, "add_labels", fail)
    monkeypatch.setattr(sys, "argv", ["push_refined_strategies.py"])
    with pytest.raises(type(errors[fault]), match=str(errors[fault])):
        publisher.main()
