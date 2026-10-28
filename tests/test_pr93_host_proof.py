"""Host exclusions need independent evidence; human gates stop only their ticket."""

import json
import subprocess
import sys

import pytest
from test_batch_freshness_isolation import ROOT, command, review
from test_batch_freshness_isolation import batch as batch_fixture

batch = batch_fixture


@pytest.mark.parametrize("batch", ["formatted-sme"], indirect=True)
@pytest.mark.parametrize("path", ["normal", "attachment"])
def test_encoded_sme_is_checked_before_description_or_attachment_writes(batch,path):
    import push_strategy
    from jira_utils import adf_to_markdown

    root, _, jira = batch
    command(root,"record-create")
    task = root / "artifacts/strat-tasks/RHAISTRAT-8901.md"
    comment = "## Step 1" if path == "normal" else "### Step 1"
    snippet = "\n\n### TL;DR\n```bash\n" + comment + "\necho ready\n```\n"
    task.write_text(task.read_text().replace("bounded request timeouts.","bounded request timeouts."+snippet))
    command(root,"mark","refine","RHAISTRAT-8901")
    before = jira.get("RHAISTRAT-8901")["fields"]["description"]
    if path == "normal":
        checked = subprocess.run([sys.executable,str(ROOT / "scripts/push_strategy.py"),"RHAISTRAT-8901",str(task)],
                                 cwd=root,capture_output=True,text=True,timeout=60)
        assert checked.returncode != 0,checked.stdout
        assert "SME input" in checked.stderr,checked.stderr
    else:
        with pytest.raises(ValueError,match="SME input"):
            push_strategy._push_via_attachment(jira.url,"admin","admin","RHAISTRAT-8901",
                adf_to_markdown(before),push_strategy.extract_strategy_section(task.read_text()),
                push_strategy.extract_staff_input_section(task.read_text()),[])
    fields = jira.get("RHAISTRAT-8901")["fields"]
    assert fields["description"] == before
    assert not fields.get("attachment")


@pytest.mark.parametrize("batch", ["leading-sme"], indirect=True)
def test_managed_push_validates_assembled_sme_before_writing(batch):
    root, _, jira = batch
    command(root,"record-create")
    command(root,"mark","refine","RHAISTRAT-8901")
    before = jira.get("RHAISTRAT-8901")["fields"]["description"]
    checked = subprocess.run([sys.executable,str(ROOT / "scripts/push_strategy.py"),"RHAISTRAT-8901",
                              str(root / "artifacts/strat-tasks/RHAISTRAT-8901.md")],
                             cwd=root,capture_output=True,text=True,timeout=60)
    assert checked.returncode != 0,checked.stdout
    assert "SME input" in checked.stderr,checked.stderr
    assert jira.get("RHAISTRAT-8901")["fields"]["description"] == before


@pytest.mark.parametrize("batch", ["formatted-sme"], indirect=True)
def test_formatting_only_sme_push_does_not_manufacture_deferral(batch):
    root, data, _ = batch
    command(root,"record-create")
    task = root / "artifacts/strat-tasks/RHAISTRAT-8901.md"
    task.write_text(task.read_text().replace("**shared queue**", "**shared ****queue**"))
    for key in ("RHAISTRAT-8901", "RHAISTRAT-8902"):
        command(root,"mark","refine",key)
    pushed = subprocess.run([sys.executable,str(ROOT / "scripts/push_refined_strategies.py")],
                             cwd=root,capture_output=True,text=True,timeout=60)
    assert pushed.returncode == 0,pushed.stderr
    command(root,"mark","push")
    progress = json.loads((root / "tmp/strat-progress.json").read_text())
    assert not progress.get("deferred"), progress
    command(root,"result")
    value = json.loads((root / "output/agent-result.json").read_text())
    assert value["action"] == "failed"  # Both fresh reviews are still required.
    assert host_check(root,data).returncode != 0
    for key in ("RHAISTRAT-8901", "RHAISTRAT-8902"):
        review(root,key)
    command(root,"result")
    value = json.loads((root / "output/agent-result.json").read_text())
    assert value["action"] == "completed" and len(value["strategies"]) == 2
    assert not value["deferred"]
    checked = host_check(root,data)
    assert checked.returncode == 0,checked.stdout+checked.stderr


@pytest.mark.parametrize("batch", ["old-gated"], indirect=True)
@pytest.mark.parametrize("forge_pairs", [False, True])
def test_old_closed_gates_cannot_prove_progress_only_exclusions(batch, forge_pairs):
    root, data, _ = batch
    command(root, "record-create")
    path = root / "tmp/strat-progress.json"
    progress = json.loads(path.read_text())
    for pair in progress["create"]["strategies"]:
        key, rfe = pair["strat"], pair["rfe"]
        old = key.replace("890", "870")
        task = root / f"artifacts/strat-tasks/{old}.md"
        task.write_text((task.parent / f"{key}.md").read_text().replace(key, old).replace("Refined", "Draft"))
        progress["freshness"]["targets"][old] = {"rfe":rfe, "human":{}}
        if forge_pairs:
            pair["strat"] = old
            del progress["freshness"]["targets"][key]
    path.write_text(json.dumps(progress))
    command(root, "result")
    result = json.loads((root / "output/agent-result.json").read_text())
    checked = host_check(root, data)
    assert checked.returncode != 0, checked.stdout
    assert result["action"] == "failed", result
    assert any("host-anchored" in error for error in result["errors"])


@pytest.mark.parametrize("batch", ["before-create"], indirect=True)
@pytest.mark.parametrize("operation", ["record", "push"])
def test_managed_pipeline_cannot_invent_sme_input(batch, operation):
    root, _, jira = batch
    command(root, "record-create")
    task = root / "artifacts/strat-tasks/RHAISTRAT-8901.md"
    task.write_text(task.read_text()+"\n## Staff Engineer / SME Input\nAgent-invented human guidance.\n")
    before = jira.get("RHAISTRAT-8901")["fields"]["description"]
    if operation == "record":
        checked = command(root, "mark", "refine", "RHAISTRAT-8901", check=False)
    else:
        checked = subprocess.run([sys.executable, str(ROOT / "scripts/push_strategy.py"),
                                  "RHAISTRAT-8901", str(task)],cwd=root,capture_output=True,text=True,timeout=60)
    assert checked.returncode != 0, checked.stdout
    assert "SME input" in checked.stderr, checked.stderr
    assert jira.get("RHAISTRAT-8901")["fields"]["description"] == before


def test_late_gate_during_review_recovery_continues_other_ticket(batch, monkeypatch):
    import publish_review
    import strat_pipeline_state as state
    from review_composition_support import prepare

    root, _, jira = batch
    for key in ("RHAISTRAT-8901", "RHAISTRAT-8902"):
        prepare(root,key,key.replace("RHAISTRAT", "RHAIRFE"),(1,1,1,1))
    original = publish_review.add_comment

    def intervene(server,user,token,key,body):
        if key == "RHAISTRAT-8901":
            jira.request("PUT", f"/rest/api/3/issue/{key}",
                         {"update":{"labels":[{"add":"strat-creator-human-sign-off"}]}})
        return original(server,user,token,key,body)

    monkeypatch.setattr(publish_review,"add_comment",intervene)
    monkeypatch.setenv("FULLSEND_OUTPUT_DIR", str(root / "output"))
    state.main(["prepare-reviews"])
    progress = json.loads((root / "tmp/strat-progress.json").read_text())
    assert "RHAISTRAT-8901" not in progress["review"]
    assert "RHAISTRAT-8902" in progress["review"]
    command(root,"result")
    result = json.loads((root / "output/agent-result.json").read_text())
    assert result["action"] == "completed", result["errors"]
    assert host_check(root, batch[1]).returncode == 0


def host_check(root, data):
    path = root / "host-run.json"
    path.write_text(json.dumps(data))
    return subprocess.run(
        [sys.executable, str(ROOT / ".fullsend/shared/validate_result.py"),
         "--result", str(root / "output/agent-result.json"), "--repo", str(root),
         "--schema", str(ROOT / ".fullsend/shared/result.schema.json"), "--run", str(path)],
        capture_output=True, text=True, timeout=60,
    )


@pytest.mark.parametrize("forged", ["staff", "summary", "priority", "status", "gate_labels", "published"])
def test_forged_strategy_snapshots_cannot_complete_a_batch(batch, forged):
    root, data, jira = batch
    path = root / "tmp/strat-progress.json"
    progress = json.loads(path.read_text())
    before = {key: jira.get(key)["fields"] for key in progress["freshness"]["targets"]}
    for key, target in progress["freshness"]["targets"].items():
        if forged == "published":
            progress["freshness"]["pushed"][key]["published"]["description"] = "invented-publication-hash"
        else:
            target["human"][forged] = ["invented-gate"] if forged == "gate_labels" else "invented-snapshot"
    path.write_text(json.dumps(progress))
    command(root, "result")
    result = json.loads((root / "output/agent-result.json").read_text())
    # The host must independently refuse the same exclusions, even if an agent
    # presents a completed envelope instead of the local failed result.
    if result["action"] == "failed":
        result.update(action="completed", errors=[], strategies=[], artifacts=[],
                      deferred=[{"key":rfe, "reason":"Claimed STRAT drift"} for rfe in data["acquired_keys"]],
                      completed_phases=["create", "refine", "push", "review"])
        (root / "output/agent-result.json").write_text(json.dumps(result))
    checked = host_check(root, data)
    assert checked.returncode != 0, checked.stdout + checked.stderr
    assert "host-anchored" in checked.stdout, checked.stdout + checked.stderr
    assert json.loads((root / "tmp/strat-input.json").read_text()) == data
    assert before == {key: jira.get(key)["fields"] for key in before}


@pytest.mark.parametrize("batch", ["before-create"], indirect=True)
def test_forged_create_snapshot_cannot_skip_refinement_and_review(batch):
    root, data, _ = batch
    command(root, "record-create")
    path = root / "tmp/strat-progress.json"
    progress = json.loads(path.read_text())
    assert not progress["refine"] and not progress["review"] and not progress.get("push")
    for target in progress["freshness"]["targets"].values():
        target["human"]["staff"] = "Fabricated old SME input."
    path.write_text(json.dumps(progress))
    command(root, "result")
    result = json.loads((root / "output/agent-result.json").read_text())
    assert result["action"] == "failed", result
    assert any("host-anchored" in error for error in result["errors"])
    assert host_check(root, data).returncode != 0


def test_other_rfe_change_does_not_prove_a_forged_strategy_mismatch(batch):
    from jira_utils import markdown_to_adf

    root, _, jira = batch
    jira.request("PUT", "/rest/api/3/issue/RHAIRFE-8902",
                 {"fields":{"description":markdown_to_adf("Genuine change on the other RFE.")}})
    path = root / "tmp/strat-progress.json"
    progress = json.loads(path.read_text())
    progress["freshness"]["targets"]["RHAISTRAT-8901"]["human"]["staff"] = "Invented previous SME input."
    path.write_text(json.dumps(progress))
    command(root, "result")
    result = json.loads((root / "output/agent-result.json").read_text())
    assert result["action"] == "failed"
    assert any("host-anchored" in error for error in result["errors"])


def test_our_numeric_attention_verdict_does_not_prove_forged_drift(batch):
    root, _, jira = batch
    review(root, "RHAISTRAT-8901")
    assert "strat-creator-needs-attention" in jira.get("RHAISTRAT-8901")["fields"]["labels"]
    path = root / "tmp/strat-progress.json"
    progress = json.loads(path.read_text())
    progress["freshness"]["targets"]["RHAISTRAT-8901"]["human"]["staff"] = "Invented previous SME input."
    path.write_text(json.dumps(progress))
    command(root, "result")
    result = json.loads((root / "output/agent-result.json").read_text())
    assert result["action"] == "failed"
    assert any("host-anchored" in error for error in result["errors"])


@pytest.mark.parametrize("batch", ["before-create"], indirect=True)
@pytest.mark.parametrize("new_strategy", [False, True])
@pytest.mark.parametrize("gate", ["strat-creator-human-sign-off", "strat-creator-needs-attention"])
def test_late_human_gate_isolates_its_ticket(batch, monkeypatch, gate, new_strategy):
    import push_refined_strategies as publisher

    root, data, jira = batch
    key = "RHAISTRAT-8901"
    if new_strategy:
        # Created after preparation: no host-pinned history cursor exists.
        old = root / f"artifacts/strat-tasks/{key}.md"
        key = "RHAISTRAT-8801"
        (old.parent / f"{key}.md").write_text(old.read_text().replace("RHAISTRAT-8901", key))
        old.unlink()
        jira.create(key, "New strategy", "Original business need.", issue_type="Feature",
                    labels=["strat-creator-auto-created"])
        jira.request("POST", "/rest/api/3/issueLink", {"type":{"name":"Cloners"},
                     "inwardIssue":{"key":key}, "outwardIssue":{"key":"RHAIRFE-8901"}})
        assert key not in data["freshness"]["strategy_history"]
    command(root, "record-create")
    for strat in (key, "RHAISTRAT-8902"):
        command(root, "mark", "refine", strat)
    original = publisher.add_labels

    def intervene(server, user, token, target, labels):
        if target == key:
            jira.request("PUT", f"/rest/api/3/issue/{key}", {"update":{"labels":[{"add":gate}]}})
        return original(server, user, token, target, labels)

    monkeypatch.setattr(publisher, "add_labels", intervene)
    monkeypatch.setattr(sys, "argv", ["push_refined_strategies.py"])
    assert publisher.main() == 0
    assert "strat-creator-auto-refined" not in jira.get(key)["fields"]["labels"]
    assert "strat-creator-auto-refined" in jira.get("RHAISTRAT-8902")["fields"]["labels"]
    command(root, "mark", "push")
    review(root, "RHAISTRAT-8902")
    command(root, "result")
    result = json.loads((root / "output/agent-result.json").read_text())
    assert result["action"] == "completed", result["errors"]
    assert [v["key"] for v in result["deferred"]] == ["RHAIRFE-8901"]
    checked = host_check(root, data)
    assert checked.returncode == 0, checked.stdout + checked.stderr


@pytest.mark.parametrize("batch", ["before-push"], indirect=True)
@pytest.mark.parametrize("fault", ["owner", "links", "source", "unreadable", "unrelated"])
def test_late_gate_does_not_hide_unrelated_failure(batch, monkeypatch, fault):
    import jira_utils
    import push_refined_strategies as publisher
    from strat_side_effects import owner_label

    root, _, jira = batch
    original = publisher.add_labels
    get_issue = jira_utils.get_issue

    def intervene(server, user, token, key, labels):
        if key == "RHAISTRAT-8901":
            jira.request("PUT", f"/rest/api/3/issue/{key}",
                         {"update":{"labels":[{"add":"strat-creator-human-sign-off"}]}})
            if fault == "owner":
                jira.request("PUT", "/rest/api/3/issue/RHAIRFE-8901",
                             {"fields":{"labels":["strat-creator-processing", owner_label("foreign-run")]}})
            elif fault == "links":
                jira.request("POST", "/rest/api/3/issueLink", {"type":{"name":"Cloners"},
                             "inwardIssue":{"key":key}, "outwardIssue":{"key":"RHAIRFE-8902"}})
            elif fault == "source":
                (root / "scripts/rules.py").write_text("# Changed shared source.\n")
            elif fault == "unreadable":
                def unavailable(server, user, token, target, **kwargs):
                    if target == "RHAIRFE-8901":
                        raise OSError("Synthetic unreadable owner")
                    return get_issue(server, user, token, target, **kwargs)
                monkeypatch.setattr(jira_utils, "get_issue", unavailable)
            else:
                raise ValueError("Unrelated label failure")
        return original(server, user, token, key, labels)

    monkeypatch.setattr(publisher, "add_labels", intervene)
    monkeypatch.setattr(sys, "argv", ["push_refined_strategies.py"])
    match = {"owner":"ownership", "links":"unique source RFE", "source":"source/instructions",
             "unreadable":"unreadable owner", "unrelated":"Unrelated label failure"}[fault]
    with pytest.raises((ValueError, RuntimeError, OSError), match=match):
        publisher.main()
    assert "strat-creator-auto-refined" not in jira.get("RHAISTRAT-8902")["fields"]["labels"]
