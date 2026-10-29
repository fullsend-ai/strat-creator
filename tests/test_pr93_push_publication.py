"""Late push drift, untrusted identities, publication scope and read retries."""

import copy
import json
import shutil
import subprocess
import sys
from urllib.error import HTTPError

import pytest
from test_batch_freshness_isolation import ROOT, command, review
from test_batch_freshness_isolation import batch as batch_fixture
from test_pr93_host_proof import host_check
from test_pr93_recorder_boundaries import gate
from test_strat_pipeline_outcome import finished as finished_fixture

batch = batch_fixture
finished = finished_fixture


@pytest.mark.parametrize("batch", ["before-push"], indirect=True)
@pytest.mark.parametrize("label", ["strat-creator-human-sign-off", "strat-creator-needs-attention"])
def test_gate_during_first_push_snapshot_retains_other_ticket(batch, monkeypatch, label):
    import strat_freshness
    import strat_pipeline_state as state

    root, data, jira = batch
    pushed = subprocess.run([sys.executable, str(ROOT / "scripts/push_refined_strategies.py")],
                            cwd=root, capture_output=True, text=True, timeout=60)
    assert pushed.returncode == 0, pushed.stderr
    original = strat_freshness.record
    fired = []
    def intervene(root, data, progress, phase, key, rfe):
        if phase == "push" and key == "RHAISTRAT-8901" and not fired:
            fired.append(key)
            gate(jira, key, label)
        return original(root, data, progress, phase, key, rfe)
    monkeypatch.setattr(strat_freshness, "record", intervene)
    state.main(["mark", "push"])
    saved = json.loads((root / "tmp/strat-progress.json").read_text())
    assert fired == ["RHAISTRAT-8901"]
    assert [v["key"] for v in saved["deferred"]] == ["RHAIRFE-8901"]
    assert saved["push"]["strategies"] == ["RHAISTRAT-8902"]
    assert set(saved["freshness"]["pushed"]) == {"RHAISTRAT-8902"}
    command(root, "mark", "push")  # A retry must not resurrect the excluded target.
    review(root, "RHAISTRAT-8902")
    command(root, "result")
    result = json.loads((root / "output/agent-result.json").read_text())
    assert result["action"] == "completed", result["errors"]
    checked = host_check(root, data)
    assert checked.returncode == 0, checked.stdout + checked.stderr


def proof_fixture(monkeypatch, actual_key):
    import jira_utils
    import strat_history

    calls = []
    monkeypatch.setattr(strat_history, "histories", lambda key: [])
    monkeypatch.setattr(jira_utils, "require_env", lambda: ("https://fixture.invalid", "admin", "admin"))
    def issue(*args, **kwargs):
        calls.append(args[3])
        return {"key": actual_key, "fields": {"labels": ["strat-creator-human-sign-off"],
                "status": {"name": "Open"}, "issuelinks": [{"type": {"name": "Cloners"},
                "outwardIssue": {"key": "RHAIRFE-1"}}]}}
    monkeypatch.setattr(jira_utils, "get_issue", issue)
    data = {"freshness": {"live": True, "rfe_history": {},
            "clone_inventory": {"RHAIRFE-1": ["RHAISTRAT-1"]},
            "strategy_history": {"RHAISTRAT-1": {"rfe": "RHAIRFE-1", "cursor": None,
                                 "gate_labels": ["strat-creator-human-sign-off"], "status": "Open"}}}}
    return strat_history, data, calls


@pytest.mark.parametrize("key", ["RHAISTRAT-1?alias=", "RHAISTRAT-1#alias", "RHAISTRAT-1/other",
                                  "RHAISTRAT-1\n", "RHAISTRAT-١"])
def test_alias_keys_cannot_turn_a_preexisting_gate_into_change_proof(tmp_path, monkeypatch, key):
    history, data, calls = proof_fixture(monkeypatch, "RHAISTRAT-1")
    progress = {"create": {"strategies": [{"rfe": "RHAIRFE-1", "strat": key}]}}
    assert not history.proves_change(tmp_path, data, progress, "RHAIRFE-1")
    assert calls == []


@pytest.mark.parametrize("returned", [None, "RHAISTRAT-1", "RHAISTRAT-2"])
def test_change_evidence_requires_exact_returned_issue_identity(tmp_path, monkeypatch, returned):
    history, data, calls = proof_fixture(monkeypatch, returned)
    progress = {"create": {"strategies": [{"rfe": "RHAIRFE-1", "strat": "RHAISTRAT-2"}]}}
    assert history.proves_change(tmp_path, data, progress, "RHAIRFE-1") is (returned == "RHAISTRAT-2")
    assert calls == ["RHAISTRAT-2"]


@pytest.mark.parametrize("key", ["RHAISTRAT-1?alias=", "STRAT-1\n", "RHAISTRAT-١"])
def test_deferred_create_pairs_still_require_valid_keys(finished, key):
    import strat_pipeline_outcome as outcome

    root, data, progress, result, _ = finished
    deferred = [{"key": "RHAIRFE-9601", "reason": "Proven input change"}]
    progress["create"]["strategies"][0]["strat"] = key
    progress.update(refine={}, review={}, deferred=deferred,
                    push={"done_at": progress["create"]["done_at"], "strategies": [], "skipped": True})
    result.update(strategies=[], artifacts=[], deferred=deferred)
    assert any("invalid strategy key" in e for e in outcome._business_errors(root, data, progress, result))


@pytest.mark.parametrize("kind", ["task", "review", "original"])
def test_host_rejects_renamed_copy_of_deferred_artifact(batch, kind):
    from jira_utils import markdown_to_adf
    from strat_pipeline_outcome import finalize

    root, data, jira = batch
    for key in ("RHAISTRAT-8901", "RHAISTRAT-8902"):
        review(root, key)
    jira.request("PUT", "/rest/api/3/issue/RHAIRFE-8902", {"fields": {
        "description": markdown_to_adf("Changed input for the deferred strategy.")}})
    command(root, "result")
    path = root / "output/agent-result.json"
    result = json.loads(path.read_text())
    assert result["action"] == "completed", result["errors"]
    assert [p["strat"] for p in result["strategies"]] == ["RHAISTRAT-8901"]
    assert host_check(root, data).returncode == 0
    source = {"task": "strat-tasks/RHAISTRAT-8902.md",
              "review": "strat-reviews/RHAISTRAT-8902-review.md",
              "original": "strat-originals/RHAIRFE-8902.md"}[kind]
    extra = {"task": "strat-tasks/RHAISTRAT-9999.md",
             "review": "strat-reviews/RHAISTRAT-9999-review.md",
             "original": "strat-originals/RHAIRFE-9999.md"}[kind]
    shutil.copyfile(root / "artifacts" / source, root / "artifacts" / extra)
    result["artifacts"].append("artifacts/" + extra)
    progress = json.loads((root / "tmp/strat-progress.json").read_text())
    # The agent can regenerate its own unsigned completion receipt. Exercise
    # business validation, rather than merely breaking an existing checksum.
    finalize(root, data, progress, result, [], path)
    checked = host_check(root, data)
    assert checked.returncode != 0, checked.stdout + checked.stderr
    assert "outside the completed ticket inventory" in checked.stdout


def test_changed_canonical_rfe_original_still_fails_existing_provenance(finished):
    import strat_pipeline_outcome as outcome

    root, data, progress, result, _ = finished
    path = root / "artifacts/strat-originals/RHAIRFE-9601.md"
    path.parent.mkdir(exist_ok=True)
    path.write_text("---\nkey: RHAIRFE-9999\n---\nOriginal requirement.\n")
    result["artifacts"].append(str(path.relative_to(root)))
    assert any("Review provenance/input identity disagrees" in e
               for e in outcome._business_errors(root, data, progress, result))


@pytest.mark.parametrize("kind", ["strat-original", "rfe-comments", "comment"])
def test_optional_completed_path_still_checks_embedded_or_derived_identity(finished, kind):
    import strat_pipeline_outcome as outcome
    import strat_pipeline_state as state
    from review_composition_support import prepare

    root, data, progress, result, _ = finished
    relative, content = {
        "strat-original": ("strat-originals/STRAT-9601.md", "---\nstrat_id: STRAT-9999\n---\nOther strategy.\n"),
        "rfe-comments": ("strat-originals/RHAIRFE-9601-comments.md", "---\njira_key: RHAIRFE-9999\n---\nComments.\n"),
        "comment": ("strat-reviews/STRAT-9601-review-comment.md", "Other ticket's publication summary.\n"),
    }[kind]
    path = root / "artifacts" / relative
    path.parent.mkdir(exist_ok=True)
    path.write_text(content)
    result["artifacts"].append(str(path.relative_to(root)))
    if kind == "strat-original":
        started, _ = prepare(root, "STRAT-9601", "RHAIRFE-9601", (1, 1, 1, 1))
        progress["review"]["STRAT-9601"]["composition_session"] = started["session_id"]
        result = state.assemble(data, progress, [])
    expected = "review comment identity/content" if kind == "comment" else "original artifact identity mismatch"
    assert any(expected in e for e in outcome._business_errors(root, data, progress, result))


@pytest.mark.parametrize("name", ["RHAIRFE-9601.md", "RHAIRFE-9601-comments.md", "STRAT-9601.md"])
def test_native_jira_markdown_rules_are_not_mandatory_yaml_metadata(finished, name):
    import strat_pipeline_outcome as outcome
    import strat_pipeline_state as state
    from review_composition_support import prepare

    root, data, progress, _, _ = finished
    path = root / "artifacts/strat-originals" / name
    path.parent.mkdir(exist_ok=True)
    content = "---\n* ordinary Markdown list\nSome unquoted text: [\n---\nMore Jira description.\n"
    path.write_text(content)
    started, _ = prepare(root, "STRAT-9601", "RHAIRFE-9601", (1, 1, 1, 1),
                         source_text=content if name == "RHAIRFE-9601.md" else None)
    progress["review"]["STRAT-9601"]["composition_session"] = started["session_id"]
    result = state.assemble(data, progress, [])
    assert path.read_text() == content
    assert not outcome._business_errors(root, data, progress, result)


def test_original_strategy_added_after_review_is_not_an_unbound_publication_slot(finished):
    import strat_pipeline_outcome as outcome

    root, data, progress, result, _ = finished
    path = root / "artifacts/strat-originals/STRAT-9601.md"
    path.parent.mkdir(exist_ok=True)
    path.write_text("Other ticket's raw original, copied after the review.\n")
    result["artifacts"].append(str(path.relative_to(root)))
    assert any("Review provenance/input identity disagrees" in e
               for e in outcome._business_errors(root, data, progress, result))


@pytest.mark.parametrize("target", ["expanded", "page"])
def test_both_history_reads_retry_transient_errors(monkeypatch, target):
    import jira_utils
    import strat_history

    calls = []
    marker = {"id": "1", "items": []}
    monkeypatch.setattr(jira_utils, "require_env", lambda: ("https://fixture.invalid", "admin", "admin"))
    monkeypatch.setattr(jira_utils.time, "sleep", lambda seconds: None)
    def api(server, path, user, token, *args):
        part = "expanded" if "expand=" in path else "page"
        calls.append(part)
        if part == target and calls.count(part) == 1:
            raise HTTPError(path, 503, "Temporary failure", {}, None)
        return ({"changelog": {"total": 1, "startAt": 0, "histories": []}} if part == "expanded"
                else {"startAt": 0, "values": [copy.deepcopy(marker)]})
    monkeypatch.setattr(jira_utils, "api_call", api)
    assert strat_history.histories("RHAIRFE-1") == [marker]
    assert calls.count(target) == 2


def test_exhausted_history_retries_remain_unavailable(monkeypatch):
    import jira_utils
    import strat_history

    attempts = []
    monkeypatch.setattr(jira_utils, "require_env", lambda: ("https://fixture.invalid", "admin", "admin"))
    monkeypatch.setattr(jira_utils.time, "sleep", lambda seconds: None)
    def api(server, path, user, token, *args):
        attempts.append(path)
        raise HTTPError(path, 503, "Still unavailable", {}, None)
    monkeypatch.setattr(jira_utils, "api_call", api)
    assert strat_history.cursor("RHAIRFE-1") is None
    assert len(attempts) == 3


@pytest.mark.parametrize("batch", ["before-push"], indirect=True)
@pytest.mark.parametrize("event", ["gate", "rfe", "sme"])
@pytest.mark.parametrize("when", ["first", "later"])
def test_changes_during_push_recording_have_independent_ticket_scope(batch, monkeypatch, event, when):
    import strat_freshness
    import strat_pipeline_state as state
    from jira_utils import adf_to_markdown, markdown_to_adf

    root, data, jira = batch
    pushed = subprocess.run([sys.executable, str(ROOT / "scripts/push_refined_strategies.py")],
                            cwd=root, capture_output=True, text=True, timeout=60)
    assert pushed.returncode == 0, pushed.stderr
    original = strat_freshness.record
    fired = []
    def record(root, data, progress, phase, key, rfe):
        target = "RHAISTRAT-8901" if when == "first" else "RHAISTRAT-8902"
        if phase == "push" and key == target and not fired:
            fired.append(key)
            if event == "gate":
                gate(jira, "RHAISTRAT-8901")
            else:
                issue = "RHAIRFE-8901" if event == "rfe" else "RHAISTRAT-8901"
                text = ("Changed business requirement." if event == "rfe" else
                        adf_to_markdown(jira.get(issue)["fields"]["description"]).split(
                            "## Staff Engineer / SME Input", 1)[0].rstrip() +
                        "\n\n## Staff Engineer / SME Input\nUse the newly required storage backend.")
                jira.request("PUT", f"/rest/api/3/issue/{issue}", {"fields": {"description": markdown_to_adf(text)}})
        return original(root, data, progress, phase, key, rfe)
    monkeypatch.setattr(strat_freshness, "record", record)
    state.main(["mark", "push"])
    saved = json.loads((root / "tmp/strat-progress.json").read_text())
    assert fired
    assert [v["key"] for v in saved["deferred"]] == ["RHAIRFE-8901"]
    assert saved["push"]["strategies"] == ["RHAISTRAT-8902"]
    review(root, "RHAISTRAT-8902")
    command(root, "result")
    checked = host_check(root, data)
    assert checked.returncode == 0, checked.stdout + checked.stderr


@pytest.mark.parametrize("batch", ["before-push"], indirect=True)
@pytest.mark.parametrize("fault", ["stale", "gate", "unrelated", "shared", "artifact", "scope"])
def test_push_recording_does_not_hide_unproven_or_unrelated_failures(batch, monkeypatch, capsys, fault):
    import strat_freshness
    import strat_pipeline_state as state
    from strat_side_effects import HumanGateChanged

    root, _, jira = batch
    path = root / "tmp/strat-progress.json"
    before = path.read_bytes()
    original = strat_freshness.record
    def record(root, data, progress, phase, key, rfe):
        if key == "RHAISTRAT-8901":
            if fault in ("stale", "scope"):
                if fault == "scope":
                    gate(jira, key)
                raise strat_freshness.StaleTicket("RHAIRFE-8902" if fault == "scope" else rfe, "Unproven scope")
            if fault == "gate":
                raise HumanGateChanged("Unproven gate")
            if fault == "unrelated":
                raise ValueError("Unrelated recorder error")
            if fault == "shared":
                (root / "scripts/rules.py").write_text("# Changed shared rules.\n")
            if fault == "artifact":
                task = root / f"artifacts/strat-tasks/{key}.md"
                task.write_text(task.read_text() + "\nUnrecorded local edit.\n")
        return original(root, data, progress, phase, key, rfe)
    monkeypatch.setattr(strat_freshness, "record", record)
    with pytest.raises(SystemExit):
        state.main(["mark", "push"])
    error = capsys.readouterr().err
    expected = {"stale": "Unproven", "scope": "Unproven", "gate": "Unproven", "unrelated": "Unrelated",
                "shared": "source/instructions", "artifact": "strategy changed"}[fault]
    assert expected in error
    assert path.read_bytes() == before
