"""Creation and internal recorder failures retain ticket scope, not CLI exits."""

import json
import subprocess
import sys

import pytest
from review_composition_support import prepare
from test_batch_freshness_isolation import ROOT, command, review
from test_batch_freshness_isolation import batch as batch_fixture
from test_pr93_host_proof import host_check

batch = batch_fixture


def new_candidate(root, jira):
    old = root / "artifacts/strat-tasks/RHAISTRAT-8901.md"
    key = "RHAISTRAT-8801"
    (old.parent / f"{key}.md").write_text(old.read_text().replace("RHAISTRAT-8901", key))
    old.unlink()
    jira.create(key, "New independent strategy", "Original business need.", issue_type="Feature",
                labels=["strat-creator-auto-created"])
    jira.request("POST", "/rest/api/3/issueLink", {"type": {"name": "Cloners"},
                 "inwardIssue": {"key": key}, "outwardIssue": {"key": "RHAIRFE-8901"}})
    return key


def gate(jira, key, label="strat-creator-human-sign-off"):
    jira.request("PUT", f"/rest/api/3/issue/{key}", {"update": {"labels": [{"add": label}]}})


@pytest.mark.parametrize("batch", ["before-create"], indirect=True)
@pytest.mark.parametrize("label", ["strat-creator-human-sign-off", "strat-creator-needs-attention"])
@pytest.mark.parametrize("new", [False, True])
@pytest.mark.parametrize("when", ["before", "during"])
def test_creation_gate_retains_other_ticket_and_survives_retry(batch, monkeypatch, label, new, when):
    import strat_freshness
    import strat_pipeline_state as state

    root, data, jira = batch
    key = new_candidate(root, jira) if new else "RHAISTRAT-8901"
    assert not (root / "tmp/strat-progress.json").exists()
    if when == "before":
        gate(jira, key, label)
    else:
        original = strat_freshness.record

        def intervene(root, data, progress, phase, target, rfe):
            if phase == "create" and target == key:
                gate(jira, key, label)
            return original(root, data, progress, phase, target, rfe)

        monkeypatch.setattr(strat_freshness, "record", intervene)
    state.main(["record-create"])
    saved = json.loads((root / "tmp/strat-progress.json").read_text())
    assert [v["key"] for v in saved["deferred"]] == ["RHAIRFE-8901"]
    assert "RHAISTRAT-8902" in saved["freshness"]["targets"]
    assert key not in saved["freshness"]["targets"]  # No fabricated human snapshot.
    assert len(saved["create"]["strategies"]) == 2
    assert saved["create"]["pending"] == [] and saved["create"]["done_at"]
    command(root, "record-create")
    retried = json.loads((root / "tmp/strat-progress.json").read_text())
    assert retried["deferred"] == saved["deferred"]
    assert json.loads((root / "tmp/strat-input.json").read_text()) == data
    command(root, "mark", "refine", "RHAISTRAT-8902")
    pushed = subprocess.run([sys.executable, str(ROOT / "scripts/push_refined_strategies.py")],
                            cwd=root, capture_output=True, text=True, timeout=60)
    assert pushed.returncode == 0, pushed.stderr
    command(root, "mark", "push")
    review(root, "RHAISTRAT-8902")
    command(root, "result")
    result = json.loads((root / "output/agent-result.json").read_text())
    assert result["action"] == "completed", result["errors"]
    assert [v["strat"] for v in result["strategies"]] == ["RHAISTRAT-8902"]
    checked = host_check(root, data)
    assert checked.returncode == 0, checked.stdout + checked.stderr


def patch_recorder_verifier(monkeypatch, stage, intervene):
    import publish_review
    import review_composition
    import strat_pipeline_state as state

    module = review_composition if stage == "review" else publish_review
    name = "verify_review" if stage == "review" else "verify_publication"
    verify = getattr(module, name)
    recorder = state.cmd_record_review

    def wrapped(args):
        def verify_at_boundary(root, key, *a, **kw):
            if key == "RHAISTRAT-8901":
                intervene()
            return verify(root, key, *a, **kw)
        with monkeypatch.context() as scoped:
            scoped.setattr(module, name, verify_at_boundary)
            return recorder(args)

    monkeypatch.setattr(state, "cmd_record_review", wrapped)


def composed_pair(root):
    for key in ("RHAISTRAT-8901", "RHAISTRAT-8902"):
        prepare(root, key, key.replace("RHAISTRAT", "RHAIRFE"), (1, 1, 1, 1))


@pytest.mark.parametrize("stage", ["review", "publication"])
@pytest.mark.parametrize("event", ["rfe", "gate"])
def test_drift_inside_record_review_keeps_typed_scope_and_continues(batch, monkeypatch, stage, event):
    import strat_pipeline_state as state
    from jira_utils import markdown_to_adf

    root, data, jira = batch
    composed_pair(root)

    def intervene():
        if event == "gate":
            gate(jira, "RHAISTRAT-8901")
        else:
            jira.request("PUT", "/rest/api/3/issue/RHAIRFE-8901", {"fields": {
                "description": markdown_to_adf("New requirement during recorder verification.")}})

    patch_recorder_verifier(monkeypatch, stage, intervene)
    state.main(["prepare-reviews"])
    progress = json.loads((root / "tmp/strat-progress.json").read_text())
    assert "RHAISTRAT-8901" not in progress["review"]
    assert "RHAISTRAT-8902" in progress["review"]
    assert [v["key"] for v in progress["deferred"]] == ["RHAIRFE-8901"]
    command(root, "result")
    result = json.loads((root / "output/agent-result.json").read_text())
    assert result["action"] == "completed", result["errors"]
    checked = host_check(root, data)
    assert checked.returncode == 0, checked.stdout + checked.stderr


@pytest.mark.parametrize("batch", ["before-create"], indirect=True)
def test_gate_on_second_candidate_retains_the_first_snapshot(batch):
    root, _, jira = batch
    gate(jira, "RHAISTRAT-8902")
    command(root, "record-create")
    saved = json.loads((root / "tmp/strat-progress.json").read_text())
    assert [v["key"] for v in saved["deferred"]] == ["RHAIRFE-8902"]
    assert "RHAISTRAT-8901" in saved["freshness"]["targets"]
    assert "RHAISTRAT-8902" not in saved["freshness"]["targets"]


@pytest.mark.parametrize("batch", ["before-create"], indirect=True)
@pytest.mark.parametrize("fault", ["owner", "links", "source-mismatch", "history", "old-gate", "source", "wrong-scope"])
def test_creation_does_not_defer_unverifiable_or_unrelated_failures(batch, monkeypatch, capsys, fault):
    import strat_freshness
    import strat_history
    import strat_pipeline_state as state
    from strat_side_effects import owner_label

    root, data, jira = batch
    gate(jira, "RHAISTRAT-8901")
    if fault == "owner":
        jira.request("PUT", "/rest/api/3/issue/RHAIRFE-8901", {"fields": {
            "labels": ["strat-creator-processing", owner_label("foreign-run")]}})
    elif fault in ("links", "source-mismatch"):
        if fault == "source-mismatch":
            links = jira.get("RHAISTRAT-8901")["fields"]["issuelinks"]
            for link in links:
                if link["type"]["name"] == "Cloners":
                    jira.request("DELETE", f"/rest/api/3/issueLink/{link['id']}")
        jira.request("POST", "/rest/api/3/issueLink", {"type": {"name": "Cloners"},
                     "inwardIssue": {"key": "RHAISTRAT-8901"}, "outwardIssue": {"key": "RHAIRFE-8902"}})
    elif fault == "history":
        def unreadable(*args, **kwargs):
            raise OSError("Unavailable history")
        monkeypatch.setattr(strat_history, "histories", unreadable)
    elif fault == "old-gate":
        # Model a host preparation that already observed this gate, not new drift.
        data["freshness"].update(strat_history.initial_evidence(data["acquired_keys"]))
        (root / "tmp/strat-input.json").write_text(json.dumps(data))
    elif fault == "wrong-scope":
        from strat_freshness import StaleTicket
        def wrong_scope(*args):
            raise StaleTicket("RHAIRFE-8902", "Different RFE drift")
        monkeypatch.setattr(strat_freshness, "record", wrong_scope)
    else:
        original = strat_freshness.record
        def change_source(root, *args):
            (root / "scripts/rules.py").write_text("# Changed shared source.\n")
            return original(root, *args)
        monkeypatch.setattr(strat_freshness, "record", change_source)
    with pytest.raises((SystemExit, RuntimeError)) as raised:
        state.main(["record-create"])
    expected = {"owner": "ownership", "links": "unique source RFE", "source-mismatch": "creation mapping",
                "history": "independent change evidence", "old-gate": "independent change evidence",
                "source": "source/instructions", "wrong-scope": "Different RFE drift"}[fault]
    assert expected in str(raised.value) + capsys.readouterr().err
    assert not (root / "tmp/strat-progress.json").exists()


@pytest.mark.parametrize("stage", ["review", "publication"])
@pytest.mark.parametrize("kind", ["unrelated", "unproven-stale", "unproven-gate"])
def test_review_recorder_does_not_hide_unrelated_or_unproven_errors(batch, monkeypatch, capsys, stage, kind):
    import strat_pipeline_state as state
    from strat_freshness import StaleTicket
    from strat_side_effects import HumanGateChanged

    root = batch[0]
    composed_pair(root)
    def intervene():
        if kind == "unproven-stale":
            raise StaleTicket("RHAIRFE-8901", "Unproven stale claim")
        if kind == "unproven-gate":
            raise HumanGateChanged("Unproven gate claim")
        raise ValueError("Unrelated verification failure")
    patch_recorder_verifier(monkeypatch, stage, intervene)
    with pytest.raises(SystemExit):
        state.main(["prepare-reviews"])
    assert ("Unrelated" if kind == "unrelated" else "Unproven") in capsys.readouterr().err
    progress = json.loads((root / "tmp/strat-progress.json").read_text())
    assert not progress.get("deferred") and not progress["review"]


@pytest.mark.parametrize("batch", ["formatted-sme"], indirect=True)
@pytest.mark.parametrize("when", ["before", "during"])
def test_sme_drift_before_first_creation_snapshot_is_isolated(batch, monkeypatch, when):
    import strat_freshness
    import strat_pipeline_state as state
    from jira_utils import markdown_to_adf

    root, data, jira = batch
    content = "## Staff Engineer / SME Input\nUse the independently managed queue."
    def change_sme():
        jira.request("PUT", "/rest/api/3/issue/RHAISTRAT-8901", {"fields": {
            "description": markdown_to_adf(content)}})
    if when == "before":
        change_sme()
    else:
        original = strat_freshness.record
        def intervene(root, data, progress, phase, target, rfe):
            if phase == "create" and target == "RHAISTRAT-8901":
                change_sme()
            return original(root, data, progress, phase, target, rfe)
        monkeypatch.setattr(strat_freshness, "record", intervene)
    state.main(["record-create"])
    saved = json.loads((root / "tmp/strat-progress.json").read_text())
    assert [v["key"] for v in saved["deferred"]] == ["RHAIRFE-8901"]
    assert "RHAISTRAT-8901" not in saved["freshness"]["targets"]
    assert "RHAISTRAT-8902" in saved["freshness"]["targets"]
    command(root, "mark", "refine", "RHAISTRAT-8902")
    pushed = subprocess.run([sys.executable, str(ROOT / "scripts/push_refined_strategies.py")],
                            cwd=root, capture_output=True, text=True, timeout=60)
    assert pushed.returncode == 0, pushed.stderr
    command(root, "mark", "push")
    review(root, "RHAISTRAT-8902")
    command(root, "result")
    result = json.loads((root / "output/agent-result.json").read_text())
    assert result["action"] == "completed", result["errors"]
    assert host_check(root, data).returncode == 0
    assert jira.get("RHAISTRAT-8901")["fields"]["description"] == markdown_to_adf(content)


@pytest.mark.parametrize("batch", ["before-create"], indirect=True)
def test_local_sme_edit_before_first_record_does_not_invent_a_deferral(batch):
    root, _, jira = batch
    task = root / "artifacts/strat-tasks/RHAISTRAT-8901.md"
    task.write_text(task.read_text() + "\n## Staff Engineer / SME Input\nInvented local requirement.\n")
    before = jira.get("RHAISTRAT-8901")["fields"]["description"]
    checked = command(root, "record-create", check=False)
    assert checked.returncode != 0
    assert not (root / "tmp/strat-progress.json").exists()
    assert jira.get("RHAISTRAT-8901")["fields"]["description"] == before


def test_attention_added_at_publication_label_read_is_isolated(batch, monkeypatch):
    import publish_review
    import strat_pipeline_state as state

    root, data, jira = batch
    for key in ("RHAISTRAT-8901", "RHAISTRAT-8902"):
        prepare(root, key, key.replace("RHAISTRAT", "RHAIRFE"), (2, 2, 2, 2))
    recorder = state.cmd_record_review
    get_issue = publish_review.get_issue
    def wrapped(args):
        def read_labels(server, user, token, key, **kwargs):
            if key == "RHAISTRAT-8901" and kwargs.get("fields") == ["labels"]:
                gate(jira, key, "strat-creator-needs-attention")
            return get_issue(server, user, token, key, **kwargs)
        with monkeypatch.context() as scoped:
            scoped.setattr(publish_review, "get_issue", read_labels)
            return recorder(args)
    monkeypatch.setattr(state, "cmd_record_review", wrapped)
    state.main(["prepare-reviews"])
    saved = json.loads((root / "tmp/strat-progress.json").read_text())
    assert "RHAISTRAT-8901" not in saved["review"] and "RHAISTRAT-8902" in saved["review"]
    assert [v["key"] for v in saved["deferred"]] == ["RHAIRFE-8901"]
    command(root, "result")
    result = json.loads((root / "output/agent-result.json").read_text())
    assert result["action"] == "completed", result["errors"]
    checked = host_check(root, data)
    assert checked.returncode == 0, checked.stdout + checked.stderr
